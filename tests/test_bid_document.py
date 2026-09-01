from __future__ import annotations

from copy import deepcopy

import pytest


def _content_payload() -> list[object]:
    return [
        {"schema": "content-list-v2"},
        [
            {
                "type": "title",
                "page_idx": 0,
                "bbox": [10, 20, 100, 50],
                "content": {
                    "level": 1,
                    "title_content": [{"type": "text", "content": "第一章"}],
                },
            },
            {
                "type": "paragraph",
                "page_idx": 0,
                "bbox": [10, 60, 500, 100],
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "正文内容"},
                    ]
                },
            },
            {
                "type": "list",
                "page_idx": 0,
                "content": {
                    "list_items": [
                        {
                            "prefix": "1.",
                            "item_content": [
                                {"type": "text", "content": "列表内容"},
                            ],
                        }
                    ]
                },
            },
            {
                "type": "table",
                "page_idx": 1,
                "bbox": [10, 100, 500, 300],
                "content": {
                    "html": "<table><tr><td>材料</td><td>营业执照</td></tr></table>",
                },
            },
            {
                "type": "image",
                "page_idx": 1,
                "bbox": [10, 320, 500, 600],
                "content": {
                    "image_caption": [{"type": "text", "content": "身份证扫描件"}],
                    "img_path": "images/id-card.jpg",
                },
            },
            {"type": "page_number", "text": "2", "page_idx": 1},
            {"type": "header", "text": "投标文件"},
            {"type": "paragraph", "text": " \n\t"},
            {"type": "text", "text": "。"},
            {"type": "footer", "text": "页脚中的业务备注"},
            {"type": "annotation", "text": "未知类型仍有价值"},
        ],
    ]


def test_flatten_mineru_content_list_preserves_nested_order_and_source_paths():
    from app.bid_document import flatten_mineru_content_list

    flattened = flatten_mineru_content_list(_content_payload())

    assert [item["type"] for item in flattened if isinstance(item, dict)] == [
        "title",
        "paragraph",
        "text",
        "table",
        "image",
        "page_number",
        "header",
        "paragraph",
        "text",
        "footer",
        "annotation",
    ]
    assert flattened[0]["text"] == "第一章"
    assert flattened[0]["_bid_source"]["source_path"] == [1, 0]
    assert flattened[2]["_bid_source"]["source_path"] == [1, 2, "content", "list_items", 0]
    assert flattened[3]["table_body"].startswith("<table>")
    assert flattened[4]["img_path"] == "images/id-card.jpg"


def test_clean_items_removes_only_deterministic_noise_and_keeps_material_objects():
    from app.bid_document import clean_items, flatten_mineru_content_list

    flattened = flatten_mineru_content_list(_content_payload())
    original = deepcopy(flattened)

    cleaned, log = clean_items(flattened)

    assert flattened == original
    assert [item["type"] for item in cleaned if isinstance(item, dict)] == [
        "title",
        "paragraph",
        "text",
        "table",
        "image",
        "footer",
        "annotation",
    ]
    assert "id-card.jpg" in cleaned[4]["img_path"]
    assert cleaned[5]["type"] == "footer"
    assert cleaned[6]["type"] == "annotation"
    assert {entry["reason"] for entry in log} == {
        "page_number",
        "header",
        "empty_text",
        "punctuation_only",
    }
    assert all("source_path" in entry for entry in log)


def test_clean_items_retains_non_dict_values_for_raw_traceability():
    from app.bid_document import clean_items

    value = "malformed-but-preserved"

    cleaned, log = clean_items([value])

    assert cleaned == [value]
    assert log == []


def _source_item(
    item_type: str,
    text: str,
    *,
    raw_index: int,
    page_idx: int,
    bbox: list[int],
    **extra: object,
) -> dict[str, object]:
    return {
        "type": item_type,
        "text": text,
        "page_idx": page_idx,
        "bbox": bbox,
        "_bid_source": {
            "raw_item_index": raw_index,
            "source_path": [raw_index],
        },
        **extra,
    }


