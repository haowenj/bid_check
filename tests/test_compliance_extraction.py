from __future__ import annotations

import json
import io
import zipfile
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import httpx

import app.compliance_extraction as extraction_module
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import (
    CandidateWindow,
    ComplianceExtractionError,
    FunctionalRegion,
    InMemoryRequirementCache,
    StructuredBlock,
    apply_project_applicability,
    extract_project_requirements_from_regions,
    extract_supplemental_materials_from_regions,
    extract_templates_from_regions,
    extract_tender_compliance_objects,
    identify_functional_regions,
    normalize_tender_extraction_sources,
    parse_docx_document,
)
from app.models import FileMetadata


def block(
    block_id: str,
    block_type: str,
    text: str,
    section: str,
    order: int,
) -> StructuredBlock:
    return StructuredBlock(block_id, block_type, text, section, order)


def test_functional_regions_are_semantic_and_stop_at_excluded_sections():
    blocks = [
        block("b1", "heading", "附件 响应文件格式", "附件 响应文件格式", 1),
        block("b2", "heading", "投标函", "附件 响应文件格式", 2),
        block("b3", "paragraph", "投标人名称：____", "附件 响应文件格式", 3),
        block("b4", "heading", "投标人须知前附表", "投标人须知前附表", 4),
        block("b5", "table", "投标有效期 | 90 天", "投标人须知前附表", 5),
        block("b6", "heading", "第三部分 评标办法", "第三部分 评标办法", 6),
        block("b7", "paragraph", "商务评分满分 20 分。", "第三部分 评标办法", 7),
        block("b8", "heading", "投标产品资格要求", "投标产品资格要求", 8),
        block("b9", "paragraph", "须随投标文件提供制造商登记证明。", "投标产品资格要求", 9),
    ]

    regions = identify_functional_regions(blocks)

    assert [(region.kind, region.title) for region in regions] == [
        ("templates", "附件 响应文件格式"),
        ("project_requirements", "投标人须知前附表"),
        ("supplemental_materials", "投标产品资格要求"),
    ]
    assert regions[0].block_ids == ["b1", "b2", "b3"]
    assert "商务评分" not in "\n".join(region.text for region in regions)


def test_plain_paragraph_front_table_title_is_recognized_but_toc_entry_is_not():
    blocks = [
        block("b1", "heading", "第二章 投标人须知", "第二章 投标人须知", 1),
        block("b2", "paragraph", "投标人须知前附表", "第二章 投标人须知", 2),
        block("b3", "table", "投标有效期 | 90 天", "第二章 投标人须知", 3),
        block("b4", "heading", "第六章 投标文件格式", "第六章 投标文件格式", 4),
        block("b5", "paragraph", "投标函 PAGEREF _Toc123 \\h 10", "第六章 投标文件格式", 5),
    ]

    regions = identify_functional_regions(blocks)

    assert [(region.kind, region.title) for region in regions] == [
        ("project_requirements", "投标人须知前附表")
    ]


def test_template_is_one_complete_contiguous_check_object():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="附件 投标文件格式",
        block_ids=["b1", "b2", "b3", "b4", "b5"],
        blocks=[
            block("b1", "heading", "投标文件格式", "附件 投标文件格式", 1),
            block("b2", "heading", "法定代表人身份证明", "附件 投标文件格式", 2),
            block("b3", "paragraph", "姓名：____；职务：____。", "附件 投标文件格式", 3),
            block("b4", "table", "身份证正面 | ______\n身份证反面 | ______", "附件 投标文件格式", 4),
            block("b5", "paragraph", "附：法定代表人身份证复印件。", "附件 投标文件格式", 5),
        ],
        text="",
        order=1,
    )

    templates = extract_templates_from_regions([region])

    assert len(templates) == 1
    template = templates[0]
    assert template["name"] == "法定代表人身份证明"
    assert template["block_ids"] == ["b2", "b3", "b4", "b5"]
    assert "姓名：____" in template["body"]
    assert template["tables"] == [
        {
            "block_id": "b4",
            "text": "身份证正面 | ______\n身份证反面 | ______",
            "metadata": {},
        }
    ]
    assert "姓名" in template["fields"]
    assert "职务" in template["fields"]
    assert template["attachments"] == ["法定代表人身份证复印件"]
    assert template["source"]["source_text"] == template["body"]


