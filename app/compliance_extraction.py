from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models import FileMetadata


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ComplianceRequirement = dict[str, Any]


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
        value = self._values.get(key)
        return deepcopy(value) if value is not None else None

    def set(self, key: str, value: list[dict[str, Any]]) -> None:
        self._values[key] = deepcopy(value)


class JsonRequirementCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> list[dict[str, Any]] | None:
        try:
            payload = json.loads(self._path(key).read_text(encoding="utf-8"))
        except FileNotFoundError, OSError, json.JSONDecodeError:
            return None
        return deepcopy(payload) if isinstance(payload, list) else None

    def set(self, key: str, value: list[dict[str, Any]]) -> None:
        target = self._path(key)
        temporary = target.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, target)


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

    try:
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ComplianceExtractionError(f"无法解析招标文件结构：{path.name}") from exc

    blocks: list[StructuredBlock] = []
    current_section = ""
    order = 0
    body = root.find(f"{W_NS}body")
    if body is None:
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
        if not self.command:
            return parse_docx_document(path)
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
            return _blocks_from_mineru_payload(json.loads(completed.stdout))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise ComplianceExtractionError("MinerU 文档解析失败。") from exc


_EXCLUDED_RE = re.compile(
    r"评分|得分|分值|评标|评审因素|商务评分|技术评分|价格评分|报价评分|综合评分"
)
_COMPLIANCE_RE = re.compile(
    r"填写|提供|附[：:]|必须|应当|须|不得|签字|签章|盖章|公章|日期|年[　 ]?月|身份证|营业执照|社保|资格证|证书|合同证明|证明材料|复印件|扫描件|业绩|人员名单|人员信息|人员姓名|联系方式|招标编号|项目名称|投标人名称|姓名|委托代理|法定代表|文件大小|附件大小|文件容量|大附件|清晰|可读|上传|加密|CA|电子投标|文件份数|组成|附件|对应|关联|每项|逐一"
)
_PLACEHOLDER_RE = re.compile(
    r"_{2,}|[…·.]{2,}|【[^】]{1,40}】|\[[^\]]{0,40}\]|（(?:投标人|项目|公司|日期|盖章|签字)[^）]{0,40}）"
)


def _is_candidate_block(block: StructuredBlock) -> bool:
    if not block.text or _EXCLUDED_RE.search(block.text):
        return False
    return bool(_COMPLIANCE_RE.search(block.text) or _PLACEHOLDER_RE.search(block.text))


def select_compliance_candidates(
    blocks: Iterable[StructuredBlock],
) -> list[CandidateWindow]:
    selected = [
        block
        for block in sorted(blocks, key=lambda item: item.order)
        if _is_candidate_block(block)
    ]
    if not selected:
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
    return windows


def build_candidate_batches(
    candidates: Sequence[CandidateWindow],
    *,
    max_batches: int = 8,
    max_batch_chars: int = 12000,
) -> list[list[CandidateWindow]]:
    if max_batches < 1 or max_batches > 10:
        raise ValueError("max_batches 必须在 1 到 10 之间。")
    if not candidates:
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
    return [batch for batch in batches if batch]


class _CheckModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    requirement: str = Field(min_length=1)
    check_type: str = Field(min_length=1)
    evidence_type: str = Field(min_length=1)


class _TargetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    scope: str = Field(min_length=1)


class _ApplicabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1)
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


