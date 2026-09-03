from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from app.api import build_default_workflow
from app.attachment_review import (
    ATTACHMENT_REVIEW_SYSTEM_PROMPT,
    OpenAICompatibleAttachmentReviewLLM,
    build_attachment_review_user_prompt,
    extract_attachment_requirements,
    is_complex_attachment_scope,
    run_attachment_review,
    run_compliance_review_with_attachments,
    template_has_attachment_requirement,
)
from app.compliance_artifacts import ComplianceExtractionRecorder


class RecordingAttachmentLLM:
    model = "qwen3.8-27b"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []

    def review_attachment(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt, images))
        image_ids = [str(image["image_id"]) for image in images]
        return {
            "status": "pass",
            "summary": "当前证明材料能够支持模板要求。",
            "semantic_match": {
                "status": "matched",
                "reason": "当前 requirement 明确要求投标时提供独立身份证明材料。",
            },
            "materials": [
                {
                    "material_type": "居民身份证",
                    "image_ids": image_ids,
                    "facts": [
                        {
                            "name": "身份证人像面",
                            "status": "present",
                            "evidence_image_ids": image_ids[:1],
                        }
                    ],
                }
            ],
            "requirements": [
                {
                    "requirement": "投标文件应提供当前证明材料。",
                    "status": "pass",
                    "evidence_image_ids": image_ids,
                    "reason": "图片中存在能够支持该要求的材料。",
                }
            ],
        }


class FixedSemanticAttachmentLLM(RecordingAttachmentLLM):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__()
        self.response = response

    def review_attachment(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt, images))
        return copy.deepcopy(self.response)


def _template(name: str, body: str) -> dict[str, Any]:
    return {
        "id": f"tpl-{name}",
        "name": name,
        "body": body,
        "attachments": ["辅助附件提示"],
        "source": {"source_text": body},
    }


def _document() -> dict[str, Any]:
    return {
        "sections": [
            {
                "section_id": "s-id",
                "parent_section_id": None,
                "title": "1 法定代表人/负责人身份证明",
                "path": ["1 法定代表人/负责人身份证明"],
                "direct_block_ids": ["b-id-heading", "b-id-text", "b-id-image"],
            },
            {
                "section_id": "s-authority",
                "parent_section_id": None,
                "title": "2 法定代表人授权委托书",
                "path": ["2 法定代表人授权委托书"],
                "direct_block_ids": ["b-authority-heading", "b-authority-text", "b-authority-image"],
            },
            {
                "section_id": "s-bank",
                "parent_section_id": None,
                "title": "3 基本开户银行情况",
                "path": ["3 基本开户银行情况"],
                "direct_block_ids": ["b-bank-heading", "b-bank-text", "b-bank-table"],
            },
            {
                "section_id": "s-other",
                "parent_section_id": None,
                "title": "4 营业执照",
                "path": ["4 营业执照"],
                "direct_block_ids": ["b-other"],
            },
        ],
        "blocks": [
            {"block_id": "b-id-heading", "type": "heading", "text": "1 身份证明", "order": 1},
            {"block_id": "b-id-text", "type": "paragraph", "text": "法定代表人：张三。", "order": 2},
            {"block_id": "b-id-image", "type": "image", "text": "身份证", "order": 3},
            {"block_id": "b-authority-heading", "type": "heading", "text": "2 授权委托书", "order": 4},
            {"block_id": "b-authority-text", "type": "paragraph", "text": "委托代理人：李四。", "order": 5},
            {"block_id": "b-authority-image", "type": "image", "text": "代理人身份证", "order": 6},
            {"block_id": "b-bank-heading", "type": "heading", "text": "3 基本开户银行情况", "order": 7},
            {"block_id": "b-bank-text", "type": "paragraph", "text": "开户银行信息如下。", "order": 8},
            {"block_id": "b-bank-table", "type": "table", "text": "银行名称 | 账户名称 | 账号", "order": 9},
            {"block_id": "b-other", "type": "paragraph", "text": "营业执照内容。", "order": 10},
        ],
        "tables": [
            {
                "block_id": "b-bank-table",
                "rows": [["银行名称", "账户名称", "账号"], ["示例银行", "示例公司", "123"]],
            }
        ],
        "images": [
            {"image_id": "image-id", "block_id": "b-id-image", "img_path": "images/id.png", "asset_status": "ready"},
            {"image_id": "image-authority", "block_id": "b-authority-image", "img_path": "images/authority.png", "asset_status": "ready"},
        ],
    }


def test_attachment_candidate_uses_full_template_body_when_attachments_are_empty():
    template = _template("软件配置", "应提供相关软件的合法使用权证明。")
    template["attachments"] = []

    assert template_has_attachment_requirement(template) is True


def test_attachment_candidate_does_not_use_attachment_placeholder_alone():
    template = _template("投标函", "本页填写投标函正文。")
    template["attachments"] = ["身份证复印件"]

    assert template_has_attachment_requirement(template) is False


