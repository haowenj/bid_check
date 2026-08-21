from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bid_check.models import BlockType
from bid_check.normalizer import NormalizationError, normalize_docx_output


FIXTURES = Path(__file__).parent / "fixtures/mineru_3_4_4_docx"


def _copy_contract(tmp_path: Path, *, flavor: str = "v2") -> Path:
    raw_dir = tmp_path / "raw"
    office_dir = raw_dir / "mineru_docx_features/office"
    office_dir.mkdir(parents=True)
    source_name = "content_list_v2.json" if flavor == "v2" else "content_list.json"
    shutil.copy(FIXTURES / source_name, office_dir / f"mineru_docx_features_{source_name}")
    if flavor == "v2":
        payload = json.loads((FIXTURES / source_name).read_text())
        image_path = payload[0][13]["content"]["image_source"]["path"]
        image_file = office_dir / image_path
        image_file.parent.mkdir(parents=True, exist_ok=True)
        image_file.write_bytes(b"image")
    return raw_dir


def _write_v2(raw_dir: Path, payload: object) -> None:
    office_dir = raw_dir / "office"
    office_dir.mkdir(parents=True)
    (office_dir / "sample_content_list_v2.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_real_v2_preserves_order_and_section_paths(tmp_path: Path):
    raw_dir = _copy_contract(tmp_path)

    result = normalize_docx_output(raw_dir, "abcdef1234567890")

    assert result.source_json == "content_list_v2"
    assert [block.text for block in result.blocks] == [
        "第三章 技术要求",
        "3.1 总体要求",
        "3.1.2 人员要求",
        "项目经理应具有三年以上同类项目经验。",
        "项目团队应满足招标文件规定的岗位要求。",
        "3.1.3 其他人员要求",
        "技术负责人应具有相关专业高级职称。",
        "第四章 商务要求",
        "商务响应应完整。",
        "- 第一项要求\n- 第二项要求",
        "1. 第一步报价\n2. 第二步承诺",
        "",
        "人员配置表",
        "图 1 系统架构示意图",
        "公式示例：",
    ]
    paragraph = result.blocks[3]
    assert paragraph.section_path == [
        "第三章 技术要求",
        "3.1 总体要求",
        "3.1.2 人员要求",
    ]
    assert result.blocks[5].section_path == [
        "第三章 技术要求",
        "3.1 总体要求",
        "3.1.3 其他人员要求",
    ]
    assert result.blocks[7].section_path == ["第四章 商务要求"]
    assert all(block.page_idx is None for block in result.blocks)
    assert all(block.source_object_index is None for block in result.blocks)


def test_real_v2_preserves_table_image_and_observed_formula_absence(tmp_path: Path):
    raw_dir = _copy_contract(tmp_path)

    result = normalize_docx_output(raw_dir, "abcdef1234567890")

    table = next(block for block in result.blocks if block.block_type is BlockType.TABLE)
    assert table.table is not None
    assert table.table.html.startswith("<table>")
    assert table.table.cells is None

    image = next(block for block in result.blocks if block.block_type is BlockType.IMAGE)
    assert image.image is not None
    assert image.image.path == (
        "mineru_docx_features/office/images/"
        "dc37d7cc9f1f551e3fbefcdab47207aa55d6fd3bfcf501243f23000b50e823d1.jpg"
    )
    assert image.text == "图 1 系统架构示意图"
    assert not any(block.block_type is BlockType.FORMULA for block in result.blocks)


def test_v2_filters_empty_blocks_and_keeps_meaningful_unknown(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    _write_v2(
        raw_dir,
        [[
            {"type": "paragraph", "content": {"paragraph_content": []}},
            {"type": "list", "content": {"list_items": []}},
            {"type": "mystery", "content": {"payload": "保留这个未知块"}},
            {"type": "unknown-empty", "content": {}},
        ]],
    )

    result = normalize_docx_output(raw_dir, "abcdef1234567890")

    assert len(result.blocks) == 1
    assert result.blocks[0].block_type is BlockType.UNKNOWN
    assert result.blocks[0].text == "保留这个未知块"
    assert result.blocks[0].source_type == "mystery"
    assert result.blocks[0].metadata["unmapped_fields"] == {"payload": "保留这个未知块"}


def test_invalid_title_level_does_not_pollute_section_stack(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    _write_v2(
        raw_dir,
        [[
            {
                "type": "title",
                "content": {
                    "title_content": [{"type": "text", "content": "有效标题"}],
                    "level": 1,
                },
            },
            {
                "type": "title",
                "content": {
                    "title_content": [{"type": "text", "content": "无层级标题"}],
                    "level": 0,
                },
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"type": "text", "content": "正文"}]
                },
            },
        ]],
    )

    result = normalize_docx_output(raw_dir, "abcdef1234567890")

    assert result.blocks[1].block_type is BlockType.TITLE
    assert result.blocks[1].section_path == ["有效标题"]
    assert result.blocks[2].section_path == ["有效标题"]
    assert any("invalid title level" in warning for warning in result.warnings)


def test_neighbor_links_and_ids_are_assigned_after_empty_filtering(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    _write_v2(
        raw_dir,
        [[
            {"type": "paragraph", "content": {"paragraph_content": []}},
            {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "A"}]}},
            {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "B"}]}},
        ]],
    )

    result = normalize_docx_output(raw_dir, "abcdef1234567890")

    assert [block.id for block in result.blocks] == [
        "doc_abcdef123456_b000000",
        "doc_abcdef123456_b000001",
    ]
    assert result.blocks[0].prev_id is None
    assert result.blocks[0].next_id == result.blocks[1].id
    assert result.blocks[1].prev_id == result.blocks[0].id
    assert result.blocks[1].next_id is None


def test_legacy_content_list_is_fallback_and_strips_heading_markdown(tmp_path: Path):
    raw_dir = _copy_contract(tmp_path, flavor="legacy")

    result = normalize_docx_output(raw_dir, "abcdef1234567890")

    assert result.source_json == "content_list"
    assert result.blocks[0].text == "第三章 技术要求"
    assert result.blocks[0].title_level == 1
    assert next(block for block in result.blocks if block.block_type is BlockType.TABLE).table is not None
    assert next(block for block in result.blocks if block.block_type is BlockType.IMAGE).image is not None


def test_normalizer_reports_missing_supported_source(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    with pytest.raises(NormalizationError, match="supported content list"):
        normalize_docx_output(raw_dir, "abcdef1234567890")