def test_template_attachment_extraction_ignores_attachment_word_and附加_clause():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="附件 投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "附件 投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标函\n有关附件，我方不提出任何附加条件。\n附：法定代表人身份证复印件。",
                "附件 投标文件格式",
                2,
            ),
        ],
        text="投标文件格式\n投标函\n有关附件，我方不提出任何附加条件。\n附：法定代表人身份证复印件。",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == ["法定代表人身份证复印件"]


def test_template_does_not_become_one_requirement_per_block():
    region = FunctionalRegion(
        kind="templates",
        title="响应文件格式",
        section="响应文件格式",
        block_ids=["b1", "b2", "b3"],
        blocks=[
            block("b1", "heading", "响应文件格式", "响应文件格式", 1),
            block("b2", "heading", "投标函", "响应文件格式", 2),
            block("b3", "paragraph", "项目名称：____；投标人名称：____", "响应文件格式", 3),
        ],
        text="",
        order=1,
    )

    templates = extract_templates_from_regions([region])

    assert len(templates) == 1
    assert templates[0]["block_ids"] == ["b2", "b3"]


def test_flattened_paragraph_titles_segment_complete_templates():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2", "b3", "b4", "b5"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block("b2", "paragraph", "投标函", "投标文件格式", 2),
            block("b3", "paragraph", "投标人名称：____", "投标文件格式", 3),
            block("b4", "paragraph", "法定代表人身份证明", "投标文件格式", 4),
            block("b5", "paragraph", "姓名：____", "投标文件格式", 5),
        ],
        text="",
        order=1,
    )

    templates = extract_templates_from_regions([region])

    assert [(item["name"], item["block_ids"]) for item in templates] == [
        ("投标函", ["b2", "b3"]),
        ("法定代表人身份证明", ["b4", "b5"]),
    ]


def test_project_requirements_keep_only_file_compilation_rows():
    region = FunctionalRegion(
        kind="project_requirements",
        title="项目专用表",
        section="项目专用表",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "项目专用表", "项目专用表", 1),
            block(
                "b2",
                "table",
                "投标文件组成 | 商务、技术、报价文件\n"
                "各组成部分 | 分别编制\n"
                "单个组成部分大小 | 不超过 50MB\n"
                "总容量 | 不超过 500MB\n"
                "投标有效期 | 90 天\n"
                "投标保证金 | 无需递交投标保证金\n"
                "备选方案 | 不允许\n"
                "报价 | 保留两位小数\n"
                "人员经验 | 具有丰富项目经验\n"
                "履约支持 | 提供 7×24 小时服务",
                "项目专用表",
                2,
            ),
        ],
        text="",
        order=1,
    )

    requirements = extract_project_requirements_from_regions([region])
    text = "\n".join(item["requirement"] for item in requirements)

    assert len(requirements) == 8
    assert "不超过 50MB" in text
    assert "500MB" in text
    assert "无需递交投标保证金" in text
    assert "丰富项目经验" not in text
    assert "7×24" not in text
    assert all(item["source"]["block_ids"] == ["b2"] for item in requirements)


def test_supplemental_materials_only_include_explicit_submission_evidence():
    regions = [
        FunctionalRegion(
            kind="supplemental_materials",
            title="投标人资格要求",
            section="投标人资格要求",
            block_ids=["b1", "b2", "b3", "b4"],
            blocks=[
                block("b1", "heading", "投标人资格要求", "投标人资格要求", 1),
                block("b2", "paragraph", "须随投标文件提供营业执照或事业单位法人证书。", "投标人资格要求", 2),
                block("b3", "paragraph", "提供指定时间范围内的业绩证明及合同关键页。", "投标人资格要求", 3),
                block("b4", "paragraph", "具有良好的商业信誉并能够提供 7×24 小时服务。", "投标人资格要求", 4),
            ],
            text="",
            order=1,
        )
    ]

    materials = extract_supplemental_materials_from_regions(regions)

    assert [item["name"] for item in materials] == [
        "营业执照",
        "业绩证明",
        "合同关键页",
    ]
    assert all("商业信誉" not in item["material"] for item in materials)
    assert all(item["source"]["section"] == "投标人资格要求" for item in materials)


