from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ComplianceRequirement = dict[str, Any]
logger = logging.getLogger(__name__)
REQUIREMENT_CACHE_VERSION = "compliance-v2-enums-boundary"


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _serialize_blocks(blocks: Sequence[StructuredBlock]) -> list[dict[str, Any]]:
    return [asdict(block) for block in blocks]


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


class ComplianceExtractionError(RuntimeError):
    """Raised when a tender cannot produce source-grounded requirements."""


class DocumentParser(Protocol):
    def parse(self, path: Path) -> list[StructuredBlock]: ...


class RequirementLLM(Protocol):
    def extract(self, batch: Sequence[CandidateWindow]) -> list[dict[str, Any]]: ...


class RequirementCache(Protocol):
    def get(self, key: str) -> list[dict[str, Any]] | None: ...

    def set(self, key: str, value: list[dict[str, Any]]) -> None: ...


class InMemoryRequirementCache:
    def __init__(self):
        self._values: dict[str, list[dict[str, Any]]] = {}

    def get(self, key: str) -> list[dict[str, Any]] | None:
        logger.info("cache.read.start backend=in_memory key=%s", key[:12])
        value = self._values.get(key)
        logger.info(
            "cache.read.end backend=in_memory key=%s status=%s",
            key[:12],
            "hit" if value is not None else "miss",
        )
        return deepcopy(value) if value is not None else None

    def set(self, key: str, value: list[dict[str, Any]]) -> None:
        logger.info(
            "cache.write.start backend=in_memory key=%s requirements=%d",
            key[:12],
            len(value),
        )
        self._values[key] = deepcopy(value)
        logger.info(
            "cache.write.end backend=in_memory key=%s requirements=%d",
            key[:12],
            len(value),
        )


class JsonRequirementCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> list[dict[str, Any]] | None:
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
        value = deepcopy(payload) if isinstance(payload, list) else None
        logger.info(
            "cache.read.end backend=json key=%s status=%s requirements=%d elapsed_ms=%d",
            key[:12],
            "hit" if value is not None else "miss",
            len(value) if value is not None else 0,
            _elapsed_ms(started_at),
        )
        return value

    def set(self, key: str, value: list[dict[str, Any]]) -> None:
        started_at = time.perf_counter()
        logger.info(
            "cache.write.start backend=json key=%s requirements=%d",
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
            "cache.write.end backend=json key=%s requirements=%d elapsed_ms=%d",
            key[:12],
            len(value),
            _elapsed_ms(started_at),
        )


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

    This is the local structural fallback used when a MinerU command is not
    configured.  It deliberately does not infer compliance semantics.
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
    if isinstance(payload, dict):
        payload = payload.get("blocks", payload.get("content", payload.get("items")))
    if not isinstance(payload, list):
        raise ComplianceExtractionError("MinerU 返回结果不是结构化内容列表。")

    blocks: list[StructuredBlock] = []
    section = ""
    for index, raw in enumerate(payload, start=1):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", raw.get("content", ""))).strip()
        if not text:
            continue
        kind = str(raw.get("type", raw.get("block_type", "paragraph"))).lower()
        if kind in {"title", "heading", "header"}:
            kind = "heading"
            section = text
        elif kind not in {"paragraph", "table", "image"}:
            kind = "paragraph"
        block_id = str(raw.get("block_id", raw.get("id", f"b{index:04d}")))
        blocks.append(
            StructuredBlock(
                block_id=block_id,
                type=kind,  # type: ignore[arg-type]
                text=text,
                section=str(raw.get("section", section)),
                order=int(raw.get("order", index)),
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
    """MinerU-compatible parser with a command adapter and local DOCX fallback."""

    def __init__(self, command: str | None = None):
        self.command = command if command is not None else os.getenv("MINERU_COMMAND")

    def parse(self, path: Path) -> list[StructuredBlock]:
        started_at = time.perf_counter()
        parser_name = "mineru" if self.command else "docx"
        logger.info(
            "document.parse.dispatch.start parser=%s file=%s", parser_name, path.name
        )
        if not self.command:
            blocks = parse_docx_document(path)
            logger.info(
                "document.parse.dispatch.end parser=%s file=%s blocks=%d elapsed_ms=%d",
                parser_name,
                path.name,
                len(blocks),
                _elapsed_ms(started_at),
            )
            return blocks
        command = [part.format(input=str(path)) for part in shlex.split(self.command)]
        if "{input}" not in self.command:
            command.append(str(path))
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            blocks = _blocks_from_mineru_payload(json.loads(completed.stdout))
            logger.info(
                "document.parse.dispatch.end parser=%s file=%s blocks=%d elapsed_ms=%d",
                parser_name,
                path.name,
                len(blocks),
                _elapsed_ms(started_at),
            )
            return blocks
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            logger.error(
                "document.parse.dispatch.error parser=%s file=%s error_type=%s elapsed_ms=%d",
                parser_name,
                path.name,
                type(exc).__name__,
                _elapsed_ms(started_at),
            )
            raise ComplianceExtractionError("MinerU 文档解析失败。") from exc


_EXCLUDED_RE = re.compile(
    r"评分|得分|分值|评标|评审因素|商务评分|技术评分|价格评分|报价评分|综合评分"
)
_NOISE_RE = re.compile(r"PAGEREF|_Toc|HYPERLINK|目录")
_COMPLIANCE_RE = re.compile(
    r"填写|提供|附[：:]|必须|应当|须|不得|签字|签章|盖章|公章|日期|年[　 ]?月|身份证|营业执照|社保|资格证|证书|合同证明|证明材料|复印件|扫描件|业绩|人员名单|人员信息|人员姓名|联系方式|招标编号|项目名称|投标人名称|姓名|委托代理|法定代表|文件大小|附件大小|文件容量|大附件|清晰|可读|上传|加密|CA|电子投标|文件份数|组成|附件|对应|关联|每项|逐一"
)
_PLACEHOLDER_RE = re.compile(
    r"_{2,}|[…·.]{2,}|【[^】]{1,40}】|\[[^\]]{0,40}\]|（(?:投标人|项目|公司|日期|盖章|签字)[^）]{0,40}）"
)


def _is_candidate_block(block: StructuredBlock) -> bool:
    if (
        not block.text
        or _EXCLUDED_RE.search(block.text)
        or _NOISE_RE.search(block.text)
    ):
        return False
    return bool(_COMPLIANCE_RE.search(block.text) or _PLACEHOLDER_RE.search(block.text))


def select_compliance_candidates(
    blocks: Iterable[StructuredBlock],
) -> list[CandidateWindow]:
    started_at = time.perf_counter()
    logger.info("candidate.filter.start")
    selected = [
        block
        for block in sorted(blocks, key=lambda item: item.order)
        if _is_candidate_block(block)
    ]
    if not selected:
        logger.info(
            "candidate.filter.end selected_blocks=0 windows=0 elapsed_ms=%d",
            _elapsed_ms(started_at),
        )
        return []

    windows: list[CandidateWindow] = []
    current: list[StructuredBlock] = []

    def flush() -> None:
        if not current:
            return
        windows.append(
            CandidateWindow(
                block_ids=[block.block_id for block in current],
                section=current[0].section,
                text="\n".join(block.text for block in current),
                order=current[0].order,
            )
        )

    previous: StructuredBlock | None = None
    for block in selected:
        contiguous = (
            previous is not None
            and block.section == previous.section
            and block.order <= previous.order + 2
        )
        if current and not contiguous:
            flush()
            current = []
        current.append(block)
        previous = block
    flush()
    logger.info(
        "candidate.filter.end selected_blocks=%d windows=%d elapsed_ms=%d",
        len(selected),
        len(windows),
        _elapsed_ms(started_at),
    )
    return windows


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
    if not candidates:
        logger.info("batch.build.end batches=0 elapsed_ms=%d", _elapsed_ms(started_at))
        return []
    count = min(max_batches, len(candidates))
    batches: list[list[CandidateWindow]] = [[] for _ in range(count)]
    # Evenly distribute windows first so large documents never create dozens
    # of calls.  Prompt serialization applies the character cap per batch.
    for index, candidate in enumerate(candidates):
        bucket = min(index * count // len(candidates), count - 1)
        batches[bucket].append(candidate)
    # The serialized prompt is capped by ``OpenAICompatibleLLM``.  Keep every
    # candidate in the bounded batch list instead of splitting into extra model
    # calls when a window is large.
    del max_batch_chars
    result = [batch for batch in batches if batch]
    logger.info(
        "batch.build.end batches=%d candidate_count=%d elapsed_ms=%d",
        len(result),
        sum(len(batch) for batch in result),
        _elapsed_ms(started_at),
    )
    return result


CHECK_TYPES = (
    "required_field",
    "placeholder",
    "attachment_exists",
    "attachment_content",
    "signature",
    "seal",
    "date",
    "consistency",
    "file_metadata",
)
TARGET_SCOPES = (
    "single_section",
    "each_section",
    "each_person",
    "each_contract",
    "whole_document",
)
APPLICABILITY_TYPES = ("always", "conditional")
EVIDENCE_TYPES = ("text", "structure", "vision", "metadata")
EVIDENCE_BY_CHECK_TYPE = {
    "required_field": "text",
    "placeholder": "text",
    "attachment_exists": "structure",
    "attachment_content": "vision",
    "signature": "vision",
    "seal": "vision",
    "date": "text",
    "consistency": "structure",
    "file_metadata": "metadata",
}


class _CheckModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    requirement: str = Field(min_length=1)
    check_type: Literal[
        "required_field",
        "placeholder",
        "attachment_exists",
        "attachment_content",
        "signature",
        "seal",
        "date",
        "consistency",
        "file_metadata",
    ]
    evidence_type: Literal["text", "structure", "vision", "metadata"]


class _TargetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    scope: Literal[
        "single_section",
        "each_section",
        "each_person",
        "each_contract",
        "whole_document",
    ]


class _ApplicabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["always", "conditional"]
    condition: str | None = None


class _RawRequirementModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    target: _TargetModel
    checks: list[_CheckModel] = Field(min_length=1)
    applicability: _ApplicabilityModel
    source_block_ids: list[str] = Field(min_length=1)


def _legacy_check_type(requirement: str) -> tuple[str, str]:
    if _PLACEHOLDER_RE.search(requirement):
        return "placeholder", EVIDENCE_BY_CHECK_TYPE["placeholder"]
    if re.search(r"签字", requirement):
        return "signature", EVIDENCE_BY_CHECK_TYPE["signature"]
    if re.search(r"签章|盖章|公章", requirement):
        return "seal", EVIDENCE_BY_CHECK_TYPE["seal"]
    if re.search(r"人像面|国徽面|正面|反面|关键页|内容完整|扫描件内容", requirement):
        return "attachment_content", EVIDENCE_BY_CHECK_TYPE["attachment_content"]
    if re.search(
        r"附件|提供|身份证|营业执照|社保|证书|合同|证明材料|复印件|扫描件",
        requirement,
    ):
        return "attachment_exists", EVIDENCE_BY_CHECK_TYPE["attachment_exists"]
    if re.search(r"日期|年.{0,8}月", requirement):
        return "date", EVIDENCE_BY_CHECK_TYPE["date"]
    return "required_field", EVIDENCE_BY_CHECK_TYPE["required_field"]


_CHECK_TYPE_ALIASES = {
    # Presence / completeness labels used by previous model prompts.
    "presence": "attachment_exists",
    "存在性检查": "attachment_exists",
    "附件存在性检查": "attachment_exists",
    "attachment_presence": "attachment_exists",
    "attachment_check": "attachment_exists",
    "document_presence": "attachment_exists",
    "document_exists": "attachment_exists",
    "文件存在性检查": "attachment_exists",
    "主体资格检查": "attachment_exists",
    "数量/主体检查": "attachment_exists",
    "list_presence": "attachment_exists",
    "存在": "attachment_exists",
    # Content / image labels.
    "content_check": "attachment_content",
    "content_presence": "attachment_content",
    "content_integrity": "attachment_content",
    "内容完整性检查": "attachment_content",
    "内容合规性检查": "attachment_content",
    "内容检查": "attachment_content",
    "document_content": "attachment_content",
    "文本语义检查": "attachment_content",
    "内容完整": "attachment_content",
    # Placeholder and fixed-value labels.
    "placeholder_removal": "placeholder",
    "占位符/特定值检查": "placeholder",
    "specific_value": "placeholder",
    "specific_value_or_empty": "placeholder",
    "conditional_presence": "placeholder",
    "默认状态检查": "placeholder",
    "空值判断": "placeholder",
    # Signature / seal labels.
    "signature_field": "signature",
    "signature_check": "signature",
    "conditional_signature": "signature",
    "替代签署检查": "signature",
    "签章完整性检查": "signature",
    "签字盖章检查": "signature",
    "seal_check": "seal",
    "印章识别": "seal",
    # Date labels.
    "date_check": "date",
    "日期检查": "date",
    # Cross-reference / consistency labels.
    "content_consistency": "consistency",
    "cross_reference": "consistency",
    "附件关联检查": "consistency",
    "一致性检查": "consistency",
    "内容一致性检查": "consistency",
    "逻辑一致性检查": "consistency",
    "顺序一致性检查": "consistency",
    "逻辑顺序检查": "consistency",
    "数据排序验证": "consistency",
    "索引映射检查": "consistency",
    "order_check": "consistency",
    # Direct document/file metadata labels.
    "file_upload": "file_metadata",
    "流程合规检查": "file_metadata",
    "文件类型检查": "file_metadata",
    "document_format": "file_metadata",
    "document_count": "file_metadata",
    "electronic_file": "file_metadata",
    "文件计数/主体识别": "file_metadata",
    # General field / format labels.
    "completeness": "required_field",
    "完整性检查": "required_field",
    "信息完整性检查": "required_field",
    "required": "required_field",
    "required_field_check": "required_field",
    "format_check": "required_field",
    "format_compliance": "required_field",
    "format_compliance_check": "required_field",
    "格式合规检查": "required_field",
    "格式合规性检查": "required_field",
    "格式标识检查": "required_field",
    "禁止性检查": "required_field",
    "modification_prohibition": "required_field",
    "条件适用性检查": "required_field",
    "有效性检查": "required_field",
    "主体检查": "required_field",
    "count_check": "required_field",
    "数量合规性检查": "required_field",
    "data_accuracy": "required_field",
    "document_text": "required_field",
    "text_field": "required_field",
    "form_fields": "required_field",
}


def _canonicalize_check_type(value: Any, requirement: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComplianceExtractionError("LLM Schema 校验失败：check_type 不能为空。")
    raw = value.strip()
    if raw in CHECK_TYPES:
        # Even a canonical model label can be too coarse for an attachment
        # whose sides/pages must be inspected (for example an ID card's
        # portrait and national-emblem sides).
        if raw == "attachment_exists" and re.search(
            r"人像面|国徽面|正面|反面|关键页|扫描件内容|附件内容", requirement
        ):
            canonical = "attachment_content"
        else:
            canonical = raw
    else:
        # Generic labels such as ``presence`` and ``content_check`` need the
        # requirement text to disambiguate fields, attachments, signatures,
        # and dates before consulting the legacy alias table.
        if raw in {
            "presence",
            "存在性检查",
            "completeness",
            "完整性检查",
            "content_check",
            "content_presence",
            "内容完整性检查",
        } and re.search(r"日期|年.{0,8}月", requirement):
            canonical = "date"
        elif raw in {
            "presence",
            "存在性检查",
            "completeness",
            "完整性检查",
            "content_check",
            "content_presence",
            "内容完整性检查",
        } and re.search(r"一一对应|对应|关联|顺序|映射|相互解释|一致", requirement):
            canonical = "consistency"
        elif re.search(r"人像面|国徽面|正面|反面|关键页|扫描件内容|附件内容", requirement):
            canonical = "attachment_content"
        elif re.search(r"签字|签名|签署", requirement) and raw in {
            "presence",
            "存在性检查",
            "content_check",
            "content_presence",
        }:
            canonical = "signature"
        elif re.search(r"盖章|公章|印章", requirement) and raw in {
            "presence",
            "存在性检查",
            "content_check",
            "content_presence",
        }:
            canonical = "seal"
        elif raw in {"presence", "存在性检查", "content_check", "content_presence"} and not re.search(
            r"附件|提供|身份证|营业执照|社保|证书|合同|证明材料|复印件|扫描件|保函",
            requirement,
        ):
            canonical = "required_field"
        else:
            canonical = _CHECK_TYPE_ALIASES.get(raw)
            if canonical is None:
                lowered = raw.lower()
                canonical = _CHECK_TYPE_ALIASES.get(lowered)
        if canonical is None:
            # A small, deterministic vocabulary fallback handles new labels
            # while still rejecting arbitrary model-generated enum strings.
            if re.search(r"人像面|国徽面|正面|反面|关键页|扫描件内容|附件内容", requirement):
                canonical = "attachment_content"
            elif re.search(r"签字|签名|签署", requirement):
                canonical = "signature"
            elif re.search(r"盖章|公章|印章", requirement):
                canonical = "seal"
            elif re.search(r"占位|不涉及|无偏离|不得留空|留空|特定值", requirement):
                canonical = "placeholder"
            elif re.search(r"日期|年.{0,8}月", requirement):
                canonical = "date"
            elif re.search(r"一一对应|对应|关联|顺序|映射|相互解释|一致", requirement):
                canonical = "consistency"
            elif re.search(r"文件大小|文件容量|文件份数|电子版|上传至大附件|文件格式|可编辑", requirement):
                canonical = "file_metadata"
            elif re.search(r"附件|提供|身份证|营业执照|证书|合同关键页|证明材料|复印件|扫描件", requirement):
                canonical = "attachment_exists"
            else:
                raise ComplianceExtractionError(
                    f"LLM Schema 校验失败：不支持的 check_type：{raw}。"
                )
    return canonical


def _canonicalize_scope(value: Any, target_name: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComplianceExtractionError("LLM Schema 校验失败：target.scope 不能为空。")
    raw = value.strip()
    if raw in TARGET_SCOPES:
        return raw
    if raw in {"各章节", "各部分", "每个章节", "每个部分", "各标段"}:
        return "each_section"
    if re.search(r"每个(?:人|人员)|各(?:人员|人)", raw):
        return "each_person"
    if re.search(r"每份合同|各合同|每个合同|合同文件及附件", raw):
        return "each_contract"
    if re.search(r"所有递交|整个|整体|全文|所有版本|全部文件", raw):
        return "whole_document"
    if raw in {"投标文件", "投标文件整体", "投标文件及往来函电"}:
        return "whole_document"
    if re.search(r"投标文件|商务|技术标|技术投标|报价文件|附件|章节|封面|资格|委托|联合体|代理商|投标人|申报表|承诺函|保函|业绩|订单模板|系统|备选|项目", raw):
        return "single_section"
    if "合同" in raw:
        return "each_contract"
    if "人员" in raw:
        return "each_person"
    raise ComplianceExtractionError(
        f"LLM Schema 校验失败：不支持的 target.scope：{raw}。"
    )


def _canonicalize_applicability(value: Any, condition: Any = None) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value.strip():
        if condition:
            return "conditional", str(condition).strip()
        raise ComplianceExtractionError(
            "LLM Schema 校验失败：applicability.type 不能为空。"
        )
    raw = value.strip()
    if raw in APPLICABILITY_TYPES:
        canonical = raw
    elif raw in {"mandatory", "universal", "通用适用", "所有投标人", "全部投标人"}:
        canonical = "always"
    elif raw in {"条件适用", "conditional", "条件适用性"}:
        canonical = "conditional"
    elif re.search(r"所有|全部|通用|无条件", raw):
        canonical = "always"
    elif re.search(r"条件|仅当|如果|若|如|涉及|适用于|代理商|联合体", raw):
        canonical = "conditional"
    else:
        raise ComplianceExtractionError(
            f"LLM Schema 校验失败：不支持的 applicability.type：{raw}。"
        )
    condition_text = str(condition).strip() if condition is not None else None
    if canonical == "always" and condition_text in {
        "所有投标人",
        "全部投标人",
        "通用适用",
        "universal",
        "mandatory",
    }:
        condition_text = None
    if canonical == "conditional" and not condition_text:
        condition_text = raw if raw not in APPLICABILITY_TYPES else "按招标文件条件"
    return canonical, condition_text


def _canonicalize_category(value: Any, checks: Sequence[dict[str, Any]]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComplianceExtractionError("LLM Schema 校验失败：category 不能为空。")
    raw = value.strip()
    forbidden_category_aliases = {
        "存在性检查",
        "附件存在性检查",
        "完整性检查",
        "内容完整性检查",
        "内容合规性检查",
        "签字盖章检查",
        "签章完整性检查",
        "格式合规检查",
        "格式合规性检查",
        "一致性检查",
        "附件关联检查",
    }
    if raw not in forbidden_category_aliases:
        return raw
    check_type = checks[0]["check_type"] if checks else "required_field"
    if check_type in {"attachment_exists", "attachment_content"}:
        return "attachment"
    if check_type in {"signature", "seal"}:
        return "signature"
    return check_type


def _coerce_legacy_shape(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = dict(raw)
    coerced_fields: list[str] = []

    target = normalized.get("target")
    if isinstance(target, str):
        normalized["target"] = {
            "name": target.strip(),
            "scope": "single_section",
        }
        coerced_fields.append("target")
    elif isinstance(target, dict):
        target_copy = dict(target)
        if not target_copy.get("scope"):
            target_copy["scope"] = "single_section"
            coerced_fields.append("target.scope")
        target_copy["scope"] = _canonicalize_scope(
            target_copy["scope"], str(target_copy.get("name", ""))
        )
        normalized["target"] = target_copy

    checks = normalized.get("checks")
    if isinstance(checks, list):
        normalized_checks: list[Any] = []
        for check in checks:
            if isinstance(check, str):
                requirement = check.strip()
                check_type, evidence_type = _legacy_check_type(requirement)
                normalized_checks.append(
                    {
                        "requirement": requirement,
                        "check_type": check_type,
                        "evidence_type": evidence_type,
                    }
                )
                coerced_fields.append("checks[]")
                continue
            if isinstance(check, dict):
                check_copy = dict(check)
                requirement = check_copy.get("requirement")
                if isinstance(requirement, str):
                    inferred_type, _ = _legacy_check_type(requirement)
                    supplied_type = check_copy.get("check_type") or inferred_type
                    check_type = _canonicalize_check_type(
                        supplied_type, requirement
                    )
                    if check_copy.get("check_type") != check_type:
                        coerced_fields.append("checks[].check_type")
                    check_copy["check_type"] = check_type
                    evidence_type = EVIDENCE_BY_CHECK_TYPE[check_type]
                    if check_copy.get("evidence_type") != evidence_type:
                        coerced_fields.append("checks[].evidence_type")
                    # Evidence is a program-owned field.  Never trust a free
                    # model label here, even when it happens to validate as a
                    # string.
                    check_copy["evidence_type"] = evidence_type
                normalized_checks.append(check_copy)
                continue
            normalized_checks.append(check)
        normalized["checks"] = normalized_checks

    applicability = normalized.get("applicability")
    if isinstance(applicability, str):
        condition = applicability.strip()
        applicability_type, condition = _canonicalize_applicability(
            condition, condition
        )
        normalized["applicability"] = {
            "type": applicability_type,
            "condition": condition,
        }
        coerced_fields.append("applicability")
    elif isinstance(applicability, dict):
        applicability_copy = dict(applicability)
        supplied_type = applicability_copy.get("type")
        if not supplied_type and applicability_copy.get("condition"):
            supplied_type = "conditional"
            coerced_fields.append("applicability.type")
        applicability_type, condition = _canonicalize_applicability(
            supplied_type, applicability_copy.get("condition")
        )
        if applicability_copy.get("type") != applicability_type:
            coerced_fields.append("applicability.type")
        applicability_copy["type"] = applicability_type
        applicability_copy["condition"] = condition
        normalized["applicability"] = applicability_copy

    checks_for_category = normalized.get("checks")
    if isinstance(checks_for_category, list):
        normalized["category"] = _canonicalize_category(
            normalized.get("category"),
            [check for check in checks_for_category if isinstance(check, dict)],
        )

    return normalized, coerced_fields


def _coerce_raw_requirement(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ComplianceExtractionError("LLM Schema 校验失败：要求项不是对象。")
    if "source_block_ids" not in raw and isinstance(raw.get("source"), dict):
        raw = {
            **{key: value for key, value in raw.items() if key != "source"},
            "source_block_ids": raw["source"].get("block_ids", []),
        }
    raw, coerced_fields = _coerce_legacy_shape(raw)
    if coerced_fields:
        logger.warning(
            "requirements.normalize.coerce fields=%s",
            ",".join(sorted(set(coerced_fields))),
        )
    try:
        model = _RawRequirementModel.model_validate(raw)
    except ValidationError as exc:
        raise ComplianceExtractionError(f"LLM Schema 校验失败：{exc}") from exc
    return model.model_dump()


_EXTERNAL_SYSTEM_RE = re.compile(
    r"电子采购系统|电子招标投标系统|电子招标系统|交易平台|采购平台|系统上传|平台上传|系统提交|平台提交|"
    r"加密电子投标文件|电子投标文件加密|上传至大附件|上传至平台|完成上传|上传投标文件|"
    r"CA证书|CA锁|证书在使用时有效|加密提交"
)
_FUTURE_PERFORMANCE_RE = re.compile(
    r"合同签订后|签订后的|履约期间|履约阶段|履约过程中|履约期|未来合同|合同订单模板|合同正文|"
    r"正式提交的合同文件|甲乙双方信息|安全危险因素告知书|安全保密相关协议"
)
_CURRENT_BID_RE = re.compile(
    r"投标文件|投标阶段|投标时|随投标|作为投标文件|当前投标|递交投标|投标人应"
)
_JOINT_BID_RE = re.compile(r"联合体协议|联合体各方|联合体牵头|联合体成员|联合体投标")
_ALTERNATIVE_BID_RE = re.compile(r"备选投标方案|备选方案")


def _project_constraints(blocks: Sequence[StructuredBlock]) -> dict[str, bool]:
    document_text = "\n".join(block.text for block in blocks)
    return {
        "joint_disallowed": bool(
            re.search(r"(?:不接受|不允许|不得采用|禁止).*联合体|联合体.*(?:不接受|不允许|不得采用|禁止)", document_text)
        ),
        "alternative_disallowed": bool(
            re.search(r"(?:不接受|不允许|不得提交|禁止).*备选|备选.*(?:不接受|不允许|不得提交|禁止)", document_text)
        ),
    }


def _filter_requirement_reason(
    item: dict[str, Any],
    *,
    constraints: dict[str, bool],
) -> str | None:
    checks_text = " ".join(check["requirement"] for check in item["checks"])
    target_text = item["target"]["name"]
    searchable = " ".join(
        [item["name"], target_text, checks_text, item["applicability"].get("condition") or ""]
    )
    if _EXTERNAL_SYSTEM_RE.search(searchable):
        return "external_system_state"
    if (
        _FUTURE_PERFORMANCE_RE.search(searchable)
        and not _CURRENT_BID_RE.search(searchable)
    ):
        return "future_contract_or_performance"
    if (
        re.search(r"订单模板|合同条款、附件一", searchable)
        and not re.search(r"投标文件中|作为投标文件|随投标文件", searchable)
    ):
        return "future_contract_or_performance"
    if (
        re.search(r"合同条款[-—：: ]*双方信息|合同主体|甲乙双方|合同正文|安全保密相关协议", searchable)
        and not re.search(r"投标文件中|作为投标文件|随投标文件", searchable)
    ):
        return "future_contract_or_performance"
    if constraints.get("joint_disallowed") and _JOINT_BID_RE.search(searchable):
        return "project_disallowed_joint_bid"
    if constraints.get("alternative_disallowed") and _ALTERNATIVE_BID_RE.search(searchable):
        return "project_disallowed_alternative_bid"
    return None


def _filter_reason_counts(report: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in report:
        reason = str(item.get("reason", "unknown"))
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _normalize_requirements(
    raw_requirements: Iterable[Any],
    blocks: Sequence[StructuredBlock],
    *,
    filter_report: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    started_at = time.perf_counter()
    raw_items = list(raw_requirements)
    logger.info(
        "requirements.normalize.start raw_requirements=%d source_blocks=%d",
        len(raw_items),
        len(blocks),
    )
    block_map = {block.block_id: block for block in blocks}
    constraints = _project_constraints(blocks)
    grouped: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for raw in raw_items:
        item = _coerce_raw_requirement(raw)
        searchable_text = " ".join(
            [item["name"], *(check["requirement"] for check in item["checks"])]
        )
        if _EXCLUDED_RE.search(searchable_text):
            continue
        filter_reason = _filter_requirement_reason(
            item,
            constraints=constraints,
        )
        if filter_reason is not None:
            if filter_report is not None:
                filter_report.append(
                    {
                        "name": item["name"],
                        "target_name": item["target"]["name"],
                        "check_types": [
                            check["check_type"] for check in item["checks"]
                        ],
                        "source_block_ids": list(item["source_block_ids"]),
                        "reason": filter_reason,
                    }
                )
            logger.info(
                "requirements.normalize.filter name=%s reason=%s",
                item["name"],
                filter_reason,
            )
            continue
        source_ids = list(dict.fromkeys(item["source_block_ids"]))
        if any(block_id not in block_map for block_id in source_ids):
            raise ComplianceExtractionError(
                "LLM Schema 校验失败：来源 block_id 不存在。"
            )
        source_blocks = sorted(
            (block_map[block_id] for block_id in source_ids),
            key=lambda block: block.order,
        )
        check_key = tuple(sorted(check["requirement"] for check in item["checks"]))
        key = (item["name"].strip(), item["target"]["name"].strip(), check_key)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                **item,
                "source_block_ids": [block.block_id for block in source_blocks],
            }
        else:
            existing["source_block_ids"] = list(
                dict.fromkeys(
                    existing["source_block_ids"]
                    + [block.block_id for block in source_blocks]
                )
            )

    normalized: list[dict[str, Any]] = []
    for item in sorted(
        grouped.values(),
        key=lambda value: min(
            block_map[block_id].order for block_id in value["source_block_ids"]
        ),
    ):
        source_blocks = sorted(
            (block_map[block_id] for block_id in item["source_block_ids"]),
            key=lambda block: block.order,
        )
        requirement_id = f"compliance_{len(normalized) + 1:03d}"
        checks = [
            {
                "id": f"{requirement_id}_{index:02d}",
                "requirement": check["requirement"].strip(),
                "check_type": check["check_type"].strip(),
                "evidence_type": check["evidence_type"].strip(),
            }
            for index, check in enumerate(item["checks"], start=1)
        ]
        normalized.append(
            {
                "id": requirement_id,
                "name": item["name"].strip(),
                "category": item["category"].strip(),
                "target": item["target"],
                "checks": checks,
                "applicability": item["applicability"],
                "source": {
                    "section": source_blocks[0].section,
                    "block_ids": [block.block_id for block in source_blocks],
                    "source_text": "\n".join(block.text for block in source_blocks),
                },
            }
        )
    logger.info(
        "requirements.normalize.end requirements=%d elapsed_ms=%d",
        len(normalized),
        _elapsed_ms(started_at),
    )
    return normalized


def _is_transient_extraction_error(error: ComplianceExtractionError) -> bool:
    cause = error.__cause__
    return isinstance(
        cause,
        (TimeoutError, ConnectionError, OSError, urllib.error.URLError),
    )


class DeterministicComplianceLLM:
    """Local fallback that extracts structured checks directly from candidates.

    It is source-grounded and deterministic, so development can run without a
    model credential; production can select ``OpenAICompatibleLLM`` instead.
    """

    def extract(self, batch: Sequence[CandidateWindow]) -> list[dict[str, Any]]:
        started_at = time.perf_counter()
        logger.info(
            "llm.call.start provider=deterministic model=local batch_size=%d candidate_chars=%d",
            len(batch),
            sum(len(candidate.text) for candidate in batch),
        )
        result: list[dict[str, Any]] = []
        for candidate in batch:
            text = candidate.text
            checks: list[dict[str, str]] = []
            category = "required_field"
            if _PLACEHOLDER_RE.search(text):
                category = "placeholder"
                checks.append(
                    {
                        "requirement": "模板中的待填写占位内容应完成替换。",
                        "check_type": "placeholder",
                        "evidence_type": "text",
                    }
                )
            if re.search(r"签字|签章|盖章|公章", text):
                category = "signature"
                if "签字" in text:
                    checks.append(
                        {
                            "requirement": "招标文件要求的签字位置应按要求处理。",
                            "check_type": "signature",
                            "evidence_type": "text",
                        }
                    )
                if re.search(r"签章|盖章|公章", text):
                    checks.append(
                        {
                            "requirement": "招标文件要求的盖章或签章位置应按要求处理。",
                            "check_type": "seal",
                            "evidence_type": "text",
                        }
                    )
            if re.search(
                r"附件|提供|身份证|营业执照|社保|证书|合同|证明材料|复印件|扫描件", text
            ):
                category = "attachment"
                checks.append(
                    {
                        "requirement": "招标文件要求的附件或证明材料应随投标文件提供。",
                        "check_type": "attachment_exists",
                        "evidence_type": "structure",
                    }
                )
            if re.search(r"日期|年.{0,8}月", text):
                checks.append(
                    {
                        "requirement": "要求填写的日期应完整。",
                        "check_type": "date",
                        "evidence_type": "text",
                    }
                )
            if not checks:
                checks.append(
                    {
                        "requirement": text,
                        "check_type": "required_field",
                        "evidence_type": "text",
                    }
                )
            result.append(
                {
                    "name": f"{candidate.section or '投标文件'}完整性",
                    "category": category,
                    "target": {
                        "name": candidate.section or "投标文件",
                        "scope": "single_section",
                    },
                    "checks": checks,
                    "applicability": {"type": "always", "condition": None},
                    "source_block_ids": candidate.block_ids,
                }
            )
        logger.info(
            "llm.call.end provider=deterministic model=local batch_size=%d requirements=%d elapsed_ms=%d",
            len(batch),
            len(result),
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
        max_tokens: int = 4096,
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

    def extract(self, batch: Sequence[CandidateWindow]) -> list[dict[str, Any]]:
        started_at = time.perf_counter()
        logger.info(
            "llm.call.start provider=openai_compatible model=%s batch_size=%d candidate_chars=%d",
            self.model,
            len(batch),
            sum(len(candidate.text) for candidate in batch),
        )
        source = "\n\n".join(
            f"[{candidate.section}] block_ids={','.join(candidate.block_ids)}\n{candidate.text[:6000]}"
            for candidate in batch
        )[:12000]
        prompt = (
            "从以下招标文件候选内容中提取投标文件本身可直接检查的合规要求。只返回 JSON 对象 "
            '{"requirements":[...]}。每项必须包含 name、category、target、checks、'
            "applicability、source_block_ids。target 必须是对象，包含 name 和 scope；"
            "scope 只能是 single_section、each_section、each_person、each_contract、whole_document；"
            "checks 每项必须是对象，包含 requirement、check_type、evidence_type；"
            "check_type 只能是 required_field、placeholder、attachment_exists、attachment_content、"
            "signature、seal、date、consistency、file_metadata；"
            "evidence_type 不要自由发挥，将由程序按 check_type 确定；"
            "applicability 必须是对象，type 只能是 always 或 conditional，condition 可为 null；"
            "source_block_ids 必须且只能复制输入候选中的真实 block_ids，不得编造 source_text。"
            "只提取用户已生成的投标文件可检查的填写完整、材料/附件齐全、模板占位符替换、日期、签字、"
            "盖章、合同关键页、表格与证明材料对应关系、开户证明、文件组成和文件元数据要求。"
            "排除评分/评标规则、电子采购系统上传或加密提交、CA证书有效性等外部系统状态；"
            "排除合同签订后或履约阶段要求，除非原文明确要求这些材料作为当前投标文件一并填写签署并提交；"
            "结合原文项目专用条款，若项目明确不接受联合体或不允许备选方案，不要生成对应编制要求。\n\n"
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
            requirements = (
                decoded.get("requirements") if isinstance(decoded, dict) else decoded
            )
            if not isinstance(requirements, list):
                raise ValueError("requirements must be a list")
            logger.info(
                "llm.call.end provider=openai_compatible model=%s batch_size=%d requirements=%d elapsed_ms=%d",
                self.model,
                len(batch),
                len(requirements),
                _elapsed_ms(started_at),
            )
            return requirements
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


def extract_compliance_requirements_real(
    tender_file: FileMetadata,
    *,
    parser: DocumentParser | None = None,
    llm: RequirementLLM | None = None,
    cache: RequirementCache | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
    max_batches: int = 8,
    max_batch_chars: int = 12000,
    max_retries: int = 2,
) -> list[dict[str, Any]]:
    started_at = time.perf_counter()
    logger.info(
        "compliance.extract.start file=%s max_batches=%d max_batch_chars=%d max_retries=%d cache_enabled=%s llm=%s",
        tender_file.filename,
        max_batches,
        max_batch_chars,
        max_retries,
        cache is not None,
        type(llm).__name__ if llm is not None else "default",
    )
    path = Path(tender_file.storage_path)
    active_recorder = recorder
    blocks: list[StructuredBlock] = []
    candidates: list[CandidateWindow] = []
    batches: list[list[CandidateWindow]] = []
    raw_requirements: list[Any] = []
    filter_report: list[dict[str, Any]] = []
    successful_call_ids: list[str] = []
    stats: dict[str, Any] = {
        "filename": tender_file.filename,
        "file_size": tender_file.size,
        "cache_enabled": cache is not None,
        "cache_kind": "requirements_result",
        "cache_hit": False,
        "cache_elapsed_ms": None,
        "parser": None,
        "parser_elapsed_ms": None,
        "candidate_filter_elapsed_ms": None,
        "batch_build_elapsed_ms": None,
        "parsed_blocks": 0,
        "candidate_selected_blocks": 0,
        "candidate_windows": 0,
        "batch_count": 0,
        "llm_model": None,
        "llm_total_calls": 0,
        "llm_completed_calls": 0,
        "llm_failed_calls": 0,
        "llm_retries": 0,
        "raw_requirements": 0,
        "filtered_requirements": 0,
        "filtered_by_reason": {},
        "schema_valid_calls": 0,
        "normalization_elapsed_ms": None,
        "final_requirements": 0,
    }
    current_stage = "initialization"
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

    try:
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数。")
        if not path.is_file():
            raise FileNotFoundError(path)
        if active_recorder is None:
            try:
                active_recorder = ComplianceExtractionRecorder.from_tender_path(path)
            except OSError as recorder_error:
                logger.warning(
                    "artifact.init.error file=%s error_type=%s",
                    tender_file.filename,
                    type(recorder_error).__name__,
                )
        if active_recorder is not None:
            stats["artifact_directory"] = str(active_recorder.artifact_dir)
            stats["task_id"] = active_recorder.task_dir.name
        record_event(
            "compliance.extract.start",
            file=tender_file.filename,
            max_batches=max_batches,
            max_batch_chars=max_batch_chars,
            max_retries=max_retries,
            cache_enabled=cache is not None,
        )

        cache_key: str | None = None
        current_stage = "cache_check"
        cache_started_at = time.perf_counter()
        record_event(
            "cache.check.start",
            kind="requirements_result",
            enabled=cache is not None,
        )
        logger.info(
            "cache.check.start file=%s enabled=%s",
            tender_file.filename,
            cache is not None,
        )
        if cache is not None:
            cache_key = hashlib.sha256(
                REQUIREMENT_CACHE_VERSION.encode("utf-8") + b"\0" + path.read_bytes()
            ).hexdigest()
            try:
                cached = cache.get(cache_key)
            except Exception as exc:
                stats["cache_elapsed_ms"] = _elapsed_ms(cache_started_at)
                logger.error(
                    "cache.check.error file=%s error_type=%s",
                    tender_file.filename,
                    type(exc).__name__,
                )
                raise
            if cached is not None:
                stats["cache_hit"] = True
                stats["cache_elapsed_ms"] = _elapsed_ms(cache_started_at)
                logger.info(
                    "cache.check.end file=%s status=hit requirements=%d",
                    tender_file.filename,
                    len(cached),
                )
                logger.info(
                    "compliance.extract.end file=%s status=cache_hit requirements=%d elapsed_ms=%d",
                    tender_file.filename,
                    len(cached),
                    _elapsed_ms(started_at),
                )
                record_event(
                    "cache.check.end",
                    kind="requirements_result",
                    status="hit",
                    requirements=len(cached),
                    elapsed_ms=stats["cache_elapsed_ms"],
                )
                persist_artifact(
                    "04_raw_requirements.json",
                    {"source": "requirements_cache", "requirements": cached},
                )
                persist_artifact(
                    "05_normalized_requirements.json",
                    {
                        "source": "requirements_cache",
                        "requirements": cached,
                        "filtered_requirements": [],
                        "filter_count": 0,
                        "filter_by_reason": {},
                    },
                )
                persist_artifact("06_filter_report.json", [])
                stats["final_requirements"] = len(cached)
                run_status = "complete"
                record_event(
                    "compliance.extract.end",
                    status="complete",
                    source="requirements_cache",
                    requirements=len(cached),
                    elapsed_ms=_elapsed_ms(started_at),
                )
                return cached
            stats["cache_elapsed_ms"] = _elapsed_ms(cache_started_at)
            logger.info(
                "cache.check.end file=%s status=miss key=%s",
                tender_file.filename,
                cache_key[:12],
            )
            record_event(
                "cache.check.end",
                kind="requirements_result",
                status="miss",
                key=cache_key[:12],
                elapsed_ms=stats["cache_elapsed_ms"],
            )
        else:
            stats["cache_elapsed_ms"] = _elapsed_ms(cache_started_at)
            logger.info(
                "cache.check.end file=%s status=disabled",
                tender_file.filename,
            )
            record_event(
                "cache.check.end",
                kind="requirements_result",
                status="disabled",
                elapsed_ms=stats["cache_elapsed_ms"],
            )

        active_parser = parser or MinerUDocumentParser()
        parser_name = type(active_parser).__name__
        parser_mode = (
            "mineru"
            if isinstance(active_parser, MinerUDocumentParser) and active_parser.command
            else "docx"
            if isinstance(active_parser, MinerUDocumentParser)
            else parser_name
        )
        stats["parser"] = parser_mode
        current_stage = "document_parse"
        parse_started_at = time.perf_counter()
        record_event(
            "document.parse.start",
            parser=parser_mode,
            file=tender_file.filename,
        )
        logger.info(
            "document.parse.start parser=%s file=%s",
            parser_name,
            tender_file.filename,
        )
        parse_fn = (
            active_parser.parse if hasattr(active_parser, "parse") else active_parser
        )
        try:
            blocks = parse_fn(path)  # type: ignore[operator]
        except Exception as exc:
            stats["parser_elapsed_ms"] = _elapsed_ms(parse_started_at)
            logger.error(
                "document.parse.error parser=%s file=%s error_type=%s",
                parser_name,
                tender_file.filename,
                type(exc).__name__,
            )
            raise
        stats["parser_elapsed_ms"] = _elapsed_ms(parse_started_at)
        stats["parsed_blocks"] = len(blocks)
        persist_artifact(
            "01_parsed_blocks.json",
            {
                "filename": tender_file.filename,
                "parser": parser_mode,
                "mineru_configured": parser_mode == "mineru",
                "blocks": _serialize_blocks(blocks),
            },
        )
        record_event(
            "document.parse.end",
            parser=parser_mode,
            blocks=len(blocks),
            elapsed_ms=stats["parser_elapsed_ms"],
        )
        logger.info(
            "document.parse.end parser=%s file=%s blocks=%d",
            parser_name,
            tender_file.filename,
            len(blocks),
        )

        candidate_started_at = time.perf_counter()
        record_event("candidate.filter.start")
        try:
            candidates = select_compliance_candidates(blocks)
        except Exception as exc:
            stats["candidate_filter_elapsed_ms"] = _elapsed_ms(candidate_started_at)
            logger.error(
                "candidate.filter.error file=%s error_type=%s",
                tender_file.filename,
                type(exc).__name__,
            )
            raise
        stats["candidate_filter_elapsed_ms"] = _elapsed_ms(candidate_started_at)
        stats["candidate_selected_blocks"] = sum(
            len(candidate.block_ids) for candidate in candidates
        )
        stats["candidate_windows"] = len(candidates)
        persist_artifact(
            "02_candidates.json",
            {
                "selected_block_count": stats["candidate_selected_blocks"],
                "window_count": len(candidates),
                "candidates": _serialize_candidates(candidates),
            },
        )
        record_event(
            "candidate.filter.end",
            selected_blocks=stats["candidate_selected_blocks"],
            windows=len(candidates),
            elapsed_ms=stats["candidate_filter_elapsed_ms"],
        )
        batch_build_started_at = time.perf_counter()
        record_event(
            "batch.build.start",
            candidates=len(candidates),
            max_batches=max_batches,
            max_batch_chars=max_batch_chars,
        )
        try:
            batches = build_candidate_batches(
                candidates,
                max_batches=max_batches,
                max_batch_chars=max_batch_chars,
            )
        except Exception as exc:
            stats["batch_build_elapsed_ms"] = _elapsed_ms(batch_build_started_at)
            logger.error(
                "batch.build.error file=%s error_type=%s",
                tender_file.filename,
                type(exc).__name__,
            )
            raise
        stats["batch_build_elapsed_ms"] = _elapsed_ms(batch_build_started_at)
        stats["batch_count"] = len(batches)
        persist_artifact(
            "03_batches.json",
            {
                "batch_count": len(batches),
                "max_batches": max_batches,
                "max_batch_chars": max_batch_chars,
                "batches": [
                    {
                        "index": index,
                        "candidate_count": len(batch),
                        "candidate_chars": sum(
                            len(candidate.text) for candidate in batch
                        ),
                        "candidates": _serialize_candidates(batch),
                    }
                    for index, batch in enumerate(batches, start=1)
                ],
            },
        )
        record_event(
            "batch.build.end",
            batches=len(batches),
            candidate_count=len(candidates),
            elapsed_ms=stats["batch_build_elapsed_ms"],
        )
        if not batches:
            empty_result: list[dict[str, Any]] = []
            persist_artifact("04_raw_requirements.json", {"requirements": []})
            persist_artifact(
                "05_normalized_requirements.json",
                {
                    "requirements": empty_result,
                    "filtered_requirements": [],
                    "filter_count": 0,
                    "filter_by_reason": {},
                },
            )
            current_stage = "cache_write"
            if cache is not None and cache_key is not None:
                try:
                    cache.set(cache_key, empty_result)
                except Exception as exc:
                    logger.error(
                        "cache.write.error file=%s error_type=%s",
                        tender_file.filename,
                        type(exc).__name__,
                    )
                    raise
            stats["raw_requirements"] = 0
            stats["filtered_requirements"] = 0
            stats["final_requirements"] = 0
            run_status = "complete"
            record_event(
                "compliance.extract.end",
                status="complete",
                candidates=len(candidates),
                batches=0,
                requirements=0,
                elapsed_ms=_elapsed_ms(started_at),
            )
            logger.info(
                "compliance.extract.end file=%s status=empty candidates=%d requirements=0 elapsed_ms=%d",
                tender_file.filename,
                len(candidates),
                _elapsed_ms(started_at),
            )
            return empty_result

        active_llm = llm or DeterministicComplianceLLM()
        llm_name = type(active_llm).__name__
        llm_model = getattr(active_llm, "model", llm_name)
        stats["llm_model"] = llm_model
        llm_fn = active_llm.extract if hasattr(active_llm, "extract") else active_llm
        retries_remaining = min(max_retries, max(0, 10 - len(batches)))
        current_stage = "llm"
        for batch_index, batch in enumerate(batches, start=1):
            batch_started_at = time.perf_counter()
            logger.info(
                "compliance.batch.start index=%d total=%d llm=%s candidates=%d chars=%d retries_remaining=%d",
                batch_index,
                len(batches),
                llm_name,
                len(batch),
                sum(len(candidate.text) for candidate in batch),
                retries_remaining,
            )
            record_event(
                "compliance.batch.start",
                index=batch_index,
                total=len(batches),
                model=llm_model,
                candidates=len(batch),
                chars=sum(len(candidate.text) for candidate in batch),
                retries_remaining=retries_remaining,
            )
            attempts = 0
            while True:
                attempts += 1
                stats["llm_total_calls"] += 1
                if attempts > 1:
                    stats["llm_retries"] += 1
                call_started_at = time.perf_counter()
                call_id: str | None = None
                if active_recorder is not None:
                    try:
                        call_id = active_recorder.start_llm_call(
                            batch_index=batch_index,
                            batch_count=len(batches),
                            attempt=attempts,
                            model=str(llm_model),
                            batch=_serialize_candidates(batch),
                        )
                    except Exception as recorder_error:
                        logger.error(
                            "artifact.llm.start.error batch=%d error_type=%s",
                            batch_index,
                            type(recorder_error).__name__,
                        )
                if isinstance(active_llm, OpenAICompatibleLLM):
                    active_llm.set_call_context(
                        recorder=active_recorder,
                        call_id=call_id,
                    )
                try:
                    batch_output = llm_fn(batch)  # type: ignore[operator]
                    break
                except ComplianceExtractionError as exc:
                    stats["llm_failed_calls"] += 1
                    if call_id is not None and active_recorder is not None:
                        active_recorder.fail_llm_call(
                            call_id,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                            elapsed_ms=_elapsed_ms(call_started_at),
                        )
                    if retries_remaining and _is_transient_extraction_error(exc):
                        retries_remaining -= 1
                        logger.warning(
                            "compliance.batch.retry index=%d total=%d attempt=%d retries_remaining=%d error_type=%s",
                            batch_index,
                            len(batches),
                            attempts,
                            retries_remaining,
                            type(exc).__name__,
                        )
                        record_event(
                            "compliance.batch.retry",
                            index=batch_index,
                            total=len(batches),
                            attempt=attempts,
                            retries_remaining=retries_remaining,
                            error_type=type(exc).__name__,
                        )
                        continue
                    logger.error(
                        "compliance.batch.error index=%d total=%d attempt=%d error_type=%s elapsed_ms=%d",
                        batch_index,
                        len(batches),
                        attempts,
                        type(exc).__name__,
                        _elapsed_ms(batch_started_at),
                    )
                    raise
                except Exception as exc:
                    stats["llm_failed_calls"] += 1
                    if call_id is not None and active_recorder is not None:
                        active_recorder.fail_llm_call(
                            call_id,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                            elapsed_ms=_elapsed_ms(call_started_at),
                        )
                    raise
            if isinstance(batch_output, dict):
                batch_output = batch_output.get("requirements")
            if not isinstance(batch_output, list):
                if call_id is not None and active_recorder is not None:
                    active_recorder.complete_llm_call(
                        call_id,
                        parsed_requirements=batch_output,
                        elapsed_ms=_elapsed_ms(call_started_at),
                    )
                    active_recorder.mark_llm_schema(
                        call_id,
                        valid=False,
                        error_message="批次结果不是列表",
                    )
                stats["llm_failed_calls"] += 1
                raise ComplianceExtractionError(
                    "LLM Schema 校验失败：批次结果不是列表。"
                )
            if call_id is not None and active_recorder is not None:
                active_recorder.complete_llm_call(
                    call_id,
                    parsed_requirements=batch_output,
                    elapsed_ms=_elapsed_ms(call_started_at),
                )
                successful_call_ids.append(call_id)
            stats["llm_completed_calls"] += 1
            raw_requirements.extend(batch_output)
            stats["raw_requirements"] = len(raw_requirements)
            persist_artifact(
                "04_raw_requirements.json",
                {
                    "through_batch": batch_index,
                    "requirements": raw_requirements,
                },
            )
            record_event(
                "compliance.batch.end",
                index=batch_index,
                total=len(batches),
                attempts=attempts,
                requirements=len(batch_output),
                elapsed_ms=_elapsed_ms(batch_started_at),
            )
            logger.info(
                "compliance.batch.end index=%d total=%d attempts=%d requirements=%d elapsed_ms=%d",
                batch_index,
                len(batches),
                attempts,
                len(batch_output),
                _elapsed_ms(batch_started_at),
            )

        persist_artifact(
            "04_raw_requirements.json",
            {"through_batch": len(batches), "requirements": raw_requirements},
        )
        current_stage = "requirements_normalize"
        record_event(
            "requirements.normalize.start",
            raw_requirements=len(raw_requirements),
            source_blocks=len(blocks),
        )
        normalization_started_at = time.perf_counter()
        try:
            normalized = _normalize_requirements(
                raw_requirements,
                blocks,
                filter_report=filter_report,
            )
        except Exception as exc:
            stats["normalization_elapsed_ms"] = _elapsed_ms(normalization_started_at)
            stats["filtered_requirements"] = len(filter_report)
            stats["filtered_by_reason"] = _filter_reason_counts(filter_report)
            persist_artifact("06_filter_report.json", filter_report)
            persist_artifact(
                "05_normalized_requirements.json",
                {
                    "status": "failed",
                    "requirements": [],
                    "filtered_requirements": filter_report,
                    "filter_count": len(filter_report),
                    "filter_by_reason": _filter_reason_counts(filter_report),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            for call_id in successful_call_ids:
                if active_recorder is not None:
                    active_recorder.mark_llm_schema(
                        call_id,
                        valid=False,
                        error_message=str(exc),
                    )
            logger.error(
                "requirements.normalize.error file=%s error_type=%s",
                tender_file.filename,
                type(exc).__name__,
            )
            raise
        stats["normalization_elapsed_ms"] = _elapsed_ms(normalization_started_at)
        stats["filtered_requirements"] = len(filter_report)
        stats["filtered_by_reason"] = _filter_reason_counts(filter_report)
        stats["schema_valid_calls"] = len(successful_call_ids)
        for call_id in successful_call_ids:
            if active_recorder is not None:
                active_recorder.mark_llm_schema(call_id, valid=True)
        stats["final_requirements"] = len(normalized)
        persist_artifact(
            "05_normalized_requirements.json",
            {
                "requirements": normalized,
                "filtered_requirements": filter_report,
                "filter_count": len(filter_report),
                "filter_by_reason": _filter_reason_counts(filter_report),
            },
        )
        persist_artifact("06_filter_report.json", filter_report)
        record_event(
            "requirements.normalize.end",
            requirements=len(normalized),
            filtered_requirements=len(filter_report),
            filtered_by_reason=_filter_reason_counts(filter_report),
            elapsed_ms=stats["normalization_elapsed_ms"],
        )
        current_stage = "cache_write"
        if cache is not None and cache_key is not None:
            try:
                cache.set(cache_key, normalized)
            except Exception as exc:
                logger.error(
                    "cache.write.error file=%s error_type=%s",
                    tender_file.filename,
                    type(exc).__name__,
                )
                raise
        run_status = "complete"
        record_event(
            "compliance.extract.end",
            status="complete",
            candidates=len(candidates),
            batches=len(batches),
            raw_requirements=len(raw_requirements),
            requirements=len(normalized),
            elapsed_ms=_elapsed_ms(started_at),
        )
        logger.info(
            "compliance.extract.end file=%s status=complete candidates=%d batches=%d requirements=%d elapsed_ms=%d",
            tender_file.filename,
            len(candidates),
            len(batches),
            len(normalized),
            _elapsed_ms(started_at),
        )
        return normalized
    except Exception as exc:
        failure = exc
        failed_stage = current_stage
        run_status = "failed"
        record_event(
            "compliance.extract.error",
            stage=current_stage,
            error_type=type(exc).__name__,
            error_message=str(exc),
            elapsed_ms=_elapsed_ms(started_at),
        )
        logger.error(
            "compliance.extract.error file=%s error_type=%s elapsed_ms=%d",
            tender_file.filename,
            type(exc).__name__,
            _elapsed_ms(started_at),
        )
        raise
    finally:
        stats["total_elapsed_ms"] = _elapsed_ms(started_at)
        stats["raw_requirements"] = len(raw_requirements)
        if run_status != "complete" and stats["final_requirements"] == 0:
            stats["final_requirements"] = 0
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


extract_compliance_requirements = extract_compliance_requirements_real
