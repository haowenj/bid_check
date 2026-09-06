from __future__ import annotations

import json

import app.compliance_extraction as extraction_module
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import CandidateWindow, StructuredBlock, extract_tender_compliance_objects
from app.models import FileMetadata


def test_file_requirement_normalization_keeps_only_explicit_single_file_rules():
    blocks = [
        StructuredBlock(
            "b1",
            "table",
            "电子投标文件不得超过 200MB；文件名称应包含项目名称和投标人名称。",
            "投标人须知前附表",
            1,
        )
    ]
    raw_output = {
        "file_requirements": [
            {
                "name": "电子投标文件大小限制",
                "requirement": "电子投标文件不得超过 200MB",
                "target": "single_bid_file",
                "requirement_type": "size",
                "constraint_status": "explicit_constraint",
                "parameters": {"max_value": 200, "unit": "MB"},
                "auto_checkable": True,
                "support_reason": "原文明确给出单份电子投标文件上限",
                "source_block_ids": ["b1"],
            },
            {
                "name": "投标文件命名规范",
                "requirement": "文件名称应包含项目名称和投标人名称",
                "target": "single_bid_file",
                "requirement_type": "filename",
                "constraint_status": "explicit_constraint",
                "parameters": {"contains": ["项目名称", "投标人名称"]},
                "auto_checkable": True,
                "support_reason": "原文明确给出命名要求",
                "source_block_ids": ["b1"],
            },
            {
                "name": "商务标与技术标分别上传",
                "requirement": "商务标和技术标应分别上传",
                "target": "file_collection",
                "requirement_type": "other",
                "constraint_status": "explicit_constraint",
                "parameters": {},
                "auto_checkable": False,
                "support_reason": "涉及多个上传文件",
                "source_block_ids": ["b1"],
            },
            {
                "name": "平台上传能力",
                "requirement": "系统最大支持上传 500MB 文件",
                "target": "single_bid_file",
                "requirement_type": "size",
                "constraint_status": "platform_capability",
                "parameters": {"max_value": 500, "unit": "MB"},
                "auto_checkable": True,
                "support_reason": "平台能力说明",
                "source_block_ids": ["b1"],
            },
        ]
    }

    normalizer = getattr(extraction_module, "normalize_file_requirements", None)
    assert normalizer is not None

    result = normalizer(raw_output, blocks)

    assert [item["requirement_type"] for item in result] == ["size", "filename"]
    assert result[0]["parameters"]["limit_bytes"] == 200 * 1024 * 1024
    assert result[0]["parameters"]["raw_value"] == 200
    assert result[0]["source"]["block_ids"] == ["b1"]
    assert result[0]["source"]["source_text"] == blocks[0].text
    assert result[1]["auto_checkable"] is False
    assert "具体" in result[1]["support_reason"]


def test_file_requirement_normalization_preserves_extension_rule_and_source():
    blocks = [
        StructuredBlock(
            "b1",
            "paragraph",
            "电子投标文件采用 PDF 格式。",
            "电子投标文件",
            1,
        )
    ]
    normalizer = getattr(extraction_module, "normalize_file_requirements", None)
    assert normalizer is not None

    result = normalizer(
        {
            "file_requirements": [
                {
                    "name": "文件格式",
                    "requirement": "电子投标文件采用 PDF 格式",
                    "target": "single_bid_file",
                    "requirement_type": "extension",
                    "constraint_status": "explicit_constraint",
                    "parameters": {"allowed_extensions": ["PDF"]},
                    "auto_checkable": True,
                    "support_reason": "明确要求 PDF",
                    "source_block_ids": ["b1"],
                }
            ]
        },
        blocks,
    )

    assert result[0]["parameters"]["allowed_extensions"] == [".pdf"]
    assert result[0]["source"]["section"] == "电子投标文件"


