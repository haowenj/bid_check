from __future__ import annotations

import json
import logging
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import app.compliance_extraction as extraction_module

from app.compliance_extraction import (
    CandidateWindow,
    ComplianceExtractionError,
    InMemoryRequirementCache,
    StructuredBlock,
    build_candidate_batches,
    extract_compliance_requirements_real,
    parse_docx_document,
    select_compliance_candidates,
)
from app.models import FileMetadata


def make_docx(*paragraphs: tuple[str, str | None]) -> bytes:
    body = []
    for text, style in paragraphs:
        style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
        body.append(f"<w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}</w:body></w:document>"
    ).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def test_parse_docx_recovers_ordered_blocks_and_sections(tmp_path):
    path = tmp_path / "tender.docx"
    path.write_bytes(
        make_docx(
            ("第六章 投标文件格式", "Heading1"),
            ("投标人名称：____", None),
            ("法定代表人应签字并加盖公章。", None),
        )
    )

    blocks = parse_docx_document(path)

    assert [block.block_id for block in blocks] == ["b0001", "b0002", "b0003"]
    assert blocks[0].type == "heading"
    assert blocks[1].section == "第六章 投标文件格式"
    assert blocks[1].text == "投标人名称：____"


def test_parse_docx_recovers_table_as_single_ordered_block(tmp_path):
    path = tmp_path / "tender.docx"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>人员姓名</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>身份证明</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:tbl></w:body></w:document>"
    ).encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    path.write_bytes(output.getvalue())

    blocks = parse_docx_document(path)

    assert len(blocks) == 1
    assert blocks[0].type == "table"
    assert blocks[0].text == "人员姓名 | 身份证明"


def test_candidate_selection_keeps_compliance_and_excludes_scoring_text():
    blocks = [
        StructuredBlock("b0001", "heading", "商务评分标准", "商务评分标准", 1),
        StructuredBlock(
            "b0002", "paragraph", "商务评分满分 20 分。", "商务评分标准", 2
        ),
        StructuredBlock("b0003", "heading", "投标文件格式", "投标文件格式", 3),
        StructuredBlock(
            "b0004", "paragraph", "投标人名称应填写完整。", "投标文件格式", 4
        ),
        StructuredBlock(
            "b0005", "paragraph", "法定代表人应签字并加盖公章。", "投标文件格式", 5
        ),
    ]

    candidates = select_compliance_candidates(blocks)

    assert len(candidates) == 1
    assert candidates[0].block_ids == ["b0004", "b0005"]
    assert "评分" not in candidates[0].text


def test_candidate_selection_drops_table_of_contents_field_codes():
    blocks = [
        StructuredBlock(
            "b0001",
            "paragraph",
            "3.1 投标文件的组成 PAGEREF _Toc123 \\h 26",
            "目录",
            1,
        ),
        StructuredBlock(
            "b0002", "paragraph", "投标文件应加盖公章。", "投标文件格式", 2
        ),
    ]

    candidates = select_compliance_candidates(blocks)

    assert [candidate.block_ids for candidate in candidates] == [["b0002"]]


def test_candidate_batches_are_bounded_to_eight_by_default():
    candidates = [
        CandidateWindow(
            block_ids=[f"b{i:04d}"],
            section=f"第{i}章",
            text=f"第{i}章投标人名称应填写。",
            order=i,
        )
        for i in range(1, 16)
    ]

    batches = build_candidate_batches(candidates)

    assert len(batches) == 8
    assert all(batch for batch in batches)


def test_real_extractor_restores_source_text_and_deduplicates(tmp_path):
    blocks = [
        StructuredBlock("b0001", "heading", "投标文件格式", "投标文件格式", 1),
        StructuredBlock(
            "b0002", "paragraph", "投标人名称应填写完整。", "投标文件格式", 2
        ),
        StructuredBlock(
            "b0003", "paragraph", "投标人名称应填写完整。", "投标文件格式", 3
        ),
    ]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def extract(self, batch):
            self.calls.append(batch)
            return [
                {
                    "name": "投标文件格式完整性",
                    "category": "required_field",
                    "target": {"name": "投标文件", "scope": "single_section"},
                    "checks": [
                        {
                            "requirement": "投标人名称应填写完整。",
                            "check_type": "required_field",
                            "evidence_type": "text",
                        }
                    ],
                    "applicability": {"type": "always", "condition": None},
                    "source_block_ids": ["b0002", "b0003"],
                },
                {
                    "name": "投标文件格式完整性",
                    "category": "required_field",
                    "target": {"name": "投标文件", "scope": "single_section"},
                    "checks": [
                        {
                            "requirement": "投标人名称应填写完整。",
                            "check_type": "required_field",
                            "evidence_type": "text",
                        }
                    ],
                    "applicability": {"type": "always", "condition": None},
                    "source_block_ids": ["b0002"],
                },
            ]

    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"docx")
    fake_llm = FakeLLM()
    result = extract_compliance_requirements_real(
        FileMetadata("招标文件.docx", 1, str(tender)),
        parser=FakeParser(),
        llm=fake_llm,
    )

    assert len(fake_llm.calls) == 1
    assert len(result) == 1
    assert result[0]["id"] == "compliance_001"
    assert result[0]["source"]["block_ids"] == ["b0002", "b0003"]
    assert result[0]["source"]["source_text"] == (
        "投标人名称应填写完整。\n投标人名称应填写完整。"
    )


def test_real_extractor_rejects_invalid_llm_schema(tmp_path):
    class FakeParser:
        def parse(self, path):
            return [StructuredBlock("b0001", "paragraph", "投标人名称应填写。", "", 1)]

    class BadLLM:
        def extract(self, batch):
            return [{"name": "缺字段"}]

    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"docx")
    with pytest.raises(ComplianceExtractionError, match="Schema"):
        extract_compliance_requirements_real(
            FileMetadata("招标文件.docx", 1, str(tender)),
            parser=FakeParser(),
            llm=BadLLM(),
        )