def test_conditional_attachment_requirement_is_a_candidate_without_local_fail_rule():
    template = _template(
        "软件说明",
        "如采用第三方软件，应提供合法使用权证明；不涉及则无需提供。",
    )

    assert template_has_attachment_requirement(template) is True


def test_attachment_candidate_accepts_explicit_scanned_material_without_action_verb():
    template = _template(
        "资格审查资料",
        "营业执照正本或副本、事业单位法人证书或扫描件。",
    )

    assert template_has_attachment_requirement(template) is True


def test_attachment_candidate_rejects_future_commitment_material():
    template = _template(
        "网络及信息安全承诺书",
        "本单位承诺中标后提供相关证明材料，发生问题时再提交书面说明。",
    )

    assert extract_attachment_requirements(template) == []
    assert template_has_attachment_requirement(template) is False


def test_attachment_candidate_rejects_stamp_scan_without_proof_material():
    template = _template(
        "投标专用印章授权函",
        "本文件仅适用于要求递交加盖印章扫描件的情形。",
    )

    assert extract_attachment_requirements(template) == []
    assert template_has_attachment_requirement(template) is False


def test_attachment_scope_keeps_conditional_proof_and_excludes_table_fields():
    template = _template(
        "主要元器件来源清单",
        "主要元器件来源清单（如有）。"
        "<table>CPU、GPU、型号、技术规格、供应商、生产地</table>"
        "代理商投标的，本文件应由有签署权的代表签字确认，并提供委托签署权的相关证明材料。",
    )

    scope = extract_attachment_requirements(template)

    assert len(scope) == 1
    assert "委托签署权" in scope[0]
    assert "CPU" not in scope[0]
    assert "签字" not in scope[0]


def test_attachment_prompt_states_independent_material_boundary():
    template = _template(
        "资格审查资料",
        "营业执照正本或副本、事业单位法人证书或扫描件。"
        "不得存在情形承诺函正文由投标人填写。",
    )

    prompt = build_attachment_review_user_prompt(
        template,
        bid_module_name="13 资格审查资料",
        bid_module_content="营业执照扫描件。承诺函正文。",
        images=[],
    )

    assert "独立证明材料" in prompt
    assert "不能把模板正文、表格填写或承诺函文本" in prompt
    assert "签字或盖章" in prompt
    assert "营业执照正本或副本、事业单位法人证书或扫描件" in prompt


def test_attachment_semantic_mismatch_skips_business_conclusion():
    llm = FixedSemanticAttachmentLLM(
        {
            "status": "fail",
            "summary": "不应被采用的附件缺失结论。",
            "semantic_match": {
                "status": "mismatched",
                "reason": "当前候选只是模板正文中的承诺函要求，不是独立证明材料。",
            },
            "materials": [
                {
                    "material_type": "居民身份证",
                    "image_ids": ["image-id"],
                    "facts": [],
                }
            ],
            "requirements": [
                {
                    "requirement": "附：法定代表人的身份证明复印件。",
                    "status": "fail",
                    "evidence_image_ids": ["not-a-real-image"],
                    "reason": "图片中没有材料。",
                }
            ],
        }
    )

    result = run_attachment_review(
        {"templates": [_template("法定代表人/负责人身份证明", "应提供身份证明复印件。 ")]},
        {"structured_document": _document()},
        llm=llm,
    )
    review = result["attachment_reviews"][0]

    assert review["semantic_match"]["status"] == "mismatched"
    assert review["business_status"] == "not_run"
    assert review["execution_status"] == "semantic_skipped"
    assert review["materials"] == []
    assert review["requirements"] == []
    assert review["status"] != "fail"
    assert result["stats"]["semantic_mismatched_count"] == 1
    assert result["stats"]["fail_count"] == 0


def test_attachment_semantic_uncertain_does_not_become_missing_material_fail():
    llm = FixedSemanticAttachmentLLM(
        {
            "status": "fail",
            "summary": "当前证据不足。",
            "semantic_match": {
                "status": "uncertain",
                "reason": "当前识别到的文本不足以确认是独立附件要求。",
            },
            "materials": [],
            "requirements": [
                {
                    "requirement": "附：法定代表人的身份证明复印件。",
                    "status": "fail",
                    "evidence_image_ids": [],
                    "reason": "没有图片。",
                }
            ],
        }
    )

    result = run_attachment_review(
        {"templates": [_template("法定代表人/负责人身份证明", "应提供身份证明复印件。 ")]},
        {"structured_document": _document()},
        llm=llm,
    )
    review = result["attachment_reviews"][0]

    assert review["semantic_match"]["status"] == "uncertain"
    assert review["business_status"] == "not_run"
    assert review["execution_status"] == "semantic_skipped"
    assert result["stats"]["semantic_uncertain_count"] == 1
    assert result["stats"]["fail_count"] == 0


def test_attachment_prompt_requires_independent_material_semantic_gate():
    prompt = build_attachment_review_user_prompt(
        _template("资格审查资料", "应提供营业执照复印件。"),
        bid_module_name="13 资格审查资料",
        bid_module_content="营业执照材料。",
        images=[],
    )

    assert "先确认" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "独立证明材料要求" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "模板正文" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "未来履约义务" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "semantic_match" in prompt