def test_supplemental_materials_can_extract_evidence_from_mixed_qualification_block():
    region = FunctionalRegion(
        kind="supplemental_materials",
        title="招标公告",
        section="招标公告",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "招标公告", "招标公告", 1),
            block(
                "b2",
                "paragraph",
                "投标人应具有良好的银行资信和商业信誉，如非事业单位，须提供有效的营业执照正本或副本扫描件；"
                "能够提供 7×24 小时服务。",
                "招标公告",
                2,
            ),
        ],
        text="",
        order=1,
    )

    materials = extract_supplemental_materials_from_regions([region])

    assert [item["name"] for item in materials] == ["营业执照"]
    assert "须提供" in materials[0]["material"]
    assert "商业信誉" not in materials[0]["material"]


def _template(name: str, block_id: str) -> dict:
    return {
        "id": f"t-{block_id}",
        "name": name,
        "section": "投标文件格式",
        "block_ids": [block_id],
        "body": name,
        "tables": [],
        "fields": [],
        "attachments": [],
        "source": {
            "section": "投标文件格式",
            "block_ids": [block_id],
            "source_text": name,
        },
    }


def test_project_specific_applicability_filters_bond_and_paper_templates():
    templates = [
        _template("投标保证金缴纳凭证", "b1"),
        _template("纸质投标文件正本密封", "b2"),
        {**_template("投标函", "b3"), "body": "投标函及通用投标保证金说明"},
    ]
    project_requirements = [
        {
            "id": "p1",
            "requirement": "投标保证金 | 无需递交投标保证金",
            "value": "无需递交投标保证金",
            "source": {"section": "前附表", "block_ids": ["p1"], "source_text": ""},
        },
        {
            "id": "p2",
            "requirement": "递交方式 | 只需上传一份加密电子投标文件",
            "value": "只需上传一份加密电子投标文件",
            "source": {"section": "前附表", "block_ids": ["p2"], "source_text": ""},
        },
    ]

    filtered, report = apply_project_applicability(templates, project_requirements)

    assert [item["name"] for item in filtered] == ["投标函"]
    assert {item["reason"] for item in report} == {
        "project_no_bid_bond",
        "project_electronic_only",
    }


def test_source_normalization_rebuilds_text_from_real_blocks():
    blocks = [
        block("b1", "paragraph", "招标文件原始正文。", "真实章节", 1),
        block("b2", "paragraph", "第二个连续块。", "真实章节", 2),
    ]
    result = {
        "templates": [
            {
                **_template("模板", "b1"),
                "block_ids": ["b1", "b2"],
                "source": {
                    "section": "模型伪造章节",
                    "block_ids": ["b2", "b1"],
                    "source_text": "模型伪造文本",
                },
            }
        ],
        "project_requirements": [],
        "supplemental_materials": [],
    }

    normalized = normalize_tender_extraction_sources(result, blocks)

    assert normalized["templates"][0]["block_ids"] == ["b1", "b2"]
    assert normalized["templates"][0]["source"] == {
        "section": "真实章节",
        "block_ids": ["b1", "b2"],
        "source_text": "招标文件原始正文。\n第二个连续块。",
    }
    with pytest.raises(ComplianceExtractionError, match="block_id"):
        normalize_tender_extraction_sources(
            {
                "templates": [
                    {
                        **_template("模板", "b1"),
                        "source": {"block_ids": ["missing"]},
                    }
                ],
                "project_requirements": [],
                "supplemental_materials": [],
            },
            blocks,
        )


