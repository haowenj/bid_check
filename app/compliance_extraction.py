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
from app.models import (
    FileMetadata,
    ProjectRequirement,
    TenderRequirement,
    TenderTemplate,
)

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Keep the old import name as a data-only compatibility alias.  The former
# execution-oriented fields are no longer part of either shape.
ComplianceRequirement = TenderRequirement
logger = logging.getLogger(__name__)
REQUIREMENT_PROMPT_VERSION = "tender-requirement-prompt-v3"
REQUIREMENT_CACHE_VERSION = f"tender-requirement-v4-source-repaired:{REQUIREMENT_PROMPT_VERSION}"
PARSED_DOCUMENT_CACHE_VERSION = "mineru-parse-v1"


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
    return block.type == "heading" or block.text.strip() == block.section.strip()


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
        if current_kind is not None:
            current_blocks.append(block)

    flush()
    return regions


_TEMPLATE_ITEM_NAME_RE = re.compile(
    r"封面|投标函|响应函|法定代表人身份证明|身份证明|授权委托书|"
    r"廉洁承诺|关联关系|诉讼仲裁|基本账户|账户信息|业绩情况|业绩表|"
    r"知识产权|安全承诺|资格审查|报价表|情况表|声明|承诺函"
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
    r"(?:附|附件|须附|应附)[：:\s]*(.+?)(?=[。；;\n]|$)"
)


def _template_name(value: str) -> str:
    name = _TEMPLATE_NUMBER_PREFIX_RE.sub("", value.strip())
    name = _TEMPLATE_FORMAT_SUFFIX_RE.sub("", name).strip()
    return name.strip(" ：:。；;") or "投标文件模板"


def _is_template_item_title(block: StructuredBlock, region: FunctionalRegion) -> bool:
    if block.block_id == region.block_ids[0] or block.text.strip() == region.title.strip():
        return False
    if block.type != "heading":
        return False
    name = _template_name(block.text)
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
        item_indexes = [
            index
            for index, block in enumerate(region.blocks)
            if _is_template_item_title(block, region)
        ]
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
        if item_indexes[0] > 1:
            item_indexes[0] = 1
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
    return templates


