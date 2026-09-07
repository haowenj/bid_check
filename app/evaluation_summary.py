from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models import BidCheckTask


STATUS_LABELS = {
    "pass": "通过",
    "fail": "未通过",
    "uncertain": "待确认",
    "present": "已提供",
    "absent": "未发现",
    "confirmed_present": "已确认存在",
    "confirmed_absent": "已确认不存在",
    "not_supported": "暂不支持",
    "matched": "已匹配",
    "mismatched": "不匹配",
    "unmatched": "未匹配",
    "ambiguous": "待确认",
    "failed": "检查失败",
    "auto_scored": "已确定",
    "ai_scored": "已评分",
    "triggered": "已触发否决",
    "not_triggered": "明确未触发",
    "not_applicable": "不适用",
    "file_scope_missing": "文件范围不足",
    "evidence_insufficient": "证据不足",
    "external_data_required": "尚需外部数据",
    "other_bidder_data_required": "尚需其他投标人数据",
    "manual_review_required": "尚需人工复核",
    "unsupported": "暂不支持自动评分",
    "llm_error": "AI辅助评分异常",
}

STATUS_TONES = {
    "auto_scored": "positive",
    "ai_scored": "positive",
    "triggered": "danger",
    "not_triggered": "positive",
    "not_applicable": "neutral",
    "file_scope_missing": "warning",
    "evidence_insufficient": "warning",
    "external_data_required": "warning",
    "other_bidder_data_required": "warning",
    "manual_review_required": "warning",
    "unsupported": "neutral",
    "llm_error": "warning",
}

DEPENDENCY_LABELS = {
    "manual_review_required": "尚需人工复核",
    "external_data_required": "尚需外部数据",
    "other_bidder_data_required": "尚需其他投标人数据",
}


