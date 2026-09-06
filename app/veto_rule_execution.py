from __future__ import annotations

import re
import time
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata
from app.objective_scoring import load_reusable_bid_evidence

VETO_RULE_REVIEW_ARTIFACT = "veto_rule_reviews.json"
VETO_STATUSES = frozenset(
    {
        "triggered",
        "not_triggered",
        "not_applicable",
        "evidence_insufficient",
        "file_scope_missing",
        "external_data_required",
        "other_bidder_data_required",
        "manual_review_required",
    }
)

_REUSABLE_ARTIFACT_NAMES = (
    "08_template_text_reviews.json",
    "09_attachment_reviews.json",
    "10_file_requirement_reviews.json",
    "10_performance_reviews.json",
)

_OTHER_BIDDER_TERMS = (
    "串通投标",
    "异常一致",
    "异常关联",
    "投标人之间",
    "多个投标人",
    "关联投标",
)
_EXTERNAL_TERMS = (
    "信用中国",
    "裁判文书",
    "供应商不良行为",
    "外部系统",
    "处罚记录",
    "失信被执行人",
)
_MANUAL_TERMS = (
    "低于成本",
    "成本价",
    "算术修正",
    "算术错误",
    "接受修正",
    "评标委员会认定",
    "澄清说明",
    "现场认定",
    "逾期送达",
    "未送达指定地点",
    "密封",
    "不予接收",
)
_PRELIMINARY_TERMS = (
    "初步评审",
    "资格审查",
    "形式评审",
    "响应性评审",
)
_COMMON_ANCHORS = {
    "投标文件",
    "招标文件",
    "投标人",
    "供应商",
    "提供",
    "应提供",
    "须提供",
    "评标委员会",
    "不符合要求",
    "不满足要求",
    "符合性审查",
    "资格审查",
    "资格资料",
    "审查资料",
    "证明材料",
}
_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_APPLICABILITY_STATUSES = frozenset(
    {"applicable", "not_applicable", "applicability_uncertain"}
)
_PROJECT_SPECIFIC_SECTIONS = (
    "投标人须知前附表",
    "招标文件否决投标条款汇总",
    "专用部分",
    "前附表",
)
_SUBCONDITION_BLOCKING_STATUSES = frozenset(
    {
        "evidence_insufficient",
        "file_scope_missing",
        "external_data_required",
        "other_bidder_data_required",
        "manual_review_required",
    }
)
_SUBCONDITION_DEPENDENCY_KEYS = (
    "external_data_required",
    "other_bidder_data_required",
    "manual_review_required",
)
_VETO_009_MARKER_RE = re.compile(
    r"[（(]\s*(\d{1,2})\s*[）)]|(?<![\d.])(\d{1,2})[、.](?!\d)"
)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _copy_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"section": "", "block_ids": [], "source_text": ""}
    return deepcopy(dict(value))


def _rule_text(rule: Mapping[str, Any]) -> str:
    return " ".join(
        value
        for value in (
            _as_text(rule.get("name")),
            _as_text(rule.get("trigger_condition")),
            _as_text(rule.get("original_rule")),
        )
        if value
    )


def _document_text(document: Mapping[str, Any] | None) -> str:
    if not isinstance(document, Mapping):
        return ""
    sections = document.get("sections", [])
    blocks = document.get("blocks", [])
    values: list[str] = []
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, Mapping):
                continue
            values.append(_as_text(section.get("title")))
            path = section.get("path")
            if isinstance(path, list):
                values.extend(_as_text(value) for value in path)
    if isinstance(blocks, list):
        values.extend(
            _as_text(block.get("text"))
            for block in blocks
            if isinstance(block, Mapping)
        )
    return "\n".join(value for value in values if value)


def _section_titles(document: Mapping[str, Any] | None) -> str:
    if not isinstance(document, Mapping):
        return ""
    sections = document.get("sections", [])
    if not isinstance(sections, list):
        return ""
    return "\n".join(
        _as_text(section.get("title"))
        for section in sections
        if isinstance(section, Mapping)
    )


def _is_business_only(
    document: Mapping[str, Any] | None,
    filename: str,
) -> bool:
    titles = _section_titles(document)
    if "商务" not in filename and "商务" not in titles:
        return False
    return not any(
        term in titles
        for term in ("技术标", "技术规范书", "技术响应", "报价文件", "投标一览表")
    )


def _is_preliminary_aggregate_text(text: str) -> bool:
    if _contains_any(text, ("逾期送达", "未送达指定地点", "密封", "不予接收")):
        return False
    if _contains_any(text, _PRELIMINARY_TERMS) and _contains_any(
        text,
        ("有一项", "任一项", "不通过", "不符合"),
    ):
        return True
    return _contains_any(
        text,
        ("任一情形", "存在以下任一", "下列情形之一", "以下情形之一"),
    )


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _parse_threshold(text: str) -> int | None:
    match = re.search(r"超过\s*([0-9]{1,4}|[一二两三四五六七八九十百]+)\s*(?:项|条|个)?", text)
    if match is None:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        parts = raw.split("十")
        tens = _CHINESE_DIGITS.get(parts[0], 1) if parts[0] else 1
        ones = _CHINESE_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return sum(_CHINESE_DIGITS.get(char, 0) for char in raw)


def _shared_anchor(left: str, right: str) -> str | None:
    left = re.sub(r"\s+", "", left)
    right = re.sub(r"\s+", "", right)
    for length in range(min(8, len(left)), 2, -1):
        for index in range(0, len(left) - length + 1):
            candidate = left[index : index + length]
            if candidate in _COMMON_ANCHORS:
                continue
            if candidate in right and not all(char in "一项任一有无的了" for char in candidate):
                return candidate
    return None


def _artifact_entries(
    evidence: Mapping[str, Any],
    artifact_name: str,
    *keys: str,
) -> list[tuple[int, dict[str, Any]]]:
    artifacts = evidence.get("artifacts", {})
    payload = artifacts.get(artifact_name) if isinstance(artifacts, Mapping) else None
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        raw_entries = payload.get(key)
        if isinstance(raw_entries, list):
            return [
                (index, dict(entry))
                for index, entry in enumerate(raw_entries)
                if isinstance(entry, Mapping)
            ]
    return []


