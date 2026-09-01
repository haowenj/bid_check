from __future__ import annotations

import copy
import unicodedata
from collections.abc import Sequence
from typing import Any


_SOURCE_KEY = "_bid_source"


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
