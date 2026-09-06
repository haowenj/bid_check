from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata
from app.objective_scoring import load_reusable_bid_evidence

logger = logging.getLogger(__name__)

SUBJECTIVE_SCORE_ARTIFACT = "subjective_scores.json"
SUBJECTIVE_STATUSES = frozenset(
    {"ai_scored", "file_scope_missing", "evidence_insufficient", "llm_error"}
)
SUBJECTIVE_MAX_WORKERS = 5


class SubjectiveScoringError(RuntimeError):
    """Raised when a subjective scoring call or response is unusable."""


class SubjectiveScoreLLM(Protocol):
    model: str
    available: bool

    def score(
        self,
        score_item: Mapping[str, Any],
        matched_bid_content: Sequence[Mapping[str, Any]],
        allowed_bands: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...

_NAVIGATION_TERMS = ("索引", "目录", "对应页码", "页码")
_TECHNICAL_SCOPE_TERMS = (
    "技术标",
    "技术投标文件",
    "技术响应",
    "技术方案",
    "技术标准和要求响应",
)
_SERVICE_SCOPE_TERMS = (
    "服务方案",
    "服务质量保障措施",
    "质量服务保障措施",
    "服务支撑方案",
)
_TECHNICAL_ITEM_TERMS = (
    "项目需求的分析及理解",
    "云网产品",
    "大模型技术支持",
    "技术标准和要求",
    "技术响应",
)
_SERVICE_ITEM_TERMS = (
    "质量服务保障",
    "服务质量保障",
    "服务方案",
)
_SCORE_ITEM_001_ID = "score_item_001"
_SCORE_ITEM_001_DEDUCTIONS = (
    "未按规定制作投标文件",
    "文件内容错误",
    "文件内容模糊不清",
    "材料缺失",
    "标书阅读困难",
)
_QUALITY_ABSENT_STATUSES = frozenset(
    {"pass", "absent", "confirmed_absent", "not_applicable", "not_found"}
)
_QUALITY_PRESENT_STATUSES = frozenset(
    {"fail", "present", "confirmed_present", "exists", "issue"}
)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _normalized(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", _as_text(value)).lower()


def _document_sections(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    raw_sections = document.get("sections", [])
    if not isinstance(raw_sections, list):
        return []
    return [dict(section) for section in raw_sections if isinstance(section, Mapping)]


def _document_blocks(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    raw_blocks = document.get("blocks", [])
    if not isinstance(raw_blocks, list):
        return []
    return [dict(block) for block in raw_blocks if isinstance(block, Mapping)]


def _section_texts(document: Mapping[str, Any] | None) -> list[str]:
    values: list[str] = []
    for section in _document_sections(document):
        values.append(_as_text(section.get("title")))
        path = section.get("path")
        if isinstance(path, list):
            values.extend(_as_text(part) for part in path)
    return [value for value in values if value.strip()]


def _is_navigation_block(block: Mapping[str, Any]) -> bool:
    values = [
        _as_text(block.get("section")),
        _as_text(block.get("section_title")),
    ]
    path = block.get("section_path", block.get("path"))
    if isinstance(path, list):
        values.extend(_as_text(part) for part in path)
    return any(term in " ".join(values) for term in _NAVIGATION_TERMS)


def _is_business_only(
    document: Mapping[str, Any] | None,
    filename: str,
) -> bool:
    section_text = "\n".join(_section_texts(document))
    has_business_signal = "商务" in filename or "商务" in section_text
    has_other_scope = any(
        term in filename or term in section_text
        for term in (*_TECHNICAL_SCOPE_TERMS, *_SERVICE_SCOPE_TERMS)
    )
    return has_business_signal and not has_other_scope


def _item_scope(item: Mapping[str, Any]) -> str | None:
    item_id = _as_text(item.get("id"))
    item_name = _as_text(item.get("name"))
    combined = f"{item_id} {item_name}"
    if item_id in {"score_item_003", "score_item_004", "score_item_005", "score_item_006"}:
        return "technical"
    if item_id == "score_item_009":
        return "service"
    if any(term in combined for term in _TECHNICAL_ITEM_TERMS):
        return "technical"
    if any(term in combined for term in _SERVICE_ITEM_TERMS):
        return "service"
    if "投标文件编写质量" in combined:
        return "business"
    return None


def _scope_available(
    scope: str | None,
    document: Mapping[str, Any] | None,
    filename: str,
) -> bool:
    if scope in {None, "business"}:
        return True
    scope_terms = (
        _TECHNICAL_SCOPE_TERMS if scope == "technical" else _SERVICE_SCOPE_TERMS
    )
    filename_text = _normalized(filename)
    section_text = _normalized("\n".join(_section_texts(document)))
    return any(
        _normalized(term) in filename_text or _normalized(term) in section_text
        for term in scope_terms
    )


def _keyword_phrases(item: Mapping[str, Any]) -> list[str]:
    phrases: list[str] = []
    for value in [_as_text(item.get("name"))]:
        if value.strip():
            phrases.append(value)
    evidence_requirements = item.get("evidence_requirements", [])
    if isinstance(evidence_requirements, list):
        phrases.extend(
            _as_text(value)
            for value in evidence_requirements
            if _as_text(value).strip()
        )
    return list(dict.fromkeys(_normalized(value) for value in phrases if value.strip()))


def _compact_block(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": _as_text(block.get("block_id")),
        "section": _as_text(block.get("section")),
        "type": _as_text(block.get("type")) or "paragraph",
        "text": _as_text(block.get("text")),
        "order": block.get("order"),
    }


def select_subjective_items(
    evaluation_rules: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_items = evaluation_rules.get("score_items", [])
    if not isinstance(raw_items, list):
        return []
    return [
        dict(item)
        for item in raw_items
        if isinstance(item, Mapping)
        and item.get("evaluation_type") == "subjective"
    ]


def match_subjective_bid_content(
    item: Mapping[str, Any],
    bid_document: Mapping[str, Any] | None,
    bid_filename: str,
) -> dict[str, Any]:
    scope = _item_scope(item)
    if scope in {"technical", "service"} and not _scope_available(
        scope, bid_document, bid_filename
    ):
        return {
            "status": "file_scope_missing",
            "scope": scope,
            "reason": (
                "当前商务投标文件范围未包含该评分项所需的技术标/技术响应内容。"
                if scope == "technical"
                else "当前投标文件范围未包含该评分项所需的服务方案或质量服务保障措施。"
            ),
            "blocks": [],
        }

    phrases = _keyword_phrases(item)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, raw_block in enumerate(_document_blocks(bid_document)):
        if _is_navigation_block(raw_block):
            continue
        block = _compact_block(raw_block)
        if not block["block_id"] or not block["text"].strip():
            continue
        haystack = _normalized(
            f"{block['section']} {block['text']}"
        )
        matched_phrases = [phrase for phrase in phrases if phrase in haystack]
        if not matched_phrases:
            continue
        score = max(
            100 if phrase == _normalized(_as_text(item.get("name"))) else 80
            for phrase in matched_phrases
        )
        score += min(15, len(matched_phrases) * 3)
        order = block["order"] if isinstance(block["order"], int) else index
        ranked.append((score, order, block))

    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    selected = [entry[2] for entry in ranked[:12]]
    selected.sort(
        key=lambda block: block["order"]
        if isinstance(block["order"], int)
        else 0
    )
    if not selected:
        return {
            "status": "evidence_insufficient",
            "scope": scope,
            "reason": "当前已解析投标文件中没有定位到该评分项对应的正文 block。",
            "blocks": [],
        }
    return {
        "status": "matched",
        "scope": scope,
        "reason": "已从现有结构化投标内容定位到评分项对应 block。",
        "blocks": selected,
    }


_INTERVAL_RE = re.compile(
    r"([\[\(])\s*([0-9]+(?:\.[0-9]+)?)\s*[,，]\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*([\]\)])"
)


def _numeric(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def _full_score(item: Mapping[str, Any]) -> float | int | None:
    return _numeric(item.get("full_score"))


def _rule_text(item: Mapping[str, Any]) -> str:
    values = [
        _as_text(item.get("original_rule")),
        _as_text(item.get("conditions")),
        _as_text(item.get("scoring_method")),
    ]
    for key in ("conditions", "scoring_method"):
        value = item.get(key)
        if isinstance(value, (Mapping, list)):
            values.append(json.dumps(value, ensure_ascii=False))
    return " ".join(value for value in values if value)


def _band_number(value: str) -> float | int:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _allowed_score_bands(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    text = _rule_text(item)
    bands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _INTERVAL_RE.finditer(text):
        label = f"{match.group(1)}{match.group(2)},{match.group(3)}{match.group(4)}"
        if label in seen:
            continue
        seen.add(label)
        bands.append(
            {
                "label": label,
                "min": _band_number(match.group(2)),
                "max": _band_number(match.group(3)),
                "min_inclusive": match.group(1) == "[",
                "max_inclusive": match.group(4) == "]",
            }
        )
    if "未提供" in text or re.search(r"(?:^|[^0-9])0\s*分", text):
        bands.append(
            {
                "label": "0",
                "min": 0,
                "max": 0,
                "min_inclusive": True,
                "max_inclusive": True,
            }
        )
    if "扣" in text:
        bands.append(
            {
                "label": "扣分规则",
                "min": 0,
                "max": _full_score(item),
                "min_inclusive": True,
                "max_inclusive": True,
            }
        )
    if not bands:
        max_score = _full_score(item)
        bands.append(
            {
                "label": "规则范围",
                "min": 0,
                "max": max_score,
                "min_inclusive": True,
                "max_inclusive": True,
            }
        )
    return bands


def _band_contains(band: Mapping[str, Any], score: float) -> bool:
    minimum = _numeric(band.get("min"))
    maximum = _numeric(band.get("max"))
    if minimum is not None:
        if band.get("min_inclusive", False):
            if score < float(minimum):
                return False
        elif score <= float(minimum):
            return False
    if maximum is not None:
        if band.get("max_inclusive", False):
            if score > float(maximum):
                return False
        elif score >= float(maximum):
            return False
    return True


def _uncertainty(value: Any, *, default_note: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        level = _as_text(value.get("level")) or "medium"
        notes = value.get("notes", [])
        normalized_notes = (
            [_as_text(note) for note in notes if _as_text(note).strip()]
            if isinstance(notes, list)
            else [_as_text(notes)] if _as_text(notes).strip() else []
        )
        if default_note:
            normalized_notes.append(default_note)
        return {"level": level, "notes": normalized_notes}
    notes = [default_note] if default_note else []
    return {"level": "medium", "notes": notes}


def _base_result(
    item: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    matched_blocks: Sequence[Mapping[str, Any]] = (),
    uncertainty: Any = None,
) -> dict[str, Any]:
    if status not in SUBJECTIVE_STATUSES:
        raise ValueError(f"unknown subjective score status: {status}")
    compact_blocks = [dict(block) for block in matched_blocks]
    block_ids = [
        _as_text(block.get("block_id"))
        for block in compact_blocks
        if _as_text(block.get("block_id"))
    ]
    result = {
        "score_item_id": _as_text(item.get("id")),
        "rule_name": _as_text(item.get("name")),
        "max_score": _full_score(item),
        "status": status,
        "score_band": None,
        "recommended_score": None,
        "reason": reason,
        "matched_bid_content": compact_blocks,
        "evidence": [],
        "block_ids": block_ids,
        "uncertainty": _uncertainty(uncertainty, default_note=reason),
    }
    deduction_checks = item.get("deduction_checks")
    if isinstance(deduction_checks, list):
        result["deduction_checks"] = [
            dict(check) for check in deduction_checks if isinstance(check, Mapping)
        ]
    return result


def _quality_review_status(entry: Mapping[str, Any]) -> str:
    for key in ("final_status", "business_status", "status"):
        value = _as_text(entry.get(key)).strip().lower()
        if value:
            return value
    return ""


def _quality_review_entries(
    artifacts: Mapping[str, Any],
    artifact_name: str,
    *keys: str,
) -> list[dict[str, Any]]:
    payload = artifacts.get(artifact_name)
    if not isinstance(payload, Mapping):
        return []
    entries: list[dict[str, Any]] = []
    for key in keys:
        raw_entries = payload.get(key, [])
        if not isinstance(raw_entries, list):
            continue
        entries.extend(
            dict(entry) for entry in raw_entries if isinstance(entry, Mapping)
        )
    return entries


def _quality_stats(
    artifacts: Mapping[str, Any],
    artifact_name: str,
) -> Mapping[str, Any]:
    payload = artifacts.get(artifact_name)
    stats = payload.get("stats") if isinstance(payload, Mapping) else None
    return stats if isinstance(stats, Mapping) else {}


def _quality_coverage_complete(
    artifacts: Mapping[str, Any],
    artifact_name: str,
    entries: Sequence[Mapping[str, Any]],
) -> bool:
    stats = _quality_stats(artifacts, artifact_name)
    expected = stats.get("participating_template_count")
    selected = stats.get("selected_template_count")
    if not isinstance(expected, int) or not isinstance(selected, int):
        return False
    if expected <= 0 or selected < expected or len(entries) < expected:
        return False
    if any(
        isinstance(stats.get(key), int) and stats.get(key, 0) > 0
        for key in ("semantic_uncertain_count", "semantic_mismatched_count")
    ):
        return False
    no_bid_candidates = stats.get("no_bid_candidate_template_ids", [])
    if isinstance(no_bid_candidates, list) and no_bid_candidates:
        return False
    for entry in entries:
        if _quality_review_status(entry) not in _QUALITY_ABSENT_STATUSES:
            return False
        if entry.get("final_issues") or entry.get("issues"):
            return False
        requirements = entry.get("requirements", [])
        if isinstance(requirements, list) and any(
            isinstance(requirement, Mapping)
            and _quality_review_status(requirement) not in _QUALITY_ABSENT_STATUSES
            for requirement in requirements
        ):
            return False
    return True


def _quality_issue_entries(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for entry in entries:
        for key in (
            "final_issues",
            "issues",
            "deterministic_placeholder_residuals",
            "requirements",
        ):
            raw_issues = entry.get(key, [])
            if not isinstance(raw_issues, list):
                continue
            issues.extend(
                dict(issue) for issue in raw_issues if isinstance(issue, Mapping)
            )
    return issues


def _quality_issue_category(value: Mapping[str, Any]) -> str | None:
    """Map one upstream issue to at most one writing-quality deduction."""
    issue_type = _normalized(
        value.get("type") or value.get("issue_type") or value.get("category")
    )
    typed_categories = (
        (
            "readability",
            ("reading_difficulty", "readability", "阅读困难", "无法阅读"),
        ),
        ("clarity", ("blur", "模糊", "不清", "ocr_failed", "清晰度")),
        (
            "material",
            (
                "missing_attachment",
                "missing_material",
                "missing_content",
                "material_missing",
                "材料缺失",
                "附件缺失",
                "未提供",
            ),
        ),
        (
            "content",
            (
                "missing_fill",
                "placeholder",
                "content_error",
                "content_mismatch",
                "wrong_value",
                "invalid_value",
                "semantic_error",
                "内容错误",
                "内容不一致",
                "占位符",
                "提示文字",
            ),
        ),
        (
            "format",
            (
                "format_error",
                "format_mismatch",
                "production_rule",
                "文件格式",
                "格式错误",
                "排版错误",
                "未按规定制作",
                "制作规范",
                "装订",
                "页码顺序",
                "签章格式",
            ),
        ),
    )
    for category, terms in typed_categories:
        if any(term in issue_type for term in terms):
            return category

    semantic_text = _normalized(
        " ".join(
            _quality_value_text(value.get(key))
            for key in (
                "type",
                "issue_type",
                "category",
                "name",
                "title",
                "description",
                "message",
                "reason",
                "requirement",
            )
        )
    )
    for category, terms in typed_categories:
        if any(term in semantic_text for term in terms):
            return category
    return None


def _quality_value_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return _as_text(value)


def _quality_block_ids(value: Any) -> list[str]:
    block_ids: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                normalized_key = _as_text(key).lower()
                if normalized_key in {"block_id", "bid_block_id"}:
                    block_id = _as_text(child).strip()
                    if block_id:
                        block_ids.add(block_id)
                elif normalized_key in {"block_ids", "bid_block_ids"}:
                    if isinstance(child, list):
                        block_ids.update(
                            _as_text(block_id).strip()
                            for block_id in child
                            if _as_text(block_id).strip()
                        )
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return sorted(block_ids)


def _quality_quote(value: Mapping[str, Any]) -> str:
    for key in (
        "quote",
        "bid_text",
        "actual",
        "message",
        "summary",
        "reason",
        "requirement",
    ):
        text = _as_text(value.get(key)).strip()
        if text:
            return text[:800]
    return ""


def _quality_artifact_evidence(
    artifact_name: str,
    value: Mapping[str, Any],
    document: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    nested_values: list[Mapping[str, Any]] = [value]
    for key in (
        "final_issues",
        "issues",
        "deterministic_placeholder_residuals",
        "requirements",
    ):
        raw_values = value.get(key, [])
        if isinstance(raw_values, list):
            nested_values.extend(
                child for child in raw_values if isinstance(child, Mapping)
            )
    for child in nested_values[:5]:
        reference: dict[str, Any] = {"artifact": artifact_name}
        block_ids = _quality_block_ids(child)
        if block_ids:
            reference["block_ids"] = block_ids
        quote = _quality_quote(child)
        if quote:
            reference["quote"] = quote
            if "block_ids" not in reference and isinstance(document, Mapping):
                matched_block_ids = [
                    _as_text(block.get("block_id"))
                    for block in _document_blocks(document)
                    if _as_text(block.get("block_id"))
                    and _normalized(quote)
                    in _normalized(block.get("text"))
                ]
                if matched_block_ids:
                    reference["block_ids"] = matched_block_ids[:3]
        if "template_id" in child:
            reference["record_id"] = _as_text(child.get("template_id"))
        if "requirement" in child:
            reference["requirement"] = _as_text(child.get("requirement"))
        evidence.append(reference)
    return evidence


def _quality_structured_checks(
    document: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(document, Mapping):
        return {}
    raw_checks: Any = document.get("quality_checks")
    diagnostics = document.get("diagnostics")
    if raw_checks is None and isinstance(diagnostics, Mapping):
        raw_checks = diagnostics.get("quality_checks")
    if raw_checks is None:
        stats = document.get("stats")
        if isinstance(stats, Mapping):
            raw_checks = stats.get("quality_checks")
    checks: list[dict[str, Any]] = []
    if isinstance(raw_checks, list):
        checks = [dict(check) for check in raw_checks if isinstance(check, Mapping)]
    elif isinstance(raw_checks, Mapping):
        for name, check in raw_checks.items():
            if isinstance(check, Mapping):
                entry = dict(check)
                entry.setdefault("name", _as_text(name))
                checks.append(entry)
    return {
        _normalized(check.get("name") or check.get("deduction_item")): check
        for check in checks
        if _normalized(check.get("name") or check.get("deduction_item"))
    }


def _quality_structured_evidence(
    check: Mapping[str, Any],
    document: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    blocks_by_id = {
        _as_text(block.get("block_id")): block
        for block in _document_blocks(document)
        if _as_text(block.get("block_id"))
    }
    raw_evidence = check.get("evidence", [])
    if not isinstance(raw_evidence, list):
        return []
    evidence: list[dict[str, Any]] = []
    for entry in raw_evidence:
        if not isinstance(entry, Mapping):
            continue
        block_id = _as_text(entry.get("block_id")).strip()
        quote = _as_text(entry.get("quote")).strip()
        source_block = blocks_by_id.get(block_id)
        if (
            not block_id
            or not quote
            or source_block is None
            or _normalized(quote) not in _normalized(source_block.get("text"))
        ):
            continue
        evidence.append(
            {
                "artifact": "structured_document.json",
                "block_id": block_id,
                "quote": quote,
                "relation": _as_text(entry.get("relation"))
                or "对应结构化质量事实",
            }
        )
    return evidence


def _quality_fact(
    deduction_item: str,
    *,
    confirmed_exists: bool | None,
    reason: str,
    evidence: Sequence[Mapping[str, Any]] = (),
    source_artifacts: Sequence[str] = (),
) -> dict[str, Any]:
    status = (
        "confirmed_present"
        if confirmed_exists is True
        else "confirmed_absent"
        if confirmed_exists is False
        else "insufficient"
    )
    return {
        "deduction_item": deduction_item,
        "status": status,
        "confirmed_exists": confirmed_exists,
        "reason": reason,
        "evidence": [dict(item) for item in evidence],
        "source_artifacts": list(dict.fromkeys(source_artifacts)),
    }


def _score_item_001_facts(
    document: Mapping[str, Any] | None,
    artifacts: Mapping[str, Any],
    matched_blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    structured_checks = _quality_structured_checks(document)
    template_name = "08_template_text_reviews.json"
    attachment_name = "09_attachment_reviews.json"
    file_name = "10_file_requirement_reviews.json"
    template_entries = _quality_review_entries(
        artifacts, template_name, "template_text_reviews"
    )
    attachment_entries = _quality_review_entries(
        artifacts, attachment_name, "attachment_reviews"
    )
    file_entries = _quality_review_entries(
        artifacts, file_name, "requirements", "file_requirement_reviews"
    )
    template_complete = _quality_coverage_complete(
        artifacts, template_name, template_entries
    )
    attachment_complete = _quality_coverage_complete(
        artifacts, attachment_name, attachment_entries
    )
    file_stats = _quality_stats(artifacts, file_name)
    file_complete = bool(
        file_entries
        and isinstance(file_stats.get("requirement_count"), int)
        and len(file_entries) >= file_stats.get("requirement_count", 0)
        and all(_quality_review_status(entry) in _QUALITY_ABSENT_STATUSES for entry in file_entries)
        and file_stats.get("fail_count", 0) == 0
        and file_stats.get("not_supported_count", 0) == 0
    )
    template_issues = _quality_issue_entries(template_entries)
    attachment_issues = _quality_issue_entries(attachment_entries)
    attestation_evidence = [
        {
            "artifact": "structured_document.json",
            "block_id": _as_text(block.get("block_id")),
            "quote": _as_text(block.get("text")),
            "relation": "仅作为投标人自我承诺辅助证据",
            "sufficient": False,
        }
        for block in matched_blocks
        if (
            "承诺" in _as_text(block.get("section"))
            or "承诺" in _as_text(block.get("text"))
            or "不存在" in _as_text(block.get("text"))
        )
    ][:2]

    def explicit_fact(label: str) -> dict[str, Any] | None:
        check = structured_checks.get(_normalized(label))
        if check is None:
            return None
        status = _as_text(check.get("status")).strip().lower()
        evidence = _quality_structured_evidence(check, document)
        if status in _QUALITY_ABSENT_STATUSES and evidence:
            return _quality_fact(
                label,
                confirmed_exists=False,
                reason="结构化质量事实检查确认该类问题不存在。",
                evidence=evidence,
                source_artifacts=["structured_document.json"],
            )
        if status in _QUALITY_PRESENT_STATUSES and evidence:
            return _quality_fact(
                label,
                confirmed_exists=True,
                reason="结构化质量事实检查确认该类问题存在。",
                evidence=evidence,
                source_artifacts=["structured_document.json"],
            )
        return _quality_fact(
            label,
            confirmed_exists=None,
            reason="结构化质量事实缺少可核验的 block 证据。",
            evidence=evidence,
            source_artifacts=["structured_document.json"],
        )

    def fallback_fact(
        label: str,
        *,
        confirmed_exists: bool | None,
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        source_artifacts: Sequence[str],
    ) -> dict[str, Any]:
        fact = _quality_fact(
            label,
            confirmed_exists=confirmed_exists,
            reason=reason,
            evidence=evidence or attestation_evidence,
            source_artifacts=source_artifacts,
        )
        if attestation_evidence:
            fact["auxiliary_evidence"] = [dict(item) for item in attestation_evidence]
        return fact

    format_issue_refs = [
        (template_name, issue)
        for issue in template_issues
        if _quality_issue_category(issue) == "format"
    ]
    format_issue_refs.extend(
        (file_name, entry)
        for entry in file_entries
        if _quality_issue_category(entry) == "format"
    )
    format_evidence = [
        reference
        for artifact_name, issue in format_issue_refs[:5]
        for reference in _quality_artifact_evidence(artifact_name, issue, document)
    ]
    if format_issue_refs:
        format_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[0],
            confirmed_exists=True,
            reason="现有模板或文件检查发现未按招标文件要求制作的具体问题。",
            evidence=format_evidence,
            source_artifacts=[template_name, file_name],
        )
    elif template_complete and file_complete:
        format_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[0],
            confirmed_exists=False,
            reason="现有模板检查和文件检查均完成且未发现制作规范问题。",
            evidence=[
                reference
                for entry in template_entries[:5]
                for reference in _quality_artifact_evidence(
                    template_name, entry, document
                )
            ],
            source_artifacts=[template_name, file_name],
        )
    else:
        format_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[0],
            confirmed_exists=None,
            reason="模板或文件检查覆盖不完整，无法确认不存在制作规范问题。",
            evidence=[],
            source_artifacts=[template_name, file_name],
        )

    content_issues = [
        issue for issue in template_issues if _quality_issue_category(issue) == "content"
    ]
    if content_issues:
        content_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[1],
            confirmed_exists=True,
            reason="模板检查明确记录了文件内容错误或内容不一致问题。",
            evidence=[
                reference
                for issue in content_issues[:5]
                for reference in _quality_artifact_evidence(
                    template_name, issue, document
                )
            ],
            source_artifacts=[template_name],
        )
    elif template_complete:
        content_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[1],
            confirmed_exists=False,
            reason="模板文本检查覆盖完整且未发现内容错误。",
            evidence=[
                reference
                for entry in template_entries[:5]
                for reference in _quality_artifact_evidence(
                    template_name, entry, document
                )
            ],
            source_artifacts=[template_name],
        )
    else:
        content_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[1],
            confirmed_exists=None,
            reason="现有模板检查存在未完成或不确定结果，无法确认不存在内容错误。",
            evidence=[
                reference
                for entry in template_entries[:3]
                for reference in _quality_artifact_evidence(
                    template_name, entry, document
                )
            ],
            source_artifacts=[template_name],
        )

    clarity_issue_refs = [
        (template_name, issue)
        for issue in template_issues
        if _quality_issue_category(issue) == "clarity"
    ]
    clarity_issue_refs.extend(
        (attachment_name, issue)
        for issue in attachment_issues
        if _quality_issue_category(issue) == "clarity"
    )
    if clarity_issue_refs:
        clarity_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[2],
            confirmed_exists=True,
            reason="现有检查明确记录了内容模糊或不可清晰识别的问题。",
            evidence=[
                reference
                for artifact_name, issue in clarity_issue_refs[:5]
                for reference in _quality_artifact_evidence(artifact_name, issue, document)
            ],
            source_artifacts=[template_name, attachment_name],
        )
    else:
        clarity_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[2],
            confirmed_exists=None,
            reason="结构化解析能够提供解析结果，但现有产物没有覆盖完整清晰度事实。",
            evidence=[
                {
                    "artifact": "structured_document.json",
                    "quote": json.dumps(
                        (document or {}).get("stats", {}), ensure_ascii=False
                    )[:800],
                    "relation": "仅证明已有结构化解析产物，不足以确认清晰度",
                }
            ],
            source_artifacts=["structured_document.json"],
        )

    missing_issues = [
        issue
        for issue in [*template_issues, *attachment_issues]
        if _quality_issue_category(issue) == "material"
    ]
    file_material_issues = [
        entry
        for entry in file_entries
        if _quality_review_status(entry) == "fail"
        and not any(
            term in _normalized(_quality_value_text(entry.get("requirement")))
            for term in ("大小", "容量", "mb", "文件大小")
        )
    ]
    attachment_bad = [
        entry
        for entry in attachment_entries
        if _quality_review_status(entry) == "fail"
        or any(
            _quality_review_status(requirement) == "fail"
            for requirement in entry.get("requirements", [])
            if isinstance(requirement, Mapping)
        )
    ]
    if missing_issues or file_material_issues or attachment_bad:
        material_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[3],
            confirmed_exists=True,
            reason="现有模板、附件或文件检查明确记录了材料或必填内容缺失。",
            evidence=[
                reference
                for artifact_name, entries in (
                    (template_name, missing_issues),
                    (file_name, file_material_issues),
                    (attachment_name, attachment_bad),
                )
                for entry in entries[:5]
                for reference in _quality_artifact_evidence(
                    artifact_name, entry, document
                )
            ],
            source_artifacts=[template_name, attachment_name, file_name],
        )
    elif template_complete and attachment_complete and file_complete:
        material_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[3],
            confirmed_exists=False,
            reason="模板、附件和文件检查覆盖完整且未发现材料缺失。",
            evidence=[
                reference
                for artifact_name, entries in (
                    (template_name, template_entries),
                    (attachment_name, attachment_entries),
                    (file_name, file_entries),
                )
                for entry in entries[:2]
                for reference in _quality_artifact_evidence(
                    artifact_name, entry, document
                )
            ],
            source_artifacts=[template_name, attachment_name, file_name],
        )
    else:
        material_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[3],
            confirmed_exists=None,
            reason="现有材料检查覆盖不完整或存在不确定结果，无法确认材料完整。",
            evidence=[],
            source_artifacts=[template_name, attachment_name, file_name],
        )

    readability_issue_refs = [
        (template_name, issue)
        for issue in template_issues
        if _quality_issue_category(issue) == "readability"
    ]
    readability_issue_refs.extend(
        (attachment_name, issue)
        for issue in attachment_issues
        if _quality_issue_category(issue) == "readability"
    )
    if readability_issue_refs:
        readability_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[4],
            confirmed_exists=True,
            reason="现有检查明确记录了标书阅读困难问题。",
            evidence=[
                reference
                for artifact_name, issue in readability_issue_refs[:5]
                for reference in _quality_artifact_evidence(artifact_name, issue, document)
            ],
            source_artifacts=[template_name, "structured_document.json"],
        )
    else:
        readability_fact = fallback_fact(
            _SCORE_ITEM_001_DEDUCTIONS[4],
            confirmed_exists=None,
            reason="现有产物没有完整覆盖标书阅读困难的独立检查事实。",
            evidence=[
                {
                    "artifact": "structured_document.json",
                    "quote": "结构化投标解析产物已复用",
                    "relation": "缺少独立阅读困难检查结论",
                }
            ],
            source_artifacts=["structured_document.json"],
        )

    facts_by_label = {
        fact["deduction_item"]: fact
        for fact in (
            format_fact,
            content_fact,
            clarity_fact,
            material_fact,
            readability_fact,
        )
    }
    for label in _SCORE_ITEM_001_DEDUCTIONS:
        explicit = explicit_fact(label)
        if explicit is not None and (
            explicit.get("confirmed_exists") is True
            or facts_by_label[label].get("confirmed_exists") is not True
        ):
            facts_by_label[label] = explicit
    return [facts_by_label[label] for label in _SCORE_ITEM_001_DEDUCTIONS]


def _quality_facts_complete(facts: Sequence[Mapping[str, Any]]) -> bool:
    return len(facts) == len(_SCORE_ITEM_001_DEDUCTIONS) and all(
        fact.get("confirmed_exists") in {True, False}
        and isinstance(fact.get("evidence"), list)
        and bool(fact.get("evidence"))
        for fact in facts
    )


def _quality_facts_reason(facts: Sequence[Mapping[str, Any]]) -> str:
    insufficient = [
        _as_text(fact.get("deduction_item"))
        for fact in facts
        if fact.get("confirmed_exists") not in {True, False}
    ]
    if not insufficient:
        return "score_item_001 五类扣分项缺少可核验覆盖证据，无法确认不扣分。"
    return (
        "score_item_001 五类扣分项事实覆盖不足，无法确认不扣分："
        + "、".join(insufficient)
        + "；评审要求承诺函仅作为辅助证据。"
    )


def _normalize_evidence(
    value: Any,
    allowed_blocks: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SubjectiveScoringError("模型未返回可追溯的 evidence 列表")
    evidence: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise SubjectiveScoringError("模型 evidence 条目结构异常")
        block_id = _as_text(entry.get("block_id"))
        quote = _as_text(entry.get("quote")).strip()
        relation = _as_text(entry.get("relation")).strip()
        if block_id not in allowed_blocks:
            raise SubjectiveScoringError(
                f"模型 evidence 引用了未送模 block_id: {block_id or '<empty>'}"
            )
        if not quote:
            raise SubjectiveScoringError("模型 evidence 缺少 quote")
        relation = relation or "对应评分规则判断"
        source_text = _as_text(allowed_blocks[block_id].get("text"))
        if _normalized(quote) not in _normalized(source_text):
            raise SubjectiveScoringError(
                f"模型 evidence quote 不在 block_id {block_id} 的投标原文中"
            )
        evidence.append(
            {"block_id": block_id, "quote": quote, "relation": relation}
        )
    return evidence


def _validate_llm_result(
    item: Mapping[str, Any],
    matched_blocks: Sequence[Mapping[str, Any]],
    allowed_bands: Sequence[Mapping[str, Any]],
    raw_result: Any,
) -> dict[str, Any]:
    if not isinstance(raw_result, Mapping):
        raise SubjectiveScoringError("模型返回结果不是 JSON 对象")
    score_band = _as_text(raw_result.get("score_band")).strip()
    bands_by_label = {
        _as_text(band.get("label")): band
        for band in allowed_bands
        if _as_text(band.get("label"))
    }
    if score_band not in bands_by_label:
        raise SubjectiveScoringError(f"模型返回了规则之外的评分档: {score_band or '<empty>'}")
    score = _numeric(raw_result.get("recommended_score"))
    if score is None:
        raise SubjectiveScoringError("模型未返回有限的 recommended_score")
    score_as_float = float(score)
    max_score = _full_score(item)
    if max_score is not None and not 0 <= score_as_float <= float(max_score):
        raise SubjectiveScoringError("recommended_score 超出 0 到 max_score 范围")
    if not _band_contains(bands_by_label[score_band], score_as_float):
        raise SubjectiveScoringError(
            f"recommended_score 不落在评分档 {score_band} 的边界内"
        )
    reason = _as_text(raw_result.get("reason")).strip()
    if not reason:
        raise SubjectiveScoringError("模型未返回评分理由")
    blocks_by_id = {
        _as_text(block.get("block_id")): block
        for block in matched_blocks
        if _as_text(block.get("block_id"))
    }
    evidence = _normalize_evidence(raw_result.get("evidence"), blocks_by_id)
    deduction_checks = item.get("deduction_checks")
    if isinstance(deduction_checks, list) and deduction_checks:
        confirmed_present_count = sum(
            check.get("confirmed_exists") is True
            for check in deduction_checks
            if isinstance(check, Mapping)
        )
        expected_score = (
            float(max_score) - confirmed_present_count
            if max_score is not None
            else None
        )
        if expected_score is not None and score_as_float != expected_score:
            raise SubjectiveScoringError(
                "recommended_score 未按五类扣分项确认结果每项扣1分计算"
            )
    return {
        "score_item_id": _as_text(item.get("id")),
        "rule_name": _as_text(item.get("name")),
        "max_score": max_score,
        "status": "ai_scored",
        "score_band": score_band,
        "recommended_score": score,
        "reason": reason,
        "matched_bid_content": [dict(block) for block in matched_blocks],
        "evidence": evidence,
        "block_ids": sorted(blocks_by_id),
        "uncertainty": _uncertainty(raw_result.get("uncertainty")),
        "deduction_checks": [
            dict(check) for check in deduction_checks if isinstance(check, Mapping)
        ]
        if isinstance(deduction_checks, list)
        else [],
    }


class OpenAICompatibleSubjectiveScoreLLM:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout_seconds: float = 180,
        max_tokens: int = 2048,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max(256, min(max_tokens, 8192))
        self._call_context = threading.local()
        self._last_usage = threading.local()
        self.available = True

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return getattr(self._last_usage, "value", None)

    @last_usage.setter
    def last_usage(self, value: dict[str, Any] | None) -> None:
        self._last_usage.value = value

    def set_call_context(
        self,
        *,
        recorder: ComplianceExtractionRecorder | None,
        call_id: str | None,
    ) -> None:
        self._call_context.value = {"recorder": recorder, "call_id": call_id}

    def _context(self) -> dict[str, Any]:
        return getattr(self._call_context, "value", {})

    def score(
        self,
        score_item: Mapping[str, Any],
        matched_bid_content: Sequence[Mapping[str, Any]],
        allowed_bands: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        rule_payload = {
            key: score_item.get(key)
            for key in (
                "id",
                "name",
                "full_score",
                "original_rule",
                "conditions",
                "scoring_method",
                "evidence_requirements",
                "deduction_checks",
            )
        }
        prompt = (
            "你是招标文件主观评分辅助器。只能依据本评分项原始规则和输入中的投标 block"
            "进行建议评分，不得增加招标文件没有的评价维度，不得使用其他评分项、否决规则、"
            "整份投标文件或输入之外的信息。必须先选择 allowed_bands 中的原始评分档，"
            "再给出属于该档的 recommended_score。每条 evidence 必须引用输入中的真实 block_id"
            "和直接摘录；无法确认时不要猜测，返回 uncertainty 并保持证据边界。"
            "如果输入包含 deduction_checks，必须逐项依据其 confirmed_exists 结果；"
            "每个 confirmed_present 按原始规则扣1分，不能把自我承诺当作已确认不存在。"
            "只返回 JSON 对象，字段必须为 score_band、recommended_score、reason、evidence、uncertainty。\n\n"
            f"评分项规则：{json.dumps(rule_payload, ensure_ascii=False)}\n"
            f"允许的评分档：{json.dumps(list(allowed_bands), ensure_ascii=False)}\n"
            f"匹配投标内容：{json.dumps(list(matched_bid_content), ensure_ascii=False)}"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "enable_thinking": False,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你只输出来源受限的主观评分 JSON。"},
                {"role": "user", "content": prompt},
            ],
        }
        context = self._context()
        recorder = context.get("recorder")
        call_id = context.get("call_id")
        if recorder is not None and call_id is not None:
            recorder.attach_llm_input(call_id, payload)
        started_at = time.perf_counter()
        try:
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            choice = response_payload.get("choices", [{}])[0]
            self.last_usage = response_payload.get("usage")
            if recorder is not None and call_id is not None:
                recorder.attach_llm_response(
                    call_id,
                    raw_response=response_payload,
                    finish_reason=choice.get("finish_reason"),
                    usage=self.last_usage,
                )
            content = choice["message"]["content"]
            decoded = json.loads(content) if isinstance(content, str) else content
            if not isinstance(decoded, Mapping):
                raise TypeError("subjective scoring result must be an object")
            return decoded
        except TimeoutError as exc:
            raise SubjectiveScoringError("主观评分 LLM 请求超时。") from exc
        except urllib.error.HTTPError as exc:
            raise SubjectiveScoringError(
                f"主观评分 LLM 请求失败：HTTP {exc.code}。"
            ) from exc
        except urllib.error.URLError as exc:
            raise SubjectiveScoringError("主观评分 LLM 网络连接失败。") from exc
        except json.JSONDecodeError as exc:
            raise SubjectiveScoringError("主观评分 LLM 响应不是有效 JSON。") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SubjectiveScoringError("主观评分 LLM 响应结构异常。") from exc
        finally:
            logger.info(
                "subjective_scoring.llm.call elapsed_ms=%d model=%s item=%s",
                int((time.perf_counter() - started_at) * 1000),
                self.model,
                _as_text(score_item.get("id")),
            )


class DeterministicSubjectiveScoreLLM:
    model = "deterministic"
    available = False

    def score(
        self,
        score_item: Mapping[str, Any],
        matched_bid_content: Sequence[Mapping[str, Any]],
        allowed_bands: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        del score_item, matched_bid_content, allowed_bands
        return {}


def _llm_error_result(
    item: Mapping[str, Any],
    matched_blocks: Sequence[Mapping[str, Any]],
    reason: str,
) -> dict[str, Any]:
    return _base_result(
        item,
        status="llm_error",
        reason=reason,
        matched_blocks=matched_blocks,
        uncertainty={"level": "high", "notes": [reason]},
    )


def _execute_subjective_item(
    item: Mapping[str, Any],
    matched_blocks: Sequence[Mapping[str, Any]],
    allowed_bands: Sequence[Mapping[str, Any]],
    *,
    subjective_llm: SubjectiveScoreLLM,
    recorder: ComplianceExtractionRecorder | None,
    batch_index: int,
    batch_count: int,
) -> tuple[dict[str, Any], bool, bool, int]:
    if not getattr(subjective_llm, "available", True):
        reason = "未配置可用的主观评分 LLM，未生成 AI 辅助建议分。"
        return (
            _llm_error_result(item, matched_blocks, reason),
            False,
            False,
            0,
        )

    started_at = time.perf_counter()
    call_id: str | None = None
    raw_result: Mapping[str, Any] | None = None
    if recorder is not None:
        call_id = recorder.start_llm_call(
            batch_index=batch_index,
            batch_count=batch_count,
            attempt=1,
            model=getattr(subjective_llm, "model", type(subjective_llm).__name__),
            batch={
                "score_item_id": _as_text(item.get("id")),
                "block_ids": [
                    _as_text(block.get("block_id")) for block in matched_blocks
                ],
            },
        )
    set_context = getattr(subjective_llm, "set_call_context", None)
    if callable(set_context):
        set_context(recorder=recorder, call_id=call_id)
    try:
        raw_result = subjective_llm.score(item, matched_blocks, allowed_bands)
        normalized = _validate_llm_result(
            item,
            matched_blocks,
            allowed_bands,
            raw_result,
        )
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        if recorder is not None and call_id is not None:
            recorder.complete_llm_call(
                call_id,
                raw_response=dict(raw_result),
                parsed_objects=dict(raw_result),
                usage=getattr(subjective_llm, "last_usage", None),
                schema_valid=True,
                elapsed_ms=elapsed_ms,
            )
        return normalized, True, True, elapsed_ms
    except Exception as exc:  # noqa: BLE001 - isolate one item model failure
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        message = str(exc) or "主观评分 LLM 调用失败。"
        if recorder is not None and call_id is not None:
            if raw_result is None:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=message,
                    elapsed_ms=elapsed_ms,
                )
            else:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=message,
                    elapsed_ms=elapsed_ms,
                    raw_response=dict(raw_result),
                )
        return (
            _llm_error_result(item, matched_blocks, message),
            True,
            False,
            elapsed_ms,
        )
    finally:
        if callable(set_context):
            set_context(recorder=None, call_id=None)


def _subjective_source(
    evidence: Mapping[str, Any],
    bid_file: FileMetadata,
) -> dict[str, Any]:
    artifacts = evidence.get("artifacts", {})
    reused_artifacts = (
        sorted(artifacts)
        if isinstance(artifacts, Mapping)
        else []
    )
    return {
        "evaluation_rules_artifact": "11_evaluation_rules.json",
        "bid_filename": bid_file.filename,
        "bid_path": evidence.get("bid_path"),
        "bid_document_artifact": evidence.get("bid_document_artifact"),
        "bid_document_hash_verified": bool(
            evidence.get("bid_document_hash_verified")
        ),
        "reused_artifacts": reused_artifacts,
        "missing_artifacts": sorted(evidence.get("missing_artifacts", [])),
    }


def run_subjective_scoring(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    subjective_llm: SubjectiveScoreLLM,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Execute only subjective score rules from reusable bid evidence."""

    started_at = time.perf_counter()
    evidence = load_reusable_bid_evidence(
        bid_file,
        bid_document=bid_document,
        artifact_dir=artifact_dir,
        existing_artifacts=existing_artifacts,
    )
    items = select_subjective_items(evaluation_rules)
    records: dict[int, dict[str, Any]] = {}
    eligible: list[tuple[int, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for index, item in enumerate(items):
        match = match_subjective_bid_content(
            item,
            evidence.get("bid_document"),
            bid_file.filename,
        )
        blocks = match.get("blocks", [])
        blocks = [dict(block) for block in blocks if isinstance(block, Mapping)]
        if match.get("status") != "matched":
            item_without_match = item
            reason = _as_text(match.get("reason")) or "未定位到评分证据。"
            if _as_text(item.get("id")) == _SCORE_ITEM_001_ID:
                deduction_checks = _score_item_001_facts(
                    evidence.get("bid_document"),
                    evidence.get("artifacts", {})
                    if isinstance(evidence.get("artifacts"), Mapping)
                    else {},
                    blocks,
                )
                item_without_match = dict(item)
                item_without_match["deduction_checks"] = deduction_checks
                reason = _quality_facts_reason(deduction_checks)
                if not reason:
                    reason = "未定位到评分项对应正文 block，无法形成完整评分证据。"
            records[index] = _base_result(
                item_without_match,
                status=_as_text(match.get("status")) or "evidence_insufficient",
                reason=reason,
                matched_blocks=blocks,
            )
            continue
        item_for_scoring = item
        if _as_text(item.get("id")) == _SCORE_ITEM_001_ID:
            deduction_checks = _score_item_001_facts(
                evidence.get("bid_document"),
                evidence.get("artifacts", {})
                if isinstance(evidence.get("artifacts"), Mapping)
                else {},
                blocks,
            )
            item_for_scoring = dict(item)
            item_for_scoring["deduction_checks"] = deduction_checks
            if not _quality_facts_complete(deduction_checks):
                records[index] = _base_result(
                    item_for_scoring,
                    status="evidence_insufficient",
                    reason=_quality_facts_reason(deduction_checks),
                    matched_blocks=blocks,
                )
                continue
        eligible.append(
            (index, item_for_scoring, blocks, _allowed_score_bands(item_for_scoring))
        )

    call_stats: dict[int, tuple[bool, bool, int]] = {}
    if eligible:
        worker_count = min(SUBJECTIVE_MAX_WORKERS, len(eligible))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="subjective-score",
        ) as executor:
            futures = {
                executor.submit(
                    _execute_subjective_item,
                    item,
                    blocks,
                    bands,
                    subjective_llm=subjective_llm,
                    recorder=recorder,
                    batch_index=index + 1,
                    batch_count=len(eligible),
                ): index
                for index, item, blocks, bands in eligible
            }
            for future in as_completed(futures):
                index = futures[future]
                item, blocks = next(
                    (item, blocks)
                    for eligible_index, item, blocks, _ in eligible
                    if eligible_index == index
                )
                try:
                    record, called, completed, elapsed_ms = future.result()
                except Exception as exc:  # noqa: BLE001 - continue other items
                    reason = str(exc) or "主观评分 worker 执行失败。"
                    record = _llm_error_result(item, blocks, reason)
                    called, completed, elapsed_ms = True, False, 0
                records[index] = record
                call_stats[index] = (called, completed, elapsed_ms)

    score_items = [records[index] for index in range(len(items))]
    status_counts = {status: 0 for status in sorted(SUBJECTIVE_STATUSES)}
    for record in score_items:
        status = record["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    llm_total_calls = sum(called for called, _, _ in call_stats.values())
    llm_completed_calls = sum(completed for _, completed, _ in call_stats.values())
    llm_failed_calls = llm_total_calls - llm_completed_calls
    call_elapsed = [
        elapsed_ms
        for index, (called, _, elapsed_ms) in sorted(call_stats.items())
        if called
    ]
    result: dict[str, Any] = {
        "schema_version": "subjective-score-v1",
        "source": _subjective_source(evidence, bid_file),
        "score_items": score_items,
        "stats": {
            "subjective_item_count": len(score_items),
            "ai_scored_count": status_counts.get("ai_scored", 0),
            "file_scope_missing_count": status_counts.get("file_scope_missing", 0),
            "evidence_insufficient_count": status_counts.get(
                "evidence_insufficient", 0
            ),
            "llm_error_count": status_counts.get("llm_error", 0),
            "status_counts": status_counts,
            "llm_total_calls": llm_total_calls,
            "llm_completed_calls": llm_completed_calls,
            "llm_failed_calls": llm_failed_calls,
            "llm_elapsed_ms": sum(call_elapsed),
            "llm_call_elapsed_ms": call_elapsed,
            "bid_parse_reused": bool(evidence.get("bid_document_hash_verified")),
            "new_parse_calls": 0,
            "new_ocr_calls": 0,
            "new_mineru_calls": 0,
            "duplicate_parse": False,
            "total_score_computed": False,
            "ranking_computed": False,
            "veto_executed": False,
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        },
    }
    if recorder is not None:
        recorder.write_json(SUBJECTIVE_SCORE_ARTIFACT, result)
        recorder.event(
            "subjective.score.execution.end",
            status="complete",
            subjective_item_count=len(score_items),
            ai_scored_count=status_counts.get("ai_scored", 0),
            llm_total_calls=llm_total_calls,
            elapsed_ms=result["stats"]["elapsed_ms"],
        )
    return result
