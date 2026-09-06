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
)
from app.models import FileMetadata


def test_compliance_mineru_flatten_keeps_raw_index_items_for_later_filtering():
    flattened = extraction_module._flatten_mineru_content_list(
        [
            {
                "type": "index",
                "content": "商务评审索引表\n对应页码",
                "page_idx": 2,
            },
            {"type": "text", "text": "投标函"},
        ]
    )

    assert [item["type"] for item in flattened] == ["index", "text"]
    assert flattened[0]["content"] == "商务评审索引表\n对应页码"


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


def test_file_requirement_candidates_keep_front_table_whole_and_exclude_irrelevant_regions():
    blocks = [
        block("b1", "heading", "第二章 投标人须知", "第二章 投标人须知", 1),
        block(
            "b2",
            "heading",
            "投标人须知前附表",
            "第二章 投标人须知",
            2,
        ),
        block(
            "b3",
            "table",
            "电子投标文件格式 | PDF\n文件大小 | 不得超过 200MB\n文件名称 | 应包含项目名称和投标人名称",
            "第二章 投标人须知",
            3,
        ),
        block(
            "b4",
            "heading",
            "投标文件递交",
            "第二章 投标人须知",
            4,
        ),
        block(
            "b5",
            "paragraph",
            "电子投标文件采用 PDF 格式上传。",
            "第二章 投标人须知",
            5,
        ),
        block("b6", "heading", "第六章 投标文件格式", "第六章 投标文件格式", 6),
        block("b7", "paragraph", "投标函模板字段和签章位置。", "第六章 投标文件格式", 7),
        block("b8", "heading", "第三章 评标办法", "第三章 评标办法", 8),
        block("b9", "paragraph", "商务评分满分 20 分。", "第三章 评标办法", 9),
        block("b10", "heading", "合同条款", "合同条款", 10),
        block("b11", "paragraph", "合同总价及付款方式。", "合同条款", 11),
    ]

    candidate_builder = getattr(
        extraction_module,
        "build_file_requirement_candidates",
        None,
    )
    assert candidate_builder is not None

    candidates = candidate_builder(blocks)
    assert candidates
    front_table_candidates = [
        candidate
        for candidate in candidates
        if "投标人须知前附表" in candidate.text
    ]
    assert len(front_table_candidates) == 1
    assert "不得超过 200MB" in front_table_candidates[0].text
    assert "应包含项目名称和投标人名称" in front_table_candidates[0].text
    assert any("电子投标文件采用 PDF 格式上传" in candidate.text for candidate in candidates)
    assert all(candidate.kind == "file_requirements" for candidate in candidates)
    candidate_text = "\n".join(candidate.text for candidate in candidates)
    assert "商务评分满分" not in candidate_text
    assert "合同总价及付款方式" not in candidate_text
    assert "投标函模板字段" not in candidate_text


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


def test_index_navigation_template_is_preserved_and_marked_for_later_exclusion():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2", "b3", "b4", "b5"],
        blocks=[
            StructuredBlock(
                "b1",
                "heading",
                "投标文件格式",
                "投标文件格式",
                1,
                heading_level=1,
            ),
            StructuredBlock(
                "b2",
                "heading",
                "商务评审索引表",
                "投标文件格式",
                2,
                heading_level=3,
            ),
            StructuredBlock(
                "b3",
                "table",
                "序号 | 评审因素 | 投标文件页码",
                "投标文件格式",
                3,
            ),
            StructuredBlock(
                "b4",
                "heading",
                "法定代表人身份证明",
                "投标文件格式",
                4,
                heading_level=3,
            ),
            StructuredBlock(
                "b5",
                "paragraph",
                "姓名：____",
                "投标文件格式",
                5,
            ),
        ],
        text="",
        order=1,
    )

    templates = extract_templates_from_regions([region])

    assert [item["name"] for item in templates] == [
        "商务评审索引表",
        "法定代表人身份证明",
    ]


@pytest.mark.parametrize(
    "navigation_name",
    ["商务评审索引表", "投标文件目录", "目录导航"],
)
def test_standalone_index_navigation_region_is_preserved_for_later_exclusion(
    navigation_name,
):
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2", "b3"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block("b2", "heading", navigation_name, "投标文件格式", 2),
            block("b3", "table", "序号 | 评审因素 | 投标文件页码", "投标文件格式", 3),
        ],
        text="",
        order=1,
    )

    templates = extract_templates_from_regions([region])

    assert [item["name"] for item in templates] == [navigation_name]
    assert extraction_module._ambiguous_regions([region]) == []


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


