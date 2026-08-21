from __future__ import annotations

import json
from pathlib import Path

from bid_check.models import BlockType, DocumentBlock
from bid_check.reporting import build_report, format_report, write_json


def _block(
    index: int,
    text: str,
    *,
    block_type: BlockType = BlockType.PARAGRAPH,
    title_level: int | None = None,
    section_path: list[str] | None = None,
    source_type: str = "paragraph",
    warnings: list[str] | None = None,
) -> DocumentBlock:
    return DocumentBlock(
        id=f"doc_report_b{index:06d}",
        block_type=block_type,
        text=text,
        title_level=title_level,
        section_path=section_path or [],
        page_idx=None,
        anchor=None,
        source_object_index=None,
        source_type=source_type,
        table=None,
        image=None,
        prev_id=None,
        next_id=None,
        metadata={"normalization_warnings": warnings or []},
    )


def test_build_report_has_deterministic_counts_and_longest_blocks():
    blocks = [
        _block(
            0,
            "",
            block_type=BlockType.TITLE,
            title_level=1,
            section_path=["章"],
            source_type="title",
        ),
        _block(
            1,
            "abc",
            block_type=BlockType.TITLE,
            title_level=2,
            section_path=["章", "节"],
            source_type="title",
        ),
        _block(
            2,
            "123456789",
            section_path=["章", "节"],
            source_type="mystery",
            warnings=["unknown type"],
        ),
    ]

    report = build_report(blocks, top_longest=5)

    assert report["block_count"] == 3
    assert report["block_type_counts"] == {"paragraph": 1, "title": 2}
    assert report["text_length"] == {
        "total": 12,
        "average": 4.0,
        "median": 3,
        "maximum": 9,
    }
    assert report["title_level_counts"] == {"1": 1, "2": 1}
    assert report["section_path_examples"] == [["章"], ["章", "节"]]
    assert report["source_type_counts"] == {"mystery": 1, "title": 2}
    assert report["warning_counts"] == {"unknown type": 1}
    assert report["longest_text_blocks"] == [
        {
            "id": "doc_report_b000002",
            "block_type": "paragraph",
            "length": 9,
            "preview": "123456789",
            "section_path": ["章", "节"],
        },
        {
            "id": "doc_report_b000001",
            "block_type": "title",
            "length": 3,
            "preview": "abc",
            "section_path": ["章", "节"],
        },
        {
            "id": "doc_report_b000000",
            "block_type": "title",
            "length": 0,
            "preview": "",
            "section_path": ["章"],
        },
    ]


def test_format_report_contains_user_facing_statistics():
    report = build_report([_block(0, "正文")])

    text = format_report(report)

    assert "块总数" in text
    assert "文本长度" in text
    assert "标题层级" in text
    assert "section_path" in text
    assert "最长文本块" in text


def test_write_json_is_utf8_indented_and_replaces_existing_file(tmp_path: Path):
    output = tmp_path / "report.json"
    output.write_text("old", encoding="utf-8")

    write_json(output, {"中文": [1, 2]})

    assert json.loads(output.read_text(encoding="utf-8")) == {"中文": [1, 2]}
    assert output.read_text(encoding="utf-8").endswith("\n")
    assert not list(tmp_path.glob(".report.json.tmp-*"))
