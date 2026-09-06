from __future__ import annotations

import importlib.util
import json

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.attachment_review import run_compliance_review_with_attachments
from app.template_text_review import DeterministicTemplateTextReviewLLM
from app.models import FileMetadata


def _requirement(
    requirement_id: str,
    name: str,
    text: str,
    requirement_type: str,
    parameters: dict,
    *,
    auto_checkable: bool = True,
):
    return {
        "id": requirement_id,
        "name": name,
        "requirement": text,
        "target": "single_bid_file",
        "requirement_type": requirement_type,
        "constraint_status": "explicit_constraint",
        "parameters": parameters,
        "auto_checkable": auto_checkable,
        "support_reason": "招标文件明确要求",
        "source": {
            "section": "投标人须知前附表",
            "block_ids": ["t1"],
            "source_text": text,
        },
    }


def _load_review_module():
    assert importlib.util.find_spec("app.file_requirement_review") is not None
    return __import__("app.file_requirement_review", fromlist=["run_file_requirement_review"])


def test_file_review_uses_real_original_bytes_not_recorded_upload_size(tmp_path):
    module = _load_review_module()
    original = tmp_path / "投标文件.pdf"
    original.write_bytes(b"0123456789abcdef")
    metadata = FileMetadata("投标文件.pdf", 1, str(original))
    extraction_result = {
        "file_requirements": [
            _requirement(
                "file_requirement_001",
                "文件大小限制",
                "电子投标文件不得超过 10B",
                "size",
                {"limit_bytes": 10, "raw_value": 10, "raw_unit": "B"},
            )
        ]
    }

    result = module.run_file_requirement_review(
        extraction_result,
        {"original_file_metadata": metadata.to_dict()},
    )

    review = result["file_requirement_reviews"][0]
    assert review["status"] == "fail"
    assert review["actual"]["size_bytes"] == 16
    assert review["actual"]["recorded_upload_size"] == 1
    assert review["expected"]["max_bytes"] == 10
    assert "16" in review["message"]


def test_file_review_checks_extension_and_concrete_filename_rules(tmp_path):
    module = _load_review_module()
    original = tmp_path / "项目甲-投标人乙.pdf"
    original.write_bytes(b"pdf")
    metadata = FileMetadata("项目甲-投标人乙.pdf", original.stat().st_size, str(original))
    extraction_result = {
        "file_requirements": [
            _requirement(
                "file_requirement_001",
                "文件格式",
                "电子投标文件采用 PDF 格式",
                "extension",
                {"allowed_extensions": [".pdf"]},
            ),
            _requirement(
                "file_requirement_002",
                "文件名称",
                "文件名称应包含项目甲和投标人乙",
                "filename",
                {"contains": ["项目甲", "投标人乙"], "concrete_contains": ["项目甲", "投标人乙"]},
            ),
            _requirement(
                "file_requirement_003",
                "无法确认的命名规则",
                "文件名称应包含项目名称和投标人名称",
                "filename",
                {"contains": ["项目名称", "投标人名称"]},
                auto_checkable=False,
            ),
        ]
    }

    result = module.run_file_requirement_review(
        extraction_result,
        {"original_file_metadata": metadata.to_dict()},
    )

    assert [item["status"] for item in result["file_requirement_reviews"]] == [
        "pass",
        "pass",
        "not_supported",
    ]
    assert result["file_requirement_reviews"][0]["actual"]["extension"] == ".pdf"
    assert result["file_requirement_reviews"][2]["status_label"] == "无法自动检查"


def test_file_review_writes_independent_artifact_with_all_requirements(tmp_path):
    module = _load_review_module()
    original = tmp_path / "bid.docx"
    original.write_bytes(b"bid")
    metadata = FileMetadata("bid.docx", original.stat().st_size, str(original))
    recorder = ComplianceExtractionRecorder(tmp_path / "task")

    result = module.run_file_requirement_review(
        {
            "file_requirements": [
                _requirement(
                    "file_requirement_001",
                    "文件格式",
                    "电子投标文件采用 PDF 格式",
                    "extension",
                    {"allowed_extensions": [".pdf"]},
                )
            ]
        },
        {"original_file_metadata": metadata.to_dict()},
        recorder=recorder,
    )

    artifact = json.loads(
        (recorder.artifact_dir / "10_file_requirement_reviews.json").read_text()
    )
    assert artifact["source"] == "original_uploaded_file"
    assert artifact["original_file"]["filename"] == "bid.docx"
    assert artifact["original_file"]["size_bytes"] == 3
    assert artifact["requirements"] == result["file_requirement_reviews"]
    assert artifact["stats"]["fail_count"] == 1


def test_composed_review_includes_file_results_in_existing_review_structure(tmp_path):
    original = tmp_path / "bid.docx"
    original.write_bytes(b"bid")
    metadata = FileMetadata("bid.docx", original.stat().st_size, str(original))

    result = run_compliance_review_with_attachments(
        {
            "templates": [],
            "project_requirements": [],
            "supplemental_materials": [],
            "file_requirements": [
                _requirement(
                    "file_requirement_001",
                    "文件格式",
                    "电子投标文件采用 PDF 格式",
                    "extension",
                    {"allowed_extensions": [".pdf"]},
                )
            ],
        },
        {"original_file_metadata": metadata.to_dict()},
        template_review_llm=DeterministicTemplateTextReviewLLM(),
        attachment_review_llm=object(),
    )

    assert result["mode"] == "template_text_and_file_requirements"
    assert result["file_requirement_reviews"][0]["status"] == "fail"
    assert result["file_requirement_stats"]["fail_count"] == 1
