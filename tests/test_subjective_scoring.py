from __future__ import annotations

from app.subjective_scoring import (
    match_subjective_bid_content,
    select_subjective_items,
)


def test_select_subjective_items_excludes_non_subjective_rules():
    rules = {
        "score_items": [
            {"id": "s1", "evaluation_type": "subjective", "name": "主观一"},
            {"id": "o1", "evaluation_type": "objective", "name": "客观一"},
            {"id": "m1", "evaluation_type": "mixed", "name": "混合一"},
        ],
        "veto_rules": [{"id": "v1", "name": "不得否决"}],
    }

    assert [item["id"] for item in select_subjective_items(rules)] == ["s1"]


def test_match_subjective_bid_content_ignores_index_and_keeps_body_block():
    document = {
        "sections": [
            {"title": "3 商务评审索引表", "path": ["3 商务评审索引表"]},
            {"title": "13.5 评审要求承诺函", "path": ["13.5 评审要求承诺函"]},
        ],
        "blocks": [
            {
                "block_id": "index",
                "type": "table",
                "section": "3 商务评审索引表",
                "text": "投标文件编写质量的情况 | 对应页码 22",
                "order": 1,
            },
            {
                "block_id": "body",
                "type": "paragraph",
                "section": "13.5 评审要求承诺函",
                "text": "投标文件编写质量的情况良好，不存在材料缺失。",
                "order": 2,
            },
        ],
    }
    item = {
        "id": "score_item_001",
        "name": "投标文件编写质量的情况",
        "evidence_requirements": ["投标文件整体"],
    }

    result = match_subjective_bid_content(item, document, "商务投标文件部分.docx")

    assert result["status"] == "matched"
    assert [block["block_id"] for block in result["blocks"]] == ["body"]


def test_match_subjective_bid_content_returns_file_scope_missing_for_technical_item():
    document = {
        "sections": [{"title": "1 商务投标文件封面", "path": ["1 商务投标文件封面"]}],
        "blocks": [
            {
                "block_id": "b1",
                "type": "paragraph",
                "section": "1 商务投标文件封面",
                "text": "商务投标文件",
                "order": 1,
            }
        ],
    }
    item = {
        "id": "score_item_003",
        "name": "项目需求的分析及理解程度",
        "evidence_requirements": ["本项目需求的分析及理解描述"],
    }

    result = match_subjective_bid_content(item, document, "商务投标文件部分.docx")

    assert result["status"] == "file_scope_missing"
    assert result["blocks"] == []


def test_match_subjective_bid_content_does_not_treat_technical_word_in_case_as_scope():
    document = {
        "sections": [{"title": "21 业绩情况表", "path": ["21 业绩情况表"]}],
        "blocks": [
            {
                "block_id": "b1",
                "type": "paragraph",
                "section": "21 业绩情况表",
                "text": "项目为技术服务类业绩，合同已经提供。",
                "order": 1,
            }
        ],
    }
    item = {
        "id": "score_item_004",
        "name": "云网产品开发服务、大模型技术支持服务、云网产品测试服务支撑方案",
        "evidence_requirements": ["支撑方案"],
    }

    result = match_subjective_bid_content(item, document, "商务投标文件部分.docx")

    assert result["status"] == "file_scope_missing"
