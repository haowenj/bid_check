from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SUBJECTIVE_SCORE_ARTIFACT = "subjective_scores.json"
SUBJECTIVE_STATUSES = frozenset(
    {"ai_scored", "file_scope_missing", "evidence_insufficient", "llm_error"}
)
SUBJECTIVE_MAX_WORKERS = 5

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
