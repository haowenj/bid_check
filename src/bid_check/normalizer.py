"""Normalize the observed MinerU Office content-list formats."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal, Sequence

from .models import BlockType, DocumentBlock, ImageContent, TableContent, blocks_to_jsonable


class NormalizationError(RuntimeError):
    """Raised when MinerU raw output cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    blocks: list[DocumentBlock]
    source_json: str
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class SourceItem:
    value: dict[str, Any]
    source_path: Path
    page_group_index: int | None
    item_index: int
    flat_index: int


@dataclass(slots=True)
class CandidateBlock:
    block_type: BlockType
    text: str
    title_level: int | None
    page_idx: int | None
    anchor: str | None
    source_object_index: int | str | None
    source_type: str
    table: TableContent | None
    image: ImageContent | None
    source_position: dict[str, int | None]
    unmapped_fields: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_LEGACY_TITLE_MARKDOWN = re.compile(r"^\*\*(?P<text>.*)\*\*$", re.DOTALL)


def normalize_docx_output(raw_dir: Path, document_sha256: str) -> NormalizationResult:
    raw_dir = Path(raw_dir).resolve()
    source_json, source_path = _discover_source(raw_dir)
    payload = _load_json(source_path)
    if source_json == "content_list_v2":
        source_items = _iter_v2_items(payload, source_path)
    else:
        source_items = _iter_legacy_items(payload, source_path)

    candidates: list[CandidateBlock] = []
    global_warnings: list[str] = []
    headings: dict[int, str] = {}
    for source_item in source_items:
        candidate = (
            _map_v2_item(source_item, raw_dir)
            if source_json == "content_list_v2"
            else _map_legacy_item(source_item, raw_dir)
        )
        if candidate.block_type is BlockType.TITLE:
            if candidate.title_level is not None and candidate.title_level > 0:
                for level in [key for key in headings if key >= candidate.title_level]:
                    del headings[level]
                headings[candidate.title_level] = candidate.text
            else:
                candidate.warnings.append("invalid title level; heading stack unchanged")
                global_warnings.append(
                    f"flat_index {candidate.source_position['flat_index']}: invalid title level"
                )
        section_path = [headings[level] for level in sorted(headings)]
        if not _is_meaningful(candidate):
            continue
        metadata = {
            "source_format": "docx",
            "source_json": source_json,
            "source_position": candidate.source_position,
            "normalization_warnings": list(candidate.warnings),
            "unmapped_fields": candidate.unmapped_fields,
        }
        candidates.append(
            DocumentBlock(
                id="",
                block_type=candidate.block_type,
                text=candidate.text,
                title_level=candidate.title_level,
                section_path=section_path,
                page_idx=candidate.page_idx,
                anchor=candidate.anchor,
                source_object_index=candidate.source_object_index,
                source_type=candidate.source_type,
                table=candidate.table,
                image=candidate.image,
                prev_id=None,
                next_id=None,
                metadata=metadata,
            )
        )

    prefix = document_sha256[:12]
    for index, block in enumerate(candidates):
        block.id = f"doc_{prefix}_b{index:06d}"
    for index, block in enumerate(candidates):
        block.prev_id = candidates[index - 1].id if index else None
        block.next_id = candidates[index + 1].id if index + 1 < len(candidates) else None
    return NormalizationResult(candidates, source_json, global_warnings)