def test_attachment_prompt_limits_matched_reason_and_visual_facts():
    prompt = build_attachment_review_user_prompt(
        _template("基本开户银行情况", "应提供基本账户开户证明。"),
        bid_module_name="14 基本开户银行情况",
        bid_module_content="基本账户开户证明。",
        images=[],
    )

    assert "semantic_match.status=matched 时，reason 使用极短描述" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "不再复述招标模板条款、模板名称或投标模块内容" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "materials.facts 只提取" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "用于确认这是什么材料的事实" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "用于确认材料必要组成部分是否完整的事实" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "后续跨材料一致性检查有明确复用价值的核心身份信息" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "普通签字、公章等" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "不主动提取" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "semantic_match=matched 时 reason 只返回极短说明" in prompt
    assert "materials.facts 只提取与材料识别、完整性及后续主体一致性有价值的核心事实" in prompt
    assert "不主动检查普通签字盖章" in prompt


def test_attachment_prompt_requires_requirement_status_aggregation():
    assert len(ATTACHMENT_REVIEW_SYSTEM_PROMPT) <= 2300
    assert "最终 status 必须严格根据 requirements 聚合" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "任意 requirement.status = fail" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "没有 fail，但存在任意 requirement.status = uncertain" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "所有实际 requirement.status = pass" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "禁止出现 overall status = pass 但 requirements 中仍然存在 uncertain" in ATTACHMENT_REVIEW_SYSTEM_PROMPT
    assert "没有形成任何真实独立证明材料 requirement" in ATTACHMENT_REVIEW_SYSTEM_PROMPT


class InconsistentAttachmentLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        return {
            "status": "pass",
            "summary": "模型顶层错误返回 pass。",
            "semantic_match": {
                "status": "matched",
                "reason": "当前 requirement 属于独立证明材料要求。",
            },
            "materials": [],
            "requirements": [
                {
                    "requirement": "如有条件性证明材料，应提供。",
                    "status": "uncertain",
                    "evidence_image_ids": [],
                    "reason": "当前输入无法确认条件是否触发。",
                }
            ],
        }


class ConditionalPassAttachmentLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        return {
            "status": "pass",
            "summary": "模型错误地将条件性材料判定为通过。",
            "semantic_match": {
                "status": "matched",
                "reason": "当前 requirement 属于独立证明材料要求。",
            },
            "materials": [],
            "requirements": [
                {
                    "requirement": "如采用第三方软件，应提供合法使用权证明。",
                    "status": "pass",
                    "evidence_image_ids": [],
                    "reason": "投标模块标注为无。",
                }
            ],
        }


def test_conditional_attachment_cannot_pass_from_unverified_no_declaration(
    tmp_path: Path,
):
    result = run_attachment_review(
        {
            "templates": [
                _template("软件配置", "如采用第三方软件，应提供合法使用权证明。")
            ]
        },
        {
            "structured_document": {
                "sections": [
                    {
                        "section_id": "s-software",
                        "parent_section_id": None,
                        "title": "1 软件配置（无）",
                        "path": ["1 软件配置（无）"],
                        "direct_block_ids": ["b-software"],
                    }
                ],
                "blocks": [
                    {
                        "block_id": "b-software",
                        "type": "paragraph",
                        "text": "本模块标注：无。",
                        "order": 1,
                    }
                ],
                "tables": [],
                "images": [],
            },
            "artifact_dir": str(tmp_path),
        },
        llm=ConditionalPassAttachmentLLM(),
    )

    review = result["attachment_reviews"][0]
    assert review["status"] == "uncertain"
    assert review["requirements"][0]["status"] == "uncertain"
    assert "适用条件" in review["requirements"][0]["reason"]


class EmptyRequirementsAttachmentLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        return {
            "status": "uncertain",
            "summary": "当前未形成附件 requirement 结论。",
            "semantic_match": {
                "status": "matched",
                "reason": "当前 requirement 属于独立证明材料要求。",
            },
            "materials": [],
            "requirements": [],
        }


def test_empty_model_requirements_are_materialized_as_uncertain_for_explicit_scope(
    tmp_path: Path,
):
    result = run_attachment_review(
        {"templates": [_template("软件配置", "如有，应提供合法使用权证明。")]},
        {
            "structured_document": {
                "sections": [
                    {
                        "section_id": "s-software",
                        "parent_section_id": None,
                        "title": "1 软件配置",
                        "path": ["1 软件配置"],
                        "direct_block_ids": ["b-software"],
                    }
                ],
                "blocks": [
                    {
                        "block_id": "b-software",
                        "type": "paragraph",
                        "text": "软件配置。",
                        "order": 1,
                    }
                ],
                "tables": [],
                "images": [],
            },
            "artifact_dir": str(tmp_path),
        },
        llm=EmptyRequirementsAttachmentLLM(),
    )

    review = result["attachment_reviews"][0]
    assert review["status"] == "uncertain"
    assert len(review["requirements"]) == 1
    assert review["requirements"][0]["status"] == "uncertain"
    assert "如有，应提供合法使用权证明" in review["requirements"][0]["requirement"]


