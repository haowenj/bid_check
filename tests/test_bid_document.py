from __future__ import annotations

from copy import deepcopy
import io
import json
import zipfile

import httpx
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


def test_flatten_supports_mineru_grouped_v2_content_and_image_source_path():
    from app.bid_document import flatten_mineru_content_list

    payload = [
        [
            {
                "type": "image",
                "content": {
                    "image_source": {"path": "images/license.png"},
                    "image_caption": [],
                },
            }
        ],
        [
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "分组正文"}
                    ]
                },
            }
        ],
    ]

    flattened = flatten_mineru_content_list(payload)

    assert [item["type"] for item in flattened] == ["image", "paragraph"]
    assert flattened[0]["img_path"] == "images/license.png"
    assert flattened[0]["_bid_source"]["source_path"] == [0, 0]
    assert flattened[1]["_bid_source"]["source_path"] == [1, 0]
    assert [item["_bid_source"]["raw_item_index"] for item in flattened] == [0, 1]


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


def test_clean_items_preserves_navigation_items_for_compliance_boundary_filter():
    from app.bid_document import clean_items, flatten_mineru_content_list

    flattened = flatten_mineru_content_list(
        [
            {"type": "index", "text": "商务评审索引表", "page_idx": 1},
            {"type": "text", "text": "投标函", "page_idx": 1},
        ]
    )

    cleaned, log = clean_items(flattened)

    assert [item["type"] for item in cleaned] == ["index", "text"]
    assert cleaned[0]["text"] == "商务评审索引表"
    assert log == []


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


def test_structure_content_list_keeps_sections_tables_images_and_sources():
    from app.bid_document import structure_content_list

    items = [
        _source_item(
            "title",
            "第一章 总则",
            raw_index=0,
            page_idx=0,
            bbox=[10, 20, 500, 50],
            text_level=1,
        ),
        _source_item(
            "paragraph",
            "投标文件说明。",
            raw_index=1,
            page_idx=0,
            bbox=[10, 60, 500, 100],
        ),
        _source_item(
            "title",
            "1.1 投标说明",
            raw_index=2,
            page_idx=0,
            bbox=[10, 110, 500, 140],
            text_level=2,
        ),
        _source_item(
            "paragraph",
            "本节正文。",
            raw_index=3,
            page_idx=0,
            bbox=[10, 150, 500, 190],
        ),
        _source_item(
            "table",
            "",
            raw_index=4,
            page_idx=1,
            bbox=[10, 20, 500, 180],
            table_body=(
                "<table><tr><th>材料</th><th>状态</th></tr>"
                "<tr><td>营业执照</td><td>已提供</td></tr></table>"
            ),
            table_caption="资格材料表",
        ),
        _source_item(
            "image",
            "身份证扫描件",
            raw_index=5,
            page_idx=1,
            bbox=[10, 200, 500, 600],
            img_path="images/id-card.jpg",
        ),
        _source_item(
            "footer",
            "页脚中的业务备注",
            raw_index=6,
            page_idx=1,
            bbox=[10, 850, 500, 880],
        ),
    ]

    document = structure_content_list(
        items,
        source_filename="bid.docx",
        source_sha256="abc123",
        parser_diagnostics={"parser": "fixture"},
    )

    assert document["schema_version"] == "bid-document-v1"
    assert document["source"] == {
        "filename": "bid.docx",
        "sha256": "abc123",
    }
    assert [block["type"] for block in document["blocks"]] == [
        "heading",
        "paragraph",
        "heading",
        "paragraph",
        "table",
        "image",
        "paragraph",
    ]
    assert document["stats"] == {
        "block_count": 7,
        "block_type_counts": {
            "heading": 2,
            "paragraph": 3,
            "table": 1,
            "image": 1,
        },
        "section_count": 2,
        "table_count": 1,
        "image_count": 1,
        "page_count": 2,
        "unsupported_item_count": 0,
    }
    assert document["blocks"][1]["section"] == "第一章 总则"
    assert document["blocks"][1]["metadata"]["section_path"] == ["第一章 总则"]
    assert document["blocks"][3]["section"] == "1.1 投标说明"
    assert document["blocks"][3]["metadata"]["section_path"] == [
        "第一章 总则",
        "1.1 投标说明",
    ]
    assert document["blocks"][4]["metadata"]["source_item_indices"] == [4]
    assert document["tables"][0]["block_id"] == document["blocks"][4]["block_id"]
    assert document["tables"][0]["rows"] == [
        ["材料", "状态"],
        ["营业执照", "已提供"],
    ]
    assert document["images"][0]["img_path"] == "images/id-card.jpg"
    assert document["images"][0]["section_path"] == [
        "第一章 总则",
        "1.1 投标说明",
    ]
    assert document["images"][0]["source"]["raw_item_index"] == 5


