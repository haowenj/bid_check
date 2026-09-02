from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.attachment_review import (
    ATTACHMENT_REVIEW_SYSTEM_PROMPT,
    OpenAICompatibleAttachmentReviewLLM,
    is_complex_attachment_scope,
    run_compliance_review_with_attachments,
    run_attachment_review,
    template_has_attachment_requirement,
)
from app.api import build_default_workflow
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


def test_21_and_21_x_are_excluded_from_attachment_jobs():
    assert is_complex_attachment_scope({}, {"title": "21 业绩情况表"}) is True
    assert is_complex_attachment_scope({}, {"title": "21.3 合同关键页"}) is True
    assert is_complex_attachment_scope({}, {"title": "20 基本开户银行情况"}) is False


def test_attachment_review_selects_only_three_matched_cases_and_preserves_visual_evidence(
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
            return {"status": "pass", "summary": "文本完整。", "issues": []}

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


def test_default_workflow_wires_attachment_review_after_stable_text_review(
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

    assert result["mode"] == "template_text_and_attachments"
    assert result["template_text_reviews"][0]["status"] == "uncertain"
    assert len(result["attachment_reviews"]) == 1
    assert result["attachment_reviews"][0]["status"] == "uncertain"


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
