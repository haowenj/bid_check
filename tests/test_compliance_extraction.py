from __future__ import annotations

import json
import logging
from io import BytesIO
from typing import get_type_hints
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import app.compliance_extraction as extraction_module
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import (
    CandidateWindow,
    ComplianceExtractionError,
    FunctionalRegion,
    InMemoryRequirementCache,
    StructuredBlock,
    build_candidate_batches,
    extract_compliance_requirements_real,
    extract_project_requirements_from_regions,
    extract_templates_from_regions,
    identify_functional_regions,
    parse_docx_document,
    select_compliance_candidates,
)
from app.models import (
    FileMetadata,
    ProjectRequirement,
    SupplementalMaterial,
    TenderExtractionResult,
    TenderTemplate,
)


def test_tender_extraction_object_types_define_three_result_collections():
    assert set(get_type_hints(TenderExtractionResult)) == {
        "templates",
        "project_requirements",
        "supplemental_materials",
    }
    assert set(get_type_hints(TenderTemplate)) >= {
        "id",
        "name",
        "section",
        "block_ids",
        "body",
        "tables",
        "fields",
        "attachments",
        "source",
    }
    assert set(get_type_hints(ProjectRequirement)) >= {
        "id",
        "requirement",
        "value",
        "source",
    }
    assert set(get_type_hints(SupplementalMaterial)) >= {
        "id",
        "name",
        "material",
        "source",
    }


def test_functional_regions_use_semantic_titles_without_fixed_chapter_numbers():
    blocks = [
        StructuredBlock(
            "b0001",
            "heading",
            "第一部分 投标人须知前附表",
            "第一部分 投标人须知前附表",
            1,
        ),
        StructuredBlock(
            "b0002",
            "table",
            "投标文件组成 | 商务文件、技术文件、报价文件",
            "第一部分 投标人须知前附表",
            2,
        ),
        StructuredBlock(
            "b0003",
            "heading",
            "第三章 评标办法",
            "第三章 评标办法",
            3,
        ),
        StructuredBlock(
            "b0004",
            "paragraph",
            "商务评分满分 20 分。",
            "第三章 评标办法",
            4,
        ),
        StructuredBlock(
            "b0005",
            "heading",
            "附件 响应文件格式",
            "附件 响应文件格式",
            5,
        ),
        StructuredBlock(
            "b0006",
            "paragraph",
            "商务投标文件封面",
            "附件 响应文件格式",
            6,
        ),
        StructuredBlock(
            "b0007",
            "heading",
            "投标产品资格要求",
            "投标产品资格要求",
            7,
        ),
        StructuredBlock(
            "b0008",
            "paragraph",
            "须随投标文件提供制造商登记证明。",
            "投标产品资格要求",
            8,
        ),
    ]

    regions = identify_functional_regions(blocks)

    assert [(region.kind, region.title) for region in regions] == [
        ("project_requirements", "第一部分 投标人须知前附表"),
        ("templates", "附件 响应文件格式"),
        ("supplemental_materials", "投标产品资格要求"),
    ]
    assert regions[0].block_ids == ["b0001", "b0002"]
    assert regions[1].block_ids == ["b0005", "b0006"]
    assert regions[2].block_ids == ["b0007", "b0008"]
    assert all(region.kind != "templates" or "评分" not in region.text for region in regions)


def test_functional_region_type_keeps_source_order_and_blocks():
    assert set(get_type_hints(FunctionalRegion)) >= {
        "kind",
        "title",
        "section",
        "block_ids",
        "blocks",
        "text",
        "order",
    }