def _walk_evidence(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        if any(
            key in value
            for key in ("block_id", "image_id", "section_id", "evidence_image_ids")
        ):
            found.append(dict(value))
        for nested in value.values():
            found.extend(_walk_evidence(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_walk_evidence(nested))
    return found


def _bid_evidence(entry: Mapping[str, Any]) -> dict[str, list[str]]:
    block_ids: list[str] = []
    image_ids: list[str] = []
    for item in _walk_evidence(entry):
        for key in ("block_id", "section_id"):
            value = item.get(key)
            if value and str(value) not in block_ids:
                block_ids.append(str(value))
        for key in ("image_id",):
            value = item.get(key)
            if value and str(value) not in image_ids:
                image_ids.append(str(value))
        for key in ("image_ids", "evidence_image_ids"):
            values = item.get(key)
            if isinstance(values, list):
                for value in values:
                    if value and str(value) not in image_ids:
                        image_ids.append(str(value))
    return {"block_ids": block_ids, "image_ids": image_ids}


def _merge_bid_evidence(*values: Mapping[str, Any]) -> dict[str, list[str]]:
    merged = {"block_ids": [], "image_ids": []}
    for value in values:
        for key in merged:
            items = value.get(key, []) if isinstance(value, Mapping) else []
            if not isinstance(items, list):
                continue
            for item in items:
                if item and str(item) not in merged[key]:
                    merged[key].append(str(item))
    return merged


def _review_base(rule: Mapping[str, Any]) -> dict[str, Any]:
    source = _copy_source(rule.get("source"))
    evidence_requirements = rule.get("evidence_requirements", [])
    return {
        "id": _as_text(rule.get("id")),
        "name": _as_text(rule.get("name")),
        "original_rule": _as_text(rule.get("original_rule")),
        "trigger_condition": _as_text(rule.get("trigger_condition")),
        "consequence": _as_text(rule.get("consequence")),
        "additional_consequence": deepcopy(rule.get("additional_consequence")),
        "evidence_requirements": (
            deepcopy(evidence_requirements)
            if isinstance(evidence_requirements, list)
            else []
        ),
        "tender_rule_source": source,
        "applicability": {
            "status": "applicable",
            "reason": "该规则已由招标文件编译为当前项目正式规则，未发现明确的不适用事实。",
            "facts": [],
            "evidence": [],
        },
        "status": "evidence_insufficient",
        "triggered": False,
        "facts_required": [],
        "confirmed_facts": [],
        "reason": "",
        "evidence": [],
        "bid_evidence": {"block_ids": [], "image_ids": []},
        "related_artifacts": [],
        "dependencies": {
            "external_data_required": False,
            "other_bidder_data_required": False,
            "manual_review_required": False,
        },
        "parent_rule_ids": [],
        "triggered_by": [],
        "rule_relations": [],
    }


def _tender_context_records(
    evaluation_rules: Mapping[str, Any],
    tender_evidence: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(tender_evidence, Mapping):
        blocks = tender_evidence.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                text = _as_text(block.get("text"))
                if not text:
                    continue
                block_id = _as_text(block.get("block_id"))
                records.append(
                    {
                        "section": _as_text(block.get("section")),
                        "block_ids": [block_id] if block_id else [],
                        "source_text": text,
                        "artifact": _as_text(tender_evidence.get("artifact_path"))
                        or "01_parsed_blocks.json",
                    }
                )
    if records:
        return records

    source_sections = evaluation_rules.get("source_sections", [])
    if not isinstance(source_sections, list):
        return records
    for source in source_sections:
        if not isinstance(source, Mapping):
            continue
        text = _as_text(source.get("source_text"))
        if not text:
            continue
        block_ids = source.get("block_ids", [])
        records.append(
            {
                "section": _as_text(source.get("section")),
                "block_ids": deepcopy(block_ids) if isinstance(block_ids, list) else [],
                "source_text": text,
                "artifact": "11_evaluation_rules.json",
            }
        )
    return records


def _is_project_specific_tender_record(record: Mapping[str, Any]) -> bool:
    section = _as_text(record.get("section"))
    text = _as_text(record.get("source_text"))
    return _contains_any(section, _PROJECT_SPECIFIC_SECTIONS) or bool(
        re.search(
            r"(?:1\.4\s*最高投标限价|3\.3\.3\s*最高投标限价|"
            r"3\.5(?:\.1)?\s*投标保证金|第一部分：专用部分)",
            text,
        )
    )


def _applicability_for_rule(
    rule: Mapping[str, Any],
    *,
    evaluation_rules: Mapping[str, Any],
    tender_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rule_name = _as_text(rule.get("name"))
    records = [
        record
        for record in _tender_context_records(evaluation_rules, tender_evidence)
        if _is_project_specific_tender_record(record)
    ]

    kind = None
    if "最高投标限价" in rule_name:
        kind = "highest_bid_limit"
        negative_terms = (
            "不设置最高投标限价",
            "不设最高投标限价",
            "不涉及最高投标限价",
        )
        positive_terms = ("设置最高投标限价", "设有最高投标限价")
    elif "投标保证金" in rule_name or "投标担保" in rule_name:
        kind = "bid_bond"
        negative_terms = (
            "无需递交投标保证金",
            "不要求递交投标保证金",
            "不涉及要求递交投标保证金",
            "不设置投标保证金",
        )
        positive_terms = ("要求递交投标保证金", "要求投标人递交投标保证金")
    else:
        return {
            "status": "applicable",
            "reason": "该规则不是当前已识别的项目条件性规则，按编译结果进入执行。",
            "facts": [],
            "evidence": [],
        }

    relevant: list[dict[str, Any]] = []
    for record in records:
        record_text = _as_text(record.get("source_text"))
        if kind == "highest_bid_limit" and "最高投标限价" not in record_text:
            continue
        if kind == "bid_bond" and "投标保证金" not in record_text:
            continue
        relevant.append(record)

    if not relevant:
        return {
            "status": "applicability_uncertain",
            "reason": "当前招标侧没有可核验的项目专用条件，不能确定该条件性规则是否适用。",
            "facts": [],
            "evidence": [],
        }

    matched_negative: list[dict[str, Any]] = []
    matched_positive: list[dict[str, Any]] = []
    for record in relevant:
        record_text = _as_text(record.get("source_text"))
        negative = [term for term in negative_terms if term in record_text]
        positive = [term for term in positive_terms if term in record_text]
        if negative:
            matched_negative.append({"record": record, "terms": negative})
        elif positive:
            matched_positive.append({"record": record, "terms": positive})

    if matched_negative:
        facts = [
            {
                "kind": kind,
                "status": "not_applicable",
                "matched_terms": item["terms"],
            }
            for item in matched_negative
        ]
        evidence = [
            {
                "artifact": item["record"]["artifact"],
                "block_ids": item["record"]["block_ids"],
                "source_text": item["record"]["source_text"],
                "matched_terms": item["terms"],
            }
            for item in matched_negative
        ]
        label = "最高投标限价" if kind == "highest_bid_limit" else "投标保证金"
        return {
            "status": "not_applicable",
            "reason": f"招标文件项目专用条件明确：本项目不适用{label}否决规则。",
            "facts": facts,
            "evidence": evidence,
        }

    if matched_positive:
        facts = [
            {
                "kind": kind,
                "status": "applicable",
                "matched_terms": item["terms"],
            }
            for item in matched_positive
        ]
        evidence = [
            {
                "artifact": item["record"]["artifact"],
                "block_ids": item["record"]["block_ids"],
                "source_text": item["record"]["source_text"],
                "matched_terms": item["terms"],
            }
            for item in matched_positive
        ]
        return {
            "status": "applicable",
            "reason": "招标文件项目专用条件明确该条件性规则适用。",
            "facts": facts,
            "evidence": evidence,
        }

    return {
        "status": "applicability_uncertain",
        "reason": "已找到项目专用条款，但其内容不足以明确判断该条件性规则是否适用。",
        "facts": [],
        "evidence": [],
    }


def _has_ordinary_fail(evidence: Mapping[str, Any]) -> bool:
    artifacts = evidence.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        return False
    for payload in artifacts.values():
        if not isinstance(payload, Mapping):
            continue
        for key in (
            "template_text_reviews",
            "attachment_reviews",
            "performance_reviews",
            "requirements",
            "file_requirement_reviews",
        ):
            entries = payload.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                if _as_text(entry.get("status")) == "fail":
                    return True
                issues = entry.get("issues")
                if isinstance(issues, list) and any(
                    isinstance(issue, Mapping)
                    and _as_text(issue.get("status")) == "fail"
                    for issue in issues
                ):
                    return True
    return False


def _default_review(
    review: dict[str, Any],
    *,
    ordinary_fail_present: bool = False,
) -> dict[str, Any]:
    if not review["facts_required"]:
        review["facts_required"] = [
            _as_text(review.get("trigger_condition")) or "当前规则触发条件的确定性事实"
        ]
    if ordinary_fail_present:
        review["reason"] = (
            "现有产物仅提供普通合规检查问题，未证明该问题属于当前规则对应的正式评审项；"
            "不能把普通 fail 直接升级为否决。"
        )
    else:
        review["reason"] = (
            "当前已有投标文件事实和检查产物不足以将该规则的触发条件与明确证据对应，"
            "不能据此判定触发或未触发。"
        )
    return review


def _finish_review(
    review: dict[str, Any],
    *,
    status: str,
    reason: str,
    facts_required: list[str] | None = None,
    confirmed_facts: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    bid_evidence: Mapping[str, Any] | None = None,
    related_artifacts: list[str] | None = None,
    dependencies: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    if status not in VETO_STATUSES:
        raise ValueError(f"unknown veto status: {status}")
    review.update(
        {
            "status": status,
            "triggered": status == "triggered",
            "reason": reason,
            "facts_required": list(facts_required or []),
            "confirmed_facts": list(confirmed_facts or []),
            "evidence": list(evidence or []),
            "bid_evidence": _merge_bid_evidence(
                review.get("bid_evidence", {}),
                bid_evidence or {},
            ),
            "related_artifacts": list(dict.fromkeys(related_artifacts or [])),
        }
    )
    if dependencies:
        review["dependencies"].update(
            {
                key: bool(value)
                for key, value in dependencies.items()
                if key in review["dependencies"]
            }
        )
    if status == "manual_review_required":
        review["dependencies"]["manual_review_required"] = True
    if status == "external_data_required":
        review["dependencies"]["external_data_required"] = True
    if status == "other_bidder_data_required":
        review["dependencies"]["other_bidder_data_required"] = True
    return review


def _extract_veto_009_conditions(original_rule: str) -> list[str]:
    matches = list(_VETO_009_MARKER_RE.finditer(original_rule))
    for start, marker in enumerate(matches):
        first_index = int(marker.group(1) or marker.group(2))
        if first_index != 1:
            continue
        selected = matches[start : start + 16]
        indexes = [
            int(item.group(1) or item.group(2))
            for item in selected
        ]
        if indexes != list(range(1, 17)):
            continue
        conditions: list[str] = []
        for index, item in enumerate(selected):
            end = selected[index + 1].start() if index < 15 else len(original_rule)
            condition = original_rule[item.end() : end].strip().rstrip("；;。")
            conditions.append(condition)
        if all(conditions):
            return conditions
    return []


def _subcondition_base(
    parent_rule: Mapping[str, Any],
    index: int | str,
    condition: str,
) -> dict[str, Any]:
    suffix = f"{index:02d}" if isinstance(index, int) else str(index)
    child = _review_base(
        {
            "id": f"veto_009_{suffix}",
            "name": condition,
            "original_rule": condition,
            "trigger_condition": condition,
            "consequence": parent_rule.get("consequence"),
            "additional_consequence": parent_rule.get("additional_consequence"),
            "evidence_requirements": parent_rule.get("evidence_requirements", []),
            "source": parent_rule.get("source"),
        }
    )
    child.update(
        {
            "index": index,
            "condition": condition,
            "original_condition": condition,
        }
    )
    return child


def _applicability_evidence(
    applicability: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    evidence = [
        dict(item)
        for item in applicability.get("evidence", [])
        if isinstance(item, Mapping)
    ]
    related_artifacts = list(
        dict.fromkeys(
            _as_text(item.get("artifact"))
            for item in evidence
            if _as_text(item.get("artifact"))
        )
    )
    return evidence, related_artifacts


def _finish_not_applicable_subcondition(
    child: dict[str, Any],
    applicability: Mapping[str, Any],
) -> dict[str, Any]:
    evidence, related_artifacts = _applicability_evidence(applicability)
    child["applicability"] = dict(applicability)
    return _finish_review(
        child,
        status="not_applicable",
        reason=_as_text(applicability.get("reason")),
        facts_required=["当前项目/投标人的适用性事实"],
        confirmed_facts=[
            dict(item)
            for item in applicability.get("facts", [])
            if isinstance(item, Mapping)
        ],
        evidence=evidence,
        related_artifacts=related_artifacts,
    )


def _finish_uncertain_applicability_subcondition(
    child: dict[str, Any],
    applicability: Mapping[str, Any],
) -> dict[str, Any]:
    child["applicability"] = dict(applicability)
    return _finish_review(
        child,
        status="evidence_insufficient",
        reason=_as_text(applicability.get("reason")),
        facts_required=["当前项目/投标人的适用性事实"],
    )


def _joint_venture_applicability(
    evaluation_rules: Mapping[str, Any],
    tender_evidence: Mapping[str, Any] | None,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    records = _tender_context_records(evaluation_rules, tender_evidence)
    rejected = [
        record
        for record in records
        if _contains_any(
            _as_text(record.get("source_text")),
            ("不接受联合体投标", "不允许联合体投标"),
        )
    ]
    if rejected:
        return {
            "status": "not_applicable",
            "reason": "招标文件明确本项目不接受联合体投标，该子条件不适用。",
            "facts": [
                {"status": "not_applicable", "kind": "joint_venture"}
            ],
            "evidence": [
                {
                    "artifact": record.get("artifact"),
                    "block_ids": record.get("block_ids", []),
                    "source_text": record.get("source_text", ""),
                    "matched_terms": ["不接受联合体投标"],
                }
                for record in rejected
            ],
        }
    allowed = [
        record
        for record in records
        if _contains_any(
            _as_text(record.get("source_text")),
            ("允许联合体投标", "接受联合体投标"),
        )
    ]
    if not allowed:
        return {
            "status": "applicability_uncertain",
            "reason": "未找到能够确认本项目是否允许联合体投标的招标侧事实。",
            "facts": [],
            "evidence": [],
        }
    bid_text = _document_text(evidence.get("bid_document"))
    is_joint_bid = _contains_any(
        bid_text,
        ("联合体牵头人", "联合体成员", "联合体协议", "组成联合体"),
    )
    if not is_joint_bid:
        return {
            "status": "not_applicable",
            "reason": "招标文件允许联合体，但当前投标文件没有形成联合体投标事实，该子条件不适用。",
            "facts": [
                {"status": "not_applicable", "kind": "joint_venture", "bid_marker_found": False}
            ],
            "evidence": [
                {
                    "artifact": record.get("artifact"),
                    "block_ids": record.get("block_ids", []),
                    "source_text": record.get("source_text", ""),
                    "matched_terms": ["允许联合体投标"],
                }
                for record in allowed
            ],
        }
    return {
        "status": "applicable",
        "reason": "招标文件允许联合体且当前投标文件出现联合体投标事实。",
        "facts": [{"status": "applicable", "kind": "joint_venture"}],
        "evidence": [],
    }


def _goods_packaging_applicability(
    evaluation_rules: Mapping[str, Any],
    tender_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    records = _tender_context_records(evaluation_rules, tender_evidence)
    excluded_terms = (
        "不涉及货物包装",
        "不适用货物包装",
        "无货物包装要求",
        "不涉及包装、检验标准和方法",
    )
    for record in records:
        text = _as_text(record.get("source_text"))
        matched = [term for term in excluded_terms if term in text]
        if matched:
            return {
                "status": "not_applicable",
                "reason": "招标文件明确当前项目不存在货物包装、检验标准和方法要求，该子条件不适用。",
                "facts": [{"status": "not_applicable", "kind": "goods_packaging", "matched_terms": matched}],
                "evidence": [
                    {
                        "artifact": record.get("artifact"),
                        "block_ids": record.get("block_ids", []),
                        "source_text": text,
                        "matched_terms": matched,
                    }
                ],
            }
    for record in records:
        text = _as_text(record.get("source_text"))
        project_name = re.search(r"项目名称\s*[:：]\s*([^<\n]{0,160})", text)
        if project_name and "服务" in project_name.group(1) and "货物" not in project_name.group(1):
            return {
                "status": "not_applicable",
                "reason": "招标文件项目名称明确为服务项目，未形成货物包装、检验标准和方法的项目适用事实。",
                "facts": [
                    {
                        "status": "not_applicable",
                        "kind": "goods_packaging",
                        "inferred_from": "service_project_name",
                    }
                ],
                "evidence": [
                    {
                        "artifact": record.get("artifact"),
                        "block_ids": record.get("block_ids", []),
                        "source_text": text,
                        "matched_terms": ["项目名称", "服务"],
                    }
                ],
            }
    return {
        "status": "applicability_uncertain",
        "reason": "当前招标侧没有足够事实判断货物包装、检验标准和方法是否适用于本项目。",
        "facts": [],
        "evidence": [],
    }


def _aggregate_nested_subconditions(
    review: dict[str, Any],
    nodes: list[dict[str, Any]],
    *,
    summary_key: str,
) -> dict[str, Any]:
    statuses = [_as_text(node.get("status")) for node in nodes]
    triggered = [node for node in nodes if node.get("status") == "triggered"]
    blocking = [
        node
        for node in nodes
        if _as_text(node.get("status")) in _SUBCONDITION_BLOCKING_STATUSES
    ]
    dependencies = {
        key: any(
            isinstance(node.get("dependencies"), Mapping)
            and bool(node["dependencies"].get(key))
            for node in nodes
        )
        for key in _SUBCONDITION_DEPENDENCY_KEYS
    }
    review["dependencies"].update(dependencies)
    review[summary_key] = {
        "total": len(nodes),
        "status_counts": {
            status: statuses.count(status)
            for status in sorted(VETO_STATUSES)
            if status in statuses
        },
        "blocking_statuses": sorted(
            set(_as_text(node.get("status")) for node in blocking)
        ),
    }
    evidence = [
        dict(item)
        for node in nodes
        for item in node.get("evidence", [])
        if isinstance(item, Mapping)
    ]
    related_artifacts = list(
        dict.fromkeys(
            _as_text(item.get("artifact"))
            for node in nodes
            for item in node.get("evidence", [])
            if isinstance(item, Mapping) and _as_text(item.get("artifact"))
        )
    )
    bid_evidence = _merge_bid_evidence(
        *(
            node.get("bid_evidence", {})
            for node in nodes
            if isinstance(node.get("bid_evidence"), Mapping)
        )
    )
    facts = [
        {
            "id": _as_text(node.get("id")),
            "status": _as_text(node.get("status")),
            "triggered": bool(node.get("triggered")),
            "confirmed_facts": list(node.get("confirmed_facts", [])),
        }
        for node in nodes
    ]
    facts_required = list(
        dict.fromkeys(
            _as_text(item)
            for node in nodes
            for item in node.get("facts_required", [])
            if _as_text(item)
        )
    )
    if triggered:
        return _finish_review(
            review,
            status="triggered",
            reason="该复合否决条件的内部子条件已有明确触发事实。",
            facts_required=facts_required,
            confirmed_facts=facts,
            evidence=evidence,
            bid_evidence=bid_evidence,
            related_artifacts=related_artifacts,
            dependencies=dependencies,
        )
    if blocking:
        blocking_statuses = {
            _as_text(node.get("status"))
            for node in blocking
        }
        status = next(iter(blocking_statuses)) if len(blocking_statuses) == 1 else "evidence_insufficient"
        blocking_ids = [
            _as_text(node.get("id"))
            for node in blocking
            if _as_text(node.get("id"))
        ]
        return _finish_review(
            review,
            status=status,
            reason=(
                f"该复合否决条件仍有未完成判断的内部子条件：{', '.join(blocking_ids)}；"
                "未触发和不适用子条件不能替代这些未决事实。"
            ),
            facts_required=facts_required,
            confirmed_facts=facts,
            evidence=evidence,
            bid_evidence=bid_evidence,
            related_artifacts=related_artifacts,
            dependencies=dependencies,
        )
    return _finish_review(
        review,
        status="not_triggered",
        reason="所有适用的内部子条件均已明确确认未触发，不适用子条件不参与触发判断。",
        facts_required=facts_required,
        confirmed_facts=facts,
        evidence=evidence,
        bid_evidence=bid_evidence,
        related_artifacts=related_artifacts,
        dependencies=dependencies,
    )


def _explicit_fail_status(value: Any) -> bool:
    return _as_text(value).casefold() == "fail"


def _review_issue_entries(
    evidence: Mapping[str, Any],
) -> list[tuple[str, int, dict[str, Any], dict[str, Any]]]:
    entries: list[tuple[str, int, dict[str, Any], dict[str, Any]]] = []
    for artifact_name, key in (
        ("08_template_text_reviews.json", "template_text_reviews"),
        ("09_attachment_reviews.json", "attachment_reviews"),
        ("10_performance_reviews.json", "performance_reviews"),
        ("10_file_requirement_reviews.json", "requirements"),
    ):
        for entry_index, entry in _artifact_entries(evidence, artifact_name, key):
            issues = entry.get("issues")
            if isinstance(issues, list):
                for issue in issues:
                    if isinstance(issue, Mapping):
                        entries.append(
                            (artifact_name, entry_index, entry, dict(issue))
                        )
            else:
                entries.append((artifact_name, entry_index, entry, entry))
    return entries


def _threshold_review(
    review: dict[str, Any],
    *,
    evidence: Mapping[str, Any],
    threshold: int,
) -> dict[str, Any]:
    counted: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    related: list[str] = []
    bid_refs: list[dict[str, Any]] = []
    evidence_refs: list[dict[str, Any]] = []
    for artifact_name, entry_index, entry, issue in _review_issue_entries(evidence):
        issue_text = " ".join(
            _as_text(issue.get(key))
            for key in ("type", "name", "reason", "requirement")
        )
        if "非实质性" not in issue_text:
            continue
        related.append(artifact_name)
        refs = _bid_evidence(issue)
        refs = _merge_bid_evidence(refs, _bid_evidence(entry))
        bid_refs.append(refs)
        evidence_refs.append(
            {
                "artifact": artifact_name,
                "entry_index": entry_index,
                "issue_type": _as_text(issue.get("type")),
                "reason": _as_text(issue.get("reason")),
                "bid_evidence": refs,
            }
        )
        fact = {
            "artifact": artifact_name,
            "entry_index": entry_index,
            "status": _as_text(issue.get("status")) or _as_text(entry.get("status")),
            "issue_type": _as_text(issue.get("type")),
            "reason": _as_text(issue.get("reason")),
        }
        if _explicit_fail_status(issue.get("status")):
            fact["counted_failure"] = True
            counted.append(fact)
        elif _as_text(issue.get("status")) in {"uncertain", "pending"} or (
            _as_text(entry.get("status")) == "uncertain"
        ):
            fact["counted_failure"] = False
            uncertain.append(fact)

    coverage_complete = _coverage_complete(evidence, related)
    confirmed = [
        {
            "counted_failure_count": len(counted),
            "threshold": threshold,
            "coverage_complete": coverage_complete,
            "counted_failures": counted,
            "uncertain_items": uncertain,
        }
    ]
    if uncertain:
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason="已确认部分非实质性偏离，但仍存在未确定项目，不能完整判断阈值是否成立。",
            facts_required=["全部非实质性条款逐项检查结果"],
            confirmed_facts=confirmed,
            evidence=evidence_refs,
            related_artifacts=related,
            bid_evidence=_merge_bid_evidence(*bid_refs),
        )
    if not coverage_complete:
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason="已累计明确的不满足项，但当前产物没有证明全部非实质性条款均已覆盖，不能据此触发或判定未触发。",
            facts_required=["全部非实质性条款逐项检查结果"],
            confirmed_facts=confirmed,
            evidence=evidence_refs,
            related_artifacts=related,
            bid_evidence=_merge_bid_evidence(*bid_refs),
        )
    if len(counted) > threshold:
        return _finish_review(
            review,
            status="triggered",
            reason="完整覆盖的非实质性条款检查明确显示不满足项数量超过招标文件阈值。",
            facts_required=["全部非实质性条款逐项检查结果"],
            confirmed_facts=confirmed,
            evidence=evidence_refs,
            related_artifacts=related,
            bid_evidence=_merge_bid_evidence(*bid_refs),
        )
    return _finish_review(
        review,
        status="not_triggered",
        reason="完整覆盖的非实质性条款检查显示明确不满足项数量未超过招标文件阈值。",
        facts_required=["全部非实质性条款逐项检查结果"],
        confirmed_facts=confirmed,
        evidence=evidence_refs,
        related_artifacts=related,
        bid_evidence=_merge_bid_evidence(*bid_refs),
    )


def _coverage_complete(
    evidence: Mapping[str, Any],
    artifact_names: list[str],
) -> bool:
    artifacts = evidence.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        return False
    for name in dict.fromkeys(artifact_names):
        payload = artifacts.get(name)
        if not isinstance(payload, Mapping):
            continue
        stats = payload.get("stats")
        if isinstance(stats, Mapping) and any(
            stats.get(key) is True
            for key in ("coverage_complete", "all_items_checked", "complete")
        ):
            return True
        if any(
            payload.get(key) is True
            for key in ("coverage_complete", "all_items_checked", "complete")
        ):
            return True
    return False


def _direct_material_review(
    review: dict[str, Any],
    *,
    rule: Mapping[str, Any],
    evidence: Mapping[str, Any],
    requirement_terms: tuple[str, ...] = (),
    require_semantic_match: bool = False,
) -> dict[str, Any]:
    rule_text = _rule_text(rule)
    matched_uncertain: list[dict[str, Any]] = []
    for entry_index, attachment in _artifact_entries(
        evidence,
        "09_attachment_reviews.json",
        "attachment_reviews",
    ):
        semantic_match = attachment.get("semantic_match")
        semantic_status = (
            _as_text(semantic_match.get("status"))
            if isinstance(semantic_match, Mapping)
            else ""
        )
        requirements = attachment.get("requirements")
        if not isinstance(requirements, list):
            continue
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                continue
            requirement_text = _as_text(requirement.get("requirement"))
            if requirement_terms and not _contains_any(
                requirement_text,
                requirement_terms,
            ):
                continue
            anchor = _shared_anchor(rule_text, requirement_text)
            if not anchor:
                continue
            if require_semantic_match and semantic_status != "matched":
                continue
            fact = {
                "artifact": "09_attachment_reviews.json",
                "entry_index": entry_index,
                "requirement": requirement_text,
                "status": _as_text(requirement.get("status")),
                "reason": _as_text(requirement.get("reason")),
                "matched_anchor": anchor,
                "semantic_match_status": semantic_status or None,
            }
            bid_evidence = _merge_bid_evidence(
                _bid_evidence(requirement),
                _bid_evidence(attachment),
            )
            evidence_ref = {
                "artifact": "09_attachment_reviews.json",
                "entry_index": entry_index,
                "requirement": requirement_text,
                "matched_anchor": anchor,
                "bid_evidence": bid_evidence,
            }
            if _explicit_fail_status(requirement.get("status")):
                if semantic_status and semantic_status != "matched":
                    matched_uncertain.append(fact)
                    continue
                if not bid_evidence["block_ids"] and not bid_evidence["image_ids"]:
                    return _finish_review(
                        review,
                        status="evidence_insufficient",
                        reason="附件检查明确记录该要求不满足，但没有可追溯的投标 block/image 证据。",
                        facts_required=["明确缺失的独立证明材料及其投标证据"],
                        confirmed_facts=[fact],
                        evidence=[evidence_ref],
                        related_artifacts=["09_attachment_reviews.json"],
                    )
                return _finish_review(
                    review,
                    status="triggered",
                    reason="附件检查已明确确认与当前否决规则同一材料要求不满足。",
                    facts_required=["该独立证明材料已提供且满足要求"],
                    confirmed_facts=[fact],
                    evidence=[evidence_ref],
                    bid_evidence=bid_evidence,
                    related_artifacts=["09_attachment_reviews.json"],
                )
            if _as_text(requirement.get("status")) == "pass":
                return _finish_review(
                    review,
                    status="not_triggered",
                    reason="附件检查已完成同一材料要求的核验，并明确记录为满足。",
                    facts_required=["该独立证明材料已提供且满足要求"],
                    confirmed_facts=[fact],
                    evidence=[evidence_ref],
                    bid_evidence=bid_evidence,
                    related_artifacts=["09_attachment_reviews.json"],
                )
            if _as_text(requirement.get("status")) in {"uncertain", "pending"}:
                matched_uncertain.append(fact)
    if matched_uncertain:
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason="已找到与规则同一材料要求的附件检查记录，但材料状态仍不确定。",
            facts_required=["该独立证明材料的确定性核验结果"],
            confirmed_facts=matched_uncertain,
            related_artifacts=["09_attachment_reviews.json"],
        )
    return _default_review(
        review,
        ordinary_fail_present=_has_ordinary_fail(evidence),
    )


def _execute_veto_009_subconditions(
    review: dict[str, Any],
    *,
    rule: Mapping[str, Any],
    evidence: Mapping[str, Any],
    bid_file: FileMetadata,
    evaluation_rules: Mapping[str, Any],
    tender_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    conditions = _extract_veto_009_conditions(
        _as_text(rule.get("original_rule"))
    )
    if len(conditions) != 16:
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason="veto_009 的正式原始规则未能完整解析为 16 个子条件，暂不执行任何子条件推断。",
            facts_required=["veto_009 的完整 16 项正式原始规则"],
        )

    subconditions: list[dict[str, Any]] = []
    for index, condition in enumerate(conditions, start=1):
        child = _subcondition_base(rule, index, condition)
        if index == 1:
            child = _finish_review(
                child,
                status="external_data_required",
                reason="投标人须知第 1.8 款涉及信用、违法、处罚及资格限制事实，当前已有产物不能替代对应外部数据核验。",
                facts_required=[
                    "投标人须知第 1.8 款规定的停业、资格暂停、严重违法、信用及裁判文书等事实",
                    "信用中国、裁判文书网或信用信息共享平台核验结果",
                ],
                dependencies={"external_data_required": True},
            )
        elif index == 2:
            child = _finish_review(
                child,
                status="manual_review_required",
                reason="该子条件依赖评标委员会在评标过程中的澄清、说明或补正要求及投标人响应，静态投标文件不能完成判断。",
                facts_required=["评标过程中的澄清、说明或补正要求及响应记录"],
            )
        elif index == 3:
            child = _direct_material_review(
                child,
                rule=child,
                evidence=evidence,
                require_semantic_match=True,
            )
        elif index == 4:
            applicability = _joint_venture_applicability(
                evaluation_rules,
                tender_evidence,
                evidence,
            )
            if applicability["status"] == "not_applicable":
                child = _finish_not_applicable_subcondition(child, applicability)
            elif applicability["status"] == "applicability_uncertain":
                child = _finish_uncertain_applicability_subcondition(child, applicability)
            else:
                child["applicability"] = dict(applicability)
                child = _direct_material_review(
                    child,
                    rule=child,
                    evidence=evidence,
                )
        elif index == 5:
            child = _finish_review(
                child,
                status="evidence_insufficient",
                reason="现有业绩、附件或普通合规检查只能作为资格事实来源，当前尚未形成完整资格后审结论，不能据此认定资格条件不符合。",
                facts_required=["完整的资格后审逐项结论及对应投标证据"],
            )
        elif index == 6:
            child = _finish_review(
                child,
                status="other_bidder_data_required",
                reason="该子条件需要核验当前投标人是否提交多份投标文件或报价，当前数据范围只有单份商务投标文件。",
                facts_required=["全部递交文件和报价的清单及相互比较结果"],
            )
        elif index == 7:
            low_cost = _subcondition_base(
                rule,
                "07_low_cost",
                "投标报价低于成本",
            )
            low_cost = _finish_review(
                low_cost,
                status="manual_review_required",
                reason="低于成本的认定依赖其他投标报价、书面质疑、投标人说明及评标委员会认定，不能从单份投标文件自动触发。",
                facts_required=[
                    "其他投标人的报价或标底比较事实",
                    "书面质疑、投标人说明及评标委员会认定",
                ],
                dependencies={"other_bidder_data_required": True},
            )
            limit = _subcondition_base(
                rule,
                "07_highest_bid_limit",
                "投标报价高于最高投标限价",
            )
            applicability = _applicability_for_rule(
                {
                    "name": "超过最高投标限价",
                    "trigger_condition": "投标报价超过最高投标限价",
                    "original_rule": "投标报价超过最高投标限价",
                },
                evaluation_rules=evaluation_rules,
                tender_evidence=tender_evidence,
            )
            if applicability["status"] == "not_applicable":
                limit = _finish_not_applicable_subcondition(limit, applicability)
            elif applicability["status"] == "applicability_uncertain":
                limit = _finish_uncertain_applicability_subcondition(limit, applicability)
            else:
                limit["applicability"] = dict(applicability)
                limit = _dispatch_rule(
                    limit,
                    rule=limit,
                    evidence=evidence,
                    bid_file=bid_file,
                )
            child["branches"] = [low_cost, limit]
            child = _aggregate_nested_subconditions(
                child,
                child["branches"],
                summary_key="branch_summary",
            )
        elif index == 8:
            if _is_business_only(evidence.get("bid_document"), bid_file.filename):
                child = _finish_review(
                    child,
                    status="file_scope_missing",
                    reason="当前上传文件范围仅包含商务文件，不能完整核验投标文件对全部实质性要求和条件的响应。",
                    facts_required=["技术文件及全部实质性条款的逐项响应事实"],
                )
            else:
                child = _direct_material_review(
                    child,
                    rule=child,
                    evidence=evidence,
                    requirement_terms=("实质性", "★"),
                    require_semantic_match=True,
                )
        elif index == 9:
            collusion = _subcondition_base(
                rule,
                "09_collusion",
                "串通投标",
            )
            collusion = _finish_review(
                collusion,
                status="other_bidder_data_required",
                reason="串通投标需要多个投标文件之间的异常一致或关联证据，单份投标文件不能直接认定。",
                facts_required=["多个投标人的文件、报价及异常关联比较事实"],
            )
            fraud = _subcondition_base(
                rule,
                "09_fraud",
                "弄虚作假",
            )
            fraud = _finish_review(
                fraud,
                status="evidence_insufficient",
                reason="普通材料不一致或业绩金额差异不等同于弄虚作假，当前没有直接、可追溯的虚假材料或伪造事实。",
                facts_required=["直接、可追溯的虚假材料或伪造事实"],
            )
            bribery = _subcondition_base(
                rule,
                "09_bribery",
                "行贿等违法行为",
            )
            bribery = _finish_review(
                bribery,
                status="external_data_required",
                reason="行贿等违法行为通常需要外部案件、处罚或评标认定事实，当前已有产物不能完成核验。",
                facts_required=["对应外部案件、处罚或评标认定记录"],
            )
            child["branches"] = [collusion, fraud, bribery]
            child = _aggregate_nested_subconditions(
                child,
                child["branches"],
                summary_key="branch_summary",
            )
        elif index == 10:
            child = _finish_review(
                child,
                status="evidence_insufficient",
                reason="当前静态投标文件没有足以直接证明以他人名义投标的身份、授权或实际控制事实。",
                facts_required=["投标主体、授权关系及实际投标人身份的直接证据"],
            )
        elif index == 11:
            applicability = _applicability_for_rule(
                {
                    "name": "未递交投标保证金",
                    "trigger_condition": "未递交投标保证金或者投标保证金有瑕疵",
                    "original_rule": "未递交投标保证金或者投标保证金有瑕疵",
                },
                evaluation_rules=evaluation_rules,
                tender_evidence=tender_evidence,
            )
            if applicability["status"] == "not_applicable":
                child = _finish_not_applicable_subcondition(child, applicability)
            elif applicability["status"] == "applicability_uncertain":
                child = _finish_uncertain_applicability_subcondition(child, applicability)
            else:
                child["applicability"] = dict(applicability)
                child = _direct_material_review(
                    child,
                    rule=child,
                    evidence=evidence,
                )
        elif index == 12:
            child = _finish_review(
                child,
                status="evidence_insufficient",
                reason="当前没有可复用的投标完成期限与招标文件要求的完整、可比对事实，不能默认满足或超期。",
                facts_required=["招标文件完成期限与投标文件承诺期限的可比对事实"],
            )
        elif index == 13:
            if _is_business_only(evidence.get("bid_document"), bid_file.filename):
                child = _finish_review(
                    child,
                    status="file_scope_missing",
                    reason="当前上传文件范围仅包含商务文件，不包含完整技术标，不能核验技术规格和技术标准响应。",
                    facts_required=["完整技术文件及技术规格、技术标准逐项响应事实"],
                )
            else:
                child = _finish_review(
                    child,
                    status="evidence_insufficient",
                    reason="当前尚未完成技术规格和技术标准的确定性逐项核验，不能直接触发。",
                    facts_required=["技术规格、技术标准逐项响应事实"],
                )
        elif index == 14:
            applicability = _goods_packaging_applicability(
                evaluation_rules,
                tender_evidence,
            )
            if applicability["status"] == "not_applicable":
                child = _finish_not_applicable_subcondition(child, applicability)
            elif applicability["status"] == "applicability_uncertain":
                child = _finish_uncertain_applicability_subcondition(child, applicability)
            else:
                child["applicability"] = dict(applicability)
                child = _direct_material_review(
                    child,
                    rule=child,
                    evidence=evidence,
                )
        elif index == 15:
            child = _finish_review(
                child,
                status="evidence_insufficient",
                reason="当前没有识别到招标人明确不能接受且已由投标文件实际附加的具体条件，不能据此触发。",
                facts_required=["投标文件附加条件及招标人可接受性判断事实"],
            )
        elif index == 16:
            child = _direct_material_review(
                child,
                rule=child,
                evidence=evidence,
                requirement_terms=("实质性", "★"),
                require_semantic_match=True,
            )
        subconditions.append(child)

    review["sub_conditions"] = subconditions
    status_counts = {
        status: sum(
            _as_text(item.get("status")) == status
            for item in subconditions
        )
        for status in sorted(VETO_STATUSES)
    }
    triggered = [item for item in subconditions if item.get("status") == "triggered"]
    blocking = [
        item
        for item in subconditions
        if _as_text(item.get("status")) in _SUBCONDITION_BLOCKING_STATUSES
    ]
    review["subcondition_summary"] = {
        "total": len(subconditions),
        "status_counts": status_counts,
        "triggered_subcondition_indices": [item["index"] for item in triggered],
        "blocking_subcondition_indices": [item["index"] for item in blocking],
        "not_applicable_subcondition_indices": [
            item["index"]
            for item in subconditions
            if item.get("status") == "not_applicable"
        ],
    }
    dependencies = {
        key: any(
            isinstance(item.get("dependencies"), Mapping)
            and bool(item["dependencies"].get(key))
            for item in subconditions
        )
        for key in _SUBCONDITION_DEPENDENCY_KEYS
    }
    evidence_refs = [
        dict(item)
        for child in subconditions
        for item in child.get("evidence", [])
        if isinstance(item, Mapping)
    ]
    related_artifacts = list(
        dict.fromkeys(
            _as_text(item.get("artifact"))
            for child in subconditions
            for item in child.get("evidence", [])
            if isinstance(item, Mapping) and _as_text(item.get("artifact"))
        )
    )
    bid_evidence = _merge_bid_evidence(
        *(
            child.get("bid_evidence", {})
            for child in subconditions
            if isinstance(child.get("bid_evidence"), Mapping)
        )
    )
    facts = [
        {
            "sub_condition_id": _as_text(child.get("id")),
            "index": child.get("index"),
            "status": _as_text(child.get("status")),
            "triggered": bool(child.get("triggered")),
            "confirmed_facts": list(child.get("confirmed_facts", [])),
        }
        for child in subconditions
    ]
    facts_required = list(
        dict.fromkeys(
            _as_text(item)
            for child in subconditions
            for item in child.get("facts_required", [])
            if _as_text(item)
        )
    )
    if triggered:
        result = _finish_review(
            review,
            status="triggered",
            reason="veto_009 的内部子条件中至少有一项已由可追溯事实明确触发。",
            facts_required=facts_required,
            confirmed_facts=facts,
            evidence=evidence_refs,
            bid_evidence=bid_evidence,
            related_artifacts=related_artifacts,
            dependencies=dependencies,
        )
        result["triggered_by"] = [
            _as_text(item.get("id"))
            for item in triggered
            if _as_text(item.get("id"))
        ]
        return result
    if blocking:
        blocking_statuses = {
            _as_text(item.get("status"))
            for item in blocking
        }
        status = (
            next(iter(blocking_statuses))
            if len(blocking_statuses) == 1
            else "evidence_insufficient"
        )
        return _finish_review(
            review,
            status=status,
            reason=(
                "veto_009 的 16 个子条件中仍有未完成判断的条件；"
                f"当前未决状态包括：{'、'.join(sorted(blocking_statuses))}。"
                "因此不能汇总为 not_triggered；not_applicable 子条件不参与触发判断。"
            ),
            facts_required=facts_required,
            confirmed_facts=facts,
            evidence=evidence_refs,
            bid_evidence=bid_evidence,
            related_artifacts=related_artifacts,
            dependencies=dependencies,
        )
    return _finish_review(
        review,
        status="not_triggered",
        reason="16 个子条件中所有适用条件均已明确确认未触发，不适用条件不参与触发判断。",
        facts_required=facts_required,
        confirmed_facts=facts,
        evidence=evidence_refs,
        bid_evidence=bid_evidence,
        related_artifacts=related_artifacts,
        dependencies=dependencies,
    )


def _dispatch_rule(
    review: dict[str, Any],
    *,
    rule: Mapping[str, Any],
    evidence: Mapping[str, Any],
    bid_file: FileMetadata,
) -> dict[str, Any]:
    text = _rule_text(rule)
    document = evidence.get("bid_document")
    if "★" in text:
        if _is_business_only(document, bid_file.filename):
            return _finish_review(
                review,
                status="file_scope_missing",
                reason="当前上传文件范围仅包含商务文件，不包含该 ★ 条款所需的技术/响应文件。",
                facts_required=["★ 条款逐项响应事实"],
            )
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason="当前没有完成该 ★ 条款的逐项响应检查，不能默认全部满足或直接否决。",
            facts_required=["★ 条款逐项响应事实"],
        )
    if _contains_any(text, ("逾期送达", "未送达指定地点", "密封", "不予接收")):
        return _finish_review(
            review,
            status="manual_review_required",
            reason="该规则依赖投标递交状态、评标过程中的说明、算术修正接受情况或评标委员会认定，静态投标文件不能完成判断。",
            facts_required=["后续评标过程事实和评标委员会认定"],
        )
    if _is_preliminary_aggregate_text(text):
        reason = "该规则是正式评审结果的汇总规则，当前没有可追溯的正式子评审项触发事实。"
        if _has_ordinary_fail(evidence):
            reason += "现有产物中的普通 fail 不属于已确认的正式子评审项。"
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason=reason,
            facts_required=["全部正式评审情形的逐项结果"],
        )
    if _contains_any(text, _MANUAL_TERMS):
        return _finish_review(
            review,
            status="manual_review_required",
            reason="该规则依赖评标过程中的说明、算术修正接受情况或评标委员会认定，静态投标文件不能完成判断。",
            facts_required=["后续评标过程事实和评标委员会认定"],
        )
    if "弄虚作假" in text or "虚假" in text or "伪造" in text:
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason="现有产物最多能证明材料一致性或内容差异，不能仅凭普通不一致认定弄虚作假。",
            facts_required=["直接、可追溯的虚假材料或伪造事实"],
        )
    if _contains_any(text, _OTHER_BIDDER_TERMS):
        return _finish_review(
            review,
            status="other_bidder_data_required",
            reason="该规则需要其他投标人文件或报价进行横向比较，当前只有单份投标文件。",
            facts_required=["其他投标人的投标文件或报价比较事实"],
        )
    if _contains_any(text, _EXTERNAL_TERMS):
        return _finish_review(
            review,
            status="external_data_required",
            reason="该规则依赖当前系统未接入的外部信用、处罚或供应商管理数据。",
            facts_required=["对应外部系统记录"],
        )
    if "非实质性" in text and _parse_threshold(text) is not None:
        return _threshold_review(
            review,
            evidence=evidence,
            threshold=_parse_threshold(text) or 0,
        )
    if "最高投标限价" in text or "最高响应限价" in text:
        if _is_business_only(document, bid_file.filename) or not _contains_any(
            _section_titles(document),
            ("报价文件", "投标一览表"),
        ):
            return _finish_review(
                review,
                status="file_scope_missing",
                reason="当前文件范围未包含可核验投标报价或报价一览表，不能判断是否超过最高限价。",
                facts_required=["投标报价和最高限价比较事实"],
            )
        return _finish_review(
            review,
            status="evidence_insufficient",
            reason="当前没有可复用的完整报价与最高限价比较事实。",
            facts_required=["投标报价和最高限价比较事实"],
        )
    if _contains_any(text, ("未提供", "缺少", "未按要求", "不具备")):
        return _direct_material_review(
            review,
            rule=rule,
            evidence=evidence,
        )
    return _default_review(
        review,
        ordinary_fail_present=_has_ordinary_fail(evidence),
    )


def _enforce_audit_chain(review: dict[str, Any]) -> dict[str, Any]:
    if review.get("status") != "triggered":
        review["triggered"] = False
        return review
    source = review.get("tender_rule_source")
    bid_evidence = review.get("bid_evidence")
    if (
        not review.get("confirmed_facts")
        or not review.get("related_artifacts")
        or not isinstance(source, Mapping)
        or not source.get("block_ids")
        or not isinstance(bid_evidence, Mapping)
        or not bid_evidence.get("block_ids") and not bid_evidence.get("image_ids")
    ):
        review.update(
            {
                "status": "evidence_insufficient",
                "triggered": False,
                "reason": (
                    f"{_as_text(review.get('reason'))}；"
                    "触发所需的完整证据链（规则来源、事实、上游产物和投标 block/image）不完整。"
                ),
            }
        )
        return review
    review["triggered"] = True
    return review


def _is_preliminary_aggregate(rule: Mapping[str, Any]) -> bool:
    return _is_preliminary_aggregate_text(_rule_text(rule))


def _semantic_relation_anchor(
    child_rule: Mapping[str, Any],
    parent_rule: Mapping[str, Any],
) -> str | None:
    child_text = _rule_text(child_rule)
    parent_text = _rule_text(parent_rule)
    if _contains_any(child_text, ("逾期送达", "未送达指定地点", "密封", "不予接收")):
        return None
    if "最高投标限价" in child_text and (
        "最高投标限价" in parent_text
        and _contains_any(parent_text, ("高于", "超过", "不得超过"))
    ):
        return "最高投标限价"
    if _contains_any(child_text, ("投标保证金", "投标担保")) and (
        _contains_any(parent_text, ("投标保证金", "投标担保"))
        and _contains_any(parent_text, ("提供", "瑕疵", "没有按照"))
    ):
        return "投标保证金/投标担保"
    if "低于成本" in child_text and "低于成本" in parent_text:
        return "低于成本"
    if "资格" in child_text and _contains_any(
        parent_text,
        ("资格条件", "资格审查", "不符合国家或者招标文件规定的资格"),
    ):
        return "资格条件"
    child_source = child_rule.get("source")
    child_section = (
        _as_text(child_source.get("section"))
        if isinstance(child_source, Mapping)
        else ""
    )
    if (
        "资格审查" in child_section
        and _contains_any(parent_text, ("初步评审", "评审标准"))
        and "有一项" in parent_text
    ):
        return "初步评审资格子项"
    return None


def _link_preliminary_relations(
    reviews: list[dict[str, Any]],
    rules: list[Mapping[str, Any]],
) -> None:
    parents = [
        index
        for index, rule in enumerate(rules)
        if _is_preliminary_aggregate(rule)
    ]
    if not parents:
        return
    for parent_index in parents:
        parent = reviews[parent_index]
        for child_index, child_rule in enumerate(rules):
            if child_index == parent_index or _is_preliminary_aggregate(child_rule):
                continue
            child = reviews[child_index]
            relation = _semantic_relation_anchor(child_rule, rules[parent_index])
            if relation is None:
                continue
            parent_id = _as_text(rules[parent_index].get("id"))
            if parent_id and parent_id not in child["parent_rule_ids"]:
                child["parent_rule_ids"].append(parent_id)
                child["rule_relations"].append(
                    {"parent_rule_id": parent_id, "relation": relation}
                )
            if child.get("status") != "triggered":
                continue
            child_id = _as_text(child_rule.get("id"))
            if child_id and child_id not in parent["triggered_by"]:
                parent["triggered_by"].append(child_id)
            parent["status"] = "triggered"
            parent["triggered"] = True
            parent["reason"] = (
                "具体正式评审子规则已触发，当前汇总规则因此触发；"
                "该结果不构成第二个独立否决原因。"
            )
            parent["facts_required"] = ["具体正式初步评审项的触发事实"]
            parent["confirmed_facts"] = [
                {
                    "derived_from_rule_id": child_id,
                    "relation": "preliminary_review_aggregate",
                    "relation_anchor": relation,
                    "status": "triggered",
                }
            ]
            parent["evidence"] = list(child.get("evidence", []))
            parent["bid_evidence"] = dict(child.get("bid_evidence", {}))
            parent["related_artifacts"] = list(child.get("related_artifacts", []))


def _source_payload(
    evidence: Mapping[str, Any],
    *,
    objective_scores: Mapping[str, Any] | None,
) -> dict[str, Any]:
    artifacts = evidence.get("artifacts", {})
    artifact_paths = evidence.get("artifact_paths", {})
    source: dict[str, Any] = {
        "evaluation_rules_artifact": "11_evaluation_rules.json",
        "bid_filename": evidence.get("bid_filename"),
        "bid_path": evidence.get("bid_path"),
        "bid_sha256": evidence.get("bid_sha256"),
        "bid_document_artifact": evidence.get("bid_document_artifact"),
        "bid_document_source": evidence.get("bid_document_source"),
        "bid_document_hash_verified": bool(
            evidence.get("bid_document_hash_verified")
        ),
        "reused_artifacts": sorted(
            name
            for name in artifacts
            if name in _REUSABLE_ARTIFACT_NAMES
        ),
        "missing_artifacts": sorted(
            str(name) for name in evidence.get("missing_artifacts", [])
        ),
        "artifact_paths": {
            str(name): str(path)
            for name, path in artifact_paths.items()
            if name in _REUSABLE_ARTIFACT_NAMES
        },
    }
    if isinstance(objective_scores, Mapping):
        source["objective_scores_artifact"] = "objective_scores.json"
    return source


def _subcondition_stats(
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    subconditions = [
        item
        for review in reviews
        for item in review.get("sub_conditions", [])
        if isinstance(item, Mapping)
    ]
    branches = [
        branch
        for item in subconditions
        for branch in item.get("branches", [])
        if isinstance(branch, Mapping)
    ]
    dependency_counts = {
        key: sum(
            isinstance(item.get("dependencies"), Mapping)
            and bool(item["dependencies"].get(key))
            for item in subconditions
        )
        for key in _SUBCONDITION_DEPENDENCY_KEYS
    }
    return {
        "subcondition_count": len(subconditions),
        "subcondition_status_counts": {
            status: sum(_as_text(item.get("status")) == status for item in subconditions)
            for status in sorted(VETO_STATUSES)
        },
        "subcondition_branch_count": len(branches),
        "subcondition_branch_status_counts": {
            status: sum(_as_text(item.get("status")) == status for item in branches)
            for status in sorted(VETO_STATUSES)
        },
        "dependency_counts": dependency_counts,
    }


def _stats(
    reviews: list[dict[str, Any]],
    *,
    evidence: Mapping[str, Any],
    started_at: float,
) -> dict[str, Any]:
    status_counts = {status: 0 for status in sorted(VETO_STATUSES)}
    applicability_counts = {
        status: 0 for status in sorted(_APPLICABILITY_STATUSES)
    }
    for review in reviews:
        status = _as_text(review.get("status"))
        if status in VETO_STATUSES:
            status_counts[status] += 1
        applicability = review.get("applicability")
        applicability_status = (
            _as_text(applicability.get("status"))
            if isinstance(applicability, Mapping)
            else ""
        )
        if applicability_status in _APPLICABILITY_STATUSES:
            applicability_counts[applicability_status] += 1
    nested_stats = _subcondition_stats(reviews)
    return {
        "formal_rule_count": len(reviews),
        "triggered_count": status_counts["triggered"],
        "status_counts": status_counts,
        "applicability_counts": applicability_counts,
        **nested_stats,
        "external_data_required": nested_stats["dependency_counts"][
            "external_data_required"
        ],
        "other_bidder_data_required": nested_stats["dependency_counts"][
            "other_bidder_data_required"
        ],
        "manual_review_required": nested_stats["dependency_counts"][
            "manual_review_required"
        ],
        "llm_total_calls": 0,
        "ocr_reused": bool(evidence.get("bid_document_hash_verified")),
        "duplicate_parse": False,
        "bid_parse_reused": bool(evidence.get("bid_document_hash_verified")),
        "missing_artifact_count": len(evidence.get("missing_artifacts", [])),
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
    }


def run_veto_rule_execution(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
    objective_scores: Mapping[str, Any] | None = None,
    tender_evidence: Mapping[str, Any] | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Execute formal veto rules from reusable structured evidence only."""

    started_at = time.perf_counter()
    evidence = load_reusable_bid_evidence(
        bid_file,
        bid_document=bid_document,
        artifact_dir=artifact_dir,
        existing_artifacts=existing_artifacts,
    )
    raw_rules = evaluation_rules.get("veto_rules", [])
    rules = [rule for rule in raw_rules if isinstance(rule, Mapping)] \
        if isinstance(raw_rules, list) else []
    reviews: list[dict[str, Any]] = []
    for rule in rules:
        review = _review_base(rule)
        applicability = _applicability_for_rule(
            rule,
            evaluation_rules=evaluation_rules,
            tender_evidence=tender_evidence,
        )
        review["applicability"] = applicability
        if applicability["status"] == "not_applicable":
            review = _finish_review(
                review,
                status="not_applicable",
                reason=applicability["reason"],
                facts_required=["招标文件项目专用条件"],
                confirmed_facts=applicability["facts"],
                evidence=applicability["evidence"],
                related_artifacts=[
                    str(item.get("artifact"))
                    for item in applicability["evidence"]
                    if isinstance(item, Mapping) and item.get("artifact")
                ],
            )
        elif applicability["status"] == "applicability_uncertain":
            review = _finish_review(
                review,
                status="evidence_insufficient",
                reason=applicability["reason"],
                facts_required=["招标文件项目专用条件"],
            )
        elif _as_text(rule.get("id")) == "veto_009":
            review = _execute_veto_009_subconditions(
                review,
                rule=rule,
                evidence=evidence,
                bid_file=bid_file,
                evaluation_rules=evaluation_rules,
                tender_evidence=tender_evidence,
            )
        else:
            review = _dispatch_rule(
                review,
                rule=rule,
                evidence=evidence,
                bid_file=bid_file,
            )
        reviews.append(review)
    _link_preliminary_relations(reviews, rules)
    reviews = [_enforce_audit_chain(review) for review in reviews]
    result: dict[str, Any] = {
        "schema_version": "veto-rule-review-v1",
        "source": _source_payload(
            evidence,
            objective_scores=objective_scores,
        ),
        "veto_rule_reviews": reviews,
        "stats": _stats(
            reviews,
            evidence=evidence,
            started_at=started_at,
        ),
    }
    if recorder is not None:
        recorder.write_json(VETO_RULE_REVIEW_ARTIFACT, result)
        recorder.event(
            "veto.rule.execution.end",
            status="complete",
            formal_rule_count=result["stats"]["formal_rule_count"],
            triggered_count=result["stats"]["triggered_count"],
            llm_total_calls=0,
            elapsed_ms=result["stats"]["elapsed_ms"],
        )
    return result
