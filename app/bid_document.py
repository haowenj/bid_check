from __future__ import annotations

import copy
import hashlib
import io
import json
import mimetypes
import os
import re
import tempfile
import time
import unicodedata
import urllib.parse
import zipfile
from collections.abc import Sequence
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

import httpx


_SOURCE_KEY = "_bid_source"
MINERU_TASKS_PROTOCOL_VERSION = "mineru-tasks-v1"
MINERU_TASKS_PROTOCOL_LABEL = "mineru_tasks"
DEFAULT_MINERU_BACKEND = "hybrid-engine"
SUPPORTED_MINERU_BACKENDS = {"hybrid-engine", "hybrid-http-client"}
DEFAULT_MINERU_TIMEOUT_SECONDS = 1800.0
DEFAULT_MINERU_POLL_INTERVAL_SECONDS = 2.0
DOCUMENT_SCHEMA_VERSION = "bid-document-v1"
_EDGE_RATIO = 0.2
_COMPLETE_ENDINGS = frozenset("。！？.!?；;…")
_CLOSING_CHARS = frozenset("\"'”’）)]】》」』")
_HEADING_PATTERNS = (
    re.compile(r"^\s*(?:附件|附录)\s*[0-9０-９一二三四五六七八九十百千万]+"),
    re.compile(r"^\s*第\s*[0-9０-９一二三四五六七八九十百千万]+\s*条"),
    re.compile(r"^\s*[一二三四五六七八九十百千万]+[、.．]"),
    re.compile(r"^\s*[0-9０-９]+[、.．.)）]"),
    re.compile(r"^\s*[（(][0-9０-９一二三四五六七八九十百千万]+[）)]"),
    re.compile(r"^\s*[➢•·▪◦○●■□◆◇—–-]\s*"),
)


def _inline_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_inline_text(item) for item in value)
    if isinstance(value, dict):
        return _inline_text(value.get("content", ""))
    return ""