def test_template_extraction_keeps_one_complete_object_across_contiguous_blocks():
    blocks = [
        StructuredBlock(
            "b0100",
            "heading",
            "附件 商务投标文件格式",
            "附件 商务投标文件格式",
            100,
        ),
        StructuredBlock(
            "b0101",
            "heading",
            "一、法定代表人身份证明（格式）",
            "附件 商务投标文件格式",
            101,
        ),
        StructuredBlock(
            "b0102",
            "paragraph",
            "姓名：____ 性别：____ 年龄：____ 职务：____",
            "附件 商务投标文件格式",
            102,
        ),
        StructuredBlock(
            "b0103",
            "table",
            "投标人名称 | ____\n身份证附件 | 国徽面、人像面",
            "附件 商务投标文件格式",
            103,
            metadata={"rows": 2},
        ),
        StructuredBlock(
            "b0104",
            "paragraph",
            "附：身份证正反面扫描件。",
            "附件 商务投标文件格式",
            104,
        ),
        StructuredBlock(
            "b0105",
            "heading",
            "二、授权委托书",
            "附件 商务投标文件格式",
            105,
        ),
        StructuredBlock(
            "b0106",
            "paragraph",
            "委托代理人姓名：____，附授权委托书。",
            "附件 商务投标文件格式",
            106,
        ),
    ]

    regions = identify_functional_regions(blocks)
    templates = extract_templates_from_regions(regions)

    assert [template["name"] for template in templates] == [
        "法定代表人身份证明",
        "授权委托书",
    ]
    first = templates[0]
    assert first["block_ids"] == ["b0101", "b0102", "b0103", "b0104"]
    assert first["section"] == "附件 商务投标文件格式"
    assert "姓名：____" in first["body"]
    assert first["tables"] == [
        {
            "block_id": "b0103",
            "text": "投标人名称 | ____\n身份证附件 | 国徽面、人像面",
            "metadata": {"rows": 2},
        }
    ]
    assert {"姓名", "性别", "年龄", "职务", "投标人名称"} <= set(first["fields"])
    assert "身份证正反面扫描件" in first["attachments"]
    assert first["source"] == {
        "section": "附件 商务投标文件格式",
        "block_ids": ["b0101", "b0102", "b0103", "b0104"],
        "source_text": "\n".join(block.text for block in blocks[1:5]),
    }


def test_template_extraction_does_not_create_rule_per_block():
    blocks = [
        StructuredBlock(
            "b0200", "heading", "第九章 投标文件模板", "第九章 投标文件模板", 200
        ),
        StructuredBlock(
            "b0201", "heading", "投标函", "第九章 投标文件模板", 201
        ),
        StructuredBlock(
            "b0202", "paragraph", "项目名称：____", "第九章 投标文件模板", 202
        ),
        StructuredBlock(
            "b0203", "paragraph", "投标人名称：____", "第九章 投标文件模板", 203
        ),
    ]

    templates = extract_templates_from_regions(identify_functional_regions(blocks))

    assert len(templates) == 1
    assert templates[0]["name"] == "投标函"
    assert templates[0]["block_ids"] == ["b0201", "b0202", "b0203"]
    assert "项目名称：____" in templates[0]["body"]
    assert "投标人名称：____" in templates[0]["body"]


def test_project_requirement_extraction_keeps_only_bid_compilation_rows():
    blocks = [
        StructuredBlock(
            "b0300",
            "heading",
            "投标须知前附表",
            "投标须知前附表",
            300,
        ),
        StructuredBlock(
            "b0301",
            "table",
            "投标文件组成 | 商务文件、技术文件、报价文件\n"
            "单个组成部分大小 | 不得超过 50MB\n"
            "投标文件总容量 | 不得超过 500MB\n"
            "文件要求 | 清晰可读，第三方签字盖章的上传扫描件\n"
            "投标有效期 | 90 天\n"
            "投标保证金 | 无需递交投标保证金\n"
            "备选方案 | 不允许\n"
            "报价 | 保留两位小数",
            "投标须知前附表",
            301,
        ),
        StructuredBlock(
            "b0302",
            "paragraph",
            "评标委员会按照评分标准评审，中标候选人规则另行规定。",
            "投标须知前附表",
            302,
        ),
        StructuredBlock(
            "b0303",
            "paragraph",
            "合同签订后的履约期间应提供 7×24 小时服务。",
            "投标须知前附表",
            303,
        ),
    ]

    requirements = extract_project_requirements_from_regions(
        identify_functional_regions(blocks)
    )

    requirement_text = "\n".join(item["requirement"] for item in requirements)
    values = {item["value"] for item in requirements if item["value"] is not None}
    assert len(requirements) == 8
    assert "投标文件组成" in requirement_text
    assert "清晰可读" in requirement_text
    assert "无需递交投标保证金" in requirement_text
    assert {"50MB", "500MB", "90 天", "两位小数"} <= values
    assert all(item["source"]["block_ids"] == ["b0301"] for item in requirements)
    assert "评分标准" not in requirement_text
    assert "履约" not in requirement_text


