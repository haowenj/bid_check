"""Statistics and JSON output for normalized document blocks."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from .models import DocumentBlock


def build_report(
    blocks: Sequence[DocumentBlock], top_longest: int = 5
) -> dict[str, object]:
    lengths = [len(block.text) for block in blocks]
    type_counts = Counter(block.block_type.value for block in blocks)
    title_counts = Counter(
        str(block.title_level)
        for block in blocks
        if block.title_level is not None
    )
    source_counts = Counter(block.source_type for block in blocks)
    warning_counts = Counter(
        warning
        for block in blocks
        for warning in block.metadata.get("normalization_warnings", [])
        if isinstance(warning, str)
    )

    unique_paths: list[list[str]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for block in blocks:
        path = tuple(block.section_path)
        if path and path not in seen_paths:
            seen_paths.add(path)
            unique_paths.append(list(path))
        if len(unique_paths) == 5:
            break

    ranked = sorted(
        enumerate(blocks), key=lambda pair: (-len(pair[1].text), pair[0])
    )[: max(0, top_longest)]
    longest = [
        {
            "id": block.id,
            "block_type": block.block_type.value,
            "length": len(block.text),
            "preview": _preview(block.text),
            "section_path": list(block.section_path),
        }
        for _, block in ranked
    ]

    return {
        "block_count": len(blocks),
        "block_type_counts": dict(sorted(type_counts.items())),
        "text_length": {
            "total": sum(lengths),
            "average": (sum(lengths) / len(lengths)) if lengths else 0.0,
            "median": median(lengths) if lengths else 0,
            "maximum": max(lengths) if lengths else 0,
        },
        "title_level_counts": dict(sorted(title_counts.items(), key=lambda item: int(item[0]))),
        "section_path_examples": unique_paths,
        "source_type_counts": dict(sorted(source_counts.items())),
        "warning_counts": dict(sorted(warning_counts.items())),
        "longest_text_blocks": longest,
    }


def _preview(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def format_report(report: Mapping[str, object]) -> str:
    text_length = report["text_length"]
    lines = [
        f"块总数: {report['block_count']}",
        f"各 block_type 数量: {json.dumps(report['block_type_counts'], ensure_ascii=False)}",
        f"文本长度: {json.dumps(text_length, ensure_ascii=False)}",
        f"标题层级: {json.dumps(report['title_level_counts'], ensure_ascii=False)}",
        "section_path 示例:",
        json.dumps(report["section_path_examples"], ensure_ascii=False, indent=2),
        "最长文本块:",
    ]
    for item in report["longest_text_blocks"]:  # type: ignore[union-attr]
        lines.append(
            f"- {item['id']} [{item['block_type']}] {item['length']} 字符: "
            f"{item['preview']}"
        )
    warning_counts = report.get("warning_counts")
    if warning_counts:
        lines.append(f"标准化 warning: {json.dumps(warning_counts, ensure_ascii=False)}")
    return "\n".join(lines)


def write_json(path: Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        Path(temporary_name).replace(path)
    except BaseException:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass
        raise
