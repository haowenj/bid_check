from __future__ import annotations

import re
import time
from collections.abc import Mapping
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


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _copy_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"section": "", "block_ids": [], "source_text": ""}
    block_ids = value.get("block_ids", [])
    return {
        "section": _as_text(value.get("section")),
        "block_ids": [str(item) for item in block_ids]
        if isinstance(block_ids, list)
        else [],
        "source_text": _as_text(value.get("source_text")),
    }


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
    if _contains_any(text, _PRELIMINARY_TERMS) and _contains_any(
        text,
        ("有一项", "任一项", "不通过", "不符合"),
    ):
        return True
    return _contains_any(text, ("任一情形", "存在以下任一", "下列情形之一"))


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
        "additional_consequence": rule.get("additional_consequence"),
        "evidence_requirements": (
            [str(item) for item in evidence_requirements]
            if isinstance(evidence_requirements, list)
            else []
        ),
        "tender_rule_source": source,
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
            anchor = _shared_anchor(rule_text, requirement_text)
            if not anchor:
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
            source = _copy_source(child_rule.get("source"))
            child_text = _rule_text(child_rule)
            if not (
                _contains_any(source.get("section", ""), _PRELIMINARY_TERMS)
                or _contains_any(child_text, ("资格", "形式评审", "响应性"))
            ):
                continue
            parent_id = _as_text(rules[parent_index].get("id"))
            if parent_id and parent_id not in child["parent_rule_ids"]:
                child["parent_rule_ids"].append(parent_id)
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


def _stats(
    reviews: list[dict[str, Any]],
    *,
    evidence: Mapping[str, Any],
    started_at: float,
) -> dict[str, Any]:
    status_counts = {status: 0 for status in sorted(VETO_STATUSES)}
    for review in reviews:
        status = _as_text(review.get("status"))
        if status in VETO_STATUSES:
            status_counts[status] += 1
    return {
        "formal_rule_count": len(reviews),
        "triggered_count": status_counts["triggered"],
        "status_counts": status_counts,
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
    reviews = [
        _dispatch_rule(
            _review_base(rule),
            rule=rule,
            evidence=evidence,
            bid_file=bid_file,
        )
        for rule in rules
    ]
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