def test_project_requirement_extraction_does_not_turn_qualifications_into_rules():
    blocks = [
        StructuredBlock(
            "b0310",
            "heading",
            "项目专用表",
            "项目专用表",
            310,
        ),
        StructuredBlock(
            "b0311",
            "paragraph",
            "具有良好的商业信誉，具备丰富的软件项目经验，能够提供 7×24 小时服务。",
            "项目专用表",
            311,
        ),
        StructuredBlock(
            "b0312",
            "paragraph",
            "各组成部分分别编制，单个文件不得超过 50MB。",
            "项目专用表",
            312,
        ),
    ]

    requirements = extract_project_requirements_from_regions(
        identify_functional_regions(blocks)
    )

    assert len(requirements) == 1
    assert all("商业信誉" not in item["requirement"] for item in requirements)
    assert all("软件项目经验" not in item["requirement"] for item in requirements)
    assert any("分别编制" in item["requirement"] for item in requirements)


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
                    "rule": "投标人名称应填写完整。",
                    "condition": None,
                    "source_block_ids": ["b0002", "b0003"],
                },
                {
                    "name": "投标文件格式完整性",
                    "rule": "投标人名称应填写完整。",
                    "condition": None,
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
    assert result[0]["id"] == "tender_requirement_001"
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


def test_real_extractor_rejects_legacy_execution_schema(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"docx")

    class FakeParser:
        def parse(self, path):
            return [
                StructuredBlock(
                    "b0001",
                    "paragraph",
                    "非事业单位须提供营业执照副本扫描件。",
                    "资格材料",
                    1,
                )
            ]

    class LegacyShapeLLM:
        def extract(self, batch):
            return [
                {
                    "name": "投标人主体资格证明",
                    "category": "attachment",
                    "target": "投标人主体资格证明",
                    "checks": [
                        "非事业单位须提供营业执照副本扫描件",
                        "事业单位须提供法人证书扫描件",
                    ],
                    "applicability": "所有投标人",
                    "source_block_ids": ["b0001"],
                }
            ]

    with pytest.raises(ComplianceExtractionError, match="Schema"):
        extract_compliance_requirements_real(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=FakeParser(),
            llm=LegacyShapeLLM(),
        )


def test_real_extractor_rejects_execution_fields_even_with_source_shape(tmp_path):
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

    with pytest.raises(ComplianceExtractionError, match="Schema"):
        extract_compliance_requirements_real(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=FakeParser(),
            llm=FakeLLM(),
        )


def test_normalization_rejects_legacy_execution_metadata(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"docx")

    class FakeParser:
        def parse(self, path):
            return [
                StructuredBlock(
                    "b0001",
                    "paragraph",
                    "须提供身份证人像面和国徽面。",
                    "法定代表人身份证明",
                    1,
                )
            ]

    class AliasLLM:
        def extract(self, batch):
            return [
                {
                    "name": "法定代表人身份证明",
                    "category": "存在性检查",
                    "target": {
                        "name": "投标文件商务部分",
                        "scope": "投标文件商务部分",
                    },
                    "checks": [
                        {
                            "requirement": "须提供身份证人像面和国徽面。",
                            "check_type": "存在性检查",
                            "evidence_type": "自由生成值",
                        }
                    ],
                    "applicability": {
                        "type": "所有投标人",
                        "condition": "所有投标人",
                    },
                    "source_block_ids": ["b0001"],
                }
            ]

    with pytest.raises(ComplianceExtractionError, match="Schema"):
        extract_compliance_requirements_real(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=FakeParser(),
            llm=AliasLLM(),
        )


def test_normalization_rejects_unknown_or_missing_simplified_fields(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"docx")

    class FakeParser:
        def parse(self, path):
            return [StructuredBlock("b0001", "paragraph", "必须填写。", "格式", 1)]

    class UnknownEnumLLM:
        def extract(self, batch):
            return [
                {
                    "name": "未知规则",
                    "source_block_ids": ["b0001"],
                }
            ]

    with pytest.raises(ComplianceExtractionError, match="Schema"):
        extract_compliance_requirements_real(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=FakeParser(),
            llm=UnknownEnumLLM(),
        )


def test_normalization_filters_non_executable_and_project_conflict_rules(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"docx")

    class FakeParser:
        def parse(self, path):
            return [
                StructuredBlock(
                    "b0001",
                    "paragraph",
                    "本项目不接受联合体投标。",
                    "投标人资格要求",
                    1,
                ),
                StructuredBlock(
                    "b0002",
                    "paragraph",
                    "投标人名称应填写，须提供身份证人像面和国徽面。",
                    "投标文件格式",
                    2,
                ),
                StructuredBlock(
                    "b0003",
                    "paragraph",
                    "投标文件封面项目名称和日期应填写。",
                    "商务投标文件封面",
                    3,
                ),
            ]

    class BoundaryLLM:
        def extract(self, batch):
            def item(name, rule, block_id):
                return {
                    "name": name,
                    "rule": rule,
                    "condition": None,
                    "source_block_ids": [block_id],
                }

            return [
                item(
                    "封面字段",
                    "投标人名称应填写。",
                    "b0002",
                ),
                item(
                    "身份证附件",
                    "须提供身份证人像面和国徽面。",
                    "b0002",
                ),
                item(
                    "电子采购系统上传",
                    "应在电子采购系统完成加密上传。",
                    "b0002",
                ),
                item(
                    "履约阶段安全告知书",
                    "合同签订后的履约期间安全告知书需签名。",
                    "b0002",
                ),
                item(
                    "联合体协议书",
                    "联合体各方应签订联合体协议书。",
                    "b0001",
                ),
            ]

    result = extract_compliance_requirements_real(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
        llm=BoundaryLLM(),
    )

    names = {item["name"] for item in result}
    assert {"封面字段", "身份证附件"} <= names
    assert "电子采购系统上传" not in names
    assert "履约阶段安全告知书" not in names
    assert "联合体协议书" not in names


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
                    "rule": "投标人名称应填写。",
                    "condition": None,
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
    assert captured["payload"]["max_tokens"] == 8192
    assert captured["timeout"] == 17
    prompt = captured["payload"]["messages"][1]["content"]
    assert "name、rule、condition、source_block_ids" in prompt
    assert "不得生成 check_type" in prompt


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
                    "rule": "投标人名称应填写。",
                    "condition": None,
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
    blocks = [StructuredBlock("b0001", "paragraph", "投标人名称应填写。", "格式", 1)]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FakeLLM:
        def extract(self, batch):
            return [
                {
                    "name": "投标人信息",
                    "rule": "投标人名称应填写。",
                    "condition": None,
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


def test_openai_compatible_llm_persists_raw_http_exchange(monkeypatch, tmp_path):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "response-1",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"requirements": []}'},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    },
                }
            ).encode()

    def fake_urlopen(request, timeout):
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    recorder = ComplianceExtractionRecorder(tmp_path / "task-001")
    call_id = recorder.start_llm_call(
        batch_index=1,
        batch_count=1,
        attempt=1,
        model="test-model",
        batch=[{"block_ids": ["b0001"], "text": "须提供证明材料"}],
    )
    llm = extraction_module.OpenAICompatibleLLM(
        api_key="test-key",
        model="test-model",
    )
    llm.set_call_context(recorder=recorder, call_id=call_id)
    result = llm.extract([CandidateWindow(["b0001"], "资格", "须提供证明材料", 1)])
    assert result == []
    recorder.complete_llm_call(call_id, parsed_requirements=result, elapsed_ms=2)

    input_payload = json.loads(
        (recorder.artifact_dir / "llm" / f"{call_id}_input.json").read_text()
    )
    output_payload = json.loads(
        (recorder.artifact_dir / "llm" / f"{call_id}_output.json").read_text()
    )
    assert input_payload["request_payload"]["model"] == "test-model"
    assert "Authorization" not in json.dumps(input_payload)
    assert output_payload["raw_response"]["id"] == "response-1"
    assert output_payload["finish_reason"] == "stop"
    assert output_payload["usage"]["total_tokens"] == 18