def test_template_attachment_extraction_uses_submission_semantics_for_materials():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标人应提交营业执照、开户证明及软件合法使用权证明。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == [
        "营业执照",
        "开户证明",
        "软件合法使用权证明",
    ]


def test_template_attachment_extraction_filters_non_material_items_from_explicit_list():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "附件：投标文件及营业执照复印件。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == ["营业执照复印件"]


def test_template_attachment_extraction_accepts_material_quantity_suffixes():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标人应提交营业执照原件1份及开户证明复印件各一份。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == ["营业执照原件1份", "开户证明复印件各一份"]


def test_template_attachment_extraction_preserves_conditional_material_requirements():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "如为代理商，须提供制造商授权证明；"
                "联合体投标时，需分别提交各成员单位的主体资格证明文件；"
                "非事业单位时，应提供营业执照扫描件。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == [
        "如为代理商，须提供制造商授权证明",
        "联合体投标时，需分别提交各成员单位的主体资格证明文件",
        "非事业单位时，应提供营业执照扫描件",
    ]


def test_template_attachment_extraction_preserves_long_supplier_condition():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标产品制造商注册地在境外，使用代理商投标且制造商无法盖章的，"
                "应提供委托签署权的相关证明材料。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == [
        "投标产品制造商注册地在境外，使用代理商投标且制造商无法盖章的，应提供委托签署权的相关证明材料"
    ]


def test_template_attachment_extraction_reads_materials_from_table_cells():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "table",
                "<table><tr><th>材料</th><th>要求</th></tr>"
                "<tr><td>资格证明</td><td>需提交开户证明及软件授权文件</td></tr></table>",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == ["开户证明", "软件授权文件"]


def test_template_attachment_extraction_carries_standalone_attachment_marker_to_next_block():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2", "b3"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block("b2", "paragraph", "附：", "投标文件格式", 2),
            block(
                "b3",
                "paragraph",
                "1.委托代理人的合法有效身份证明复印件或扫描件(如提供居民身份证，需同时提供正反面)",
                "投标文件格式",
                3,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == [
        "1.委托代理人的合法有效身份证明复印件或扫描件(如提供居民身份证，需同时提供正反面)"
    ]


def test_template_attachment_extraction_keeps_multiple_blocks_after_attachment_marker():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2", "b3", "b4"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block("b2", "paragraph", "附：", "投标文件格式", 2),
            block("b3", "paragraph", "1.营业执照复印件。", "投标文件格式", 3),
            block("b4", "paragraph", "2.开户证明文件。", "投标文件格式", 4),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == ["1.营业执照复印件", "2.开户证明文件"]


def test_template_attachment_extraction_does_not_emit_table_layout_separators():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "table",
                "<table><tr><td>附：</td><td>营业执照复印件</td></tr></table>",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == ["营业执照复印件"]


def test_template_attachment_extraction_does_not_treat_capability_as_material():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标人应提供服务能力和售后支持，不附加任何条件。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == []


def test_template_attachment_extraction_requires_a_material_entity_beyond_evidence_words():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标人应提供扫描件，并提交相关证明材料。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == []


def test_template_attachment_extraction_ignores_unrelated_document_prose():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "本单位承诺与贵公司的合同/协议约定一致，不得向其他方提供、披露；"
                "投标文件编制、签署并递交相关资料；"
                "还应报送审查工作需要的材料。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == []


def test_template_attachment_extraction_accepts_material_before_submission_action():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "营业执照复印件应随投标文件一并提交；软件合法使用权证明须附。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == [
        "营业执照复印件",
        "软件合法使用权证明",
    ]


def test_template_attachment_extraction_ignores_descriptive_document_prose():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "本保函作为（投标人）对（项目）的投标邀请而提供的投标保函。"
                "本保函有效期应不短于投标有效期。"
                "要求提供原件的，需提供文件原件。"
                "投标文件编制、签署并递交相关资料。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == []


def test_template_attachment_extraction_does_not_treat_bare_附_as_submission_action():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "前附表3.3.6投标报价具体要求，未按照招标文件要求进行报价。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == []


def test_template_attachment_extraction_ignores_process_and_contract_prose():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "我方在评标过程中根据评标委员会要求提供的符合相关规定的澄清文件，"
                "联合体递交投标文件，履行合同并处理相关事务。"
                "其他途径开具的电子投标保函，投标人须提供可在评标现场核验保函真实性的有效途径。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == []


def test_template_attachment_extraction_ignores_post_award_and_review_process_materials():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "合同签订后应提交验收报告；评标过程中应提供澄清材料。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == []


def test_template_attachment_condition_detection_requires_condition_grammar():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标人应当提交营业执照；如为代理商，须提交制造商授权证明。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == [
        "营业执照",
        "如为代理商，须提交制造商授权证明",
    ]


