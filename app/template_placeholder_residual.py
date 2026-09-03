from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

from app.compliance_extraction import _EXTERNAL_PLACEHOLDER_LABEL_RE

_BRACKET_PAIRS = {
    "（": "）",
    "(": ")",
    "【": "】",
    "[": "]",
}
_EXTERNAL_PLACEHOLDER_PAIRS = {
    "（": "）",
    "[": "]",
}
_UNDERSCORE_SLOT_RE = re.compile(r"_{2,}|(?:_\s+_+)+")
_X_SLOT_RE = re.compile(r"[Xx]{2,}")
_NOT_APPLICABLE_TITLE_RE = re.compile(
    r"[（(][^（）()]*?(?:不适用|不涉及|无)[^（）()]*?[）)]\s*$"
)
_NOT_APPLICABLE_LINE_RE = re.compile(r"^(?:本项目)?(?:不适用|不涉及|无)[。；;，,：:]?$")


def _plain_text(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")
    text = html.unescape(text)
    text = re.sub(
        r"</(?:p|tr|div|br|li|h[1-6])\s*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", "", text)
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _block_text(block: dict[str, Any]) -> str:
    if str(block.get("type", "paragraph")) == "image":
        return ""
    rows = block.get("rows")
    if str(block.get("type", "paragraph")) == "table" and isinstance(rows, list):
        rendered_rows: list[str] = []
        for row in rows:
            cells = row if isinstance(row, list) else [row]
            rendered_rows.append(" | ".join(_plain_text(cell) for cell in cells))
        return "\n".join(rendered_rows)
    return _plain_text(block.get("text", ""))


def _iter_blocks(section: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [block for block in section.get("blocks", []) if isinstance(block, dict)]
    blocks.sort(
        key=lambda block: (block.get("order", 0), str(block.get("block_id", "")))
    )
    result = blocks[:]
    children = section.get("child_sections", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                result.extend(_iter_blocks(child))
    return result


def _paired_spans(text: str) -> list[tuple[int, int]]:
    closing_to_opening = {
        closing: opening for opening, closing in _BRACKET_PAIRS.items()
    }
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        if character in _BRACKET_PAIRS:
            stack.append((character, index))
            continue
        opening = closing_to_opening.get(character)
        if opening is None or not stack or stack[-1][0] != opening:
            continue
        _, start = stack.pop()
        spans.append((start, index + 1))
    return spans


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _field_markers(text: str, field: str) -> list[tuple[int, int, str]]:
    markers: list[tuple[int, int, str]] = []
    field_key = _compact(field)
    for start, end in sorted(
        _paired_spans(text), key=lambda span: (span[1] - span[0], span[0])
    ):
        if _compact(text[start + 1 : end - 1]) == field_key:
            markers.append((start, end, text[start:end]))
    return markers


def _external_placeholder_markers(text: str) -> list[tuple[int, int, str]]:
    """Return bracket labels recognized by the existing external-label rule."""

    markers: list[tuple[int, int, str]] = []
    for start, end in _paired_spans(text):
        opening = text[start]
        closing = text[end - 1]
        if _EXTERNAL_PLACEHOLDER_PAIRS.get(opening) != closing:
            continue
        label = text[start + 1 : end - 1]
        nested_pair_characters = "[]" if opening == "[" else "（）"
        if not 1 <= len(label) <= 60 or any(
            character in nested_pair_characters for character in label
        ):
            continue
        if not _EXTERNAL_PLACEHOLDER_LABEL_RE.search(_compact(label)):
            continue
        markers.append((start, end, text[start:end]))
    return markers


def _marker_sides(text: str, marker: tuple[int, int, str]) -> tuple[str, str]:
    marker_start, marker_end, _ = marker
    enclosing = [
        span
        for span in _paired_spans(text)
        if span[0] < marker_start and marker_end < span[1]
    ]
    if enclosing:
        start, end = min(enclosing, key=lambda span: span[1] - span[0])
        return text[start + 1 : marker_start], text[marker_end : end - 1]
    line_start = text.rfind("\n", 0, marker_start) + 1
    line_end = text.find("\n", marker_end)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:marker_start], text[marker_end:line_end]


def _context_key(value: str) -> str:
    return _compact(value).strip(" \t\r\n，,。；;：:、.!！?？")


def _external_marker_context(
    text: str,
    marker: tuple[int, int, str],
) -> str:
    return next(
        (
            side
            for side in _marker_sides(text, marker)
            if _context_key(side)
        ),
        "",
    )


def _has_enclosing_pair(text: str, marker: tuple[int, int, str]) -> bool:
    marker_start, marker_end, _ = marker
    return any(
        span[0] < marker_start and marker_end < span[1]
        for span in _paired_spans(text)
    )


def _has_filled_context(value: str) -> bool:
    if re.search(r"[:：]\s*$", value):
        return False
    context = _context_key(value)
    if not context or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", context):
        return False
    # An unchanged XX-style context is still only a placeholder.  This is
    # scoped to a field or external marker already identified from template
    # parsing, not a global XX rule.
    return not _X_SLOT_RE.search(context)


def _has_strong_standalone_filled_context(value: str) -> bool:
    context = _context_key(value)
    return _has_filled_context(value) and bool(re.search(r"[0-9A-Za-z]", context))


def _slot_signature(value: str, slot_res: list[re.Pattern[str]]) -> tuple[str, str]:
    signature = value
    without_slots = value
    for slot_re in slot_res:
        signature = slot_re.sub("<field-slot>", signature)
        without_slots = slot_re.sub("", without_slots)
    return _compact(signature), _compact(without_slots)


def _field_line_candidates(text: str, field: str) -> list[tuple[str, int]]:
    """Return source lines where a parsed field has a source-derived slot."""

    candidates: list[tuple[str, int]] = []
    for line in text.splitlines():
        for match in re.finditer(re.escape(field), line):
            tail = line[match.end() :]
            if not re.match(r"\s*[：:]", tail):
                continue
            slot_res: list[re.Pattern[str]] = []
            if _UNDERSCORE_SLOT_RE.search(tail):
                slot_res.append(_UNDERSCORE_SLOT_RE)
            if _X_SLOT_RE.search(tail):
                slot_res.append(_X_SLOT_RE)
            if not slot_res:
                continue
            candidates.append((line, match.end()))
    return candidates


def _source_slot_patterns(value: str) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    if _UNDERSCORE_SLOT_RE.search(value):
        patterns.append(_UNDERSCORE_SLOT_RE)
    if _X_SLOT_RE.search(value):
        patterns.append(_X_SLOT_RE)
    return patterns


def _not_applicable(section: dict[str, Any], blocks: list[dict[str, Any]]) -> bool:
    title = str(section.get("title", "")).strip()
    if _NOT_APPLICABLE_TITLE_RE.search(title):
        return True
    for block in blocks:
        for line in _block_text(block).splitlines():
            if _NOT_APPLICABLE_LINE_RE.fullmatch(line.strip()):
                return True
    return False


def _source_text(template: dict[str, Any]) -> str:
    body = template.get("body")
    if isinstance(body, str) and body.strip():
        return _plain_text(body)
    source = template.get("source")
    if isinstance(source, dict):
        return _plain_text(source.get("source_text", ""))
    return ""


def _hit_base(
    *,
    field: str,
    marker: str,
    template_marker: str,
    template: dict[str, Any],
    bid_block: dict[str, Any],
    template_context: str,
    actual_context: str,
) -> dict[str, Any]:
    source = template.get("source")
    source_block_ids = source.get("block_ids", []) if isinstance(source, dict) else []
    return {
        "field": field,
        "marker": marker,
        "template_marker": template_marker,
        "template_context": template_context.strip(),
        "actual_context": actual_context.strip(),
        "bid_text": _block_text(bid_block),
        "bid_block_id": bid_block.get("block_id", ""),
        "bid_block_order": bid_block.get("order", 0),
        "template_source_block_ids": [str(item) for item in source_block_ids],
        "template_id": str(template.get("id", "")),
    }


def find_template_placeholder_residuals(
    template: dict[str, Any],
    bid_section: dict[str, Any],
    materialized: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_fields = template.get("fields", [])
    fields = (
        [
            field.strip()
            for field in raw_fields
            if isinstance(field, str) and field.strip()
        ]
        if isinstance(raw_fields, list)
        else []
    )
    source_text = _source_text(template)
    bid_blocks = _iter_blocks(materialized)
    if not source_text or _not_applicable(bid_section, bid_blocks):
        return []

    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for field in fields:
        source_markers = _field_markers(source_text, field)
        for source_marker in source_markers:
            for bid_block in bid_blocks:
                bid_text = _block_text(bid_block)
                if not bid_text:
                    continue
                for bid_marker in _field_markers(bid_text, field):
                    source_sides = _marker_sides(source_text, source_marker)
                    bid_sides = _marker_sides(bid_text, bid_marker)
                    changed_sides = [
                        (source_side, bid_side)
                        for source_side, bid_side in zip(
                            source_sides, bid_sides, strict=True
                        )
                        if _context_key(bid_side)
                        and _context_key(bid_side) != _context_key(source_side)
                    ]
                    same_filled_sides = [
                        (source_side, bid_side)
                        for source_side, bid_side in zip(
                            source_sides, bid_sides, strict=True
                        )
                        if _context_key(bid_side)
                        and _context_key(bid_side) == _context_key(source_side)
                        and _has_filled_context(bid_side)
                        and (
                            (
                                _has_enclosing_pair(source_text, source_marker)
                                and _has_enclosing_pair(bid_text, bid_marker)
                            )
                            or _has_strong_standalone_filled_context(bid_side)
                        )
                    ]
                    context_pairs = changed_sides or same_filled_sides
                    if not context_pairs:
                        continue
                    template_context, actual_context = context_pairs[0]
                    key = (field, bid_marker[2], str(bid_block.get("block_id", "")))
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append(
                        _hit_base(
                            field=field,
                            marker=bid_marker[2],
                            template_marker=source_marker[2],
                            template=template,
                            bid_block=bid_block,
                            template_context=template_context,
                            actual_context=actual_context,
                        )
                    )

        for source_line, field_end in _field_line_candidates(source_text, field):
            source_tail = source_line[field_end:]
            slot_res = _source_slot_patterns(source_tail)
            if not slot_res:
                continue
            source_signature, source_without_slots = _slot_signature(
                source_line, slot_res
            )
            for bid_block in bid_blocks:
                for bid_line in _block_text(bid_block).splitlines():
                    for bid_match in re.finditer(re.escape(field), bid_line):
                        bid_tail = bid_line[bid_match.end() :]
                        if not re.match(r"\s*[：:]", bid_tail):
                            continue
                        if not any(slot_re.search(bid_tail) for slot_re in slot_res):
                            continue
                        bid_signature, bid_without_slots = _slot_signature(
                            bid_line, slot_res
                        )
                        if (
                            bid_signature == source_signature
                            or bid_without_slots == source_without_slots
                        ):
                            continue
                        marker_match = next(
                            slot_re.search(bid_tail)
                            for slot_re in slot_res
                            if slot_re.search(bid_tail)
                        )
                        assert marker_match is not None
                        marker = marker_match.group(0)
                        key = (
                            field,
                            marker,
                            str(bid_block.get("block_id", "")),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        hits.append(
                            _hit_base(
                                field=field,
                                marker=marker,
                                template_marker=source_line,
                                template=template,
                                bid_block=bid_block,
                                template_context=source_line,
                                actual_context=bid_line,
                            )
                        )

    for source_marker in _external_placeholder_markers(source_text):
        field = source_marker[2][1:-1]
        template_context = _external_marker_context(source_text, source_marker)
        source_is_nested = _has_enclosing_pair(source_text, source_marker)
        for bid_block in bid_blocks:
            bid_text = _block_text(bid_block)
            if not bid_text:
                continue
            for bid_marker in _external_placeholder_markers(bid_text):
                if bid_marker[2] != source_marker[2]:
                    continue
                actual_context = _external_marker_context(bid_text, bid_marker)
                if not _has_filled_context(actual_context):
                    continue
                if (
                    (not source_is_nested or not _has_enclosing_pair(bid_text, bid_marker))
                    and not _has_strong_standalone_filled_context(actual_context)
                ):
                    continue
                key = (field, bid_marker[2], str(bid_block.get("block_id", "")))
                if key in seen:
                    continue
                seen.add(key)
                hits.append(
                    _hit_base(
                        field=field,
                        marker=bid_marker[2],
                        template_marker=source_marker[2],
                        template=template,
                        bid_block=bid_block,
                        template_context=template_context,
                        actual_context=actual_context,
                    )
                )
    return hits