def test_real_extractor_persists_intermediates_and_summary(tmp_path):
    task_dir = tmp_path / "task-001"
    tender = task_dir / "tender.docx"
    task_dir.mkdir()
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock("b0001", "paragraph", "投标人名称应填写。", "格式", 1),
        StructuredBlock("b0002", "paragraph", "须提供营业执照扫描件。", "资格", 2),
    ]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FakeLLM:
        def extract(self, batch):
            return [
                {
                    "name": "投标人要求",
                    "rule": "投标人名称应填写。",
                    "condition": None,
                    "source_block_ids": [batch[0].block_ids[0]],
                }
            ]

    recorder = ComplianceExtractionRecorder(task_dir)
    result = extract_compliance_requirements_real(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
        llm=FakeLLM(),
        recorder=recorder,
        max_batches=2,
    )

    artifact_dir = task_dir / "compliance_extraction"
    assert result
    for name in (
        "01_parsed_blocks.json",
        "02_candidates.json",
        "03_batches.json",
        "04_raw_requirements.json",
        "05_normalized_requirements.json",
        "06_filter_report.json",
        "summary.json",
        "execution.jsonl",
        "llm/call_001_input.json",
        "llm/call_001_output.json",
    ):
        assert (artifact_dir / name).is_file(), name

    parsed = json.loads((artifact_dir / "01_parsed_blocks.json").read_text())
    candidates = json.loads((artifact_dir / "02_candidates.json").read_text())
    batches = json.loads((artifact_dir / "03_batches.json").read_text())
    input_payload = json.loads((artifact_dir / "llm/call_001_input.json").read_text())
    output = json.loads((artifact_dir / "llm/call_001_output.json").read_text())
    raw = json.loads((artifact_dir / "04_raw_requirements.json").read_text())
    normalized = json.loads(
        (artifact_dir / "05_normalized_requirements.json").read_text()
    )
    summary = json.loads((artifact_dir / "summary.json").read_text())
    events = [
        json.loads(line)
        for line in (artifact_dir / "execution.jsonl").read_text().splitlines()
    ]

    assert len(parsed["blocks"]) == 2
    assert candidates["window_count"] == 2
    assert batches["batch_count"] == 2
    assert input_payload["batch_index"] == 1
    assert input_payload["batch"]
    assert output["status"] == "success"
    assert output["schema_valid"] is True
    assert output["elapsed_ms"] is not None
    assert output["parsed_requirements"] == raw["requirements"][:1]
    assert normalized["requirements"] == result
    assert normalized["filter_count"] == 0
    assert json.loads((artifact_dir / "06_filter_report.json").read_text()) == []
    assert summary["status"] == "complete"
    assert summary["stats"]["llm_total_calls"] == 2
    assert summary["stats"]["final_requirements"] == len(result)
    assert summary["stats"]["cache_elapsed_ms"] is not None
    assert summary["stats"]["parser_elapsed_ms"] is not None
    assert summary["stats"]["candidate_filter_elapsed_ms"] is not None
    assert summary["stats"]["batch_build_elapsed_ms"] is not None
    assert summary["stats"]["normalization_elapsed_ms"] is not None
    assert summary["stats"]["total_elapsed_ms"] is not None
    event_names = [event["event"] for event in events]
    assert "compliance.extract.start" in event_names
    assert "llm.call.start" in event_names
    assert "llm.call.end" in event_names
    assert "compliance.extract.finalize" in event_names