def test_template_attachment_extraction_keeps_qualification_evidence_names():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "paragraph",
                "投标人须提交项目经验报告及商业信誉证明。",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["attachments"] == ["项目经验报告", "商业信誉证明"]


def test_template_condition_is_retained_in_raw_template_source():
    region = FunctionalRegion(
        kind="templates",
        title="投标文件格式",
        section="投标文件格式",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标文件格式", "投标文件格式", 1),
            block(
                "b2",
                "heading",
                "法定代表人身份证明（如有）",
                "投标文件格式",
                2,
            ),
        ],
        text="",
        order=1,
    )

    template = extract_templates_from_regions([region])[0]

    assert template["name"] == "法定代表人身份证明"
    assert "（如有）" in template["body"]
    assert template["source"]["source_text"] == template["body"]


def test_template_fields_prefer_mineru_underline_runs_over_bare_colons():
    payload = [
        {"type": "text", "content": "投标人名称："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "单位性质："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "成立时间："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "经营期限："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "姓名："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "性别："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "年龄："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "职务："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "日期："},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "年"},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "月"},
        {"type": "text", "content": "                       ", "style": ["underline"]},
        {"type": "text", "content": "日"},
        {"type": "text", "content": "我方承诺如下内容："},
        {"type": "text", "content": "如我方中标："},
        {"type": "text", "content": "现承诺如下："},
        {"type": "text", "content": "附："},
        {"type": "text", "content": "即："},
        {"type": "text", "content": "包括但不限于："},
        {"type": "text", "content": "编制要求："},
        {"type": "text", "content": "复制、查阅和传播含有以下内容的信息："},
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == [
        "投标人名称",
        "单位性质",
        "成立时间",
        "经营期限",
        "姓名",
        "性别",
        "年龄",
        "职务",
        "日期",
    ]


def test_template_fields_read_underline_styles_inside_nested_paragraph_content():
    payload = [
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "法定代表人："},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                    {"type": "text", "content": "签发机关："},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                ]
            },
        }
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == ["法定代表人", "签发机关"]


def test_template_fields_read_underline_styles_from_flat_content_runs():
    payload = [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "content": "法定代表人："},
                {"type": "text", "content": "       ", "style": ["underline"]},
            ],
        }
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == ["法定代表人"]


def test_template_fields_use_underlined_semantic_text_without_placeholder_syntax():
    payload = [
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "投标人名称", "style": ["underline"]},
                    {"type": "text", "content": "（联系人）", "style": ["underline"]},
                    {
                        "type": "text",
                        "content": "复制、查阅和传播含有以下内容的信息",
                        "style": ["underline"],
                    },
                ]
            },
        }
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == ["投标人名称", "联系人"]


def test_template_fields_do_not_promote_narrative_before_blank_underline_to_field():
    payload = [
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "自愿组成："},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                    {"type": "text", "content": "现就联合体投标事宜订立如下协议："},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                    {"type": "text", "content": "投标人名称："},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                ]
            },
        }
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == ["投标人名称"]


def test_template_table_fields_require_a_structural_label_for_blank_cells():
    table = (
        "<table><tr><td>项目名称</td><td></td></tr>"
        "<tr><td></td><td></td></tr></table>"
    )

    blocks = [block("b1", "table", table, "模板", 1)]

    assert extraction_module._template_fields(blocks) == ["项目名称"]


def test_template_fields_keep_literal_placeholders_and_noun_phrase_fallbacks():
    block_value = (
        "成立时间：____年____月____日；"
        "委托期限：。；"
        "本单位名称：____；应答内容：____；"
        "我方承诺如下内容：；如我方中标：；现承诺如下："
    )

    blocks = [block("b1", "paragraph", block_value, "模板", 1)]

    assert extraction_module._template_fields(blocks) == [
        "成立时间",
        "委托期限",
        "本单位名称",
        "应答内容",
    ]


def test_template_fields_keep_parenthesized_input_slots():
    blocks = [
        block(
            "b1",
            "paragraph",
            "投标人名称：（盖章）；联系人：（请填写）",
            "模板",
            1,
        )
    ]

    assert extraction_module._template_fields(blocks) == ["投标人名称", "联系人"]


def test_template_fields_associate_literal_placeholder_blocks_with_labels():
    blocks = [
        block("b1", "paragraph", "法定代表人：", "模板", 1),
        block("b2", "paragraph", "________", "模板", 2),
    ]

    assert extraction_module._template_fields(blocks) == ["法定代表人"]


