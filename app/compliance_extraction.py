from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol
from xml.etree import ElementTree

import httpx

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import (
    FileMetadata,
    ProjectRequirement,
    SupplementalMaterial,
    TenderExtractionResult,
    TenderTemplate,
)

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
logger = logging.getLogger(__name__)
REQUIREMENT_PROMPT_VERSION = "tender-compliance-objects-prompt-v1"
# Bump the object-result cache when deterministic segmentation or source
# reconciliation changes.  The parsed MinerU document cache intentionally has
# its own stable version and remains reusable.
REQUIREMENT_CACHE_VERSION = f"tender-compliance-objects-v14:{REQUIREMENT_PROMPT_VERSION}"
PARSED_DOCUMENT_CACHE_VERSION = "mineru-parse-v2"
MINERU_TASKS_PROTOCOL_VERSION = "pdf-trans-tasks-v1"
MINERU_TASKS_PROTOCOL_LABEL = "pdf_trans_tasks"
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
            for key in ("text_level", "heading_level", "level"):
                value = block.metadata.get(key)
                try:
                    if int(value) > 0:
                        heading_levels.add(int(value))
                        break
                except (TypeError, ValueError):
                    continue
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


@dataclass(frozen=True)
class CandidateWindow:
    block_ids: list[str]
    section: str
    text: str
    order: int
    kind: Literal["templates", "project_requirements", "supplemental_materials"] = "templates"


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
        except FileNotFoundError, OSError, json.JSONDecodeError:
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
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return blocks


def _element_text(element: ElementTree.Element) -> str:
    return "".join(element.itertext()).replace("\u00a0", " ").strip()


def _paragraph_style(paragraph: ElementTree.Element) -> str:
    style = paragraph.find(f"{W_NS}pPr/{W_NS}pStyle")
    return (style.get(f"{W_NS}val") if style is not None else "") or ""


def _is_heading(text: str, style: str) -> bool:
    style_lower = style.lower()
    if style_lower.startswith("heading") or style_lower in {"title", "subtitle"}:
        return True
    return bool(
        re.match(
            r"^(?:第[一二三四五六七八九十百千万0-9]+[章节部分篇]|附件|投标文件格式)",
            text,
        )
    )


def parse_docx_document(path: Path) -> list[StructuredBlock]:
    """Recover ordered paragraph/table blocks from a DOCX package.

    This parser is intentionally only a development/test fallback.  Normal
    tender extraction must use the project's MinerU service integration.
    """

    started_at = time.perf_counter()
    logger.info("document.parse.start parser=docx file=%s", path.name)
    try:
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        logger.error(
            "document.parse.error parser=docx file=%s error_type=%s elapsed_ms=%d",
            path.name,
            type(exc).__name__,
            _elapsed_ms(started_at),
        )
        raise ComplianceExtractionError(f"无法解析招标文件结构：{path.name}") from exc

    blocks: list[StructuredBlock] = []
    current_section = ""
    order = 0
    body = root.find(f"{W_NS}body")
    if body is None:
        logger.info(
            "document.parse.end parser=docx file=%s blocks=0 elapsed_ms=%d",
            path.name,
            _elapsed_ms(started_at),
        )
        return blocks

    for child in body:
        if child.tag == f"{W_NS}p":
            text = _element_text(child)
            if not text:
                continue
            order += 1
            kind = (
                "heading" if _is_heading(text, _paragraph_style(child)) else "paragraph"
            )
            if kind == "heading":
                current_section = text
            blocks.append(
                StructuredBlock(
                    block_id=f"b{order:04d}",
                    type=kind,
                    text=text,
                    section=current_section,
                    order=order,
                )
            )
        elif child.tag == f"{W_NS}tbl":
            rows: list[str] = []
            for row in child.findall(f"{W_NS}tr"):
                cells = [_element_text(cell) for cell in row.findall(f"{W_NS}tc")]
                row_text = " | ".join(cell for cell in cells if cell)
                if row_text:
                    rows.append(row_text)
            if rows:
                order += 1
                blocks.append(
                    StructuredBlock(
                        block_id=f"b{order:04d}",
                        type="table",
                        text="\n".join(rows),
                        section=current_section,
                        order=order,
                        metadata={"rows": len(rows)},
                    )
                )
    logger.info(
        "document.parse.end parser=docx file=%s blocks=%d elapsed_ms=%d",
        path.name,
        len(blocks),
        _elapsed_ms(started_at),
    )
    return blocks