def test_real_extractor_keeps_prior_artifacts_when_later_llm_call_fails(tmp_path):
    task_dir = tmp_path / "task-001"
    tender = task_dir / "tender.docx"
    task_dir.mkdir()
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock("b0001", "paragraph", "投标人名称应填写。", "格式一", 1),
        StructuredBlock("b0002", "paragraph", "营业执照须提供。", "格式二", 2),
    ]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def extract(self, batch):
            self.calls += 1
            if self.calls == 2:
                raise ComplianceExtractionError("模拟第二批失败")
            return [
                {
                    "name": "投标人要求",
                    "rule": "投标人名称应填写。",
                    "condition": None,
                    "source_block_ids": ["b0001"],
                }
            ]

    recorder = ComplianceExtractionRecorder(task_dir)
    with pytest.raises(ComplianceExtractionError, match="第二批失败"):
        extract_compliance_requirements_real(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=FakeParser(),
            llm=FlakyLLM(),
            recorder=recorder,
            max_batches=2,
            max_retries=0,
        )

    artifact_dir = task_dir / "compliance_extraction"
    assert (artifact_dir / "01_parsed_blocks.json").is_file()
    assert (artifact_dir / "02_candidates.json").is_file()
    assert (artifact_dir / "03_batches.json").is_file()
    assert (artifact_dir / "llm/call_001_output.json").is_file()
    assert (artifact_dir / "llm/call_002_output.json").is_file()
    summary = json.loads((artifact_dir / "summary.json").read_text())
    assert summary["status"] == "failed"
    assert summary["failed_stage"] == "llm"
    assert summary["stats"]["llm_total_calls"] == 2
    assert "llm.call.error" in (artifact_dir / "execution.jsonl").read_text()