def test_template_fields_preserve_source_order_when_style_and_text_markers_mix():
    blocks = [
        block("b1", "paragraph", "项目名称：____", "模板", 1),
        block("b2", "paragraph", "法定代表人：", "模板", 2),
        StructuredBlock(
            "b3",
            "paragraph",
            "       ",
            "模板",
            3,
            metadata={"style": ["underline"]},
        ),
    ]

    assert extraction_module._template_fields(blocks) == ["项目名称", "法定代表人"]


def test_template_fields_read_html_table_placeholder_cells_without_using_style():
    table = (
        "<table><tbody>"
        "<tr><th>字段</th><th>填写内容</th></tr>"
        "<tr><td>申报人名称</td><td>【XX公司[投标人名称]】</td></tr>"
        "<tr><td>地址</td><td></td></tr>"
        "<tr><td>电话</td><td>________</td></tr>"
        "<tr><td>固定信息</td><td>北京市</td></tr>"
        "</tbody></table>"
    )
    blocks = [block("b1", "table", table, "模板", 1)]
    blocks.append(
        block("b2", "table", "身份证正面 | ______\n身份证反面 | ______", "模板", 2)
    )

    assert extraction_module._template_fields(blocks) == [
        "申报人名称",
        "地址",
        "电话",
        "身份证正面",
        "身份证反面",
    ]


def test_template_fields_do_not_attach_prose_parentheses_to_previous_label():
    blocks = [
        block("b1", "paragraph", "附：", "模板", 1),
        block(
            "b2",
            "paragraph",
            "1.委托代理人的合法有效身份证明复印件或扫描件(如提供中华人民共和国居民身份证的，需同时提供国徽面及人像面)",
            "模板",
            2,
        ),
        block("b3", "paragraph", "编制要求：", "模板", 3),
        block(
            "b4",
            "paragraph",
            "除本文件允许投标人进行填写的内容以外，投标人不得对本文件进行修改（含单位或者个人）。",
            "模板",
            4,
        ),
        block("b5", "paragraph", "投标人名称：", "模板", 5),
        StructuredBlock(
            "b6",
            "paragraph",
            "       ",
            "模板",
            6,
            metadata={"style": ["underline"]},
        ),
    ]

    assert extraction_module._template_fields(blocks) == ["投标人名称"]


def test_template_fields_reject_recipient_label_before_bracket_placeholder():
    blocks = [
        block("b1", "paragraph", "致：【XX公司[招标人名称]】：", "模板", 1),
        block("b2", "paragraph", "联系人：（请填写）", "模板", 2),
    ]

    assert extraction_module._template_fields(blocks) == ["联系人"]


def test_template_fields_extract_semantic_label_from_underlined_bracket_run():
    payload = [
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "本人"},
                    {
                        "type": "text",
                        "content": "【XX [法定代表人姓名]】",
                        "style": ["underline"],
                    },
                    {"type": "text", "content": "签字："},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                ]
            },
        }
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == ["法定代表人姓名", "签字"]


def test_template_fields_use_matrix_headers_for_blank_table_cells():
    table = (
        "<table><tr><th>序号</th><th>关键元器件名称</th><th>关键元器件型号</th>"
        "<th>生产厂商</th><th>备注</th></tr>"
        "<tr><td>1</td><td>CPU</td><td></td><td></td><td></td></tr>"
        "<tr><td>2</td><td>GPU</td><td></td><td></td><td></td></tr></table>"
    )

    blocks = [block("b1", "table", table, "模板", 1)]

    assert extraction_module._template_fields(blocks) == [
        "关键元器件型号",
        "生产厂商",
        "备注",
    ]


def test_template_fields_extract_inline_labels_from_html_table_cells():
    table = (
        "<table><tr><td>开户银行</td><td>名称：【XX[基本账户开户银行名称]】</td></tr>"
        "<tr><td>地址：【XX[基本账户开户银行地址]】</td></tr>"
        "<tr><td>电话：【XX[基本账户开户银行电话]】</td>"
        "<td>联系人及职务：【XX[姓名]，XX[职务]】</td></tr></table>"
    )

    blocks = [block("b1", "table", table, "模板", 1)]

    assert extraction_module._template_fields(blocks) == [
        "名称",
        "地址",
        "电话",
        "联系人及职务",
    ]


def test_template_fields_choose_detailed_header_over_group_header_and_total_row():
    table = (
        "<table><tr><th>序号</th><th colspan='2'>人员情况</th></tr>"
        "<tr><th>姓名</th><th>职务</th><th>联系方式</th></tr>"
        "<tr><td>示例</td><td>张三</td><td>法定代表人</td><td>13800000000</td></tr>"
        "<tr><td>1</td><td></td><td></td><td></td></tr>"
        "<tr><td>总计</td><td></td></tr></table>"
    )

    blocks = [block("b1", "table", table, "模板", 1)]

    assert extraction_module._template_fields(blocks) == [
        "姓名",
        "职务",
        "联系方式",
    ]


