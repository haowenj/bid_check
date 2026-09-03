from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

MATCH_STATUS_LABELS = {
    "matched": "已匹配",
    "unmatched": "未匹配",
    "ambiguous": "存在候选但不能确定",
}

_HEADING_NUMBER_RE = re.compile(
    r"^\s*(?:(?:第\s*)?\d+(?:\.\d+)*|第[一二三四五六七八九十百千万]+章)"
    r"\s*[、.．:：\-—]*\s*"
)
_APPLICABILITY_RE = re.compile(
    r"[（(]\s*(?:如有|本项目不适用|不适用|无)\s*[）)]"
)


def normalize_module_title(title: Any) -> str:
    """Normalize a module title for comparison without changing its display text."""

    text = unicodedata.normalize("NFKC", str(title or "")).strip()
    text = _APPLICABILITY_RE.sub("", text)
    text = _HEADING_NUMBER_RE.sub("", text)
    # Keep letters, numbers, and Chinese characters. This removes heading marks,
    # slash variants, OCR punctuation, and whitespace while preserving words.
    return "".join(char for char in text.casefold() if char.isalnum())


def _candidate_summary(section: dict[str, Any], *, score: float) -> dict[str, Any]:
    return {
        "section_id": section.get("section_id", ""),
        "title": section.get("title", "未命名章节"),
        "level": section.get("level", 1),
        "path": section.get("path", []),
        "block_count": section.get("block_count", 0),
        "table_count": section.get("table_count", 0),
        "image_count": section.get("image_count", 0),
        "subsection_count": section.get("subsection_count", 0),
        "score": round(score, 3),
    }


def _candidate_score(template_title: str, bid_title: str) -> tuple[float, str]:
    if not template_title or not bid_title:
        return 0.0, ""
    if (
        (template_title in bid_title or bid_title in template_title)
        and min(len(template_title), len(bid_title)) >= 4
    ):
        return 0.94, "标题包含关系候选"

    ratio = SequenceMatcher(None, template_title, bid_title).ratio()
    if min(len(template_title), len(bid_title)) >= 5 and ratio >= 0.72:
        return ratio, "标题相似候选"
    return 0.0, ""


def build_template_comparisons(
    templates: list[dict[str, Any]] | None,
    bid_sections: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build one comparison row for every extracted tender template.

    A unique normalized title is the only confirmed match. Fuzzy or containment
    matches remain candidates so the page does not silently make a business
    decision on behalf of the reviewer.
    """

    tender_templates = templates if isinstance(templates, list) else []
    sections = bid_sections if isinstance(bid_sections, list) else []
    normalized_sections = [
        (section, normalize_module_title(section.get("title", "")))
        for section in sections
        if isinstance(section, dict)
    ]

    comparisons: list[dict[str, Any]] = []
    for template in tender_templates:
        if not isinstance(template, dict):
            continue
        template_title = normalize_module_title(template.get("name", ""))
        exact_matches = [
            section
            for section, normalized_title in normalized_sections
            if template_title and normalized_title == template_title
        ]

        if len(exact_matches) == 1:
            section = exact_matches[0]
            candidate = _candidate_summary(section, score=1.0)
            comparisons.append(
                {
                    "status": "matched",
                    "status_label": MATCH_STATUS_LABELS["matched"],
                    "match_basis": "归一化标题完全一致",
                    "tender": template,
                    "bid": section,
                    "candidates": [candidate],
                    "candidate_count": 1,
                }
            )
            continue

        if len(exact_matches) > 1:
            candidates = [
                _candidate_summary(section, score=1.0)
                for section in exact_matches
            ]
            comparisons.append(
                {
                    "status": "ambiguous",
                    "status_label": MATCH_STATUS_LABELS["ambiguous"],
                    "match_basis": "归一化标题命中多个章节",
                    "tender": template,
                    "bid": None,
                    "candidates": candidates,
                    "candidate_count": len(candidates),
                }
            )
            continue

        scored_candidates: list[tuple[float, str, dict[str, Any]]] = []
        for section, normalized_title in normalized_sections:
            score, basis = _candidate_score(template_title, normalized_title)
            if score:
                scored_candidates.append((score, basis, section))

        scored_candidates.sort(key=lambda item: item[0], reverse=True)
        if scored_candidates:
            best_score, basis, _ = scored_candidates[0]
            # Keep close alternatives visible. This is deliberately a candidate
            # state even when there is only one fuzzy result.
            candidates = [
                _candidate_summary(section, score=score)
                for score, _, section in scored_candidates
                if score >= max(0.72, best_score - 0.08)
            ][:5]
            comparisons.append(
                {
                    "status": "ambiguous",
                    "status_label": MATCH_STATUS_LABELS["ambiguous"],
                    "match_basis": basis,
                    "tender": template,
                    "bid": None,
                    "candidates": candidates,
                    "candidate_count": len(candidates),
                }
            )
            continue

        comparisons.append(
            {
                "status": "unmatched",
                "status_label": MATCH_STATUS_LABELS["unmatched"],
                "match_basis": "没有足够确定的对应章节",
                "tender": template,
                "bid": None,
                "candidates": [],
                "candidate_count": 0,
            }
        )

    return comparisons