def test_llm_schema_accepts_only_narrow_object_protocol():
    valid = {
        "templates": [{"name": "投标函", "source_block_ids": ["b1"]}],
        "project_requirements": [
            {
                "requirement": "投标有效期 | 90 天",
                "value": "90 天",
                "source_block_ids": ["b2"],
            }
        ],
        "supplemental_materials": [
            {
                "name": "营业执照",
                "material": "须随投标文件提供营业执照。",
                "source_block_ids": ["b3"],
            }
        ],
    }
    assert extraction_module._coerce_object_output(valid) == valid

    with pytest.raises(ComplianceExtractionError, match="未允许字段"):
        extraction_module._coerce_object_output(
            {
                "templates": [
                    {
                        "name": "投标函",
                        "source_block_ids": ["b1"],
                        "checks": ["不得出现规则编译字段"],
                    }
                ],
                "project_requirements": [],
                "supplemental_materials": [],
            }
        )
    with pytest.raises(ComplianceExtractionError, match="顶层字段"):
        extraction_module._coerce_object_output(
            {
                **valid,
                "requirements": [],
            }
        )


def test_openai_prompt_limits_llm_to_three_object_collections(monkeypatch):
    captured = {}

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
                                "content": '{"templates": [], "project_requirements": [], "supplemental_materials": []}'
                            }
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    extraction_module.OpenAICompatibleLLM(api_key="test-key").extract(
        [CandidateWindow(["b1"], "响应文件格式", "投标函\n投标人名称：____", 1)]
    )

    prompt = captured["payload"]["messages"][1]["content"]
    assert '"templates":[]' in prompt
    assert "不得生成 check_type" in prompt
    assert "scope" in prompt
    assert "不得把模板编译成自然语言规则" in prompt
    assert "name、rule、condition" not in prompt


def test_main_extractor_returns_complete_three_collection_result_and_artifacts(tmp_path):
    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    tender = task_dir / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        block("b1", "heading", "响应文件格式", "响应文件格式", 1),
        block("b2", "heading", "投标函", "响应文件格式", 2),
        block("b3", "paragraph", "投标人名称：____", "响应文件格式", 3),
        block("b4", "heading", "投标人须知前附表", "投标人须知前附表", 4),
        block("b5", "table", "投标有效期 | 90 天\n递交方式 | 只需上传电子投标文件", "投标人须知前附表", 5),
        block("b6", "heading", "投标人资格要求", "投标人资格要求", 6),
        block("b7", "paragraph", "须随投标文件提供营业执照。", "投标人资格要求", 7),
    ]

    class FakeParser:
        def parse(self, path):
            return blocks

    recorder = ComplianceExtractionRecorder(task_dir)
    result = extract_tender_compliance_objects(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
        recorder=recorder,
    )

    assert set(result) == {"templates", "project_requirements", "supplemental_materials"}
    assert "requirements" not in result
    assert [item["name"] for item in result["templates"]] == ["投标函"]
    assert result["templates"][0]["body"] == "投标函\n投标人名称：____"
    assert result["project_requirements"][0]["value"] == "90 天"
    assert result["supplemental_materials"][0]["name"] == "营业执照"
    assert result["templates"][0]["source"]["source_text"]
    artifact_dir = task_dir / "compliance_extraction"
    assert all(
        (artifact_dir / name).is_file()
        for name in (
            "01_parsed_blocks.json",
            "02_functional_regions.json",
            "03_templates.json",
            "04_project_requirements.json",
            "05_supplemental_materials.json",
            "06_filter_report.json",
            "07_result.json",
            "summary.json",
            "execution.jsonl",
        )
    )
    summary = json.loads((artifact_dir / "summary.json").read_text())
    assert summary["stats"]["template_count"] == 1
    assert summary["stats"]["project_requirement_count"] == 2
    assert summary["stats"]["supplemental_material_count"] == 1
    assert summary["stats"]["llm_total_calls"] == 0


