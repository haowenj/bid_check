from bid_check.models import BlockType, DocumentBlock, ImageContent, TableContent


def test_block_type_includes_index():
    assert BlockType.INDEX.value == "index"


def test_document_block_serializes_nested_dataclasses_and_enum_values():
    block = DocumentBlock(
        id="doc_abcdef123456_b000000",
        block_type=BlockType.TABLE,
        text="人员配置表",
        title_level=None,
        section_path=["第三章 技术要求"],
        page_idx=None,
        anchor=None,
        source_object_index=None,
        source_type="table",
        table=TableContent(markdown="|角色|要求|", caption=["人员配置表"]),
        image=None,
        prev_id=None,
        next_id=None,
        metadata={"source_format": "docx"},
    )

    result = block.to_dict()

    assert result["block_type"] == "table"
    assert result["table"]["markdown"] == "|角色|要求|"
    assert result["table"]["cells"] is None
    assert result["image"] is None


def test_document_block_keeps_all_image_fields_in_json_shape():
    block = DocumentBlock(
        id="doc_abcdef123456_b000000",
        block_type=BlockType.IMAGE,
        text="系统架构示意图",
        title_level=None,
        section_path=[],
        page_idx=None,
        anchor="img-1",
        source_object_index=None,
        source_type="image",
        table=None,
        image=ImageContent(path="images/example.png"),
        prev_id=None,
        next_id=None,
        metadata={},
    )

    result = block.to_dict()

    assert result["image"] == {
        "path": "images/example.png",
        "caption": [],
        "footnote": [],
        "alt_text": None,
    }
    assert set(result) == {
        "id",
        "block_type",
        "text",
        "title_level",
        "section_path",
        "page_idx",
        "anchor",
        "source_object_index",
        "source_type",
        "table",
        "image",
        "prev_id",
        "next_id",
        "metadata",
    }
