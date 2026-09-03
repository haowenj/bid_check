from __future__ import annotations

from app.navigation_content import (
    classify_navigation_item,
    filter_navigation_sections,
    filter_navigation_templates,
)


def _navigation_template(name: str, body: str) -> dict:
    return {
        "id": f"template-{name}",
        "name": name,
        "section": "第六章 投标文件格式",
        "body": body,
        "source": {"source_text": body, "block_ids": ["b1"]},
        "tables": [],
    }


def _section(section_id: str, title: str, blocks: list[dict]) -> dict:
    return {
        "section_id": section_id,
        "title": title,
        "path": [title],
        "blocks": blocks,
        "child_sections": [],
    }


def test_classifies_index_table_by_navigation_purpose_not_exact_title():
    item = _navigation_template(
        "初步评审索引表",
        "初步评审索引表\n条款号 | 评审因素 | 投标文件组成 | 对应页码 | 备注或者说明\n"
        "1 | 投标函 | 5.投标函 | 7 | /\n"
        "2 | 资格要求 | 13.1资格审查资料 | 19 | /",
    )

    classification = classify_navigation_item(item)

    assert classification["is_navigation"] is True
    assert classification["reason"] == "navigation_content"
    assert "title:navigation" in classification["signals"]
    assert "table:page_reference" in classification["signals"]


def test_classifies_toc_field_and_plain_directory_rows_without_business_title():
    toc = _navigation_template(
        "投标文件结构",
        "投标函................................7\n资格审查资料........................19\n"
        "技术响应文件........................35",
    )
    toc["body"] += "\nTOC PAGEREF _Toc123 \\h"

    classification = classify_navigation_item(toc)

    assert classification["is_navigation"] is True
    assert "field:toc" in classification["signals"]


def test_does_not_classify_business_forms_or_material_tables_as_navigation():
    business_items = [
        _navigation_template(
            "投标函",
            "投标人名称：示例公司\n我方承诺按招标文件要求履行全部义务。",
        ),
        _navigation_template(
            "资格审查资料",
            "营业执照、事业单位法人证书或相关资格证书扫描件。",
        ),
        _navigation_template(
            "技术参数响应表",
            "序号 | 技术参数 | 投标响应 | 备注\n1 | 处理能力 | 满足 | 已响应",
        ),
        {
            **_section(
                "s-form",
                "5 投标函",
                [{"type": "heading", "text": "5 投标函"}],
            ),
            "metadata": {"anchor": "_Toc4918"},
        },
        _navigation_template(
            "业绩情况表",
            "项目名称 | 合同金额 | 对应页码\n示例项目 | 100 | 42",
        ),
    ]

    assert [classify_navigation_item(item)["is_navigation"] for item in business_items] == [
        False,
        False,
        False,
        False,
        False,
    ]


def test_filter_records_tender_templates_and_bid_modules_with_same_semantics():
    templates = [
        _navigation_template(
            "商务评审索引表",
            "评审因素 | 投标文件组成 | 对应页码\n1 | 投标函 | 7",
        ),
        _navigation_template("投标函", "投标人名称：____"),
    ]
    sections = [
        _section(
            "s-index",
            "3 商务评审索引表",
            [
                {
                    "type": "table",
                    "text": "评审因素 | 投标文件组成 | 对应页码\n1 | 投标函 | 7",
                }
            ],
        ),
        _section(
            "s-letter",
            "5 投标函",
            [{"type": "paragraph", "text": "投标人名称：示例公司"}],
        ),
    ]

    remaining_templates, excluded_templates = filter_navigation_templates(templates)
    remaining_sections, excluded_sections = filter_navigation_sections(sections)

    assert [item["name"] for item in remaining_templates] == ["投标函"]
    assert [item["name"] for item in excluded_templates] == ["商务评审索引表"]
    assert [item["title"] for item in remaining_sections] == ["5 投标函"]
    assert [item["title"] for item in excluded_sections] == ["3 商务评审索引表"]