def test_llm_template_segments_replace_coarse_fallback_and_ignore_materials_in_template_region(
    tmp_path,
):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        block("b1", "heading", "响应文件格式", "响应文件格式", 1),
        block("b2", "paragraph", "模板一\n须提供营业执照。", "响应文件格式", 2),
        block("b3", "paragraph", "模板二", "响应文件格式", 3),
    ]

    class Parser:
        def parse(self, path):
            return blocks

    class LLM:
        model = "test-model"

        def extract(self, batch):
            return {
                "templates": [
                    {"name": "投标函", "source_block_ids": ["b2"]},
                    {"name": "授权委托书", "source_block_ids": ["b3"]},
                ],
                "project_requirements": [],
                "supplemental_materials": [
                    {
                        "name": "营业执照",
                        "material": "须提供营业执照。",
                        "source_block_ids": ["b2"],
                    }
                ],
            }

    result = extract_tender_compliance_objects(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=Parser(),
        llm=LLM(),
    )

    assert [item["name"] for item in result["templates"]] == [
        "投标函",
        "授权委托书",
    ]
    assert result["supplemental_materials"] == []


def test_main_extractor_reuses_parser_and_result_cache(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        block("b1", "heading", "投标文件格式", "投标文件格式", 1),
        block("b2", "heading", "投标函", "投标文件格式", 2),
        block("b3", "paragraph", "投标人名称：____", "投标文件格式", 3),
    ]

    class FakeParser:
        def __init__(self):
            self.calls = 0

        def parse(self, path):
            self.calls += 1
            return blocks

    parser = FakeParser()
    cache = InMemoryRequirementCache()
    parser_cache = InMemoryRequirementCache()
    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    first = extract_tender_compliance_objects(
        metadata, parser=parser, cache=cache, parser_cache=parser_cache
    )
    second_recorder = ComplianceExtractionRecorder(tmp_path / "task-002")
    second = extract_tender_compliance_objects(
        metadata,
        parser=parser,
        cache=cache,
        parser_cache=parser_cache,
        recorder=second_recorder,
    )

    assert first == second
    assert parser.calls == 1
    summary = json.loads((second_recorder.artifact_dir / "summary.json").read_text())
    assert summary["stats"]["cache_hit"] is True
    assert summary["stats"]["parser_cache_hit"] is False


def test_ambiguous_llm_failure_keeps_structural_artifacts(tmp_path):
    task_dir = tmp_path / "task-003"
    task_dir.mkdir()
    tender = task_dir / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        block("b1", "heading", "响应文件格式", "响应文件格式", 1),
        block("b2", "paragraph", "投标人名称：____", "响应文件格式", 2),
    ]

    class FakeParser:
        def parse(self, path):
            return blocks

    class FailingLLM:
        model = "test-model"

        def extract(self, batch):
            raise ComplianceExtractionError("模拟对象识别失败")

    recorder = ComplianceExtractionRecorder(task_dir)
    with pytest.raises(ComplianceExtractionError, match="对象识别失败"):
        extract_tender_compliance_objects(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=FakeParser(),
            llm=FailingLLM(),
            recorder=recorder,
            max_retries=0,
        )

    artifact_dir = task_dir / "compliance_extraction"
    assert (artifact_dir / "01_parsed_blocks.json").is_file()
    assert (artifact_dir / "02_functional_regions.json").is_file()
    assert (artifact_dir / "03_templates.json").is_file() is False
    assert (artifact_dir / "llm/call_001_output.json").is_file()
    summary = json.loads((artifact_dir / "summary.json").read_text())
    assert summary["status"] == "failed"
    assert summary["stats"]["llm_total_calls"] == 1
    assert summary["stats"]["llm_failed_calls"] == 1


def test_deterministic_fallback_has_no_execution_rule_fields():
    result = extraction_module.DeterministicComplianceLLM().extract(
        [CandidateWindow(["b1"], "投标文件格式", "投标函\n投标人名称：____", 1)]
    )

    assert result == {
        "templates": [{"name": "投标函", "source_block_ids": ["b1"]}],
        "project_requirements": [],
        "supplemental_materials": [],
    }


