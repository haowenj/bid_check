"""Stable data models used after MinerU-specific normalization."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Sequence


class BlockType(str, Enum):
    TITLE = "title"
    PARAGRAPH = "paragraph"
    LIST = "list"
    INDEX = "index"
    TABLE = "table"
    IMAGE = "image"
    FORMULA = "formula"
    CODE = "code"
    CHART = "chart"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    FOOTNOTE = "footnote"
    ASIDE = "aside"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class TableContent:
    html: str | None = None
    markdown: str | None = None
    cells: list[list[str]] | None = None
    caption: list[str] = field(default_factory=list)
    footnote: list[str] = field(default_factory=list)
    image_path: str | None = None


@dataclass(slots=True)
class ImageContent:
    path: str | None = None
    caption: list[str] = field(default_factory=list)
    footnote: list[str] = field(default_factory=list)
    alt_text: str | None = None


@dataclass(slots=True)
class DocumentBlock:
    id: str
    block_type: BlockType
    text: str
    title_level: int | None
    section_path: list[str]
    page_idx: int | None
    anchor: str | None
    source_object_index: int | str | None
    source_type: str
    table: TableContent | None
    image: ImageContent | None
    prev_id: str | None
    next_id: str | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def blocks_to_jsonable(blocks: Sequence[DocumentBlock]) -> list[dict[str, Any]]:
    return [block.to_dict() for block in blocks]