def test_structure_content_list_registers_table_images_with_section_and_table_provenance():
    from app.bid_document import structure_content_list

    shared_path = "images/shared-proof.png"
    items = [
        _source_item(
            "title",
            "第一章 证明材料",
            raw_index=0,
            page_idx=0,
            bbox=[10, 20, 500, 50],
            block_id="heading-1",
        ),
        _source_item(
            "table",
            "",
            raw_index=1,
            page_idx=0,
            bbox=[10, 60, 500, 300],
            block_id="table-1",
            table_body=(
                "<table><tr><td>证明文件</td></tr>"
                f'<tr><td><img src="{shared_path}"/></td></tr></table>'
            ),
        ),
        _source_item(
            "image",
            "独立图片路径与表格内图片相同",
            raw_index=2,
            page_idx=0,
            bbox=[10, 320, 500, 600],
            block_id="image-1",
            img_path=shared_path,
        ),
    ]

    document = structure_content_list(
        items,
        source_filename="bid.docx",
        source_sha256="sha",
    )

    assert document["stats"]["image_count"] == 1
    assert document["tables"][0]["image_ids"] == ["i0001"]
    assert document["blocks"][1]["metadata"]["image_ids"] == ["i0001"]
    image = document["images"][0]
    assert image["image_id"] == "i0001"
    assert image["section_id"] == "s0001"
    assert image["section_path"] == ["第一章 证明材料"]
    assert image["source_type"] == "table_embedded"
    assert image["source_table_id"] == "t0001"
    assert image["source_table_block_id"] == "table-1"
    assert image["source_table_ids"] == ["t0001"]
    assert set(image["source_block_ids"]) == {"table-1", "image-1"}
    assert {item["kind"] for item in image["source_references"]} == {
        "table_embedded",
        "image",
    }


def test_mineru_bid_parser_extracts_table_embedded_images_once_and_marks_asset_ready(
    tmp_path,
):
    from app.bid_document import MinerUBidDocumentParser

    bid_path = tmp_path / "bid.docx"
    bid_path.write_bytes(b"fixture bid")
    embedded_path = "images/embedded-proof.png"
    table_only_path = "images/table-only-proof.png"
    content_payload = [
        {
            "type": "title",
            "page_idx": 0,
            "content": {"level": 1, "title_content": [{"type": "text", "content": "证明材料"}]},
        },
        {
            "type": "table",
            "page_idx": 0,
            "content": {
                "html": (
                    "<table><tr><td>证明图片</td></tr>"
                    f'<tr><td><img src="{embedded_path}"></td></tr>'
                    f'<tr><td><img src="{table_only_path}"></td></tr></table>'
                )
            },
        },
        {
            "type": "image",
            "page_idx": 0,
            "content": {
                "image_caption": [{"type": "text", "content": "同一张证明图片"}],
                "img_path": embedded_path,
            },
        },
    ]
    raw_content_bytes = json.dumps(content_payload, ensure_ascii=False).encode("utf-8")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("results/content_list.json", raw_content_bytes)
        archive.writestr("results/images/embedded-proof.png", b"embedded-proof")
        archive.writestr("results/images/table-only-proof.png", b"table-only-proof")
    zip_bytes = zip_buffer.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks":
            return httpx.Response(
                202,
                json={
                    "task_id": "task-table-image",
                    "status_url": "/tasks/task-table-image",
                    "result_url": "/tasks/task-table-image/result",
                },
            )
        if request.url.path == "/tasks/task-table-image":
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path == "/tasks/task-table-image/result":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://mineru.example",
        trust_env=False,
    )
    output_dir = tmp_path / "bid_document_cleaning"
    parser = MinerUBidDocumentParser(
        "https://mineru.example",
        poll_interval_seconds=0,
        http_client=client,
    )

    result = parser.parse(bid_path, output_dir=output_dir)
    structured = json.loads((output_dir / "structured_document.json").read_text())

    assert result["stats"]["image_count"] == 2
    assert result["diagnostics"]["asset_reference_count"] == 2
    assert result["stats"]["asset_ready_count"] == 2
    assert (output_dir / "images/embedded-proof.png").read_bytes() == b"embedded-proof"
    assert (output_dir / "images/table-only-proof.png").read_bytes() == b"table-only-proof"
    assert structured["tables"][0]["image_ids"] == ["i0001", "i0002"]
    assert [image["asset_status"] for image in structured["images"]] == ["ready", "ready"]
    assert structured["images"][0]["source_table_id"] == "t0001"