def _read_artifact(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _artifact_or_fallback(
    artifact_dir: Path,
    filename: str,
    fallback: dict[str, Any],
    fallback_key: str,
) -> dict[str, Any] | None:
    artifact = _read_artifact(artifact_dir / filename)
    if artifact is not None:
        return artifact
    value = fallback.get(fallback_key)
    return value if isinstance(value, dict) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _score_label(value: Any) -> str:
    if not _is_number(value):
        return "暂无法确定"
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def _status_view(status: Any) -> tuple[str, str]:
    normalized = status if isinstance(status, str) else "unknown"
    return (
        STATUS_LABELS.get(normalized, "待确认"),
        STATUS_TONES.get(normalized, "neutral"),
    )


def _status_code(status: Any) -> str:
    return status if isinstance(status, str) else "unknown"


def _status_display(status: Any) -> str:
    if not isinstance(status, str):
        return ""
    return STATUS_LABELS.get(status, status)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


_EVIDENCE_ARTIFACT_LABELS = {
    "08_template_text_reviews.json": "模板检查结果",
    "09_attachment_reviews.json": "附件检查结果",
    "09_file_requirement_reviews.json": "文件要求检查结果",
    "10_file_requirement_reviews.json": "文件要求检查结果",
    "10_performance_reviews.json": "业绩合同检查结果",
    "structured_document.json": "结构化投标内容",
}

_FACT_LABELS = {
    "performance_summary": "业绩核验结论",
    "matched_terms": "匹配内容",
    "technical_response_present": "技术响应",
    "deviation_count": "偏离项数量",
    "all_required_conditions_confirmed": "必备条件核验",
    "single_certificate_not_sufficient": "单份证书是否足够",
    "verified_member_count": "已核验成员数",
    "roster_count_not_used": "是否仅使用名单数量",
    "valid_extra_case_count": "有效评分业绩数",
    "uncertain_extra_case_count": "待确认评分业绩数",
    "invalid_extra_case_count": "无效评分业绩数",
    "excluded_qualification_case_numbers": "排除的资格业绩案例",
    "excluded_qualification_amount": "排除的资格业绩金额",
    "announcement_date": "招标公告日期",
    "announcement_date_source": "公告日期依据",
    "other_bidder_prices_available": "其他投标人报价",
    "benchmark_price_available": "评标基准价",
    "valid_bidder_count": "有效投标人数",
    "external_data_available": "外部数据",
}

_CASE_FIELD_LABELS = {
    "case_number": "案例编号",
    "project_name": "项目名称",
    "role_label": "业绩类型",
    "role": "业绩角色",
    "overall_status": "检查状态",
    "status": "状态",
    "final_user": "最终用户",
    "counterparty": "合同相对方",
    "contract_amount": "合同金额",
    "implementation_time": "服务期限",
    "service_content": "服务内容",
    "announcement_date": "招标公告日期",
    "included_in_scoring": "是否计入评分",
    "current_rule_status": "评分判定",
    "reason": "判断说明",
}

_TABLE_FIELD_LABELS = {
    "项目名称": "项目名称",
    "投标产品": "投标产品",
    "最终用户": "最终用户",
    "最终用户联系人及联系方式": "最终用户联系人",
    "供货数量（XX单位）": "服务期限",
    "销售金额（万元）": "表中金额（万元）",
    "证明文件所在页码": "证明文件页码",
    "备注": "备注",
}

_CHECK_LABELS = {
    "order_alignment": "业绩材料顺序",
    "service_content": "合同服务内容",
    "implementation_time": "实施时间",
    "contract_amount": "合同金额",
    "table_project_name_consistency": "项目名称一致性",
    "table_counterparty_consistency": "最终用户一致性",
    "table_amount_consistency": "金额一致性",
    "table_time_consistency": "服务期限一致性",
    "signature_page": "合同签页",
    "signature_date": "合同签署日期",
}

_SCORING_CONDITION_LABELS = {
    "role": "业绩类型",
    "contract_signing_date": "合同签署日期",
    "same_type_technical_service": "同类型技术服务",
    "signature_page": "合同签页",
    "proof_material": "证明材料",
}


def _display_value(value: Any) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, dict):
        for key in ("value", "label", "name", "description", "text"):
            candidate = _display_value(value.get(key))
            if candidate:
                return candidate
        return "、".join(
            f"{key}：{display}"
            for key, raw in value.items()
            if (display := _display_value(raw))
        )
    if isinstance(value, list):
        values = [_display_value(item) for item in value]
        return "；".join(item for item in values if item)
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _first_fact_value(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            result = _first_fact_value(item)
            if result:
                return result
        return ""
    return _display_value(value)


def _business_fields(raw: dict[str, Any]) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, value: Any) -> None:
        display = _display_value(value)
        if not display or label in seen:
            return
        seen.add(label)
        fields.append({"label": label, "value": display})

    for key, label in _CASE_FIELD_LABELS.items():
        if key == "case_number" or (key == "role" and "role_label" in raw):
            continue
        if key in raw:
            value = (
                _status_display(raw.get(key))
                if key in {"overall_status", "status", "current_rule_status"}
                else raw.get(key)
            )
            add(label, value)

    table_row = raw.get("table_row")
    if isinstance(table_row, dict):
        for key, value in table_row.items():
            add(_TABLE_FIELD_LABELS.get(key, key), value)

    nested_facts = (
        ("service_content_facts", "服务内容"),
        ("contract_amount_facts", "合同金额"),
        ("implementation_time_facts", "服务期限"),
        ("parties_facts", "合同主体"),
    )
    for key, label in nested_facts:
        if key in raw:
            add(label, _first_fact_value(raw.get(key)))

    return fields


def _artifact_label(artifact: Any) -> str:
    filename = Path(str(artifact)).name if artifact else ""
    return _EVIDENCE_ARTIFACT_LABELS.get(filename, "评分证据")


