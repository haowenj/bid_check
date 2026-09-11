from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol
from xml.etree import ElementTree
import httpx

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.llm_concurrency import llm_request_slot
from app.llm_protocol import build_thinking_params, normalize_llm_provider
from app.models import (
    FileMetadata,
    FileRequirement,
    ProjectRequirement,
    SupplementalMaterial,
    TenderExtractionResult,
    TenderTemplate,
)
from app.navigation_content import classify_navigation_item

logger = logging.getLogger(__name__)
REQUIREMENT_PROMPT_VERSION = "tender-compliance-objects-prompt-v5-single-file"
# Keep object-result and parsed-document caches independently versioned.  A
# change to the MinerU adapter must invalidate parsed blocks as well as the
# downstream deterministic result.
REQUIREMENT_CACHE_VERSION = f"tender-compliance-objects-v26:{REQUIREMENT_PROMPT_VERSION}"
PARSED_DOCUMENT_CACHE_VERSION = "mineru-parse-v6"
MINERU_TASKS_PROTOCOL_VERSION = "mineru-tasks-v1"
MINERU_TASKS_PROTOCOL_LABEL = "mineru_tasks"
DEFAULT_MINERU_BACKEND = "hybrid-engine"
SUPPORTED_MINERU_BACKENDS = {"hybrid-engine", "hybrid-http-client"}
DEFAULT_MINERU_TIMEOUT_SECONDS = 1800.0
DEFAULT_MINERU_POLL_INTERVAL_SECONDS = 2.0


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _serialize_blocks(blocks: Sequence[StructuredBlock]) -> list[dict[str, Any]]:
    return [asdict(block) for block in blocks]


def _block_structure_stats(blocks: Sequence[StructuredBlock]) -> dict[str, Any]:
    type_counts = {kind: 0 for kind in ("heading", "paragraph", "table", "image")}
    heading_levels: set[int] = set()
    metadata_keys: set[str] = set()
    blocks_with_metadata = 0
    for block in blocks:
        type_counts[block.type] = type_counts.get(block.type, 0) + 1
        if block.metadata:
            blocks_with_metadata += 1
            metadata_keys.update(str(key) for key in block.metadata)
        if block.type == "heading":
            level = _block_heading_level(block)
            if level is not None:
                heading_levels.add(level)
    return {
        "parsed_block_counts": type_counts,
        "parsed_heading_levels": sorted(heading_levels),
        "parsed_blocks_with_metadata": blocks_with_metadata,
        "parsed_metadata_keys": sorted(metadata_keys),
    }


def _serialize_candidates(
    candidates: Sequence[CandidateWindow],
) -> list[dict[str, Any]]:
    return [asdict(candidate) for candidate in candidates]


@dataclass(frozen=True)
class StructuredBlock:
    block_id: str
    type: Literal["heading", "paragraph", "table", "image"]
    text: str
    section: str
    order: int
    metadata: dict[str, Any] = field(default_factory=dict)
    heading_level: int | None = None


@dataclass(frozen=True)
class CandidateWindow:
    block_ids: list[str]
    section: str
    text: str
    order: int
    kind: Literal[
        "templates",
        "project_requirements",
        "supplemental_materials",
        "file_requirements",
    ] = "templates"


FunctionalRegionKind = Literal[
    "templates", "project_requirements", "supplemental_materials"
]


@dataclass(frozen=True)
class FunctionalRegion:
    kind: FunctionalRegionKind
    title: str
    section: str
    block_ids: list[str]
    blocks: list[StructuredBlock]
    text: str
    order: int


class ComplianceExtractionError(RuntimeError):
    """Raised when a tender cannot produce source-grounded requirements."""


class DocumentParser(Protocol):
    def parse(self, path: Path) -> list[StructuredBlock]: ...


class RequirementLLM(Protocol):
    def extract(self, batch: Sequence[CandidateWindow]) -> dict[str, list[dict[str, Any]]]: ...


class RequirementCache(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any) -> None: ...


class InMemoryRequirementCache:
    def __init__(self):
        self._values: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        logger.info("cache.read.start backend=in_memory key=%s", key[:12])
        value = self._values.get(key)
        logger.info(
            "cache.read.end backend=in_memory key=%s status=%s",
            key[:12],
            "hit" if value is not None else "miss",
        )
        return deepcopy(value) if value is not None else None

    def set(self, key: str, value: Any) -> None:
        logger.info(
            "cache.write.start backend=in_memory key=%s objects=%d",
            key[:12],
            len(value),
        )
        self._values[key] = deepcopy(value)
        logger.info(
            "cache.write.end backend=in_memory key=%s objects=%d",
            key[:12],
            len(value),
        )


class JsonRequirementCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> Any | None:
        started_at = time.perf_counter()
        logger.info("cache.read.start backend=json key=%s", key[:12])
        try:
            payload = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            logger.info(
                "cache.read.end backend=json key=%s status=miss elapsed_ms=%d",
                key[:12],
                _elapsed_ms(started_at),
            )
            return None
        value = deepcopy(payload) if isinstance(payload, (list, dict)) else None
        logger.info(
            "cache.read.end backend=json key=%s status=%s objects=%d elapsed_ms=%d",
            key[:12],
            "hit" if value is not None else "miss",
            len(value) if value is not None else 0,
            _elapsed_ms(started_at),
        )
        return value

    def set(self, key: str, value: Any) -> None:
        started_at = time.perf_counter()
        logger.info(
            "cache.write.start backend=json key=%s objects=%d",
            key[:12],
            len(value),
        )
        target = self._path(key)
        temporary = target.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, target)
        logger.info(
            "cache.write.end backend=json key=%s objects=%d elapsed_ms=%d",
            key[:12],
            len(value),
            _elapsed_ms(started_at),
        )


class JsonDocumentCache(JsonRequirementCache):
    """JSON cache for parsed MinerU/DOCX structural blocks.

    It intentionally uses the same small atomic JSON backend as requirement
    results, while having an independent version/key namespace so a prompt or
    requirement schema change never invalidates parsed document data.
    """