def test_template_fields_do_not_use_narrative_prefix_for_explicit_placeholder_label():
    payload = [
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {
                        "type": "text",
                        "content": "【XX公司、XX公司[所有成员单位名称]】",
                        "style": ["underline"],
                    },
                    {"type": "text", "content": "自愿组成："},
                    {
                        "type": "text",
                        "content": "【XX和XX[联合体名称]】",
                        "style": ["underline"],
                    },
                    {
                        "type": "text",
                        "content": "联合体，共同参加【2026年云网络项目[项目名称]】【XX标包[标包名称]】投标。",
                        "style": ["underline"],
                    },
                    {"type": "text", "content": "现就联合体投标事宜订立如下协议："},
                ]
            },
        },
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "1. "},
                    {
                        "type": "text",
                        "content": "【XX公司[某成员单位名称]】",
                        "style": ["underline"],
                    },
                    {"type": "text", "content": "为联合体牵头人。"},
                ]
            },
        },
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == [
        "所有成员单位名称",
        "联合体名称",
        "项目名称",
        "某成员单位名称",
    ]


def test_template_fields_recognize_date_slots_without_date_label():
    payload = [
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "____年____月____日"}
                ]
            },
        },
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "       ", "style": ["underline"]},
                    {"type": "text", "content": "年"},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                    {"type": "text", "content": "月"},
                    {"type": "text", "content": "       ", "style": ["underline"]},
                    {"type": "text", "content": "日"},
                ]
            },
        },
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)

    assert extraction_module._template_fields(blocks) == ["日期"]


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


def test_supplemental_material_keeps_complete_parenthetical_contract_evidence():
    region = FunctionalRegion(
        kind="supplemental_materials",
        title="投标产品资格要求",
        section="投标产品资格要求",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标产品资格要求", "投标产品资格要求", 1),
            block(
                "b2",
                "paragraph",
                "投标人须提供业绩证明。（需提供合同关键页扫描件，如为单项合同，应包括项目名称、金额页；"
                "如为框架合同，还需提供相应的采购订单或结算单据）。\n"
                "2.4 投标产品制造商资格要求。",
                "投标产品资格要求",
                2,
            ),
        ],
        text="",
        order=1,
    )

    materials = extract_supplemental_materials_from_regions([region])

    contract_pages = next(
        item for item in materials if item["name"] == "合同关键页"
    )
    assert contract_pages["material"] == (
        "（需提供合同关键页扫描件，如为单项合同，应包括项目名称、金额页；"
        "如为框架合同，还需提供相应的采购订单或结算单据）"
    )


@pytest.mark.parametrize(
    ("case_text", "expected", "forbidden"),
    [
        (
            "须提供合同关键页扫描件。（需提供合同关键页扫描件，如为单项合同，应包括项目名称。）后续章节内容。",
            "（需提供合同关键页扫描件，如为单项合同，应包括项目名称。）",
            "后续章节内容",
        ),
        (
            "须提供合同关键页扫描件。(需提供合同关键页扫描件,如为单项合同,应包括项目名称;合同签订日期页.)后续章节内容。",
            "(需提供合同关键页扫描件,如为单项合同,应包括项目名称;合同签订日期页.)",
            "后续章节内容",
        ),
        (
            "须提供合同关键页扫描件。（需提供合同关键页扫描件，如为单项合同，应包括项目名称、金额页；"
            "如为框架合同，应包括签字盖章页等，还需提供采购订单、结算单据或发票。）后续章节内容。",
            "（需提供合同关键页扫描件，如为单项合同，应包括项目名称、金额页；"
            "如为框架合同，应包括签字盖章页等，还需提供采购订单、结算单据或发票。）",
            "后续章节内容",
        ),
        (
            "须提供合同关键页扫描件，（需提供合同关键页扫描件，如为单项合同，应包括项目名称；"
            "后续章节开始，其他材料要求。",
            None,
            "后续章节开始，其他材料要求",
        ),
    ],
    ids=[
        "chinese_parentheses",
        "english_parentheses",
        "multiple_internal_punctuation",
        "incomplete_parentheses",
    ],
)
def test_supplemental_material_parenthesis_boundaries(
    case_text: str,
    expected: str | None,
    forbidden: str,
):
    region = FunctionalRegion(
        kind="supplemental_materials",
        title="投标产品资格要求",
        section="投标产品资格要求",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标产品资格要求", "投标产品资格要求", 1),
            block("b2", "paragraph", case_text, "投标产品资格要求", 2),
        ],
        text="",
        order=1,
    )

    contract_pages = next(
        item
        for item in extract_supplemental_materials_from_regions([region])
        if item["name"] == "合同关键页"
    )

    if expected is not None:
        assert contract_pages["material"] == expected
    assert forbidden not in contract_pages["material"]


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