def test_real_extractor_records_retry_attempts_and_call_metadata(tmp_path):
    task_dir = tmp_path / "task-001"
    tender = task_dir / "tender.docx"
    task_dir.mkdir()
    tender.write_bytes(b"tender")
    blocks = [StructuredBlock("b0001", "paragraph", "须提供营业执照。", "资格", 1)]

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
                    raise TimeoutError("temporary")
                except TimeoutError as exc:
                    raise ComplianceExtractionError("请求超时") from exc
            return [
                {
                    "name": "资格材料",
                    "rule": "须提供营业执照。",
                    "condition": None,
                    "source_block_ids": ["b0001"],
                }
            ]

    recorder = ComplianceExtractionRecorder(task_dir)
    extract_compliance_requirements_real(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
        llm=FlakyLLM(),
        recorder=recorder,
        max_retries=1,
    )

    artifact_dir = task_dir / "compliance_extraction"
    first = json.loads((artifact_dir / "llm/call_001_output.json").read_text())
    second_input = json.loads((artifact_dir / "llm/call_002_input.json").read_text())
    second = json.loads((artifact_dir / "llm/call_002_output.json").read_text())
    summary = json.loads((artifact_dir / "summary.json").read_text())
    assert first["status"] == "failed"
    assert first["error_type"] == "ComplianceExtractionError"
    assert second_input["attempt"] == 2
    assert second_input["retry"] is True
    assert second["status"] == "success"
    assert summary["stats"]["llm_total_calls"] == 2
    assert summary["stats"]["llm_retries"] == 1
    assert summary["stats"]["llm_failed_calls"] == 1
    assert summary["stats"]["schema_valid_calls"] == 1


def test_real_extractor_records_requirement_cache_hit(tmp_path):
    task_dir = tmp_path / "task-001"
    tender = task_dir / "tender.docx"
    task_dir.mkdir()
    tender.write_bytes(b"tender")
    blocks = [StructuredBlock("b0001", "paragraph", "须提供营业执照。", "资格", 1)]

    class FakeParser:
        def __init__(self):
            self.calls = 0

        def parse(self, path):
            self.calls += 1
            return blocks

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def extract(self, batch):
            self.calls += 1
            return [
                {
                    "name": "资格材料",
                    "rule": "须提供营业执照。",
                    "condition": None,
                    "source_block_ids": ["b0001"],
                }
            ]

    cache = InMemoryRequirementCache()
    parser = FakeParser()
    llm = FakeLLM()
    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    extract_compliance_requirements_real(
        metadata,
        parser=parser,
        llm=llm,
        cache=cache,
        recorder=ComplianceExtractionRecorder(task_dir),
    )

    second_recorder = ComplianceExtractionRecorder(tmp_path / "task-002")
    result = extract_compliance_requirements_real(
        metadata,
        parser=parser,
        llm=llm,
        cache=cache,
        recorder=second_recorder,
    )

    summary = json.loads((second_recorder.artifact_dir / "summary.json").read_text())
    raw = json.loads(
        (second_recorder.artifact_dir / "04_raw_requirements.json").read_text()
    )
    assert result
    assert parser.calls == 1
    assert llm.calls == 1
    assert summary["stats"]["cache_hit"] is True
    assert summary["stats"]["cache_elapsed_ms"] is not None
    assert raw["source"] == "requirements_cache"
