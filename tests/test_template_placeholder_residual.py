from __future__ import annotations

import copy
from typing import Any

from app.template_placeholder_residual import find_template_placeholder_residuals
from app.template_text_review import run_template_text_review


class PassLLM:
    model = "placeholder-fixture-model"

    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls = 0

    def review_template(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls += 1
        return copy.deepcopy(self.response)


def _template(
    body: str,
    fields: list[str],
    *,
    name: str = "法定代表人/负责人身份证明",
) -> dict[str, Any]:
    return {
        "id": "template-placeholder",
        "name": name,
        "section": "投标文件格式",
        "body": body,
        "fields": fields,
        "source": {
            "section": "投标文件格式",
            "block_ids": ["tender-block-1"],
            "source_text": body,
        },
    }


def _bid_materialized(
    text: str,
    *,
    title: str = "7 法定代表人/负责人身份证明",
    block_id: str = "bid-block-1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    section = {
        "section_id": "bid-section-1",
        "title": title,
    }
    materialized = {
        "blocks": [
            {
                "block_id": block_id,
                "type": "paragraph",
                "text": text,
                "order": 1,
            }
        ],
        "child_sections": [],
    }
    return section, materialized


def _structured_document(text: str, *, title: str) -> dict[str, Any]:
    return {
        "sections": [
            {
                "section_id": "bid-section-1",
                "parent_section_id": None,
                "title": title,
                "path": [title],
                "start_order": 1,
                "direct_block_ids": ["bid-block-1"],
                "block_ids": ["bid-block-1"],
            }
        ],
        "blocks": [
            {
                "block_id": "bid-block-1",
                "type": "paragraph",
                "text": text,
                "order": 1,
            }
        ],
        "tables": [],
        "images": [],
    }


def _matched_response(
    *, status: str = "pass", issues: list[dict[str, Any]] | None = None
):
    return {
        "status": status,
        "summary": f"LLM 结论：{status}",
        "issues": issues or [],
        "semantic_match": {
            "status": "matched",
            "reason": "用途与核心内容对应。",
        },
    }


def test_actual_value_with_field_prompt_residual_is_detected_from_field_source():
    template = _template(
        "系【XX公司[投标人单位名称]】的法定代表人/负责人。",
        ["投标人单位名称"],
    )
    bid_section, materialized = _bid_materialized(
        "系【惠州惠阳图迹智慧运营科技有限公司[投标人单位名称]】的法定代表人/负责人。"
    )

    hits = find_template_placeholder_residuals(template, bid_section, materialized)

    assert len(hits) == 1
    assert hits[0]["field"] == "投标人单位名称"
    assert hits[0]["marker"] == "[投标人单位名称]"
    assert hits[0]["bid_block_id"] == "bid-block-1"
    assert hits[0]["template_source_block_ids"] == ["tender-block-1"]
    assert hits[0]["actual_context"] == "惠州惠阳图迹智慧运营科技有限公司"


def test_field_prompt_is_not_reported_when_bidder_replaced_it_completely():
    template = _template(
        "系【XX公司[投标人单位名称]】的法定代表人。", ["投标人单位名称"]
    )
    bid_section, materialized = _bid_materialized(
        "系【惠州惠阳图迹智慧运营科技有限公司】的法定代表人。"
    )

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_field_prompt_is_not_reported_when_bid_module_does_not_contain_it():
    template = _template(
        "系【XX公司[投标人单位名称]】的法定代表人。", ["投标人单位名称"]
    )
    bid_section, materialized = _bid_materialized(
        "系【惠州惠阳图迹智慧运营科技有限公司】的法定代表人。"
    )

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_special_characters_without_field_source_evidence_are_ignored():
    template = _template(
        "普通说明文字（不要改动）\n普通方括号[说明文字]\n普通XX和______。",
        [],
    )
    bid_section, materialized = _bid_materialized(
        "普通说明文字（已经补充）\n普通方括号[说明文字]\n普通XX和______。"
    )

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_external_project_marker_is_detected_even_when_context_is_unchanged():
    template = _template(
        "我方参加【2026-2027年云网络客户项目支持服务[招标项目名称]】投标。",
        [],
    )
    bid_section, materialized = _bid_materialized(
        "我方参加【2026-2027年云网络客户项目支持服务[招标项目名称]】投标。"
    )

    hits = find_template_placeholder_residuals(template, bid_section, materialized)

    assert len(hits) == 1
    assert hits[0]["field"] == "招标项目名称"
    assert hits[0]["marker"] == "[招标项目名称]"
    assert hits[0]["actual_context"] == "2026-2027年云网络客户项目支持服务"


def test_external_project_marker_supports_chinese_parentheses():
    template = _template(
        "我方参加【2026-2027年云网络客户项目支持服务（招标项目名称）】投标。",
        [],
    )
    bid_section, materialized = _bid_materialized(
        "我方参加【2026-2027年云网络客户项目支持服务（招标项目名称）】投标。"
    )

    hits = find_template_placeholder_residuals(
        template,
        {"投标函": bid_section},
        materialized,
    )

    assert len(hits) == 1
    assert hits[0]["field"] == "招标项目名称"
    assert hits[0]["marker"] == "（招标项目名称）"


def test_standalone_chinese_external_marker_is_detected_after_actual_value():
    template = _template("已仔细研究了（招标项目名称）标包。", [])
    bid_section, materialized = _bid_materialized(
        "已仔细研究了2026-2027年云网络客户项目支持服务（招标项目名称）"
        "2026-2027年云网络客户项目支持服务标包。"
    )

    hits = find_template_placeholder_residuals(template, bid_section, materialized)

    assert len(hits) == 1
    assert hits[0]["marker"] == "（招标项目名称）"


def test_standalone_chinese_external_marker_with_unchanged_actual_value_is_detected():
    template = _template(
        "2026-2027年云网络客户项目支持服务（招标项目名称）",
        [],
    )
    bid_section, materialized = _bid_materialized(
        "2026-2027年云网络客户项目支持服务（招标项目名称）"
    )

    hits = find_template_placeholder_residuals(template, bid_section, materialized)

    assert len(hits) == 1
    assert hits[0]["marker"] == "（招标项目名称）"


def test_standalone_external_marker_without_actual_value_is_ignored():
    template = _template("已仔细研究了（招标项目名称）标包。", [])
    bid_section, materialized = _bid_materialized("已仔细研究了（招标项目名称）标包。")

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_standalone_external_marker_with_only_changed_prose_is_ignored():
    template = _template("已仔细研究了（招标项目名称）标包。", [])
    bid_section, materialized = _bid_materialized("我方认真研究了（招标项目名称）标包。")

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_field_marker_is_detected_when_filled_context_is_unchanged():
    template = _template(
        "我方参加【2026-2027年云网络客户项目支持服务[项目名称]】投标。",
        ["项目名称"],
    )
    bid_section, materialized = _bid_materialized(
        "我方参加【2026-2027年云网络客户项目支持服务[项目名称]】投标。"
    )

    hits = find_template_placeholder_residuals(
        template,
        {"投标函": bid_section},
        materialized,
    )

    assert len(hits) == 1
    assert hits[0]["field"] == "项目名称"
    assert hits[0]["marker"] == "[项目名称]"


def test_standalone_field_marker_with_unchanged_actual_value_is_detected():
    template = _template("2026-2027年云网络客户项目支持服务[项目名称]", ["项目名称"])
    bid_section, materialized = _bid_materialized(
        "2026-2027年云网络客户项目支持服务[项目名称]"
    )

    hits = find_template_placeholder_residuals(template, bid_section, materialized)

    assert len(hits) == 1
    assert hits[0]["field"] == "项目名称"
    assert hits[0]["marker"] == "[项目名称]"


def test_same_unfilled_field_context_is_not_detected():
    template = _template("我方参加【XX公司[投标人名称]】投标。", ["投标人名称"])
    bid_section, materialized = _bid_materialized("我方参加【XX公司[投标人名称]】投标。")

    hits = find_template_placeholder_residuals(
        template,
        {"投标函": bid_section},
        materialized,
    )

    assert hits == []


def test_external_package_marker_is_detected_from_changed_bid_context():
    template = _template("共同参加【XX标包[标包名称]】投标。", [])
    bid_section, materialized = _bid_materialized(
        "共同参加【2026-2027年云网络客户项目支持服务标包[标包名称]】投标。"
    )

    hits = find_template_placeholder_residuals(template, bid_section, materialized)

    assert len(hits) == 1
    assert hits[0]["field"] == "标包名称"
    assert hits[0]["marker"] == "[标包名称]"


def test_external_marker_without_filled_context_is_ignored():
    template = _template("项目范围：【[招标项目名称]】。", [])
    bid_section, materialized = _bid_materialized("项目范围：【[招标项目名称]】。")

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_pass_llm_result_is_overridden_by_external_placeholder_issues():
    template = _template(
        "我方参加【2026-2027年云网络客户项目支持服务[招标项目名称]】"
        "【XX标包[标包名称]】投标。",
        [],
        name="投标函",
    )
    llm = PassLLM(_matched_response())

    result = run_template_text_review(
        {"templates": [template]},
        {
            "structured_document": _structured_document(
                "我方参加【2026-2027年云网络客户项目支持服务[招标项目名称]】"
                "【2026-2027年云网络客户项目支持服务标包[标包名称]】投标。",
                title="1 投标函",
            )
        },
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert llm.calls == 1
    assert review["status"] == "fail"
    assert {issue["placeholder_marker"] for issue in review["issues"]} == {
        "[招标项目名称]",
        "[标包名称]",
    }


def test_underlines_are_scoped_to_a_parsed_field_and_require_actual_value():
    template = _template("日期：____年__月__日", ["日期"])
    no_actual_section, no_actual = _bid_materialized("日期：____年__月__日")
    actual_section, actual = _bid_materialized("日期：2026年__月__日")

    assert (
        find_template_placeholder_residuals(template, no_actual_section, no_actual)
        == []
    )
    hits = find_template_placeholder_residuals(template, actual_section, actual)
    assert len(hits) == 1
    assert hits[0]["field"] == "日期"
    assert "__" in hits[0]["marker"]


def test_table_block_uses_the_same_field_scoped_source_evidence():
    template = _template(
        "<table><tr><td>名称</td><td>【XX公司[投标人名称]】</td></tr></table>",
        ["投标人名称"],
    )
    bid_section = {"section_id": "bid-section-1", "title": "1 基本情况表"}
    materialized = {
        "blocks": [
            {
                "block_id": "bid-table-1",
                "type": "table",
                "text": "表格",
                "rows": [["名称", "【惠州公司[投标人名称]】"]],
                "order": 1,
            }
        ],
        "child_sections": [],
    }

    hits = find_template_placeholder_residuals(template, bid_section, materialized)

    assert len(hits) == 1
    assert hits[0]["field"] == "投标人名称"
    assert hits[0]["bid_block_id"] == "bid-table-1"


def test_ellipsis_and_blank_extension_rows_are_not_deterministic_placeholders():
    template = _template("项目名称：……\n扩展行：", ["项目名称"])
    bid_section, materialized = _bid_materialized("项目名称：实际项目\n扩展行：……")

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_not_applicable_bid_section_is_skipped_even_if_marker_and_text_coexist():
    template = _template(
        "联合体名称：【XX和XX[联合体名称]】",
        ["联合体名称"],
        name="联合体协议书（如有）",
    )
    bid_section, materialized = _bid_materialized(
        "联合体名称：【惠州公司[联合体名称]】",
        title="7 联合体协议书（本项目不适用）",
    )

    assert (
        find_template_placeholder_residuals(template, bid_section, materialized) == []
    )


def test_pass_llm_result_is_overridden_by_high_confidence_deterministic_issue():
    template = _template(
        "系【XX公司[投标人单位名称]】的法定代表人/负责人。",
        ["投标人单位名称"],
    )
    llm = PassLLM(_matched_response())

    result = run_template_text_review(
        {"templates": [template]},
        {
            "structured_document": _structured_document(
                "系【惠州惠阳图迹智慧运营科技有限公司[投标人单位名称]】的法定代表人/负责人。",
                title="7 法定代表人/负责人身份证明",
            )
        },
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert llm.calls == 1
    assert review["status"] == "fail"
    assert review["business_status"] == "fail"
    assert len(review["issues"]) == 1
    assert review["issues"][0]["type"] == "missing_fill"
    assert review["issues"][0]["detected_by"] == ["deterministic"]
    assert review["deterministic_placeholder_residuals"][0]["field"] == "投标人单位名称"


def test_llm_and_deterministic_same_residual_are_deduplicated_and_audited():
    template = _template(
        "系【XX公司[投标人单位名称]】的法定代表人/负责人。",
        ["投标人单位名称"],
    )
    issue = {
        "type": "missing_fill",
        "requirement": "投标人单位名称应填写实际名称。",
        "actual": "仍保留[投标人单位名称]提示。",
        "reason": "字段提示文字仍残留。",
    }
    llm = PassLLM(_matched_response(status="fail", issues=[issue]))

    result = run_template_text_review(
        {"templates": [template]},
        {
            "structured_document": _structured_document(
                "系【惠州惠阳图迹智慧运营科技有限公司[投标人单位名称]】的法定代表人/负责人。",
                title="7 法定代表人/负责人身份证明",
            )
        },
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert len(review["issues"]) == 1
    assert review["issues"][0]["detected_by"] == ["llm", "deterministic"]


def test_different_field_residuals_in_one_block_are_not_merged():
    template = _template(
        "【XX公司[投标人单位名称]】【XX姓名[法定代表人姓名]】",
        ["投标人单位名称", "法定代表人姓名"],
    )
    llm = PassLLM(_matched_response())

    result = run_template_text_review(
        {"templates": [template]},
        {
            "structured_document": _structured_document(
                "【惠州公司[投标人单位名称]】【刘县军[法定代表人姓名]】",
                title="7 法定代表人/负责人身份证明",
            )
        },
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert len(review["deterministic_placeholder_residuals"]) == 2
    assert len(review["issues"]) == 2
    assert {issue["placeholder_marker"] for issue in review["issues"]} == {
        "[投标人单位名称]",
        "[法定代表人姓名]",
    }