def _blocks_from_mineru_payload(payload: Any) -> list[StructuredBlock]:
    """Convert MinerU content-list objects without compiling them into rules."""
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
        for text_key in (
            "text",
            "content",
            "table_body",
            "html",
            "caption",
            "alt",
            "img_path",
        ):
            value = raw.get(text_key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            # Keep an image as a real source block even when MinerU has no
            # caption; the raw image reference remains in metadata.
            if kind == "image":
                text = "[MinerU image]"
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
        blocks.append(
            StructuredBlock(
                block_id=block_id,
                type=kind,  # type: ignore[arg-type]
                text=text,
                section=str(raw.get("section", section)),
                order=order,
                metadata={
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
            )
        )
    return blocks


class MinerUDocumentParser:
    """Use the existing MinerU ``/tasks`` service, with explicit test fallback.

    ``MINERU_COMMAND`` remains a compatibility adapter for an explicitly
    configured MinerU command.  It is never used as the signal for whether a
    normal business parse is allowed.  When neither the existing service nor
    the command adapter is configured, this class fails unless
    ``allow_docx_fallback`` is explicitly enabled.
    """

    def __init__(
        self,
        command: str | None = None,
        *,
        mineru_url: str | None = None,
        mineru_api_key: str | None = None,
        mineru_backend: str | None = None,
        mineru_server_url: str | None = None,
        timeout_seconds: float = DEFAULT_MINERU_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_MINERU_POLL_INTERVAL_SECONDS,
        allow_docx_fallback: bool = False,
        http_client: httpx.Client | None = None,
    ):
        self.command = (
            command if command is not None else os.getenv("MINERU_COMMAND")
        )
        self.mineru_url = (
            mineru_url
            if mineru_url is not None
            else os.getenv("PDF_TRANS_MINERU_URL")
            or os.getenv("MINERU_API_URL")
        )
        self.mineru_api_key = (
            mineru_api_key
            if mineru_api_key is not None
            else os.getenv("MINERU_API_KEY")
            or os.getenv("PDF_TRANS_MINERU_API_KEY")
        )
        self.mineru_backend = mineru_backend or os.getenv(
            "PDF_TRANS_MINERU_BACKEND",
            os.getenv("MINERU_BACKEND", DEFAULT_MINERU_BACKEND),
        )
        self.mineru_server_url = (
            mineru_server_url
            if mineru_server_url is not None
            else os.getenv("PDF_TRANS_MINERU_SERVER_URL")
            or os.getenv("MINERU_SERVER_URL")
        )
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.allow_docx_fallback = allow_docx_fallback
        self._http_client = http_client
        self.parse_diagnostics: dict[str, Any] = {
            "parser": self.parser_name,
            "mineru_called": False,
            "service_protocol": None,
            "elapsed_ms": None,
        }

    @property
    def parser_name(self) -> str:
        if (self.mineru_url or "").strip() or (self.command or "").strip():
            return "mineru"
        return "docx_fallback" if self.allow_docx_fallback else "mineru"

    @property
    def cache_descriptor(self) -> dict[str, Any]:
        if (self.mineru_url or "").strip():
            transport = "pdf_trans_tasks"
        elif (self.command or "").strip():
            transport = "command"
        else:
            transport = "docx_fallback"
        return {
            "parser": self.parser_name,
            "transport": transport,
            "protocol": MINERU_TASKS_PROTOCOL_VERSION if transport == "pdf_trans_tasks" else None,
            "url": (self.mineru_url or "").strip(),
            "backend": self.mineru_backend,
            "server_url": (self.mineru_server_url or "").strip(),
            "command": (self.command or "").strip(),
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
            elif (self.command or "").strip():
                blocks = self._parse_with_mineru_command(path)
            elif self.allow_docx_fallback:
                blocks = parse_docx_document(path)
                self.parse_diagnostics = {
                    "parser": "docx_fallback",
                    "mineru_called": False,
                    "service_protocol": None,
                    "elapsed_ms": _elapsed_ms(started_at),
                }
            else:
                raise ComplianceExtractionError(
                    "MinerU 未配置：请配置 PDF_TRANS_MINERU_URL（或 MINERU_API_URL）"
                    "，正常业务不允许自动使用 DOCX XML fallback。"
                )
        except ComplianceExtractionError:
            self.parse_diagnostics = {
                "parser": self.parser_name,
                "mineru_called": bool((self.mineru_url or "").strip() or (self.command or "").strip()),
                "service_protocol": (
                    MINERU_TASKS_PROTOCOL_LABEL
                    if (self.mineru_url or "").strip()
                    else "command" if (self.command or "").strip() else None
                ),
                "elapsed_ms": _elapsed_ms(started_at),
            }
            raise
        except Exception as exc:
            self.parse_diagnostics = {
                "parser": self.parser_name,
                "mineru_called": bool((self.mineru_url or "").strip() or (self.command or "").strip()),
                "service_protocol": (
                    MINERU_TASKS_PROTOCOL_LABEL
                    if (self.mineru_url or "").strip()
                    else "command" if (self.command or "").strip() else None
                ),
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

    def _parse_with_mineru_command(self, path: Path) -> list[StructuredBlock]:
        command_text = str(self.command)
        try:
            command = [
                part.format(input=str(path)) for part in shlex.split(command_text)
            ]
            if "{input}" not in command_text:
                command.append(str(path))
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            blocks = _blocks_from_mineru_payload(json.loads(completed.stdout))
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            logger.error(
                "document.parse.dispatch.error parser=mineru transport=command file=%s error_type=%s",
                path.name,
                type(exc).__name__,
            )
            raise ComplianceExtractionError("MinerU 命令解析失败。") from exc
        self.parse_diagnostics.update(
            {
                "parser": "mineru",
                "mineru_called": True,
                "service_protocol": "command",
            }
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
                "hybrid-http-client 模式下必须配置 PDF_TRANS_MINERU_SERVER_URL。"
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
            blocks = _blocks_from_mineru_payload(
                self._content_list_from_zip(result_response.content)
            )
            self.parse_diagnostics.update(
                {
                    "parser": "mineru",
                    "mineru_called": True,
                    "service_protocol": MINERU_TASKS_PROTOCOL_LABEL,
                    "task_id": task_id,
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
                    if normalized.lower().endswith(("_content_list.json", "content_list.json")):
                        members.append(info)
                if len(members) != 1:
                    raise ComplianceExtractionError(
                        "MinerU 结果 ZIP 未返回唯一 content list。"
                    )
                payload = json.loads(archive.read(members[0]))
        except ComplianceExtractionError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ComplianceExtractionError("无法读取 MinerU 结果 ZIP。") from exc
        if not isinstance(payload, list):
            raise ComplianceExtractionError("MinerU content list 必须是 JSON 数组。")
        return [item for item in payload if isinstance(item, dict)]


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


def _normalize_region_title(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text).strip()


def _functional_region_kind(title: str) -> FunctionalRegionKind | None:
    normalized = _normalize_region_title(title)
    if _EXCLUDED_REGION_TITLE_RE.search(normalized):
        return None
    for kind, pattern in _FUNCTIONAL_REGION_PATTERNS:
        if pattern.search(normalized):
            return kind
    return None


def _is_region_title_block(block: StructuredBlock) -> bool:
    if block.type == "heading" or block.text.strip() == block.section.strip():
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
    return len(text) <= 80 and _functional_region_kind(text) is not None


def identify_functional_regions(
    blocks: Iterable[StructuredBlock],
) -> list[FunctionalRegion]:
    """Identify narrow tender-function areas without relying on chapter numbers."""

    ordered_blocks = sorted(blocks, key=lambda item: item.order)
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
        title_kind = (
            _functional_region_kind(block.text)
            if _is_region_title_block(block)
            else None
        )
        is_excluded_title = bool(
            _is_region_title_block(block)
            and _EXCLUDED_REGION_TITLE_RE.search(_normalize_region_title(block.text))
        )
        is_major_boundary = bool(
            _is_region_title_block(block)
            and _MAJOR_SECTION_TITLE_RE.search(block.text.strip())
            and title_kind is None
            and not is_excluded_title
        )

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


_TEMPLATE_ITEM_NAME_RE = re.compile(
    r"封面|投标函|响应函|法定代表人身份证明|身份证明|授权委托书|"
    r"廉洁承诺|关联关系|诉讼仲裁|基本账户|账户信息|业绩情况|业绩表|"
    r"知识产权|安全承诺|资格审查(?:文件|表)|报价表|情况表|声明|承诺函|"
    r"保证金|保函|缴纳|纸质|正本|副本|密封|包封|联合体|授权函|"
    r"特定关系|控股|管理关系|申报表|偏离表|索引表|开源软件|第三方软件|"
    r"元器件|来源清单|一览表|投标产品承诺|增值税专用发票"
)
_TEMPLATE_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:[一二三四五六七八九十百千万0-9]+[、.)．]|\([一二三四五六七八九十百千万0-9]+\))\s*"
)
_TEMPLATE_FORMAT_SUFFIX_RE = re.compile(r"\s*[（(](?:格式|范本|样式)[）)]\s*$")
_TEMPLATE_FIELD_RE = re.compile(
    r"(?<![\w])([^\s：:|,，。；;]{1,20})\s*[：:]\s*(?=_{2,}|[…·.]{2,}|（|\(|\[|$)"
)
_TABLE_FIELD_RE = re.compile(
    r"(?:^|\n|\|)\s*([^|\n：:]{1,20})\s*\|\s*(?=_{2,}|[…·.]{2,}|$)"
)
_ATTACHMENT_RE = re.compile(
    r"(?:附件|须附|应附|附)(?!加|带|近)[：:\s]+(.+?)(?=[。；;\n]|$)"
)


def _template_name(value: str) -> str:
    name = re.sub(r"^\s*\d+(?:\.\d+)+\s*", "", value.strip())
    name = _TEMPLATE_NUMBER_PREFIX_RE.sub("", name)
    name = _TEMPLATE_FORMAT_SUFFIX_RE.sub("", name).strip()
    name = re.sub(r"[★☆*]\s*", "", name, count=1)
    name = re.sub(r"\s*[（(]如有[）)]\s*$", "", name).strip()
    return name.strip(" ：:。；;") or "投标文件模板"


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
    if block.type == "paragraph":
        text = block.text.strip()
        if len(text) > 80 or re.search(r"[。；;，,：:]", text):
            return False
    elif block.type != "heading":
        return False
    name = _template_name(block.text)
    if re.match(r"(?:中国电信在职员工|中国电信员工的近亲属)", name):
        return False
    if _functional_region_kind(name) is not None:
        return False
    return bool(_TEMPLATE_ITEM_NAME_RE.search(name))


def _template_fields(blocks: Sequence[StructuredBlock]) -> list[str]:
    fields: list[str] = []
    for block in blocks:
        for pattern in (_TEMPLATE_FIELD_RE, _TABLE_FIELD_RE):
            for match in pattern.finditer(block.text):
                field = match.group(1).strip(" \t：:|")
                if field and field not in fields:
                    fields.append(field)
    return fields


def _template_attachments(blocks: Sequence[StructuredBlock]) -> list[str]:
    attachments: list[str] = []
    for block in blocks:
        for match in _ATTACHMENT_RE.finditer(block.text):
            attachment = match.group(1).strip(" \t。；;")
            if attachment and attachment not in attachments:
                attachments.append(attachment)
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
        if _is_template_index_region(region):
            continue
        index_block_ids = _template_index_block_ids(region)
        candidate_indexes = [
            index
            for index, block in enumerate(region.blocks)
            if block.block_id not in index_block_ids
            and _is_template_item_title(block, region)
        ]
        item_indexes: list[int] = []
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


def _project_rows(region: FunctionalRegion) -> Iterable[tuple[StructuredBlock, str]]:
    for block in region.blocks[1:]:
        lines = block.text.splitlines() if block.type == "table" else [block.text]
        for line in lines:
            text = line.strip()
            if text:
                yield block, text


def _project_row_label(row: str) -> str:
    columns = [column.strip() for column in row.split("|")]
    if len(columns) >= 2:
        return " | ".join(columns[:2])
    return row


def _project_value(row: str) -> str | None:
    value_part = re.split(r"\||[：:]", row, maxsplit=1)
    candidate = value_part[1].strip() if len(value_part) == 2 else row
    match = _PROJECT_VALUE_RE.search(candidate)
    if match:
        return match.group(0)
    if re.search(r"无需|不允许|允许|只需|仅需|不得|不超过|应当|必须", candidate):
        return candidate
    return candidate if len(value_part) == 2 and candidate else None


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
    boundaries = "。；;，,\n"
    snippets: list[str] = []
    for match in re.finditer(re.escape(keyword), text):
        start = max(
            (text.rfind(mark, 0, match.start()) for mark in boundaries),
            default=-1,
        )
        end_candidates = [text.find(mark, match.end()) for mark in boundaries]
        end_candidates = [index for index in end_candidates if index >= 0]
        end = min(end_candidates, default=len(text))
        snippets.append(text[start + 1 : end].strip())
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
            text = re.sub(r"\s+", " ", block.text).strip()
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
                            "source_text": block.text,
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
    return {
        "section": source_blocks[0].section,
        "block_ids": [block.block_id for block in source_blocks],
        "source_text": "\n".join(block.text for block in source_blocks),
    }


def normalize_tender_extraction_sources(
    result: TenderExtractionResult,
    blocks: Sequence[StructuredBlock],
) -> TenderExtractionResult:
    """Validate and restore source metadata for all three object collections."""

    block_map = {block.block_id: block for block in blocks}
    normalized: TenderExtractionResult = {
        "templates": [],
        "project_requirements": [],
        "supplemental_materials": [],
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


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout_seconds: float = 90,
        max_tokens: int = 8192,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max(256, min(max_tokens, 8192))
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
            "enable_thinking": False,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是招标文件合规要求抽取器。"},
                {"role": "user", "content": prompt},
            ],
        }
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
            if _is_template_index_region(region) or len(region.blocks) == 1:
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
        "applicability_elapsed_ms": None,
        "parsed_blocks": 0,
        "parsed_block_counts": {},
        "parsed_heading_levels": [],
        "parsed_blocks_with_metadata": 0,
        "parsed_metadata_keys": [],
        "functional_regions": 0,
        "template_count": 0,
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
            stats["cache_hit"] = True
            stats["parser_execution_source"] = "result_cache"
            descriptor = getattr(active_parser, "cache_descriptor", None)
            if callable(descriptor):
                descriptor = descriptor()
            if isinstance(descriptor, dict):
                stats["parser_transport"] = descriptor.get("transport")
            stats["template_count"] = len(result["templates"])
            stats["project_requirement_count"] = len(result["project_requirements"])
            stats["supplemental_material_count"] = len(result["supplemental_materials"])
            persist_artifact("02_functional_regions.json", {"source": "result_cache", "regions": []})
            persist_result(result, source="result_cache")
            run_status = "complete"
            record_event(
                "cache.check.end",
                status="hit",
                kind="tender_compliance_objects",
                templates=stats["template_count"],
                project_requirements=stats["project_requirement_count"],
                supplemental_materials=stats["supplemental_material_count"],
            )
            record_event(
                "compliance.extract.end",
                status="complete",
                source="result_cache",
                templates=stats["template_count"],
                project_requirements=stats["project_requirement_count"],
                supplemental_materials=stats["supplemental_material_count"],
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
        }

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

        applicability_started_at = time.perf_counter()
        filtered_templates, applicability_report = apply_project_applicability(
            result["templates"], result["project_requirements"]
        )
        result["templates"] = filtered_templates
        result["supplemental_materials"] = _dedupe_supplemental_materials(
            result["supplemental_materials"], result["templates"]
        )
        stats["applicability_elapsed_ms"] = _elapsed_ms(applicability_started_at)
        normalized = normalize_tender_extraction_sources(result, blocks)
        stats["filtered_objects"] = len(applicability_report)
        stats["filtered_by_reason"] = _filter_reason_counts(applicability_report)
        stats["template_count"] = len(normalized["templates"])
        stats["project_requirement_count"] = len(normalized["project_requirements"])
        stats["supplemental_material_count"] = len(normalized["supplemental_materials"])
        persist_result(normalized)
        persist_artifact("06_filter_report.json", applicability_report)
        if cache is not None:
            cache.set(cache_key, normalized)
        record_event(
            "compliance.extract.end",
            status="complete",
            templates=stats["template_count"],
            project_requirements=stats["project_requirement_count"],
            supplemental_materials=stats["supplemental_material_count"],
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