def test_merge_items_combines_unfinished_cross_page_body_and_keeps_full_provenance():
    from app.bid_document import merge_items

    items = [
        _source_item(
            "paragraph",
            "上一页未完",
            raw_index=10,
            page_idx=0,
            bbox=[10, 800, 300, 950],
        ),
        _source_item(
            "paragraph",
            "下一页继续完成。",
            raw_index=11,
            page_idx=1,
            bbox=[10, 10, 300, 150],
        ),
        _source_item(
            "image",
            "",
            raw_index=12,
            page_idx=0,
            bbox=[10, 10, 300, 150],
        ),
        _source_item(
            "image",
            "",
            raw_index=13,
            page_idx=1,
            bbox=[10, 800, 300, 950],
        ),
    ]
    original = deepcopy(items)

    merged, logs = merge_items(items)

    assert items == original
    assert merged[0]["text"] == "上一页未完下一页继续完成。"
    assert merged[0]["start_page_idx"] == 0
    assert merged[0]["end_page_idx"] == 1
    assert merged[0]["source_item_indices"] == [10, 11]
    assert merged[0]["source_page_indices"] == [0, 1]
    assert merged[0]["source_bboxes"] == [
        [10, 800, 300, 950],
        [10, 10, 300, 150],
    ]
    assert merged[0]["merged_cross_page"] is True
    assert logs[0]["a"] == items[0]
    assert logs[0]["b"] == items[1]
    assert logs[0]["merged"] == merged[0]


@pytest.mark.parametrize(
    "items",
    [
            [
                _source_item(
                    "paragraph",
                    "前一段",
                    raw_index=1,
                page_idx=0,
                bbox=[10, 800, 300, 950],
            ),
            _source_item(
                "heading",
                "下一章节",
                raw_index=2,
                page_idx=1,
                bbox=[10, 10, 300, 150],
                text_level=1,
            ),
        ],
        [
            _source_item(
                "paragraph",
                "前一段",
                raw_index=1,
                page_idx=0,
                bbox=[10, 800, 300, 950],
            ),
            _source_item(
                "table",
                "<table></table>",
                raw_index=2,
                page_idx=1,
                bbox=[10, 10, 300, 150],
            ),
            _source_item(
                "paragraph",
                "后续内容。",
                raw_index=3,
                page_idx=1,
                bbox=[10, 200, 300, 300],
            ),
        ],
        [
            _source_item(
                "paragraph",
                "已经完成。",
                raw_index=1,
                page_idx=0,
                bbox=[10, 800, 300, 950],
            ),
            _source_item(
                "paragraph",
                "下一段",
                raw_index=2,
                page_idx=1,
                bbox=[10, 10, 300, 150],
            ),
        ],
        [
            _source_item(
                "paragraph",
                "标题提示：",
                raw_index=1,
                page_idx=0,
                bbox=[10, 800, 300, 950],
            ),
            _source_item(
                "paragraph",
                "下一页说明",
                raw_index=2,
                page_idx=1,
                bbox=[10, 10, 300, 150],
            ),
        ],
        [
            _source_item(
                "paragraph",
                "前一段",
                raw_index=1,
                page_idx=0,
                bbox=[10, 100, 300, 200],
            ),
            _source_item(
                "paragraph",
                "下一段",
                raw_index=2,
                    page_idx=1,
                    bbox=[10, 10, 300, 150],
                ),
                _source_item(
                    "image",
                    "",
                    raw_index=3,
                    page_idx=0,
                    bbox=[10, 10, 300, 950],
                ),
            ],
        ],
    ids=["heading", "table_boundary", "complete_sentence", "colon", "not_bottom"],
)
def test_merge_items_does_not_cross_document_boundaries(items):
    from app.bid_document import merge_items

    merged, logs = merge_items(items)

    assert merged == items
    assert logs == []
