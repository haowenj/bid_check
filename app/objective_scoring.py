from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
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
    r"\s*(?:万元|万|元)"
)
_PERCENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")


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


def _has_scope(document: Mapping[str, Any] | None, filename: str, terms: tuple[str, ...]) -> bool:
    text = _document_text(document)
    if any(term in text for term in terms):
        return True
    # A filename is only used as a conservative scope signal, never as proof
    # of a scoreable fact.
    normalized = filename.lower()
    return any(term.lower() in normalized for term in terms)


def _is_business_only(document: Mapping[str, Any] | None, filename: str) -> bool:
    text = _document_text(document)
    if "商务" not in filename and "商务" not in text:
        return False
    return not any(term in text for term in ("技术标", "技术规范书", "技术响应", "报价文件", "投标一览表"))


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
        amount_check = checks.get("contract_amount")
        amount = None
        if isinstance(amount_check, Mapping):
            for key in ("amount_value", "amount", "value"):
                raw_amount = amount_check.get(key)
                if isinstance(raw_amount, (int, float)):
                    amount = float(raw_amount)
                    break
        facts.append(
            {
                "entry_index": index,
                "case_number": row.get("序号") or str(index + 1),
                "project_name": row.get("项目名称"),
                "role": role,
                "role_label": remark,
                "overall_status": _as_text(review.get("status")) or "uncertain",
                "table_row": row,
                "amount": amount,
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
    if any(fact["overall_status"] != "pass" for fact in qualification):
        return "资格要求业绩的既有检查结果不是全部通过，无法可靠建立资格业绩排除关系。"
    return None


def _performance_handler(
    result: dict[str, Any],
    *,
    item_number: str,
    artifacts: Mapping[str, Any],
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

    extra = [fact for fact in facts if fact["role"] == "scoring"]
    valid_extra = [fact for fact in extra if fact["overall_status"] == "pass"]
    unresolved = [fact for fact in extra if fact["overall_status"] not in {"pass", "fail"}]
    invalid = [fact for fact in extra if fact["overall_status"] == "fail"]
    if unresolved:
        return _status_result(
            result,
            status="evidence_insufficient",
            reason="存在评分业绩检查结果为不确定，无法确认其是否属于可计分的有效新增业绩。",
            facts={"performance_cases": facts, "valid_extra_case_count": len(valid_extra)},
            evidence=evidence,
            related_artifacts=related,
        )
    if item_number == "010":
        score = min(5, len(valid_extra))
        calculation = {
            "formula": "min(有效新增评分业绩数量 × 1, 5)",
            "valid_extra_case_count": len(valid_extra),
            "excluded_qualification_case_numbers": [
                fact["case_number"]
                for fact in facts
                if fact["role"] == "qualification"
            ],
        }
    else:
        if any(fact.get("amount") is None for fact in valid_extra):
            return _status_result(
                result,
                status="evidence_insufficient",
                reason="有效新增评分业绩缺少可用于累计的固定合同金额，无法执行剔除资格业绩后的金额评分。",
                facts={"performance_cases": facts, "valid_extra_case_count": len(valid_extra)},
                evidence=evidence,
                related_artifacts=related,
            )
        amount_total = sum(float(fact["amount"]) for fact in valid_extra)
        score = 5 if amount_total >= 1900 else 3 if amount_total >= 1500 else 1 if amount_total >= 1000 else 0
        calculation = {
            "formula": "剔除资格要求业绩金额后累计评分业绩金额，并按 1900/1500/1000 万元档位计分",
            "qualified_excluded_amount": 0,
            "scoring_amount": amount_total,
            "valid_extra_case_count": len(valid_extra),
        }
    return _status_result(
        result,
        status="auto_scored",
        score=score,
        reason="已依据业绩表角色标识和既有业绩合同检查结果完成资格业绩排除后的确定性计算。",
        facts={
            "performance_cases": facts,
            "valid_extra_case_count": len(valid_extra),
            "invalid_extra_case_count": len(invalid),
        },
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
            term in text for term in ("团队成员", "人员名单", "身份证", "社保", "缴费单位")
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
    relevant = [
        entry
        for name, key in (
            ("08_template_text_reviews.json", "template_text_reviews"),
            ("09_attachment_reviews.json", "attachment_reviews"),
        )
        for entry in _review_entries(artifacts, name, key)
        if any(term in json.dumps(entry, ensure_ascii=False) for term in ("项目经理", "项目负责人"))
    ]
    if not relevant and not any(term in text for term in ("项目经理", "项目负责人")):
        status = "file_scope_missing" if _is_business_only(bid_document, filename) else "evidence_insufficient"
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
        return _performance_handler(result, item_number="010", artifacts=artifacts)
    if "score_item_011" in normalized or "类似案例2" in name:
        return _performance_handler(result, item_number="011", artifacts=artifacts)
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
    recorder: ComplianceExtractionRecorder | None = None,
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
            )
        )

    statuses = {status: 0 for status in sorted(OBJECTIVE_STATUSES)}
    for item in score_items:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    all_auto_scored = bool(score_items) and all(
        item["status"] == "auto_scored" for item in score_items
    )
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
            "score_sum": (
                sum(float(item["score"]) for item in score_items)
                if all_auto_scored
                else None
            ),
            "total_score_computed": False,
            "llm_total_calls": 0,
            "duplicate_parse": False,
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        },
    }
    if recorder is not None:
        recorder.write_json(OBJECTIVE_SCORE_ARTIFACT, payload)
    return payload