def test_overall_attachment_status_is_aggregated_from_requirements(tmp_path: Path):
    result = run_attachment_review(
        {"templates": [_template("软件配置", "如采用第三方软件，应提供合法使用权证明。")]},
        {
            "structured_document": {
                "sections": [
                    {
                        "section_id": "s-software",
                        "parent_section_id": None,
                        "title": "1 软件配置",
                        "path": ["1 软件配置"],
                        "direct_block_ids": ["b-software"],
                    }
                ],
                "blocks": [
                    {
                        "block_id": "b-software",
                        "type": "paragraph",
                        "text": "软件配置标注为无。",
                        "order": 1,
                    }
                ],
                "tables": [],
                "images": [],
            },
            "artifact_dir": str(tmp_path),
        },
        llm=InconsistentAttachmentLLM(),
    )

    assert result["attachment_reviews"][0]["status"] == "uncertain"
    assert result["stats"]["uncertain_count"] == 1


class MixedScopeAttachmentLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        return {
            "status": "pass",
            "summary": "包含一个证明材料要求和两个越界判断。",
            "semantic_match": {
                "status": "matched",
                "reason": "当前 requirement 范围属于独立证明材料要求。",
            },
            "materials": [],
            "requirements": [
                {
                    "requirement": "应提供营业执照扫描件。",
                    "status": "pass",
                    "evidence_image_ids": [],
                    "reason": "模板明确要求该证明材料。",
                },
                {
                    "requirement": "承诺函正文已经填写。",
                    "status": "fail",
                    "evidence_image_ids": [],
                    "reason": "这是模板文本检查内容。",
                },
                {
                    "requirement": "承诺函需加盖单位公章。",
                    "status": "uncertain",
                    "evidence_image_ids": [],
                    "reason": "当前没有盖章图片。",
                },
            ],
        }


def test_model_requirements_are_limited_to_independent_material_scope(tmp_path: Path):
    result = run_attachment_review(
        {"templates": [_template("营业执照材料", "应提供营业执照扫描件。") ]},
        {
            "structured_document": {
                "sections": [
                    {
                        "section_id": "s-license",
                        "parent_section_id": None,
                        "title": "1 营业执照材料",
                        "path": ["1 营业执照材料"],
                        "direct_block_ids": ["b-license"],
                    }
                ],
                "blocks": [
                    {
                        "block_id": "b-license",
                        "type": "paragraph",
                        "text": "营业执照材料。",
                        "order": 1,
                    }
                ],
                "tables": [],
                "images": [],
            },
            "artifact_dir": str(tmp_path),
        },
        llm=MixedScopeAttachmentLLM(),
    )

    review = result["attachment_reviews"][0]
    assert [item["requirement"] for item in review["requirements"]] == [
        "应提供营业执照扫描件。"
    ]
    assert review["status"] == "pass"


class FailAndUncertainAttachmentLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        return {
            "status": "uncertain",
            "summary": "同时存在明确缺失和无法确认。",
            "semantic_match": {
                "status": "matched",
                "reason": "当前 requirement 范围属于独立证明材料要求。",
            },
            "materials": [],
            "requirements": [
                {
                    "requirement": "应提供营业执照扫描件。",
                    "status": "uncertain",
                    "evidence_image_ids": [],
                    "reason": "图片无法确认。",
                },
                {
                    "requirement": "应提供营业执照副本扫描件。",
                    "status": "fail",
                    "evidence_image_ids": [],
                    "reason": "明确没有提供。",
                },
            ],
        }


def test_requirement_fail_takes_precedence_over_uncertain(tmp_path: Path):
    result = run_attachment_review(
        {"templates": [_template("营业执照材料", "应提供营业执照扫描件。") ]},
        {
            "structured_document": {
                "sections": [
                    {
                        "section_id": "s-license",
                        "parent_section_id": None,
                        "title": "1 营业执照材料",
                        "path": ["1 营业执照材料"],
                        "direct_block_ids": ["b-license"],
                    }
                ],
                "blocks": [
                    {
                        "block_id": "b-license",
                        "type": "paragraph",
                        "text": "营业执照材料。",
                        "order": 1,
                    }
                ],
                "tables": [],
                "images": [],
            },
            "artifact_dir": str(tmp_path),
        },
        llm=FailAndUncertainAttachmentLLM(),
    )

    assert result["attachment_reviews"][0]["status"] == "fail"


class NoScopedRequirementLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        return {
            "status": "pass",
            "summary": "仅返回文本填写判断。",
            "semantic_match": {
                "status": "matched",
                "reason": "当前 requirement 范围属于独立证明材料要求。",
            },
            "materials": [],
            "requirements": [
                {
                    "requirement": "承诺函正文已填写。",
                    "status": "pass",
                    "evidence_image_ids": [],
                    "reason": "这是模板文本内容。",
                }
            ],
        }