def test_real_extractor_accepts_legacy_source_shape_and_replaces_ids(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"docx")

    class FakeParser:
        def parse(self, path):
            return [
                StructuredBlock(
                    "b0001", "paragraph", "必须提供营业执照。", "资格材料", 1
                )
            ]

    class FakeLLM:
        def extract(self, batch):
            return [
                {
                    "id": "model-id",
                    "name": "营业执照",
                    "category": "attachment",
                    "target": {"name": "资格材料", "scope": "single_section"},
                    "checks": [
                        {
                            "id": "model-check-id",
                            "requirement": "必须提供营业执照。",
                            "check_type": "attachment_exists",
                            "evidence_type": "structure",
                        }
                    ],
                    "applicability": {"type": "always", "condition": None},
                    "source": {"block_ids": ["b0001"], "source_text": "伪造文本"},
                }
            ]

    result = extract_compliance_requirements_real(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
        llm=FakeLLM(),
    )

    assert result[0]["id"] == "compliance_001"
    assert result[0]["checks"][0]["id"] == "compliance_001_01"
    assert result[0]["source"]["source_text"] == "必须提供营业执照。"


def test_real_extractor_caches_source_grounded_result(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"same tender")
    blocks = [StructuredBlock("b0001", "paragraph", "投标人名称应填写。", "格式", 1)]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def extract(self, batch):
            self.calls += 1
            return [
                {
                    "name": "投标人信息",
                    "category": "required_field",
                    "target": {"name": "投标文件", "scope": "single_section"},
                    "checks": [
                        {
                            "requirement": "投标人名称应填写。",
                            "check_type": "required_field",
                            "evidence_type": "text",
                        }
                    ],
                    "applicability": {"type": "always", "condition": None},
                    "source_block_ids": ["b0001"],
                }
            ]

    cache = InMemoryRequirementCache()
    llm = FakeLLM()
    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    first = extract_compliance_requirements_real(
        metadata, parser=FakeParser(), llm=llm, cache=cache
    )
    second = extract_compliance_requirements_real(
        metadata, parser=FakeParser(), llm=llm, cache=cache
    )

    assert first == second
    assert llm.calls == 1


def test_openai_compatible_llm_disables_thinking_and_bounds_output(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": '{"requirements": []}'}}]}
            ).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    llm = extraction_module.OpenAICompatibleLLM(api_key="test-key", timeout_seconds=17)
    result = llm.extract([CandidateWindow(["b0001"], "格式", "投标人名称应填写。", 1)])

    assert result == []
    assert captured["payload"]["enable_thinking"] is False
    assert captured["payload"]["max_tokens"] <= 8192
    assert captured["timeout"] == 17


def test_real_extractor_retries_transient_llm_timeout_with_global_budget(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [StructuredBlock("b0001", "paragraph", "投标人名称应填写。", "格式", 1)]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def extract(self, batch):
            self.calls += 1
            if self.calls == 1:
                try:
                    raise TimeoutError("temporary timeout")
                except TimeoutError as exc:
                    raise ComplianceExtractionError("请求超时") from exc
            return [
                {
                    "name": "投标人信息",
                    "category": "required_field",
                    "target": {"name": "投标文件", "scope": "single_section"},
                    "checks": [
                        {
                            "requirement": "投标人名称应填写。",
                            "check_type": "required_field",
                            "evidence_type": "text",
                        }
                    ],
                    "applicability": {"type": "always", "condition": None},
                    "source_block_ids": ["b0001"],
                }
            ]

    llm = FlakyLLM()
    result = extract_compliance_requirements_real(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
        llm=llm,
        max_retries=1,
    )

    assert result[0]["name"] == "投标人信息"
    assert llm.calls == 2


def test_real_extractor_logs_each_pipeline_stage(tmp_path, caplog):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock("b0001", "paragraph", "投标人名称应填写。", "格式", 1)
    ]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FakeLLM:
        def extract(self, batch):
            return [
                {
                    "name": "投标人信息",
                    "category": "required_field",
                    "target": {"name": "投标文件", "scope": "single_section"},
                    "checks": [
                        {
                            "requirement": "投标人名称应填写。",
                            "check_type": "required_field",
                            "evidence_type": "text",
                        }
                    ],
                    "applicability": {"type": "always", "condition": None},
                    "source_block_ids": ["b0001"],
                }
            ]

    caplog.set_level(logging.INFO, logger="app.compliance_extraction")
    extract_compliance_requirements_real(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
        llm=FakeLLM(),
    )

    messages = [record.getMessage() for record in caplog.records]
    for event in (
        "compliance.extract.start",
        "cache.check.end",
        "document.parse.end",
        "candidate.filter.end",
        "batch.build.end",
        "compliance.batch.end",
        "requirements.normalize.end",
        "compliance.extract.end",
    ):
        assert any(event in message for message in messages), event


def test_openai_compatible_llm_logs_call_start_and_end(monkeypatch, caplog):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": '{"requirements": []}'}}]}
            ).encode()

    def fake_urlopen(request, timeout):
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    caplog.set_level(logging.INFO, logger="app.compliance_extraction")
    llm = extraction_module.OpenAICompatibleLLM(api_key="test-key", model="test-model")
    llm.extract([CandidateWindow(["b0001"], "格式", "投标人名称应填写。", 1)])

    messages = [record.getMessage() for record in caplog.records]
    assert any("llm.call.start" in message for message in messages)
    assert any("llm.call.end" in message for message in messages)
    assert all("test-key" not in message for message in messages)