def _evidence_block_ids(raw: dict[str, Any]) -> list[str]:
    block_ids = raw.get("block_ids")
    if not block_ids and raw.get("block_id"):
        block_ids = [raw["block_id"]]
    if not block_ids and raw.get("source_blocks"):
        block_ids = raw["source_blocks"]
    if not isinstance(block_ids, list):
        return []
    return [str(item) for item in block_ids if item not in (None, "")]


def _check_detail_views(raw: dict[str, Any]) -> list[dict[str, str]]:
    checks = raw.get("checks")
    if not isinstance(checks, dict):
        return []

    details: list[dict[str, str]] = []
    for key, check in checks.items():
        if not isinstance(check, dict):
            continue
        if check.get("status") == "pass":
            continue
        status_label, status_tone = _status_view(check.get("status"))
        image_ids = _string_list(check.get("evidence_image_ids"))
        block_ids = _string_list(check.get("block_ids"))
        evidence = check.get("evidence")
        if isinstance(evidence, list):
            for entry in evidence:
                if not isinstance(entry, dict):
                    continue
                for image_id in _string_list(entry.get("image_ids")):
                    if image_id not in image_ids:
                        image_ids.append(image_id)
                if entry.get("image_id") not in (None, ""):
                    image_id = str(entry["image_id"])
                    if image_id not in image_ids:
                        image_ids.append(image_id)
                for block_id in _evidence_block_ids(entry):
                    if block_id not in block_ids:
                        block_ids.append(block_id)
        meta_parts: list[str] = []
        if image_ids:
            meta_parts.append(f"证据图片：{'、'.join(image_ids)}")
        if block_ids:
            meta_parts.append(f"block：{'、'.join(block_ids)}")
        details.append(
            {
                "key": str(key),
                "label": _CHECK_LABELS.get(
                    str(key), str(check.get("requirement") or key)
                ),
                "status_label": status_label,
                "status_tone": status_tone,
                "reason": str(check.get("reason") or "未记录该项检查原因。"),
                "meta": "；".join(meta_parts),
            }
        )
    return details


def _subjective_evidence_reason(raw: dict[str, Any]) -> str:
    """Return a business-readable finding instead of the bid's long quote."""
    for key in ("reason", "issue_reason", "finding"):
        reason = str(raw.get(key) or "").strip()
        if reason:
            return reason

    requirement = str(raw.get("requirement") or "").strip()
    if "应在填写实际值后清理" in requirement:
        return requirement.replace(
            "应在填写实际值后清理",
            "仍残留在投标文件中",
        )
    if requirement:
        return f"检查发现：{requirement}"
    return "已发现与该扣分事实相关的检查问题，具体来源见证据定位。"


def _evidence_entry_view(
    raw: dict[str, Any],
    *,
    source_artifact: str = "",
    display_reason: bool = False,
) -> dict[str, Any]:
    artifact = str(raw.get("artifact") or source_artifact or "")
    is_performance_case = (
        bool(raw.get("case_number") or raw.get("project_name"))
        and (
            Path(artifact).name == "10_performance_reviews.json"
            or "table_row" in raw
            or "source_blocks" in raw
        )
    )
    title = "业绩合同检查结果" if is_performance_case else _artifact_label(artifact)
    subtitle = ""
    if is_performance_case and raw.get("case_number") not in (None, ""):
        subtitle = f"案例 {raw['case_number']}"
    text = (
        _subjective_evidence_reason(raw)
        if display_reason
        else next(
            (
                str(raw[key])
                for key in ("quote", "source_text", "text", "requirement", "relation")
                if raw.get(key) not in (None, "")
            ),
            "",
        )
    )
    if not text and raw.get("status") not in (None, ""):
        text = f"检查状态：{_status_display(raw['status'])}"
    meta_parts: list[str] = []
    block_ids = _evidence_block_ids(raw)
    if block_ids:
        meta_parts.append(f"block：{'、'.join(block_ids)}")
    image_ids = raw.get("image_ids") or raw.get("source_images")
    if isinstance(image_ids, list) and image_ids:
        meta_parts.append(f"证据图片：{'、'.join(str(item) for item in image_ids)}")
    return {
        "kind": "performance_case" if is_performance_case else "evidence",
        "title": title,
        "subtitle": subtitle,
        "case_number": str(raw.get("case_number"))
        if raw.get("case_number") not in (None, "")
        else "",
        "text": text if not is_performance_case else "",
        "meta": "；".join(meta_parts),
        "fields": _business_fields(raw),
        "check_details": (check_details := _check_detail_views(raw)),
        "check_issue_count": len(check_details),
        "source_artifact": artifact,
        "raw_json": json.dumps(raw, ensure_ascii=False, indent=2),
    }