def test_main_extractor_returns_complete_four_collection_result_and_artifacts(tmp_path):
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

    assert set(result) == {
        "templates",
        "project_requirements",
        "supplemental_materials",
        "file_requirements",
    }
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
            "08_file_requirement_candidates.json",
            "09_file_requirements.json",
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


def test_main_extractor_keeps_and_marks_navigation_template(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        block("b1", "heading", "投标文件格式", "投标文件格式", 1),
        block("b2", "heading", "商务评审索引表", "投标文件格式", 2),
        block(
            "b3",
            "table",
            "评审因素 | 投标文件组成 | 对应页码\n1 | 投标函 | 7",
            "投标文件格式",
            3,
        ),
        block("b4", "heading", "投标函", "投标文件格式", 4),
        block("b5", "paragraph", "投标人名称：____", "投标文件格式", 5),
    ]

    class FakeParser:
        def parse(self, path):
            return blocks

    result = extract_tender_compliance_objects(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=FakeParser(),
    )

    navigation = next(
        item for item in result["templates"] if item["name"] == "商务评审索引表"
    )
    assert navigation["compliance_excluded"] is True
    assert navigation["compliance_exclusion_reason"] == "navigation_content"
    assert result["templates"][-1]["name"] == "投标函"


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


def test_mineru_parser_requires_real_mineru_by_default(tmp_path):
    path = tmp_path / "tender.docx"
    path.write_bytes(b"not-a-docx")

    parser = extraction_module.MinerUDocumentParser(
        mineru_url="",
    )

    with pytest.raises(ComplianceExtractionError, match="MinerU"):
        parser.parse(path)

    assert parser.parser_name == "mineru"
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
    assert blocks[0].heading_level == 1
    assert blocks[0].metadata["mineru_raw_type"] == "text"
    assert blocks[0].metadata["mineru_source_index"] == 0
    assert parser.parse_diagnostics["mineru_raw_table_count"] == 1
    assert parser.parser_name == "mineru"
    assert parser.parse_diagnostics["mineru_called"] is True
    assert parser.parse_diagnostics["service_protocol"] == "mineru_tasks"
    assert "command" not in parser.cache_descriptor
    client.close()