def write_document_blocks(path: Path, blocks: Sequence[DocumentBlock]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(blocks_to_jsonable(blocks), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _discover_source(
    raw_dir: Path,
) -> tuple[Literal["content_list_v2", "content_list"], Path]:
    v2_candidates = sorted(raw_dir.rglob("*_content_list_v2.json"))
    if v2_candidates:
        path = v2_candidates[0]
        payload = _load_json(path)
        if _is_v2_shape(payload):
            return "content_list_v2", path
    legacy_candidates = sorted(raw_dir.rglob("*_content_list.json"))
    if legacy_candidates:
        path = legacy_candidates[0]
        payload = _load_json(path)
        if _is_legacy_shape(payload):
            return "content_list", path
    discovered = ", ".join(path.name for path in sorted(raw_dir.rglob("*.json"))) or "none"
    raise NormalizationError(
        f"no supported content list found under {raw_dir}; discovered: {discovered}"
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"invalid JSON in {path}: {exc}") from exc


def _is_v2_shape(payload: Any) -> bool:
    return isinstance(payload, list) and all(
        isinstance(group, list) and all(isinstance(item, dict) for item in group)
        for group in payload
    )


def _is_legacy_shape(payload: Any) -> bool:
    return isinstance(payload, list) and all(isinstance(item, dict) for item in payload)


def _iter_v2_items(payload: Any, source_path: Path) -> Iterator[SourceItem]:
    if not _is_v2_shape(payload):
        raise NormalizationError(f"unsupported content_list_v2 structure in {source_path}")
    flat_index = 0
    for page_group_index, group in enumerate(payload):
        for item_index, item in enumerate(group):
            yield SourceItem(
                value=item,
                source_path=source_path,
                page_group_index=page_group_index,
                item_index=item_index,
                flat_index=flat_index,
            )
            flat_index += 1


def _iter_legacy_items(payload: Any, source_path: Path) -> Iterator[SourceItem]:
    if not _is_legacy_shape(payload):
        raise NormalizationError(f"unsupported content_list structure in {source_path}")
    for flat_index, item in enumerate(payload):
        yield SourceItem(
            value=item,
            source_path=source_path,
            page_group_index=None,
            item_index=flat_index,
            flat_index=flat_index,
        )


def _base_candidate(source_item: SourceItem, source_type: str) -> CandidateBlock:
    item = source_item.value
    page_idx = item.get("page_idx")
    if isinstance(page_idx, bool) or not isinstance(page_idx, int):
        page_idx = None
    anchor = item.get("anchor") if isinstance(item.get("anchor"), str) else None
    source_object_index = item.get("source_object_index")
    if source_object_index is None:
        source_object_index = item.get("object_index")
    return CandidateBlock(
        block_type=BlockType.UNKNOWN,
        text="",
        title_level=None,
        page_idx=page_idx,
        anchor=anchor,
        source_object_index=source_object_index,
        source_type=source_type,
        table=None,
        image=None,
        source_position={
            "page_group_index": source_item.page_group_index,
            "item_index": source_item.item_index,
            "flat_index": source_item.flat_index,
        },
    )


def _map_v2_item(source_item: SourceItem, raw_dir: Path) -> CandidateBlock:
    item = source_item.value
    source_type = str(item.get("type", "unknown"))
    candidate = _base_candidate(source_item, source_type)
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    consumed: set[str] = set()

    if source_type == "title":
        candidate.block_type = BlockType.TITLE
        candidate.text = _span_text(content.get("title_content"))
        candidate.title_level = _positive_int(content.get("level"))
        consumed.update({"title_content", "level"})
    elif source_type == "paragraph":
        candidate.block_type = BlockType.PARAGRAPH
        candidate.text = _span_text(content.get("paragraph_content"))
        consumed.add("paragraph_content")
    elif source_type == "list":
        candidate.block_type = BlockType.LIST
        items = content.get("list_items")
        list_text: list[str] = []
        if isinstance(items, list):
            for list_item in items:
                if not isinstance(list_item, dict):
                    continue
                value = _span_text(list_item.get("item_content"))
                prefix = list_item.get("prefix")
                list_text.append(f"{prefix} {value}".strip() if prefix else value)
        candidate.text = "\n".join(filter(None, list_text))
        consumed.update({"list_type", "attribute", "list_items"})
    elif source_type == "table":
        candidate.block_type = BlockType.TABLE
        captions = _span_text_list(content.get("table_caption"))
        html = content.get("html") if isinstance(content.get("html"), str) else None
        candidate.text = "\n".join(captions)
        candidate.table = TableContent(html=html, caption=captions)
        consumed.update({"table_caption", "html", "table_type", "table_nest_level"})
    elif source_type in {"image", "chart"}:
        candidate.block_type = BlockType.IMAGE if source_type == "image" else BlockType.CHART
        image_source = content.get("image_source")
        raw_path = image_source.get("path") if isinstance(image_source, dict) else None
        image_path = _normalize_image_path(
            raw_path, source_item.source_path.parent, raw_dir, candidate
        )
        captions = _span_text_list(content.get("image_caption"))
        candidate.text = "\n".join(captions)
        candidate.image = ImageContent(path=image_path, caption=captions)
        consumed.update({"image_source", "image_caption", "sub_type"})
    elif source_type in {"equation", "formula", "inline_equation"}:
        candidate.block_type = BlockType.FORMULA
        candidate.text = _span_text(
            content.get("equation_content")
            or content.get("formula_content")
            or content.get("paragraph_content")
        )
        consumed.update({"equation_content", "formula_content", "paragraph_content"})
    elif source_type == "code":
        candidate.block_type = BlockType.CODE
        candidate.text = _span_text(content.get("code_body"))
        consumed.update({"code_body", "code_caption", "code_footnote", "sub_type"})
    else:
        candidate.text = _collect_strings(content)

    candidate.unmapped_fields = {
        key: value for key, value in content.items() if key not in consumed and value not in (None, [], {})
    }
    return candidate


def _map_legacy_item(source_item: SourceItem, raw_dir: Path) -> CandidateBlock:
    item = source_item.value
    source_type = str(item.get("type", "unknown"))
    candidate = _base_candidate(source_item, source_type)
    if source_type == "text":
        title_level = _positive_int(item.get("text_level"))
        candidate.block_type = BlockType.TITLE if title_level is not None else BlockType.PARAGRAPH
        candidate.title_level = title_level
        candidate.text = _clean_legacy_title(item.get("text", ""))
        consumed = {"type", "text", "text_level", "page_idx", "anchor"}
    elif source_type == "list":
        candidate.block_type = BlockType.LIST
        values = item.get("list_items")
        candidate.text = "\n".join(value.strip() for value in values if isinstance(value, str)) if isinstance(values, list) else ""
        consumed = {"type", "list_items", "page_idx", "anchor"}
    elif source_type == "table":
        candidate.block_type = BlockType.TABLE
        captions = _string_list(item.get("table_caption"))
        body = item.get("table_body") if isinstance(item.get("table_body"), str) else None
        candidate.text = "\n".join(captions)
        candidate.table = TableContent(html=body, caption=captions)
        consumed = {"type", "table_caption", "table_body", "page_idx", "anchor"}
    elif source_type == "image":
        candidate.block_type = BlockType.IMAGE
        captions = _string_list(item.get("image_caption"))
        image_path = _normalize_image_path(
            item.get("img_path"), source_item.source_path.parent, raw_dir, candidate
        )
        candidate.text = "\n".join(captions)
        candidate.image = ImageContent(path=image_path, caption=captions)
        consumed = {"type", "img_path", "image_caption", "page_idx", "anchor"}
    else:
        candidate.text = _collect_strings(item)
        consumed = {"type", "page_idx", "anchor"}
    candidate.unmapped_fields = {
        key: value for key, value in item.items() if key not in consumed and value not in (None, [], {})
    }
    return candidate


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _span_text(value: Any) -> str:
    return "".join(_span_text_values(value)).strip()


def _span_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in (_span_text(item) for item in value) if (text := item)]


def _span_text_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _span_text_values(item)
    elif isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, (list, dict)):
            yield from _span_text_values(content)