def _coerce_raw_requirement(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ComplianceExtractionError("LLM Schema 校验失败：要求项不是对象。")
    if "source_block_ids" not in raw and isinstance(raw.get("source"), dict):
        raw = {
            **{key: value for key, value in raw.items() if key != "source"},
            "source_block_ids": raw["source"].get("block_ids", []),
        }
    try:
        model = _RawRequirementModel.model_validate(raw)
    except ValidationError as exc:
        raise ComplianceExtractionError(f"LLM Schema 校验失败：{exc}") from exc
    return model.model_dump()


def _normalize_requirements(
    raw_requirements: Iterable[Any],
    blocks: Sequence[StructuredBlock],
) -> list[dict[str, Any]]:
    block_map = {block.block_id: block for block in blocks}
    grouped: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for raw in raw_requirements:
        item = _coerce_raw_requirement(raw)
        searchable_text = " ".join(
            [item["name"], *(check["requirement"] for check in item["checks"])]
        )
        if _EXCLUDED_RE.search(searchable_text):
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
    return normalized


class DeterministicComplianceLLM:
    """Local fallback that extracts structured checks directly from candidates.

    It is source-grounded and deterministic, so development can run without a
    model credential; production can select ``OpenAICompatibleLLM`` instead.
    """

    def extract(self, batch: Sequence[CandidateWindow]) -> list[dict[str, Any]]:
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
        return result


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout_seconds: float = 90,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def extract(self, batch: Sequence[CandidateWindow]) -> list[dict[str, Any]]:
        source = "\n\n".join(
            f"[{candidate.section}] block_ids={','.join(candidate.block_ids)}\n{candidate.text[:6000]}"
            for candidate in batch
        )[:12000]
        prompt = (
            "从以下招标文件候选内容中提取投标文件编制合规要求。只返回 JSON 对象 "
            '{"requirements":[...]}。每项必须包含 name、category、target、checks、'
            "applicability、source_block_ids；只关注填写、占位符、附件、签字盖章日期、"
            "材料关联和文件编制要求，排除评分、得分、评标规则。\n\n" + source
        )
        payload = {
            "model": self.model,
            "temperature": 0,
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
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            content = response_payload["choices"][0]["message"]["content"]
            decoded = json.loads(content) if isinstance(content, str) else content
            requirements = (
                decoded.get("requirements") if isinstance(decoded, dict) else decoded
            )
            if not isinstance(requirements, list):
                raise ValueError("requirements must be a list")
            return requirements
        except (
            OSError,
            urllib.error.URLError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ComplianceExtractionError("LLM 合规要求提取失败。") from exc


def extract_compliance_requirements_real(
    tender_file: FileMetadata,
    *,
    parser: DocumentParser | None = None,
    llm: RequirementLLM | None = None,
    cache: RequirementCache | None = None,
    max_batches: int = 8,
    max_batch_chars: int = 12000,
) -> list[dict[str, Any]]:
    path = Path(tender_file.storage_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    cache_key: str | None = None
    if cache is not None:
        cache_key = hashlib.sha256(path.read_bytes()).hexdigest()
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    active_parser = parser or MinerUDocumentParser()
    parse_fn = active_parser.parse if hasattr(active_parser, "parse") else active_parser
    blocks = parse_fn(path)  # type: ignore[operator]
    candidates = select_compliance_candidates(blocks)
    batches = build_candidate_batches(
        candidates,
        max_batches=max_batches,
        max_batch_chars=max_batch_chars,
    )
    if not batches:
        empty_result: list[dict[str, Any]] = []
        if cache is not None and cache_key is not None:
            cache.set(cache_key, empty_result)
        return empty_result
    active_llm = llm or DeterministicComplianceLLM()
    llm_fn = active_llm.extract if hasattr(active_llm, "extract") else active_llm
    raw_requirements: list[Any] = []
    for batch in batches:
        batch_output = llm_fn(batch)  # type: ignore[operator]
        if isinstance(batch_output, dict):
            batch_output = batch_output.get("requirements")
        if not isinstance(batch_output, list):
            raise ComplianceExtractionError("LLM Schema 校验失败：批次结果不是列表。")
        raw_requirements.extend(batch_output)
    normalized = _normalize_requirements(raw_requirements, blocks)
    if cache is not None and cache_key is not None:
        cache.set(cache_key, normalized)
    return normalized


extract_compliance_requirements = extract_compliance_requirements_real