def test_mineru_parser_recovers_docx_front_table_when_mineru_omits_it(tmp_path):
    path = tmp_path / "tender.docx"
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>投标人须知前附表</w:t></w:r></w:p>
    <w:p><w:r><w:t>说明</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>条款号</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>条款名称</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>编列内容</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>3.1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>投标文件组成</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>商务、技术、报价文件</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>3.4.1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>投标有效期</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>90天</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:t>总则</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "result_content_list.json",
            json.dumps(
                [
                    {"type": "text", "text": "投标人须知前附表", "text_level": 2},
                    {"type": "text", "text": "说明"},
                    {"type": "text", "text": "总则", "text_level": 1},
                ],
                ensure_ascii=False,
            ),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            return httpx.Response(
                202,
                json={
                    "task_id": "task-front-table-recovery",
                    "status_url": "https://mineru.example/tasks/task-front-table-recovery",
                    "result_url": "https://mineru.example/tasks/task-front-table-recovery/result",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/result"):
            return httpx.Response(200, content=archive_bytes.getvalue())
        if request.method == "GET" and request.url.path.endswith("task-front-table-recovery"):
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    parser = extraction_module.MinerUDocumentParser(
        mineru_url="https://mineru.example",
        mineru_backend="hybrid-engine",
        http_client=client,
        poll_interval_seconds=0,
    )

    blocks = parser.parse(path)
    front_table = next(block for block in blocks if block.type == "table")
    regions = identify_functional_regions(blocks)
    requirements = extract_project_requirements_from_regions(regions)

    assert front_table.metadata["source_recovery"] == "docx_front_table"
    assert front_table.metadata["table_body"].startswith("<table>")
    assert [item["value"] for item in requirements] == ["商务、技术、报价文件", "90天"]
    assert parser.parse_diagnostics["mineru_front_table_present"] is False
    assert parser.parse_diagnostics["project_front_table_recovered"] is True
    client.close()


def test_mineru_structure_drives_front_table_and_template_segmentation():
    payload = [
        {"type": "text", "text": "第二章 投标人须知", "text_level": 1},
        {"type": "text", "text": "投标人须知前附表", "text_level": 2},
        {
            "type": "table",
            "table_body": (
                "<table><tbody>"
                "<tr><th>项目</th><th>要求</th></tr>"
                "<tr><td>单个组成部分大小</td><td>不超过50MB</td></tr>"
                "<tr><td>投标有效期</td><td>90天</td></tr>"
                "</tbody></table>"
            ),
        },
        {"type": "text", "text": "3.5投标保证金", "text_level": 1},
        {"type": "text", "text": "无需递交投标保证金。"},
        {"type": "text", "text": "3.7投标文件的式样、密封和标记", "text_level": 1},
        {"type": "text", "text": "纸质文件规则仅适用特定情形。"},
        {"type": "text", "text": "第六章 投标文件格式", "text_level": 1},
        {"type": "text", "text": "18.1.8 法定代表人授权委托书", "text_level": 3},
        {"type": "text", "text": "委托事项：____"},
        {"type": "text", "text": "18.1.9 廉洁投标承诺书", "text_level": 3},
        {"type": "text", "text": "承诺内容：____"},
        {"type": "text", "text": "18.1.10 特定关系信息收集表", "text_level": 3},
        {"type": "text", "text": "关系信息：____"},
        {"type": "text", "text": "18.1.11 诉讼及仲裁情况", "text_level": 3},
        {"type": "text", "text": "近年情况：____"},
        {
            "type": "text",
            "text": "18.1.12 招标代理服务费支付承诺函",
            "text_level": 3,
        },
        {"type": "text", "text": "服务费承诺：____"},
        {"type": "text", "text": "18.1.13 资格审查资料", "text_level": 3},
        {"type": "text", "text": "资格资料：____"},
        {"type": "text", "text": "第七章 其他", "text_level": 1},
    ]

    blocks = extraction_module._blocks_from_mineru_payload(payload)
    regions = identify_functional_regions(blocks)
    requirements = extract_project_requirements_from_regions(regions)
    templates = extract_templates_from_regions(regions)

    assert len(requirements) == 2
    assert any("50MB" in item["requirement"] for item in requirements)
    assert any("90天" in item["requirement"] for item in requirements)
    names = [item["name"] for item in templates]
    assert names == [
        "法定代表人授权委托书",
        "廉洁投标承诺书",
        "特定关系信息收集表",
        "诉讼及仲裁情况",
        "招标代理服务费支付承诺函",
        "资格审查资料",
    ]
    assert all("3.5" not in name and "3.7" not in name for name in names)
    assert all(item["body"].strip() != item["name"] for item in templates)
    proxy_index = names.index("招标代理服务费支付承诺函")
    assert "服务费承诺" in templates[proxy_index]["body"]
    assert "资格资料" not in templates[proxy_index]["body"]


def test_structural_parent_heading_closes_previous_template_without_parent_template():
    blocks = [
        StructuredBlock(
            "b1",
            "heading",
            "第六章 投标文件格式",
            "第六章 投标文件格式",
            1,
            heading_level=1,
        ),
        StructuredBlock(
            "b2",
            "heading",
            "18.1.22 ★知识产权不侵权承诺函",
            "第六章 投标文件格式",
            2,
            heading_level=3,
        ),
        block(
            "b3",
            "paragraph",
            "我方承诺不存在知识产权侵权行为。",
            "第六章 投标文件格式",
            3,
        ),
        StructuredBlock(
            "b4",
            "heading",
            "二、特定关系信息收集表",
            "第六章 投标文件格式",
            4,
            heading_level=2,
        ),
        block(
            "b5",
            "paragraph",
            "特定关系信息表内容。",
            "第六章 投标文件格式",
            5,
        ),
        StructuredBlock(
            "b6",
            "heading",
            "18.1.23 技术投标文件封面",
            "第六章 投标文件格式",
            6,
            heading_level=3,
        ),
        block(
            "b7",
            "paragraph",
            "技术投标文件封面内容。",
            "第六章 投标文件格式",
            7,
        ),
    ]
    region = FunctionalRegion(
        kind="templates",
        title="第六章 投标文件格式",
        section="第六章 投标文件格式",
        block_ids=[block.block_id for block in blocks],
        blocks=blocks,
        text="\n".join(block.text for block in blocks),
        order=1,
    )

    templates = extract_templates_from_regions([region])

    intellectual_property = next(
        template
        for template in templates
        if template["name"] == "知识产权不侵权承诺函"
    )
    assert intellectual_property["block_ids"] == ["b2", "b3"]
    assert "特定关系信息收集表" not in intellectual_property["body"]
    assert all(
        template["name"] != "二、特定关系信息收集表"
        for template in templates
    )
    assert next(
        template
        for template in templates
        if template["name"] == "技术投标文件封面"
    )["block_ids"] == ["b6", "b7"]


def test_project_requirements_parse_mineru_html_table_rows():
    blocks = extraction_module._blocks_from_mineru_payload(
        [
            {"type": "text", "text": "投标人须知前附表", "text_level": 2},
            {
                "type": "table",
                "table_body": (
                    "<table><tr><td>投标文件组成</td><td>商务、技术文件</td></tr>"
                    "<tr><td>总容量</td><td>不超过500MB</td></tr></table>"
                ),
            },
        ]
    )
    region = identify_functional_regions(blocks)[0]

    requirements = extract_project_requirements_from_regions([region])

    assert [item["requirement"] for item in requirements] == [
        "投标文件组成 | 商务、技术文件",
        "总容量 | 不超过500MB",
    ]


def test_mineru_v2_nested_title_and_table_are_flattened_without_losing_structure():
    blocks = extraction_module._blocks_from_mineru_payload(
        [
            {
                "type": "title",
                "anchor": "_Toc1",
                "content": {
                    "title_content": [
                        {"type": "text", "content": "投标人须知前附表", "style": ["bold"]}
                    ],
                    "level": 2,
                },
            },
            {
                "type": "table",
                "content": {
                    "table_caption": [],
                    "html": (
                        "<table><tr><td>总容量</td><td>不超过500MB</td></tr></table>"
                    ),
                    "table_type": "simple_table",
                },
            },
        ]
    )

    assert [block.type for block in blocks] == ["heading", "table"]
    assert blocks[0].text == "投标人须知前附表"
    assert blocks[0].heading_level == 2
    assert blocks[0].metadata["anchor"] == "_Toc1"
    assert blocks[1].text.startswith("<table>")
    assert blocks[1].metadata["table_type"] == "simple_table"


def test_mineru_zip_prefers_structured_content_list_v2():
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "tender_content_list.json",
            json.dumps([{"type": "text", "text": "legacy"}], ensure_ascii=False),
        )
        archive.writestr(
            "tender_content_list_v2.json",
            json.dumps([{"type": "title", "content": {"title_content": [], "level": 1}}], ensure_ascii=False),
        )

    payload = extraction_module.MinerUDocumentParser._content_list_from_zip(
        archive_bytes.getvalue()
    )

    assert payload[0]["type"] == "title"


def test_supplemental_material_source_text_is_evidence_snippet_not_whole_block():
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
                "投标人应具有良好的商业信誉；如非事业单位，须提供有效的营业执照正本或副本扫描件；"
                "能够提供售后服务。",
                "招标公告",
                2,
            ),
        ],
        text="",
        order=1,
    )

    material = extract_supplemental_materials_from_regions([region])[0]

    assert material["source"]["block_ids"] == ["b2"]
    assert material["source"]["source_text"] == material["material"]
    assert "商业信誉" not in material["source"]["source_text"]


