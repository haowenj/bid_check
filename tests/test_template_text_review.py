from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import app.compliance_extraction as extraction_module
from app.api import build_default_workflow
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import OpenAICompatibleLLM
from app.template_text_review import (
    TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT,
    TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT,
    build_template_text_review_user_prompt,
    run_template_text_review,
)

TARGET_NAMES = (
    "投标函",
    "法定代表人/负责人授权委托书",
    "联合体协议书",
)


class RecordingReviewLLM:
    model = "fixture-review-model"

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.responses = responses or {
            "投标函": {
                "status": "pass",
                "summary": "投标函文本完整响应。",
                "issues": [],
                "semantic_match": {
                    "status": "matched",
                    "reason": "文件用途、填写对象和核心内容均与模板对应。",
                },
            },
            "法定代表人/负责人授权委托书": {
                "status": "fail",
                "summary": "授权委托书仍残留模板占位内容。",
                "issues": [
                    {
                        "type": "missing_fill",
                        "requirement": "应填写投标人法定代表人/负责人姓名。",
                        "actual": "仍保留[投标人法定代表人/负责人姓名]。",
                        "reason": "实际模块中仍出现明显占位提示。",
                    }
                ],
                "semantic_match": {
                    "status": "matched",
                    "reason": "文件用途、填写对象和核心内容均与模板对应。",
                },
            },
            "联合体协议书": {
                "status": "uncertain",
                "summary": "当前文本不足以确认联合体模板是否适用。",
                "issues": [],
                "semantic_match": {
                    "status": "matched",
                    "reason": "模块用途与联合体协议模板对应，适用性另行判断。",
                },
            },
        }

    def review_template(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt))
        template_name = next(
            name for name in TARGET_NAMES if f"模板名称：\n{name}" in user_prompt
        )
        return self.responses[template_name]


class ConcurrentReviewLLM:
    model = "concurrent-fixture-review-model"

    def __init__(
        self,
        *,
        responses: dict[str, dict[str, Any]],
        failure_modes: dict[str, str] | None = None,
        delays: dict[str, float] | None = None,
    ):
        self.responses = responses
        self.failure_modes = failure_modes or {}
        self.delays = delays or {}
        self.calls: list[tuple[str, str]] = []
        self.started_names: list[str] = []
        self.max_active = 0
        self._active = 0
        self._entered = 0
        self._lock = threading.Lock()
        self._first_workers = threading.Barrier(3, timeout=1)

    def review_template(self, system_prompt: str, user_prompt: str) -> Any:
        marker = "模板名称：\n"
        start = user_prompt.index(marker) + len(marker)
        end = user_prompt.index("\n\n招标模板完整内容", start)
        template_name = user_prompt[start:end]
        with self._lock:
            self.calls.append((system_prompt, user_prompt))
            self.started_names.append(template_name)
            self._entered += 1
            first_workers = self._entered <= 3
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if first_workers:
                self._first_workers.wait()
            time.sleep(self.delays.get(template_name, 0))
            failure_mode = self.failure_modes.get(template_name)
            if failure_mode == "exception":
                raise RuntimeError(f"模拟 {template_name} LLM 请求失败")
            if failure_mode == "invalid_json":
                return "不是 JSON"
            return self.responses[template_name]
        finally:
            with self._lock:
                self._active -= 1


class RetryOnceReviewLLM:
    model = "retry-once-fixture-model"

    def __init__(self, *, fail_name: str | None = None):
        self.fail_name = fail_name
        self.calls: list[tuple[str, str]] = []
        self.attempts_by_name: dict[str, int] = {}

    def review_template(self, system_prompt: str, user_prompt: str) -> Any:
        marker = "模板名称：\n"
        start = user_prompt.index(marker) + len(marker)
        end = user_prompt.index("\n\n招标模板完整内容", start)
        template_name = user_prompt[start:end]
        self.calls.append((system_prompt, user_prompt))
        attempt = self.attempts_by_name.get(template_name, 0) + 1
        self.attempts_by_name[template_name] = attempt
        if template_name == self.fail_name:
            raise TimeoutError(f"模拟 {template_name} 请求超时")
        if attempt == 1:
            raise TimeoutError(f"模拟 {template_name} 首次请求超时")
        return {
            "status": "pass",
            "summary": "重试后文本完整。",
            "issues": [],
            "semantic_match": {
                "status": "matched",
                "reason": "文件用途、填写对象和核心内容均与模板对应。",
            },
        }


class FixedSemanticReviewLLM:
    model = "semantic-fixture-review-model"

    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def review_template(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt))
        return copy.deepcopy(self.response)


class ExceptionReviewFixtureLLM:
    model = "exception-fixture-review-model"

    def __init__(
        self,
        *,
        main_response: dict[str, Any],
        exception_response: dict[str, Any] | Exception,
    ):
        self.main_response = main_response
        self.exception_response = exception_response
        self.main_calls: list[tuple[str, str]] = []
        self.exception_calls: list[tuple[str, str]] = []

    def review_template(self, system_prompt: str, user_prompt: str) -> Any:
        self.main_calls.append((system_prompt, user_prompt))
        return copy.deepcopy(self.main_response)

    def review_template_exception(self, system_prompt: str, user_prompt: str) -> Any:
        self.exception_calls.append((system_prompt, user_prompt))
        if isinstance(self.exception_response, Exception):
            raise self.exception_response
        return copy.deepcopy(self.exception_response)


def _matched_main_response(
    *,
    status: str,
    issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": f"主检查结论：{status}。",
        "issues": issues or [],
        "semantic_match": {
            "status": "matched",
            "reason": "模块用途与模板核心内容对应。",
        },
    }


def _exception_response(
    *,
    outcome: str,
    issue_reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "issue_reviews": issue_reviews,
    }