def _payload_items(payload: Any) -> tuple[list[Any], list[int]]:
    if isinstance(payload, dict):
        for key in ("blocks", "content", "items", "content_list"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, []
        raise ValueError("MinerU 返回结果不是结构化内容列表。")
    if not isinstance(payload, list):
        raise ValueError("MinerU 返回结果不是结构化内容列表。")
    if (
        len(payload) == 2
        and isinstance(payload[0], dict)
        and isinstance(payload[1], list)
        and all(isinstance(item, dict) for item in payload[1])
    ):
        return payload[1], [1]
    return payload, []


def _expanded_payload_items(payload: Any) -> list[tuple[int, Any, list[Any]]]:
    """Expand MinerU's optional grouped top-level list without losing paths."""

    items, prefix = _payload_items(payload)
    expanded: list[tuple[int, Any, list[Any]]] = []
    raw_item_index = 0

    def visit(value: Any, source_path: list[Any]) -> None:
        nonlocal raw_item_index
        if isinstance(value, list):
            for child_index, child in enumerate(value):
                visit(child, [*source_path, child_index])
            return
        expanded.append((raw_item_index, value, source_path))
        raw_item_index += 1

    for top_index, raw in enumerate(items):
        visit(raw, [*prefix, top_index])
    return expanded


def _source_metadata(
    *,
    source_path: list[Any],
    raw_item_index: int,
    parent_raw_item_index: int | None = None,
    child_index: int | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "source_path": copy.deepcopy(source_path),
        "raw_item_index": raw_item_index,
    }
    if parent_raw_item_index is not None:
        source["parent_raw_item_index"] = parent_raw_item_index
    if child_index is not None:
        source["child_index"] = child_index
    return source


def _with_source(
    value: dict[str, Any],
    *,
    source_path: list[Any],
    raw_item_index: int,
    parent_raw_item_index: int | None = None,
    child_index: int | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result[_SOURCE_KEY] = _source_metadata(
        source_path=source_path,
        raw_item_index=raw_item_index,
        parent_raw_item_index=parent_raw_item_index,
        child_index=child_index,
    )
    return result


def flatten_mineru_content_list(payload: Any) -> list[Any]:
    """Flatten supported MinerU envelopes while retaining raw source paths."""

    flattened: list[Any] = []
    for raw_item_index, raw, source_path in _expanded_payload_items(payload):
        if not isinstance(raw, dict):
            flattened.append(raw)
            continue

        raw_type = str(raw.get("type", raw.get("block_type", "paragraph"))).lower()
        content = raw.get("content")
        if raw_type == "list" and isinstance(content, dict):
            list_items = content.get("list_items", [])
            if not isinstance(list_items, list):
                continue
            for child_index, child in enumerate(list_items):
                if not isinstance(child, dict):
                    continue
                text = _inline_text(child.get("item_content", []))
                prefix_text = str(child.get("prefix", "")).strip()
                text = f"{prefix_text} {text}".strip()
                if not text:
                    continue
                flattened.append(
                    _with_source(
                        {
                            **{
                                key: value
                                for key, value in raw.items()
                                if key != "content"
                            },
                            "type": "text",
                            "text": text,
                            "mineru_nested_content": copy.deepcopy(child),
                            "mineru_parent_type": raw_type,
                        },
                        source_path=[
                            *source_path,
                            "content",
                            "list_items",
                            child_index,
                        ],
                        raw_item_index=raw_item_index,
                        parent_raw_item_index=raw_item_index,
                        child_index=child_index,
                    )
                )
            continue

        if isinstance(content, (dict, list)):
            normalized = {
                key: value for key, value in raw.items() if key != "content"
            }
            normalized["mineru_nested_content"] = copy.deepcopy(content)
            normalized["mineru_parent_type"] = raw_type
            if raw_type == "title":
                normalized["type"] = "title"
                normalized["text"] = _inline_text(
                    content.get("title_content", [])
                    if isinstance(content, dict)
                    else content
                ).strip()
                if isinstance(content, dict) and "level" in content:
                    normalized["text_level"] = content["level"]
            elif raw_type == "paragraph":
                normalized["type"] = "paragraph"
                normalized["text"] = _inline_text(
                    content.get("paragraph_content", [])
                    if isinstance(content, dict)
                    else content
                ).strip()
            elif raw_type == "table":
                normalized["type"] = "table"
                if isinstance(content, dict):
                    table_body = content.get("html", content.get("table_body", ""))
                    if isinstance(table_body, str):
                        normalized["table_body"] = table_body
                    for key, value in content.items():
                        if key not in {"html", "table_body"}:
                            normalized[key] = copy.deepcopy(value)
            elif raw_type in {"image", "figure"}:
                normalized["type"] = raw_type
                if isinstance(content, dict):
                    normalized["text"] = _inline_text(
                        content.get("image_caption", content.get("caption", ""))
                    ).strip()
                    if content.get("img_path"):
                        normalized["img_path"] = content["img_path"]
                    image_source = content.get("image_source")
                    if (
                        not normalized.get("img_path")
                        and isinstance(image_source, dict)
                        and isinstance(image_source.get("path"), str)
                    ):
                        normalized["img_path"] = image_source["path"]
            else:
                normalized["text"] = _inline_text(content).strip()
            flattened.append(
                _with_source(
                    normalized,
                    source_path=source_path,
                    raw_item_index=raw_item_index,
                )
            )
            continue

        flattened.append(
            _with_source(
                raw,
                source_path=source_path,
                raw_item_index=raw_item_index,
            )
        )
    return flattened


def _text_for_cleaning(item: dict[str, Any]) -> str | None:
    value = item.get("text")
    return value if isinstance(value, str) else None


def _cleaning_reason(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type", item.get("block_type", ""))).lower()
    if item_type == "page_number":
        return "page_number"
    if item_type == "header":
        return "header"
    if item_type not in {"text", "paragraph"}:
        return None
    text = _text_for_cleaning(item)
    if text is None:
        return None
    compact_text = "".join(char for char in text if not char.isspace())
    if not compact_text:
        return "empty_text"
    if len(compact_text) == 1 and unicodedata.category(compact_text).startswith("P"):
        return "punctuation_only"
    return None


def clean_items(items: Sequence[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Remove only deterministic noise and return a source-indexed clean log."""

    cleaned: list[Any] = []
    log: list[dict[str, Any]] = []
    for item in items:
        reason = _cleaning_reason(item)
        if reason is None:
            cleaned.append(copy.deepcopy(item))
            continue
        source = item.get(_SOURCE_KEY, {}) if isinstance(item, dict) else {}
        entry = {
            "reason": reason,
            "type": item.get("type") if isinstance(item, dict) else None,
            "source_path": copy.deepcopy(source.get("source_path", [])),
            "raw_item_index": source.get("raw_item_index"),
            "text_preview": (
                _text_for_cleaning(item)[:240]
                if isinstance(item, dict) and _text_for_cleaning(item) is not None
                else None
            ),
        }
        log.append(entry)
    return cleaned, log


def _item_type(item: dict[str, Any]) -> str:
    return str(item.get("type", item.get("block_type", ""))).lower()


def _bbox_y(item: Any) -> tuple[float, float] | None:
    if not isinstance(item, dict):
        return None
    bbox = item.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    y0, y1 = bbox[1], bbox[3]
    if isinstance(y0, bool) or isinstance(y1, bool):
        return None
    if not isinstance(y0, (int, float)) or not isinstance(y1, (int, float)):
        return None
    return min(float(y0), float(y1)), max(float(y0), float(y1))


def _page_y_bounds(items: Sequence[Any]) -> dict[int, tuple[float, float]]:
    bounds: dict[int, list[float]] = {}
    for item in items:
        if not isinstance(item, dict) or type(item.get("page_idx")) is not int:
            continue
        y_range = _bbox_y(item)
        if y_range is None:
            continue
        bounds.setdefault(item["page_idx"], []).extend(y_range)
    return {page: (min(values), max(values)) for page, values in bounds.items()}


def _is_at_bottom(item: dict[str, Any], bounds: dict[int, tuple[float, float]]) -> bool:
    y_range = _bbox_y(item)
    page_bounds = bounds.get(item.get("page_idx"))
    if y_range is None or page_bounds is None:
        return False
    minimum, maximum = page_bounds
    span = maximum - minimum
    return span > 0 and y_range[1] >= minimum + (1 - _EDGE_RATIO) * span


def _is_at_top(item: dict[str, Any], bounds: dict[int, tuple[float, float]]) -> bool:
    y_range = _bbox_y(item)
    page_bounds = bounds.get(item.get("page_idx"))
    if y_range is None or page_bounds is None:
        return False
    minimum, maximum = page_bounds
    span = maximum - minimum
    return span > 0 and y_range[0] <= minimum + _EDGE_RATIO * span


def _looks_like_new_heading(text: str) -> bool:
    return any(pattern.match(text) for pattern in _HEADING_PATTERNS)


def _has_complete_ending(text: str) -> bool:
    text = text.rstrip()
    while text and text[-1] in _CLOSING_CHARS:
        text = text[:-1].rstrip()
    return bool(text) and text[-1] in _COMPLETE_ENDINGS


def _ends_with_colon(text: str) -> bool:
    text = text.rstrip()
    while text and text[-1] in _CLOSING_CHARS:
        text = text[:-1].rstrip()
    return bool(text) and text[-1] in {"：", ":"}


def _block_level(item: dict[str, Any]) -> int | None:
    for key in ("text_level", "heading_level", "level"):
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
    return None


def _is_body_text(item: dict[str, Any]) -> bool:
    return _item_type(item) in {"text", "paragraph"}


def _can_merge(
    previous: Any,
    next_item: Any,
    bounds: dict[int, tuple[float, float]],
) -> bool:
    if not isinstance(previous, dict) or not isinstance(next_item, dict):
        return False
    if not _is_body_text(previous) or not _is_body_text(next_item):
        return False
    if _item_type(next_item) in {"heading", "title"}:
        return False
    if _block_level(previous) is not None or _block_level(next_item) is not None:
        return False

    previous_page = previous.get("page_idx")
    next_page = next_item.get("page_idx")
    if type(previous_page) is not int or type(next_page) is not int:
        return False
    if next_page != previous_page + 1:
        return False
    if not _is_at_bottom(previous, bounds) or not _is_at_top(next_item, bounds):
        return False

    previous_text = previous.get("text")
    next_text = next_item.get("text")
    if not isinstance(previous_text, str) or not previous_text.strip():
        return False
    if not isinstance(next_text, str) or not next_text.strip():
        return False
    if _looks_like_new_heading(next_text):
        return False
    if _ends_with_colon(previous_text):
        return False
    return not _has_complete_ending(previous_text)


def _source_value(item: dict[str, Any], key: str, fallback: Any = None) -> Any:
    source = item.get(_SOURCE_KEY)
    if isinstance(source, dict) and key in source:
        return source[key]
    return fallback


def _merge_result(
    current: dict[str, Any],
    source_items: list[dict[str, Any]],
    next_item: dict[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(current)
    merged["text"] = merged["text"].rstrip() + next_item["text"].lstrip()
    all_sources = [*source_items, next_item]
    merged["start_page_idx"] = all_sources[0]["page_idx"]
    merged["end_page_idx"] = all_sources[-1]["page_idx"]
    merged["source_item_indices"] = [
        _source_value(item, "raw_item_index", index)
        for index, item in enumerate(all_sources)
    ]
    merged["source_page_indices"] = [item["page_idx"] for item in all_sources]
    merged["source_bboxes"] = [copy.deepcopy(item.get("bbox")) for item in all_sources]
    merged["source_paths"] = [
        copy.deepcopy(_source_value(item, "source_path", [])) for item in all_sources
    ]
    merged["merged_cross_page"] = True
    return merged


def merge_items(items: Sequence[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Merge only adjacent unfinished body text across a page boundary.

    The original sequence is never mutated. Every merge stores the contributing
    raw item indices, pages, bboxes, and source paths on the derived item.
    """

    bounds = _page_y_bounds(items)
    merged_items: list[Any] = []
    logs: list[dict[str, Any]] = []
    index = 0

    while index < len(items):
        current = copy.deepcopy(items[index])
        source_items = [items[index]] if isinstance(items[index], dict) else []
        next_index = index + 1

        while source_items and next_index < len(items):
            previous = source_items[-1]
            next_item = items[next_index]
            if not _can_merge(previous, next_item, bounds):
                break

            current = _merge_result(current, source_items, next_item)
            logs.append(
                {
                    "previous_index": next_index - 1,
                    "next_index": next_index,
                    "previous_page_idx": previous["page_idx"],
                    "next_page_idx": next_item["page_idx"],
                    "a": copy.deepcopy(previous),
                    "b": copy.deepcopy(next_item),
                    "merged": copy.deepcopy(current),
                }
            )
            source_items.append(next_item)
            next_index += 1

        merged_items.append(current)
        index = next_index if next_index > index + 1 else index + 1

    return merged_items, logs


class _TableStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            if self._row is not None and self._row:
                self.rows.append(self._row)
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None:
                self._row = []
            if self._cell is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            if self._row is None:
                self._row = []
            self._row.append("".join(self._cell or []).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:
                self._row.append("".join(self._cell).strip())
                self._cell = None
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _table_rows(table_body: Any) -> list[list[str]]:
    if not isinstance(table_body, str) or not table_body.strip():
        return []
    parser = _TableStructureParser()
    try:
        parser.feed(table_body)
        parser.close()
    except (TypeError, ValueError):
        return []
    if parser._row:
        if parser._cell is not None:
            parser._row.append("".join(parser._cell).strip())
        parser.rows.append(parser._row)
    return parser.rows


class _TableImageReferenceParser(HTMLParser):
    """Collect image references embedded in a table's HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def _collect(self, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() == "src" and isinstance(value, str) and value.strip():
                self.references.append(value.strip())
                break

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self._collect(attrs)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() == "img":
            self._collect(attrs)


def _canonical_image_reference(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().replace("\\", "/")


def _table_image_references(table_body: Any) -> list[str]:
    if not isinstance(table_body, str) or not table_body.strip():
        return []
    parser = _TableImageReferenceParser()
    try:
        parser.feed(table_body)
        parser.close()
    except (TypeError, ValueError):
        return []
    references = [
        reference
        for reference in (
            _canonical_image_reference(item) for item in parser.references
        )
        if reference is not None
    ]
    return list(dict.fromkeys(references))


def _structure_source(item: dict[str, Any], position: int) -> dict[str, Any]:
    source = item.get(_SOURCE_KEY)
    source_ref = copy.deepcopy(source) if isinstance(source, dict) else {}
    raw_index = source_ref.get("raw_item_index", position)
    source_ref.setdefault("raw_item_index", raw_index)
    source_ref.setdefault("source_path", [position])
    return source_ref


def _source_lists(
    item: dict[str, Any], source: dict[str, Any]
) -> tuple[list[Any], list[Any], list[Any]]:
    source_item_indices = item.get("source_item_indices")
    if not isinstance(source_item_indices, list):
        source_item_indices = [source.get("raw_item_index")]
    source_page_indices = item.get("source_page_indices")
    if not isinstance(source_page_indices, list):
        page_idx = item.get("page_idx")
        source_page_indices = [page_idx] if type(page_idx) is int else []
    source_bboxes = item.get("source_bboxes")
    if not isinstance(source_bboxes, list):
        source_bboxes = (
            [copy.deepcopy(item.get("bbox"))]
            if item.get("bbox") is not None
            else []
        )
    return (
        copy.deepcopy(source_item_indices),
        copy.deepcopy(source_page_indices),
        copy.deepcopy(source_bboxes),
    )


def _structure_level(item: dict[str, Any], kind: str) -> int | None:
    level = _block_level(item)
    if kind == "heading":
        return level or 1
    return level


def _structure_kind(item: dict[str, Any]) -> tuple[str, int | None]:
    raw_type = _item_type(item) or "paragraph"
    level = _block_level(item)
    if raw_type in {"title", "heading", "header"} or (
        raw_type in {"text", "paragraph"} and level is not None
    ):
        return "heading", level or 1
    if raw_type == "table":
        return "table", None
    if raw_type in {"image", "figure"}:
        return "image", None
    return "paragraph", None


def _structure_text(item: dict[str, Any], kind: str) -> str:
    if kind == "table":
        values = (item.get("table_body"), item.get("text"), item.get("html"))
    else:
        values = (item.get("text"), item.get("caption"), item.get("alt"))
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    if kind == "table":
        return "[MinerU table]"
    if kind == "image":
        return "[MinerU image]"
    return ""


def _unique_block_id(item: dict[str, Any], order: int, used: set[str]) -> str:
    candidate = item.get("block_id", item.get("id"))
    block_id = str(candidate) if candidate is not None and str(candidate) else f"b{order:04d}"
    if block_id in used:
        suffix = 2
        while f"{block_id}_{suffix}" in used:
            suffix += 1
        block_id = f"{block_id}_{suffix}"
    used.add(block_id)
    return block_id


def structure_content_list(
    items: Sequence[Any],
    *,
    source_filename: str,
    source_sha256: str | None = None,
    parser_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build section-aware blocks while retaining tables/images separately.

    ``items`` is expected to be the cleaned and optionally cross-page-merged
    list. The returned JSON-compatible object is deliberately independent from
    compliance checks, template matching, and retrieval indexes.
    """

    from app.compliance_extraction import StructuredBlock

    blocks: list[StructuredBlock] = []
    sections: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    unsupported_items: list[dict[str, Any]] = []
    section_stack: list[dict[str, Any]] = []
    used_block_ids: set[str] = set()
    images_by_reference: dict[str, dict[str, Any]] = {}
    table_embedded_references: set[str] = set()
    table_embedded_reference_count = 0
    table_embedded_reused_asset_count = 0
    deduplicated_image_reference_count = 0

    def register_image(
        reference: Any,
        *,
        common: dict[str, Any],
        caption: Any,
        source_type: str,
        table_id: str | None = None,
    ) -> str | None:
        nonlocal deduplicated_image_reference_count
        canonical_reference = _canonical_image_reference(reference)
        if canonical_reference is None:
            return None

        source_reference = {
            "kind": source_type,
            "reference": canonical_reference,
            "block_id": common["block_id"],
            "section_id": common["section_id"],
            "section_path": copy.deepcopy(common["section_path"]),
            "order": common["order"],
            "source": copy.deepcopy(common["source"]),
        }
        if table_id is not None:
            source_reference.update(
                {
                    "table_id": table_id,
                    "table_block_id": common["block_id"],
                }
            )
        existing = images_by_reference.get(canonical_reference)
        if existing is not None:
            deduplicated_image_reference_count += 1
            source_key = (
                source_reference["kind"],
                source_reference["block_id"],
                source_reference.get("table_id"),
                source_reference["reference"],
            )
            source_keys = {
                (
                    item.get("kind"),
                    item.get("block_id"),
                    item.get("table_id"),
                    item.get("reference"),
                )
                for item in existing.get("source_references", [])
                if isinstance(item, dict)
            }
            if source_key not in source_keys:
                existing.setdefault("source_references", []).append(source_reference)
            if table_id is not None:
                existing.setdefault("source_table_id", table_id)
                existing.setdefault("source_table_block_id", common["block_id"])
                existing.setdefault("source_table_ids", [])
                if table_id not in existing["source_table_ids"]:
                    existing["source_table_ids"].append(table_id)
                existing.setdefault("source_table_block_ids", [])
                if common["block_id"] not in existing["source_table_block_ids"]:
                    existing["source_table_block_ids"].append(common["block_id"])
            if common["block_id"] not in existing.setdefault("source_block_ids", []):
                existing["source_block_ids"].append(common["block_id"])
            if common["section_id"] is not None:
                existing.setdefault("section_ids", [])
                if common["section_id"] not in existing["section_ids"]:
                    existing["section_ids"].append(common["section_id"])
            existing.setdefault("section_paths", [])
            if common["section_path"] not in existing["section_paths"]:
                existing["section_paths"].append(copy.deepcopy(common["section_path"]))
            existing["source_reference_count"] = len(existing.get("source_references", []))
            return str(existing["image_id"])

        image = {
            "image_id": f"i{len(images) + 1:04d}",
            **copy.deepcopy(common),
            "img_path": canonical_reference,
            "caption": caption if isinstance(caption, str) else "",
            "source_type": source_type,
            "source_references": [source_reference],
            "source_block_ids": [common["block_id"]],
            "section_ids": (
                [common["section_id"]] if common["section_id"] is not None else []
            ),
            "section_paths": [copy.deepcopy(common["section_path"])],
            "source_reference_count": 1,
        }
        if table_id is not None:
            image.update(
                {
                    "source_table_id": table_id,
                    "source_table_block_id": common["block_id"],
                    "source_table_ids": [table_id],
                    "source_table_block_ids": [common["block_id"]],
                }
            )
        images.append(image)
        images_by_reference[canonical_reference] = image
        return str(image["image_id"])

    def append_unresolved_image(*, common: dict[str, Any], caption: str) -> None:
        images.append(
            {
                "image_id": f"i{len(images) + 1:04d}",
                **common,
                "img_path": None,
                "caption": caption,
                "source_type": "image",
                "source_references": [],
                "source_block_ids": [common["block_id"]],
                "section_ids": (
                    [common["section_id"]]
                    if common["section_id"] is not None
                    else []
                ),
                "section_paths": [copy.deepcopy(common["section_path"])],
                "source_reference_count": 0,
            }
        )

    for position, raw in enumerate(items):
        if not isinstance(raw, dict):
            unsupported_items.append(
                {"position": position, "item": copy.deepcopy(raw)}
            )
            continue

        kind, heading_level = _structure_kind(raw)
        text = _structure_text(raw, kind)
        if not text and kind == "paragraph":
            unsupported_items.append(
                {"position": position, "item": copy.deepcopy(raw), "reason": "no_text"}
            )
            continue

        order = len(blocks) + 1
        block_id = _unique_block_id(raw, order, used_block_ids)
        source = _structure_source(raw, position)
        source_item_indices, source_page_indices, source_bboxes = _source_lists(
            raw, source
        )

        if kind == "heading":
            level = heading_level or 1
            while section_stack and section_stack[-1]["level"] >= level:
                section_stack.pop()
            parent = section_stack[-1] if section_stack else None
            section = {
                "section_id": f"s{len(sections) + 1:04d}",
                "parent_section_id": parent["section_id"] if parent else None,
                "level": level,
                "title": text,
                "path": [
                    *(parent["path"] if parent else []),
                    text,
                ],
                "start_order": order,
                "end_order": order,
                "block_ids": [],
                "direct_block_ids": [],
            }
            sections.append(section)
            section_stack.append(section)

        current_section = section_stack[-1] if section_stack else None
        section_path = [section["title"] for section in section_stack]
        section_name = current_section["title"] if current_section else ""

        for section in section_stack:
            section["block_ids"].append(block_id)
            section["end_order"] = order
        if current_section is not None:
            current_section["direct_block_ids"].append(block_id)

        metadata: dict[str, Any] = {
            "mineru_raw_type": _item_type(raw) or "paragraph",
            "mineru_source_index": source.get("raw_item_index"),
            "mineru_source": copy.deepcopy(source),
            "source_item_indices": source_item_indices,
            "source_page_indices": source_page_indices,
            "source_bboxes": source_bboxes,
            "source_paths": copy.deepcopy(raw.get("source_paths", [source["source_path"]])),
            "section_id": current_section["section_id"] if current_section else None,
            "section_path": section_path,
            "mineru_item": copy.deepcopy(raw),
        }
        for key, value in raw.items():
            if key not in {"type", "block_type", "text", _SOURCE_KEY}:
                metadata.setdefault(key, copy.deepcopy(value))

        block = StructuredBlock(
            block_id=block_id,
            type=kind,  # type: ignore[arg-type]
            text=text,
            section=section_name,
            order=order,
            metadata=metadata,
            heading_level=heading_level if kind == "heading" else None,
        )
        blocks.append(block)

        common = {
            "block_id": block_id,
            "section_id": current_section["section_id"] if current_section else None,
            "section_path": section_path,
            "order": order,
            "page_idx": raw.get("page_idx"),
            "bbox": copy.deepcopy(raw.get("bbox")),
            "source": copy.deepcopy(source),
            "source_item_indices": source_item_indices,
            "source_page_indices": source_page_indices,
            "source_bboxes": source_bboxes,
        }
        if kind == "table":
            table_body = raw.get("table_body", raw.get("html", text))
            table_id = f"t{len(tables) + 1:04d}"
            table_image_ids: list[str] = []
            table_asset_reference = _canonical_image_reference(raw.get("img_path"))
            if table_asset_reference is not None:
                image_id = register_image(
                    table_asset_reference,
                    common=common,
                    caption=raw.get("table_caption", raw.get("caption")),
                    source_type="table_asset",
                    table_id=table_id,
                )
                if image_id is not None:
                    table_image_ids.append(image_id)
            embedded_references = _table_image_references(table_body)
            table_embedded_reference_count += len(embedded_references)
            for reference in embedded_references:
                canonical_reference = _canonical_image_reference(reference)
                if canonical_reference is None:
                    continue
                table_embedded_references.add(canonical_reference)
                if canonical_reference in images_by_reference:
                    table_embedded_reused_asset_count += 1
                image_id = register_image(
                    canonical_reference,
                    common=common,
                    caption=raw.get("table_caption", raw.get("caption")),
                    source_type="table_embedded",
                    table_id=table_id,
                )
                if image_id is not None and image_id not in table_image_ids:
                    table_image_ids.append(image_id)
            tables.append(
                {
                    "table_id": table_id,
                    **common,
                    "table_body": table_body if isinstance(table_body, str) else "",
                    "rows": _table_rows(table_body),
                    "caption": raw.get("table_caption", raw.get("caption")),
                    "img_path": table_asset_reference,
                    "image_ids": table_image_ids,
                }
            )
            metadata["image_ids"] = table_image_ids
        elif kind == "image":
            caption = text if not text.startswith("[MinerU ") else ""
            if register_image(
                raw.get("img_path"),
                common=common,
                caption=caption,
                source_type="image",
            ) is None:
                append_unresolved_image(common=common, caption=caption)

    block_dicts = [asdict(block) for block in blocks]
    page_indices = {
        page
        for block in blocks
        for page in block.metadata.get("source_page_indices", [])
        if type(page) is int
    }
    type_counts = {kind: 0 for kind in ("heading", "paragraph", "table", "image")}
    for block in blocks:
        type_counts[block.type] += 1

    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "source": {
            "filename": str(source_filename),
            "sha256": source_sha256,
        },
        "diagnostics": {
            **copy.deepcopy(parser_diagnostics or {}),
            "table_embedded_image_reference_count": table_embedded_reference_count,
            "table_embedded_image_unique_reference_count": len(
                table_embedded_references
            ),
            "table_embedded_image_asset_count": sum(
                any(
                    isinstance(reference, dict)
                    and reference.get("kind") == "table_embedded"
                    for reference in image.get("source_references", [])
                )
                for image in images
            ),
            "table_embedded_image_reused_asset_count": table_embedded_reused_asset_count,
            "deduplicated_image_reference_count": deduplicated_image_reference_count,
        },
        "blocks": block_dicts,
        "sections": sections,
        "tables": tables,
        "images": images,
        "unsupported_items": unsupported_items,
        "stats": {
            "block_count": len(blocks),
            "block_type_counts": type_counts,
            "section_count": len(sections),
            "table_count": len(tables),
            "image_count": len(images),
            "page_count": len(page_indices),
            "unsupported_item_count": len(unsupported_items),
        },
    }


class BidDocumentCleaningError(RuntimeError):
    """Raised when MinerU output cannot be converted into audit artifacts."""


class MinerUBidDocumentParser:
    """Fetch MinerU content and write traceable bid-document artifacts."""

    def __init__(
        self,
        mineru_url: str | None = None,
        *,
        mineru_api_key: str | None = None,
        mineru_backend: str | None = None,
        mineru_server_url: str | None = None,
        timeout_seconds: float = DEFAULT_MINERU_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_MINERU_POLL_INTERVAL_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.mineru_url = mineru_url
        self.mineru_api_key = mineru_api_key
        self.mineru_backend = mineru_backend or DEFAULT_MINERU_BACKEND
        self.mineru_server_url = mineru_server_url
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._http_client = http_client

    def parse(self, path: Path, *, output_dir: Path) -> dict[str, Any]:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        artifact_dir = Path(output_dir).expanduser().resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)

        result_zip, task_id = self._run_mineru_task(source_path)
        payload, raw_content_bytes, content_member, zip_diagnostics = (
            self._content_list_from_zip(result_zip)
        )
        raw_item_count = len(_expanded_payload_items(payload))
        flattened = flatten_mineru_content_list(payload)
        cleaned, cleaning_log = clean_items(flattened)
        merged, merge_log = merge_items(cleaned)

        source_sha256 = _sha256_file(source_path)
        diagnostics = {
            "parser": "mineru_bid_document_cleaner",
            "service_protocol": MINERU_TASKS_PROTOCOL_LABEL,
            "protocol_version": MINERU_TASKS_PROTOCOL_VERSION,
            "task_id": task_id,
            "mineru_backend": self.mineru_backend,
            "content_member": content_member,
            **zip_diagnostics,
        }
        document = structure_content_list(
            merged,
            source_filename=source_path.name,
            source_sha256=source_sha256,
            parser_diagnostics=diagnostics,
        )
        diagnostics.update(document.get("diagnostics", {}))
        asset_statuses, asset_diagnostics = self._extract_assets(
            result_zip,
            content_member=content_member,
            payload=payload,
            output_dir=artifact_dir,
        )
        diagnostics.update(asset_diagnostics)
        self._apply_asset_statuses(document, asset_statuses)
        document["diagnostics"] = copy.deepcopy(diagnostics)

        artifacts = {
            "raw_content_list": artifact_dir / "raw_content_list.json",
            "cleaned_content_list": artifact_dir / "cleaned_content_list.json",
            "merged_content_list": artifact_dir / "merged_content_list.json",
            "structured_document": artifact_dir / "structured_document.json",
            "cleaning_summary": artifact_dir / "cleaning_summary.json",
            "cleaning_log": artifact_dir / "cleaning_log.json",
            "merge_log": artifact_dir / "merge_log.json",
        }
        # The raw content list is copied byte-for-byte from the MinerU ZIP.
        _write_bytes_atomic(artifacts["raw_content_list"], raw_content_bytes)
        _write_json_atomic(artifacts["cleaned_content_list"], cleaned)
        _write_json_atomic(artifacts["merged_content_list"], merged)
        _write_json_atomic(artifacts["structured_document"], document)
        _write_json_atomic(artifacts["cleaning_log"], cleaning_log)
        _write_json_atomic(artifacts["merge_log"], merge_log)

        stats = {
            "raw_item_count": raw_item_count,
            "flattened_item_count": len(flattened),
            "cleaned_item_count": len(cleaned),
            "merged_item_count": len(merged),
            "structured_block_count": document["stats"]["block_count"],
            "section_count": document["stats"]["section_count"],
            "table_count": document["stats"]["table_count"],
            "image_count": document["stats"]["image_count"],
            "page_count": document["stats"]["page_count"],
            "cleaned_out_count": len(cleaning_log),
            "cross_page_merge_count": len(merge_log),
            "asset_ready_count": sum(
                1
                for status in asset_statuses.values()
                if status == "ready"
            ),
        }
        summary = {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "source": {
                "filename": source_path.name,
                "path": str(source_path),
                "sha256": source_sha256,
                "size": source_path.stat().st_size,
            },
            "stats": stats,
            "diagnostics": diagnostics,
            "artifacts": {key: str(value) for key, value in artifacts.items()},
        }
        _write_json_atomic(artifacts["cleaning_summary"], summary)

        return {
            "status": "success",
            "document_name": source_path.name,
            "artifact_dir": str(artifact_dir),
            "artifacts": {key: str(value) for key, value in artifacts.items()},
            "stats": stats,
            "diagnostics": diagnostics,
        }

    def _run_mineru_task(self, path: Path) -> tuple[bytes, str]:
        if not (self.mineru_url or "").strip():
            raise BidDocumentCleaningError("MinerU 服务未配置，请配置 MINERU_URL。")
        if self.mineru_backend not in SUPPORTED_MINERU_BACKENDS:
            raise BidDocumentCleaningError(
                "MinerU backend 仅支持 hybrid-engine 或 hybrid-http-client。"
            )
        if self.mineru_backend == "hybrid-http-client" and not (
            self.mineru_server_url or ""
        ).strip():
            raise BidDocumentCleaningError(
                "hybrid-http-client 模式下必须配置 MinerU server_url。"
            )
        if self.timeout_seconds <= 0 or self.poll_interval_seconds < 0:
            raise BidDocumentCleaningError("MinerU 超时或轮询间隔配置无效。")

        base_url = str(self.mineru_url).strip().rstrip("/")
        headers = (
            {"Authorization": f"Bearer {self.mineru_api_key.strip()}"}
            if self.mineru_api_key and self.mineru_api_key.strip()
            else {}
        )
        form = {
            "parse_method": "auto",
            "effort": "medium",
            "formula_enable": "true",
            "table_enable": "true",
            "image_analysis": "false",
            "return_md": "false",
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_content_list": "true",
            "return_images": "true",
            "response_format_zip": "true",
            "backend": self.mineru_backend,
        }
        if self.mineru_backend == "hybrid-http-client":
            form["server_url"] = str(self.mineru_server_url).strip().rstrip("/")

        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(
            timeout=httpx.Timeout(
                connect=30.0,
                read=self.timeout_seconds,
                write=self.timeout_seconds,
                pool=30.0,
            ),
            trust_env=False,
            follow_redirects=False,
        )
        try:
            try:
                with path.open("rb") as source:
                    response = client.post(
                        f"{base_url}/tasks",
                        data=form,
                        files={
                            "files": (
                                path.name,
                                source,
                                mimetypes.guess_type(path.name)[0]
                                or "application/octet-stream",
                            )
                        },
                        headers=headers,
                        follow_redirects=False,
                    )
            except (OSError, httpx.HTTPError) as exc:
                raise BidDocumentCleaningError(
                    f"MinerU 任务提交失败：{type(exc).__name__}。"
                ) from exc
            if response.status_code != 202:
                raise BidDocumentCleaningError(
                    f"MinerU 任务提交失败：HTTP {response.status_code}。"
                )
            submission = self._json_object(response, "任务提交")
            task_id = submission.get("task_id")
            status_url = self._trusted_task_url(
                submission.get("status_url"), base_url
            )
            result_url = self._trusted_task_url(
                submission.get("result_url"), base_url
            )
            if not isinstance(task_id, str) or not task_id:
                raise BidDocumentCleaningError("MinerU 返回了无效任务响应。")

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                try:
                    status_response = client.get(
                        status_url,
                        headers=headers,
                        follow_redirects=False,
                    )
                except httpx.HTTPError as exc:
                    raise BidDocumentCleaningError(
                        f"MinerU 任务状态查询失败：{type(exc).__name__}。"
                    ) from exc
                if status_response.status_code != 200:
                    raise BidDocumentCleaningError(
                        f"MinerU 任务状态查询失败：HTTP {status_response.status_code}。"
                    )
                status_payload = self._json_object(status_response, "任务状态")
                status = status_payload.get("status")
                if status == "completed":
                    break
                if status == "failed":
                    raise BidDocumentCleaningError("MinerU 任务处理失败。")
                if status not in {"pending", "processing"}:
                    raise BidDocumentCleaningError(
                        f"MinerU 返回未知任务状态：{status!r}。"
                    )
                if self.poll_interval_seconds:
                    time.sleep(self.poll_interval_seconds)
            else:
                raise BidDocumentCleaningError("MinerU 任务等待超时。")

            try:
                result_response = client.get(
                    result_url,
                    headers=headers,
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                raise BidDocumentCleaningError(
                    f"MinerU 结果下载失败：{type(exc).__name__}。"
                ) from exc
            if result_response.status_code != 200:
                raise BidDocumentCleaningError(
                    f"MinerU 结果下载失败：HTTP {result_response.status_code}。"
                )
            return result_response.content, task_id
        finally:
            if owns_client:
                client.close()

    @staticmethod
    def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BidDocumentCleaningError(
                f"MinerU {label}响应不是有效 JSON。"
            ) from exc
        if not isinstance(payload, dict):
            raise BidDocumentCleaningError(f"MinerU {label}响应必须是 JSON 对象。")
        return payload

    @staticmethod
    def _trusted_task_url(value: Any, base_url: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise BidDocumentCleaningError("MinerU 返回了无效任务 URL。")
        base = urllib.parse.urlsplit(f"{base_url}/")
        resolved = urllib.parse.urlsplit(
            urllib.parse.urljoin(f"{base_url}/", value)
        )
        if (
            resolved.scheme,
            resolved.hostname,
            resolved.port,
        ) != (base.scheme, base.hostname, base.port) or resolved.fragment:
            raise BidDocumentCleaningError("MinerU 返回了不可信任务 URL。")
        return urllib.parse.urlunsplit(resolved)

    @staticmethod
    def _content_list_from_zip(
        raw: bytes,
    ) -> tuple[Any, bytes, str, dict[str, Any]]:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                safe_members: list[zipfile.ZipInfo] = []
                unsafe_members: list[str] = []
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    normalized = info.filename.replace("\\", "/")
                    safe_path = PurePosixPath(normalized)
                    if safe_path.is_absolute() or ".." in safe_path.parts:
                        unsafe_members.append(info.filename)
                        continue
                    safe_members.append(info)

                content_members = [
                    info
                    for info in safe_members
                    if info.filename.replace("\\", "/").lower().endswith(
                        (
                            "content_list_v2.json",
                            "_content_list_v2.json",
                            "content_list.json",
                            "_content_list.json",
                        )
                    )
                ]
                if not content_members:
                    raise BidDocumentCleaningError(
                        "MinerU 结果 ZIP 未返回 content list。"
                    )
                v2_members = [
                    info
                    for info in content_members
                    if info.filename.replace("\\", "/").lower().endswith(
                        ("content_list_v2.json", "_content_list_v2.json")
                    )
                ]
                if len(v2_members) > 1:
                    raise BidDocumentCleaningError(
                        "MinerU 结果 ZIP 返回了多个 content_list_v2。"
                    )
                selected = v2_members[0] if v2_members else content_members[0]
                raw_content_bytes = archive.read(selected)
                payload = json.loads(raw_content_bytes)
        except BidDocumentCleaningError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise BidDocumentCleaningError("无法读取 MinerU 结果 ZIP。") from exc
        return payload, raw_content_bytes, selected.filename, {
            "unsafe_zip_member_count": len(unsafe_members),
            "unsafe_zip_members": unsafe_members[:20],
        }

    @staticmethod
    def _extract_assets(
        raw_zip: bytes,
        *,
        content_member: str,
        payload: Any,
        output_dir: Path,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        references: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"img_path", "image_path", "table_img_path"} and isinstance(
                        child, str
                    ):
                        reference = _canonical_image_reference(child)
                        if reference is not None:
                            references.append(reference)
                    elif (
                        key == "image_source"
                        and isinstance(child, dict)
                        and isinstance(child.get("path"), str)
                    ):
                        reference = _canonical_image_reference(child["path"])
                        if reference is not None:
                            references.append(reference)
                    elif key in {"table_body", "html"}:
                        references.extend(_table_image_references(child))
                    else:
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(payload)
        statuses: dict[str, str] = {}
        unsafe_references: list[str] = []
        missing_references: list[str] = []
        output_root = output_dir.resolve()
        content_parent = PurePosixPath(content_member.replace("\\", "/")).parent

        try:
            archive = zipfile.ZipFile(io.BytesIO(raw_zip))
        except (OSError, zipfile.BadZipFile) as exc:
            raise BidDocumentCleaningError("无法读取 MinerU 结果 ZIP。") from exc
        with archive:
            members = {
                info.filename.replace("\\", "/"): info
                for info in archive.infolist()
                if not info.is_dir()
                and not PurePosixPath(info.filename.replace("\\", "/")).is_absolute()
                and ".." not in PurePosixPath(info.filename.replace("\\", "/")).parts
            }
            for reference in dict.fromkeys(references):
                normalized = reference.replace("\\", "/")
                reference_path = PurePosixPath(normalized)
                if reference_path.is_absolute() or ".." in reference_path.parts:
                    statuses[reference] = "unsafe"
                    unsafe_references.append(reference)
                    continue
                candidate_names = [
                    str(content_parent / reference_path),
                    str(reference_path),
                ]
                member_name = next(
                    (name for name in candidate_names if name in members),
                    None,
                )
                output_relative = reference_path
                if not output_relative.parts or output_relative.parts[0] != "images":
                    output_relative = PurePosixPath("images") / output_relative
                destination = (output_root / Path(*output_relative.parts)).resolve()
                if output_root not in destination.parents:
                    statuses[reference] = "unsafe"
                    unsafe_references.append(reference)
                    continue
                if member_name is None:
                    statuses[reference] = "missing"
                    missing_references.append(reference)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write_bytes_atomic(destination, archive.read(members[member_name]))
                statuses[reference] = "ready"

        return statuses, {
            "asset_reference_count": len(set(references)),
            "asset_unsafe_reference_count": len(unsafe_references),
            "asset_missing_reference_count": len(missing_references),
            "asset_unsafe_references": unsafe_references[:20],
            "asset_missing_references": missing_references[:20],
        }

    @staticmethod
    def _apply_asset_statuses(
        document: dict[str, Any], statuses: dict[str, str]
    ) -> None:
        for collection_name in ("images", "tables"):
            for entry in document.get(collection_name, []):
                reference = entry.get("img_path")
                if not isinstance(reference, str) or not reference:
                    if collection_name == "images":
                        entry["asset_status"] = "unresolved"
                    continue
                canonical_reference = _canonical_image_reference(reference)
                status = (
                    statuses.get(canonical_reference, "missing")
                    if canonical_reference is not None
                    else "missing"
                )
                entry["asset_status"] = status
                block_id = entry.get("block_id")
                for block in document.get("blocks", []):
                    if block.get("block_id") == block_id:
                        block.setdefault("metadata", {})["asset_status"] = status


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def parse_bid_document(
    bid_file: Any,
    *,
    parser: MinerUBidDocumentParser | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Workflow adapter for a bid ``FileMetadata`` object."""

    source_path = Path(bid_file.storage_path)
    if parser is None:
        from app.config import load_settings

        settings = load_settings()
        parser = MinerUBidDocumentParser(
            settings.mineru_url,
            mineru_api_key=settings.mineru_api_key,
            mineru_backend=settings.mineru_backend,
            mineru_server_url=settings.mineru_server_url,
            timeout_seconds=settings.mineru_timeout_seconds,
            poll_interval_seconds=settings.mineru_poll_interval_seconds,
        )
    result = parser.parse(
        source_path,
        output_dir=output_dir
        or source_path.parent / "bid_document_cleaning",
    )
    if isinstance(result, dict):
        result["document_name"] = str(
            getattr(bid_file, "filename", source_path.name)
        )
    return result


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="清洗 MinerU 投标文件结果")
    parser.add_argument("input_file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    result = parse_bid_document(
        type("BidFile", (), {"storage_path": str(args.input_file)})(),
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