_PROJECT_COMPILATION_RE = re.compile(
    r"组成|分别编制|编制|文件大小|文件容量|容量|大小|清晰|可读|签字盖章|扫描件|"
    r"投标有效期|有效期|保证金|备选|加密电子|电子投标文件|纸质|正本|副本|"
    r"报价.*(?:小数|位数|格式)|保留.*小数"
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
        for block, row in _project_rows(region):
            if not _PROJECT_COMPILATION_RE.search(row):
                continue
            if _PROJECT_NON_COMPILATION_RE.search(row):
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


class _RawTenderRequirementModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    condition: str | None = None
    source_block_ids: list[str] = Field(min_length=1)


def _coerce_raw_requirement(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ComplianceExtractionError("LLM Schema 校验失败：要求项不是对象。")
    # A previous extraction cache could contain a nested source object.  Keep
    # this narrow compatibility path, but never translate any execution
    # fields or infer missing requirement semantics.
    if "source_block_ids" not in raw and isinstance(raw.get("source"), dict):
        raw = {
            **{key: value for key, value in raw.items() if key != "source"},
            "source_block_ids": raw["source"].get("block_ids", []),
        }
    for field_name in ("name", "rule"):
        value = raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ComplianceExtractionError(
                f"LLM Schema 校验失败：{field_name} 不能为空。"
            )
    try:
        model = _RawTenderRequirementModel.model_validate(raw)
    except ValidationError as exc:
        raise ComplianceExtractionError(f"LLM Schema 校验失败：{exc}") from exc
    return model.model_dump(exclude_none=False)


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
    rule_text = item["rule"]
    searchable = " ".join(
        [item["name"], rule_text, item.get("condition") or ""]
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


def _compact_source_text(value: str) -> str:
    return re.sub(r"[\s，。；、：:（）()【】“”\"'《》…,.!?！？\-—_]", "", value)


def _source_text_supports_rule(rule: str, source_text: str) -> bool:
    compact_rule = _compact_source_text(rule)
    compact_source = _compact_source_text(source_text)
    if not compact_rule or compact_rule in compact_source:
        return bool(compact_rule)
    # A compressed rule can span several source blocks. Matching a meaningful
    # clause is enough to identify a supporting block without inventing text.
    clauses = re.split(r"[，。；、：:,.!?！？]+", rule)
    return any(
        len(clause_text) >= 8
        and _compact_source_text(clause_text) in compact_source
        for clause_text in clauses
    )


def _repair_source_block_ids(
    item: dict[str, Any],
    source_ids: Sequence[str],
    block_map: dict[str, StructuredBlock],
    candidate_windows: Sequence[CandidateWindow] | None,
) -> list[str]:
    if not candidate_windows:
        return list(source_ids)
    source_id_set = set(source_ids)
    context_ids: list[str] = []
    for candidate in candidate_windows:
        if source_id_set.intersection(candidate.block_ids):
            context_ids.extend(candidate.block_ids)
    context_ids = list(dict.fromkeys(context_ids))
    original_supported_ids = [
        block_id
        for block_id in source_ids
        if _source_text_supports_rule(item["rule"], block_map[block_id].text)
    ]
    if original_supported_ids:
        # Keep the model's complete set when at least one cited block directly
        # supports the rule; a rule may intentionally span several blocks.
        return list(source_ids)
    supporting_ids = [
        block_id
        for block_id in context_ids
        if _source_text_supports_rule(item["rule"], block_map[block_id].text)
    ]
    if not supporting_ids:
        return list(source_ids)
    return sorted(supporting_ids, key=lambda block_id: block_map[block_id].order)


def _normalize_requirements(
    raw_requirements: Iterable[Any],
    blocks: Sequence[StructuredBlock],
    *,
    filter_report: list[dict[str, Any]] | None = None,
    candidate_windows: Sequence[CandidateWindow] | None = None,
) -> list[TenderRequirement]:
    started_at = time.perf_counter()
    raw_items = list(raw_requirements)
    logger.info(
        "requirements.normalize.start raw_requirements=%d source_blocks=%d",
        len(raw_items),
        len(blocks),
    )
    block_map = {block.block_id: block for block in blocks}
    constraints = _project_constraints(blocks)
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in raw_items:
        item = _coerce_raw_requirement(raw)
        searchable_text = " ".join(
            [item["name"], item["rule"], item.get("condition") or ""]
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
                        "rule": item["rule"],
                        "condition": item.get("condition"),
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
        repaired_source_ids = _repair_source_block_ids(
            item,
            source_ids,
            block_map,
            candidate_windows,
        )
        if repaired_source_ids != source_ids:
            logger.warning(
                "requirements.source.repair name=%s model_source_count=%d repaired_source_count=%d",
                item["name"],
                len(source_ids),
                len(repaired_source_ids),
            )
            source_ids = repaired_source_ids
        source_blocks = sorted(
            (block_map[block_id] for block_id in source_ids),
            key=lambda block: block.order,
        )
        source_section = source_blocks[0].section
        key = (
            source_section,
            item["name"].strip(),
            item["rule"].strip(),
            (item.get("condition") or "").strip(),
        )
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                **item,
                "source_section": source_section,
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
        requirement_id = f"tender_requirement_{len(normalized) + 1:03d}"
        normalized.append(
            {
                "id": requirement_id,
                "name": item["name"].strip(),
                "rule": item["rule"].strip(),
                "condition": (
                    item.get("condition").strip()
                    if isinstance(item.get("condition"), str)
                    and item.get("condition").strip()
                    else None
                ),
                "source": {
                    "section": item["source_section"],
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
    """Local fallback that keeps candidate text as a source-grounded rule.

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
            text = candidate.text.strip()
            result.append(
                {
                    "name": candidate.section or "投标文件",
                    "rule": text,
                    "condition": None,
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

    def extract(self, batch: Sequence[CandidateWindow]) -> list[dict[str, Any]]:
        started_at = time.perf_counter()
        logger.info(
            "llm.call.start provider=openai_compatible model=%s batch_size=%d candidate_chars=%d",
            self.model,
            len(batch),
            sum(len(candidate.text) for candidate in batch),
        )
        source = "\n\n".join(
            f"[{candidate.section}] block_ids={','.join(candidate.block_ids)}\n{candidate.text}"
            for candidate in batch
        )
        prompt = (
            "从以下已经筛选的招标文件候选内容中，忠实提取明确要求投标文件做到的事项。"
            "只返回 JSON 对象 {\"requirements\":[...]}。每项只能包含 "
            "name、rule、condition、source_block_ids；name 是简短展示名称，rule 是原文要求的忠实表达，"
            "condition 仅在原文明确存在条件时填写，否则为 null。source_block_ids 必须且只能复制输入候选中的真实 block_ids，"
            "不得生成 source_text。\n"
            "保留原文中的且、或、或者、同时、分别、如有、如适用、若、除非、不得、可以、无需等逻辑关系；"
            "不要把一句包含 OR/或者 的要求拆成多个 AND 要求，不要过度原子化。原文简短明确时尽量原样保留，"
            "原文很长时只压缩与投标文件当前编制和提交有关的规则，不得增加原文不存在的要求。\n"
            "LLM 只负责发现要求、忠实压缩、保留明确条件并返回来源 block_id；不得生成 check_type；"
            "不得生成 scope；不得生成 evidence_type；不得生成 category；不得生成 checks；"
            "不得判断文本/图片/结构/metadata 执行方式，不得设计执行器或投标文件定位方式。\n"
            "继续排除评分/评标规则、CA证书当前有效性、电子采购系统上传/加密提交等外部系统状态，"
            "排除合同签订后或履约阶段动作；若项目明确不接受联合体或不允许备选方案，不生成对应编制要求。\n\n"
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


def _parsed_document_cache_key(path: Path, parser: DocumentParser) -> str:
    # Keep this key format stable so existing MinerU/DOCX parse caches remain
    # reusable across requirement-schema and prompt changes.
    parser_descriptor = type(parser).__name__
    if isinstance(parser, MinerUDocumentParser):
        parser_descriptor += f"\0{parser.command or ''}"
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
    """Hash all inputs that can change the extracted TenderRequirement list."""
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


def extract_compliance_requirements_real(
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
) -> list[TenderRequirement]:
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
    active_parser = parser or MinerUDocumentParser()
    active_llm = llm or DeterministicComplianceLLM()
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
        "requirement_prompt_version": REQUIREMENT_PROMPT_VERSION,
        "parser_cache_enabled": parser_cache is not None,
        "parser_cache_hit": False,
        "parser_cache_elapsed_ms": None,
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
            cache_key = _requirement_cache_key(
                path,
                active_parser,
                active_llm,
                max_batches=max_batches,
                max_batch_chars=max_batch_chars,
            )
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
        parser_cache_key: str | None = None
        parser_cache_started_at = time.perf_counter()
        record_event(
            "document.parse.start",
            parser=parser_mode,
            file=tender_file.filename,
            cache_enabled=parser_cache is not None,
        )
        logger.info(
            "document.parse.start parser=%s file=%s",
            parser_name,
            tender_file.filename,
        )
        parse_fn = (
            active_parser.parse if hasattr(active_parser, "parse") else active_parser
        )
        blocks: list[StructuredBlock] | None = None
        if parser_cache is not None:
            parser_cache_key = _parsed_document_cache_key(path, active_parser)
            try:
                cached_blocks = parser_cache.get(parser_cache_key)
                blocks = _deserialize_blocks(cached_blocks)
            except Exception as exc:
                logger.warning(
                    "document.parse.cache.error parser=%s file=%s error_type=%s",
                    parser_name,
                    tender_file.filename,
                    type(exc).__name__,
                )
            if blocks is not None:
                stats["parser_cache_hit"] = True
                stats["parser_cache_elapsed_ms"] = _elapsed_ms(parser_cache_started_at)
                record_event(
                    "document.parse.cache",
                    parser=parser_mode,
                    status="hit",
                    blocks=len(blocks),
                    elapsed_ms=stats["parser_cache_elapsed_ms"],
                )
        if blocks is None:
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
            if parser_cache is not None and parser_cache_key is not None:
                try:
                    parser_cache.set(parser_cache_key, _serialize_blocks(blocks))
                except Exception as exc:
                    logger.warning(
                        "document.parse.cache.write.error parser=%s file=%s error_type=%s",
                        parser_name,
                        tender_file.filename,
                        type(exc).__name__,
                    )
        stats["parser_cache_elapsed_ms"] = _elapsed_ms(parser_cache_started_at)
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
                candidate_windows=candidates,
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
