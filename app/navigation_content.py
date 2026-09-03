"""Shared recognition and exclusion rules for document navigation content.

Navigation objects are kept in the MinerU and cleaned-document artifacts.  This
module only decides whether an item is eligible for compliance candidates; it
does not remove or rewrite the source object.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


_RAW_NAVIGATION_TYPES = {
    "index",
    "toc",
    "table_of_contents",
    "table-of-contents",
}
_TOC_FIELD_RE = re.compile(r"(?i)\b(?:toc|pageref|hyperlink)\b")
_PAGE_REFERENCE_RE = re.compile(
    r"(?:页码|页次|页号|所在页|对应页|页数|章节号|条款号)"
)
_NAVIGATION_SUFFIX_RE = re.compile(
    r"(?:目录|目录表|目录导航|导航|导航目录|导航表|索引|索引表|索引目录)$"
)
_NAVIGATION_CONTEXT_RE = re.compile(
    r"(?:评审|投标|响应|文件|商务|技术|初步|资格|材料|清单|条款|服务|报价)"
)
_NAVIGATION_ROW_RE = re.compile(
    r"(?:\.{2,}|…{2,}|·{3,}|。{3,})\s*"
    r"[0-9０-９]+(?:\s*[-~—至]\s*[0-9０-９]+)?\s*$"
)
_NAVIGATION_TABLE_RE = re.compile(
    r"(?:评审因素.{0,40}投标文件组成|投标文件组成.{0,40}(?:对应)?页|"
    r"目录.{0,30}(?:页码|页次)|索引.{0,30}(?:页码|页次))"
)
_BUSINESS_FORM_RE = re.compile(
    r"(?:投标函|承诺函|承诺书|授权委托书|资格审查资料|资格证明|营业执照|"
    r"身份证|技术参数响应表|法定代表人|联合体协议书|报价表)"
)


def _normalized_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"<[^>]+>", " ", text)
    return text.replace("\u00a0", " ").strip()


def _iter_text(value: Any, *, key: str = "") -> Iterable[str]:
    if isinstance(value, str):
        text = _normalized_text(value)
        if text:
            yield text
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key in {
                "id",
                "block_id",
                "section_id",
                "source_block_ids",
                "block_ids",
                "raw_item_index",
                "source_path",
            }:
                continue
            yield from _iter_text(child, key=str(child_key))
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_text(child, key=key)


def _item_text(item: dict[str, Any]) -> str:
    # Explicitly enumerate content-bearing fields first so a structured
    # section and a tender template receive the same evidence treatment.
    fields = (
        "name",
        "title",
        "body",
        "text",
        "source_text",
        "content",
        "table_body",
        "tables",
        "blocks",
        "child_sections",
        "metadata",
        "source",
    )
    parts: list[str] = []
    seen: set[str] = set()
    for field in fields:
        value = item.get(field)
        for text in _iter_text(value, key=field):
            if text not in seen:
                seen.add(text)
                parts.append(text)
    return "\n".join(parts)


def _item_title(item: dict[str, Any]) -> str:
    title = item.get("name", item.get("title", ""))
    title = _normalized_text(title)
    # Matching titles can include a structural chapter/section number.  It is
    # irrelevant to the navigation decision but should not hide a suffix.
    return re.sub(
        r"^\s*(?:(?:第\s*)?\d+(?:\.\d+)*|第[一二三四五六七八九十百千万]+章)"
        r"\s*[、.．:：\-—]*\s*",
        "",
        title,
    ).strip()


def _has_raw_navigation_type(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"type", "block_type", "mineru_parent_type", "content_type"}:
                if _normalized_text(child).casefold() in _RAW_NAVIGATION_TYPES:
                    return True
            if _has_raw_navigation_type(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_has_raw_navigation_type(child) for child in value)
    return False


def _navigation_title(title: str) -> bool:
    if not title or not _NAVIGATION_SUFFIX_RE.search(title):
        return False
    if title in {
        "目录",
        "目录表",
        "目录导航",
        "导航",
        "导航目录",
        "导航表",
        "索引",
        "索引表",
        "索引目录",
    }:
        return True
    if title.endswith(("目录", "目录表", "目录导航", "导航", "导航目录", "导航表")):
        return True
    return bool(_NAVIGATION_CONTEXT_RE.search(title))


def classify_navigation_item(item: dict[str, Any]) -> dict[str, Any]:
    """Classify one tender template or bid section by navigation purpose.

    The classifier intentionally requires either a strong parser/content signal
    or a combination of navigation title and navigation structure.  A normal
    form, qualification material, commitment, or business table therefore does
    not become excluded merely because it contains a page or section word.
    """

    if not isinstance(item, dict):
        return {
            "is_navigation": False,
            "reason": "not_navigation",
            "signals": [],
        }

    title = _item_title(item)
    text = _item_text(item)
    compact_text = re.sub(r"\s+", "", text)
    signals: list[str] = []

    if _has_raw_navigation_type(item):
        signals.append("type:navigation")
    if _TOC_FIELD_RE.search(text):
        signals.append("field:toc")
    if _navigation_title(title):
        signals.append("title:navigation")

    page_reference = bool(_PAGE_REFERENCE_RE.search(text))
    blocks = item.get("blocks")
    table_block = isinstance(blocks, list) and any(
        isinstance(block, dict)
        and str(block.get("type", "")).casefold() == "table"
        for block in blocks
    )
    table_like = bool(
        item.get("tables")
        or item.get("table_count")
        or table_block
        or "|" in text
    )
    if page_reference and table_like:
        signals.append("table:page_reference")

    dotted_rows = sum(
        bool(_NAVIGATION_ROW_RE.search(line.strip()))
        for line in text.splitlines()
        if line.strip()
    )
    if dotted_rows >= 2:
        signals.append("rows:page_leaders")
    if _NAVIGATION_TABLE_RE.search(compact_text):
        signals.append("table:navigation_columns")

    strong_signal = any(
        signal in signals for signal in ("type:navigation", "field:toc")
    )
    title_signal = "title:navigation" in signals
    structure_signal = any(
        signal in signals
        for signal in (
            "table:page_reference",
            "table:navigation_columns",
            "rows:page_leaders",
        )
    )
    exact_navigation_title = title in {
        "目录",
        "目录表",
        "目录导航",
        "导航",
        "导航目录",
        "导航表",
        "索引",
        "索引表",
        "索引目录",
    }

    is_navigation = (
        strong_signal
        or (title_signal and (structure_signal or exact_navigation_title))
        or (
            any(
                signal in signals
                for signal in ("table:navigation_columns", "rows:page_leaders")
            )
            and not _BUSINESS_FORM_RE.search(title)
        )
    )

    return {
        "is_navigation": bool(is_navigation),
        "reason": "navigation_content" if is_navigation else "not_navigation",
        "signals": signals,
    }


def _exclusion_record(
    item: dict[str, Any], classification: dict[str, Any]
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "reason": classification["reason"],
        "signals": list(classification.get("signals", [])),
    }
    for key in (
        "id",
        "section_id",
        "name",
        "title",
        "section",
        "path",
        "block_ids",
    ):
        if key in item:
            record[key] = item[key]
    return record


def _filter_navigation_items(
    items: Iterable[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    remaining: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        classification = classify_navigation_item(item)
        if classification["is_navigation"]:
            excluded.append(_exclusion_record(item, classification))
        else:
            remaining.append(item)
    return remaining, excluded


def filter_navigation_templates(
    templates: Iterable[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return eligible tender templates and an audit list of exclusions."""

    return _filter_navigation_items(templates)


def filter_navigation_sections(
    sections: Iterable[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return eligible bid modules and an audit list of exclusions."""

    return _filter_navigation_items(sections)


__all__ = [
    "classify_navigation_item",
    "filter_navigation_sections",
    "filter_navigation_templates",
]