def test_supplemental_evidence_uses_smallest_structural_clause_in_mixed_mineru_block():
    region = FunctionalRegion(
        kind="supplemental_materials",
        title="投标产品资格要求",
        section="投标产品资格要求",
        block_ids=["b1", "b2"],
        blocks=[
            block("b1", "heading", "投标产品资格要求", "投标产品资格要求", 1),
            block(
                "b2",
                "paragraph",
                "2.3.6 投标产品应满足以下关键技术指标：\n"
                "2.3.3 业绩要求：投标人须提供同类型业绩证明材料。\n"
                "2.4.1 制造商应提供合法有效的登记（或注册）证明文件。",
                "投标产品资格要求",
                2,
            ),
        ],
        text="",
        order=1,
    )

    materials = extract_supplemental_materials_from_regions([region])

    by_name = {item["name"]: item for item in materials}
    assert by_name["业绩证明"]["material"] == "投标人须提供同类型业绩证明材料"
    assert by_name["制造商登记证明"]["material"] == (
        "2.4.1 制造商应提供合法有效的登记（或注册）证明文件"
    )


def test_parse_cache_key_distinguishes_mineru_service_configurations(tmp_path):
    path = tmp_path / "tender.docx"
    path.write_bytes(b"same bytes")
    mineru_a = extraction_module.MinerUDocumentParser(
        mineru_url="https://mineru.example",
    )
    mineru_b = extraction_module.MinerUDocumentParser(
        mineru_url="https://other-mineru.example",
    )

    assert extraction_module._parsed_document_cache_key(path, mineru_a) != (
        extraction_module._parsed_document_cache_key(path, mineru_b)
    )