def test_openai_file_requirement_prompt_has_single_file_scope(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"file_requirements": []}'}}]}).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    extractor = getattr(extraction_module.OpenAICompatibleLLM(api_key="test-key"), "extract_file_requirements", None)
    assert extractor is not None

    extractor(
        [
            CandidateWindow(
                ["b1"],
                "投标人须知前附表",
                "电子投标文件不得超过 200MB。",
                1,
                kind="file_requirements",
            )
        ]
    )

    prompt = captured["payload"]["messages"][1]["content"]
    assert "single_bid_file" in prompt
    assert "不得把平台支持上传容量当作投标约束" in prompt
    assert "分别上传" in prompt
    assert "单个组成部分" in prompt
    assert "总容量" in prompt
    assert "source_block_ids" in prompt


def test_main_tender_extraction_persists_file_requirements_and_candidate_stats(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock(
            "b1",
            "heading",
            "投标人须知前附表",
            "投标人须知前附表",
            1,
        ),
        StructuredBlock(
            "b2",
            "table",
            "电子投标文件不得超过 200MB；文件名称应包含项目名称和投标人名称。",
            "投标人须知前附表",
            2,
        ),
    ]

    class Parser:
        def parse(self, path):
            return blocks

    class LLM:
        model = "test-model"

        def extract(self, batch):
            return {
                "templates": [],
                "project_requirements": [],
                "supplemental_materials": [],
            }

        def extract_file_requirements(self, candidates):
            assert len(candidates) == 1
            assert "不得超过 200MB" in candidates[0].text
            return {
                "file_requirements": [
                    {
                        "name": "电子投标文件大小限制",
                        "requirement": "电子投标文件不得超过 200MB",
                        "target": "single_bid_file",
                        "requirement_type": "size",
                        "constraint_status": "explicit_constraint",
                        "parameters": {"max_value": 200, "unit": "MB"},
                        "auto_checkable": True,
                        "support_reason": "原文明确给出单份文件大小上限",
                        "source_block_ids": ["b1", "b2"],
                    }
                ]
            }

    task_dir = tmp_path / "task"
    result = extract_tender_compliance_objects(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=Parser(),
        llm=LLM(),
        recorder=ComplianceExtractionRecorder(task_dir),
    )

    assert result["file_requirements"][0]["parameters"]["limit_bytes"] == 200 * 1024 * 1024
    artifact_dir = task_dir / "compliance_extraction"
    artifact = json.loads((artifact_dir / "08_file_requirement_candidates.json").read_text())
    assert artifact["candidate_count"] == 1
    assert artifact["candidate_chars"] == len(artifact["candidates"][0]["text"])
    assert json.loads((artifact_dir / "07_result.json").read_text())["file_requirements"]
    summary = json.loads((artifact_dir / "summary.json").read_text())
    assert summary["stats"]["file_requirement_count"] == 1


def test_malformed_file_requirement_output_does_not_fail_existing_extraction(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock("b1", "heading", "电子投标文件", "电子投标文件", 1),
        StructuredBlock("b2", "paragraph", "文件不得超过 10MB。", "电子投标文件", 2),
    ]

    class Parser:
        def parse(self, path):
            return blocks

    class LLM:
        model = "test-model"

        def extract(self, batch):
            return {
                "templates": [],
                "project_requirements": [],
                "supplemental_materials": [],
            }

        def extract_file_requirements(self, candidates):
            return {
                "file_requirements": [
                    {
                        "name": "大小",
                        "requirement": "文件不得超过 10MB",
                        "target": "single_bid_file",
                        "requirement_type": "size",
                        "constraint_status": "explicit_constraint",
                        "parameters": {"max_value": 10, "unit": "MB"},
                        "auto_checkable": True,
                        "support_reason": "明确限制",
                    }
                ]
            }

    task_dir = tmp_path / "task"
    result = extract_tender_compliance_objects(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=Parser(),
        llm=LLM(),
        recorder=ComplianceExtractionRecorder(task_dir),
    )

    assert result["templates"] == []
    assert result["file_requirements"] == []
    summary = json.loads((task_dir / "compliance_extraction" / "summary.json").read_text())
    assert summary["status"] == "complete"
    assert summary["stats"]["file_requirement_llm_failed_calls"] == 1
    filter_report = json.loads(
        (task_dir / "compliance_extraction" / "09_file_requirement_filter_report.json").read_text()
    )
    assert filter_report["filtered_by_reason"]["schema_or_call_error"] == 1