def _deserialize_blocks(value: Any) -> list[StructuredBlock] | None:
    if not isinstance(value, list):
        return None
    blocks: list[StructuredBlock] = []
    try:
        for raw in value:
            if not isinstance(raw, dict):
                return None
            blocks.append(
                StructuredBlock(
                    block_id=str(raw["block_id"]),
                    type=raw["type"],
                    text=str(raw["text"]),
                    section=str(raw.get("section", "")),
                    order=int(raw["order"]),
                    metadata=dict(raw.get("metadata", {})),
                    heading_level=(
                        int(raw["heading_level"])
                        if raw.get("heading_level") is not None
                        else None
                    ),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return blocks


def _mineru_inline_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_mineru_inline_text(item) for item in value)
    if isinstance(value, dict):
        return _mineru_inline_text(value.get("content", ""))
    return ""


def _mineru_has_style(value: Any, style_name: str) -> bool:
    target = style_name.casefold()
    if isinstance(value, dict):
        styles = value.get("style", [])
        if isinstance(styles, str):
            styles = [styles]
        if isinstance(styles, (list, tuple, set)) and any(
            str(style).casefold() == target for style in styles
        ):
            return True
        return any(_mineru_has_style(item, style_name) for item in value.values())
    if isinstance(value, list):
        return any(_mineru_has_style(item, style_name) for item in value)
    return False


def _flatten_mineru_content_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize MinerU v1 and nested v2 content items to one ordered stream."""
    if isinstance(payload, dict):
        payload = payload.get(
            "blocks",
            payload.get(
                "content",
                payload.get("items", payload.get("content_list")),
            ),
        )
    if not isinstance(payload, list):
        raise ComplianceExtractionError("MinerU 返回结果不是结构化内容列表。")

    # content_list_v2 wraps the document stream in a small top-level envelope.
    if (
        len(payload) == 2
        and isinstance(payload[1], list)
        and all(isinstance(item, dict) for item in payload[1])
    ):
        payload = payload[1]

    flattened: list[dict[str, Any]] = []
    for source_index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            continue
        raw_type = str(
            raw.get("type", raw.get("block_type", "paragraph"))
        ).lower()
        content = raw.get("content")
        if raw_type == "list" and isinstance(content, dict):
            for item in content.get("list_items", []):
                if not isinstance(item, dict):
                    continue
                text = _mineru_inline_text(item.get("item_content", []))
                prefix = str(item.get("prefix", "")).strip()
                text = f"{prefix} {text}".strip()
                if not text:
                    continue
                flattened.append(
                    {
                        **{
                            key: value
                            for key, value in raw.items()
                            if key != "content"
                        },
                        "type": "text",
                        "text": text,
                        "mineru_parent_source_index": source_index,
                        "mineru_parent_type": raw_type,
                        "mineru_nested_content": item,
                    }
                )
            continue
        if isinstance(content, (dict, list)):
            normalized = {
                key: value for key, value in raw.items() if key != "content"
            }
            normalized["mineru_nested_content"] = content
            normalized["mineru_parent_source_index"] = source_index
            normalized["mineru_parent_type"] = raw_type
            if raw_type == "title":
                normalized["type"] = "title"
                normalized["text"] = _mineru_inline_text(
                    content.get("title_content", [])
                    if isinstance(content, dict)
                    else content
                ).strip()
                normalized["text_level"] = (
                    content.get(
                        "level", raw.get("text_level", raw.get("heading_level"))
                    )
                    if isinstance(content, dict)
                    else raw.get("text_level", raw.get("heading_level"))
                )
            elif raw_type == "paragraph":
                normalized["type"] = "paragraph"
                normalized["text"] = _mineru_inline_text(
                    content.get("paragraph_content", [])
                    if isinstance(content, dict)
                    else content
                ).strip()
            elif raw_type == "table":
                normalized["type"] = "table"
                if isinstance(content, dict):
                    normalized["table_body"] = content.get(
                        "html", content.get("table_body", "")
                    )
                    normalized.update(
                        {
                            key: value
                            for key, value in content.items()
                            if key not in {"html", "table_body"}
                        }
                    )
            elif raw_type in {"image", "figure"}:
                normalized["type"] = raw_type
                normalized["text"] = _mineru_inline_text(
                    content.get("image_caption", content.get("caption", ""))
                ).strip()
                if content.get("img_path"):
                    normalized["img_path"] = content["img_path"]
            else:
                normalized["text"] = _mineru_inline_text(content).strip()
            flattened.append(normalized)
            continue
        flattened.append(dict(raw))
    return flattened


def _blocks_from_mineru_payload(payload: Any) -> list[StructuredBlock]:
    """Convert MinerU content-list objects without compiling them into rules."""
    payload = _flatten_mineru_content_list(payload)

    blocks: list[StructuredBlock] = []
    section = ""
    for index, raw in enumerate(payload, start=1):
        if not isinstance(raw, dict):
            continue
        raw_type = str(raw.get("type", raw.get("block_type", "paragraph"))).lower()
        level = raw.get("text_level", raw.get("heading_level", raw.get("level", 0)))
        try:
            has_heading_level = int(level or 0) > 0
        except (TypeError, ValueError):
            has_heading_level = False
        if raw_type in {"title", "heading", "header"} or (
            raw_type in {"text", "paragraph"} and has_heading_level
        ):
            kind = "heading"
        elif raw_type == "table":
            kind = "table"
        elif raw_type in {"image", "figure"}:
            kind = "image"
        else:
            kind = "paragraph"

        text = ""
        text_keys = (
            ("table_body", "text", "content", "html", "caption", "alt", "img_path")
            if kind == "table"
            else ("text", "content", "table_body", "html", "caption", "alt", "img_path")
        )
        for text_key in text_keys:
            value = raw.get(text_key)
            if not isinstance(value, str):
                continue
            if value.strip():
                text = value.strip()
                break
            if not text and _mineru_has_style(raw, "underline"):
                # An underline-only run is an input marker even though its
                # visible text consists only of whitespace.  Preserve that
                # source block so field extraction can associate it with the
                # preceding label.
                text = value
        if not text:
            # Keep an image as a real source block even when MinerU has no
            # caption; the raw image reference remains in metadata.
            if kind == "image":
                text = "[MinerU image]"
            elif kind == "paragraph" and _mineru_has_style(raw, "underline"):
                # Nested paragraph content may have been stripped while
                # flattening, but its underline style remains in metadata.
                text = " "
            else:
                continue

        if kind == "heading":
            section = text
        elif kind not in {"paragraph", "table", "image"}:
            kind = "paragraph"
        block_id = str(raw.get("block_id", raw.get("id", f"b{index:04d}")))
        try:
            order = int(raw.get("order", index))
        except (TypeError, ValueError):
            order = index
        metadata = {
            "mineru_raw_type": raw_type,
            "mineru_source_index": index - 1,
            **{
                key: value
                for key, value in raw.items()
                if key
                not in {
                    "block_id",
                    "id",
                    "type",
                    "block_type",
                    "text",
                    "content",
                    "section",
                    "order",
                }
            },
        }
        blocks.append(
            StructuredBlock(
                block_id=block_id,
                type=kind,  # type: ignore[arg-type]
                text=text,
                section=str(raw.get("section", section)),
                order=order,
                metadata=metadata,
                heading_level=int(level) if has_heading_level else None,
            )
        )
    return blocks


_DOCX_WML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_WML = f"{{{_DOCX_WML_NS}}}"


def _docx_element_text(element: ElementTree.Element) -> str:
    return "".join(
        node.text or ""
        for node in element.iter(f"{_DOCX_WML}t")
    ).strip()


def _docx_table_rows(table: ElementTree.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall(f"{_DOCX_WML}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{_DOCX_WML}tc"):
            paragraphs = [
                _docx_element_text(paragraph)
                for paragraph in cell.findall(f"{_DOCX_WML}p")
            ]
            value = " ".join(text for text in paragraphs if text)
            if not paragraphs:
                value = _docx_element_text(cell)
            cells.append(re.sub(r"\s+", " ", value).strip())
        if any(cells):
            rows.append(cells)
    return rows


def _docx_table_html(rows: Sequence[Sequence[str]]) -> str:
    return "<table><tbody>" + "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(cell).replace(chr(10), '<br/>')}</td>"
            for cell in row
        )
        + "</tr>"
        for row in rows
    ) + "</tbody></table>"


def _docx_front_table(path: Path) -> tuple[str, int] | None:
    """Recover the semantic front-table block omitted by MinerU Office output.

    The recovery is deliberately limited to a DOCX table whose own header is
    ``条款号 / 条款名称 / 编列内容`` and which follows the actual front-table
    heading.  It is not a general DOCX parser or a second document parser.
    """

    if path.suffix.lower() != ".docx":
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return None

    body = root.find(f"{_DOCX_WML}body")
    if body is None:
        return None
    children = list(body)
    for index, child in enumerate(children):
        if child.tag != f"{_DOCX_WML}p":
            continue
        if re.sub(r"\s+", "", _docx_element_text(child)) != "投标人须知前附表":
            continue
        for candidate in children[index + 1 : index + 9]:
            if candidate.tag != f"{_DOCX_WML}tbl":
                continue
            rows = _docx_table_rows(candidate)
            if not rows:
                continue
            header = "|".join(rows[0])
            if not all(label in header for label in ("条款号", "条款名称", "编列内容")):
                continue
            return _docx_table_html(rows), index
    return None


def _recover_project_front_table(
    path: Path,
    blocks: Sequence[StructuredBlock],
) -> tuple[list[StructuredBlock], bool]:
    """Add one source-grounded front-table block when MinerU omitted it."""

    front_index = next(
        (
            index
            for index, block in enumerate(blocks)
            if block.type == "heading"
            and _normalize_region_title(block.text) == "投标人须知前附表"
        ),
        None,
    )
    if front_index is None:
        return list(blocks), False

    front_level = _block_heading_level(blocks[front_index])
    boundary = len(blocks)
    for index in range(front_index + 1, len(blocks)):
        block_level = _block_heading_level(blocks[index])
        if (
            blocks[index].type == "heading"
            and front_level is not None
            and block_level is not None
            and block_level <= front_level
        ):
            boundary = index
            break
    if any(block.type == "table" for block in blocks[front_index + 1 : boundary]):
        return list(blocks), False

    recovered = _docx_front_table(path)
    if recovered is None:
        return list(blocks), False
    table_body, docx_body_index = recovered
    anchor = blocks[front_index]
    table = StructuredBlock(
        block_id=f"{anchor.block_id}_docx_front_table",
        type="table",
        text=table_body,
        section=anchor.section or anchor.text,
        order=blocks[boundary - 1].order if boundary > front_index + 1 else anchor.order,
        metadata={
            "source_recovery": "docx_front_table",
            "docx_body_index": docx_body_index,
            "table_body": table_body,
        },
    )
    result = list(blocks)
    result.insert(boundary, table)
    return result, True


def _mineru_raw_value(raw: dict[str, Any]) -> str:
    for key in ("text", "table_body", "html", "caption", "alt", "img_path"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _mineru_payload_diagnostics(payload: Sequence[dict[str, Any]]) -> dict[str, Any]:
    type_counts: Counter[str] = Counter()
    table_previews: list[str] = []
    front_table_context: list[dict[str, Any]] = []
    front_table_title_indexes: list[int] = []
    for index, raw in enumerate(payload):
        raw_type = str(raw.get("type", raw.get("block_type", "paragraph"))).lower()
        type_counts[raw_type] += 1
        raw_value = _mineru_raw_value(raw)
        is_table = raw_type == "table" or isinstance(raw.get("table_body"), str)
        if is_table and raw_value and len(table_previews) < 8:
            table_previews.append(raw_value[:240])
        normalized_value = _normalize_region_title(raw_value)
        if normalized_value == "投标人须知前附表" and (
            raw_type in {"title", "heading"}
            or raw.get("text_level")
            or raw.get("heading_level")
            or raw.get("level")
        ):
            front_table_title_indexes.append(index)

    def raw_level(raw: dict[str, Any]) -> int | None:
        for key in ("text_level", "heading_level", "level"):
            try:
                value = int(raw.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    for title_index in front_table_title_indexes:
        context_indexes = list(range(max(0, title_index - 2), title_index + 1))
        title_level = raw_level(payload[title_index])
        for index in range(title_index + 1, min(len(payload), title_index + 16)):
            raw = payload[index]
            raw_type = str(raw.get("type", raw.get("block_type", "paragraph"))).lower()
            level = raw_level(raw)
            if (
                index > title_index
                and raw_type in {"title", "heading", "header"}
                and title_level is not None
                and level is not None
                and level <= title_level
            ):
                break
            context_indexes.append(index)
        for index in context_indexes:
            raw = payload[index]
            raw_type = str(raw.get("type", raw.get("block_type", "paragraph"))).lower()
            raw_value = _mineru_raw_value(raw)
            front_table_context.append(
                {
                    "source_index": index,
                    "type": raw_type,
                    "text_preview": raw_value[:240],
                    "is_table": raw_type == "table"
                    or isinstance(raw.get("table_body"), str),
                }
            )

    return {
        "mineru_raw_item_count": len(payload),
        "mineru_raw_type_counts": dict(sorted(type_counts.items())),
        "mineru_raw_table_count": sum(
            1
            for raw in payload
            if str(raw.get("type", raw.get("block_type", "paragraph"))).lower()
            == "table"
            or isinstance(raw.get("table_body"), str)
        ),
        "mineru_raw_table_previews": table_previews,
        "mineru_front_table_raw_context": front_table_context,
        "mineru_front_table_present": any(
            item["is_table"] for item in front_table_context
        ),
    }


class MinerUDocumentParser:
    """Use the project's MinerU ``/tasks`` service for every document parse."""

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
    ):
        self.mineru_url = mineru_url
        self.mineru_api_key = mineru_api_key
        self.mineru_backend = mineru_backend or DEFAULT_MINERU_BACKEND
        self.mineru_server_url = mineru_server_url
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._http_client = http_client
        self.parse_diagnostics: dict[str, Any] = {
            "parser": self.parser_name,
            "mineru_called": False,
            "service_protocol": None,
            "elapsed_ms": None,
            "project_front_table_recovered": False,
        }

    @property
    def parser_name(self) -> str:
        return "mineru"

    @property
    def cache_descriptor(self) -> dict[str, Any]:
        transport = "mineru_tasks"
        return {
            "parser": self.parser_name,
            "transport": transport,
            "protocol": MINERU_TASKS_PROTOCOL_VERSION,
            "url": (self.mineru_url or "").strip(),
            "backend": self.mineru_backend,
            "server_url": (self.mineru_server_url or "").strip(),
        }

    def parse(self, path: Path) -> list[StructuredBlock]:
        started_at = time.perf_counter()
        logger.info(
            "document.parse.dispatch.start parser=%s file=%s",
            self.parser_name,
            path.name,
        )
        try:
            if (self.mineru_url or "").strip():
                blocks = self._parse_with_mineru_service(path)
            else:
                raise ComplianceExtractionError(
                    "MinerU 服务未配置，请配置 MINERU_URL。"
                )
        except ComplianceExtractionError:
            self.parse_diagnostics = {
                "parser": self.parser_name,
                "mineru_called": False,
                "service_protocol": MINERU_TASKS_PROTOCOL_LABEL
                if (self.mineru_url or "").strip()
                else None,
                "elapsed_ms": _elapsed_ms(started_at),
            }
            raise
        except Exception as exc:
            self.parse_diagnostics = {
                "parser": self.parser_name,
                "mineru_called": bool((self.mineru_url or "").strip()),
                "service_protocol": MINERU_TASKS_PROTOCOL_LABEL
                if (self.mineru_url or "").strip()
                else None,
                "elapsed_ms": _elapsed_ms(started_at),
            }
            raise ComplianceExtractionError(
                f"MinerU 文档解析失败：{type(exc).__name__}。"
            ) from exc

        self.parse_diagnostics["elapsed_ms"] = _elapsed_ms(started_at)
        logger.info(
            "document.parse.dispatch.end parser=%s file=%s blocks=%d elapsed_ms=%d",
            self.parser_name,
            path.name,
            len(blocks),
            self.parse_diagnostics["elapsed_ms"],
        )
        return blocks

    def _parse_with_mineru_service(self, path: Path) -> list[StructuredBlock]:
        if self.mineru_backend not in SUPPORTED_MINERU_BACKENDS:
            raise ComplianceExtractionError(
                "MinerU backend 仅支持 hybrid-engine 或 hybrid-http-client。"
            )
        if self.mineru_backend == "hybrid-http-client" and not (
            self.mineru_server_url or ""
        ).strip():
            raise ComplianceExtractionError(
                "hybrid-http-client 模式下必须配置 MinerU server_url。"
            )
        if self.timeout_seconds <= 0 or self.poll_interval_seconds < 0:
            raise ComplianceExtractionError("MinerU 超时或轮询间隔配置无效。")

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
                raise ComplianceExtractionError(
                    f"MinerU 任务提交失败：{type(exc).__name__}。"
                ) from exc
            if response.status_code != 202:
                raise ComplianceExtractionError(
                    f"MinerU 任务提交失败：HTTP {response.status_code}。"
                )
            submission = self._json_object(response, "任务提交")
            task_id = submission.get("task_id")
            status_url = self._trusted_task_url(submission.get("status_url"), base_url)
            result_url = self._trusted_task_url(submission.get("result_url"), base_url)
            if not isinstance(task_id, str) or not task_id:
                raise ComplianceExtractionError("MinerU 返回了无效任务响应。")

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                try:
                    status_response = client.get(
                        status_url,
                        headers=headers,
                        follow_redirects=False,
                    )
                except httpx.HTTPError as exc:
                    raise ComplianceExtractionError(
                        f"MinerU 任务状态查询失败：{type(exc).__name__}。"
                    ) from exc
                if status_response.status_code != 200:
                    raise ComplianceExtractionError(
                        f"MinerU 任务状态查询失败：HTTP {status_response.status_code}。"
                    )
                status_payload = self._json_object(status_response, "任务状态")
                status = status_payload.get("status")
                if status == "completed":
                    break
                if status == "failed":
                    raise ComplianceExtractionError("MinerU 任务处理失败。")
                if status not in {"pending", "processing"}:
                    raise ComplianceExtractionError(
                        f"MinerU 返回未知任务状态：{status!r}。"
                    )
                if self.poll_interval_seconds:
                    time.sleep(self.poll_interval_seconds)
            else:
                raise ComplianceExtractionError("MinerU 任务等待超时。")

            try:
                result_response = client.get(
                    result_url,
                    headers=headers,
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                raise ComplianceExtractionError(
                    f"MinerU 结果下载失败：{type(exc).__name__}。"
                ) from exc
            if result_response.status_code != 200:
                raise ComplianceExtractionError(
                    f"MinerU 结果下载失败：HTTP {result_response.status_code}。"
                )
            content_list = self._content_list_from_zip(result_response.content)
            blocks = _blocks_from_mineru_payload(content_list)
            blocks, project_front_table_recovered = _recover_project_front_table(
                path, blocks
            )
            self.parse_diagnostics.update(
                {
                    "parser": "mineru",
                    "mineru_called": True,
                    "service_protocol": MINERU_TASKS_PROTOCOL_LABEL,
                    "task_id": task_id,
                    "project_front_table_recovered": project_front_table_recovered,
                    **_mineru_payload_diagnostics(content_list),
                }
            )
            return blocks
        finally:
            if owns_client:
                client.close()

    @staticmethod
    def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ComplianceExtractionError(f"MinerU {label}响应不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise ComplianceExtractionError(f"MinerU {label}响应必须是 JSON 对象。")
        return payload

    @staticmethod
    def _trusted_task_url(value: Any, base_url: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ComplianceExtractionError("MinerU 返回了无效任务 URL。")
        base = urllib.parse.urlsplit(f"{base_url}/")
        resolved = urllib.parse.urljoin(f"{base_url}/", value)
        parsed = urllib.parse.urlsplit(resolved)
        if (
            parsed.scheme,
            parsed.hostname,
            parsed.port,
        ) != (base.scheme, base.hostname, base.port) or parsed.fragment:
            raise ComplianceExtractionError("MinerU 返回了不可信任务 URL。")
        return resolved

    @staticmethod
    def _content_list_from_zip(raw: bytes) -> list[dict[str, Any]]:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = []
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    normalized = info.filename.replace("\\", "/")
                    safe_path = PurePosixPath(normalized)
                    if safe_path.is_absolute() or ".." in safe_path.parts:
                        raise ComplianceExtractionError(
                            "MinerU 结果 ZIP 包含不安全路径。"
                        )
                    if normalized.lower().endswith(
                        ("_content_list.json", "_content_list_v2.json", "content_list.json")
                    ):
                        members.append(info)
                if not members:
                    raise ComplianceExtractionError(
                        "MinerU 结果 ZIP 未返回 content list。"
                    )
                v2_members = [
                    info
                    for info in members
                    if info.filename.replace("\\", "/").lower().endswith(
                        "_content_list_v2.json"
                    )
                ]
                if len(v2_members) > 1:
                    raise ComplianceExtractionError(
                        "MinerU 结果 ZIP 返回了多个 content_list_v2。"
                    )
                selected = v2_members[0] if v2_members else members[0]
                payload = json.loads(archive.read(selected))
        except ComplianceExtractionError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ComplianceExtractionError("无法读取 MinerU 结果 ZIP。") from exc
        return _flatten_mineru_content_list(payload)


_FUNCTIONAL_REGION_PATTERNS: tuple[tuple[FunctionalRegionKind, re.Pattern[str]], ...] = (
    (
        "templates",
        re.compile(
            r"投标文件(?:格式|组成|模板)|响应文件(?:格式|组成|模板)|"
            r"资格审查文件格式|商务投标文件格式|技术投标文件格式|报价文件格式"
        ),
    ),
    (
        "project_requirements",
        re.compile(
            r"投标人须知前附表|投标须知前附表|项目专用条款|项目专用表|"
            r"响应人须知前附表"
        ),
    ),
    (
        "supplemental_materials",
        re.compile(
            r"招标公告|资格条件|投标人资格要求|投标产品资格要求|制造商资格要求"
        ),
    ),
)
_EXCLUDED_REGION_TITLE_RE = re.compile(
    r"评标办法|评审办法|评分标准|评标委员会|招标代理|中标候选人"
)
_MAJOR_SECTION_TITLE_RE = re.compile(
    r"^第[一二三四五六七八九十百千万0-9]+[章节部分篇]\s*"
)
_FILE_REQUIREMENT_REGION_RE = re.compile(
    r"投标人须知(?:前附表)?|投标文件(?:组成|编制|制作|递交)|"
    r"电子投标文件|电子招标投标|上传|加密|解密|密封和标记|"
    r"特别说明|补充条款"
)
_FILE_REQUIREMENT_EXCLUDED_REGION_RE = re.compile(
    r"投标文件(?:格式|模板)|响应文件(?:格式|模板)|"
    r"商务投标文件格式|技术投标文件格式|报价文件格式|"
    r"合同(?:条款|协议)|技术(?:需求|规格)|工程量清单|"
    r"评标办法|评审办法|评分标准|评标委员会|招标代理|中标候选人"
)


def _is_file_requirement_excluded_title(title: str) -> bool:
    normalized = _normalize_region_title(title)
    if re.search(r"电子投标文件格式", normalized) and not re.search(
        r"模板|范本", normalized
    ):
        return False
    return bool(_FILE_REQUIREMENT_EXCLUDED_REGION_RE.search(normalized))


def _strip_markup(text: str) -> str:
    """Remove presentation markup while retaining the source wording."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("**", "")
    return unescape(text)


def _block_heading_level(block: StructuredBlock) -> int | None:
    if block.heading_level is not None and block.heading_level > 0:
        return block.heading_level
    for key in ("text_level", "heading_level", "level"):
        value = block.metadata.get(key)
        try:
            level = int(value)
        except (TypeError, ValueError):
            continue
        if level > 0:
            return level
    return None


def _has_structural_heading_levels(blocks: Sequence[StructuredBlock]) -> bool:
    return any(_block_heading_level(block) is not None for block in blocks)


def _normalize_region_title(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", _strip_markup(text)).strip()


def _functional_region_kind(title: str) -> FunctionalRegionKind | None:
    normalized = _normalize_region_title(title)
    if _EXCLUDED_REGION_TITLE_RE.search(normalized):
        return None
    for kind, pattern in _FUNCTIONAL_REGION_PATTERNS:
        if pattern.search(normalized):
            return kind
    return None


def _is_region_title_block(
    block: StructuredBlock,
    *,
    has_structural_heading_levels: bool = False,
) -> bool:
    if block.type == "heading":
        return True
    if block.text.strip() == block.section.strip():
        if has_structural_heading_levels and (
            _functional_region_kind(block.text) == "templates"
        ):
            return False
        return True
    if block.type != "paragraph":
        return False
    text = block.text.strip()
    # DOCX/MinerU exports can flatten a standalone functional title into a
    # paragraph.  Table-of-contents entries carry field codes and must not
    # start a real extraction region.
    if re.search(r"\b(?:TOC|PAGEREF|_Toc|HYPERLINK)\b", text, re.IGNORECASE):
        return False
    if re.search(r"[。；;，,：:]", text):
        return False
    # Once MinerU has supplied heading levels, a flattened paragraph that
    # merely repeats a chapter title is not allowed to start a template
    # region.  Project and qualification titles are still accepted here
    # because some MinerU exports keep those standalone labels as paragraphs.
    if has_structural_heading_levels and (
        _functional_region_kind(text) == "templates"
    ):
        return False
    return len(text) <= 80 and _functional_region_kind(text) is not None


def _heading_closes_region(
    block: StructuredBlock,
    current_blocks: Sequence[StructuredBlock],
) -> bool:
    if not current_blocks:
        return True
    current_level = _block_heading_level(current_blocks[0])
    block_level = _block_heading_level(block)
    if current_level is None or block_level is None:
        return True
    return block_level <= current_level


def identify_functional_regions(
    blocks: Iterable[StructuredBlock],
) -> list[FunctionalRegion]:
    """Identify narrow tender-function areas without relying on chapter numbers."""

    ordered_blocks = sorted(blocks, key=lambda item: item.order)
    has_structural_heading_levels = _has_structural_heading_levels(ordered_blocks)
    regions: list[FunctionalRegion] = []
    current_kind: FunctionalRegionKind | None = None
    current_title = ""
    current_section = ""
    current_blocks: list[StructuredBlock] = []

    def flush() -> None:
        nonlocal current_kind, current_title, current_section, current_blocks
        if current_kind is None or not current_blocks:
            current_kind = None
            current_title = ""
            current_section = ""
            current_blocks = []
            return
        # A chapter title followed only by a field-code TOC line is not an
        # extraction region.  Drop it here so callers do not mistake the TOC
        # itself for a real template area.
        if current_kind == "templates":
            region_text = "\n".join(block.text for block in current_blocks)
            if len(current_blocks) == 1 or (
                region_text.count("PAGEREF") >= 2
                or region_text.count("_Toc") >= 2
                or current_blocks[1:]
                and all(
                    re.search(r"\b(?:TOC|PAGEREF|_Toc|HYPERLINK)\b", block.text, re.IGNORECASE)
                    for block in current_blocks[1:]
                )
            ):
                current_kind = None
                current_title = ""
                current_section = ""
                current_blocks = []
                return
        regions.append(
            FunctionalRegion(
                kind=current_kind,
                title=current_title,
                section=current_section,
                block_ids=[block.block_id for block in current_blocks],
                blocks=list(current_blocks),
                text="\n".join(block.text for block in current_blocks),
                order=current_blocks[0].order,
            )
        )
        current_kind = None
        current_title = ""
        current_section = ""
        current_blocks = []

    for block in ordered_blocks:
        is_title_block = _is_region_title_block(
            block,
            has_structural_heading_levels=has_structural_heading_levels,
        )
        title_kind = (
            _functional_region_kind(block.text)
            if is_title_block
            else None
        )
        is_excluded_title = bool(
            is_title_block
            and _EXCLUDED_REGION_TITLE_RE.search(_normalize_region_title(block.text))
            and (
                current_kind is None
                or _heading_closes_region(block, current_blocks)
            )
        )
        is_major_boundary = bool(
            is_title_block
            and _MAJOR_SECTION_TITLE_RE.search(block.text.strip())
            and title_kind is None
            and not is_excluded_title
        )
        if (
            has_structural_heading_levels
            and is_title_block
            and block.type == "heading"
            and _block_heading_level(block) == 1
            and title_kind is None
            and not is_excluded_title
        ):
            is_major_boundary = True

        if title_kind is not None:
            flush()
            current_kind = title_kind
            current_title = block.text.strip()
            current_section = block.section or block.text.strip()
            current_blocks = [block]
            continue
        if is_excluded_title or is_major_boundary:
            flush()
            continue
        if (
            current_kind == "project_requirements"
            and any(item.type == "table" for item in current_blocks[1:])
            and block.type != "table"
            and title_kind is None
        ):
            flush()
            continue
        if current_kind is not None:
            current_blocks.append(block)

    flush()
    return regions


def _file_requirement_region_title(block: StructuredBlock) -> bool:
    """Return whether a structural title can introduce single-file context."""

    if block.type not in {"heading", "paragraph"}:
        return False
    title = _normalize_region_title(block.text)
    if not title or len(title) > 120:
        return False
    if re.search(r"\b(?:TOC|PAGEREF|_Toc|HYPERLINK)\b", title, re.IGNORECASE):
        return False
    if _is_file_requirement_excluded_title(title):
        return False
    if block.type == "paragraph" and re.search(r"[。；;，,：:]", title):
        return False
    return bool(_FILE_REQUIREMENT_REGION_RE.search(title))


def _file_requirement_boundary(
    block: StructuredBlock,
    current_blocks: Sequence[StructuredBlock],
) -> bool:
    """Close a candidate at an unrelated structural section boundary."""

    if block.type != "heading" or not current_blocks:
        return False
    current_level = _block_heading_level(current_blocks[0])
    block_level = _block_heading_level(block)
    if current_level is not None and block_level is not None:
        return block_level <= current_level
    return True


def build_file_requirement_candidates(
    blocks: Iterable[StructuredBlock],
) -> list[CandidateWindow]:
    """Build complete, low-noise contexts for single-upload-file requirements.

    This is deliberately a structural pass rather than a keyword snippet
    search.  A front-table block stays intact, while unrelated headings close
    the current context before their large bodies can leak into the prompt.
    """

    ordered_blocks = sorted(blocks, key=lambda item: item.order)
    candidates: list[CandidateWindow] = []
    current_blocks: list[StructuredBlock] = []

    def flush() -> None:
        nonlocal current_blocks
        if not current_blocks:
            return
        candidates.append(
            CandidateWindow(
                block_ids=[block.block_id for block in current_blocks],
                section=current_blocks[0].section or current_blocks[0].text.strip(),
                text="\n".join(block.text for block in current_blocks).strip(),
                order=current_blocks[0].order,
                kind="file_requirements",
            )
        )
        current_blocks = []

    for block in ordered_blocks:
        title_is_candidate = _file_requirement_region_title(block)
        is_excluded_title = bool(
            block.type in {"heading", "paragraph"}
            and _is_file_requirement_excluded_title(block.text)
            and (
                block.type == "heading"
                or block.text.strip() == block.section.strip()
                or not re.search(r"[。；;，,：:]", block.text)
            )
        )
        is_major_boundary = bool(
            block.type == "heading"
            and _MAJOR_SECTION_TITLE_RE.search(block.text.strip())
            and not title_is_candidate
            and not is_excluded_title
        )

        if title_is_candidate:
            if current_blocks and _file_requirement_boundary(block, current_blocks):
                flush()
            current_blocks.append(block)
            continue
        if is_excluded_title or is_major_boundary:
            flush()
            continue
        if current_blocks:
            if _file_requirement_boundary(block, current_blocks):
                flush()
            else:
                current_blocks.append(block)

    flush()
    return [candidate for candidate in candidates if candidate.text]


_TEMPLATE_ITEM_NAME_RE = re.compile(
    r"封面|投标函|响应函|法定代表人身份证明|身份证明|授权委托书|"
    r"廉洁承诺|关联关系|诉讼仲裁|基本账户|账户信息|业绩情况|业绩表|"
    r"知识产权|安全承诺|资格审查(?:文件|表)|报价表|情况表|声明|承诺函|"
    r"保证金|保函|缴纳|纸质|正本|副本|密封|包封|联合体|授权函|"
    r"特定关系|控股|管理关系|申报表|偏离表|开源软件|第三方软件|"
    r"元器件|来源清单|一览表|投标产品承诺|增值税专用发票"
)
_TEMPLATE_NAVIGATION_NAME_RE = re.compile(
    r"索引(?:表|目录)?|目录(?:导航|表)?|导航(?:目录|表)?"
)
_TEMPLATE_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:[一二三四五六七八九十百千万0-9]+[、.)．]|\([一二三四五六七八九十百千万0-9]+\))\s*"
)
_TEMPLATE_FORMAT_SUFFIX_RE = re.compile(r"\s*[（(](?:格式|范本|样式)[）)]\s*$")
_TEMPLATE_FIELD_RE = re.compile(
    r"(?<![\w])([^\s：:|,，。；;、.!！?？]{1,20})\s*[：:]"
)
_TABLE_FIELD_RE = re.compile(
    r"(?:^|\n|\|)\s*([^|\n：:]{1,20})\s*\|\s*(?=_{2,}|[…·.]{2,}|$)"
)
_FIELD_LABEL_SUFFIX_RE = re.compile(
    r"(?:名称|姓名|性别|年龄|职务|地址|电话|手机|邮箱|邮编|日期|时间|期限|"
    r"性质|单位|联系人|负责人|法人|代表人|账号|账户|开户行|编号|证号|代码|金额|"
    r"数量|比例|方式|类别|类型|内容|事项|专业|学历|等级|资质|网址|传真|"
    r"税号|注册资本|经营范围|全称|简称|签字|盖章|印章|证明文件|证明材料|"
    r"页码|型号|厂商|软件|银行|编码|机关)$"
)
_FIELD_NARRATIVE_RE = re.compile(
    r"^(?:我方|我司)(?:承诺|声明|保证|确认|同意|提供|将|在)"
    r"|^(?:如|若).{0,16}(?:中标|提供|填写|提交)"
    r"|^现(?:承诺|声明|说明|如下)"
    r"|^(?:附|即)$"
    r"|(?:承诺如下|包括但不限于|以下内容|含有以下)"
    r"|^(?:复制|查阅|传播)"
)
_FIELD_INSTRUCTION_LABEL_RE = re.compile(
    r"^(?:附|即|注|说明|提示|注意|注意事项|编制(?:说明|要求)?|"
    r"填写说明|填报说明|使用说明)$"
)
_FIELD_PLACEHOLDER_RE = re.compile(
    r"_{2,}|[…·.]{2,}|"
    r"(?:由|供)[^。；;，,]{0,8}(?:填写|录入|补充|提供|选择)"
)
_FIELD_DATE_SLOT_RE = re.compile(r"(?=.*年)(?=.*月)(?=.*日)(?!.*\d)")
_FIELD_PLAIN_X_PLACEHOLDER_RE = re.compile(
    r"[Xx]{2,}(?:\s*[、/.-]\s*[Xx]{2,})*"
)
_PLACEHOLDER_FIELD_LABEL_RE = re.compile(r"\[([^\[\]]{1,60})\]")
_EXTERNAL_PLACEHOLDER_LABEL_RE = re.compile(r"招标人|招标项目|标包")
_TABLE_INDEX_LABEL_RE = re.compile(r"^(?:序号|编号|行号)$")
_ATTACHMENT_RE = re.compile(
    r"附件(?!加|带|近)[：:\s]+(?P<attachment>.+?)(?=[。；;\n]|$)|"
    r"(?:须附|应附)(?!加|带|近)[：:\s]*(?P<direct_attachment>.+?)(?=[。；;\n]|$)|"
    r"(?<!附)附(?!加|带|近)[：:\s]+(?P<legacy_attachment>.+?)(?=[。；;\n]|$)"
)
_ATTACHMENT_SUBMISSION_RE = re.compile(
    r"附上|附带|附送|(?:应当|必须|须|需|应)附|提供|提交|递交|上传|出具|报送"
)
_ATTACHMENT_MATERIAL_RE = re.compile(
    r"证明|材料|文件|证书|证照|执照|证件|报告|合同|协议|授权|凭证|发票|订单|"
    r"清单|复印件|扫描件|影印件|截图|照片|回执|许可证|批件|申请表|登记表|表单|函"
)
_ATTACHMENT_GENERIC_MATERIAL_RE = re.compile(
    r"^(?:相关|相应|有关|上述|必要的)?(?:证明材料|证明文件|证明|材料|文件|资料|"
    r"扫描件|复印件|影印件|证件|文件原件|原件|数据文件)$|"
    r"^的?(?:其他|相关|有关)相关?文件$|"
    r"^[^。；;，,]{1,20}(?:需要的|所需的)(?:证明材料|证明文件|材料|文件|资料)$"
)
_ATTACHMENT_NOT_INDEPENDENT_RE = re.compile(
    r"投标文件|响应文件|报价文件|履约保证金|履约保函|履行合同|合同签订后|"
    r"终验|承诺如下|承诺内容|招标文件|"
    r"本(?:文件|承诺函|清单|表|函)|"
    r"虚假|伪造|变造|弄虚作假|骗取中标"
)
_ATTACHMENT_NEGATIVE_CONTEXT_RE = re.compile(
    r"无需|无须|不得|不应|不向|不提供|不提交|不递交|不上传|不出具"
)
_ATTACHMENT_PROCESS_CONTEXT_RE = re.compile(
    r"合同签订后|合同履行过程中|履约阶段|评标过程中|评审过程中|评标委员会要求"
)
_ATTACHMENT_MATERIAL_END_RE = re.compile(
    r"(?:证明|材料|文件|证书|证照|执照|证件|报告|合同|协议|授权|凭证|发票|订单|"
    r"清单|复印件|扫描件|影印件|截图|照片|回执|许可证|批件|申请表|登记表|表单|函)"
    r"(?:\s*(?:原件|正本|副本|电子版|纸质版|各?[0-9一二三四五六七八九十百]+份|"
    r"[0-9一二三四五六七八九十百]+套|[0-9一二三四五六七八九十百]+页|"
    r"[（(][^（）()]*[）)]))*\s*$")
_ATTACHMENT_CONDITION_RE = re.compile(
    r"(?:^|[，,；;。．.：:（(\[【])\s*(?:如|若|如果|当|仅在|仅限于?|非)"
    r"[^。；;\n]{0,60}(?:时|情况下|情形|条件|才|则|，|,)|"
    r"(?:^|[，,；;。．.：:（(\[【])\s*(?:联合体|代理商|经销商|制造商|分支机构)"
    r"[^。；;\n]{0,40}(?:投标|时|情况下|情形|分别|各成员)|"
    r"(?:^|[，,；;。．.：:（(\[【])[^。；;\n]{0,20}"
    r"(?:联合体|代理商|经销商|制造商|分支机构)"
    r"[^。；;\n]{0,80}(?:投标的?|投标时|使用代理商投标|需要(?:单独|分别)?提供)"
)


def _template_name(value: str) -> str:
    name = re.sub(r"^\s*\d+(?:\.\d+)+\s*", "", _strip_markup(value).strip())
    name = _TEMPLATE_NUMBER_PREFIX_RE.sub("", name)
    name = _TEMPLATE_FORMAT_SUFFIX_RE.sub("", name).strip()
    name = re.sub(r"[★☆*]\s*", "", name, count=1)
    name = re.sub(r"\s*[（(]如有[）)]\s*$", "", name).strip()
    return name.strip(" ：:。；;") or "投标文件模板"


def _is_template_navigation_title(value: str) -> bool:
    return bool(_TEMPLATE_NAVIGATION_NAME_RE.search(_template_name(value)))


def _is_template_navigation_region(region: FunctionalRegion) -> bool:
    if _is_template_navigation_title(region.title):
        return True
    return any(
        block.type in {"heading", "paragraph"}
        and len(block.text.strip()) <= 80
        and _is_template_navigation_title(block.text)
        for block in region.blocks[1:]
    )


def _is_template_index_region(region: FunctionalRegion) -> bool:
    text = region.text
    return text.count("PAGEREF") >= 2 or text.count("_Toc") >= 2


def _template_index_block_ids(region: FunctionalRegion) -> set[str]:
    """Find a plain-text table-of-contents prefix in a flattened chapter."""

    def key(value: str) -> str:
        return re.sub(r"\s+", "", value).strip()

    directory_index = next(
        (
            index
            for index, block in enumerate(region.blocks)
            if key(block.text) in {"目录", "目錄"}
        ),
        None,
    )
    if directory_index is None:
        return set()
    for index in range(directory_index + 1, len(region.blocks)):
        candidate = key(region.blocks[index].text)
        if not re.match(r"^[一二三四五六七八九十]+、", candidate):
            continue
        duplicate_index = next(
            (
                later_index
                for later_index in range(index + 1, len(region.blocks))
                if key(region.blocks[later_index].text) == candidate
            ),
            None,
        )
        if duplicate_index is not None:
            return {
                block.block_id
                for block in region.blocks[directory_index:duplicate_index]
            }
    return set()


_TEMPLATE_MATERIAL_ONLY_RE = re.compile(
    r"营业执照|事业单位法人证书|合同关键页|身份证明复印件|扫描件"
)


def _is_template_item_title(block: StructuredBlock, region: FunctionalRegion) -> bool:
    if block.block_id == region.block_ids[0] or block.text.strip() == region.title.strip():
        return False
    name = _template_name(block.text)
    if _is_template_navigation_title(name):
        return False
    if _has_structural_heading_levels(region.blocks):
        root_level = _block_heading_level(region.blocks[0])
        level = _block_heading_level(block)
        # A structured template chapter may contain level-2 grouping headings
        # (目录、商务部分等).  Its actual form entries are the next structural
        # level, so use hierarchy rather than the form-name keyword list.
        if (
            block.type != "heading"
            or root_level is None
            or level is None
            or level < root_level + 2
        ):
            return False
        return True
    if block.type == "paragraph":
        text = block.text.strip()
        if len(text) > 80 or re.search(r"[。；;，,：:]", text):
            return False
    elif block.type != "heading":
        return False
    if re.match(r"(?:中国电信在职员工|中国电信员工的近亲属)", name):
        return False
    if _functional_region_kind(name) is not None:
        return False
    return bool(_TEMPLATE_ITEM_NAME_RE.search(name))


def _normalize_field_label(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n：:|")


def _is_field_label_candidate(
    value: str,
    *,
    require_field_suffix: bool = False,
    reject_narrative: bool = True,
) -> bool:
    label = _normalize_field_label(value)
    compact = re.sub(r"\s+", "", label)
    if not compact or len(compact) > 20:
        return False
    if re.fullmatch(r"[0-9０-９]+(?:[.．、)）])?", compact):
        return False
    if re.fullmatch(r"[…·.]+", compact):
        return False
    if re.search(r"[\[\]【】]", label):
        return False
    if re.search(r"[，,。；;、.!！?？\n|]", label):
        return False
    if reject_narrative and _FIELD_NARRATIVE_RE.search(compact):
        return False
    if _FIELD_INSTRUCTION_LABEL_RE.fullmatch(compact):
        return False
    return not require_field_suffix or bool(_FIELD_LABEL_SUFFIX_RE.search(compact))


def _bracket_group_end(text: str) -> int | None:
    if not text or text[0] not in _PARENTHETICAL_PAIRS:
        return None
    closing_to_opening = {
        closing: opening
        for opening, closing in _PARENTHETICAL_PAIRS.items()
    }
    stack: list[str] = []
    for index, character in enumerate(text):
        if character in _PARENTHETICAL_PAIRS:
            stack.append(character)
            continue
        opening = closing_to_opening.get(character)
        if opening is None or not stack or stack[-1] != opening:
            continue
        stack.pop()
        if not stack:
            return index
    return None


def _is_standalone_bracket_placeholder(value: str) -> bool:
    text = value.strip().strip("：:，,；;。")
    found_group = False
    while text:
        text = text.lstrip()
        end = _bracket_group_end(text)
        if end is None:
            return False
        inner = text[1:end]
        if (
            re.search(r"[，,。；;]", inner)
            and not re.search(
                r"请填写|请录入|请补充|请提供|请选择|待填|待定|\[[^\]]+\]",
                inner,
            )
        ):
            return False
        found_group = True
        text = text[end + 1 :].strip(" \t：:，,；;。")
    return found_group


def _is_text_placeholder(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return bool(
        _FIELD_PLACEHOLDER_RE.search(value)
        or _FIELD_DATE_SLOT_RE.search(value)
        or _FIELD_PLAIN_X_PLACEHOLDER_RE.fullmatch(text)
        or _is_standalone_bracket_placeholder(text)
    )


def _has_filling_position(
    value: str,
    styles: frozenset[str] = frozenset(),
) -> bool:
    """Return structural evidence that a value area is reserved for input.

    The marker's spelling is deliberately not interpreted as a field name.  It
    only supports the surrounding label or table structure as evidence that an
    input position exists.
    """

    return "underline" in styles or _is_text_placeholder(value)


def _is_table_placeholder(value: str) -> bool:
    # An empty table cell is evidence only after a row label or matrix header
    # has supplied the field semantics.  The caller must never append a field
    # from this predicate alone.
    return not value.strip() or _has_filling_position(value)


def _field_value_segment(value: str) -> str:
    closing_to_opening = {
        closing: opening
        for opening, closing in _PARENTHETICAL_PAIRS.items()
    }
    stack: list[str] = []
    for index, character in enumerate(value):
        if character in _PARENTHETICAL_PAIRS:
            stack.append(character)
            continue
        opening = closing_to_opening.get(character)
        if opening is not None and stack and stack[-1] == opening:
            stack.pop()
            continue
        if not stack and character in "\n。；;，,":
            return value[:index]
    return value


def _trailing_field_label(
    value: str,
    *,
    reject_narrative: bool = True,
    require_field_suffix: bool = False,
) -> str | None:
    text = value.rstrip()
    matches = list(_TEMPLATE_FIELD_RE.finditer(text))
    if not matches or matches[-1].end() != len(text):
        return None
    raw_label = matches[-1].group(1)
    closing_positions = [
        raw_label.rfind(closing) for closing in _PARENTHETICAL_PAIRS.values()
    ]
    last_closing = max(closing_positions, default=-1)
    if last_closing >= 0 and last_closing < len(raw_label) - 1:
        raw_label = raw_label[last_closing + 1 :]
    label = _normalize_field_label(raw_label)
    compact = re.sub(r"\s+", "", label)
    if require_field_suffix and len(compact) > 16:
        return None
    return (
        label
        if _is_field_label_candidate(
            label,
            require_field_suffix=require_field_suffix,
            reject_narrative=reject_narrative,
        )
        else None
    )


def _placeholder_field_labels(text: str) -> list[str]:
    labels: list[str] = []
    for match in _PLACEHOLDER_FIELD_LABEL_RE.finditer(text):
        label = _normalize_field_label(match.group(1))
        compact = re.sub(r"\s+", "", label)
        if _EXTERNAL_PLACEHOLDER_LABEL_RE.search(compact):
            continue
        if not _is_field_label_candidate(
            label,
            require_field_suffix=True,
            reject_narrative=True,
        ):
            continue
        if label not in labels:
            labels.append(label)
    return labels


def _underlined_field_labels(text: str) -> list[str]:
    """Extract semantic labels from an underlined input run.

    MinerU's underline style identifies the input position.  The run itself
    may contain a label (possibly enclosed in a parenthetical group), while
    literal examples such as XX or bracket placeholders are only optional
    naming evidence.
    """

    labels = _placeholder_field_labels(text)
    candidates: list[str] = []
    trailing_label = _trailing_field_label(
        text,
        reject_narrative=True,
        require_field_suffix=True,
    )
    if trailing_label:
        candidates.append(trailing_label)

    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
        if stripped[0] in _PARENTHETICAL_PAIRS:
            end = _bracket_group_end(stripped)
            if end is not None and not stripped[end + 1 :].strip(" \t：:，,；;。"):
                candidates.append(stripped[1:end])

    for candidate in candidates:
        label = _normalize_field_label(candidate)
        compact = re.sub(r"\s+", "", label)
        if (
            _EXTERNAL_PLACEHOLDER_LABEL_RE.search(compact)
            or not _is_field_label_candidate(
                label,
                require_field_suffix=True,
                reject_narrative=True,
            )
            or label in labels
        ):
            continue
        labels.append(label)
    return labels


def _template_text_fields(text: str) -> list[str]:
    fields: list[str] = []
    for match in _TEMPLATE_FIELD_RE.finditer(text):
        label = _normalize_field_label(match.group(1))
        remainder = text[match.end() :]
        value = _field_value_segment(remainder)
        has_filling_position = _has_filling_position(value)
        if not _is_field_label_candidate(
            label,
            reject_narrative=not has_filling_position,
        ):
            continue
        if has_filling_position and not _is_field_label_candidate(
            label,
            require_field_suffix=True,
            reject_narrative=True,
        ):
            continue
        if has_filling_position or (
            not value.strip(" \t\r\n。；;，,")
            and _is_field_label_candidate(label, require_field_suffix=True)
        ):
            if label not in fields:
                fields.append(label)
    return fields


def _has_labeled_date_slot(text: str) -> bool:
    for match in _TEMPLATE_FIELD_RE.finditer(text):
        value = _field_value_segment(text[match.end() :])
        if _FIELD_DATE_SLOT_RE.search(value):
            return True
    return False


def _mineru_content_runs(
    value: Any,
    inherited_styles: frozenset[str] = frozenset(),
) -> Iterable[tuple[str, frozenset[str]]]:
    if isinstance(value, str):
        yield value, inherited_styles
        return
    if isinstance(value, list):
        for item in value:
            yield from _mineru_content_runs(item, inherited_styles)
        return
    if not isinstance(value, dict):
        return

    styles = set(inherited_styles)
    raw_styles = value.get("style", [])
    if isinstance(raw_styles, str):
        raw_styles = [raw_styles]
    if isinstance(raw_styles, (list, tuple, set)):
        styles.update(str(style).casefold() for style in raw_styles)
    styles_frozen = frozenset(styles)

    for key in ("paragraph_content", "title_content", "item_content"):
        if key in value:
            yield from _mineru_content_runs(value[key], styles_frozen)
            return
    if "content" in value:
        yield from _mineru_content_runs(value["content"], styles_frozen)


def _block_mineru_content_runs(
    block: StructuredBlock,
) -> list[tuple[str, frozenset[str]]]:
    nested = block.metadata.get("mineru_nested_content")
    if nested is not None:
        runs = list(_mineru_content_runs(nested))
        if runs:
            return runs
    styles = block.metadata.get("style", [])
    if isinstance(styles, str):
        styles = [styles]
    if not isinstance(styles, (list, tuple, set)):
        styles = []
    return [(block.text, frozenset(str(style).casefold() for style in styles))]


def _template_styled_fields(
    blocks: Sequence[StructuredBlock],
) -> list[tuple[int, str]]:
    fields: list[tuple[int, str]] = []
    seen: set[str] = set()
    previous_text = ""
    for block_index, block in enumerate(blocks):
        runs = _block_mineru_content_runs(block)
        prefix = ""
        for text, styles in runs:
            is_underlined = "underline" in styles
            if is_underlined and text.strip():
                for label in _underlined_field_labels(text):
                    if label not in seen:
                        fields.append((block_index, label))
                        seen.add(label)
            if _has_filling_position(text, styles):
                label = _trailing_field_label(
                    prefix,
                    reject_narrative=True,
                    require_field_suffix=True,
                ) or _trailing_field_label(
                    previous_text,
                    reject_narrative=True,
                    require_field_suffix=True,
                )
                if label and label not in seen:
                    fields.append((block_index, label))
                    seen.add(label)
            prefix += text
        meaningful_text = "".join(text for text, styles in runs if text.strip())
        has_date_marker = any(
            "underline" in styles
            or "_" in text
            or _FIELD_PLAIN_X_PLACEHOLDER_RE.search(text)
            for text, styles in runs
        )
        if (
            _FIELD_DATE_SLOT_RE.search(block.text)
            and has_date_marker
            and not _has_labeled_date_slot(block.text)
        ):
            if "日期" not in seen:
                fields.append((block_index, "日期"))
                seen.add("日期")
        if meaningful_text:
            previous_text = meaningful_text
    return fields


def _template_table_rows(block: StructuredBlock) -> list[list[str]]:
    table_text = block.metadata.get("table_body")
    if not isinstance(table_text, str) or not table_text.strip():
        table_text = block.text
    html_rows = _mineru_html_table_rows(table_text)
    if html_rows:
        return html_rows
    rows: list[list[str]] = []
    for line in _strip_markup(table_text).splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) >= 2:
            rows.append(cells)
    return rows


def _template_table_fields(block: StructuredBlock) -> list[str]:
    rows = _template_table_rows(block)
    fields: list[str] = []

    def append(label: str) -> None:
        if label and label not in fields:
            fields.append(label)

    def is_data_marker(row: Sequence[str]) -> bool:
        if not row:
            return False
        first = row[0].strip()
        return bool(
            first == "示例"
            or re.fullmatch(r"[0-9０-９]+(?:[.．、)）])?", first)
            or re.fullmatch(r"[…·.]+", first)
        )

    data_start = next(
        (
            index
            for index, row in enumerate(rows[1:], start=1)
            if is_data_marker(row) or (row and all(not cell.strip() for cell in row))
        ),
        None,
    )
    data_rows = rows[data_start:] if data_start is not None else []
    has_data_marker = any(is_data_marker(row) for row in data_rows)
    has_key_value_row = not has_data_marker and any(
        len(row) >= 2
        and _is_field_label_candidate(row[0])
        and _is_table_placeholder(row[1])
        for row in rows[1:]
    )
    header_index = None
    if data_start is not None and not has_key_value_row:
        header_index = max(
            range(data_start),
            key=lambda index: len(rows[index]),
        )
    looks_like_matrix = header_index is not None and len(rows[header_index]) >= 2

    for row_index, row in enumerate(rows):
        for cell in row:
            for label in _template_text_fields(cell):
                append(label)
        if looks_like_matrix and row_index > header_index:
            continue
        for index in range(len(row) - 1):
            label = _normalize_field_label(row[index])
            compact = re.sub(r"\s+", "", label)
            if (
                not _is_field_label_candidate(label)
                or _TABLE_INDEX_LABEL_RE.fullmatch(compact)
            ):
                continue
            if _is_table_placeholder(row[index + 1]):
                append(label)

    if looks_like_matrix:
        headers = list(rows[header_index])
        if (
            header_index > 0
            and rows[0]
            and _TABLE_INDEX_LABEL_RE.fullmatch(
                re.sub(r"\s+", "", rows[0][0])
            )
            and len(headers) < max(len(row) for row in rows[header_index + 1 :])
        ):
            headers.insert(0, rows[0][0])
        data_rows = rows[header_index + 1 :]
        for index, header in enumerate(headers):
            label = _normalize_field_label(header)
            compact = re.sub(r"\s+", "", label)
            if (
                not _is_field_label_candidate(label)
                or _TABLE_INDEX_LABEL_RE.fullmatch(compact)
            ):
                continue
            if any(
                len(row) > index
                and _is_table_placeholder(row[index])
                for row in data_rows
            ):
                append(label)
    return fields


def _template_fields(blocks: Sequence[StructuredBlock]) -> list[str]:
    fields: list[str] = []
    styled_by_block: dict[int, list[str]] = {}
    for block_index, field in _template_styled_fields(blocks):
        styled_by_block.setdefault(block_index, []).append(field)

    def append(candidates: Iterable[str]) -> None:
        for field in candidates:
            if field and field not in fields:
                fields.append(field)

    for block_index, block in enumerate(blocks):
        append(styled_by_block.get(block_index, []))
        if block.type == "table":
            append(_template_table_fields(block))
        else:
            append(_template_text_fields(block.text))
    return fields


def _template_attachments(blocks: Sequence[StructuredBlock]) -> list[str]:
    attachments: list[str] = []

    def append(value: str, *, clause: str) -> None:
        normalized = re.sub(r"\s+", " ", value).strip(" \t。；;")
        if not normalized:
            return
        display = (
            re.sub(r"\s+", " ", clause).strip(" \t。；;")
            if _ATTACHMENT_CONDITION_RE.search(clause)
            else normalized
        )
        if display and display not in attachments:
            attachments.append(display)

    def split_materials(value: str) -> list[str]:
        parts: list[str] = []
        start = 0
        stack: list[str] = []
        closing_to_opening = {
            closing: opening for opening, closing in _PARENTHETICAL_PAIRS.items()
        }
        for index, character in enumerate(value):
            if character in _PARENTHETICAL_PAIRS:
                stack.append(character)
                continue
            opening = closing_to_opening.get(character)
            if opening is not None and stack and stack[-1] == opening:
                stack.pop()
                continue
            if not stack and character in "、及":
                part = value[start:index].strip(" \t，,")
                if part:
                    parts.append(part)
                start = index + 1
        tail = value[start:].strip(" \t，,")
        if tail:
            parts.append(tail)
        return parts or [value]

    def clauses(value: str) -> list[str]:
        result: list[str] = []
        start = 0
        stack: list[str] = []
        closing_to_opening = {
            closing: opening for opening, closing in _PARENTHETICAL_PAIRS.items()
        }
        for index, character in enumerate(value):
            if character in _PARENTHETICAL_PAIRS:
                stack.append(character)
                continue
            opening = closing_to_opening.get(character)
            if opening is not None and stack and stack[-1] == opening:
                stack.pop()
                continue
            if not stack and character in "。；;\n":
                clause = value[start:index].strip()
                if clause:
                    result.append(clause)
                start = index + 1
        tail = value[start:].strip()
        if tail:
            result.append(tail)
        return result

    def material_candidates(value: str) -> list[str]:
        value = re.sub(r"\s+", " ", _strip_markup(value)).strip(" \t|：:")
        value = re.sub(r"[，,]\s*(?:并|且|同时)?\s*$", "", value)
        value = re.sub(r"^(?:以下|相关|相应)?(?:材料|文件)\s*[：:]\s*", "", value)
        if (
            not value
            or not _ATTACHMENT_MATERIAL_RE.search(value)
            or _ATTACHMENT_GENERIC_MATERIAL_RE.fullmatch(value)
        ):
            return []
        candidates: list[str] = []
        for candidate in split_materials(value):
            candidate = candidate.strip(" \t|，,。；;")
            if (
                candidate
                and _ATTACHMENT_MATERIAL_RE.search(candidate)
                and not _ATTACHMENT_GENERIC_MATERIAL_RE.fullmatch(candidate)
                and not _ATTACHMENT_NOT_INDEPENDENT_RE.search(candidate)
            ):
                candidates.append(candidate)
        return candidates

    def submission_material_candidates(value: str) -> list[str]:
        """Extract noun-like material phrases immediately after a submission verb."""
        candidates: list[str] = []
        for segment in clauses(re.sub(r"([，,])", r"\1\n", value)):
            segment = re.sub(r"^(?:以及|并且|并|同时|或者|或)\s*", "", segment)
            for candidate in split_materials(segment):
                candidate = candidate.strip(" \t|，,。；;")
                if (
                    candidate
                    and _ATTACHMENT_MATERIAL_END_RE.search(candidate)
                    and material_candidates(candidate)
                ):
                    candidates.append(candidate)
        return candidates

    def submission_is_requirement(match: re.Match[str], clause: str) -> bool:
        action = match.group(0)
        if action in {"附上", "附带", "附送"} or action.endswith("附"):
            return True
        context = clause[max(0, match.start() - 36) : match.start()]
        if (
            _ATTACHMENT_NEGATIVE_CONTEXT_RE.search(context)
            or _ATTACHMENT_PROCESS_CONTEXT_RE.search(context)
        ):
            return False
        if action in {"提交", "递交", "上传", "出具", "报送"}:
            return True
        return bool(re.search(r"应当|必须|须|需|应|要求|承诺", context))

    def preceding_material(match: re.Match[str], clause: str) -> list[str]:
        prefix = clause[: match.start()].strip(" \t，,：:")
        if not prefix:
            return []
        action = match.group(0)
        if action.endswith("附"):
            before_modal = prefix
        else:
            modal_match = list(re.finditer(r"应当|必须|须|需|应", prefix))
            if not modal_match:
                return []
            modal = modal_match[-1]
            context = prefix[max(0, modal.start() - 4) : modal.end() + 4]
            if _ATTACHMENT_NEGATIVE_CONTEXT_RE.search(context):
                return []
            before_modal = prefix[: modal.start()]
        before_modal = re.split(r"[。；;，,：:\n]", before_modal)[-1]
        return submission_material_candidates(before_modal)

    pending_attachment_marker = False
    for block in blocks:
        if pending_attachment_marker and block.type == "heading":
            pending_attachment_marker = False
        texts = [block.text]
        if block.type == "table":
            rows = _template_table_rows(block)
            texts.extend(" | ".join(row) for row in rows)
            texts.extend(cell for row in rows for cell in row)
        for raw_text in texts:
            text = _strip_markup(raw_text)
            for clause in clauses(text):
                if pending_attachment_marker:
                    pending_candidates = material_candidates(clause)
                    if not pending_candidates:
                        pending_attachment_marker = False
                    for candidate in pending_candidates:
                        append(candidate, clause=clause)

                explicit_matches = list(_ATTACHMENT_RE.finditer(clause))
                for match in explicit_matches:
                    attachment = next(
                        (
                            match.group(group)
                            for group in (
                                "attachment",
                                "direct_attachment",
                                "legacy_attachment",
                            )
                            if match.group(group)
                        ),
                        "",
                    )
                    for candidate in material_candidates(attachment):
                        append(candidate, clause=clause)
                if re.fullmatch(r"附\s*[：:]", clause.strip()):
                    pending_attachment_marker = True

                submission_matches = list(_ATTACHMENT_SUBMISSION_RE.finditer(clause))
                for index, match in enumerate(submission_matches):
                    if not submission_is_requirement(match, clause):
                        continue
                    end = (
                        submission_matches[index + 1].start()
                        if index + 1 < len(submission_matches)
                        else len(clause)
                    )
                    after_candidates = submission_material_candidates(
                        clause[match.end() : end]
                    )
                    for candidate in after_candidates:
                        append(candidate, clause=clause)
                    if not after_candidates:
                        for candidate in preceding_material(match, clause):
                            append(candidate, clause=clause)
    return attachments


def _template_from_blocks(
    *,
    region: FunctionalRegion,
    name: str,
    blocks: Sequence[StructuredBlock],
    index: int,
) -> TenderTemplate:
    source_blocks = list(blocks)
    block_ids = [block.block_id for block in source_blocks]
    source_text = "\n".join(block.text for block in source_blocks)
    return {
        "id": f"tender_template_{index:03d}",
        "name": _template_name(name),
        "section": region.section,
        "block_ids": block_ids,
        "body": source_text,
        "tables": [
            {
                "block_id": block.block_id,
                "text": block.text,
                "metadata": dict(block.metadata),
            }
            for block in source_blocks
            if block.type == "table"
        ],
        "fields": _template_fields(source_blocks),
        "attachments": _template_attachments(source_blocks),
        "source": {
            "section": region.section,
            "block_ids": block_ids,
            "source_text": source_text,
        },
    }


def extract_templates_from_regions(
    regions: Sequence[FunctionalRegion],
) -> list[TenderTemplate]:
    """Build complete template objects from template regions."""

    templates: list[TenderTemplate] = []
    for region in regions:
        if region.kind != "templates":
            continue
        has_navigation_title = _is_template_navigation_region(region)
        index_block_ids = _template_index_block_ids(region)
        candidate_indexes = [
            index
            for index, block in enumerate(region.blocks)
            if block.block_id not in index_block_ids
            and _is_template_item_title(block, region)
        ]
        navigation_indexes = [
            index
            for index, block in enumerate(region.blocks[1:], start=1)
            if _is_template_navigation_title(block.text)
        ]
        for navigation_index in navigation_indexes:
            next_boundary = next(
                (
                    boundary
                    for boundary in range(navigation_index + 1, len(region.blocks))
                    if boundary in candidate_indexes
                    or boundary in navigation_indexes
                ),
                len(region.blocks),
            )
            templates.append(
                _template_from_blocks(
                    region=region,
                    name=region.blocks[navigation_index].text,
                    blocks=region.blocks[navigation_index:next_boundary],
                    index=len(templates) + 1,
                )
            )
        if _has_structural_heading_levels(region.blocks):
            # MinerU has already distinguished chapter/group/form levels.  Do
            # not re-discover form boundaries from bold text inside a form;
            # those unlevelled labels are part of the parent form body.
            item_indexes = candidate_indexes
        else:
            item_indexes = []
            for candidate_index in candidate_indexes:
                candidate = region.blocks[candidate_index]
                candidate_key = _compact_source_text(_template_name(candidate.text))
                if item_indexes:
                    previous = region.blocks[item_indexes[-1]]
                    previous_key = _compact_source_text(_template_name(previous.text))
                    if (
                        candidate_key == previous_key
                        and candidate_index - item_indexes[-1] <= 2
                    ):
                        continue
                    if (
                        "如有" in previous.text
                        and candidate_index - item_indexes[-1] <= 2
                        and (
                            previous_key[:10] in candidate_key
                            or candidate_key[:10] in previous_key
                        )
                    ):
                        continue
                    if (
                        previous_key
                        and previous_key in candidate_key
                        and candidate_index - item_indexes[-1] <= 2
                        and candidate.text.strip().startswith("近")
                    ):
                        continue
                has_structural_prefix = bool(
                    _TEMPLATE_NUMBER_PREFIX_RE.match(candidate.text.strip())
                    or re.match(r"^\s*\d+(?:\.\d+)+", candidate.text.strip())
                )
                if has_structural_prefix:
                    next_candidate = next(
                        (
                            next_index
                            for next_index in candidate_indexes
                            if next_index > candidate_index
                            and next_index - candidate_index <= 2
                            and _compact_source_text(
                                _template_name(region.blocks[next_index].text)
                            )
                            == candidate_key
                        ),
                        None,
                    )
                    if next_candidate is not None:
                        continue
                item_indexes.append(candidate_index)
        if len(region.blocks) == 1:
            continue
        if not item_indexes:
            if has_navigation_title:
                if not navigation_indexes and _is_template_navigation_title(region.title):
                    templates.append(
                        _template_from_blocks(
                            region=region,
                            name=region.title,
                            blocks=region.blocks[1:] or region.blocks,
                            index=len(templates) + 1,
                        )
                    )
                continue
            content_blocks = region.blocks[1:] or region.blocks
            templates.append(
                _template_from_blocks(
                    region=region,
                    name=region.title,
                    blocks=content_blocks,
                    index=len(templates) + 1,
                )
            )
            continue
        for item_number, start in enumerate(item_indexes):
            end = item_indexes[item_number + 1] if item_number + 1 < len(item_indexes) else len(region.blocks)
            if _has_structural_heading_levels(region.blocks):
                item_level = _block_heading_level(region.blocks[start])
                parent_boundary = next(
                    (
                        boundary_index
                        for boundary_index in range(start + 1, end)
                        if region.blocks[boundary_index].type == "heading"
                        and item_level is not None
                        and (
                            boundary_level := _block_heading_level(
                                region.blocks[boundary_index]
                            )
                        ) is not None
                        and boundary_level <= item_level
                    ),
                    None,
                )
                if parent_boundary is not None:
                    end = parent_boundary
            template_blocks = region.blocks[start:end]
            templates.append(
                _template_from_blocks(
                    region=region,
                    name=template_blocks[0].text,
                    blocks=template_blocks,
                    index=len(templates) + 1,
                )
            )
    deduplicated: list[TenderTemplate] = []
    for template in templates:
        same_name = [
            existing
            for existing in deduplicated
            if _compact_source_text(existing["name"])
            == _compact_source_text(template["name"])
            and template["name"] != "投标一览表"
        ]
        if not same_name:
            deduplicated.append(template)
            continue
        existing = same_name[0]
        if len(template["body"]) > len(existing["body"]):
            deduplicated[deduplicated.index(existing)] = template
    for index, template in enumerate(deduplicated, start=1):
        template["id"] = f"tender_template_{index:03d}"
    return deduplicated


_PROJECT_COMPILATION_RE = re.compile(
    r"投标文件组成|分别编制|电子投标文件的编制|文件大小|文件容量|容量|大小|"
    r"清晰|可读|签字盖章|扫描件|投标有效期|有效期|投标保证金|备选|"
    r"加密电子|投标文件形式|投标文件份数|递交方式|上传形式|报价.*(?:小数|位数|格式)|保留.*小数"
)
_PROJECT_NON_COMPILATION_RE = re.compile(
    r"评分|评标|评审因素|评标委员会|招标代理|中标候选人|履约|合同签订后|"
    r"终验|人员(?:请假|调班|更换|替换|报备)|知识产权归属|违约责任|"
    r"商业信誉|项目经验|服务能力|7\s*[×xX*]\s*24|售后服务"
)
_PROJECT_VALUE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:MB|GB|天|日)|(?:一|两|二|三|四|五|六|七|八|九|十)位小数"
)


class _MinerUTableParser(HTMLParser):
    """Extract logical rows from MinerU's HTML table representation."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag == "tr":
            if self._row is not None:
                self._finish_row()
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None:
                self._row = []
            if self._cell is not None:
                self._finish_cell()
            self._cell = []
        elif tag in {"br", "p", "div", "li"} and self._cell:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def _finish_cell(self) -> None:
        if self._cell is None:
            return
        if self._row is None:
            self._row = []
        value = re.sub(r"\s+", " ", unescape("".join(self._cell))).strip()
        self._row.append(value)
        self._cell = None

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None

    def finish(self) -> list[list[str]]:
        self._finish_row()
        return self.rows


def _mineru_html_table_rows(text: str) -> list[list[str]]:
    if "<tr" not in text.lower():
        return []
    parser = _MinerUTableParser()
    try:
        parser.feed(text)
        parser.close()
    except (TypeError, ValueError):
        return []
    return parser.finish()


def _project_rows(region: FunctionalRegion) -> Iterable[tuple[StructuredBlock, str]]:
    for block in region.blocks[1:]:
        if block.type == "table":
            table_text = block.metadata.get("table_body")
            if not isinstance(table_text, str) or not table_text.strip():
                table_text = block.text
            html_rows = _mineru_html_table_rows(table_text)
            lines = (
                [" | ".join(cell for cell in row if cell) for row in html_rows]
                if html_rows
                else block.text.splitlines()
            )
        else:
            lines = [block.text]
        for line in lines:
            text = _strip_markup(line).strip()
            if text:
                yield block, text


def _project_row_label(row: str) -> str:
    columns = [column.strip() for column in _strip_markup(row).split("|")]
    if len(columns) >= 2:
        return " | ".join(columns[:2])
    return row


def _project_value(row: str) -> str | None:
    columns = [column.strip() for column in _strip_markup(row).split("|")]
    if len(columns) >= 3:
        candidate = " | ".join(column for column in columns[2:] if column).strip()
    else:
        value_part = re.split(r"\||[：:]", row, maxsplit=1)
        candidate = value_part[1].strip() if len(value_part) == 2 else row
    match = _PROJECT_VALUE_RE.search(candidate)
    if match:
        return match.group(0)
    if re.search(r"无需|不允许|允许|只需|仅需|不得|不超过|应当|必须", candidate):
        return candidate
    return candidate if candidate and (len(columns) >= 2 or "|" in row or re.search(r"[：:]", row)) else None


def extract_project_requirements_from_regions(
    regions: Sequence[FunctionalRegion],
) -> list[ProjectRequirement]:
    """Extract only project-specific rules that shape the bid file itself."""

    requirements: list[ProjectRequirement] = []
    seen: set[str] = set()
    for region in regions:
        if region.kind != "project_requirements":
            continue
        no_bid_bond = bool(_NO_BID_BOND_RE.search(region.text))
        for block, row in _project_rows(region):
            if row.startswith("招标文件否决投标条款汇总"):
                break
            row_label = _project_row_label(row)
            if not _PROJECT_COMPILATION_RE.search(row_label):
                continue
            if _PROJECT_NON_COMPILATION_RE.search(row):
                continue
            if (
                no_bid_bond
                and re.search(r"保证金|保函", row_label)
                and not _NO_BID_BOND_RE.search(row)
            ):
                continue
            normalized_row = re.sub(r"\s+", " ", row).strip()
            if normalized_row in seen:
                continue
            seen.add(normalized_row)
            requirements.append(
                {
                    "id": f"project_requirement_{len(requirements) + 1:03d}",
                    "requirement": normalized_row,
                    "value": _project_value(normalized_row),
                    "source": {
                        "section": region.section,
                        "block_ids": [block.block_id],
                        "source_text": block.text,
                    },
                }
            )
    return requirements


_SUPPLEMENTAL_SUBMISSION_RE = re.compile(
    r"提供|提交|附|随投标文件|作为资格证明|上传"
)
_SUPPLEMENTAL_EXCLUDED_RE = re.compile(
    r"商业信誉|项目经验|服务能力|7\s*[×xX*]\s*24|履约|合同签订后|"
    r"终验|人员(?:请假|调班|更换|替换|报备)|知识产权归属|违约责任|售后服务"
)


def _supplemental_material_names(text: str) -> list[str]:
    names: list[str] = []
    if "营业执照" in text or "事业单位法人证书" in text:
        names.append("营业执照")
    if re.search(r"授权书|授权文件|分支机构授权", text):
        names.append("授权书")
    if re.search(r"业绩证明|同类业绩|业绩材料", text):
        names.append("业绩证明")
    if "合同关键页" in text:
        names.append("合同关键页")
    if "制造商登记证明" in text:
        names.append("制造商登记证明")
    if "资格证书" in text:
        names.append("资格证书")
    if "检测报告" in text:
        names.append("检测报告")
    if re.search(r"制造商.{0,12}(?:登记|注册)证明文件|登记（或注册）证明文件", text):
        names.append("制造商登记证明")
    return names


_PARENTHETICAL_PAIRS = {
    "（": "）",
    "(": ")",
    "【": "】",
    "[": "]",
}


def _complete_parenthetical_evidence(text: str, keyword: str) -> str:
    closing_to_opening = {
        closing: opening
        for opening, closing in _PARENTHETICAL_PAIRS.items()
    }
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        if character in _PARENTHETICAL_PAIRS:
            stack.append((character, index))
            continue
        opening = closing_to_opening.get(character)
        if opening is None or not stack or stack[-1][0] != opening:
            continue
        _, start = stack.pop()
        spans.append((start, index + 1))

    candidates = [
        text[start:end].strip()
        for start, end in spans
        if keyword in text[start:end]
    ]
    return min(candidates, key=len, default="")


def _supplemental_evidence_snippet(text: str, name: str) -> str:
    keyword = {
        "营业执照": "营业执照",
        "授权书": "授权",
        "业绩证明": "业绩证明",
        "合同关键页": "合同关键页",
        "制造商登记证明": "登记",
        "资格证书": "资格证书",
        "检测报告": "检测报告",
    }.get(name, name)
    parenthetical = _complete_parenthetical_evidence(text, keyword)
    if (
        parenthetical
        and _SUPPLEMENTAL_SUBMISSION_RE.search(parenthetical)
        and not _SUPPLEMENTAL_EXCLUDED_RE.search(parenthetical)
    ):
        return parenthetical
    clauses = [
        clause.strip().strip("。；;，,")
        for clause in re.split(r"(?<=[。；;，,：:])|\n+", text)
        if clause.strip()
    ]
    snippets = [clause for clause in clauses if keyword in clause]
    valid_snippets = [
        snippet
        for snippet in snippets
        if (
            _SUPPLEMENTAL_SUBMISSION_RE.search(snippet)
            and not _SUPPLEMENTAL_EXCLUDED_RE.search(snippet)
        )
    ]
    if valid_snippets:
        return min(valid_snippets, key=len)
    return snippets[0] if snippets else ""


def extract_supplemental_materials_from_regions(
    regions: Sequence[FunctionalRegion],
) -> list[SupplementalMaterial]:
    """Extract explicit bid-submission evidence outside complete templates."""

    materials: list[SupplementalMaterial] = []
    seen: set[tuple[str, str]] = set()
    for region in regions:
        if region.kind != "supplemental_materials":
            continue
        for block in region.blocks[1:]:
            # Keep MinerU line boundaries: one block may contain several
            # numbered qualification clauses, and collapsing them first makes
            # the evidence source span the whole block.
            text = re.sub(r"[ \t\f\v]+", " ", block.text).strip()
            if not text:
                continue
            for name in _supplemental_material_names(text):
                material = _supplemental_evidence_snippet(text, name)
                if (
                    not material
                    or not _SUPPLEMENTAL_SUBMISSION_RE.search(material)
                    or _SUPPLEMENTAL_EXCLUDED_RE.search(material)
                ):
                    continue
                key = (name, material)
                if key in seen:
                    continue
                seen.add(key)
                materials.append(
                    {
                        "id": f"supplemental_material_{len(materials) + 1:03d}",
                        "name": name,
                        "material": material,
                        "source": {
                            "section": region.section,
                            "block_ids": [block.block_id],
                            "source_text": material,
                        },
                    }
                )
    return materials


_BID_BOND_TEMPLATE_RE = re.compile(r"保证金|保函|缴纳凭证")
_PAPER_TEMPLATE_RE = re.compile(r"纸质|正本|副本|密封|包封")
_NO_BID_BOND_RE = re.compile(r"(?:无需|不需要|免于|免交|不递交).{0,12}保证金")
_ELECTRONIC_ONLY_RE = re.compile(
    r"(?:只需|仅需|仅|只).{0,12}(?:上传|递交|提交).{0,16}电子投标文件|"
    r"(?:上传|递交|提交).{0,20}(?:一份|一套).{0,12}(?:加密)?电子投标文件|"
    r"(?:加密)?电子投标文件(?:一份|一套)"
)
_PAPER_REQUIRED_RE = re.compile(
    r"(?:必须|需要|应当|应|提供|递交).{0,8}(?:纸质|正本|副本)"
)


def apply_project_applicability(
    templates: Sequence[TenderTemplate],
    project_requirements: Sequence[ProjectRequirement],
) -> tuple[list[TenderTemplate], list[dict[str, Any]]]:
    """Apply only explicit project-specific precedence to generic templates."""

    project_text = "\n".join(
        f"{item['requirement']} {item.get('value') or ''}"
        for item in project_requirements
    )
    no_bid_bond = bool(_NO_BID_BOND_RE.search(project_text))
    electronic_only = bool(_ELECTRONIC_ONLY_RE.search(project_text))
    paper_required = any(
        _PAPER_REQUIRED_RE.search(text)
        for text in (
            f"{item['requirement']} {item.get('value') or ''}"
            for item in project_requirements
            if not (
                no_bid_bond
                and re.search(r"保证金|保函", item["requirement"])
            )
        )
    )
    filtered: list[TenderTemplate] = []
    report: list[dict[str, Any]] = []
    for template in templates:
        reason: str | None = None
        # A normal bid-letter template may mention the generic bond clause in
        # its boilerplate.  Only a dedicated bond form/material is removed by
        # the explicit no-bond project rule.
        if no_bid_bond and _BID_BOND_TEMPLATE_RE.search(template["name"]):
            reason = "project_no_bid_bond"
        elif (
            electronic_only
            and not paper_required
            and _PAPER_TEMPLATE_RE.search(template["name"])
        ):
            reason = "project_electronic_only"
        if reason is None:
            filtered.append(template)
            continue
        report.append(
            {
                "name": template["name"],
                "block_ids": list(template["block_ids"]),
                "source_text": template["source"]["source_text"],
                "reason": reason,
            }
        )
    return filtered, report


def _normalize_source(
    source: Any,
    block_map: dict[str, StructuredBlock],
) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ComplianceExtractionError("提取对象来源结构无效。")
    source_ids = list(dict.fromkeys(source.get("block_ids", [])))
    if not source_ids or any(block_id not in block_map for block_id in source_ids):
        raise ComplianceExtractionError("提取对象来源 block_id 不存在。")
    source_blocks = sorted(
        (block_map[block_id] for block_id in source_ids),
        key=lambda block: block.order,
    )
    full_source_text = "\n".join(block.text for block in source_blocks)
    requested_source_text = source.get("source_text")
    source_text = full_source_text
    if isinstance(requested_source_text, str) and requested_source_text.strip():
        requested_key = _compact_source_text(_strip_markup(requested_source_text))
        full_source_key = _compact_source_text(_strip_markup(full_source_text))
        if requested_key and requested_key in full_source_key:
            source_text = requested_source_text.strip()
    return {
        "section": source_blocks[0].section,
        "block_ids": [block.block_id for block in source_blocks],
        "source_text": source_text,
    }


_FILE_REQUIREMENT_OUTPUT_FIELDS = {
    "name",
    "requirement",
    "target",
    "requirement_type",
    "constraint_status",
    "parameters",
    "auto_checkable",
    "support_reason",
    "source_block_ids",
}
_FILE_REQUIREMENT_TYPES = {"size", "extension", "filename", "other"}
_FILE_REQUIREMENT_OUT_OF_SCOPE_RE = re.compile(
    r"分别(?:上传|提交|递交)|同时(?:提交|上传|提供)|备份文件|"
    r"(?:商务标|技术标|报价文件).{0,20}(?:分别|各|同时)|"
    r"U\s*盘|优盘|光盘|纸质|正本|副本|密封|包装|外部标记|"
    r"多个文件|文件组合|另附\s*(?:Excel|PDF|Word|电子表格)|"
    r"(?:PDF|Excel|Word).{0,8}(?:组合|各一份|同时)"
)
_FILE_REQUIREMENT_PLATFORM_RE = re.compile(
    r"(?:系统|平台|网站|客户端).{0,16}(?:最大支持|支持上传|上传上限|容量上限)"
    r"|(?:最大支持|支持上传|上传上限|容量上限).{0,16}(?:系统|平台|网站|客户端)"
)
_FILE_REQUIREMENT_EXPLICIT_LIMIT_RE = re.compile(
    r"(?:不得|不能|不超过|小于等于|≤|上限|限值|最大为|最大不超过|应控制在)"
)
_FILE_NAME_GENERIC_LABELS = {
    "项目名称",
    "项目编号",
    "标段名称",
    "标包名称",
    "投标人名称",
    "供应商名称",
    "采购人名称",
}
_FILE_SIZE_UNITS = {
    "B": 1,
    "BYTE": 1,
    "BYTES": 1,
    "KB": 1024,
    "KIB": 1024,
    "MB": 1024**2,
    "MIB": 1024**2,
    "GB": 1024**3,
    "GIB": 1024**3,
}


def _coerce_file_requirement_output(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ComplianceExtractionError("文件级要求 Schema 校验失败：结果不是 JSON 对象。")
    if set(value) - {"file_requirements"}:
        raise ComplianceExtractionError("文件级要求 Schema 校验失败：包含未允许的顶层字段。")
    items = value.get("file_requirements", [])
    if not isinstance(items, list):
        raise ComplianceExtractionError("文件级要求 Schema 校验失败：file_requirements 必须是数组。")
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) - _FILE_REQUIREMENT_OUTPUT_FIELDS:
            raise ComplianceExtractionError(
                "文件级要求 Schema 校验失败：包含未允许字段。"
            )
        for field_name in ("name", "requirement", "target", "requirement_type", "constraint_status", "support_reason"):
            if not isinstance(item.get(field_name), str) or not item[field_name].strip():
                raise ComplianceExtractionError(
                    f"文件级要求 Schema 校验失败：{field_name} 无效。"
                )
        if item["requirement_type"] not in _FILE_REQUIREMENT_TYPES:
            raise ComplianceExtractionError(
                "文件级要求 Schema 校验失败：requirement_type 无效。"
            )
        if not isinstance(item.get("parameters"), dict):
            raise ComplianceExtractionError(
                "文件级要求 Schema 校验失败：parameters 必须是对象。"
            )
        if not isinstance(item.get("auto_checkable"), bool):
            raise ComplianceExtractionError(
                "文件级要求 Schema 校验失败：auto_checkable 必须是布尔值。"
            )
        source_block_ids = item.get("source_block_ids")
        if not isinstance(source_block_ids, list) or not source_block_ids or not all(
            isinstance(block_id, str) and block_id.strip() for block_id in source_block_ids
        ):
            raise ComplianceExtractionError(
                "文件级要求 Schema 校验失败：source_block_ids 无效。"
            )
        result.append(dict(item))
    return {"file_requirements": result}


def _numeric_value(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if decimal_value <= 0:
        return None
    if decimal_value == decimal_value.to_integral_value():
        return int(decimal_value)
    return float(decimal_value)


def _normalize_file_requirement_parameters(
    requirement_type: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], bool, str | None]:
    normalized = dict(parameters)
    if requirement_type == "size":
        raw_value = parameters.get("max_value", parameters.get("value"))
        numeric_value = _numeric_value(raw_value)
        unit = str(parameters.get("unit", "")).strip().upper()
        factor = _FILE_SIZE_UNITS.get(unit)
        if numeric_value is None or factor is None:
            return normalized, False, "大小上限缺少可识别的数值或单位"
        normalized["raw_value"] = numeric_value
        normalized["raw_unit"] = unit
        normalized["limit_bytes"] = int(Decimal(str(numeric_value)) * factor)
        return normalized, True, None
    if requirement_type == "extension":
        raw_extensions = parameters.get(
            "allowed_extensions",
            parameters.get("allowed_suffixes", parameters.get("extensions", [])),
        )
        if isinstance(raw_extensions, str):
            raw_extensions = re.split(r"[,、/；;\s]+", raw_extensions)
        if not isinstance(raw_extensions, list):
            raw_extensions = []
        extensions = []
        for value in raw_extensions:
            suffix = str(value).strip().lower()
            if not suffix:
                continue
            if not suffix.startswith("."):
                suffix = "." + suffix
            if re.fullmatch(r"\.[a-z0-9][a-z0-9._-]*", suffix):
                extensions.append(suffix)
        normalized["allowed_extensions"] = list(dict.fromkeys(extensions))
        if not normalized["allowed_extensions"]:
            return normalized, False, "文件格式要求缺少可识别的扩展名"
        return normalized, True, None
    if requirement_type == "filename":
        contains = parameters.get("contains", [])
        if isinstance(contains, str):
            contains = [contains]
        if not isinstance(contains, list):
            contains = []
        contains = [str(value).strip() for value in contains if str(value).strip()]
        normalized["contains"] = list(dict.fromkeys(contains))
        concrete = [
            value
            for value in contains
            if value not in _FILE_NAME_GENERIC_LABELS
            and not re.search(r"[<《【\[{（(].*(?:名称|编号|投标人).*[>》】\]}）)]", value)
        ]
        if not concrete:
            return normalized, False, "命名规则缺少可核对的具体项目或投标人值"
        normalized["concrete_contains"] = concrete
        return normalized, True, None
    return normalized, False, "当前没有针对该单文件属性的确定性比较器"


def normalize_file_requirements(
    value: Any,
    blocks: Sequence[StructuredBlock],
    *,
    filtered_report: list[dict[str, Any]] | None = None,
) -> list[FileRequirement]:
    """Validate, scope-filter and source-ground LLM file requirements."""

    output = _coerce_file_requirement_output(value)
    block_map = {block.block_id: block for block in blocks}
    normalized: list[FileRequirement] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()

    def reject(raw_item: dict[str, Any], reason: str) -> None:
        requirement = str(raw_item.get("requirement", "")).strip()
        logger.info(
            "file_requirements.filtered reason=%s requirement=%s",
            reason,
            requirement,
        )
        if filtered_report is not None:
            filtered_report.append(
                {
                    "reason": reason,
                    "name": raw_item.get("name", ""),
                    "requirement": requirement,
                    "target": raw_item.get("target", ""),
                    "constraint_status": raw_item.get("constraint_status", ""),
                    "source_block_ids": list(raw_item.get("source_block_ids", [])),
                }
            )

    for raw_item in output["file_requirements"]:
        requirement = raw_item["requirement"].strip()
        target = raw_item["target"].strip()
        constraint_status = raw_item["constraint_status"].strip()
        if target != "single_bid_file":
            reject(raw_item, "non_single_file_target")
            continue
        if constraint_status != "explicit_constraint":
            reject(raw_item, "not_explicit_constraint")
            continue
        if _FILE_REQUIREMENT_OUT_OF_SCOPE_RE.search(requirement):
            reject(raw_item, "out_of_scope")
            continue
        if _FILE_REQUIREMENT_PLATFORM_RE.search(requirement) and not _FILE_REQUIREMENT_EXPLICIT_LIMIT_RE.search(requirement):
            reject(raw_item, "platform_capability")
            continue
        source_ids = list(dict.fromkeys(raw_item["source_block_ids"]))
        if not source_ids or any(block_id not in block_map for block_id in source_ids):
            raise ComplianceExtractionError(
                "文件级要求 Schema 校验失败：来源 block_id 不存在。"
            )
        dedupe_key = (requirement, raw_item["requirement_type"], tuple(source_ids))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        parameters, structurally_checkable, parameter_reason = _normalize_file_requirement_parameters(
            raw_item["requirement_type"], raw_item["parameters"]
        )
        auto_checkable = bool(raw_item["auto_checkable"]) and structurally_checkable
        support_reason = raw_item["support_reason"].strip()
        if parameter_reason:
            support_reason = parameter_reason
        source = _normalize_source(
            {"block_ids": source_ids, "source_text": ""},
            block_map,
        )
        normalized.append(
            {
                "id": f"file_requirement_{len(normalized) + 1:03d}",
                "name": raw_item["name"].strip(),
                "requirement": requirement,
                "target": target,
                "requirement_type": raw_item["requirement_type"],
                "constraint_status": constraint_status,
                "parameters": parameters,
                "auto_checkable": auto_checkable,
                "support_reason": support_reason,
                "source": source,
            }
        )
    return normalized


def normalize_tender_extraction_sources(
    result: TenderExtractionResult,
    blocks: Sequence[StructuredBlock],
) -> TenderExtractionResult:
    """Validate and restore source metadata for all extracted object collections."""

    block_map = {block.block_id: block for block in blocks}
    normalized: TenderExtractionResult = {
        "templates": [],
        "project_requirements": [],
        "supplemental_materials": [],
        "file_requirements": [],
    }
    for key in normalized:
        for raw_item in result.get(key, []):
            if not isinstance(raw_item, dict):
                raise ComplianceExtractionError("提取对象不是有效对象。")
            item = dict(raw_item)
            item["source"] = _normalize_source(item.get("source"), block_map)
            if key == "templates":
                item["block_ids"] = list(item["source"]["block_ids"])
            normalized[key].append(item)  # type: ignore[arg-type]
    return normalized


def build_candidate_batches(
    candidates: Sequence[CandidateWindow],
    *,
    max_batches: int = 8,
    max_batch_chars: int = 12000,
) -> list[list[CandidateWindow]]:
    started_at = time.perf_counter()
    logger.info(
        "batch.build.start candidates=%d max_batches=%d max_batch_chars=%d",
        len(candidates),
        max_batches,
        max_batch_chars,
    )
    if max_batches < 1 or max_batches > 10:
        raise ValueError("max_batches 必须在 1 到 10 之间。")
    if max_batch_chars < 1:
        raise ValueError("max_batch_chars 必须大于 0。")
    if not candidates:
        logger.info("batch.build.end batches=0 elapsed_ms=%d", _elapsed_ms(started_at))
        return []

    def serialized_chars(candidate: CandidateWindow) -> int:
        return len(
            f"[{candidate.section}] block_ids={','.join(candidate.block_ids)}\n"
            f"{candidate.text}"
        )

    count = min(max_batches, len(candidates))
    batches: list[list[CandidateWindow]] = [[] for _ in range(count)]
    # Keep the established call-count distribution for normal documents. The
    # character check below only changes packing when an evenly distributed
    # bucket would exceed the prompt budget.
    for index, candidate in enumerate(candidates):
        bucket = min(index * count // len(candidates), count - 1)
        batches[bucket].append(candidate)

    def batch_chars(batch: Sequence[CandidateWindow]) -> int:
        return sum(serialized_chars(candidate) for candidate in batch) + max(
            0, len(batch) - 1
        ) * 2

    if any(
        len(batch) > 1 and batch_chars(batch) > max_batch_chars for batch in batches
    ):
        # Repack in source order, keeping the serialized candidate payload
        # within the configured budget. A single oversized window remains
        # intact rather than being truncated, because losing its tail could
        # lose an explicit tender requirement.
        repacked: list[list[CandidateWindow]] = []
        current: list[CandidateWindow] = []
        current_chars = 0
        for candidate in candidates:
            candidate_chars = serialized_chars(candidate)
            separator_chars = 2 if current else 0
            if (
                current
                and current_chars + separator_chars + candidate_chars
                > max_batch_chars
            ):
                repacked.append(current)
                current = []
                current_chars = 0
                separator_chars = 0
            current.append(candidate)
            current_chars += separator_chars + candidate_chars
        if current:
            repacked.append(current)
        batches = repacked
    if len(batches) > max_batches:
        raise ComplianceExtractionError(
            "候选内容超过 LLM 批次预算；请提高 max_batches 或 max_batch_chars。"
        )
    result = batches
    logger.info(
        "batch.build.end batches=%d candidate_count=%d elapsed_ms=%d",
        len(result),
        sum(len(batch) for batch in result),
        _elapsed_ms(started_at),
    )
    return result


def _filter_reason_counts(report: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in report:
        reason = str(item.get("reason", "unknown"))
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _annotate_navigation_templates(
    templates: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mark navigation templates while keeping their extracted source objects."""

    annotated: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for template in templates:
        item = dict(template)
        classification = classify_navigation_item(item)
        if classification["is_navigation"]:
            item["compliance_excluded"] = True
            item["compliance_exclusion_reason"] = classification["reason"]
            item["navigation_signals"] = list(classification["signals"])
            excluded.append(
                {
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "section": item.get("section", ""),
                    "block_ids": list(item.get("block_ids", [])),
                    "reason": classification["reason"],
                    "signals": list(classification["signals"]),
                }
            )
        annotated.append(item)
    return annotated, excluded


def _is_transient_extraction_error(error: ComplianceExtractionError) -> bool:
    cause = error.__cause__
    return isinstance(
        cause,
        (TimeoutError, ConnectionError, OSError, urllib.error.URLError),
    )


class DeterministicComplianceLLM:
    """Local fallback that keeps candidate text as source-grounded objects.

    It is source-grounded and deterministic, so development can run without a
    model credential; production can select ``OpenAICompatibleLLM`` instead.
    """

    def extract(self, batch: Sequence[CandidateWindow]) -> dict[str, list[dict[str, Any]]]:
        started_at = time.perf_counter()
        logger.info(
            "llm.call.start provider=deterministic model=local batch_size=%d candidate_chars=%d",
            len(batch),
            sum(len(candidate.text) for candidate in batch),
        )
        result: dict[str, list[dict[str, Any]]] = {
            "templates": [],
            "project_requirements": [],
            "supplemental_materials": [],
        }
        for candidate in batch:
            text = candidate.text.strip()
            if candidate.kind == "templates":
                first_line = text.splitlines()[0] if text else ""
                name = _template_name(first_line)
                if not _TEMPLATE_ITEM_NAME_RE.search(name):
                    name = _template_name(candidate.section)
                result["templates"].append(
                    {
                        "name": name,
                        "source_block_ids": list(candidate.block_ids),
                    }
                )
            elif candidate.kind == "project_requirements":
                result["project_requirements"].append(
                    {
                        "requirement": text,
                        "value": _project_value(text),
                        "source_block_ids": list(candidate.block_ids),
                    }
                )
            else:
                for name in _supplemental_material_names(text):
                    result["supplemental_materials"].append(
                        {
                            "name": name,
                            "material": text,
                            "source_block_ids": list(candidate.block_ids),
                        }
                    )
        logger.info(
            "llm.call.end provider=deterministic model=local batch_size=%d objects=%d elapsed_ms=%d",
            len(batch),
            sum(len(items) for items in result.values()),
            _elapsed_ms(started_at),
        )
        return result

    def extract_file_requirements(
        self,
        candidates: Sequence[CandidateWindow],
    ) -> dict[str, list[dict[str, Any]]]:
        """Provide a conservative local fallback for obvious single-file rules."""

        result: dict[str, list[dict[str, Any]]] = {"file_requirements": []}
        for candidate in candidates:
            for line in re.split(r"[\n；;。]+", candidate.text):
                text = line.strip()
                if not text:
                    continue
                size_match = re.search(
                    r"(?:文件|电子投标文件|组成部分)[^\n；;。]{0,40}"
                    r"(?:不得|不能|不超过|小于等于|≤)\s*"
                    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>KB|MB|GB|B)",
                    text,
                    re.IGNORECASE,
                )
                if size_match and not _FILE_REQUIREMENT_PLATFORM_RE.search(text):
                    result["file_requirements"].append(
                        {
                            "name": "投标文件大小限制",
                            "requirement": text,
                            "target": "single_bid_file",
                            "requirement_type": "size",
                            "constraint_status": "explicit_constraint",
                            "parameters": {
                                "max_value": float(size_match.group("value")),
                                "unit": size_match.group("unit").upper(),
                            },
                            "auto_checkable": True,
                            "support_reason": "原文明确给出单份文件大小上限",
                            "source_block_ids": list(candidate.block_ids),
                        }
                    )
                extension_match = re.search(
                    r"(?:文件格式|格式|采用|保存为)\s*(?:为|是|：|:)??\s*"
                    r"(?P<extension>PDF|DOCX?|XLSX?|PPTX?|ZIP|RAR)\s*(?:格式)?",
                    text,
                    re.IGNORECASE,
                )
                if extension_match and not _FILE_REQUIREMENT_OUT_OF_SCOPE_RE.search(text):
                    result["file_requirements"].append(
                        {
                            "name": "投标文件格式",
                            "requirement": text,
                            "target": "single_bid_file",
                            "requirement_type": "extension",
                            "constraint_status": "explicit_constraint",
                            "parameters": {
                                "allowed_extensions": [extension_match.group("extension")]
                            },
                            "auto_checkable": True,
                            "support_reason": "原文明确给出单份文件格式",
                            "source_block_ids": list(candidate.block_ids),
                        }
                    )
                filename_match = re.search(
                    r"文件(?:名称|名)[^\n；;。]{0,50}(?:应|须|需|必须).{0,30}"
                    r"(?:包含|含有|命名为)",
                    text,
                )
                if filename_match:
                    result["file_requirements"].append(
                        {
                            "name": "投标文件名称规范",
                            "requirement": text,
                            "target": "single_bid_file",
                            "requirement_type": "filename",
                            "constraint_status": "explicit_constraint",
                            "parameters": {"contains": ["项目名称", "投标人名称"]},
                            "auto_checkable": False,
                            "support_reason": "原文要求命名，但当前上下文未提供具体名称值",
                            "source_block_ids": list(candidate.block_ids),
                        }
                    )
        return result


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout_seconds: float = 90,
        max_tokens: int = 8192,
        provider: str = "dashscope",
        enable_thinking: bool = False,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max(256, min(max_tokens, 8192))
        self.provider = normalize_llm_provider(provider)
        self.enable_thinking = bool(enable_thinking)
        self._call_context = threading.local()

    def set_call_context(
        self,
        *,
        recorder: ComplianceExtractionRecorder | None,
        call_id: str | None,
    ) -> None:
        self._call_context.value = {"recorder": recorder, "call_id": call_id}

    def _active_call_context(self) -> dict[str, Any]:
        return getattr(self._call_context, "value", {})

    def extract(self, batch: Sequence[CandidateWindow]) -> dict[str, list[dict[str, Any]]]:
        started_at = time.perf_counter()
        logger.info(
            "llm.call.start provider=openai_compatible model=%s batch_size=%d candidate_chars=%d",
            self.model,
            len(batch),
            sum(len(candidate.text) for candidate in batch),
        )
        source = "\n\n".join(
            f"[{candidate.kind}] [{candidate.section}] block_ids={','.join(candidate.block_ids)}\n{candidate.text}"
            for candidate in batch
        )
        prompt = (
            "从以下有限的、已经按功能区域筛选的招标文件候选内容中识别投标模板和材料对象。"
            "只返回 JSON 对象 {\"templates\":[],\"project_requirements\":[],\"supplemental_materials\":[]}。"
            "templates 每项只能包含 name、source_block_ids；project_requirements 每项只能包含 requirement、value、source_block_ids；"
            "supplemental_materials 每项只能包含 name、material、source_block_ids。"
            "source_block_ids 必须且只能复制输入候选中的真实 block_ids，不得生成 source_text。\n"
            "模板必须按连续 block 分组，一个完整模板只返回一个对象；name 是稳定简短名称，不要把模板正文拆成规则。"
            "项目要求只保留直接影响投标文件组成、编制、填写、容量、形式、有效期、保证金、备选方案和报价格式的行，并保留项目具体值。"
            "补充材料只保留明确要求随投标文件提供的具体证明材料。原文不明确时不要猜测。\n"
            "LLM 只负责有限候选的分组和命名；不得生成 check_type、scope、evidence_type、checks、required_field 或其他执行字段；"
            "不得把模板编译成自然语言规则，不得扫描候选之外的招标文件。排除评分/评标、外部采购系统状态、合同签订后、履约、终验、"
            "人员管理、知识产权归属和违约责任。\n\n"
            + source
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是招标文件合规要求抽取器。"},
                {"role": "user", "content": prompt},
            ],
        }
        payload.update(build_thinking_params(self.provider, self.enable_thinking))
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
            method="POST",
        )
        call_context = self._active_call_context()
        recorder = call_context.get("recorder")
        call_id = call_context.get("call_id")
        if recorder is not None and call_id is not None:
            try:
                recorder.attach_llm_input(call_id, payload)
            except Exception as recorder_error:
                logger.error(
                    "artifact.llm.input.error call_id=%s error_type=%s",
                    call_id,
                    type(recorder_error).__name__,
                )
        try:
            with llm_request_slot():
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    response_text = response.read().decode("utf-8")
                    try:
                        response_payload = json.loads(response_text)
                    except json.JSONDecodeError:
                        if recorder is not None and call_id is not None:
                            try:
                                recorder.attach_llm_response(
                                    call_id,
                                    raw_response=response_text,
                                )
                            except Exception as recorder_error:
                                logger.error(
                                    "artifact.llm.output.error call_id=%s error_type=%s",
                                    call_id,
                                    type(recorder_error).__name__,
                                )
                        raise
            if recorder is not None and call_id is not None:
                try:
                    choice = response_payload.get("choices", [{}])[0]
                    recorder.attach_llm_response(
                        call_id,
                        raw_response=response_payload,
                        finish_reason=choice.get("finish_reason"),
                        usage=response_payload.get("usage"),
                    )
                except Exception as recorder_error:
                    logger.error(
                        "artifact.llm.output.error call_id=%s error_type=%s",
                        call_id,
                        type(recorder_error).__name__,
                    )
            content = response_payload["choices"][0]["message"]["content"]
            decoded = json.loads(content) if isinstance(content, str) else content
            if not isinstance(decoded, dict):
                raise ValueError("object result must be a JSON object")
            result = {
                key: decoded.get(key, [])
                for key in (
                    "templates",
                    "project_requirements",
                    "supplemental_materials",
                )
            }
            if any(not isinstance(items, list) for items in result.values()):
                raise ValueError("object result collections must be lists")
            logger.info(
                "llm.call.end provider=openai_compatible model=%s batch_size=%d objects=%d elapsed_ms=%d",
                self.model,
                len(batch),
                sum(len(items) for items in result.values()),
                _elapsed_ms(started_at),
            )
            return result
        except TimeoutError as exc:
            logger.error(
                "llm.call.error provider=openai_compatible model=%s error_type=timeout elapsed_ms=%d",
                self.model,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError("LLM 合规要求提取失败：请求超时。") from exc
        except urllib.error.HTTPError as exc:
            logger.error(
                "llm.call.error provider=openai_compatible model=%s error_type=http_%s elapsed_ms=%d",
                self.model,
                exc.code,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError(
                f"LLM 合规要求提取失败：HTTP {exc.code}。"
            ) from exc
        except urllib.error.URLError as exc:
            logger.error(
                "llm.call.error provider=openai_compatible model=%s error_type=url_error elapsed_ms=%d",
                self.model,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError(
                "LLM 合规要求提取失败：网络连接错误。"
            ) from exc
        except json.JSONDecodeError as exc:
            logger.error(
                "llm.call.error provider=openai_compatible model=%s error_type=invalid_json elapsed_ms=%d",
                self.model,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError(
                "LLM 合规要求提取失败：模型响应不是有效 JSON。"
            ) from exc
        except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error(
                "llm.call.error provider=openai_compatible model=%s error_type=%s elapsed_ms=%d",
                self.model,
                type(exc).__name__,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError(
                "LLM 合规要求提取失败：响应结构异常。"
            ) from exc

    def extract_file_requirements(
        self,
        candidates: Sequence[CandidateWindow],
    ) -> dict[str, list[dict[str, Any]]]:
        """Extract only explicit constraints on the current single bid file."""

        started_at = time.perf_counter()
        source = "\n\n".join(
            f"[{candidate.section}] block_ids={','.join(candidate.block_ids)}\n{candidate.text}"
            for candidate in candidates
        )
        prompt = (
            "从以下已经按章节结构筛选的招标文件上下文中，提取所有明确要求当前这一份电子投标文件自身属性的规则。"
            "只返回 JSON 对象 {\"file_requirements\":[]}，每项只能包含 name、requirement、target、"
            "requirement_type、constraint_status、parameters、auto_checkable、support_reason、source_block_ids。"
            "target 必须为 single_bid_file；requirement_type 只能为 size、extension、filename、other；"
            "constraint_status 只有在原文明示投标约束时才填 explicit_constraint。"
            "size 的 parameters 保留 max_value、unit；extension 保留 allowed_extensions；filename 保留原文明确的具体字面量，"
            "如果只有“项目名称/投标人名称”等待外部事实的泛化标签，仍可提取但 auto_checkable 必须为 false。"
            "source_block_ids 必须且只能复制输入上下文中的真实 block_id，不得生成 source_text。"
            "每个返回项都必须有至少一个 source_block_ids；如果不能确定来源 block，直接不返回该项而返回空数组。"
            "support_reason 只写不超过100字的直接依据，不得写分析过程、复核过程或自我对话。"
            "LLM 只负责提取和结构化，不检查投标文件，不得猜测缺失的名称、编号或容量。"
            "若原文明确写“单个电子投标文件”或“单个组成部分”的大小上限，且该上限可直接用于当前这一份上传文件，"
            "应提取为 single_bid_file 的 size 规则；“单个组成部分大小不超过50MB”属于每个待上传单文件的上限，"
            "即使同一段还出现“分别编制/分别上传”，也必须保留这一条单文件上限；只排除“总容量”“全部组成部分合计”等集合总量要求。"
            "严格排除以下内容：文件数量、分别提交或分别上传商务标/技术标/报价文件、备份文件、多个文件组合或多格式组合、"
            "U盘/光盘、纸质正副本、密封包装外部标记、文件之间关系，以及无法仅靠当前单份文件元数据判断的要求。"
            "“系统最大支持上传 500MB”这类平台能力说明不是投标约束，必须排除。"
            "不得把平台支持上传容量当作投标约束。原文不明确是否为投标约束时不要返回。\n\n"
            + source
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是招标文件单份电子投标文件属性要求提取器。"},
                {"role": "user", "content": prompt},
            ],
        }
        payload.update(build_thinking_params(self.provider, self.enable_thinking))
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
            method="POST",
        )
        call_context = self._active_call_context()
        recorder = call_context.get("recorder")
        call_id = call_context.get("call_id")
        if recorder is not None and call_id is not None:
            recorder.attach_llm_input(call_id, payload)
        try:
            with llm_request_slot():
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
            if recorder is not None and call_id is not None:
                choice = response_payload.get("choices", [{}])[0]
                recorder.attach_llm_response(
                    call_id,
                    raw_response=response_payload,
                    finish_reason=choice.get("finish_reason"),
                    usage=response_payload.get("usage"),
                )
            content = response_payload["choices"][0]["message"]["content"]
            decoded = json.loads(content) if isinstance(content, str) else content
            if not isinstance(decoded, dict):
                raise ValueError("file requirement result must be a JSON object")
            result = _coerce_file_requirement_output(decoded)
            logger.info(
                "llm.file_requirements.end provider=openai_compatible model=%s candidates=%d candidate_chars=%d requirements=%d elapsed_ms=%d",
                self.model,
                len(candidates),
                len(source),
                len(result["file_requirements"]),
                _elapsed_ms(started_at),
            )
            return result
        except TimeoutError as exc:
            raise ComplianceExtractionError("LLM 文件级要求提取失败：请求超时。") from exc
        except urllib.error.HTTPError as exc:
            raise ComplianceExtractionError(
                f"LLM 文件级要求提取失败：HTTP {exc.code}。"
            ) from exc
        except urllib.error.URLError as exc:
            raise ComplianceExtractionError("LLM 文件级要求提取失败：网络连接错误。") from exc
        except json.JSONDecodeError as exc:
            raise ComplianceExtractionError(
                "LLM 文件级要求提取失败：模型响应不是有效 JSON。"
            ) from exc
        except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ComplianceExtractionError(
                "LLM 文件级要求提取失败：响应结构异常。"
            ) from exc

    def review_template(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Run one JSON-only template text comparison call."""

        started_at = time.perf_counter()
        logger.info(
            "llm.template_review.start provider=openai_compatible model=%s prompt_chars=%d",
            self.model,
            len(system_prompt) + len(user_prompt),
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        payload.update(build_thinking_params(self.provider, self.enable_thinking))
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
            method="POST",
        )
        call_context = self._active_call_context()
        recorder = call_context.get("recorder")
        call_id = call_context.get("call_id")
        if recorder is not None and call_id is not None:
            try:
                recorder.attach_llm_input(call_id, payload)
            except Exception as recorder_error:
                logger.error(
                    "artifact.llm.input.error call_id=%s error_type=%s",
                    call_id,
                    type(recorder_error).__name__,
                )
        try:
            with llm_request_slot():
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    response_text = response.read().decode("utf-8")
                    try:
                        response_payload = json.loads(response_text)
                    except json.JSONDecodeError:
                        if recorder is not None and call_id is not None:
                            try:
                                recorder.attach_llm_response(
                                    call_id,
                                    raw_response=response_text,
                                )
                            except Exception as recorder_error:
                                logger.error(
                                    "artifact.llm.output.error call_id=%s error_type=%s",
                                    call_id,
                                    type(recorder_error).__name__,
                                )
                        raise
            if recorder is not None and call_id is not None:
                try:
                    choice = response_payload.get("choices", [{}])[0]
                    recorder.attach_llm_response(
                        call_id,
                        raw_response=response_payload,
                        finish_reason=choice.get("finish_reason"),
                        usage=response_payload.get("usage"),
                    )
                except Exception as recorder_error:
                    logger.error(
                        "artifact.llm.output.error call_id=%s error_type=%s",
                        call_id,
                        type(recorder_error).__name__,
                    )
            content = response_payload["choices"][0]["message"]["content"]
            decoded = json.loads(content) if isinstance(content, str) else content
            if not isinstance(decoded, dict):
                raise ValueError("template review result must be a JSON object")
            logger.info(
                "llm.template_review.end provider=openai_compatible model=%s elapsed_ms=%d",
                self.model,
                _elapsed_ms(started_at),
            )
            return decoded
        except TimeoutError as exc:
            logger.error(
                "llm.template_review.error provider=openai_compatible model=%s error_type=timeout elapsed_ms=%d",
                self.model,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError("LLM 模板文本检查失败：请求超时。") from exc
        except urllib.error.HTTPError as exc:
            logger.error(
                "llm.template_review.error provider=openai_compatible model=%s error_type=http_%s elapsed_ms=%d",
                self.model,
                exc.code,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError(
                f"LLM 模板文本检查失败：HTTP {exc.code}。"
            ) from exc
        except urllib.error.URLError as exc:
            logger.error(
                "llm.template_review.error provider=openai_compatible model=%s error_type=url_error elapsed_ms=%d",
                self.model,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError("LLM 模板文本检查失败：网络连接错误。") from exc
        except json.JSONDecodeError as exc:
            logger.error(
                "llm.template_review.error provider=openai_compatible model=%s error_type=invalid_json elapsed_ms=%d",
                self.model,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError(
                "LLM 模板文本检查失败：模型响应不是有效 JSON。"
            ) from exc
        except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error(
                "llm.template_review.error provider=openai_compatible model=%s error_type=%s elapsed_ms=%d",
                self.model,
                type(exc).__name__,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError(
                "LLM 模板文本检查失败：响应结构异常。"
            ) from exc

    def review_template_exception(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """Run one JSON-only template exception-conclusion review call."""

        return self.review_template(system_prompt, user_prompt)


def _parsed_document_cache_key(path: Path, parser: DocumentParser) -> str:
    # The parser mode is part of the namespace.  In particular, an old local
    # DOCX fallback result must never look like a successful MinerU result.
    parser_descriptor_value = getattr(parser, "cache_descriptor", None)
    if callable(parser_descriptor_value):
        parser_descriptor_value = parser_descriptor_value()
    if parser_descriptor_value is None:
        parser_descriptor_value = {"type": type(parser).__name__}
    try:
        parser_descriptor = json.dumps(
            parser_descriptor_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        parser_descriptor = str(parser_descriptor_value)
    return hashlib.sha256(
        PARSED_DOCUMENT_CACHE_VERSION.encode("utf-8")
        + b"\0"
        + parser_descriptor.encode("utf-8")
        + b"\0"
        + path.read_bytes()
    ).hexdigest()


def _component_descriptor(component: Any) -> str:
    """Return a stable, non-secret descriptor for cache-relevant components."""
    descriptor: dict[str, Any] = {
        "type": f"{type(component).__module__}.{type(component).__qualname__}"
    }
    custom_descriptor = getattr(component, "cache_descriptor", None)
    if callable(custom_descriptor):
        custom_descriptor = custom_descriptor()
    if custom_descriptor is not None:
        try:
            json.dumps(custom_descriptor, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            custom_descriptor = str(custom_descriptor)
        descriptor["custom"] = custom_descriptor
    config_names = {
        "backend",
        "base_url",
        "command",
        "config",
        "endpoint",
        "max_batch_chars",
        "max_batches",
        "max_tokens",
        "mode",
        "model",
        "options",
        "provider",
        "settings",
        "timeout_seconds",
        "version",
    }
    secret_fragments = ("key", "token", "secret", "password", "credential")
    try:
        component_values = vars(component)
    except TypeError:
        component_values = {}
    for name, value in sorted(component_values.items()):
        lowered = name.lower()
        if (
            name.startswith("_")
            or name not in config_names
            and not name.endswith(("_config", "_options"))
            or any(fragment in lowered for fragment in secret_fragments)
        ):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            descriptor[name] = value
            continue
        try:
            json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            continue
        descriptor[name] = value
    return json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _requirement_cache_key(
    path: Path,
    parser: DocumentParser,
    llm: RequirementLLM,
    *,
    max_batches: int,
    max_batch_chars: int,
) -> str:
    """Hash all inputs that can change the three tender object collections."""
    extraction_descriptor = {
        "parser": _component_descriptor(parser),
        "llm": _component_descriptor(llm),
        "max_batches": max_batches,
        "max_batch_chars": max_batch_chars,
    }
    return hashlib.sha256(
        REQUIREMENT_CACHE_VERSION.encode("utf-8")
        + b"\0"
        + json.dumps(
            extraction_descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\0"
        + path.read_bytes()
    ).hexdigest()


def _empty_tender_extraction_result() -> TenderExtractionResult:
    return {
        "templates": [],
        "project_requirements": [],
        "supplemental_materials": [],
        "file_requirements": [],
    }


def _valid_object_result(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(key), list)
        for key in ("templates", "project_requirements", "supplemental_materials")
    )


def _coerce_object_output(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ComplianceExtractionError("LLM Schema 校验失败：对象结果不是 JSON 对象。")
    allowed_fields = {
        "templates": {"name", "source_block_ids"},
        "project_requirements": {"requirement", "value", "source_block_ids"},
        "supplemental_materials": {"name", "material", "source_block_ids"},
    }
    if set(value) - set(allowed_fields):
        raise ComplianceExtractionError("LLM Schema 校验失败：包含未允许的顶层字段。")
    result: dict[str, list[dict[str, Any]]] = {}
    for kind, fields in allowed_fields.items():
        items = value.get(kind, [])
        if not isinstance(items, list):
            raise ComplianceExtractionError(
                f"LLM Schema 校验失败：{kind} 必须是数组。"
            )
        result[kind] = []
        for item in items:
            if not isinstance(item, dict) or set(item) - fields:
                raise ComplianceExtractionError(
                    f"LLM Schema 校验失败：{kind} 包含未允许字段。"
                )
            source_block_ids = item.get("source_block_ids")
            if not isinstance(source_block_ids, list) or not source_block_ids or not all(
                isinstance(block_id, str) and block_id for block_id in source_block_ids
            ):
                raise ComplianceExtractionError(
                    f"LLM Schema 校验失败：{kind} 的 source_block_ids 无效。"
                )
            if kind == "templates" and (
                not isinstance(item.get("name"), str) or not item["name"].strip()
            ):
                raise ComplianceExtractionError("LLM Schema 校验失败：模板名称为空。")
            if kind == "project_requirements" and (
                not isinstance(item.get("requirement"), str)
                or not item["requirement"].strip()
                or item.get("value") is not None
                and not isinstance(item.get("value"), str)
            ):
                raise ComplianceExtractionError("LLM Schema 校验失败：项目要求无效。")
            if kind == "supplemental_materials" and (
                not isinstance(item.get("name"), str)
                or not item["name"].strip()
                or not isinstance(item.get("material"), str)
                or not item["material"].strip()
            ):
                raise ComplianceExtractionError("LLM Schema 校验失败：补充材料无效。")
            result[kind].append(dict(item))
    return result


def _ambiguous_regions(
    regions: Sequence[FunctionalRegion],
) -> list[FunctionalRegion]:
    ambiguous: list[FunctionalRegion] = []
    for region in regions:
        if region.kind == "templates":
            if (
                _is_template_index_region(region)
                or _is_template_navigation_region(region)
                or len(region.blocks) == 1
            ):
                continue
            has_named_template = any(
                _is_template_item_title(block, region) for block in region.blocks
            )
            if not has_named_template and len(region.blocks) > 1:
                ambiguous.append(region)
        elif region.kind == "project_requirements":
            if (
                not extract_project_requirements_from_regions([region])
                and _PROJECT_COMPILATION_RE.search(region.text)
            ):
                ambiguous.append(region)
        elif (
            not extract_supplemental_materials_from_regions([region])
            and any(
                _supplemental_material_names(block.text)
                for block in region.blocks[1:]
            )
            and _SUPPLEMENTAL_SUBMISSION_RE.search(region.text)
        ):
            ambiguous.append(region)
    return ambiguous


def _object_source_region(
    source_ids: Sequence[str],
    region_by_id: dict[str, FunctionalRegion],
    *,
    kind: FunctionalRegionKind,
) -> FunctionalRegion | None:
    mapped_regions: list[FunctionalRegion] = []
    for block_id in source_ids:
        region = region_by_id.get(block_id)
        if region is None:
            raise ComplianceExtractionError(
                "LLM Schema 校验失败：来源 block_id 不存在。"
            )
        if all(id(region) != id(existing) for existing in mapped_regions):
            mapped_regions.append(region)
    if len(mapped_regions) != 1 or mapped_regions[0].kind != kind:
        logger.warning(
            "objects.source.ignore kind=%s source_ids=%s reason=region_mismatch",
            kind,
            ",".join(source_ids),
        )
        return None
    return mapped_regions[0]


def _compact_source_text(value: str) -> str:
    return re.sub(r"[\s，。；、：:（）()【】“”\"'《》…,.!?！？\-—_]", "", value)


def _source_text_supports_rule(rule: str, source_text: str) -> bool:
    compact_rule = _compact_source_text(rule)
    compact_source = _compact_source_text(source_text)
    if not compact_rule or compact_rule in compact_source:
        return bool(compact_rule)
    clauses = re.split(r"[，。；、：:,.!?！？]+", rule)
    return any(
        len(clause.strip()) >= 8
        and _compact_source_text(clause.strip()) in compact_source
        for clause in clauses
    )


def _template_name_supported(name: str, text: str) -> bool:
    name_key = _compact_source_text(_template_name(name))
    text_key = _compact_source_text(_template_name(text))
    return bool(name_key) and name_key == text_key


def _object_source_ids(
    item: dict[str, Any],
    *,
    kind: FunctionalRegionKind,
    source_ids: Sequence[str],
    region: FunctionalRegion,
) -> list[str]:
    """Repair model-selected IDs only within the same candidate region.

    The model is allowed to name and group objects, but its block references
    are still checked against the parsed source.  If the cited blocks do not
    contain the object name/value, search the same functional-region window
    for the supporting anchor.  This keeps provenance deterministic and avoids
    accepting a shifted citation from a long flattened DOCX sequence.
    """

    if kind == "templates":
        support_text = item["name"]
        supports = lambda block: _template_name_supported(support_text, block.text)
        excluded_ids = _template_index_block_ids(region)
        exact_anchors = [
            block.block_id
            for block in region.blocks
            if block.block_id not in excluded_ids and supports(block)
        ]
        if exact_anchors:
            return exact_anchors
    elif kind == "project_requirements":
        support_text = " ".join(
            value
            for value in (item.get("requirement", ""), item.get("value") or "")
            if value
        )
        supports = lambda block: _source_text_supports_rule(
            support_text, block.text
        )
    else:
        support_text = " ".join(
            value
            for value in (item.get("name", ""), item.get("material", ""))
            if value
        )
        supports = lambda block: _source_text_supports_rule(
            support_text, block.text
        )

    cited_blocks = [block for block in region.blocks if block.block_id in source_ids]
    if kind == "templates":
        cited_blocks = [
            block
            for block in cited_blocks
            if block.block_id not in excluded_ids
        ]
    if any(supports(block) for block in cited_blocks):
        return list(source_ids)
    repaired = [block.block_id for block in region.blocks if supports(block)]
    if repaired:
        logger.warning(
            "objects.source.repair kind=%s name=%s model_source_count=%d repaired_source_count=%d",
            kind,
            item.get("name") or item.get("requirement"),
            len(source_ids),
            len(repaired),
        )
        return repaired
    if kind == "templates" and source_ids and all(
        block_id in excluded_ids for block_id in source_ids
    ):
        logger.warning(
            "objects.source.ignore kind=templates source_ids=%s reason=table_of_contents",
            ",".join(source_ids),
        )
        return []
    # Some valid short names (especially synthetic/test headings) do not occur
    # verbatim in the flattened text.  Keep the model IDs in that case; the
    # deterministic block-ID and region checks still apply.
    return list(source_ids)


def _template_span_blocks(
    region: FunctionalRegion,
    source_ids: Sequence[str],
    next_start: int | None,
) -> list[StructuredBlock]:
    block_positions = {
        block.block_id: index for index, block in enumerate(region.blocks)
    }
    start = min(block_positions[block_id] for block_id in source_ids)
    end = next_start if next_start is not None and next_start > start else len(region.blocks)
    return region.blocks[start:end]


def _merge_object_output(
    result: TenderExtractionResult,
    output: dict[str, list[dict[str, Any]]],
    regions: Sequence[FunctionalRegion],
    *,
    replace_template_regions: Sequence[FunctionalRegion] = (),
) -> None:
    region_by_id = {
        block.block_id: region
        for region in regions
        for block in region.blocks
    }
    template_items_by_region: dict[int, list[tuple[dict[str, Any], list[str], FunctionalRegion]]] = {}
    for item in output["templates"]:
        if _TEMPLATE_MATERIAL_ONLY_RE.search(item["name"]):
            logger.warning(
                "objects.template.ignore name=%s reason=material_only",
                item["name"],
            )
            continue
        source_ids = list(dict.fromkeys(item["source_block_ids"]))
        region = _object_source_region(
            source_ids, region_by_id, kind="templates"
        )
        if region is None:
            continue
        source_ids = _object_source_ids(
            item,
            kind="templates",
            source_ids=source_ids,
            region=region,
        )
        if not source_ids:
            continue
        template_items_by_region.setdefault(id(region), []).append(
            (item, source_ids, region)
        )

    # Deterministic extraction deliberately creates a coarse fallback when a
    # flattened region has no visible heading blocks.  Once the LLM supplies
    # valid anchors for that same region, replace that fallback so the output
    # remains one complete object per template rather than one object for the
    # entire chapter.
    for region in replace_template_regions:
        if id(region) not in template_items_by_region:
            continue
        region_ids = set(region.block_ids)
        result["templates"] = [
            template
            for template in result["templates"]
            if not set(template["block_ids"]).issubset(region_ids)
        ]

    for entries in template_items_by_region.values():
        ordered_entries = sorted(
            entries,
            key=lambda entry: min(
                next(
                    index
                    for index, block in enumerate(entry[2].blocks)
                    if block.block_id == block_id
                )
                for block_id in entry[1]
            ),
        )
        starts = [
            min(
                next(
                    index
                    for index, block in enumerate(region.blocks)
                    if block.block_id == block_id
                )
                for block_id in source_ids
            )
            for _, source_ids, region in ordered_entries
        ]
        for entry_index, (item, source_ids, region) in enumerate(ordered_entries):
            next_start = starts[entry_index + 1] if entry_index + 1 < len(starts) else None
            source_blocks = _template_span_blocks(region, source_ids, next_start)
            result["templates"].append(
                _template_from_blocks(
                    region=region,
                    name=item["name"],
                    blocks=source_blocks,
                    index=len(result["templates"]) + 1,
                )
            )
    for item in output["project_requirements"]:
        source_ids = list(dict.fromkeys(item["source_block_ids"]))
        region = _object_source_region(
            source_ids, region_by_id, kind="project_requirements"
        )
        if region is None:
            continue
        if (
            not _PROJECT_COMPILATION_RE.search(item["requirement"])
            or _PROJECT_NON_COMPILATION_RE.search(item["requirement"])
        ):
            continue
        source_ids = _object_source_ids(
            item,
            kind="project_requirements",
            source_ids=source_ids,
            region=region,
        )
        if any(
            existing["requirement"].strip() == item["requirement"].strip()
            and existing["source"]["block_ids"] == source_ids
            for existing in result["project_requirements"]
        ):
            continue
        result["project_requirements"].append(
            {
                "id": f"project_requirement_{len(result['project_requirements']) + 1:03d}",
                "requirement": item["requirement"].strip(),
                "value": item.get("value"),
                "source": {
                    "section": "",
                    "block_ids": source_ids,
                    "source_text": "",
                },
            }
        )
    for item in output["supplemental_materials"]:
        source_ids = list(dict.fromkeys(item["source_block_ids"]))
        region = _object_source_region(
            source_ids, region_by_id, kind="supplemental_materials"
        )
        if region is None:
            continue
        if (
            not _SUPPLEMENTAL_SUBMISSION_RE.search(item["material"])
            or _SUPPLEMENTAL_EXCLUDED_RE.search(item["material"])
        ):
            continue
        source_ids = _object_source_ids(
            item,
            kind="supplemental_materials",
            source_ids=source_ids,
            region=region,
        )
        if any(
            existing["name"] == item["name"].strip()
            and existing["source"]["block_ids"] == source_ids
            for existing in result["supplemental_materials"]
        ):
            continue
        result["supplemental_materials"].append(
            {
                "id": f"supplemental_material_{len(result['supplemental_materials']) + 1:03d}",
                "name": item["name"].strip(),
                "material": item["material"].strip(),
                "source": {
                    "section": "",
                    "block_ids": source_ids,
                    "source_text": "",
                },
            }
        )


def _dedupe_supplemental_materials(
    materials: Sequence[SupplementalMaterial],
    templates: Sequence[TenderTemplate],
) -> list[SupplementalMaterial]:
    result: list[SupplementalMaterial] = []
    for material in materials:
        template_source_ids = {
            block_id
            for template in templates
            for block_id in template["source"]["block_ids"]
        }
        template_contains_material = bool(
            set(material["source"]["block_ids"]).intersection(template_source_ids)
        )
        if template_contains_material:
            continue
        if any(
            existing["name"] == material["name"]
            and existing["material"] == material["material"]
            for existing in result
        ):
            continue
        result.append(material)
    for index, material in enumerate(result, start=1):
        material["id"] = f"supplemental_material_{index:03d}"
    return result


def _serialize_functional_regions(
    regions: Sequence[FunctionalRegion],
) -> list[dict[str, Any]]:
    return [asdict(region) for region in regions]


def extract_tender_compliance_objects(
    tender_file: FileMetadata,
    *,
    parser: DocumentParser | None = None,
    llm: RequirementLLM | None = None,
    cache: RequirementCache | None = None,
    parser_cache: RequirementCache | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
    max_batches: int = 8,
    max_batch_chars: int = 12000,
    max_retries: int = 2,
) -> TenderExtractionResult:
    started_at = time.perf_counter()
    path = Path(tender_file.storage_path)
    active_parser = parser or MinerUDocumentParser()
    active_llm = llm
    cache_llm = llm or DeterministicComplianceLLM()
    active_recorder = recorder
    if active_recorder is None and path.parent.exists():
        try:
            active_recorder = ComplianceExtractionRecorder.from_tender_path(path)
        except OSError:
            active_recorder = None
    stats: dict[str, Any] = {
        "filename": tender_file.filename,
        "file_size": tender_file.size,
        "cache_enabled": cache is not None,
        "cache_kind": "tender_compliance_objects",
        "requirement_prompt_version": REQUIREMENT_PROMPT_VERSION,
        "parser_cache_enabled": parser_cache is not None,
        "parser_cache_hit": False,
        "parser_cache_elapsed_ms": None,
        "cache_hit": False,
        "cache_elapsed_ms": None,
        "parser": type(active_parser).__name__,
        "actual_parser": getattr(
            active_parser, "parser_name", type(active_parser).__name__
        ),
        "parser_transport": None,
        "mineru_called": False,
        "mineru_call_elapsed_ms": None,
        "parser_execution_source": None,
        "parser_elapsed_ms": None,
        "functional_region_elapsed_ms": None,
        "template_elapsed_ms": None,
        "project_requirement_elapsed_ms": None,
        "supplemental_material_elapsed_ms": None,
        "file_requirement_candidate_count": 0,
        "file_requirement_candidate_chars": 0,
        "file_requirement_prompt_chars": 0,
        "file_requirement_extraction_source": None,
        "file_requirement_llm_elapsed_ms": 0,
        "file_requirement_llm_total_calls": 0,
        "file_requirement_llm_completed_calls": 0,
        "file_requirement_llm_failed_calls": 0,
        "file_requirement_llm_output_count": 0,
        "file_requirement_filtered_count": 0,
        "file_requirement_filtered_by_reason": {},
        "file_requirement_count": 0,
        "applicability_elapsed_ms": None,
        "parsed_blocks": 0,
        "parsed_block_counts": {},
        "parsed_heading_levels": [],
        "parsed_blocks_with_metadata": 0,
        "parsed_metadata_keys": [],
        "mineru_raw_item_count": None,
        "mineru_raw_type_counts": {},
        "mineru_raw_table_count": None,
        "mineru_raw_table_previews": [],
        "mineru_front_table_raw_context": [],
        "mineru_front_table_present": None,
        "project_front_table_recovered": False,
        "functional_regions": 0,
        "template_count": 0,
        "participating_template_count": 0,
        "navigation_excluded_template_count": 0,
        "navigation_excluded_templates": [],
        "project_requirement_count": 0,
        "supplemental_material_count": 0,
        "filtered_objects": 0,
        "filtered_by_reason": {},
        "llm_model": getattr(active_llm, "model", None),
        "llm_total_calls": 0,
        "llm_completed_calls": 0,
        "llm_failed_calls": 0,
        "llm_retries": 0,
        "llm_elapsed_ms": 0,
        "schema_valid_calls": 0,
    }
    run_status = "failed"
    failed_stage: str | None = None
    failure: Exception | None = None

    def record_event(event: str, **fields: Any) -> None:
        if active_recorder is None:
            return
        try:
            active_recorder.event(event, **fields)
        except Exception as recorder_error:
            logger.error(
                "artifact.event.error event=%s error_type=%s",
                event,
                type(recorder_error).__name__,
            )

    def persist_artifact(name: str, payload: Any) -> None:
        if active_recorder is None:
            return
        try:
            active_recorder.write_json(name, payload)
        except Exception as recorder_error:
            logger.error(
                "artifact.write.error name=%s error_type=%s",
                name,
                type(recorder_error).__name__,
            )

    def persist_result(result: TenderExtractionResult, *, source: str = "extraction") -> None:
        persist_artifact("03_templates.json", {"source": source, "templates": result["templates"]})
        persist_artifact(
            "04_project_requirements.json",
            {"source": source, "project_requirements": result["project_requirements"]},
        )
        persist_artifact(
            "05_supplemental_materials.json",
            {"source": source, "supplemental_materials": result["supplemental_materials"]},
        )
        persist_artifact(
            "09_file_requirements.json",
            {"source": source, "file_requirements": result["file_requirements"]},
        )
        persist_artifact("07_result.json", result)

    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数。")
        if active_recorder is not None:
            stats["artifact_directory"] = str(active_recorder.artifact_dir)
            stats["task_id"] = active_recorder.task_dir.name
        record_event(
            "compliance.extract.start",
            file=tender_file.filename,
            cache_enabled=cache is not None,
            max_batches=max_batches,
            max_batch_chars=max_batch_chars,
        )

        cache_key = _requirement_cache_key(
            path,
            active_parser,
            cache_llm,
            max_batches=max_batches,
            max_batch_chars=max_batch_chars,
        )
        cache_started_at = time.perf_counter()
        cached = cache.get(cache_key) if cache is not None else None
        stats["cache_elapsed_ms"] = _elapsed_ms(cache_started_at)
        if _valid_object_result(cached):
            result: TenderExtractionResult = cached
            result["file_requirements"] = list(result.get("file_requirements", []))
            result["templates"], navigation_exclusions = _annotate_navigation_templates(
                result["templates"]
            )
            stats["cache_hit"] = True
            stats["parser_execution_source"] = "result_cache"
            descriptor = getattr(active_parser, "cache_descriptor", None)
            if callable(descriptor):
                descriptor = descriptor()
            if isinstance(descriptor, dict):
                stats["parser_transport"] = descriptor.get("transport")
            stats["template_count"] = len(result["templates"])
            stats["participating_template_count"] = (
                stats["template_count"] - len(navigation_exclusions)
            )
            stats["navigation_excluded_template_count"] = len(navigation_exclusions)
            stats["navigation_excluded_templates"] = navigation_exclusions
            stats["project_requirement_count"] = len(result["project_requirements"])
            stats["supplemental_material_count"] = len(result["supplemental_materials"])
            stats["file_requirement_count"] = len(result["file_requirements"])
            persist_artifact("02_functional_regions.json", {"source": "result_cache", "regions": []})
            persist_result(result, source="result_cache")
            persist_artifact(
                "06_navigation_exclusion_report.json",
                {"tender_templates": navigation_exclusions, "bid_modules": []},
            )
            run_status = "complete"
            record_event(
                "cache.check.end",
                status="hit",
                kind="tender_compliance_objects",
                templates=stats["template_count"],
                project_requirements=stats["project_requirement_count"],
                supplemental_materials=stats["supplemental_material_count"],
                file_requirements=stats["file_requirement_count"],
            )
            record_event(
                "compliance.extract.end",
                status="complete",
                source="result_cache",
                templates=stats["template_count"],
                project_requirements=stats["project_requirement_count"],
                supplemental_materials=stats["supplemental_material_count"],
                file_requirements=stats["file_requirement_count"],
            )
            return result

        blocks: list[StructuredBlock]
        parser_cache_started_at = time.perf_counter()
        parsed_cache_value = (
            parser_cache.get(_parsed_document_cache_key(path, active_parser))
            if parser_cache is not None
            else None
        )
        parsed_cache_blocks = _deserialize_blocks(parsed_cache_value)
        stats["parser_cache_elapsed_ms"] = _elapsed_ms(parser_cache_started_at)
        if parsed_cache_blocks is not None:
            blocks = parsed_cache_blocks
            stats["parser_cache_hit"] = True
            stats["parser_execution_source"] = "parser_cache"
            stats["project_front_table_recovered"] = any(
                block.metadata.get("source_recovery") == "docx_front_table"
                for block in blocks
            )
            descriptor = getattr(active_parser, "cache_descriptor", None)
            if callable(descriptor):
                descriptor = descriptor()
            if isinstance(descriptor, dict):
                stats["parser_transport"] = descriptor.get("transport")
            record_event("document.parse.cache_hit", blocks=len(blocks))
        else:
            parser_started_at = time.perf_counter()
            blocks = active_parser.parse(path)
            stats["parser_elapsed_ms"] = _elapsed_ms(parser_started_at)
            diagnostics = getattr(active_parser, "parse_diagnostics", {})
            if isinstance(diagnostics, dict):
                stats["actual_parser"] = diagnostics.get(
                    "parser", stats["actual_parser"]
                )
                stats["parser_transport"] = diagnostics.get(
                    "service_protocol", stats["parser_transport"]
                )
                stats["mineru_called"] = bool(diagnostics.get("mineru_called"))
                stats["mineru_call_elapsed_ms"] = diagnostics.get(
                    "elapsed_ms", stats["parser_elapsed_ms"]
                )
                for key in (
                    "mineru_raw_item_count",
                    "mineru_raw_type_counts",
                    "mineru_raw_table_count",
                    "mineru_raw_table_previews",
                    "mineru_front_table_raw_context",
                    "mineru_front_table_present",
                    "project_front_table_recovered",
                ):
                    if key in diagnostics:
                        stats[key] = diagnostics[key]
            stats["parser_execution_source"] = "parse"
            if parser_cache is not None:
                parser_cache.set(
                    _parsed_document_cache_key(path, active_parser),
                    _serialize_blocks(blocks),
                )
            record_event(
                "document.parse.end",
                parser=stats["actual_parser"],
                parser_transport=stats["parser_transport"],
                mineru_called=stats["mineru_called"],
                blocks=len(blocks),
                elapsed_ms=stats["parser_elapsed_ms"],
            )
        stats["parsed_blocks"] = len(blocks)
        stats.update(_block_structure_stats(blocks))
        persist_artifact("01_parsed_blocks.json", {"blocks": _serialize_blocks(blocks)})

        region_started_at = time.perf_counter()
        regions = identify_functional_regions(blocks)
        stats["functional_region_elapsed_ms"] = _elapsed_ms(region_started_at)
        stats["functional_regions"] = len(regions)
        persist_artifact(
            "02_functional_regions.json",
            {"regions": _serialize_functional_regions(regions)},
        )
        record_event(
            "functional.region.end",
            regions=len(regions),
            elapsed_ms=stats["functional_region_elapsed_ms"],
        )

        template_started_at = time.perf_counter()
        templates = extract_templates_from_regions(regions)
        stats["template_elapsed_ms"] = _elapsed_ms(template_started_at)
        project_started_at = time.perf_counter()
        project_requirements = extract_project_requirements_from_regions(regions)
        stats["project_requirement_elapsed_ms"] = _elapsed_ms(project_started_at)
        supplemental_started_at = time.perf_counter()
        supplemental_materials = extract_supplemental_materials_from_regions(regions)
        stats["supplemental_material_elapsed_ms"] = _elapsed_ms(supplemental_started_at)
        result = {
            "templates": templates,
            "project_requirements": project_requirements,
            "supplemental_materials": supplemental_materials,
            "file_requirements": [],
        }

        file_candidates = build_file_requirement_candidates(blocks)
        file_candidate_chars = sum(len(candidate.text) for candidate in file_candidates)
        file_prompt_chars = sum(
            len(
                f"[{candidate.section}] block_ids={','.join(candidate.block_ids)}\n"
                f"{candidate.text}"
            )
            for candidate in file_candidates
        )
        stats["file_requirement_candidate_count"] = len(file_candidates)
        stats["file_requirement_candidate_chars"] = file_candidate_chars
        stats["file_requirement_prompt_chars"] = file_prompt_chars
        persist_artifact(
            "08_file_requirement_candidates.json",
            {
                "candidate_count": len(file_candidates),
                "candidate_chars": file_candidate_chars,
                "prompt_chars": file_prompt_chars,
                "candidates": _serialize_candidates(file_candidates),
            },
        )
        file_requirement_extractor = getattr(active_llm, "extract_file_requirements", None)
        if file_requirement_extractor is None and active_llm is None:
            fallback_llm = DeterministicComplianceLLM()
            file_requirement_extractor = fallback_llm.extract_file_requirements
            stats["file_requirement_extraction_source"] = "deterministic_fallback"
        elif file_requirement_extractor is not None:
            stats["file_requirement_extraction_source"] = type(active_llm).__name__
        if file_candidates and callable(file_requirement_extractor):
            file_filter_report: list[dict[str, Any]] = []
            file_call_id = (
                active_recorder.start_llm_call(
                    batch_index=1,
                    batch_count=1,
                    attempt=1,
                    model=getattr(active_llm, "model", "deterministic"),
                    batch=_serialize_candidates(file_candidates),
                )
                if active_recorder is not None
                else None
            )
            if isinstance(active_llm, OpenAICompatibleLLM):
                active_llm.set_call_context(
                    recorder=active_recorder,
                    call_id=file_call_id,
                )
            stats["file_requirement_llm_total_calls"] += 1
            file_call_started_at = time.perf_counter()
            try:
                raw_file_output = file_requirement_extractor(file_candidates)
                if isinstance(raw_file_output, dict):
                    raw_items = raw_file_output.get("file_requirements", [])
                    stats["file_requirement_llm_output_count"] = (
                        len(raw_items) if isinstance(raw_items, list) else 0
                    )
                file_requirements = normalize_file_requirements(
                    raw_file_output,
                    blocks,
                    filtered_report=file_filter_report,
                )
                result["file_requirements"] = file_requirements
                file_elapsed_ms = _elapsed_ms(file_call_started_at)
                stats["file_requirement_llm_elapsed_ms"] = file_elapsed_ms
                stats["file_requirement_llm_completed_calls"] += 1
                stats["file_requirement_filtered_count"] = len(file_filter_report)
                stats["file_requirement_filtered_by_reason"] = _filter_reason_counts(
                    file_filter_report
                )
                if active_recorder is not None and file_call_id is not None:
                    active_recorder.complete_llm_call(
                        file_call_id,
                        parsed_objects=raw_file_output,
                        schema_valid=True,
                        elapsed_ms=file_elapsed_ms,
                    )
            except Exception as exc:
                file_elapsed_ms = _elapsed_ms(file_call_started_at)
                stats["file_requirement_llm_elapsed_ms"] = file_elapsed_ms
                stats["file_requirement_llm_failed_calls"] += 1
                file_filter_report.append(
                    {
                        "reason": "schema_or_call_error",
                        "name": "",
                        "requirement": "",
                        "target": "",
                        "constraint_status": "",
                        "source_block_ids": [],
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                stats["file_requirement_filtered_count"] = len(file_filter_report)
                stats["file_requirement_filtered_by_reason"] = _filter_reason_counts(
                    file_filter_report
                )
                result["file_requirements"] = []
                logger.warning(
                    "file_requirements.extract.error error_type=%s; existing extraction continues without formal file rules",
                    type(exc).__name__,
                )
                if active_recorder is not None and file_call_id is not None:
                    active_recorder.fail_llm_call(
                        file_call_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        elapsed_ms=file_elapsed_ms,
                    )

        ambiguous = _ambiguous_regions(regions)
        if active_llm is not None and not isinstance(active_llm, DeterministicComplianceLLM) and ambiguous:
            candidates = [
                CandidateWindow(
                    block_ids=region.block_ids,
                    section=region.section,
                    text=region.text,
                    order=region.order,
                    kind=region.kind,
                )
                for region in ambiguous
            ]
            batches = build_candidate_batches(
                candidates,
                max_batches=max_batches,
                max_batch_chars=max_batch_chars,
            )
            for batch_index, batch in enumerate(batches, start=1):
                succeeded = False
                for attempt in range(1, max_retries + 2):
                    call_id = (
                        active_recorder.start_llm_call(
                            batch_index=batch_index,
                            batch_count=len(batches),
                            attempt=attempt,
                            model=getattr(active_llm, "model", type(active_llm).__name__),
                            batch=_serialize_candidates(batch),
                        )
                        if active_recorder is not None
                        else None
                    )
                    stats["llm_total_calls"] += 1
                    if isinstance(active_llm, OpenAICompatibleLLM):
                        active_llm.set_call_context(
                            recorder=active_recorder,
                            call_id=call_id,
                        )
                    call_started_at = time.perf_counter()
                    try:
                        raw_output = active_llm.extract(batch)
                        output = _coerce_object_output(raw_output)
                        elapsed_ms = _elapsed_ms(call_started_at)
                        stats["llm_elapsed_ms"] += elapsed_ms
                        stats["llm_completed_calls"] += 1
                        stats["schema_valid_calls"] += 1
                        if active_recorder is not None and call_id is not None:
                            active_recorder.complete_llm_call(
                                call_id,
                                parsed_objects=output,
                                schema_valid=True,
                                elapsed_ms=elapsed_ms,
                            )
                        _merge_object_output(
                            result,
                            output,
                            regions,
                            replace_template_regions=[
                                candidate_region
                                for candidate_region in ambiguous
                                if candidate_region.kind == "templates"
                                and any(
                                    candidate.block_ids == candidate_region.block_ids
                                    for candidate in batch
                                )
                            ],
                        )
                        succeeded = True
                        break
                    except Exception as exc:
                        elapsed_ms = _elapsed_ms(call_started_at)
                        stats["llm_elapsed_ms"] += elapsed_ms
                        stats["llm_failed_calls"] += 1
                        if active_recorder is not None and call_id is not None:
                            active_recorder.fail_llm_call(
                                call_id,
                                error_type=type(exc).__name__,
                                error_message=str(exc),
                                elapsed_ms=elapsed_ms,
                            )
                        error = (
                            exc
                            if isinstance(exc, ComplianceExtractionError)
                            else ComplianceExtractionError(str(exc))
                        )
                        if attempt <= max_retries and _is_transient_extraction_error(error):
                            stats["llm_retries"] += 1
                            continue
                        raise error
                if not succeeded:
                    raise ComplianceExtractionError("LLM 对象提取未完成。")

        result["templates"], navigation_exclusions = _annotate_navigation_templates(
            result["templates"]
        )
        eligible_templates = [
            template
            for template in result["templates"]
            if not template.get("compliance_excluded")
        ]
        applicability_started_at = time.perf_counter()
        filtered_templates, applicability_report = apply_project_applicability(
            eligible_templates, result["project_requirements"]
        )
        filtered_template_ids = {
            str(template.get("id", "")) for template in filtered_templates
        }
        result["templates"] = [
            template
            for template in result["templates"]
            if template.get("compliance_excluded")
            or str(template.get("id", "")) in filtered_template_ids
        ]
        result["supplemental_materials"] = _dedupe_supplemental_materials(
            result["supplemental_materials"], result["templates"]
        )
        stats["applicability_elapsed_ms"] = _elapsed_ms(applicability_started_at)
        normalized = normalize_tender_extraction_sources(result, blocks)
        stats["filtered_objects"] = len(applicability_report)
        stats["filtered_by_reason"] = _filter_reason_counts(applicability_report)
        stats["template_count"] = len(normalized["templates"])
        stats["participating_template_count"] = (
            stats["template_count"] - len(navigation_exclusions)
        )
        stats["navigation_excluded_template_count"] = len(navigation_exclusions)
        stats["navigation_excluded_templates"] = navigation_exclusions
        stats["project_requirement_count"] = len(normalized["project_requirements"])
        stats["supplemental_material_count"] = len(normalized["supplemental_materials"])
        stats["file_requirement_count"] = len(normalized["file_requirements"])
        persist_result(normalized)
        persist_artifact(
            "09_file_requirement_filter_report.json",
            {
                "filtered_count": stats["file_requirement_filtered_count"],
                "filtered_by_reason": stats["file_requirement_filtered_by_reason"],
            },
        )
        persist_artifact("06_filter_report.json", applicability_report)
        persist_artifact(
            "06_navigation_exclusion_report.json",
            {"tender_templates": navigation_exclusions, "bid_modules": []},
        )
        if cache is not None:
            cache.set(cache_key, normalized)
        record_event(
            "compliance.extract.end",
            status="complete",
            templates=stats["template_count"],
            project_requirements=stats["project_requirement_count"],
            supplemental_materials=stats["supplemental_material_count"],
            file_requirements=stats["file_requirement_count"],
            filtered_objects=stats["filtered_objects"],
            elapsed_ms=_elapsed_ms(started_at),
        )
        run_status = "complete"
        return normalized
    except Exception as exc:
        failure = exc
        failed_stage = failed_stage or "extraction"
        record_event(
            "compliance.extract.error",
            stage=failed_stage,
            error_type=type(exc).__name__,
            error_message=str(exc),
            elapsed_ms=_elapsed_ms(started_at),
        )
        raise
    finally:
        stats["total_elapsed_ms"] = _elapsed_ms(started_at)
        if active_recorder is not None:
            try:
                active_recorder.finalize(
                    status=run_status,
                    stats=stats,
                    failed_stage=failed_stage,
                    error_type=type(failure).__name__ if failure else None,
                    error_message=str(failure) if failure else None,
                    elapsed_ms=stats["total_elapsed_ms"],
                )
            except Exception as recorder_error:
                logger.error(
                    "artifact.finalize.error error_type=%s",
                    type(recorder_error).__name__,
                )