def test_parse_docx_recovers_order_and_table_as_structured_blocks(tmp_path):
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>响应文件格式</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>投标函</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>字段</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>填写</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    ).encode()
    path = tmp_path / "tender.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)

    blocks = parse_docx_document(path)

    assert [item.type for item in blocks] == ["heading", "paragraph", "table"]
    assert blocks[0].section == "响应文件格式"
    assert blocks[2].text == "字段 | 填写"


def test_mineru_parser_requires_real_mineru_by_default(tmp_path):
    path = tmp_path / "tender.docx"
    path.write_bytes(b"not-a-docx")

    parser = extraction_module.MinerUDocumentParser(
        command="",
        mineru_url="",
        allow_docx_fallback=False,
    )

    with pytest.raises(ComplianceExtractionError, match="MinerU"):
        parser.parse(path)

    assert parser.parser_name == "mineru"
    assert parser.parse_diagnostics["mineru_called"] is False


def test_docx_fallback_is_explicit_and_is_not_called_as_mineru(tmp_path):
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    path = tmp_path / "tender.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)

    parser = extraction_module.MinerUDocumentParser(
        command="",
        mineru_url="",
        allow_docx_fallback=True,
    )
    blocks = parser.parse(path)

    assert blocks[0].text == "正文"
    assert parser.parser_name == "docx_fallback"
    assert parser.parse_diagnostics["mineru_called"] is False


def test_mineru_service_parser_uses_existing_tasks_protocol_and_preserves_metadata(tmp_path):
    path = tmp_path / "tender.docx"
    path.write_bytes(b"document bytes")
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "result_content_list.json",
            json.dumps(
                [
                    {"type": "text", "text": "第一章 投标文件格式", "text_level": 1, "page_idx": 0},
                    {"type": "text", "text": "投标函", "text_level": 2, "page_idx": 0},
                    {"type": "text", "text": "投标人名称：____", "page_idx": 0},
                    {"type": "table", "table_body": "<table><tr><td>字段</td></tr></table>", "page_idx": 0},
                    {"type": "image", "img_path": "images/001.jpg", "page_idx": 0},
                ],
                ensure_ascii=False,
            ),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            return httpx.Response(
                202,
                json={
                    "task_id": "task-1",
                    "status_url": "https://mineru.example/tasks/task-1",
                    "result_url": "https://mineru.example/tasks/task-1/result",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                content=archive_bytes.getvalue(),
                headers={"content-type": "application/zip"},
            )
        if request.method == "GET" and request.url.path.endswith("task-1"):
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    parser = extraction_module.MinerUDocumentParser(
        command="",
        mineru_url="https://mineru.example",
        mineru_backend="hybrid-engine",
        http_client=client,
        poll_interval_seconds=0,
    )
    blocks = parser.parse(path)

    assert [block.type for block in blocks] == [
        "heading",
        "heading",
        "paragraph",
        "table",
        "image",
    ]
    assert blocks[0].metadata["text_level"] == 1
    assert blocks[3].text.startswith("<table>")
    assert blocks[4].metadata["img_path"] == "images/001.jpg"
    assert parser.parser_name == "mineru"
    assert parser.parse_diagnostics["mineru_called"] is True
    assert parser.parse_diagnostics["service_protocol"] == "pdf_trans_tasks"
    client.close()


def test_parse_cache_key_distinguishes_mineru_from_explicit_docx_fallback(tmp_path):
    path = tmp_path / "tender.docx"
    path.write_bytes(b"same bytes")
    mineru = extraction_module.MinerUDocumentParser(
        command="",
        mineru_url="https://mineru.example",
    )
    fallback = extraction_module.MinerUDocumentParser(
        command="",
        mineru_url="",
        allow_docx_fallback=True,
    )

    assert extraction_module._parsed_document_cache_key(path, mineru) != (
        extraction_module._parsed_document_cache_key(path, fallback)
    )