def _collect_strings(value: Any) -> str:
    values: list[str] = []
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            values.extend(_collect_strings(item).splitlines())
    elif isinstance(value, list):
        for item in value:
            values.extend(_collect_strings(item).splitlines())
    return "\n".join(value.strip() for value in values if value.strip())


def _string_list(value: Any) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _clean_legacy_title(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    match = _LEGACY_TITLE_MARKDOWN.match(text)
    return match.group("text").strip() if match else text


def _normalize_image_path(
    raw_path: Any,
    source_parent: Path,
    raw_dir: Path,
    candidate: CandidateBlock,
) -> str | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    normalized = raw_path.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        raise NormalizationError(f"unsafe image reference: {raw_path!r}")
    resolved = (source_parent / Path(*relative.parts)).resolve()
    raw_resolved = raw_dir.resolve()
    if resolved != raw_resolved and raw_resolved not in resolved.parents:
        raise NormalizationError(f"image reference escapes raw directory: {raw_path!r}")
    if not resolved.is_file():
        candidate.warnings.append(f"image reference does not exist: {raw_path}")
    return resolved.relative_to(raw_resolved).as_posix()


def _is_meaningful(candidate: CandidateBlock) -> bool:
    if candidate.text.strip():
        return True
    if candidate.table is not None:
        return any(
            (
                candidate.table.html,
                candidate.table.markdown,
                candidate.table.cells,
                candidate.table.caption,
                candidate.table.footnote,
                candidate.table.image_path,
            )
        )
    if candidate.image is not None:
        return any(
            (
                candidate.image.path,
                candidate.image.caption,
                candidate.image.footnote,
                candidate.image.alt_text,
            )
        )
    return bool(candidate.unmapped_fields)
