from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Sequence
from typing import Any


_SOURCE_KEY = "_bid_source"
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
        and isinstance(payload[1], list)
        and all(isinstance(item, dict) for item in payload[1])
    ):
        return payload[1], [1]
    return payload, []


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

    items, prefix = _payload_items(payload)
    flattened: list[Any] = []
    for raw_item_index, raw in enumerate(items):
        source_path = [*prefix, raw_item_index]
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