def test_mineru_bid_parser_writes_exact_raw_result_and_safe_assets(tmp_path):
    from app.bid_document import MinerUBidDocumentParser

    bid_path = tmp_path / "bid.docx"
    bid_path.write_bytes(b"fixture bid")
    content_payload = [
        {"schema": "content-list-v2"},
        [
            {
                "type": "title",
                "page_idx": 0,
                "bbox": [10, 20, 500, 50],
                "content": {
                    "level": 1,
                    "title_content": [{"type": "text", "content": "第一章"}],
                },
            },
            {
                "type": "table",
                "page_idx": 0,
                "bbox": [10, 60, 500, 160],
                "content": {"html": "<table><tr><td>材料</td></tr></table>"},
            },
            {
                "type": "image",
                "page_idx": 0,
                "bbox": [10, 180, 500, 500],
                "content": {
                    "image_caption": [{"type": "text", "content": "证照"}],
                    "image_source": {"path": "images/license.jpg"},
                },
            },
            {"type": "paragraph", "text": "附件说明。", "page_idx": 0},
        ],
    ]
    raw_content_bytes = json.dumps(
        content_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("results/content_list_v2.json", raw_content_bytes)
        archive.writestr("results/images/license.jpg", b"license-image")
        archive.writestr("../outside.jpg", b"must-not-extract")
    zip_bytes = zip_buffer.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks":
            return httpx.Response(
                202,
                json={
                    "task_id": "task-1",
                    "status_url": "/tasks/task-1",
                    "result_url": "/tasks/task-1/result",
                },
            )
        if request.url.path == "/tasks/task-1":
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path == "/tasks/task-1/result":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://mineru.example",
        trust_env=False,
    )
    output_dir = tmp_path / "bid_document_cleaning"
    parser = MinerUBidDocumentParser(
        "https://mineru.example",
        poll_interval_seconds=0,
        http_client=client,
    )

    result = parser.parse(bid_path, output_dir=output_dir)

    assert result["status"] == "success"
    assert result["stats"]["raw_item_count"] == 4
    assert result["stats"]["table_count"] == 1
    assert result["stats"]["image_count"] == 1
    assert (output_dir / "raw_content_list.json").read_bytes() == raw_content_bytes
    assert (output_dir / "images/license.jpg").read_bytes() == b"license-image"
    assert not (tmp_path / "outside.jpg").exists()
    structured = json.loads((output_dir / "structured_document.json").read_text())
    assert structured["tables"][0]["rows"] == [["材料"]]
    assert structured["images"][0]["asset_status"] == "ready"
    assert result["diagnostics"]["asset_reference_count"] == 1
    assert json.loads((output_dir / "cleaning_log.json").read_text()) == []
