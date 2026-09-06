from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata

OBJECTIVE_SCORE_ARTIFACT = "objective_scores.json"
OBJECTIVE_STATUSES = frozenset(
    {
        "auto_scored",
        "evidence_insufficient",
        "file_scope_missing",
        "other_bidder_data_required",
        "external_data_required",
        "unsupported",
    }
)

_REUSABLE_ARTIFACT_NAMES = (
    "08_template_text_reviews.json",
    "09_attachment_reviews.json",
    "10_file_requirement_reviews.json",
    "10_performance_reviews.json",
)
_MONEY_RE = re.compile(
    r"(?<![\d.])([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
    r"\s*[】\]）)]?\s*(?:万元|万|元)"
)
_PERCENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})\s*日?"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _resolved_bid_path(bid_file: FileMetadata) -> Path:
    return Path(bid_file.storage_path).expanduser().resolve()


def _resolved_tender_path(tender_file: FileMetadata) -> Path:
    return Path(tender_file.storage_path).expanduser().resolve()


def _parse_date(value: str) -> date | None:
    match = _DATE_RE.search(value)
    if not match:
        return None
    try:
        return date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
    except ValueError:
        return None


def load_reusable_tender_evidence(
    tender_file: FileMetadata,
    *,
    existing_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load the announcement-date fact from the existing tender parse.

    Objective scoring only needs a small, deterministic fact from the tender
    document.  This function deliberately reads the already-produced
    ``01_parsed_blocks.json`` artifact and never starts MinerU or an LLM.
    """

    if isinstance(existing_evidence, Mapping):
        return dict(existing_evidence)

    tender_path = _resolved_tender_path(tender_file)
    artifact_path = tender_path.parent / "compliance_extraction" / "01_parsed_blocks.json"
    payload = _read_json(artifact_path)
    if not isinstance(payload, Mapping):
        return {
            "artifact_path": str(artifact_path),
            "announcement_date": None,
            "announcement_date_source": None,
        }

    candidates: list[dict[str, str]] = []
    blocks = payload.get("blocks", [])
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, Mapping) or "招标公告" not in _as_text(block.get("section")):
                continue
            text = _as_text(block.get("text")).strip()
            parsed = _parse_date(text)
            if parsed is not None and _DATE_RE.fullmatch(text):
                candidates.append(
                    {
                        "block_id": _as_text(block.get("block_id")),
                        "text": text,
                        "date": parsed.isoformat(),
                    }
                )

    unique_dates = {candidate["date"] for candidate in candidates}
    source = None
    announcement_date = None
    if len(unique_dates) == 1:
        announcement_date = next(iter(unique_dates))
        source_candidate = next(
            candidate for candidate in candidates if candidate["date"] == announcement_date
        )
        source = {
            "block_id": source_candidate["block_id"],
            "text": source_candidate["text"],
        }
    return {
        "artifact_path": str(artifact_path),
        "announcement_date": announcement_date,
        "announcement_date_source": source,
    }


def _artifact_path_for_bid(
    bid_file: FileMetadata,
    *,
    artifact_dir: Path | None,
) -> Path:
    if artifact_dir is not None:
        return Path(artifact_dir).expanduser().resolve()
    return _resolved_bid_path(bid_file).parent / "bid_document_cleaning"


def _document_hash_matches(document: Mapping[str, Any], bid_path: Path) -> bool:
    source = document.get("source")
    if not isinstance(source, Mapping):
        return False
    source_hash = source.get("sha256")
    if not isinstance(source_hash, str) or not source_hash:
        return False
    try:
        return source_hash == _sha256(bid_path)
    except OSError:
        return False


def load_reusable_bid_evidence(
    bid_file: FileMetadata,
    *,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load already-produced bid evidence without starting a parser or LLM call.

    The real file path is resolved before locating sibling artifacts.  This is
    important for evaluation tasks whose uploaded files are symlinks to the
    original compliance task files.
    """

    bid_path = _resolved_bid_path(bid_file)
    cleaning_dir = _artifact_path_for_bid(bid_file, artifact_dir=artifact_dir)
    compliance_dir = cleaning_dir.parent / "compliance_extraction"
    document_path = cleaning_dir / "structured_document.json"

    document = dict(bid_document) if isinstance(bid_document, Mapping) else None
    document_source = "argument" if document is not None else None
    document_hash_verified = False
    if document is None:
        candidate = _read_json(document_path)
        if isinstance(candidate, Mapping) and _document_hash_matches(candidate, bid_path):
            document = dict(candidate)
            document_source = str(document_path)
            document_hash_verified = True
        elif isinstance(candidate, Mapping):
            document_source = "hash_mismatch"
    elif _document_hash_matches(document, bid_path):
        document_hash_verified = True

    artifacts: dict[str, Any] = {}
    artifact_paths: dict[str, str] = {}
    missing_artifacts: list[str] = []
    for name in _REUSABLE_ARTIFACT_NAMES:
        if existing_artifacts is not None and name in existing_artifacts:
            artifacts[name] = existing_artifacts[name]
            continue
        path = compliance_dir / name
        payload = _read_json(path)
        if payload is None:
            missing_artifacts.append(name)
            continue
        artifacts[name] = payload
        artifact_paths[name] = str(path)

    return {
        "bid_path": str(bid_path),
        "bid_filename": bid_file.filename,
        "bid_sha256": _sha256(bid_path) if bid_path.is_file() else None,
        "bid_document": document,
        "bid_document_artifact": str(document_path) if document is not None else None,
        "bid_document_source": document_source,
        "bid_document_hash_verified": document_hash_verified,
        "artifacts": artifacts,
        "artifact_paths": artifact_paths,
        "missing_artifacts": missing_artifacts,
    }


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _document_sections(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    sections = document.get("sections")
    return [dict(section) for section in sections if isinstance(section, Mapping)] \
        if isinstance(sections, list) else []


def _document_blocks(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    blocks = document.get("blocks")
    return [dict(block) for block in blocks if isinstance(block, Mapping)] \
        if isinstance(blocks, list) else []


def _document_text(document: Mapping[str, Any] | None) -> str:
    sections = _document_sections(document)
    blocks = _document_blocks(document)
    block_text = "\n".join(_as_text(block.get("text")) for block in blocks)
    section_text = "\n".join(
        " ".join(
            [
                _as_text(section.get("title")),
                " ".join(_as_text(part) for part in section.get("path", []))
                if isinstance(section.get("path"), list)
                else "",
            ]
        ).strip()
        for section in sections
    )
    return f"{section_text}\n{block_text}".strip()


def _section_titles(document: Mapping[str, Any] | None) -> str:
    return "\n".join(
        _as_text(section.get("title"))
        for section in _document_sections(document)
    )


def _has_scope(document: Mapping[str, Any] | None, filename: str, terms: tuple[str, ...]) -> bool:
    titles = _section_titles(document)
    if any(term in titles for term in terms):
        return True
    text = _document_text(document)
    if any(term in text for term in terms):
        return True
    # A filename is only used as a conservative scope signal, never as proof
    # of a scoreable fact.
    normalized = filename.lower()
    return any(term.lower() in normalized for term in terms)


def _has_section_scope(
    document: Mapping[str, Any] | None,
    filename: str,
    terms: tuple[str, ...],
) -> bool:
    titles = _section_titles(document)
    if any(term in titles for term in terms):
        return True
    return any(term.lower() in filename.lower() for term in terms)


def _is_business_only(document: Mapping[str, Any] | None, filename: str) -> bool:
    titles = _section_titles(document)
    if "商务" not in filename and "商务" not in titles:
        return False
    return not any(
        term in titles
        for term in ("技术标", "技术规范书", "技术响应", "报价文件", "投标一览表")
    )


def _category_map(evaluation_rules: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    categories = evaluation_rules.get("score_categories", [])
    return {
        str(category.get("id")): dict(category)
        for category in categories
        if isinstance(category, Mapping) and category.get("id")
    }


def _copy_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        return {"section": "", "block_ids": [], "source_text": ""}
    block_ids = source.get("block_ids", [])
    return {
        "section": _as_text(source.get("section")),
        "block_ids": [str(block_id) for block_id in block_ids]
        if isinstance(block_ids, list)
        else [],
        "source_text": _as_text(source.get("source_text")),
    }


def _item_base(
    item: Mapping[str, Any],
    category: Mapping[str, Any] | None,
) -> dict[str, Any]:
    category_id = item.get("category_id")
    return {
        "id": _as_text(item.get("id")),
        "name": _as_text(item.get("name")),
        "category_id": category_id,
        "category_name": _as_text(category.get("name")) if category else "",
        "parent_item_id": item.get("parent_item_id"),
        "full_score": item.get("full_score"),
        "original_rule": _as_text(item.get("original_rule")),
        "conditions": item.get("conditions", {})
        if isinstance(item.get("conditions", {}), Mapping)
        else {},
        "scoring_method": item.get("scoring_method", {})
        if isinstance(item.get("scoring_method", {}), Mapping)
        else {},
        "evaluation_type": item.get("evaluation_type"),
        "evidence_requirements": item.get("evidence_requirements", [])
        if isinstance(item.get("evidence_requirements", []), list)
        else [],
        "tender_rule_source": _copy_source(item.get("source")),
        "status": "unsupported",
        "score": None,
        "facts": {},
        "calculation": None,
        "evidence": [],
        "related_artifacts": [],
        "reason": "",
    }


def _status_result(
    result: dict[str, Any],
    *,
    status: str,
    reason: str,
    facts: Mapping[str, Any] | None = None,
    calculation: Mapping[str, Any] | None = None,
    score: float | int | None = None,
    evidence: list[dict[str, Any]] | None = None,
    related_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    if status not in OBJECTIVE_STATUSES:
        raise ValueError(f"unknown objective score status: {status}")
    result.update(
        {
            "status": status,
            "score": score if status == "auto_scored" else None,
            "reason": reason,
            "facts": dict(facts or {}),
            "calculation": dict(calculation) if calculation else None,
            "evidence": list(evidence or []),
            "related_artifacts": list(related_artifacts or []),
        }
    )
    return result


def _review_entries(artifacts: Mapping[str, Any], name: str, key: str) -> list[dict[str, Any]]:
    payload = artifacts.get(name)
    if not isinstance(payload, Mapping):
        return []
    entries = payload.get(key, [])
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)] \
        if isinstance(entries, list) else []


def _artifact_ref(name: str, index: int | None = None) -> dict[str, Any]:
    ref: dict[str, Any] = {"artifact": name}
    if index is not None:
        ref["entry_index"] = index
    return ref


def _compact_check(check: Any) -> dict[str, Any]:
    if not isinstance(check, Mapping):
        return {"status": "uncertain", "reason": "检查结果结构无法识别", "evidence": []}
    evidence = check.get("evidence", [])
    compact_evidence: list[dict[str, Any]] = []
    if isinstance(evidence, list):
        for raw in evidence:
            if not isinstance(raw, Mapping):
                continue
            compact_evidence.append(
                {
                    key: raw.get(key)
                    for key in (
                        "kind",
                        "image_id",
                        "block_id",
                        "section_id",
                        "row_index",
                        "description",
                        "ocr_text",
                    )
                    if key in raw
                }
            )
    return {
        "status": _as_text(check.get("status")) or "uncertain",
        "reason": _as_text(check.get("reason")),
        "evidence": compact_evidence,
    }


def _fact_entries(review: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    ocr_facts = review.get("ocr_facts")
    if isinstance(ocr_facts, Mapping) and isinstance(ocr_facts.get(key), list):
        return [dict(entry) for entry in ocr_facts[key] if isinstance(entry, Mapping)]
    extraction = review.get("text_model_extraction")
    if isinstance(extraction, Mapping):
        text_facts = extraction.get("facts")
        if isinstance(text_facts, Mapping) and isinstance(text_facts.get(key), list):
            return [dict(entry) for entry in text_facts[key] if isinstance(entry, Mapping)]
    return []


def _fact_evidence_texts(fact: Mapping[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ("value", "reason"):
        value = _as_text(fact.get(key)).strip()
        if value and value not in texts:
            texts.append(value)
    evidence = fact.get("evidence")
    if isinstance(evidence, list):
        for entry in evidence:
            if not isinstance(entry, Mapping):
                continue
            for key in ("evidence_text", "ocr_text", "description", "value"):
                value = _as_text(entry.get(key)).strip()
                if value and value not in texts:
                    texts.append(value)
    return texts


def _compact_fact(fact: Mapping[str, Any]) -> dict[str, Any]:
    evidence = fact.get("evidence")
    evidence_texts = _fact_evidence_texts(fact)
    image_ids = fact.get("evidence_image_ids")
    if not isinstance(image_ids, list):
        image_ids = []
    if fact.get("image_id") and fact["image_id"] not in image_ids:
        image_ids = [fact["image_id"], *image_ids]
    return {
        "field": _as_text(fact.get("field")),
        "status": _as_text(fact.get("status")) or "uncertain",
        "value": fact.get("value"),
        "image_id": fact.get("image_id"),
        "evidence_image_ids": [str(value) for value in image_ids],
        "evidence_texts": evidence_texts,
        "reason": _as_text(fact.get("reason")),
        "evidence": [dict(entry) for entry in evidence if isinstance(entry, Mapping)]
        if isinstance(evidence, list)
        else [],
    }


def _check_status(fact: Mapping[str, Any], key: str) -> str:
    checks = fact.get("checks")
    check = checks.get(key) if isinstance(checks, Mapping) else None
    return _as_text(check.get("status")) if isinstance(check, Mapping) else "uncertain"


def _signature_date_evidence(review: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    extraction = review.get("text_model_extraction")
    candidates = extraction.get("signature_date_candidates") if isinstance(extraction, Mapping) else None
    if isinstance(candidates, list):
        sources.extend(
            {"source": "signature_date_candidates", **dict(candidate)}
            for candidate in candidates
            if isinstance(candidate, Mapping)
        )
    checks = review.get("checks_by_key")
    signature_check = checks.get("signature_date") if isinstance(checks, Mapping) else None
    if isinstance(signature_check, Mapping):
        sources.append({"source": "signature_date_check", **dict(signature_check)})

    dates: list[str] = []
    for source in sources:
        texts = _fact_evidence_texts(source)
        for text in texts:
            for match in _DATE_RE.finditer(text):
                parsed = _parse_date(match.group(0))
                if parsed is not None and parsed.isoformat() not in dates:
                    dates.append(parsed.isoformat())
    return dates, sources


def _source_ids(review: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    block_ids: list[str] = []
    image_ids: list[str] = []

    def add_evidence(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        block_id = value.get("block_id")
        if block_id and str(block_id) not in block_ids:
            block_ids.append(str(block_id))
        image_id = value.get("image_id")
        if image_id and str(image_id) not in image_ids:
            image_ids.append(str(image_id))

    checks = review.get("checks_by_key")
    if isinstance(checks, Mapping):
        for check in checks.values():
            if not isinstance(check, Mapping):
                continue
            for evidence in check.get("evidence", []):
                add_evidence(evidence)

    for key in ("service_content", "contract_amount", "implementation_time"):
        for fact in _fact_entries(review, key):
            add_evidence(fact)
            for evidence in fact.get("evidence", []):
                add_evidence(evidence)

    extraction = review.get("text_model_extraction")
    if isinstance(extraction, Mapping):
        for candidate_key in ("signature_page_candidates", "signature_date_candidates"):
            for candidate in extraction.get(candidate_key, []):
                if not isinstance(candidate, Mapping):
                    continue
                add_evidence(candidate)
                for evidence in candidate.get("evidence", []):
                    add_evidence(evidence)
    return block_ids, image_ids


def _normalize_contract_amount(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize the contract fact without using the self-reported table amount."""

    candidates: list[dict[str, Any]] = []
    for fact_index, fact in enumerate(facts):
        if fact.get("status") not in {"present", "pass"}:
            continue
        texts = _fact_evidence_texts(fact)
        for text in texts:
            for match in _MONEY_RE.finditer(text):
                raw = match.group(1).replace(",", "")
                suffix_match = re.search(r"[】\]）)]?\s*(万元|万|元)\s*(/\s*[^\s，。；;）)]*)?", match.group(0))
                raw_unit = suffix_match.group(1) if suffix_match else "元"
                suffix = suffix_match.group(2) if suffix_match and suffix_match.group(2) else ""
                end_context = text[match.end(): match.end() + 20]
                trailing_suffix = re.match(r"\s*(/\s*[^\s，。；;）)]*)", end_context)
                suffix = suffix or (trailing_suffix.group(1) if trailing_suffix else "")
                try:
                    numeric = float(raw)
                except ValueError:
                    continue
                unit_value = numeric if raw_unit in {"万元", "万"} else numeric / 10000
                context = text[max(0, match.start() - 80): min(len(text), match.end() + 80)]
                total_context = any(term in context for term in ("合同金额", "合同总金额", "总金额", "总费用", "合同价"))
                formula_context = any(
                    term in context
                    for term in ("费率", "单价", "按", "实际装机", "*", "×", "%", "/瓦", "/枪")
                )
                annual = "/年" in suffix and "/月" not in suffix
                candidates.append(
                    {
                        "value": unit_value,
                        "unit": f"万元{suffix}" if raw_unit in {"万元", "万"} else f"万元{suffix}",
                        "raw_value": numeric,
                        "raw_unit": raw_unit,
                        "suffix": suffix,
                        "fact_index": fact_index,
                        "source_text": text,
                        "image_id": fact.get("image_id"),
                        "total_context": total_context,
                        "formula_context": formula_context,
                        "annual": annual,
                    }
                )

    if not candidates:
        source_text = next(
            (
                text
                for fact in facts
                for text in fact.get("evidence_texts", [])
                if _as_text(text).strip()
            ),
            None,
        )
        return {
            "value": None,
            "unit": None,
            "basis": "contract_fact",
            "confirmation": "uncertain",
            "cumulative_value": None,
            "reason": "现有业绩事实中没有可识别的合同金额。",
            "source_text": source_text,
        }

    total_candidates = [
        candidate
        for candidate in candidates
        if candidate["total_context"] and not candidate["annual"]
    ]
    fixed_candidates = [
        candidate
        for candidate in candidates
        if not candidate["formula_context"] and not candidate["annual"]
    ]
    annual_candidates = [candidate for candidate in candidates if candidate["annual"]]
    chosen = (total_candidates or fixed_candidates or annual_candidates or candidates)[-1]
    if chosen["formula_context"] and not chosen["total_context"]:
        return {
            "value": None,
            "unit": chosen["unit"],
            "basis": "contract_fact",
            "confirmation": "uncertain",
            "cumulative_value": None,
            "reason": "合同事实为费率、单价或按数量/期限计费，无法直接确认累计合同金额。",
            "observed_value": chosen["raw_value"],
            "source_text": chosen["source_text"],
            "source_fact_index": chosen["fact_index"],
            "image_id": chosen["image_id"],
        }
    if chosen["annual"]:
        return {
            "value": chosen["value"],
            "unit": chosen["unit"] or "万元/年",
            "basis": "contract_fact",
            "confirmation": "confirmed",
            "cumulative_value": None,
            "reason": "已确认年度合同金额，但招标规则要求累计金额，不能直接换算为累计金额。",
            "source_text": chosen["source_text"],
            "source_fact_index": chosen["fact_index"],
            "image_id": chosen["image_id"],
        }
    return {
        "value": chosen["value"],
        "unit": chosen["unit"],
        "basis": "contract_fact",
        "confirmation": "confirmed",
        "cumulative_value": chosen["value"],
        "reason": "已从合同金额事实标准化为万元，可用于累计金额评分。",
        "source_text": chosen["source_text"],
        "source_fact_index": chosen["fact_index"],
        "image_id": chosen["image_id"],
    }


def _performance_facts(artifacts: Mapping[str, Any]) -> list[dict[str, Any]]:
    reviews = _review_entries(artifacts, "10_performance_reviews.json", "performance_reviews")
    facts: list[dict[str, Any]] = []
    for index, review in enumerate(reviews):
        row = review.get("table_row")
        row = dict(row) if isinstance(row, Mapping) else {}
        remark = _as_text(row.get("备注") or row.get("备注或说明") or row.get("remark"))
        role = (
            "qualification"
            if "资格" in remark
            else "scoring"
            if "评分" in remark
            else "unknown"
        )
        checks = review.get("checks_by_key")
        checks = checks if isinstance(checks, Mapping) else {}
        service_content_facts = [_compact_fact(fact) for fact in _fact_entries(review, "service_content")]
        contract_amount_facts = [_compact_fact(fact) for fact in _fact_entries(review, "contract_amount")]
        implementation_time_facts = [_compact_fact(fact) for fact in _fact_entries(review, "implementation_time")]
        signature_dates, signature_date_evidence = _signature_date_evidence(review)
        source_blocks, source_images = _source_ids(review)
        facts.append(
            {
                "entry_index": index,
                "case_number": row.get("序号") or str(index + 1),
                "project_name": row.get("项目名称"),
                "role": role,
                "role_label": remark,
                "overall_status": _as_text(review.get("status")) or "uncertain",
                "table_row": row,
                "service_content_facts": service_content_facts,
                "contract_amount_facts": contract_amount_facts,
                "implementation_time_facts": implementation_time_facts,
                "signature_dates": signature_dates,
                "signature_date_evidence": signature_date_evidence,
                "framework_contract": dict(review.get("framework_contract"))
                if isinstance(review.get("framework_contract"), Mapping)
                else {"status": "uncertain", "reason": "未找到框架合同判断事实。"},
                "materials": [dict(material) for material in review.get("materials", []) if isinstance(material, Mapping)]
                if isinstance(review.get("materials"), list)
                else [],
                "amount": _normalize_contract_amount(contract_amount_facts),
                "source_blocks": source_blocks,
                "source_images": source_images,
                "checks": {
                    str(key): _compact_check(value)
                    for key, value in checks.items()
                    if isinstance(key, str)
                },
            }
        )
    return facts


def _performance_evidence(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for fact in facts:
        evidence.append(
            {
                "artifact": "10_performance_reviews.json",
                "entry_index": fact["entry_index"],
                "case_number": fact["case_number"],
                "role": fact["role"],
                "overall_status": fact["overall_status"],
                "source_blocks": fact.get("source_blocks", []),
                "source_images": fact.get("source_images", []),
            }
        )
    return evidence


def _performance_common_reason(facts: list[dict[str, Any]]) -> str | None:
    if not facts:
        return "未找到可复用的业绩合同检查结果。"
    qualification = [fact for fact in facts if fact["role"] == "qualification"]
    unknown = [fact for fact in facts if fact["role"] == "unknown"]
    if not qualification:
        return "业绩表未可靠标识资格要求业绩，无法确定评分业绩的排除范围。"
    if unknown:
        return "存在未标识为资格要求业绩或评分业绩的业绩，无法可靠确定排除范围。"
    return None


def _condition_result(status: str, reason: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **details}


def _service_condition(fact: Mapping[str, Any]) -> dict[str, Any]:
    entries = fact.get("service_content_facts", [])
    if not isinstance(entries, list) or not entries:
        return _condition_result("uncertain", "未找到合同服务内容事实，无法确认是否属于同类型技术服务。")
    statuses = [_as_text(entry.get("status")) for entry in entries if isinstance(entry, Mapping)]
    text = " ".join(
        " ".join(_as_text(value) for value in (entry.get("value"), *entry.get("evidence_texts", [])))
        for entry in entries
        if isinstance(entry, Mapping)
    )
    if "fail" in statuses:
        return _condition_result("invalid", "现有业绩事实明确未满足同类型服务内容要求。", evidence_text=text)
    if not text or any(status not in {"present", "pass"} for status in statuses):
        return _condition_result("uncertain", "合同服务内容事实不完整，无法确认同类型技术服务属性。", evidence_text=text)
    service_signal = any(term in text for term in ("服务", "运维", "运营", "维护", "巡检"))
    technical_signal = any(
        term in text
        for term in ("技术", "平台", "系统", "运维", "维护", "巡检", "调试", "监控", "设备")
    )
    if service_signal and technical_signal:
        return _condition_result("pass", "合同服务内容包含技术平台、系统或设备运维/维护等技术服务事实。", evidence_text=text)
    return _condition_result("uncertain", "合同服务内容已提取，但缺少足以确认同类型技术服务的事实。", evidence_text=text)


def _proof_condition(fact: Mapping[str, Any], *, require_cumulative_amount: bool) -> dict[str, Any]:
    materials = fact.get("materials", [])
    has_contract_material = any(
        "合同" in _as_text(material.get("material_type"))
        or _as_text(material.get("role")).lower() == "contract"
        for material in materials
        if isinstance(material, Mapping)
    )
    if not has_contract_material:
        return _condition_result("uncertain", "未找到合同关键页或合同材料事实。")
    service = _service_condition(fact)
    if service["status"] != "pass":
        return _condition_result("uncertain", "合同材料存在，但服务内容事实不足以确认当前评分所需证明材料。")
    amount_facts = fact.get("contract_amount_facts", [])
    has_amount_fact = any(
        isinstance(entry, Mapping) and entry.get("status") in {"present", "pass"}
        for entry in amount_facts
    ) if isinstance(amount_facts, list) else False
    amount = fact.get("amount")
    if not has_amount_fact:
        return _condition_result("uncertain", "未找到合同金额页事实。")
    if require_cumulative_amount and amount.get("cumulative_value") is None:
        return _condition_result("uncertain", "合同金额事实不是可直接累计的固定金额。")
    framework = fact.get("framework_contract")
    framework_status = _as_text(framework.get("status")) if isinstance(framework, Mapping) else "uncertain"
    if framework_status in {"yes", "uncertain"}:
        supporting_material = any(
            any(term in json.dumps(material, ensure_ascii=False) for term in ("采购订单", "结算", "发票", "甲方确认"))
            for material in materials
            if isinstance(material, Mapping)
        )
        if not supporting_material:
            return _condition_result("uncertain", "可能属于框架合同，但未找到采购订单、结算或甲方确认材料。")
    return _condition_result("pass", "已找到合同关键页及当前评分所需的服务、金额和合同材料事实。")


def _signature_page_condition(fact: Mapping[str, Any]) -> dict[str, Any]:
    status = _check_status(fact, "signature_page")
    if status == "fail":
        return _condition_result("invalid", "现有检查明确未确认合同签字盖章页。")
    if status == "pass":
        return _condition_result("pass", "现有检查确认合同签字盖章页存在。")
    return _condition_result("uncertain", "未能确认合同签字盖章页。")


def _signature_date_condition(
    fact: Mapping[str, Any],
    *,
    announcement_date: str | None,
) -> dict[str, Any]:
    dates = fact.get("signature_dates", [])
    if not isinstance(dates, list) or not dates:
        if _check_status(fact, "signature_date") == "fail":
            return _condition_result("invalid", "现有检查确认合同签署日期栏为空或不满足要求。")
        return _condition_result("uncertain", "未能从现有事实中确认合同签署日期。")
    if len(set(dates)) != 1:
        return _condition_result("uncertain", "合同签署日期存在多个相互冲突的事实，无法确定用于评分的签署日期。", dates=dates)
    if announcement_date is None:
        return _condition_result("uncertain", "缺少招标公告发布日期，无法判断签署日期是否早于公告发布前一日。", dates=dates)
    signing_date = _parse_date(dates[0])
    announcement = _parse_date(announcement_date)
    if signing_date is None or announcement is None:
        return _condition_result("uncertain", "签署日期或公告发布日期格式无法可靠解析。", dates=dates)
    lower = date(2023, 1, 1)
    if not lower <= signing_date < announcement:
        return _condition_result(
            "invalid",
            "合同签署日期不在2023年1月1日至公告发布前一日范围内。",
            signing_date=signing_date.isoformat(),
            announcement_date=announcement.isoformat(),
        )
    return _condition_result(
        "pass",
        "合同签署日期处于招标文件要求的时间范围内。",
        signing_date=signing_date.isoformat(),
        announcement_date=announcement.isoformat(),
    )


def _performance_rule_case_evaluation(
    fact: Mapping[str, Any],
    *,
    item_number: str,
    announcement_date: str | None,
) -> dict[str, Any]:
    role = _as_text(fact.get("role")) or "unknown"
    overall_status = _as_text(fact.get("overall_status")) or "uncertain"
    base = {
        "entry_index": fact.get("entry_index"),
        "case_number": fact.get("case_number"),
        "project_name": fact.get("project_name"),
        "role": role,
        "overall_status": overall_status,
        "overall_status_ignored": True,
        "amount": fact.get("amount"),
        "source_blocks": fact.get("source_blocks", []),
        "conditions": {},
        "included_in_scoring": False,
    }
    if role == "qualification":
        base.update(
            {
                "current_rule_status": "excluded_by_role",
                "reason": "该业绩在业绩表中明确标记为资格要求业绩，按当前评分规则排除；不使用其overall_status判断排除关系。",
            }
        )
        base["conditions"] = {"role": _condition_result("pass", "业绩角色明确为资格要求业绩。")}
        return base
    if role != "scoring":
        base.update(
            {
                "current_rule_status": "uncertain",
                "reason": "业绩表未明确标记为资格要求业绩或评分业绩，无法建立当前评分项的排除关系。",
            }
        )
        base["conditions"] = {"role": _condition_result("uncertain", "角色标识缺失或无法识别。")}
        return base

    conditions = {
        "role": _condition_result("pass", "业绩表角色明确为评分业绩。"),
        "contract_signing_date": _signature_date_condition(
            fact,
            announcement_date=announcement_date,
        ),
        "same_type_technical_service": _service_condition(fact),
        "signature_page": _signature_page_condition(fact),
        "proof_material": _proof_condition(
            fact,
            require_cumulative_amount=item_number == "011",
        ),
    }
    statuses = [condition["status"] for condition in conditions.values()]
    if "invalid" in statuses:
        current_status = "invalid"
        reason = "；".join(
            condition["reason"] for condition in conditions.values() if condition["status"] == "invalid"
        )
    elif "uncertain" in statuses:
        current_status = "uncertain"
        reason = "；".join(
            condition["reason"] for condition in conditions.values() if condition["status"] == "uncertain"
        )
    else:
        current_status = "valid"
        reason = "已满足当前评分项要求的角色、签署日期、同类型技术服务及证明材料条件。"
    base.update(
        {
            "current_rule_status": current_status,
            "reason": reason,
            "conditions": conditions,
            "included_in_scoring": current_status == "valid",
        }
    )
    return base


def _performance_handler(
    result: dict[str, Any],
    *,
    item_number: str,
    artifacts: Mapping[str, Any],
    tender_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    facts = _performance_facts(artifacts)
    evidence = _performance_evidence(facts)
    related = ["10_performance_reviews.json"] if facts else []
    common_reason = _performance_common_reason(facts)
    if common_reason:
        return _status_result(
            result,
            status="evidence_insufficient",
            reason=common_reason,
            facts={"performance_cases": facts},
            evidence=evidence,
            related_artifacts=related,
        )

    announcement_date = (
        _as_text(tender_evidence.get("announcement_date"))
        if isinstance(tender_evidence, Mapping)
        else None
    ) or None
    case_evaluations = [
        _performance_rule_case_evaluation(
            fact,
            item_number=item_number,
            announcement_date=announcement_date,
        )
        for fact in facts
    ]
    extra = [case for case in case_evaluations if case["role"] == "scoring"]
    valid_extra = [case for case in extra if case["current_rule_status"] == "valid"]
    unresolved = [case for case in extra if case["current_rule_status"] == "uncertain"]
    invalid = [case for case in extra if case["current_rule_status"] == "invalid"]
    qualified_excluded_amount = sum(
        float(case["amount"]["cumulative_value"])
        for case in case_evaluations
        if case["current_rule_status"] == "excluded_by_role"
        and isinstance(case.get("amount"), Mapping)
        and isinstance(case["amount"].get("cumulative_value"), (int, float))
    )
    common_facts = {
        "performance_cases": facts,
        "case_evaluations": case_evaluations,
        "valid_extra_case_count": len(valid_extra),
        "uncertain_extra_case_count": len(unresolved),
        "invalid_extra_case_count": len(invalid),
        "excluded_qualification_case_numbers": [
            case["case_number"] for case in case_evaluations if case["current_rule_status"] == "excluded_by_role"
        ],
        "excluded_qualification_amount": qualified_excluded_amount,
        "announcement_date": announcement_date,
        "announcement_date_source": tender_evidence.get("announcement_date_source")
        if isinstance(tender_evidence, Mapping)
        else None,
    }
    if unresolved:
        if item_number == "010":
            reason = "存在评分业绩无法依据当前评分规则确认是否有效，无法可靠确定新增业绩数量。"
            calculation = {
                "formula": "min(当前评分规则下有效新增评分业绩数量 × 1, 5)",
                "confirmed_score_lower_bound": min(5, len(valid_extra)),
                "potential_score_upper_bound": min(5, len(valid_extra) + len(unresolved)),
                "unresolved_case_numbers": [case["case_number"] for case in unresolved],
            }
        else:
            confirmed_amount = sum(
                float(case["amount"]["cumulative_value"])
                for case in valid_extra
                if isinstance(case.get("amount"), Mapping)
                and isinstance(case["amount"].get("cumulative_value"), (int, float))
            )
            reason = "存在评分业绩的当前评分条件或累计金额无法确认，无法可靠确定金额分档。"
            calculation = {
                "formula": "剔除资格要求业绩后累计当前评分规则确认的合同金额，并按1900/1500/1000万元档位计分",
                "qualified_excluded_amount": qualified_excluded_amount,
                "confirmed_cumulative_amount": confirmed_amount,
                "unresolved_case_numbers": [case["case_number"] for case in unresolved],
            }
        return _status_result(
            result,
            status="evidence_insufficient",
            reason=reason,
            facts=common_facts,
            calculation=calculation,
            evidence=evidence,
            related_artifacts=related,
        )
    if item_number == "010":
        score = min(5, len(valid_extra))
        calculation = {
            "formula": "min(有效新增评分业绩数量 × 1, 5)",
            "valid_extra_case_count": len(valid_extra),
            "excluded_qualification_case_numbers": common_facts["excluded_qualification_case_numbers"],
        }
    else:
        if any(
            not isinstance(case.get("amount"), Mapping)
            or case["amount"].get("cumulative_value") is None
            for case in valid_extra
        ):
            return _status_result(
                result,
                status="evidence_insufficient",
                reason="有效新增评分业绩缺少可用于累计的固定合同金额，无法执行剔除资格业绩后的金额评分。",
                facts=common_facts,
                evidence=evidence,
                related_artifacts=related,
            )
        amount_total = sum(float(case["amount"]["cumulative_value"]) for case in valid_extra)
        score = 5 if amount_total >= 1900 else 3 if amount_total >= 1500 else 1 if amount_total >= 1000 else 0
        calculation = {
            "formula": "剔除资格要求业绩金额后累计评分业绩金额，并按 1900/1500/1000 万元档位计分",
            "qualified_excluded_amount": qualified_excluded_amount,
            "scoring_amount": amount_total,
            "valid_extra_case_count": len(valid_extra),
        }
    return _status_result(
        result,
        status="auto_scored",
        score=score,
        reason="已依据业绩表角色标识和当前评分规则下确认的业绩事实完成资格业绩排除后的确定性计算。",
        facts=common_facts,
        calculation=calculation,
        evidence=evidence,
        related_artifacts=related,
    )


def _condition_brackets(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    conditions = item.get("conditions")
    if not isinstance(conditions, Mapping):
        return []
    for key in ("member_count_brackets", "brackets", "score_brackets", "tiers"):
        values = conditions.get(key)
        if isinstance(values, list):
            return [dict(value) for value in values if isinstance(value, Mapping)]
    return []


def _bracket_score(value: float, brackets: list[Mapping[str, Any]]) -> tuple[float | int, Mapping[str, Any]] | None:
    for bracket in brackets:
        minimum = bracket.get("min")
        maximum = bracket.get("max")
        if minimum is not None and value < float(minimum):
            continue
        if maximum is not None and value > float(maximum):
            continue
        score = bracket.get("score")
        if isinstance(score, (int, float)):
            return score, bracket
    return None


def _team_handler(
    result: dict[str, Any],
    *,
    bid_document: Mapping[str, Any] | None,
    item: Mapping[str, Any],
    filename: str,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    del artifacts
    text = _document_text(bid_document)
    verified_count = None
    match = re.search(r"(?:有效|满足评分|符合要求的?)?团队成员(?:人数|数量)[：:]?\s*(\d+)", text)
    if match:
        verified_count = int(match.group(1))
    if verified_count is None:
        has_team_content = any(
            term in text for term in ("团队成员", "团队人数", "人员名单", "项目团队")
        )
        status = (
            "evidence_insufficient"
            if has_team_content
            else "file_scope_missing"
            if _is_business_only(bid_document, filename)
            else "evidence_insufficient"
        )
        return _status_result(
            result,
            status=status,
            reason=(
                "当前商务投标文件未包含团队成员及身份证、社保、缴费单位等有效人员证明。"
                if status == "file_scope_missing"
                else "未找到同时满足身份证、社保和缴费单位要求的有效团队成员数量。"
            ),
            facts={"verified_member_count": None, "roster_count_not_used": True},
        )
    brackets = _condition_brackets(item) or [
        {"min": 35, "max": None, "score": 5},
        {"min": 25, "max": 34, "score": 3},
        {"min": 15, "max": 24, "score": 1},
        {"min": None, "max": 14, "score": 0},
    ]
    matched = _bracket_score(verified_count, brackets)
    if matched is None:
        return _status_result(
            result,
            status="evidence_insufficient",
            reason="有效团队成员数量无法匹配招标文件的分档条件。",
            facts={"verified_member_count": verified_count},
        )
    score, bracket = matched
    return _status_result(
        result,
        status="auto_scored",
        score=score,
        reason="已使用满足身份证、社保和缴费单位要求的有效人员数量计算，未使用名单行数。",
        facts={"verified_member_count": verified_count, "roster_count_not_used": True},
        calculation={"formula": "按有效团队成员人数分档", "matched_bracket": dict(bracket)},
    )


def _project_manager_handler(
    result: dict[str, Any],
    *,
    bid_document: Mapping[str, Any] | None,
    filename: str,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    text = _document_text(bid_document)
    has_project_manager_content = "项目经理" in text or bool(
        re.search(
            r"项目负责人(?:姓名|[：:]|证书|简历|资质|学历|专业|工作年限|社保)",
            text,
        )
    )
    relevant = [
        entry
        for name, key in (
            ("08_template_text_reviews.json", "template_text_reviews"),
            ("09_attachment_reviews.json", "attachment_reviews"),
        )
        for entry in _review_entries(artifacts, name, key)
        if any(term in json.dumps(entry, ensure_ascii=False) for term in ("项目经理", "项目负责人"))
    ]
    if not relevant and not _has_section_scope(
        bid_document,
        filename,
        ("项目经理", "项目负责人"),
    ):
        status = (
            "evidence_insufficient"
            if has_project_manager_content
            else "file_scope_missing"
            if _is_business_only(bid_document, filename)
            else "evidence_insufficient"
        )
        return _status_result(
            result,
            status=status,
            reason=(
                "当前商务投标文件未包含项目经理的学历、专业、年限、经验、能力证明及社保材料。"
                if status == "file_scope_missing"
                else "未找到项目经理全部必需条件的结构化证据。"
            ),
            facts={"all_required_conditions_confirmed": False, "single_certificate_not_sufficient": True},
            related_artifacts=[
                name
                for name in ("08_template_text_reviews.json", "09_attachment_reviews.json")
                if name in artifacts
            ],
        )
    return _status_result(
        result,
        status="evidence_insufficient",
        reason="已发现项目经理相关内容，但现有产出物未确认学历、专业、工作年限、项目经验、能力证明和社保缴费单位全部满足。",
        facts={"all_required_conditions_confirmed": False, "single_certificate_not_sufficient": True},
        related_artifacts=[
            name
            for name in ("08_template_text_reviews.json", "09_attachment_reviews.json")
            if name in artifacts
        ],
    )


def _technical_handler(
    result: dict[str, Any],
    *,
    bid_document: Mapping[str, Any] | None,
    filename: str,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    reviews = _review_entries(artifacts, "08_template_text_reviews.json", "template_text_reviews")
    technical_review = next(
        (
            entry
            for entry in reviews
            if "技术" in _as_text(entry.get("template_name"))
            or "技术" in _as_text(entry.get("bid_module_name"))
        ),
        None,
    )
    if technical_review is None and not _has_scope(
        bid_document,
        filename,
        ("技术规范书", "技术标", "技术响应"),
    ):
        return _status_result(
            result,
            status="file_scope_missing",
            reason="当前投标文件范围未包含技术标或第五章技术标准和要求响应，不能按全部满足给分。",
            facts={"technical_response_present": False, "deviation_count": None},
        )
    if technical_review is None:
        return _status_result(
            result,
            status="evidence_insufficient",
            reason="发现技术相关文本，但没有完成技术要求逐项偏离检查，无法确定扣分项数量。",
            facts={"technical_response_present": True, "deviation_count": None},
            related_artifacts=["08_template_text_reviews.json"]
            if "08_template_text_reviews.json" in artifacts
            else [],
        )
    status = _as_text(technical_review.get("business_status") or technical_review.get("status"))
    if status == "pass":
        full_score = result.get("full_score")
        return _status_result(
            result,
            status="auto_scored",
            score=full_score if isinstance(full_score, (int, float)) else 0,
            reason="技术要求逐项响应检查结果为全部满足。",
            facts={"technical_response_present": True, "deviation_count": 0},
            calculation={"formula": "满分 - 已确认偏离项数量 × 1", "deviation_count": 0},
            evidence=[_artifact_ref("08_template_text_reviews.json")],
            related_artifacts=["08_template_text_reviews.json"],
        )
    issues = technical_review.get("final_issues") or technical_review.get("issues")
    if not isinstance(issues, list):
        issues = []
    return _status_result(
        result,
        status="evidence_insufficient",
        reason="技术响应检查未能提供可可靠计数的逐项偏离事实。",
        facts={"technical_response_present": True, "deviation_count": None, "reported_issue_count": len(issues)},
        evidence=[_artifact_ref("08_template_text_reviews.json")],
        related_artifacts=["08_template_text_reviews.json"],
    )


def _stability_handler(
    result: dict[str, Any],
    *,
    bid_document: Mapping[str, Any] | None,
    filename: str,
) -> dict[str, Any]:
    text = _document_text(bid_document)
    if not any(term in text for term in ("人员稳定性", "人员流失率", "人员流失")):
        status = "file_scope_missing" if _is_business_only(bid_document, filename) else "evidence_insufficient"
        return _status_result(
            result,
            status=status,
            reason=(
                "当前投标文件范围未包含人员稳定性承诺及流失率事实。"
                if status == "file_scope_missing"
                else "未找到人员稳定性承诺及流失率事实。"
            ),
        )
    percentages = [float(value) for value in _PERCENT_RE.findall(text)]
    if not percentages:
        return _status_result(
            result,
            status="evidence_insufficient",
            reason="已发现人员稳定性相关文字，但没有可核验的流失率比例。",
        )
    ratio = percentages[0]
    score = 5 if ratio <= 5 else 3 if ratio <= 10 else 0
    return _status_result(
        result,
        status="auto_scored",
        score=score,
        reason="已根据投标文件中的人员稳定性流失率和招标文件分档条件计算。",
        facts={"attrition_rate_percent": ratio},
        calculation={"formula": "流失率 ≤5% 得5分；≤10% 得3分；其他得0分"},
    )


def _dispatch_item(
    item: Mapping[str, Any],
    result: dict[str, Any],
    *,
    evidence: Mapping[str, Any],
    bid_file: FileMetadata,
    tender_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    item_id = _as_text(item.get("id"))
    name = _as_text(item.get("name"))
    normalized = f"{item_id} {name}"
    document = evidence.get("bid_document")
    artifacts = evidence.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        artifacts = {}
    if "score_item_002" in normalized or "技术标准和要求的偏离" in name:
        return _technical_handler(
            result,
            bid_document=document,
            filename=bid_file.filename,
            artifacts=artifacts,
        )
    if "score_item_007" in normalized or "项目经理资质" in name:
        return _project_manager_handler(
            result,
            bid_document=document,
            filename=bid_file.filename,
            artifacts=artifacts,
        )
    if "score_item_008" in normalized or "团队成员情况" in name:
        return _team_handler(
            result,
            bid_document=document,
            item=item,
            filename=bid_file.filename,
            artifacts=artifacts,
        )
    if "score_item_010" in normalized or "类似案例1" in name:
        return _performance_handler(
            result,
            item_number="010",
            artifacts=artifacts,
            tender_evidence=tender_evidence,
        )
    if "score_item_011" in normalized or "类似案例2" in name:
        return _performance_handler(
            result,
            item_number="011",
            artifacts=artifacts,
            tender_evidence=tender_evidence,
        )
    if "score_item_012" in normalized or "人员稳定性" in name:
        return _stability_handler(
            result,
            bid_document=document,
            filename=bid_file.filename,
        )
    if "score_item_013" in normalized or "不良行为" in name:
        return _status_result(
            result,
            status="external_data_required",
            reason="该规则依赖中国电信供应商不良行为处理结果等外部系统或评标现场事实，当前产出物没有对应可靠数据。",
            facts={"external_data_available": False},
        )
    if "score_item_014" in normalized or "报价评分" in name:
        return _status_result(
            result,
            status="other_bidder_data_required",
            reason="报价评分需要所有有效投标人的经评审评标价、有效投标人数 n 和基准价 P0，当前只有单个投标文件。",
            facts={"valid_bidder_count": 1, "other_bidder_prices_available": False, "benchmark_price_available": False},
        )
    return _status_result(
        result,
        status="unsupported",
        reason="当前客观评分执行器没有该评分项的确定性规则处理器，保留规则等待后续支持。",
    )


def run_objective_scoring(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
    tender_evidence: Mapping[str, Any] | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
    bid_parse_fallback_used: bool = False,
) -> dict[str, Any]:
    """Execute only objectively computable tender score rules.

    No model, parser, or veto evaluation is performed by this function.
    """

    started_at = time.perf_counter()
    evidence = load_reusable_bid_evidence(
        bid_file,
        bid_document=bid_document,
        artifact_dir=artifact_dir,
        existing_artifacts=existing_artifacts,
    )
    categories = _category_map(evaluation_rules)
    raw_items = evaluation_rules.get("score_items", [])
    score_items: list[dict[str, Any]] = []
    for raw_item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw_item, Mapping) or raw_item.get("evaluation_type") != "objective":
            continue
        item = dict(raw_item)
        category = categories.get(_as_text(item.get("category_id")))
        score_item = _item_base(item, category)
        score_items.append(
            _dispatch_item(
                item,
                score_item,
                evidence=evidence,
                bid_file=bid_file,
                tender_evidence=tender_evidence,
            )
        )

    statuses = {status: 0 for status in sorted(OBJECTIVE_STATUSES)}
    for item in score_items:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    source = {
        "evaluation_rules_artifact": "11_evaluation_rules.json",
        "bid_filename": bid_file.filename,
        "bid_path": evidence["bid_path"],
        "bid_document_artifact": evidence["bid_document_artifact"],
        "bid_document_hash_verified": evidence["bid_document_hash_verified"],
        "reused_artifacts": sorted(
            set(evidence["artifacts"]).intersection(_REUSABLE_ARTIFACT_NAMES)
        ),
        "missing_artifacts": sorted(evidence["missing_artifacts"]),
    }
    if isinstance(tender_evidence, Mapping):
        source["tender_parsed_blocks_artifact"] = tender_evidence.get("artifact_path")
        source["announcement_date"] = tender_evidence.get("announcement_date")
        source["announcement_date_source"] = tender_evidence.get("announcement_date_source")
    payload: dict[str, Any] = {
        "schema_version": "objective-score-v1",
        "source": source,
        "score_items": score_items,
        "stats": {
            "objective_item_count": len(score_items),
            "auto_scored_count": statuses.get("auto_scored", 0),
            "status_counts": statuses,
            "subjective_item_count_excluded": sum(
                1
                for item in raw_items
                if isinstance(item, Mapping)
                and item.get("evaluation_type") in {"subjective", "mixed"}
            )
            if isinstance(raw_items, list)
            else 0,
            "veto_rule_count": len(evaluation_rules.get("veto_rules", []))
            if isinstance(evaluation_rules.get("veto_rules", []), list)
            else 0,
            "score_sum": None,
            "auto_score_sum_computed": False,
            "total_score_computed": False,
            "llm_total_calls": 0,
            "bid_parse_reused": not bid_parse_fallback_used,
            "bid_parse_fallback_used": bid_parse_fallback_used,
            "duplicate_parse": False,
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        },
    }
    if recorder is not None:
        recorder.write_json(OBJECTIVE_SCORE_ARTIFACT, payload)
    return payload
