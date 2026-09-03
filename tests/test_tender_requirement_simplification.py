from __future__ import annotations

import json

import app.compliance_extraction as extraction_module
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import (
    CandidateWindow,
    InMemoryRequirementCache,
    StructuredBlock,
    extract_tender_compliance_objects,
)
from app.models import FileMetadata


def test_object_cache_version_is_new_and_parse_cache_key_is_stable():
    assert extraction_module.REQUIREMENT_CACHE_VERSION.startswith(
        "tender-compliance-objects-v25"
    )
    assert extraction_module.PARSED_DOCUMENT_CACHE_VERSION.startswith("mineru-parse-v5")


def test_object_cache_key_changes_with_parser_and_llm_configuration(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"same tender")

    class ConfiguredParser:
        def __init__(self, command):
            self.command = command

    class ConfiguredLLM:
        def __init__(self, model):
            self.model = model

    key = extraction_module._requirement_cache_key
    parser_a = ConfiguredParser("mineru-a")
    parser_b = ConfiguredParser("mineru-b")
    llm_a = ConfiguredLLM("model-a")
    llm_b = ConfiguredLLM("model-b")

    assert key(tender, parser_a, llm_a, max_batches=8, max_batch_chars=12000) != key(
        tender, parser_b, llm_a, max_batches=8, max_batch_chars=12000
    )
    assert key(tender, parser_a, llm_a, max_batches=8, max_batch_chars=12000) != key(
        tender, parser_a, llm_b, max_batches=8, max_batch_chars=12000
    )


def test_ambiguous_region_uses_narrow_llm_protocol_and_reuses_result_cache(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock("b1", "heading", "响应文件格式", "响应文件格式", 1),
        StructuredBlock("b2", "paragraph", "投标人名称：____", "响应文件格式", 2),
    ]

    class FakeParser:
        def __init__(self):
            self.calls = 0

        def parse(self, path):
            self.calls += 1
            return blocks

    class FakeLLM:
        model = "test-model"

        def __init__(self):
            self.calls = 0

        def extract(self, batch):
            self.calls += 1
            return {
                "templates": [
                    {"name": "投标函", "source_block_ids": ["b1", "b2"]}
                ],
                "project_requirements": [],
                "supplemental_materials": [],
            }

    parser = FakeParser()
    llm = FakeLLM()
    cache = InMemoryRequirementCache()
    parser_cache = InMemoryRequirementCache()
    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    first = extract_tender_compliance_objects(
        metadata,
        parser=parser,
        llm=llm,
        cache=cache,
        parser_cache=parser_cache,
    )
    recorder = ComplianceExtractionRecorder(tmp_path / "task-002")
    second = extract_tender_compliance_objects(
        metadata,
        parser=parser,
        llm=llm,
        cache=cache,
        parser_cache=parser_cache,
        recorder=recorder,
    )

    assert first == second
    assert llm.calls == 1
    assert parser.calls == 1
    summary = json.loads((recorder.artifact_dir / "summary.json").read_text())
    assert summary["stats"]["cache_hit"] is True
    assert summary["stats"]["llm_total_calls"] == 0


def test_successful_llm_call_records_object_payload_and_elapsed_time(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock("b1", "heading", "响应文件格式", "响应文件格式", 1),
        StructuredBlock("b2", "paragraph", "投标人名称：____", "响应文件格式", 2),
    ]

    class Parser:
        def parse(self, path):
            return blocks

    class LLM:
        model = "test-model"

        def extract(self, batch):
            return {
                "templates": [{"name": "投标函", "source_block_ids": ["b1", "b2"]}],
                "project_requirements": [],
                "supplemental_materials": [],
            }

    recorder = ComplianceExtractionRecorder(tmp_path / "task-001")
    extract_tender_compliance_objects(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=Parser(),
        llm=LLM(),
        recorder=recorder,
    )

    output = json.loads(
        (recorder.artifact_dir / "llm/call_001_output.json").read_text()
    )
    summary = json.loads((recorder.artifact_dir / "summary.json").read_text())
    assert output["parsed_objects"]["templates"][0]["name"] == "投标函"
    assert summary["stats"]["llm_total_calls"] == 1
    assert summary["stats"]["llm_completed_calls"] == 1
    assert summary["stats"]["llm_elapsed_ms"] >= 0


def test_deterministic_llm_returns_objects_without_execution_fields():
    result = extraction_module.DeterministicComplianceLLM().extract(
        [CandidateWindow(["b1"], "响应文件格式", "投标函\n投标人名称：____", 1)]
    )

    assert result == {
        "templates": [{"name": "投标函", "source_block_ids": ["b1"]}],
        "project_requirements": [],
        "supplemental_materials": [],
    }