def _evidence_view(
    value: Any,
    *,
    source_artifact: str = "",
    display_reason: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    evidence: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            if raw not in (None, ""):
                evidence.append(
                    {
                        "title": "关键证据",
                        "kind": "evidence",
                        "subtitle": "",
                        "text": str(raw),
                        "meta": "",
                        "fields": [],
                        "check_details": [],
                        "check_issue_count": 0,
                        "source_artifact": source_artifact,
                        "raw_json": "",
                    }
                )
            continue
        evidence.append(
            _evidence_entry_view(
                raw,
                source_artifact=source_artifact,
                display_reason=display_reason,
            )
        )
    return evidence


def _facts_evidence_view(
    value: Any,
    *,
    source_artifact: str = "",
) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return _evidence_view(value, source_artifact=source_artifact)
    if not isinstance(value, dict):
        return []
    facts: list[dict[str, Any]] = []
    performance_cases = value.get("performance_cases")
    if isinstance(performance_cases, dict):
        performance_cases = [performance_cases]
    if isinstance(performance_cases, list):
        for case in performance_cases:
            if isinstance(case, dict):
                facts.append(
                    _evidence_entry_view(
                        case,
                        source_artifact=source_artifact
                        or "10_performance_reviews.json",
                    )
                )

    summary_fields: list[dict[str, str]] = []
    for key, raw in value.items():
        if key in {"performance_cases", "case_evaluations"} or raw in (None, "", []):
            continue
        label = _FACT_LABELS.get(key, key.replace("_", " "))
        display = (
            _status_display(raw)
            if key in {"performance_summary", "status", "overall_status", "current_rule_status"}
            else _display_value(raw)
        )
        if display:
            summary_fields.append({"label": label, "value": display})
    if summary_fields:
        facts.append(
            {
                "kind": "fact_summary",
                "title": "评分事实汇总",
                "subtitle": "",
                "text": "",
                "meta": "",
                "fields": summary_fields,
                "check_details": [],
                "check_issue_count": 0,
                "source_artifact": source_artifact,
                "raw_json": json.dumps(value, ensure_ascii=False, indent=2),
            }
        )
    return facts


def _issue_summary_view(evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for item in evidence:
        if item.get("kind") != "performance_case":
            continue
        project_name = next(
            (
                field["value"]
                for field in item.get("fields", [])
                if field.get("label") == "项目名称"
            ),
            "",
        )
        case_label = " · ".join(
            label
            for label in (item.get("subtitle", ""), project_name)
            if label
        ) or "业绩合同"
        for detail in item.get("check_details", []):
            issues.append(
                {
                    "label": f"{case_label} · {detail['label']}",
                    "status_label": detail["status_label"],
                    "status_tone": detail["status_tone"],
                    "reason": detail["reason"],
                    "meta": detail["meta"],
                }
            )
    return issues


def _scope_performance_issues(
    evidence: list[dict[str, Any]],
    facts: Any,
    score_item_id: str,
) -> list[dict[str, Any]]:
    if score_item_id not in {"score_item_010", "score_item_011"}:
        return evidence
    if not isinstance(facts, dict) or not isinstance(
        facts.get("case_evaluations"), list
    ):
        return evidence

    evaluations = {
        str(item.get("case_number")): item
        for item in facts["case_evaluations"]
        if isinstance(item, dict) and item.get("case_number") not in (None, "")
    }
    for case_view in evidence:
        if case_view.get("kind") != "performance_case":
            continue
        evaluation = evaluations.get(str(case_view.get("case_number", "")))
        if not isinstance(evaluation, dict):
            continue

        generic_details = case_view.get("check_details", [])
        conditions = evaluation.get("conditions")
        scoped_details: list[dict[str, str]] = []
        if isinstance(conditions, dict):
            for key, condition in conditions.items():
                if not isinstance(condition, dict) or condition.get("status") == "pass":
                    continue
                status_label, status_tone = _status_view(condition.get("status"))
                label = _SCORING_CONDITION_LABELS.get(str(key), str(key))
                reason = str(condition.get("reason") or evaluation.get("reason") or "")
                related_keys = {str(key)}
                if score_item_id == "score_item_011" and key == "proof_material":
                    label = "累计金额"
                    amount = evaluation.get("amount")
                    if isinstance(amount, dict) and amount.get("reason"):
                        reason = str(amount["reason"])
                    related_keys.update({"contract_amount", "table_amount_consistency"})
                related_meta = [
                    detail.get("meta", "")
                    for detail in generic_details
                    if detail.get("key") in related_keys and detail.get("meta")
                ]
                meta = "；".join(dict.fromkeys(related_meta))
                if not meta:
                    amount = evaluation.get("amount")
                    if isinstance(amount, dict) and amount.get("image_id"):
                        meta = f"证据图片：{amount['image_id']}"
                if not meta and evaluation.get("source_blocks"):
                    meta = f"block：{'、'.join(_string_list(evaluation['source_blocks']))}"
                scoped_details.append(
                    {
                        "key": str(key),
                        "label": label,
                        "status_label": status_label,
                        "status_tone": status_tone,
                        "reason": reason or "未记录该项评分条件原因。",
                        "meta": meta,
                    }
                )
        if not scoped_details and evaluation.get("current_rule_status") == "uncertain":
            status_label, status_tone = _status_view("uncertain")
            scoped_details.append(
                {
                    "key": "score_condition",
                    "label": "评分条件",
                    "status_label": status_label,
                    "status_tone": status_tone,
                    "reason": str(evaluation.get("reason") or "当前评分条件无法确认。"),
                    "meta": "",
                }
            )
        case_view["check_details"] = scoped_details
        case_view["check_issue_count"] = len(scoped_details)
    return evidence


def _qualification_performance_view(
    objective_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a separate view for qualification-only performance contracts."""
    cases_by_key: dict[str, dict[str, Any]] = {}
    for item in objective_items:
        facts = item.get("facts")
        if not isinstance(facts, dict):
            continue
        cases = facts.get("performance_cases")
        if isinstance(cases, dict):
            cases = [cases]
        if not isinstance(cases, list):
            continue
        evaluations = facts.get("case_evaluations")
        evaluation_by_case = {
            str(evaluation.get("case_number")): evaluation
            for evaluation in evaluations
            if isinstance(evaluation, dict)
            and evaluation.get("case_number") not in (None, "")
        } if isinstance(evaluations, list) else {}
        for case in cases:
            if not isinstance(case, dict):
                continue
            case_number = str(case.get("case_number") or "").strip()
            project_name = str(case.get("project_name") or "").strip()
            key = case_number or project_name
            if not key:
                continue
            evaluation = evaluation_by_case.get(case_number, {})
            role = str(case.get("role") or evaluation.get("role") or "").strip()
            role_label = str(
                case.get("role_label") or evaluation.get("role_label") or ""
            ).strip()
            current_rule_status = str(
                evaluation.get("current_rule_status") or ""
            ).strip()
            is_qualification = (
                role == "qualification"
                or "资格" in role_label
                or current_rule_status == "excluded_by_role"
            )
            if not is_qualification:
                continue
            previous = cases_by_key.get(key)
            if previous is None or len(case.get("checks", {})) > len(
                previous.get("checks", {})
            ):
                cases_by_key[key] = {
                    **case,
                    "role": role or "qualification",
                    "role_label": role_label or "资格要求业绩",
                }

    views: list[dict[str, Any]] = []
    for case in cases_by_key.values():
        view = _evidence_entry_view(
            case,
            source_artifact="10_performance_reviews.json",
        )
        status = case.get("overall_status") or case.get("status")
        if status in (None, ""):
            status = (
                "fail"
                if any(detail.get("status_label") == "未通过" for detail in view["check_details"])
                else "uncertain"
                if view["check_details"]
                else "pass"
            )
        status_label, status_tone = _status_view(status)
        view.update(
            {
                "title": "资格业绩核验",
                "project_name": case.get("project_name") or "",
                "role": case.get("role") or "qualification",
                "role_label": case.get("role_label") or "资格要求业绩",
                "status_label": status_label,
                "status_tone": status_tone,
            }
        )
        views.append(view)
    return views


def _dependencies_view(status: Any, value: Any) -> list[str]:
    labels: list[str] = []
    if status == "file_scope_missing":
        labels.append("文件范围不足")
    if isinstance(value, dict):
        labels.extend(
            label for key, label in DEPENDENCY_LABELS.items() if value.get(key) is True
        )
    return list(dict.fromkeys(labels))


def _objective_item_view(
    raw: dict[str, Any],
    rule: dict[str, Any],
) -> dict[str, Any]:
    status = raw.get("status")
    status_label, status_tone = _status_view(status)
    score = raw.get("score")
    score_determined = status == "auto_scored" and _is_number(score)
    full_score = raw.get("full_score", rule.get("full_score"))
    evidence = _evidence_view(raw.get("evidence"))
    raw_facts = raw.get("facts")
    has_performance_cases = isinstance(raw_facts, dict) and isinstance(
        raw_facts.get("performance_cases"), (list, dict)
    )
    if has_performance_cases:
        evidence = [
            item for item in evidence if item.get("kind") != "performance_case"
        ]
    related_artifacts = _string_list(raw.get("related_artifacts"))
    facts_source = next(
        (
            artifact
            for artifact in related_artifacts
            if "performance" in artifact or "file_requirement" in artifact
        ),
        "",
    )
    evidence.extend(
        _facts_evidence_view(raw_facts, source_artifact=facts_source)
    )
    evidence.extend(
        {
            "title": _artifact_label(artifact),
            "subtitle": "",
            "text": "已复用关联检查产物。",
            "meta": "",
            "fields": [],
            "check_details": [],
            "source_artifact": artifact,
            "raw_json": "",
        }
        for artifact in related_artifacts
        if not (
            has_performance_cases
            and Path(artifact).name == "10_performance_reviews.json"
        )
    )
    evidence = _scope_performance_issues(evidence, raw_facts, str(raw.get("id") or rule.get("id") or ""))
    issue_summary = _issue_summary_view(evidence)
    return {
        **raw,
        "id": raw.get("id") or rule.get("id"),
        "name": raw.get("name") or rule.get("name") or "未命名评分项",
        "full_score_label": _score_label(full_score),
        "status_label": status_label,
        "status_code": _status_code(status),
        "status_tone": status_tone,
        "score_label": _score_label(score) if score_determined else "暂无法确定",
        "score_determined": score_determined,
        "reason": raw.get("reason") or "未记录无法评分原因。",
        "evidence_view": evidence,
        "issue_summary": issue_summary,
    }


def _subjective_item_view(
    raw: dict[str, Any],
    rule: dict[str, Any],
) -> dict[str, Any]:
    status = raw.get("status")
    status_label, status_tone = _status_view(status)
    recommended_score = raw.get("recommended_score")
    full_score = raw.get("max_score", rule.get("full_score"))
    deduction_checks = raw.get("deduction_checks")
    confirmed_deductions: list[dict[str, Any]] = []
    if isinstance(deduction_checks, list):
        for check in deduction_checks:
            if not isinstance(check, dict):
                continue
            if check.get("confirmed_exists") is True or check.get("status") in {
                "confirmed_present",
                "confirmed",
            }:
                confirmed_deductions.append(
                    {
                        **check,
                        "evidence_view": _evidence_view(
                            check.get("evidence"),
                            display_reason=True,
                        ),
                    }
                )
    evidence = _evidence_view(raw.get("evidence"))
    evidence.extend(_evidence_view(raw.get("matched_bid_content")))
    return {
        **raw,
        "id": raw.get("score_item_id") or rule.get("id"),
        "name": raw.get("rule_name") or rule.get("name") or "未命名评分项",
        "full_score_label": _score_label(full_score),
        "status_label": status_label,
        "status_code": _status_code(status),
        "status_tone": status_tone,
        "recommended_score_label": _score_label(recommended_score),
        "score_determined": _is_number(recommended_score),
        "score_band_label": raw.get("score_band") or "暂无法确定",
        "reason": raw.get("reason") or "未记录评分理由。",
        "evidence_view": evidence,
        "confirmed_deductions": confirmed_deductions,
    }


def _veto_condition_view(raw: dict[str, Any]) -> dict[str, Any]:
    status = raw.get("status")
    status_label, status_tone = _status_view(status)
    evidence = _evidence_view(raw.get("evidence"))
    evidence.extend(_evidence_view(raw.get("bid_evidence")))
    return {
        **raw,
        "status_label": status_label,
        "status_code": _status_code(status),
        "status_tone": status_tone,
        "is_triggered": status == "triggered",
        "dependencies_view": _dependencies_view(status, raw.get("dependencies")),
        "evidence_view": evidence,
    }


def _veto_item_view(raw: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    merged = {**rule, **raw}
    view = _veto_condition_view(merged)
    sub_conditions = raw.get("sub_conditions")
    view["sub_conditions"] = [
        _veto_condition_view(item)
        for item in sub_conditions
        if isinstance(item, dict)
    ] if isinstance(sub_conditions, list) else []
    view["id"] = raw.get("id") or rule.get("id")
    view["name"] = raw.get("name") or rule.get("name") or "未命名否决规则"
    view["reason"] = raw.get("reason") or "未记录判断理由。"
    return view


def load_evaluation_summary(task: BidCheckTask) -> dict[str, Any]:
    """Build a display-only summary from independent evaluation artifacts."""

    artifact_dir = (
        Path(task.tender_file.storage_path).parent / "compliance_extraction"
    )
    fallback = task.result if isinstance(task.result, dict) else {}
    rules = _artifact_or_fallback(
        artifact_dir,
        "11_evaluation_rules.json",
        fallback,
        "evaluation_rules",
    ) or {}
    objective = _artifact_or_fallback(
        artifact_dir,
        "objective_scores.json",
        fallback,
        "objective_scores",
    )
    subjective = _artifact_or_fallback(
        artifact_dir,
        "subjective_scores.json",
        fallback,
        "subjective_scores",
    )
    veto = _artifact_or_fallback(
        artifact_dir,
        "veto_rule_reviews.json",
        fallback,
        "veto_rule_reviews",
    )
    rules_available = isinstance(rules.get("score_items"), list)
    objective_available = (
        objective is not None and isinstance(objective.get("score_items"), list)
    )
    subjective_available = (
        subjective is not None and isinstance(subjective.get("score_items"), list)
    )
    veto_available = (
        veto is not None and isinstance(veto.get("veto_rule_reviews"), list)
    )

    rule_items = rules.get("score_items", [])
    if not isinstance(rule_items, list):
        rule_items = []
    score_rules = {
        item.get("id"): item
        for item in rule_items
        if isinstance(item, dict) and item.get("id")
    }
    rule_veto_items = rules.get("veto_rules", [])
    if not isinstance(rule_veto_items, list):
        rule_veto_items = []
    veto_rules = {
        item.get("id"): item
        for item in rule_veto_items
        if isinstance(item, dict) and item.get("id")
    }

    raw_objective_items = objective.get("score_items", []) if objective else []
    if not isinstance(raw_objective_items, list):
        raw_objective_items = []
    objective_items = [
        _objective_item_view(item, score_rules.get(item.get("id"), {}))
        for item in raw_objective_items
        if isinstance(item, dict)
    ]
    qualification_performance_cases = _qualification_performance_view(
        [item for item in raw_objective_items if isinstance(item, dict)]
    )

    raw_subjective_items = subjective.get("score_items", []) if subjective else []
    if not isinstance(raw_subjective_items, list):
        raw_subjective_items = []
    subjective_items = [
        _subjective_item_view(
            item,
            score_rules.get(item.get("score_item_id"), {}),
        )
        for item in raw_subjective_items
        if isinstance(item, dict)
    ]

    raw_veto_items = veto.get("veto_rule_reviews", []) if veto else []
    if not isinstance(raw_veto_items, list):
        raw_veto_items = []
    veto_items = [
        _veto_item_view(item, veto_rules.get(item.get("id"), {}))
        for item in raw_veto_items
        if isinstance(item, dict)
    ]

    objective_determined = sum(
        1 for item in objective_items if item["score_determined"]
    )
    subjective_scored = sum(
        1
        for item in subjective_items
        if item.get("status") == "ai_scored" and item["score_determined"]
    )
    subjective_file_missing = sum(
        1 for item in subjective_items if item.get("status") == "file_scope_missing"
    )
    subjective_other_pending = (
        len(subjective_items) - subjective_scored - subjective_file_missing
    )
    triggered_count = sum(1 for item in veto_items if item["is_triggered"])

    expected_objective_ids = {
        item.get("id")
        for item in rule_items
        if isinstance(item, dict) and item.get("evaluation_type") == "objective"
    }
    expected_subjective_ids = {
        item.get("id")
        for item in rule_items
        if isinstance(item, dict) and item.get("evaluation_type") == "subjective"
    }
    actual_objective_ids = {item.get("id") for item in objective_items}
    actual_subjective_ids = {item.get("id") for item in subjective_items}
    all_rule_items_supported = bool(rule_items) and all(
        isinstance(item, dict)
        and bool(item.get("id"))
        and item.get("evaluation_type") in {"objective", "subjective"}
        for item in rule_items
    )
    scores_complete = (
        rules_available
        and all_rule_items_supported
        and objective_available
        and subjective_available
        and expected_objective_ids <= actual_objective_ids
        and expected_subjective_ids <= actual_subjective_ids
        and objective_determined == len(objective_items)
        and subjective_scored == len(subjective_items)
    )

    return {
        "has_any_result": any(
            (objective_available, subjective_available, veto_available)
        ),
        "objective_items": objective_items,
        "qualification_performance_cases": qualification_performance_cases,
        "subjective_items": subjective_items,
        "veto_items": veto_items,
        "summary": {
            "objective_total": len(objective_items),
            "objective_available": objective_available,
            "objective_determined": objective_determined,
            "objective_pending": len(objective_items) - objective_determined,
            "subjective_total": len(subjective_items),
            "subjective_available": subjective_available,
            "subjective_scored": subjective_scored,
            "subjective_file_missing": subjective_file_missing,
            "subjective_other_pending": subjective_other_pending,
            "veto_total": len(veto_items),
            "veto_available": veto_available,
            "veto_triggered": triggered_count,
            "scores_complete": scores_complete,
        },
    }