def _compact_exception_response(
    *,
    outcome: str,
    issue_reviews: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    response = {
        "outcome": outcome,
        "issue_reviews": issue_reviews,
    }
    response.update(extra)
    return response


def _template(name: str, body: str, fields: list[str]) -> dict[str, Any]:
    return {
        "id": f"template-{name}",
        "name": name,
        "section": "第六章 投标文件格式",
        "body": body,
        "fields": fields,
        "source": {
            "section": "第六章 投标文件格式",
            "block_ids": [],
            "source_text": body,
        },
    }


def _structured_bid_document() -> dict[str, Any]:
    sections = [
        {
            "section_id": "s-tender-letter",
            "parent_section_id": None,
            "title": "1 投标函",
            "path": ["1 投标函"],
            "start_order": 1,
            "direct_block_ids": ["b1", "b2", "b3"],
            "block_ids": ["b1", "b2", "b3"],
        },
        {
            "section_id": "s-authority",
            "parent_section_id": None,
            "title": "2 法定代表人/负责人授权委托书",
            "path": ["2 法定代表人/负责人授权委托书"],
            "start_order": 4,
            "direct_block_ids": ["b4", "b5", "b6"],
            "block_ids": ["b4", "b5", "b6"],
        },
        {
            "section_id": "s-consortium",
            "parent_section_id": None,
            "title": "3 联合体协议书（本项目不适用）",
            "path": ["3 联合体协议书（本项目不适用）"],
            "start_order": 7,
            "direct_block_ids": ["b7", "b8"],
            "block_ids": ["b7", "b8"],
        },
    ]
    return {
        "sections": sections,
        "blocks": [
            {"block_id": "b1", "type": "heading", "text": "1 投标函", "order": 1},
            {
                "block_id": "b2",
                "type": "paragraph",
                "text": "投标人名称：示例公司",
                "order": 2,
            },
            {
                "block_id": "b3",
                "type": "paragraph",
                "text": "固定承诺：我方将按模板要求履行。",
                "order": 3,
            },
            {
                "block_id": "b4",
                "type": "heading",
                "text": "2 法定代表人/负责人授权委托书",
                "order": 4,
            },
            {
                "block_id": "b5",
                "type": "paragraph",
                "text": "本人[投标人法定代表人/负责人姓名]系示例公司的负责人。",
                "order": 5,
            },
            {"block_id": "b6", "type": "image", "text": "", "order": 6},
            {
                "block_id": "b7",
                "type": "heading",
                "text": "3 联合体协议书（本项目不适用）",
                "order": 7,
            },
            {
                "block_id": "b8",
                "type": "table",
                "text": "职责 | 分工",
                "order": 8,
            },
        ],
        "tables": [
            {
                "block_id": "b8",
                "rows": [["职责", "分工"], ["项目实施", "不适用"]],
            }
        ],
        "images": [{"block_id": "b6", "caption": "身份证图片"}],
    }


def _structured_bid_document_with_relation_table() -> dict[str, Any]:
    document = _structured_bid_document()
    document["sections"].append(
        {
            "section_id": "s-relation",
            "parent_section_id": None,
            "title": "4 特定关系信息收集表",
            "path": ["4 特定关系信息收集表"],
            "start_order": 9,
            "direct_block_ids": ["b9", "b10"],
            "block_ids": ["b9", "b10"],
        }
    )
    document["blocks"].extend(
        [
            {
                "block_id": "b9",
                "type": "heading",
                "text": "4 特定关系信息收集表",
                "order": 9,
            },
            {
                "block_id": "b10",
                "type": "paragraph",
                "text": "关联关系信息：待核实。",
                "order": 10,
            },
        ]
    )
    return document


def _structured_bid_document_with_open_source_section(
    *,
    section_text: str = "开源软件清单表格为空。",
) -> dict[str, Any]:
    document = _structured_bid_document()
    document["sections"].append(
        {
            "section_id": "s-open-source",
            "parent_section_id": None,
            "title": "4 开源软件清单（无）",
            "path": ["4 开源软件清单（无）"],
            "start_order": 9,
            "direct_block_ids": ["b9", "b10"],
            "block_ids": ["b9", "b10"],
        }
    )
    document["blocks"].extend(
        [
            {
                "block_id": "b9",
                "type": "heading",
                "text": "4 开源软件清单（无）",
                "order": 9,
            },
            {
                "block_id": "b10",
                "type": "paragraph",
                "text": section_text,
                "order": 10,
            },
        ]
    )
    return document


def _templates() -> list[dict[str, Any]]:
    return [
        _template(
            "投标函",
            "投标函\n固定承诺：不得删除。\n投标人名称：____",
            ["投标人名称"],
        ),
        _template(
            "法定代表人/负责人授权委托书",
            "授权委托书\n委托期限：____\n附：身份证明复印件。",
            ["委托期限"],
        ),
        _template(
            "联合体协议书",
            "联合体协议书（如有）\n联合体核心职责与分工。",
            [],
        ),
        _template("廉洁投标承诺书", "不应在本轮执行。", ["日期"]),
    ]


def test_template_semantic_mismatch_skips_business_review_and_stats_as_candidate():
    llm = FixedSemanticReviewLLM(
        {
            "status": "fail",
            "summary": "不应被采用的业务结论。",
            "issues": [
                {
                    "type": "not-a-template-issue",
                    "requirement": "模板要求填写名称。",
                    "actual": "未填写。",
                    "reason": "业务判断不应执行。",
                }
            ],
            "semantic_match": {
                "status": "mismatched",
                "reason": "投标模块只是标题相似，实际用途是另一类承诺函。",
            },
        }
    )

    result = run_template_text_review(
        {"templates": _templates()[:1]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["semantic_match"]["status"] == "mismatched"
    assert review["business_status"] == "not_run"
    assert review["execution_status"] == "semantic_skipped"
    assert review["issues"] == []
    assert result["stats"]["semantic_mismatched_count"] == 1
    assert result["stats"]["fail_count"] == 0


def test_template_semantic_uncertain_does_not_force_business_uncertain_result():
    llm = FixedSemanticReviewLLM(
        {
            "status": "uncertain",
            "summary": "不能确认模块对应关系。",
            "issues": [],
            "semantic_match": {
                "status": "uncertain",
                "reason": "当前模块文本不足以确认文件用途是否一致。",
            },
        }
    )

    result = run_template_text_review(
        {"templates": _templates()[:1]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["semantic_match"]["status"] == "uncertain"
    assert review["business_status"] == "not_run"
    assert review["execution_status"] == "semantic_skipped"
    assert result["stats"]["semantic_uncertain_count"] == 1
    assert result["stats"]["uncertain_count"] == 0


def test_template_semantic_match_preserves_existing_business_result():
    llm = FixedSemanticReviewLLM(
        {
            "status": "fail",
            "summary": "确认对应后发现文本问题。",
            "issues": [
                {
                    "type": "other",
                    "requirement": "保留固定正文。",
                    "actual": "固定正文缺失。",
                    "reason": "当前模块缺少模板要求的固定正文。",
                }
            ],
            "semantic_match": {
                "status": "matched",
                "reason": "文件用途、填写对象和核心内容均与模板对应。",
            },
        }
    )

    result = run_template_text_review(
        {"templates": _templates()[:1]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["semantic_match"]["status"] == "matched"
    assert review["business_status"] == "fail"
    assert review["execution_status"] == "completed"
    assert review["status"] == "fail"
    assert len(review["issues"]) == 1
    assert result["stats"]["fail_count"] == 1


def test_exception_review_confirms_direct_placeholder_fail_and_keeps_audit_trace():
    issue = {
        "type": "missing_fill",
        "requirement": "应填写投标人单位名称。",
        "actual": "实际文本仍残留[投标人单位名称]。",
        "reason": "实际模块中保留了明显模板占位内容。",
    }
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="fail", issues=[issue]),
        exception_response=_exception_response(
            outcome="uncertain",
            issue_reviews=[
                {
                    "original_issue_index": 0,
                    "decision": "keep",
                    "reason": "占位符直接出现在投标模块实际文本中。",
                }
            ],
        ),
    )

    result = run_template_text_review(
        {"templates": _templates()[:1]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["initial_review"]["status"] == "fail"
    assert review["exception_review"]["review_decision"] == "confirm"
    assert review["status"] == "fail"
    assert review["business_status"] == "fail"
    assert review["issues"] == [issue]
    assert result["stats"]["exception_review_candidate_count"] == 1
    assert result["stats"]["exception_review_call_count"] == 1
    assert result["stats"]["exception_review_confirm_count"] == 1
    assert result["stats"]["exception_review_revise_count"] == 0
    assert result["stats"]["exception_review_failed_count"] == 0
    assert llm.exception_calls[0][0] == TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT
    assert "不得创建第一次检查结果中不存在的新 issue" in llm.exception_calls[0][0]
    assert "第一次模板检查结果" in llm.exception_calls[0][1]


def test_exception_review_derives_kept_fail_issues_from_original_indices():
    kept_issue = {
        "type": "missing_fill",
        "requirement": "特定关系信息收集表应填写关联关系。",
        "actual": "关联关系信息仍未填写。",
        "reason": "当前表格没有填写关联关系信息。",
    }
    rejected_issue = {
        "type": "other",
        "requirement": "表格应扩展填写所有可能关系。",
        "actual": "表格没有扩展行。",
        "reason": "仅凭空白扩展行不能确认存在漏填。",
    }
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(
            status="fail",
            issues=[kept_issue, rejected_issue],
        ),
        exception_response=_compact_exception_response(
            outcome="pass",
            issue_reviews=[
                {
                    "original_issue_index": 0,
                    "decision": "keep",
                    "reason": "当前表格直接出现未填写的关联关系信息。",
                },
                {
                    "original_issue_index": 1,
                    "decision": "reject",
                    "reason": "当前输入不能证明需要扩展填写所有关系。",
                },
            ],
            # Legacy/conflicting fields must not control the code-generated result.
            review_decision="revise",
            final_status="pass",
            final_issues=[
                {
                    "type": "other",
                    "requirement": "模型重写的问题不应被采用。",
                    "actual": "模型自行生成。",
                    "reason": "不应进入最终结果。",
                }
            ],
        ),
    )

    result = run_template_text_review(
        {
            "templates": [
                _template(
                    "特定关系信息收集表",
                    "特定关系信息收集表\n关联关系信息应填写。",
                    [],
                )
            ]
        },
        {"structured_document": _structured_bid_document_with_relation_table()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["exception_review"]["execution_status"] == "completed"
    assert review["exception_review"]["review_decision"] == "confirm"
    assert review["exception_review"]["final_status"] == "fail"
    assert review["exception_review"]["final_issues"] == [kept_issue]
    assert review["issues"] == [kept_issue]
    assert review["status"] == "fail"
    assert review["business_status"] == "fail"
    assert result["stats"]["exception_review_failed_count"] == 0


def test_exception_review_all_rejected_uses_code_derived_revision_status():
    issue = {
        "type": "missing_fill",
        "requirement": "应填写表格内容。",
        "actual": "存在空白预留行。",
        "reason": "仅凭空白行不能确认漏填。",
    }
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="fail", issues=[issue]),
        exception_response=_compact_exception_response(
            outcome="uncertain",
            issue_reviews=[
                {
                    "original_issue_index": 0,
                    "decision": "reject",
                    "reason": "当前输入不足以证明该位置必须填写。",
                }
            ],
        ),
    )

    result = run_template_text_review(
        {"templates": _templates()[:1]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["exception_review"]["execution_status"] == "completed"
    assert review["exception_review"]["review_decision"] == "revise"
    assert review["exception_review"]["final_status"] == "uncertain"
    assert review["exception_review"]["final_issues"] == []
    assert review["status"] == "uncertain"
    assert review["issues"] == []


def test_exception_review_not_applicable_uses_outcome_without_final_issues():
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="not_applicable"),
        exception_response=_compact_exception_response(
            outcome="uncertain",
            issue_reviews=[],
            final_status="not_applicable",
            final_issues=[{"type": "other"}],
        ),
    )

    result = run_template_text_review(
        {"templates": [_template("投标函", "如需授权专用印章则填写。", [])]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["exception_review"]["execution_status"] == "completed"
    assert review["exception_review"]["review_decision"] == "revise"
    assert review["exception_review"]["final_status"] == "uncertain"
    assert review["exception_review"]["final_issues"] == []
    assert review["status"] == "uncertain"
    assert review["issues"] == []


def test_exception_review_is_selective_and_pass_keeps_not_run_audit_record():
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="pass"),
        exception_response={"not": "called"},
    )

    result = run_template_text_review(
        {"templates": _templates()[:1]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert llm.exception_calls == []
    assert review["status"] == "pass"
    assert review["exception_review"]["execution_status"] == "not_run"
    assert result["stats"]["exception_review_candidate_count"] == 0
    assert result["stats"]["exception_review_call_count"] == 0


def test_exception_review_rejects_local_empty_rule_issue_and_clears_all_fail_issues():
    issue = {
        "type": "missing_fill",
        "requirement": "第二张表应填写不涉及。",
        "actual": "第二张表完全留空。",
        "reason": "第一张表填写了不涉及，因此第二张表也必须填写不涉及。",
    }
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="fail", issues=[issue]),
        exception_response=_exception_response(
            outcome="pass",
            issue_reviews=[
                {
                    "original_issue_index": 0,
                    "decision": "reject",
                    "reason": "当前子区域明确规定完全留空即视为不涉及，不能套用其他表格规则。",
                }
            ],
        ),
    )

    result = run_template_text_review(
        {"templates": [_template("投标函", "第二张表：留空即视为不涉及。", [])]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["initial_review"]["issues"] == [issue]
    assert review["exception_review"]["final_issues"] == []
    assert review["status"] == "pass"
    assert review["business_status"] == "pass"
    assert review["issues"] == []
    assert result["stats"]["exception_review_revise_count"] == 1


def test_exception_review_rejects_external_chapter_and_cross_subregion_reasoning():
    issue = {
        "type": "missing_fill",
        "requirement": "应逐项填写第三章评标办法中的所有评审因素。",
        "actual": "表格存在省略号和空白预留行。",
        "reason": "第三章通常还有其他评审因素，空白行说明漏填。",
    }
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="fail", issues=[issue]),
        exception_response=_exception_response(
            outcome="uncertain",
            issue_reviews=[
                {
                    "original_issue_index": 0,
                    "decision": "reject",
                    "reason": "当前输入没有提供第三章具体评审项，不能凭省略号和空白行推断漏填。",
                }
            ],
        ),
    )

    result = run_template_text_review(
        {"templates": [_template("投标函", "按照第三章评标办法逐项填写……", [])]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["status"] == "uncertain"
    assert review["business_status"] == "uncertain"
    assert review["issues"] == []
    assert review["exception_review"]["review_decision"] == "revise"


def test_exception_review_changes_bidder_only_not_applicable_to_uncertain():
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="not_applicable"),
        exception_response=_exception_response(
            outcome="uncertain",
            issue_reviews=[],
        ),
    )

    result = run_template_text_review(
        {"templates": [_template("投标函", "如需授权专用印章则填写。", [])]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["initial_review"]["status"] == "not_applicable"
    assert review["exception_review"]["final_status"] == "uncertain"
    assert review["status"] == "uncertain"
    assert review["business_status"] == "uncertain"
    assert result["stats"]["exception_review_revise_count"] == 1


def test_exception_review_failure_requires_manual_confirmation_and_isolated():
    issue = {
        "type": "other",
        "requirement": "应保留固定正文。",
        "actual": "固定正文缺失。",
        "reason": "模板固定正文在投标模块中不存在。",
    }
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="fail", issues=[issue]),
        exception_response=TimeoutError("异常复核请求超时"),
    )

    result = run_template_text_review(
        {"templates": [_template("投标函", "固定正文：不得删除。", [])]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["initial_review"]["status"] == "fail"
    assert review["exception_review"]["execution_status"] == "failed"
    assert review["manual_review_required"] is True
    assert review["status"] == "uncertain"
    assert review["business_status"] == "uncertain"
    assert review["issues"] == []
    assert result["stats"]["exception_review_call_count"] == 2
    assert result["stats"]["exception_review_failed_count"] == 1


def test_template_prompt_requires_semantic_gate_before_content_review():
    assert "语义对应" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "标题相似" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "关键词重复" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "先判断" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT


def test_system_prompt_scopes_independent_subregions_and_conditional_content():
    assert (
        "【局部规则优先级（强制）】"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "具体子区域自身明确写出的规则"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "该子区域自身明确写出的规则"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "模板针对该子区域的编制说明"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "模板整体的一般要求"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "本检查器的通用 missing_fill / missing_content / substantive_change 判断规则"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "留空即视为不涉及"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "无填写即视为承诺不涉及"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "不涉及时无需填写"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "无时可以不填写"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "【Issue 生成前强制复核】"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "requirement、actual、reason 是否全部属于同一个具体子区域"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert (
        "任意一项无法确认，该 issue 都不得输出"
        in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert "模板允许留空" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "适用条件无法确认" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "【省略号、示例行和预留行】" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "不得仅因为投标表格中存在“……”" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "必须先证明这个位置确实是当前投标人应该完成的实际填写位置" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "【模板内部独立子区域的判断边界】" not in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "【抽象示例】" not in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT


def test_template_prompt_adds_issue_review_without_copying_system_rules():
    prompt = build_template_text_review_user_prompt(
        _template("特定关系信息收集表", "区域A：留空即视为不涉及。", ["字段A"]),
        bid_module_name="特定关系信息收集表",
        bid_module_content="区域A：",
    )

    assert (
        "生成任何 issue 前，必须按照系统提示词中的‘同一子区域、局部规则优先、Issue 生成前强制复核’规则再次确认"
        in prompt
    )
    assert "【局部规则优先级（强制）】" not in prompt
    assert "【Issue 生成前强制复核】" not in prompt


def test_template_prompt_keeps_matched_semantic_reason_short():
    assert "semantic_match.status=matched" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "reason 只允许输出一个极短结论" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "不要复述模板名称、投标模块名称、标题、正文结构" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "mismatched 或 uncertain" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT


def test_template_prompt_limits_result_fields_to_short_final_facts():
    assert "【结果字段写作边界（强制）】" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "issue.requirement 只写招标模板中的具体要求或与问题直接相关的最小原文依据" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "issue.actual 只写投标文件中直接观察到的实际情况" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "issue.reason 只写构成问题的直接原因" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "summary 只写最终结论" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "禁止把分析过程写进 JSON" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "每个字段都必须能够直接展示给用户" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT


def test_template_prompt_does_not_treat_bidder_only_non_applicable_claim_as_pass():
    assert "模块标题、括号、表格留空" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    assert "不能仅凭这些文字把条件性模板判为 pass" in TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT


def test_exception_prompt_treats_bidder_only_non_applicable_claim_as_insufficient():
    assert "标题、括号或投标人单方声明不是独立事实证据" in TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT
    assert "仅有“（无）”“无”“不涉及”或“本项目不适用”时，必须返回 uncertain" in TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT
    assert "只有招标模板或当前输入中的其他独立事实明确证明条件未触发" in TEMPLATE_TEXT_EXCEPTION_REVIEW_SYSTEM_PROMPT


def test_exception_review_downgrades_title_only_not_applicable_to_uncertain():
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="not_applicable"),
        exception_response=_compact_exception_response(
            outcome="not_applicable",
            issue_reviews=[],
        ),
    )

    result = run_template_text_review(
        {
            "templates": [
                _template(
                    "开源软件清单",
                    "开源软件清单（如有）\n软件名称 | 证明材料",
                    [],
                )
            ]
        },
        {
            "structured_document": _structured_bid_document_with_open_source_section()
        },
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["initial_review"]["status"] == "not_applicable"
    assert review["exception_review"]["execution_status"] == "completed"
    assert review["exception_review"]["review_decision"] == "revise"
    assert review["exception_review"]["final_status"] == "uncertain"
    assert review["status"] == "uncertain"
    assert review["business_status"] == "uncertain"
    assert review["issues"] == []


def test_exception_review_can_confirm_not_applicable_from_tender_fact():
    llm = ExceptionReviewFixtureLLM(
        main_response=_matched_main_response(status="not_applicable"),
        exception_response=_compact_exception_response(
            outcome="not_applicable",
            issue_reviews=[],
        ),
    )

    result = run_template_text_review(
        {
            "templates": [
                _template(
                    "开源软件清单",
                    "本项目不涉及开源软件。\n开源软件清单（如有）\n软件名称 | 证明材料",
                    [],
                )
            ]
        },
        {
            "structured_document": _structured_bid_document_with_open_source_section()
        },
        llm=llm,
    )
    review = result["template_text_reviews"][0]

    assert review["exception_review"]["review_decision"] == "confirm"
    assert review["exception_review"]["final_status"] == "not_applicable"
    assert review["status"] == "not_applicable"
    assert review["business_status"] == "not_applicable"


def test_template_review_retries_one_timeout_with_same_prompt(tmp_path: Path):
    recorder = ComplianceExtractionRecorder(tmp_path / "task")
    llm = RetryOnceReviewLLM()

    result = run_template_text_review(
        {"templates": _templates()[:1]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
        recorder=recorder,
    )

    assert llm.attempts_by_name == {"投标函": 2}
    assert len(llm.calls) == 2
    assert llm.calls[0] == llm.calls[1]
    assert result["template_text_reviews"][0]["status"] == "pass"
    assert result["stats"]["llm_total_calls"] == 2
    assert result["stats"]["llm_completed_calls"] == 1
    assert result["stats"]["llm_failed_count"] == 0
    output_files = sorted((recorder.artifact_dir / "llm").glob("call_*_output.json"))
    assert len(output_files) == 2
    output_payloads = [
        json.loads(path.read_text(encoding="utf-8")) for path in output_files
    ]
    assert sorted(payload["attempt"] for payload in output_payloads) == [1, 2]
    assert sorted(payload["status"] for payload in output_payloads) == [
        "failed",
        "success",
    ]


def test_exhausted_timeout_retry_isolated_from_other_templates():
    llm = RetryOnceReviewLLM(fail_name="投标函")

    result = run_template_text_review(
        {"templates": _templates()[:2]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )

    assert llm.attempts_by_name == {"投标函": 2, "法定代表人/负责人授权委托书": 2}
    assert [item["template_name"] for item in result["template_text_reviews"]] == [
        "投标函",
        "法定代表人/负责人授权委托书",
    ]
    assert result["template_text_reviews"][0]["execution_status"] == "failed"
    assert result["template_text_reviews"][0]["status"] == "uncertain"
    assert result["template_text_reviews"][1]["status"] == "pass"
    assert result["stats"]["llm_total_calls"] == 4
    assert result["stats"]["llm_completed_calls"] == 1
    assert result["stats"]["llm_failed_count"] == 1


def _full_review_fixture() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    matched_names = [
        "扩展模板A",
        "投标函",
        "扩展模板B",
        "扩展模板C",
        "扩展模板D",
        "扩展模板E",
    ]
    sections: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    for index, name in enumerate(matched_names, start=1):
        block_id = f"b-full-{index}"
        sections.append(
            {
                "section_id": f"s-full-{index}",
                "parent_section_id": None,
                "title": f"{index} {name}",
                "path": [f"{index} {name}"],
                "start_order": index,
                "direct_block_ids": [block_id],
                "block_ids": [block_id],
            }
        )
        blocks.append(
            {
                "block_id": block_id,
                "type": "paragraph",
                "text": f"{name} 的完整实际模块内容。",
                "order": index,
            }
        )
    sections.append(
        {
            "section_id": "s-full-candidate",
            "parent_section_id": None,
            "title": "7 候选模板说明",
            "path": ["7 候选模板说明"],
            "start_order": 7,
            "direct_block_ids": ["b-full-candidate"],
            "block_ids": ["b-full-candidate"],
        }
    )
    blocks.append(
        {
            "block_id": "b-full-candidate",
            "type": "paragraph",
            "text": "候选模板说明的实际内容。",
            "order": 7,
        }
    )
    templates = [
        _template(name, f"{name}\n完整模板正文。", [])
        for name in matched_names
    ]
    templates.extend(
        [
            _template("候选模板", "候选模板\n完整模板正文。", []),
            _template("未匹配模板", "未匹配模板\n完整模板正文。", []),
        ]
    )
    return templates, {
        "sections": sections,
        "blocks": blocks,
        "tables": [],
        "images": [],
    }


def _full_review_responses() -> dict[str, dict[str, Any]]:
    return {
        "扩展模板A": {
            "status": "pass",
            "summary": "A 通过。",
            "issues": [],
            "semantic_match": {
                "status": "matched",
                "reason": "模块用途与模板对应。",
            },
        },
        "投标函": {
            "status": "fail",
            "summary": "投标函存在明确文本问题。",
            "issues": [
                {
                    "type": "other",
                    "requirement": "保留模板正文。",
                    "actual": "实际文本存在问题。",
                    "reason": "测试响应。",
                }
            ],
            "semantic_match": {
                "status": "matched",
                "reason": "模块用途与模板对应。",
            },
        },
        "扩展模板B": {
            "status": "uncertain",
            "summary": "B 待确认。",
            "issues": [],
            "semantic_match": {
                "status": "matched",
                "reason": "模块用途与模板对应。",
            },
        },
        "扩展模板C": {
            "status": "not_applicable",
            "summary": "C 不适用。",
            "issues": [],
            "semantic_match": {
                "status": "matched",
                "reason": "模块用途与模板对应。",
            },
        },
    }


def test_all_matched_templates_run_with_bounded_concurrency_and_stable_order():
    templates, document = _full_review_fixture()
    matched_names = [
        "扩展模板A",
        "投标函",
        "扩展模板B",
        "扩展模板C",
        "扩展模板D",
        "扩展模板E",
    ]
    llm = ConcurrentReviewLLM(
        responses=_full_review_responses(),
        failure_modes={
            "扩展模板D": "exception",
            "扩展模板E": "invalid_json",
        },
    )

    result = run_template_text_review(
        {"templates": templates},
        {"structured_document": document},
        llm=llm,
    )

    assert [item["template_name"] for item in result["template_text_reviews"]] == matched_names
    assert [item["status"] for item in result["template_text_reviews"]] == [
        "pass",
        "fail",
        "uncertain",
        "not_applicable",
        "uncertain",
        "uncertain",
    ]
    assert llm.max_active == 3
    assert set(llm.started_names) == set(matched_names)
    assert all(
        system_prompt == TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
        for system_prompt, _ in llm.calls
    )
    assert [
        item["execution_status"]
        for item in result["template_text_reviews"][-2:]
    ] == ["failed", "failed"]
    assert all(
        item["error_type"] in {"RuntimeError", "JSONDecodeError"}
        for item in result["template_text_reviews"][-2:]
    )
    assert result["stats"] == {
        "template_count": 8,
        "matched_template_count": 6,
        "code_candidate_count": 6,
        "selected_template_count": 6,
        "semantic_matched_count": 4,
        "semantic_mismatched_count": 0,
        "semantic_uncertain_count": 0,
        "no_bid_candidate_template_ids": ["template-未匹配模板"],
        "candidate_without_reliable_bid_text_template_ids": [],
        "max_concurrency": 3,
        "llm_total_calls": 6,
        "llm_completed_calls": 4,
        "llm_failed_count": 2,
        "llm_failed_calls": 2,
        "pass_count": 1,
        "fail_count": 1,
        "uncertain_count": 1,
        "not_applicable_count": 1,
        "business_status_counts": {
            "pass": 1,
            "fail": 1,
            "uncertain": 1,
            "not_applicable": 1,
        },
        "exception_review_candidate_count": 0,
        "exception_review_call_count": 0,
        "exception_review_confirm_count": 0,
        "exception_review_revise_count": 0,
        "exception_review_failed_count": 0,
        "exception_review_llm_elapsed_ms": 0,
        "exception_review_wall_elapsed_ms": 0,
        "main_wall_elapsed_ms": result["stats"]["main_wall_elapsed_ms"],
        "total_llm_elapsed_ms": result["stats"]["llm_elapsed_ms"],
        "total_llm_calls": result["stats"]["llm_total_calls"],
        "total_wall_elapsed_ms": result["stats"]["total_elapsed_ms"],
        "llm_elapsed_ms": result["stats"]["llm_elapsed_ms"],
        "total_elapsed_ms": result["stats"]["total_elapsed_ms"],
    }
    assert result["stats"]["llm_elapsed_ms"] >= 0
    assert result["stats"]["total_elapsed_ms"] >= 0
    assert not any(
        item["template_name"] in {"候选模板", "未匹配模板"}
        for item in result["template_text_reviews"]
    )


def test_template_review_failures_are_recorded_without_aborting_other_calls(
    tmp_path: Path,
):
    templates, document = _full_review_fixture()
    recorder = ComplianceExtractionRecorder(tmp_path / "task")
    llm = ConcurrentReviewLLM(
        responses=_full_review_responses(),
        failure_modes={
            "扩展模板D": "exception",
            "扩展模板E": "invalid_json",
        },
    )

    result = run_template_text_review(
        {"templates": templates},
        {"structured_document": document},
        llm=llm,
        recorder=recorder,
    )

    assert result["stats"]["llm_completed_calls"] == 4
    assert result["stats"]["llm_failed_count"] == 2
    output_files = sorted((recorder.artifact_dir / "llm").glob("call_*_output.json"))
    assert len(output_files) == 6
    output_payloads = [
        json.loads(path.read_text(encoding="utf-8")) for path in output_files
    ]
    assert sorted(payload["status"] for payload in output_payloads) == [
        "failed",
        "failed",
        "success",
        "success",
        "success",
        "success",
    ]
    failed_payloads = [
        payload for payload in output_payloads if payload["status"] == "failed"
    ]
    assert {payload["error_type"] for payload in failed_payloads} == {
        "RuntimeError",
        "JSONDecodeError",
    }


def _fault_injection_template() -> dict[str, Any]:
    return _template(
        "投标函",
        (
            "投标函\n"
            "固定正文：我方确认已阅读并理解招标文件全部内容。\n"
            "投标人名称：____\n"
            "日期：____\n"
            "固定承诺：我方承诺在投标有效期内不修改、撤销投标文件。"
        ),
        ["投标人名称", "日期"],
    )


def _fault_injection_document() -> dict[str, Any]:
    return {
        "sections": [
            {
                "section_id": "s-fault-tender-letter",
                "parent_section_id": None,
                "title": "1 投标函",
                "path": ["1 投标函"],
                "start_order": 1,
                "direct_block_ids": ["b-fault-heading", "b-fault-content"],
                "block_ids": ["b-fault-heading", "b-fault-content"],
            }
        ],
        "blocks": [
            {
                "block_id": "b-fault-heading",
                "type": "heading",
                "text": "1 投标函",
                "order": 1,
            },
            {
                "block_id": "b-fault-content",
                "type": "paragraph",
                "text": (
                    "投标人名称：示例公司\n"
                    "日期：2026年4月17日\n"
                    "固定正文：我方确认已阅读并理解招标文件全部内容。\n"
                    "固定承诺：我方承诺在投标有效期内不修改、撤销投标文件。"
                ),
                "order": 2,
            },
        ],
        "tables": [],
        "images": [],
    }


class FaultInjectionFixtureLLM:
    model = "fault-injection-fixture"

    def __init__(self, fault: str):
        self.fault = fault
        self.calls: list[tuple[str, str]] = []
        self.original_user_prompts: list[str] = []

    def review_template(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        start_marker = "<<<BID_MODULE\n"
        end_marker = "\nBID_MODULE"
        start = user_prompt.index(start_marker) + len(start_marker)
        end = user_prompt.index(end_marker, start)
        bid_module_content = user_prompt[start:end]
        injected_content = _inject_fault(bid_module_content, self.fault)
        injected_prompt = user_prompt[:start] + injected_content + user_prompt[end:]
        self.original_user_prompts.append(user_prompt)
        self.calls.append((system_prompt, injected_prompt))

        if "日期：2026年4月17日" not in injected_content:
            return {
                "status": "fail",
                "summary": "投标函日期填写缺失。",
                "semantic_match": {
                    "status": "matched",
                    "reason": "模块用途与投标函模板对应。",
                },
                "issues": [
                    {
                        "type": "missing_fill",
                        "requirement": "投标函应填写日期。",
                        "actual": "实际投标模块中未出现日期填写内容。",
                        "reason": "日期填写项在实际模块中被明确删除。",
                    }
                ],
            }
        if "我方可以随时撤销投标文件。" in injected_content:
            return {
                "status": "fail",
                "summary": "投标函固定承诺含义被实质性改变。",
                "semantic_match": {
                    "status": "matched",
                    "reason": "模块用途与投标函模板对应。",
                },
                "issues": [
                    {
                        "type": "substantive_change",
                        "requirement": "不得修改投标有效期内不修改、撤销投标文件的固定承诺。",
                        "actual": "实际模块改为我方可以随时撤销投标文件。",
                        "reason": "实际文本将不得撤销改为可以随时撤销，改变了承诺原意。",
                    }
                ],
            }
        if "我方承诺在投标有效期内不修改、撤销投标文件。" not in injected_content:
            return {
                "status": "fail",
                "summary": "投标函固定承诺正文缺失。",
                "semantic_match": {
                    "status": "matched",
                    "reason": "模块用途与投标函模板对应。",
                },
                "issues": [
                    {
                        "type": "missing_content",
                        "requirement": "应保留我方在投标有效期内不修改、撤销投标文件的固定承诺。",
                        "actual": "实际投标模块中缺少该固定承诺正文。",
                        "reason": "模板要求的固定承诺在实际模块中不存在。",
                    }
                ],
            }
        if "固定正文：我方确认已阅读并理解招标文件全部内容。" not in injected_content:
            return {
                "status": "fail",
                "summary": "投标函固定正文缺失。",
                "semantic_match": {
                    "status": "matched",
                    "reason": "模块用途与投标函模板对应。",
                },
                "issues": [
                    {
                        "type": "missing_content",
                        "requirement": "应保留确认已阅读并理解招标文件全部内容的固定正文。",
                        "actual": "实际投标模块中缺少该固定正文。",
                        "reason": "模板要求的固定正文在实际模块中不存在。",
                    }
                ],
            }
        raise AssertionError(f"未识别的故障注入：{self.fault}")


def _inject_fault(bid_module_content: str, fault: str) -> str:
    if fault == "delete_date":
        return bid_module_content.replace("日期：2026年4月17日\n", "")
    if fault == "delete_fixed_body":
        return bid_module_content.replace(
            "固定正文：我方确认已阅读并理解招标文件全部内容。\n", ""
        )
    if fault == "change_fixed_commitment":
        return bid_module_content.replace(
            "固定承诺：我方承诺在投标有效期内不修改、撤销投标文件。",
            "固定承诺：我方可以随时撤销投标文件。",
        )
    raise AssertionError(f"未知的故障注入：{fault}")


@pytest.mark.parametrize(
    ("fault", "expected_issue_type"),
    [
        ("delete_date", "missing_fill"),
        ("delete_fixed_body", "missing_content"),
        ("change_fixed_commitment", "substantive_change"),
    ],
)
def test_fault_injected_bid_module_returns_expected_template_text_issue(
    fault: str,
    expected_issue_type: str,
):
    document = _fault_injection_document()
    original_document = copy.deepcopy(document)
    llm = FaultInjectionFixtureLLM(fault)

    result = run_template_text_review(
        {"templates": [_fault_injection_template()]},
        {"structured_document": document},
        llm=llm,
    )

    review = result["template_text_reviews"][0]
    assert review["status"] == "fail"
    assert review["issues"][0]["type"] == expected_issue_type
    assert result["stats"]["llm_total_calls"] == 1
    assert result["stats"]["llm_completed_calls"] == 1
    assert review["llm_elapsed_ms"] >= 0
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    original_prompt = llm.original_user_prompts[0]
    injected_prompt = llm.calls[0][1]
    start_marker = "<<<BID_MODULE\n"
    end_marker = "\nBID_MODULE"
    start = original_prompt.index(start_marker) + len(start_marker)
    end = original_prompt.index(end_marker, start)
    assert injected_prompt[:start] == original_prompt[:start]
    injected_end = injected_prompt.index(end_marker, start)
    assert injected_prompt[injected_end:] == original_prompt[end:]
    assert injected_prompt[start:injected_end] == _inject_fault(
        original_prompt[start:end], fault
    )
    assert document == original_document


def test_review_calls_once_per_selected_template_with_complete_text_inputs():
    llm = RecordingReviewLLM()

    result = run_template_text_review(
        {"templates": _templates()},
        {"structured_document": _structured_bid_document()},
        llm=llm,
    )

    assert len(llm.calls) == 3
    assert [item["template_name"] for item in result["template_text_reviews"]] == list(
        TARGET_NAMES
    )
    assert [item["status"] for item in result["template_text_reviews"]] == [
        "pass",
        "fail",
        "uncertain",
    ]
    assert all(system_prompt == TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT for system_prompt, _ in llm.calls)

    prompts_by_template = {
        name: prompt
        for name in TARGET_NAMES
        for system_prompt, prompt in llm.calls
        if (f"模板名称：\n{name}" in prompt)
    }
    bid_letter_prompt = prompts_by_template["投标函"]
    assert "固定承诺：不得删除。" in bid_letter_prompt
    assert "投标人名称：示例公司" in bid_letter_prompt
    assert "廉洁投标承诺书" not in bid_letter_prompt

    authority_prompt = prompts_by_template["法定代表人/负责人授权委托书"]
    assert "本人[投标人法定代表人/负责人姓名]系示例公司的负责人。" in authority_prompt
    assert "[图片，此轮模板文本检查不判断图片内容]" in authority_prompt

    consortium_prompt = prompts_by_template["联合体协议书"]
    assert "职责 | 分工" in consortium_prompt
    assert "项目实施 | 不适用" in consortium_prompt
    assert "本项目不适用" in consortium_prompt


def test_review_persists_prompts_raw_response_and_structured_result(tmp_path: Path):
    recorder = ComplianceExtractionRecorder(tmp_path / "task")
    llm = RecordingReviewLLM()

    result = run_template_text_review(
        {"templates": _templates()[:3]},
        {"structured_document": _structured_bid_document()},
        llm=llm,
        recorder=recorder,
    )

    assert result["stats"]["llm_total_calls"] == 3
    input_files = sorted((recorder.artifact_dir / "llm").glob("call_*_input.json"))
    output_files = sorted((recorder.artifact_dir / "llm").glob("call_*_output.json"))
    assert len(input_files) == len(output_files) == 3

    input_payloads = [
        json.loads(path.read_text(encoding="utf-8")) for path in input_files
    ]
    input_payload = next(
        payload
        for payload in input_payloads
        if payload["batch"]["template_name"] == "投标函"
    )
    assert input_payload["request_payload"]["messages"][0]["content"] == (
        TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT
    )
    assert "固定承诺：不得删除。" in input_payload["request_payload"]["messages"][1]["content"]

    output_payloads = [
        json.loads(path.read_text(encoding="utf-8")) for path in output_files
    ]
    output_payload = next(
        payload
        for payload in output_payloads
        if payload["batch"]["template_name"] == "投标函"
    )
    assert output_payload["status"] == "success"
    assert output_payload["raw_response"] == {
        "status": "pass",
        "summary": "投标函文本完整响应。",
        "issues": [],
        "semantic_match": {
            "status": "matched",
            "reason": "文件用途、填写对象和核心内容均与模板对应。",
        },
    }
    assert output_payload["parsed_objects"] == result["template_text_reviews"][0]


def test_review_skips_template_when_match_is_not_unique():
    llm = RecordingReviewLLM()
    document = _structured_bid_document()
    document["sections"].append(
        {
            "section_id": "s-authority-duplicate",
            "parent_section_id": None,
            "title": "9 法定代表人/负责人授权委托书",
            "start_order": 9,
            "direct_block_ids": [],
            "block_ids": [],
        }
    )

    result = run_template_text_review(
        {"templates": _templates()[:3]},
        {"structured_document": document},
        llm=llm,
    )

    assert [
        item["template_name"] for item in result["template_text_reviews"]
    ] == ["投标函", "联合体协议书"]
    assert len(llm.calls) == 2
    assert result["stats"]["matched_template_count"] == 2


def test_review_skips_matched_section_without_reliable_text_content():
    llm = RecordingReviewLLM()
    document = {
        "sections": [
            {
                "section_id": "s-empty-module",
                "parent_section_id": None,
                "title": "1 空模块模板",
                "start_order": 1,
                "direct_block_ids": ["b-empty-heading", "b-empty-image"],
                "block_ids": ["b-empty-heading", "b-empty-image"],
            }
        ],
        "blocks": [
            {
                "block_id": "b-empty-heading",
                "type": "heading",
                "text": "1 空模块模板",
                "order": 1,
            },
            {"block_id": "b-empty-image", "type": "image", "text": "", "order": 2},
        ],
        "tables": [],
        "images": [],
    }

    result = run_template_text_review(
        {"templates": [_template("空模块模板", "空模块模板\n正文", [])]},
        {"structured_document": document},
        llm=llm,
    )

    assert result["stats"]["matched_template_count"] == 1
    assert result["stats"]["llm_total_calls"] == 0
    assert result["template_text_reviews"] == []
    assert llm.calls == []


def test_review_skips_matched_section_with_only_empty_table_cells():
    llm = RecordingReviewLLM()
    document = {
        "sections": [
            {
                "section_id": "s-empty-table-module",
                "parent_section_id": None,
                "title": "1 空表格模板",
                "start_order": 1,
                "direct_block_ids": ["b-empty-table"],
                "block_ids": ["b-empty-table"],
            }
        ],
        "blocks": [
            {
                "block_id": "b-empty-table",
                "type": "table",
                "text": "表格",
                "order": 1,
            }
        ],
        "tables": [
            {
                "block_id": "b-empty-table",
                "rows": [["", "  "], ["\t", "\n"]],
            }
        ],
        "images": [],
    }

    result = run_template_text_review(
        {"templates": [_template("空表格模板", "空表格模板\n填写内容", [])]},
        {"structured_document": document},
        llm=llm,
    )

    assert result["stats"]["matched_template_count"] == 1
    assert result["stats"]["llm_total_calls"] == 0
    assert result["template_text_reviews"] == []
    assert llm.calls == []


def test_review_uses_table_block_text_when_rows_are_missing():
    class TableTextReviewLLM:
        model = "table-text-fixture-model"

        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def review_template(self, system_prompt: str, user_prompt: str) -> Any:
            self.calls.append((system_prompt, user_prompt))
            return {
                "status": "pass",
                "summary": "表格文本已提供。",
                "issues": [],
                "semantic_match": {
                    "status": "matched",
                    "reason": "模块用途与表格模板对应。",
                },
            }

    llm = TableTextReviewLLM()
    document = {
        "sections": [
            {
                "section_id": "s-table-text-module",
                "parent_section_id": None,
                "title": "1 表格文本模板",
                "start_order": 1,
                "direct_block_ids": ["b-table-text"],
                "block_ids": ["b-table-text"],
            }
        ],
        "blocks": [
            {
                "block_id": "b-table-text",
                "type": "table",
                "text": "<table><tr><td>实际表格正文</td></tr></table>",
                "order": 1,
            }
        ],
        "tables": [],
        "images": [],
    }

    result = run_template_text_review(
        {"templates": [_template("表格文本模板", "表格文本模板\n填写内容", [])]},
        {"structured_document": document},
        llm=llm,
    )

    assert result["stats"]["llm_total_calls"] == 1
    assert result["template_text_reviews"][0]["status"] == "pass"
    assert "实际表格正文" in llm.calls[0][1]


def test_openai_compatible_client_sends_template_review_prompts(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "status": "pass",
                                        "summary": "文本完整。",
                                        "issues": [],
                                        "semantic_match": {
                                            "status": "matched",
                                            "reason": "模块用途与模板对应。",
                                        },
                                    },
                                    ensure_ascii=False,
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 12},
                },
                ensure_ascii=False,
            ).encode("utf-8")

    request_payloads: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout):
        request_payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    llm = OpenAICompatibleLLM(
        api_key="test-key",
        base_url="https://llm.example/v1",
        model="test-model",
    )

    result = llm.review_template(
        TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT,
        "实际用户提示词",
    )

    assert result == {
        "status": "pass",
        "summary": "文本完整。",
        "issues": [],
        "semantic_match": {
            "status": "matched",
            "reason": "模块用途与模板对应。",
        },
    }
    assert request_payloads == [
        {
            "model": "test-model",
            "temperature": 0,
            "enable_thinking": False,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": "实际用户提示词"},
            ],
        }
    ]


def test_default_workflow_uses_template_text_review_service(settings, repository):
    workflow = build_default_workflow(settings, repository)
    try:
        result = workflow.services.review(
            {"templates": _templates()[:3]},
            {"structured_document": _structured_bid_document()},
        )
    finally:
        workflow.shutdown()

    assert result["mode"] == "template_text"
    assert len(result["template_text_reviews"]) == 3
    assert result["stats"]["llm_total_calls"] == 3
    assert all(
        item["status"] == "uncertain"
        for item in result["template_text_reviews"]
    )
    assert "attachment_reviews" not in result