def test_no_scoped_attachment_requirement_does_not_create_review(tmp_path: Path):
    result = run_attachment_review(
        {"templates": [_template("营业执照材料", "本模块填写承诺函正文。") ]},
        {
            "structured_document": {
                "sections": [
                    {
                        "section_id": "s-license",
                        "parent_section_id": None,
                        "title": "1 营业执照材料",
                        "path": ["1 营业执照材料"],
                        "direct_block_ids": ["b-license"],
                    }
                ],
                "blocks": [
                    {
                        "block_id": "b-license",
                        "type": "paragraph",
                        "text": "营业执照材料。",
                        "order": 1,
                    }
                ],
                "tables": [],
                "images": [],
            },
            "artifact_dir": str(tmp_path),
        },
        llm=NoScopedRequirementLLM(),
    )

    assert result["attachment_reviews"] == []
    assert result["stats"]["selected_template_count"] == 0
    assert result["stats"]["llm_total_calls"] == 0


def test_21_and_21_x_are_excluded_from_attachment_jobs():
    assert is_complex_attachment_scope({}, {"title": "21 业绩情况表"}) is True
    assert is_complex_attachment_scope({}, {"title": "21.3 合同关键页"}) is True
    assert is_complex_attachment_scope({}, {"title": "20 基本开户银行情况"}) is False


def _all_ordinary_attachment_document() -> dict[str, Any]:
    module_names = [
        ("s-license", "b-license", "4 营业执照材料", "营业执照模块内容。"),
        ("s-software", "b-software", "5 软件使用权说明", "软件使用情况说明。"),
        ("s-bank-proof", "b-bank-proof", "6 开户证明材料", "开户证明模块内容。"),
        ("s-bid-letter", "b-bid-letter", "7 投标函", "投标函正文。"),
        ("s-performance", "b-performance", "21 业绩情况表", "业绩表正文。"),
        ("s-contract", "b-contract", "21.3 合同关键页", "合同关键页正文。"),
    ]
    return {
        "sections": [
            {
                "section_id": section_id,
                "parent_section_id": None,
                "title": title,
                "path": [title],
                "direct_block_ids": [block_id],
            }
            for section_id, block_id, title, _text in module_names
        ],
        "blocks": [
            {
                "block_id": block_id,
                "type": "paragraph",
                "text": text,
                "order": index + 1,
            }
            for index, (_section_id, block_id, _title, text) in enumerate(module_names)
        ],
        "tables": [],
        "images": [],
    }


def _all_ordinary_attachment_templates() -> list[dict[str, Any]]:
    return [
        _template("营业执照材料", "应提供营业执照复印件。"),
        _template(
            "软件使用权说明",
            "如采用第三方软件，应提供合法使用权证明；不涉及则无需提供。",
        ),
        _template("开户证明材料", "应提供基本账户开户证明。"),
        _template("投标函", "本页填写投标函正文。"),
        _template("业绩情况表", "应提供业绩合同复印件。"),
        _template("合同关键页", "应提供合同关键页复印件。"),
    ]


class FailOneAttachmentLLM(RecordingAttachmentLLM):
    def __init__(self, failing_template_name: str) -> None:
        super().__init__()
        self.failing_template_name = failing_template_name

    def review_attachment(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.failing_template_name in user_prompt:
            raise RuntimeError("simulated attachment failure")
        return super().review_attachment(system_prompt, user_prompt, images)


def test_runner_checks_all_matched_ordinary_templates_in_template_order(tmp_path: Path):
    templates = _all_ordinary_attachment_templates()
    llm = RecordingAttachmentLLM()

    result = run_attachment_review(
        {"templates": templates},
        {
            "structured_document": _all_ordinary_attachment_document(),
            "artifact_dir": str(tmp_path),
        },
        llm=llm,
    )

    assert [item["template_name"] for item in result["attachment_reviews"]] == [
        "营业执照材料",
        "软件使用权说明",
        "开户证明材料",
    ]
    assert result["stats"]["selected_template_count"] == 3
    assert result["stats"]["code_candidate_count"] == 3
    assert result["stats"]["semantic_matched_count"] == 3
    assert result["stats"]["semantic_mismatched_count"] == 0
    assert result["stats"]["semantic_uncertain_count"] == 0
    assert result["stats"]["confirmed_requirement_count"] == 3
    assert result["stats"]["requirements_without_evidence_count"] == 3
    assert result["stats"]["no_bid_candidate_template_ids"] == []
    assert len(llm.calls) == 3
    conditional_call = next(
        call for call in llm.calls if "软件使用权说明" in call[1]
    )
    assert "不涉及则无需提供" in conditional_call[1]
    assert conditional_call[2] == []


def test_runner_keeps_one_model_failure_isolated_from_other_attachment_jobs(tmp_path: Path):
    result = run_attachment_review(
        {"templates": _all_ordinary_attachment_templates()},
        {
            "structured_document": _all_ordinary_attachment_document(),
            "artifact_dir": str(tmp_path),
        },
        llm=FailOneAttachmentLLM("营业执照材料"),
    )

    assert len(result["attachment_reviews"]) == 3
    failed = next(
        item
        for item in result["attachment_reviews"]
        if item["template_name"] == "营业执照材料"
    )
    assert failed["execution_status"] == "failed"
    assert result["stats"]["llm_failed_count"] == 1
    assert result["stats"]["llm_completed_calls"] == 2


class MissingIdentitySideLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        image_ids = [str(image["image_id"]) for image in images]
        if len(image_ids) == 1:
            return {
                "status": "fail",
                "summary": "身份证必要组成部分缺失。",
                "semantic_match": {
                    "status": "matched",
                    "reason": "当前 requirement 属于独立身份证明要求。",
                },
                "materials": [],
                "requirements": [
                    {
                        "requirement": "应同时提供身份证国徽面及人像面。",
                        "status": "fail",
                        "evidence_image_ids": image_ids,
                        "reason": "当前仅识别到身份证一面。",
                    }
                ],
            }
        return super().review_attachment(system_prompt, user_prompt, images)


def test_missing_identity_side_is_detected_using_temporary_image_set(tmp_path: Path):
    source_document = _document()
    document = copy.deepcopy(source_document)
    document["sections"][0]["title"] = "1 法定代表人身份证明"
    original_image_count = len(document["images"])
    document["images"] = [
        image for image in document["images"] if image["image_id"] != "image-authority"
    ]

    result = run_attachment_review(
        {"templates": [_template("法定代表人身份证明", "应提供身份证明。" )]},
        {"structured_document": document, "artifact_dir": str(tmp_path)},
        llm=MissingIdentitySideLLM(),
    )

    review = result["attachment_reviews"][0]
    assert review["status"] == "fail"
    assert review["image_ids"] == ["image-id"]
    assert "必要组成部分缺失" in review["summary"]
    assert len(source_document["images"]) == original_image_count


class MissingBankProofLLM(RecordingAttachmentLLM):
    def review_attachment(self, system_prompt, user_prompt, images):
        if not images:
            return {
                "status": "fail",
                "summary": "强制要求的开户证明未提供。",
                "semantic_match": {
                    "status": "matched",
                    "reason": "当前 requirement 属于独立开户证明要求。",
                },
                "materials": [],
                "requirements": [
                    {
                        "requirement": "应提供基本账户开户证明。",
                        "status": "fail",
                        "evidence_image_ids": [],
                        "reason": "当前模块没有关联的开户证明图片。",
                    }
                ],
            }
        return super().review_attachment(system_prompt, user_prompt, images)


def test_missing_bank_proof_is_detected_using_temporary_image_set(tmp_path: Path):
    document = _document()
    document["sections"] = [document["sections"][2]]
    document["sections"][0]["title"] = "3 基本开户银行情况"
    document["blocks"] = [
        block for block in document["blocks"] if block["block_id"] == "b-bank-table"
    ]
    document["tables"] = [
        {
            "block_id": "b-bank-table",
            "rows": [["银行名称", "账户名称", "账号"]],
            "image_ids": ["image-bank-proof"],
        }
    ]
    document["images"] = [
        {
            "image_id": "image-bank-proof",
            "block_id": "b-bank-table",
            "section_id": "s-bank",
            "section_path": ["3 基本开户银行情况"],
            "img_path": "images/bank-proof.png",
            "asset_status": "ready",
            "source_type": "table_embedded",
            "source_table_id": "t0001",
        }
    ]
    temporary_document = copy.deepcopy(document)
    temporary_document["tables"][0]["image_ids"] = []
    temporary_document["images"] = []

    result = run_attachment_review(
        {"templates": [_template("基本开户银行情况", "应提供基本账户开户证明。" )]},
        {"structured_document": temporary_document, "artifact_dir": str(tmp_path)},
        llm=MissingBankProofLLM(),
    )

    review = result["attachment_reviews"][0]
    assert review["status"] == "fail"
    assert review["image_ids"] == []
    assert "开户证明未提供" in review["summary"]
    assert document["tables"][0]["image_ids"] == ["image-bank-proof"]


def test_attachment_review_preserves_legacy_case_evidence_and_order(
    tmp_path: Path,
):
    for filename in ("id.png", "authority.png"):
        path = tmp_path / "images" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture-image")

    templates = [
        _template(
            "法定代表人/负责人身份证明",
            "合法有效身份证明；居民身份证须同时提供国徽面和人像面。",
        ),
        _template(
            "法定代表人授权委托书",
            "应附委托代理人的合法有效身份证明。",
        ),
        _template(
            "基本开户银行情况",
            "提供基本账户相关证明材料，并能够支持开户银行情况。",
        ),
        _template("营业执照", "不应在本轮附件检查。"),
    ]
    llm = RecordingAttachmentLLM()

    result = run_attachment_review(
        {"templates": templates},
        {"structured_document": _document(), "artifact_dir": str(tmp_path)},
        llm=llm,
    )

    assert [item["template_name"] for item in result["attachment_reviews"]] == [
        "法定代表人/负责人身份证明",
        "法定代表人授权委托书",
        "基本开户银行情况",
    ]
    assert len(llm.calls) == 3
    assert all(call[0] == ATTACHMENT_REVIEW_SYSTEM_PROMPT for call in llm.calls)
    assert "银行名称 | 账户名称 | 账号" in next(
        call[1] for call in llm.calls if "基本开户银行情况" in call[1]
    )
    assert '"materials"' in llm.calls[0][1]
    assert '"requirements"' in llm.calls[0][1]
    assert "身份证人像面" in result["attachment_reviews"][0]["materials"][0]["facts"][0]["name"]
    assert result["attachment_reviews"][0]["image_ids"] == ["image-id"]
    assert result["attachment_reviews"][1]["image_ids"] == ["image-authority"]
    assert result["attachment_reviews"][2]["image_ids"] == []
    assert result["stats"]["selected_template_count"] == 3


def test_attachment_review_requires_existing_matched_module_and_rejects_unknown_evidence(
    tmp_path: Path,
):
    class InvalidEvidenceLLM(RecordingAttachmentLLM):
        def review_attachment(self, system_prompt, user_prompt, images):
            return {
                "status": "pass",
                "summary": "有材料。",
                "semantic_match": {
                    "status": "matched",
                    "reason": "当前 requirement 属于独立证明材料要求。",
                },
                "materials": [],
                "requirements": [
                    {
                        "requirement": "应提供材料。",
                        "status": "pass",
                        "evidence_image_ids": ["not-a-real-image"],
                        "reason": "测试。",
                    }
                ],
            }

    templates = [_template("法定代表人身份证明", "应提供身份证明。")]
    document = _document()
    document["sections"][0]["title"] = "1 法定代表人身份证明"

    result = run_attachment_review(
        {"templates": templates},
        {"structured_document": document, "artifact_dir": str(tmp_path)},
        llm=InvalidEvidenceLLM(),
    )

    assert len(result["attachment_reviews"]) == 1
    assert result["attachment_reviews"][0]["execution_status"] == "failed"
    assert "不存在的 image_id" in result["attachment_reviews"][0]["error_message"]


def test_openai_compatible_attachment_review_sends_associated_image_as_data_url(
    tmp_path: Path,
    monkeypatch,
):
    image_path = tmp_path / "id.png"
    image_path.write_bytes(b"fixture-image")
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "status": "uncertain",
                                        "summary": "图片内容待确认。",
                                        "semantic_match": {
                                            "status": "matched",
                                            "reason": "当前 requirement 属于独立证明材料要求。",
                                        },
                                        "materials": [],
                                        "requirements": [],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("app.attachment_review.urllib.request.urlopen", fake_urlopen)
    llm = OpenAICompatibleAttachmentReviewLLM(
        api_key="test-key",
        base_url="https://llm.example/v1",
        model="qwen3.8-27b",
    )

    result = llm.review_attachment(
        ATTACHMENT_REVIEW_SYSTEM_PROMPT,
        "附件检查用户提示词",
        [{"image_id": "image-1", "resolved_path": str(image_path)}],
    )

    assert result["status"] == "uncertain"
    content = captured["payload"]["messages"][1]["content"]
    image_part = next(item for item in content if item["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert "当前图片 image_id=image-1" in [
        item["text"] for item in content if item["type"] == "text"
    ]


def test_combined_review_keeps_template_text_result_and_adds_scoped_attachment_result(
    tmp_path: Path,
):
    class TextLLM:
        model = "text-model"

        def review_template(self, system_prompt, user_prompt):
            return {
                "status": "pass",
                "summary": "文本完整。",
                "issues": [],
                "semantic_match": {
                    "status": "matched",
                    "reason": "模块用途与模板对应。",
                },
            }

    attachment_llm = RecordingAttachmentLLM()
    document = _document()
    document["sections"][0]["title"] = "1 法定代表人身份证明"
    result = run_compliance_review_with_attachments(
        {"templates": [_template("法定代表人身份证明", "应提供身份证明。")]},
        {"structured_document": document, "artifact_dir": str(tmp_path)},
        template_review_llm=TextLLM(),
        attachment_review_llm=attachment_llm,
    )

    assert result["mode"] == "template_text_and_attachments"
    assert result["template_text_reviews"][0]["status"] == "pass"
    assert len(result["attachment_reviews"]) == 1
    assert result["attachment_stats"]["selected_template_count"] == 1


def test_default_workflow_does_not_create_empty_attachment_review_without_llm(
    settings,
    repository,
):
    workflow = build_default_workflow(settings, repository)
    document = _document()
    document["sections"][0]["title"] = "1 法定代表人身份证明"
    try:
        result = workflow.services.review(
            {"templates": [_template("法定代表人身份证明", "应提供身份证明。")]},
            {"structured_document": document},
        )
    finally:
        workflow.shutdown()

    assert result["mode"] == "template_text"
    assert result["template_text_reviews"][0]["status"] == "uncertain"
    assert "attachment_reviews" not in result


def test_complete_page_renders_attachment_facts_and_requirement_conclusion(
    client,
    repository,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    artifact_dir = Path(stored_task.bid_file.storage_path).parent / "bid_document_cleaning"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "structured_document.json").write_text(
        json.dumps(
            {
                "sections": [
                    {
                        "section_id": "s-id",
                        "parent_section_id": None,
                        "title": "1 法定代表人身份证明",
                        "path": ["1 法定代表人身份证明"],
                        "direct_block_ids": ["b-id"],
                    }
                ],
                "blocks": [{"block_id": "b-id", "type": "paragraph", "text": "身份证明"}],
                "tables": [],
                "images": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository.complete(
        stored_task.task_id,
        {
            "templates": [_template("法定代表人身份证明", "应提供身份证明。")],
            "project_requirements": [],
            "supplemental_materials": [],
            "bid_parse": {"status": "success", "stats": {"section_count": 1}},
            "review_result": {
                "mode": "template_text_and_attachments",
                "template_text_reviews": [],
                "attachment_reviews": [
                    {
                        "template_id": "tpl-法定代表人身份证明",
                        "template_name": "法定代表人身份证明",
                        "bid_module_name": "1 法定代表人身份证明",
                        "status": "pass",
                        "summary": "身份证材料满足要求。",
                        "image_ids": ["image-id"],
                        "materials": [
                            {
                                "material_type": "居民身份证",
                                "image_ids": ["image-id"],
                                "facts": [
                                    {
                                        "name": "身份证人像面",
                                        "status": "present",
                                        "evidence_image_ids": ["image-id"],
                                    }
                                ],
                            }
                        ],
                        "requirements": [
                            {
                                "requirement": "应提供身份证明。",
                                "status": "pass",
                                "evidence_image_ids": ["image-id"],
                                "reason": "已识别到身份证材料。",
                            }
                        ],
                        "llm_elapsed_ms": 12,
                    }
                ],
                "attachment_stats": {"llm_total_calls": 1, "llm_elapsed_ms": 12},
                "stats": {"llm_total_calls": 0, "llm_elapsed_ms": 0},
            },
        },
    )

    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert "已执行指定模板文本与附件检查" in response.text
    assert "附件检查结果" in response.text
    assert "视觉事实" in response.text
    assert "身份证人像面" in response.text
    assert "身份证材料满足要求。" in response.text


def test_attachment_review_writes_traceable_artifact_and_events(tmp_path: Path):
    task_dir = tmp_path / "task"
    bid_artifact_dir = task_dir / "bid_document_cleaning"
    image_path = bid_artifact_dir / "images" / "id.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"fixture-image")
    document = _document()
    document["sections"] = [document["sections"][0]]
    document["sections"][0]["title"] = "1 法定代表人身份证明"
    document["images"] = [
        {
            "image_id": "image-id",
            "block_id": "b-id-image",
            "img_path": "images/id.png",
            "asset_status": "ready",
        }
    ]
    recorder = ComplianceExtractionRecorder(task_dir)

    run_attachment_review(
        {"templates": [_template("法定代表人身份证明", "应提供身份证明。")]},
        {"structured_document": document, "artifact_dir": str(bid_artifact_dir)},
        llm=RecordingAttachmentLLM(),
        recorder=recorder,
    )

    artifact_path = task_dir / "compliance_extraction" / "09_attachment_reviews.json"
    assert artifact_path.is_file()
    saved = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert saved["attachment_reviews"][0]["image_ids"] == ["image-id"]
    events = [
        json.loads(line)
        for line in recorder.execution_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "attachment.review.end" in [event["event"] for event in events]


def test_attachment_review_reads_unified_image_index_for_table_embedded_images(
    tmp_path: Path,
):
    class ImageRecordingLLM(RecordingAttachmentLLM):
        pass

    template = _template("基本开户银行情况", "应提供基本账户相关证明材料。")
    document = {
        "sections": [
            {
                "section_id": "s-bank",
                "parent_section_id": None,
                "title": "1 基本开户银行情况",
                "path": ["1 基本开户银行情况"],
                "direct_block_ids": ["table-bank"],
            }
        ],
        "blocks": [
            {
                "block_id": "table-bank",
                "type": "table",
                "text": "银行名称 | 账户名称 | 账号",
                "order": 1,
            }
        ],
        "tables": [
            {
                "block_id": "table-bank",
                "rows": [["银行名称", "账户名称", "账号"]],
                "image_ids": ["image-bank-proof"],
            }
        ],
        "images": [
            {
                "image_id": "image-bank-proof",
                "block_id": "table-bank",
                "section_id": "s-bank",
                "section_path": ["1 基本开户银行情况"],
                "img_path": "images/bank-proof.png",
                "asset_status": "ready",
                "source_type": "table_embedded",
                "source_table_id": "t0001",
            }
        ],
    }
    llm = ImageRecordingLLM()

    run_attachment_review(
        {"templates": [template]},
        {"structured_document": document, "artifact_dir": str(tmp_path)},
        llm=llm,
    )

    assert len(llm.calls) == 1
    assert [image["image_id"] for image in llm.calls[0][2]] == ["image-bank-proof"]
