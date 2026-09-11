from __future__ import annotations

import base64
import copy
import json
import logging
import mimetypes
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.file_requirement_review import run_file_requirement_review
from app.llm_concurrency import llm_request_slot
from app.navigation_content import (
    filter_navigation_sections,
    filter_navigation_templates,
)
from app.template_matching import build_template_comparisons, normalize_module_title

logger = logging.getLogger(__name__)

ATTACHMENT_REVIEW_MAX_WORKERS = 3
ATTACHMENT_REVIEW_MAX_ATTEMPTS = 2
_RETRYABLE_ATTACHMENT_REVIEW_MARKERS = (
    "请求超时",
    "网络连接错误",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
)

ATTACHMENT_REVIEW_SYSTEM_PROMPT = """你是一个招投标文件证明材料识别器。

根据招标模板、投标模块正文及关联图片，识别投标人实际提供的证明材料，并提取图片能够直接确认的客观事实。只能依据当前输入；事实、推测和无法确认的信息必须区分，不得把推测当成事实。

【语义确认闸门（必须先执行）】

在判断附件是否存在前，先确认 requirement 是否属于“投标时需要额外提供的独立证明材料要求”。这不是新增调用，而是本次多模态调用的第一步。只有 matched 才能继续识别材料、图片事实和 evidence。

不得因代码生成候选就默认分类正确。以下内容不属于本检查器：模板正文、表格填写、“不涉及”字段、日期或名称字段、普通承诺函文本、普通签字盖章、未来履约义务及其他非独立附件内容。

候选不是独立证明材料时返回 mismatched；信息不足时返回 uncertain。两种状态均不得形成附件 pass/fail，也不得因没有图片判定材料缺失。

【semantic_match.reason 输出长度】

semantic_match.status=matched 时，reason 使用极短描述，例如“属于独立证明材料要求”，不再复述招标模板条款、模板名称或投标模块内容。只有 mismatched 或 uncertain 时，才详细说明分类错误或信息不足的原因。

【职责、事实和证据边界】

只检查招标文件明确要求投标时提供、随投标文件附、应附、应同时提供、提供复印件或扫描件等当前提交的独立证明材料。中标后、合同履行中、交付后、必要时或其他未来义务不能作为本轮缺失依据；条件适用性无法确认时返回 uncertain，不得因投标人单方面写“无”“不涉及”或“不适用”就假定条件成立。

图片内容是主要依据，不得仅凭文件名判断。多张图片须整体判断；身份证要求人像面和国徽面时应分别确认。文字模糊、遮挡、裁切或无法可靠识别时标记 uncertain。不得判断证件、合同或证明文件真实性，不得增加外部法律、经验或“通常”“一般来说”“严格来说”“隐含要求”等标准。

正文和表格只能辅助身份关联，不能覆盖视觉事实。materials.facts 只提取用于确认这是什么材料的事实、用于确认材料必要组成部分是否完整的事实，以及后续跨材料一致性检查有明确复用价值的核心身份信息，例如企业名称、姓名、法定代表人、统一社会信用代码、银行账户名称、账号、证件号码、日期和文件类型。普通签字、公章等不主动提取，也不得作为 pass/fail 依据；签字盖章由独立检查链处理。

不负责文件大小、CA、加密、平台上传、外部系统验证及无关的模板文本完整性检查。

【requirements 与整体 status（强制）】

当 semantic_match.status=matched 时，最终 status 必须严格根据 requirements 聚合：任意 requirement.status = fail，overall status = fail；没有 fail，但存在任意 requirement.status = uncertain，overall status = uncertain；所有实际 requirement.status = pass，overall status = pass。

禁止出现 overall status = pass 但 requirements 中仍然存在 uncertain。如果没有形成任何真实独立证明材料 requirement，不得形成业务 pass，应返回 uncertain。

【输出】

只输出合法 JSON，不要输出 Markdown 或解释性文字。semantic_match 必须先于正式附件判断：

{
  "semantic_match": {
    "status": "matched | mismatched | uncertain",
    "reason": "为什么属于或不属于独立证明材料要求"
  },
  "status": "pass | fail | uncertain",
  "summary": "附件检查总体结论",
  "materials": [],
  "requirements": []
}

当 semantic_match.status 为 mismatched 或 uncertain 时，materials 和 requirements 必须为空；调用方会将该候选记录为未执行业务附件检查，不将其计入业务 pass 或 fail。
"""


class AttachmentReviewLLM(Protocol):
    def review_attachment(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, Any]],
    ) -> Any: ...


class AttachmentReviewError(RuntimeError):
    """Raised when a multimodal attachment review call cannot be completed."""


class DeterministicAttachmentReviewLLM:
    """Safe local fallback that never invents visual material facts."""

    model = "local"

    def review_attachment(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": "uncertain",
            "summary": "未配置附件检查多模态 LLM，无法仅依据当前图片完成判断。",
            "materials": [],
            "requirements": [],
            "semantic_match": {
                "status": "uncertain",
                "reason": "未配置附件检查多模态 LLM，无法确认候选是否属于独立证明材料要求。",
            },
        }


_ATTACHMENT_CASE_ALIASES = {
    "法定代表人负责人身份证明": "法定代表人/负责人身份证明",
    "法定代表人身份证明": "法定代表人/负责人身份证明",
    "负责人身份证明": "法定代表人/负责人身份证明",
    "法定代表人负责人授权委托书": "法定代表人/负责人授权委托书",
    "法定代表人授权委托书": "法定代表人/负责人授权委托书",
    "负责人授权委托书": "法定代表人/负责人授权委托书",
    "基本开户银行情况": "基本开户银行情况",
}

_ATTACHMENT_MATERIAL_TERM_PATTERNS = (
    "身份证明",
    "身份证",
    "营业执照",
    "事业单位法人证书",
    "法人证书",
    "证照",
    "资质证书",
    "开户证明",
    "基本账户",
    "账户",
    "银行账户",
    "使用权证明",
    "权利证明",
    "委托签署权",
    "证明材料",
    "证明文件",
    "证明",
    "相关资料",
    "授权证明",
    "复印件",
    "扫描件",
)
_ATTACHMENT_MATERIAL_TERM_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _ATTACHMENT_MATERIAL_TERM_PATTERNS)) + ")",
    re.IGNORECASE,
)
_ATTACHMENT_ACTION_RE = re.compile(
    r"(?:应|须|需|必须)?\s*"
    r"(?:同时|一并|分别|各自|应当)?\s*"
    r"(?:附|提供|提交|递交|随附|一并提供|同时提供)"
)
_ATTACHMENT_FUTURE_RE = re.compile(
    r"(?:中标后|合同(?:签订|履行)(?:后|中)?|履约(?:后|中)?|交付(?:后|中)?|"
    r"后续(?:供应|交付|履行)?|发生[^。；;\n]{0,24}(?:后|时)|必要时|"
    r"项目实施(?:后|中)?|运行(?:后|中)?)"
)
_ATTACHMENT_COMMITMENT_RE = re.compile(r"(?:本单位)?承诺(?:函|书)?")
_ATTACHMENT_SELF_TEXT_RE = re.compile(
    r"(?:承诺函|承诺书|本文件|本函|本清单|表格)"
)
_ATTACHMENT_CONDITION_RE = re.compile(
    r"(?:如有|如采用|如使用|若涉及|若采用|若使用|代理商投标|境外制造商)"
)
_ATTACHMENT_EXPERIENCE_RE = re.compile(
    r"(?:通常|一般来说|严格来说|法律上|隐含要求)"
)
_COMPLEX_ATTACHMENT_SECTION_RE = re.compile(
    r"^\s*(?:第\s*)?21(?:\.\d+)*(?:\s|$|[、.．:：\-—])"
)


def _template_requirement_text(template: dict[str, Any]) -> str:
    values: list[str] = []
    body = template.get("body")
    if isinstance(body, str) and body.strip():
        values.append(body.strip())
    source = template.get("source")
    source_text = source.get("source_text") if isinstance(source, dict) else None
    if isinstance(source_text, str) and source_text.strip():
        source_text = source_text.strip()
        if source_text not in values:
            values.append(source_text)
    return "\n".join(values)


def _attachment_requirement_clauses(text: str) -> list[str]:
    plain_text = re.sub(r"</(?:table|p|tr|li)>", "\n", text, flags=re.IGNORECASE)
    plain_text = re.sub(r"<[^>]+>", " ", plain_text)
    clauses = re.split(r"(?<=[。；;！？!?])|\n+", plain_text)
    results: list[str] = []
    for raw_clause in clauses:
        clause = re.sub(r"\s+", " ", raw_clause).strip()
        if not clause:
            continue
        if not _ATTACHMENT_MATERIAL_TERM_RE.search(clause):
            continue
        if _ATTACHMENT_FUTURE_RE.search(clause):
            continue
        if _ATTACHMENT_COMMITMENT_RE.search(clause) and not re.search(
            r"(?:应附|须附|需附|随投标文件|投标文件中|投标时)", clause
        ):
            continue
        # A bare phrase such as “递交加盖印章扫描件” describes a scan or
        # signature position, not an independent proof material.
        material_terms = [
            term
            for term in _ATTACHMENT_MATERIAL_TERM_PATTERNS
            if term in clause and term not in {"复印件", "扫描件"}
        ]
        has_submission_action = bool(_ATTACHMENT_ACTION_RE.search(clause))
        has_document_format = bool(
            re.search(r"(?:复印件|扫描件)", clause) and material_terms
        )
        if not material_terms or (not has_submission_action and not has_document_format):
            continue
        # In a compound sentence such as “代表签字确认，并提供委托签署权的
        # 相关证明材料”, only the latter independent proof document belongs
        # to this checker. Keep the split generic so signature/stamp checks do
        # not leak into the attachment requirement wording.
        signature_match = re.search(r"(?:签字|盖章|公章)", clause)
        independent_match = re.search(
            r"(?:并|且|同时|一并)?\s*(?:应当)?\s*"
            r"(?:附|提供|提交|递交|随附|一并提供|同时提供)\s*"
            r"[^。；;\n]*(?:证明材料|证明文件|相关资料|复印件|扫描件|证书|证照)",
            clause,
        )
        if signature_match and independent_match and independent_match.start() > signature_match.start():
            clause = re.sub(
                r"^(?:并|且|同时|一并)\s*",
                "",
                independent_match.group().strip(),
            ).strip()
        if clause not in results:
            results.append(clause)
    return results


def extract_attachment_requirements(template: dict[str, Any]) -> list[str]:
    """Extract current independent proof-material clauses from a full template."""

    return _attachment_requirement_clauses(_template_requirement_text(template))


def template_has_attachment_requirement(template: dict[str, Any]) -> bool:
    """Return whether the complete template explicitly requires material evidence."""

    return bool(extract_attachment_requirements(template))


def _attachment_scope_terms(scope: list[str]) -> set[str]:
    text = "\n".join(scope)
    return {term for term in _ATTACHMENT_MATERIAL_TERM_PATTERNS if term in text}


def _requirement_is_in_attachment_scope(
    requirement: str,
    scope: list[str],
) -> bool:
    if not _ATTACHMENT_MATERIAL_TERM_RE.search(requirement):
        return False
    if _ATTACHMENT_FUTURE_RE.search(requirement):
        return False
    if _ATTACHMENT_COMMITMENT_RE.search(requirement) and not re.search(
        r"(?:应附|须附|需附|随投标文件|投标文件中|投标时)", requirement
    ):
        return False
    if re.search(
        r"(?:加盖|盖章|公章|签字|真实性|真伪|伪造|是否真实|是否有效|有效性|"
        r"通常|一般来说|严格来说|法律上|隐含要求)",
        requirement,
    ):
        return False
    scope_terms = _attachment_scope_terms(scope)
    if not scope_terms:
        return False
    if any(term in requirement for term in scope_terms):
        return True
    # A model may use a generic label such as “证明材料” while still
    # referring to the concrete proof-material clause in the template.
    return bool(
        re.search(r"(?:证明材料|证明文件|复印件|扫描件)", requirement)
        and scope_terms
    )


def _material_is_in_attachment_scope(
    material_type: str,
    scope: list[str],
) -> bool:
    if _ATTACHMENT_SELF_TEXT_RE.search(material_type):
        return False
    material_terms = _attachment_scope_terms([material_type])
    scope_terms = _attachment_scope_terms(scope)
    return bool(material_terms & scope_terms)


def _requirement_scope_clauses(
    requirement: str,
    scope: list[str],
) -> list[str]:
    requirement_terms = _attachment_scope_terms([requirement])
    matched = [
        clause
        for clause in scope
        if requirement_terms & _attachment_scope_terms([clause])
    ]
    if not matched and len(scope) == 1:
        return list(scope)
    return matched


def _is_conditional_attachment_requirement(
    requirement: str,
    scope: list[str],
    template_text: str,
) -> bool:
    clauses = _requirement_scope_clauses(requirement, scope)
    if any(_ATTACHMENT_CONDITION_RE.search(clause) for clause in clauses):
        return True
    return len(scope) == 1 and bool(_ATTACHMENT_CONDITION_RE.search(template_text))


def _apply_conditional_attachment_boundary(
    parsed: dict[str, Any],
    *,
    scope: list[str],
    template_text: str,
) -> bool:
    materials = parsed.get("materials", [])
    material_image_ids = {
        image_id
        for material in materials
        if isinstance(material, dict)
        for image_id in material.get("image_ids", [])
        if isinstance(image_id, str)
    }
    changed = False
    for requirement in parsed.get("requirements", []):
        if not isinstance(requirement, dict):
            continue
        if not _is_conditional_attachment_requirement(
            requirement.get("requirement", ""),
            scope,
            template_text,
        ):
            continue
        evidence_image_ids = requirement.get("evidence_image_ids", [])
        if evidence_image_ids or material_image_ids:
            continue
        if requirement.get("status") != "uncertain":
            requirement["status"] = "uncertain"
            requirement["reason"] = (
                "该附件要求带有适用条件，当前输入没有独立材料或其他证据确认条件已经触发或不触发；"
                "不能仅凭投标文件中的“无”“不涉及”或“无图片”判定通过或缺失。"
            )
            changed = True
    if changed:
        parsed["status"] = "uncertain"
        parsed["summary"] = (
            "当前条件性独立证明材料的适用条件无法从现有投标模块确认，"
            "不能仅凭投标文件声明或缺少图片作出通过或缺失结论。"
        )
    return changed


def is_complex_attachment_scope(
    template: dict[str, Any], bid_section: dict[str, Any] | None
) -> bool:
    """Return whether this item belongs to the explicitly deferred 21.x scope."""

    candidates: list[Any] = []
    if isinstance(bid_section, dict):
        candidates.append(bid_section.get("title"))
    candidates.append(template.get("name"))
    return any(
        isinstance(value, str) and _COMPLEX_ATTACHMENT_SECTION_RE.match(value)
        for value in candidates
    )


def attachment_case_kind(template_name: Any) -> str | None:
    """Return the canonical case name for this round, if it is in scope."""

    normalized = normalize_module_title(template_name)
    return _ATTACHMENT_CASE_ALIASES.get(normalized) or (normalized or None)


def select_attachment_templates(
    templates: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Select templates whose names can be used as attachment review case labels."""

    if not isinstance(templates, list):
        return []
    return [
        template
        for template in templates
        if isinstance(template, dict) and attachment_case_kind(template.get("name"))
    ]


def _read_structured_document(
    parsed_bid: dict[str, Any],
) -> tuple[dict[str, Any] | None, Path | None]:
    direct = parsed_bid.get("structured_document")
    if isinstance(direct, dict):
        artifact_dir = parsed_bid.get("artifact_dir")
        return direct, Path(artifact_dir) if isinstance(artifact_dir, str) else None
    if isinstance(parsed_bid.get("sections"), list) and isinstance(
        parsed_bid.get("blocks"), list
    ):
        artifact_dir = parsed_bid.get("artifact_dir")
        return parsed_bid, Path(artifact_dir) if isinstance(artifact_dir, str) else None

    artifacts = parsed_bid.get("artifacts")
    artifact_path: Any = artifacts.get("structured_document") if isinstance(artifacts, dict) else None
    if artifact_path is None and isinstance(parsed_bid.get("artifact_dir"), str):
        artifact_path = str(Path(parsed_bid["artifact_dir"]) / "structured_document.json")
    if not isinstance(artifact_path, (str, Path)):
        return None, None
    try:
        path = Path(artifact_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    return (payload, path.parent) if isinstance(payload, dict) else (None, None)


def _materialized_sections(
    document: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    raw_sections = document.get("sections", [])
    raw_blocks = document.get("blocks", [])
    raw_tables = document.get("tables", [])
    raw_images = document.get("images", [])
    sections = [item for item in raw_sections if isinstance(item, dict)]
    blocks_by_id = {
        str(block.get("block_id")): block
        for block in raw_blocks
        if isinstance(block, dict) and block.get("block_id")
    }
    tables_by_block_id = {
        str(table.get("block_id")): table
        for table in raw_tables
        if isinstance(table, dict) and table.get("block_id")
    }
    images_by_block_id = {
        str(image.get("block_id")): image
        for image in raw_images
        if isinstance(image, dict) and image.get("block_id")
    }
    images_by_id = {
        str(image.get("image_id")): image
        for image in raw_images
        if isinstance(image, dict) and image.get("image_id")
    }

    materialized: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw_section in sections:
        section = copy.deepcopy(raw_section)
        block_values = raw_section.get("blocks")
        block_ids = raw_section.get("direct_block_ids")
        if not isinstance(block_ids, list):
            block_ids = raw_section.get("block_ids", [])
        blocks: list[dict[str, Any]] = []
        if isinstance(block_values, list):
            blocks = [item for item in block_values if isinstance(item, dict)]
        elif isinstance(block_ids, list):
            for block_id in block_ids:
                block = blocks_by_id.get(str(block_id))
                if block is None:
                    continue
                display_block = copy.deepcopy(block)
                block_key = str(block.get("block_id"))
                if display_block.get("type") == "table":
                    table = tables_by_block_id.get(block_key)
                    if isinstance(table, dict):
                        if "rows" in table:
                            display_block["rows"] = copy.deepcopy(table["rows"])
                        if isinstance(table.get("image_ids"), list):
                            display_block["image_ids"] = copy.deepcopy(
                                table["image_ids"]
                            )
                elif display_block.get("type") == "image":
                    image = images_by_block_id.get(block_key)
                    if isinstance(image, dict):
                        display_block.update(
                            {
                                "image_id": image.get("image_id"),
                                "img_path": image.get("img_path"),
                                "asset_status": image.get("asset_status"),
                            }
                        )
                blocks.append(display_block)
        for display_block in blocks:
            if display_block.get("type") != "table":
                continue
            table = tables_by_block_id.get(str(display_block.get("block_id")))
            if isinstance(table, dict) and isinstance(table.get("image_ids"), list):
                display_block["image_ids"] = copy.deepcopy(table["image_ids"])
        section["blocks"] = blocks
        section["child_sections"] = []
        materialized.append(section)
        if section.get("section_id"):
            by_id[str(section["section_id"])] = section

    for section in materialized:
        parent_id = section.get("parent_section_id")
        parent = by_id.get(str(parent_id)) if parent_id is not None else None
        if parent is not None:
            parent["child_sections"].append(section)

    for section in materialized:
        section["child_sections"].sort(
            key=lambda child: (
                child.get("start_order", child.get("order", 0)),
                str(child.get("section_id", "")),
            )
        )
    return materialized, by_id, images_by_block_id, images_by_id


def _block_text(block: dict[str, Any]) -> str:
    block_type = str(block.get("type", "paragraph"))
    if block_type == "image":
        image_id = block.get("image_id")
        return f"[图片，关联 image_id={image_id}]" if image_id else "[图片]"
    if block_type == "table":
        rows = block.get("rows")
        if isinstance(rows, list) and rows:
            rendered_rows: list[str] = []
            for row in rows:
                if isinstance(row, list):
                    rendered_rows.append(" | ".join(str(cell) for cell in row))
                else:
                    rendered_rows.append(str(row))
            return "表格：\n" + "\n".join(rendered_rows)
    value = block.get("text", "")
    return value if isinstance(value, str) else str(value)


def _section_content(section: dict[str, Any]) -> str:
    parts: list[str] = []
    blocks = section.get("blocks", [])
    if isinstance(blocks, list):
        ordered_blocks = sorted(
            (block for block in blocks if isinstance(block, dict)),
            key=lambda block: (block.get("order", 0), str(block.get("block_id", ""))),
        )
        parts.extend(text for block in ordered_blocks if (text := _block_text(block)))
    children = section.get("child_sections", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                child_text = _section_content(child)
                if child_text:
                    parts.append(child_text)
    return "\n".join(parts)


def _resolve_image_path(value: Any, artifact_dir: Path | None) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    if artifact_dir is None:
        return None
    root = artifact_dir.expanduser().resolve()
    candidates = [
        root / candidate
        if candidate.parts and candidate.parts[0] == "images"
        else root / "images" / candidate,
        root / candidate,
    ]
    for path in candidates:
        resolved = path.resolve()
        if root == resolved or root not in resolved.parents:
            continue
        if resolved.is_file():
            return resolved
    return None


def _collect_section_images(
    section: dict[str, Any],
    *,
    images_by_block_id: dict[str, dict[str, Any]],
    images_by_id: dict[str, dict[str, Any]],
    artifact_dir: Path | None,
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append_image(
        image: dict[str, Any],
        *,
        current: dict[str, Any],
        fallback_block_id: str,
    ) -> None:
        image_id = image.get("image_id") or fallback_block_id
        if not image_id or str(image_id) in seen:
            return
        image_id = str(image_id)
        seen.add(image_id)
        image_path = image.get("img_path")
        resolved_path = _resolve_image_path(image_path, artifact_dir)
        section_id = image.get("section_id") or current.get("section_id")
        section_path = image.get("section_path") or current.get("path", [])
        images.append(
            {
                "image_id": image_id,
                "block_id": str(image.get("block_id") or fallback_block_id),
                "section_id": section_id,
                "section_path": section_path,
                "img_path": image_path,
                "resolved_path": str(resolved_path) if resolved_path else None,
                "asset_status": image.get("asset_status", "unresolved"),
                "source_type": image.get("source_type"),
                "source_table_id": image.get("source_table_id"),
                "source_table_block_id": image.get("source_table_block_id"),
                "ocr_status": image.get("ocr_status"),
                "ocr_source": image.get("ocr_source"),
                "ocr_text": image.get("ocr_text"),
                "ocr_blocks": copy.deepcopy(image.get("ocr_blocks", [])),
                "ocr_text_block_count": image.get("ocr_text_block_count", 0),
            }
        )

    def visit(current: dict[str, Any]) -> None:
        blocks = current.get("blocks", [])
        if isinstance(blocks, list):
            ordered_blocks = sorted(
                (block for block in blocks if isinstance(block, dict)),
                key=lambda block: (block.get("order", 0), str(block.get("block_id", ""))),
            )
            for block in ordered_blocks:
                block_id = str(block.get("block_id", ""))
                block_type = str(block.get("type", ""))
                if block_type == "image":
                    image = copy.deepcopy(images_by_block_id.get(block_id, {}))
                    if not image:
                        image = {
                            "image_id": block.get("image_id"),
                            "block_id": block_id,
                            "img_path": block.get("img_path"),
                            "asset_status": block.get("asset_status", "unresolved"),
                        }
                    if not image.get("img_path") and block.get("img_path"):
                        image["img_path"] = block["img_path"]
                    if not image.get("asset_status"):
                        image["asset_status"] = block.get(
                            "asset_status", "unresolved"
                        )
                    append_image(
                        image,
                        current=current,
                        fallback_block_id=block_id,
                    )
                elif block_type == "table":
                    image_ids = block.get("image_ids", [])
                    if isinstance(image_ids, list):
                        for image_id in image_ids:
                            image = images_by_id.get(str(image_id))
                            if isinstance(image, dict):
                                append_image(
                                    copy.deepcopy(image),
                                    current=current,
                                    fallback_block_id=block_id,
                                )
        children = current.get("child_sections", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child)

    visit(section)
    return images


def _template_body(template: dict[str, Any]) -> str:
    body = template.get("body")
    if not isinstance(body, str) or not body.strip():
        source = template.get("source", {})
        body = source.get("source_text", "") if isinstance(source, dict) else ""
    return body if isinstance(body, str) else str(body)


def build_attachment_review_user_prompt(
    template: dict[str, Any],
    *,
    bid_module_name: str,
    bid_module_content: str,
    images: list[dict[str, Any]],
) -> str:
    attachment_scope = extract_attachment_requirements(template)
    attachments = template.get("attachments", [])
    if isinstance(attachments, list) and attachments:
        attachment_text = "、".join(str(item) for item in attachments)
    else:
        attachment_text = "（无）"
    image_lines = [
        f"- image_id={image.get('image_id')}，block_id={image.get('block_id')}，"
        f"关联章节={image.get('section_path') or '（未记录）'}，"
        f"资源状态={image.get('asset_status') or 'unknown'}"
        for image in images
    ]
    image_text = "\n".join(image_lines) if image_lines else "（当前模块没有关联图片）"
    scope_text = (
        "\n".join(f"- {clause}" for clause in attachment_scope)
        if attachment_scope
        else "（没有识别到独立证明材料要求）"
    )
    return f"""请检查下面这一组招标证明材料要求与投标文件实际材料。

=== 招标模板 ===

模板名称：
{template.get("name", "")}

招标模板完整内容：
<<<TENDER_TEMPLATE
{_template_body(template)}
TENDER_TEMPLATE

已提取的附件要求：
{attachment_text}

说明：

完整招标模板是主要依据。
attachments 仅作为辅助提示，如果完整模板正文中存在其他证明材料要求，也需要识别。

本轮已识别的独立证明材料要求范围：
{scope_text}
上述范围只用于约束本轮职责。requirements 只能对应招标模板明确要求投标时提供的独立证明材料，不能把模板正文、表格填写或承诺函文本，以及签字或盖章作为 requirement。


=== 投标文件模块 ===

模块名称：
{bid_module_name}

投标模块正文及表格内容：
<<<BID_MODULE
{bid_module_content}
BID_MODULE


=== 关联图片 ===

本消息同时提供当前投标模块关联的全部图片。

每张图片都有对应 image_id，请在结果中使用 image_id 引用证据。
{image_text}

=== 本次任务 ===

1. 先判断当前识别到的 requirement 是否确实属于投标时额外提供的独立证明材料要求；不得因为代码已经把候选送进来，就默认候选分类正确；
2. 只有 semantic_match.status 为 matched 时，才识别当前模块实际提供了哪些证明材料；
3. 提取各图片能够直接确认的视觉事实；
4. 判断招标模板中的证明材料要求是否满足；
5. 无法从当前图片可靠确认的内容必须标记为 uncertain；
6. 不得判断材料真实性；
7. 不得增加招标文件没有提出的检查标准；不得使用“通常”“一般来说”“严格来说”“法律上”或“隐含要求”等经验性理由；
8. 不要检查模板正文、表格填写、承诺函正文、普通签字或盖章、未来履约义务。条件性材料适用条件无法确认时，相关 requirement 必须为 uncertain；如果候选不是独立证明材料要求，不得形成附件 pass 或 fail。
9. semantic_match=matched 时 reason 只返回极短说明。
10. materials.facts 只提取与材料识别、完整性及后续主体一致性有价值的核心事实，不主动检查普通签字盖章。

返回 JSON 对象，至少包含以下字段：
{{
  "semantic_match": {{
    "status": "matched | mismatched | uncertain",
    "reason": "候选是否属于独立证明材料要求的判断依据"
  }},
  "status": "pass | fail | uncertain",
  "summary": "附件检查总体结论",
  "materials": [
    {{
      "material_type": "实际识别出的材料类型",
      "image_ids": ["实际提供的 image_id"],
      "facts": [
        {{
          "name": "能够直接确认的视觉事实",
          "status": "present | absent | uncertain",
          "evidence_image_ids": ["支持该事实的 image_id"],
          "value": "可可靠识别的具体值，没有则省略"
        }}
      ]
    }}
  ],
  "requirements": [
    {{
      "requirement": "招标模板中的证明材料要求",
      "status": "pass | fail | uncertain",
      "evidence_image_ids": ["支持结论的 image_id"],
      "reason": "判断依据"
    }}
  ]
}}

按照规定 JSON 结构返回。
"""


def _image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_attachment_request_payload(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    images: list[dict[str, Any]],
    max_tokens: int = 8192,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for image in images:
        image_id = str(image.get("image_id", ""))
        content.append({"type": "text", "text": f"当前图片 image_id={image_id}"})
        resolved_path = image.get("resolved_path")
        if isinstance(resolved_path, str) and Path(resolved_path).is_file():
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _image_data_url(Path(resolved_path))},
                }
            )
        else:
            content.append(
                {
                    "type": "text",
                    "text": f"image_id={image_id} 的图片资源当前无法读取。",
                }
            )
    return {
        "model": model,
        "temperature": 0,
        "enable_thinking": False,
        "max_tokens": max(256, min(max_tokens, 8192)),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    }


class OpenAICompatibleAttachmentReviewLLM:
    """OpenAI-compatible multimodal client for the attachment review stage."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "qwen3.8-27b",
        timeout_seconds: float = 90,
        max_tokens: int = 8192,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    def review_attachment(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, Any]],
    ) -> Any:
        payload = build_attachment_request_payload(
            model=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            max_tokens=self.max_tokens,
        )
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
        try:
            with llm_request_slot():
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
            content = response_payload["choices"][0]["message"]["content"]
            return json.loads(content) if isinstance(content, str) else content
        except TimeoutError as exc:
            raise AttachmentReviewError("附件检查 LLM 调用失败：请求超时。") from exc
        except urllib.error.HTTPError as exc:
            raise AttachmentReviewError(
                f"附件检查 LLM 调用失败：HTTP {exc.code}。"
            ) from exc
        except urllib.error.URLError as exc:
            raise AttachmentReviewError("附件检查 LLM 调用失败：网络连接错误。") from exc
        except json.JSONDecodeError as exc:
            raise AttachmentReviewError("附件检查 LLM 响应不是有效 JSON。") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AttachmentReviewError("附件检查 LLM 响应结构异常。") from exc


_FACT_STATUS = {"present", "absent", "uncertain"}
_RESULT_STATUS = {"pass", "fail", "uncertain"}


def _evidence_ids(value: Any, allowed_image_ids: set[str]) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item not in allowed_image_ids for item in value
    ):
        raise AttachmentReviewError("附件检查响应引用了不存在的 image_id。")
    return list(dict.fromkeys(value))


def _parse_attachment_review_result(
    raw: Any,
    *,
    allowed_image_ids: set[str],
    attachment_scope: list[str] | None = None,
) -> dict[str, Any]:
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, dict):
        raise AttachmentReviewError("附件检查响应不是 JSON 对象。")
    semantic_match = decoded.get("semantic_match")
    if not isinstance(semantic_match, dict):
        raise AttachmentReviewError("附件检查响应缺少 semantic_match。")
    semantic_status = semantic_match.get("status")
    semantic_reason = semantic_match.get("reason")
    if semantic_status not in {"matched", "mismatched", "uncertain"}:
        raise AttachmentReviewError(
            "附件检查响应包含无效 semantic_match.status。"
        )
    if not isinstance(semantic_reason, str) or not semantic_reason.strip():
        raise AttachmentReviewError("附件检查响应缺少 semantic_match.reason。")
    status = decoded.get("status")
    summary = decoded.get("summary")
    materials = decoded.get("materials")
    requirements = decoded.get("requirements")
    if status not in _RESULT_STATUS:
        raise AttachmentReviewError("附件检查响应包含无效 status。")
    if not isinstance(summary, str) or not summary.strip():
        raise AttachmentReviewError("附件检查响应缺少 summary。")
    if semantic_status != "matched":
        return {
            "semantic_match": {
                "status": semantic_status,
                "reason": semantic_reason.strip(),
            },
            "status": "uncertain",
            "summary": summary.strip(),
            "materials": [],
            "requirements": [],
        }
    if not isinstance(materials, list) or not isinstance(requirements, list):
        raise AttachmentReviewError("附件检查响应缺少 materials 或 requirements 数组。")

    normalized_materials: list[dict[str, Any]] = []
    for material in materials:
        if not isinstance(material, dict):
            raise AttachmentReviewError("附件检查 materials 项不是 JSON 对象。")
        material_type = material.get("material_type")
        image_ids = _evidence_ids(material.get("image_ids"), allowed_image_ids)
        facts = material.get("facts")
        if not isinstance(material_type, str) or not material_type.strip():
            raise AttachmentReviewError("附件检查 material_type 无效。")
        if not isinstance(facts, list):
            raise AttachmentReviewError("附件检查材料缺少 facts 数组。")
        normalized_facts: list[dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                raise AttachmentReviewError("附件检查 fact 不是 JSON 对象。")
            name = fact.get("name")
            fact_status = fact.get("status")
            if not isinstance(name, str) or not name.strip() or fact_status not in _FACT_STATUS:
                raise AttachmentReviewError("附件检查 fact 字段无效。")
            normalized_fact = {
                "name": name.strip(),
                "status": fact_status,
                "evidence_image_ids": _evidence_ids(
                    fact.get("evidence_image_ids"), allowed_image_ids
                ),
            }
            for optional_key in ("value", "reason"):
                optional_value = fact.get(optional_key)
                if optional_value is not None:
                    if not isinstance(optional_value, str):
                        raise AttachmentReviewError("附件检查 fact 可选字段类型无效。")
                    normalized_fact[optional_key] = optional_value
            normalized_facts.append(normalized_fact)
        normalized_materials.append(
            {
                "material_type": material_type.strip(),
                "image_ids": image_ids,
                "facts": normalized_facts,
            }
        )

    if attachment_scope is not None:
        normalized_materials = [
            material
            for material in normalized_materials
            if _material_is_in_attachment_scope(
                material["material_type"], attachment_scope
            )
        ]

    normalized_requirements: list[dict[str, Any]] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise AttachmentReviewError("附件检查 requirement 不是 JSON 对象。")
        requirement_text = requirement.get("requirement")
        requirement_status = requirement.get("status")
        reason = requirement.get("reason")
        if (
            not isinstance(requirement_text, str)
            or not requirement_text.strip()
            or requirement_status not in _RESULT_STATUS
            or not isinstance(reason, str)
        ):
            raise AttachmentReviewError("附件检查 requirement 字段无效。")
        normalized_requirement = {
            "requirement": requirement_text.strip(),
            "status": requirement_status,
            "evidence_image_ids": _evidence_ids(
                requirement.get("evidence_image_ids"), allowed_image_ids
            ),
            "reason": reason.strip(),
        }
        if _ATTACHMENT_EXPERIENCE_RE.search(normalized_requirement["reason"]):
            normalized_requirement["status"] = "uncertain"
            normalized_requirement["reason"] = (
                "模型理由包含经验性表述，不能据此形成附件检查结论；"
                "当前仅依据招标模板明确要求和可核验材料证据判断。"
            )
        if attachment_scope is None or _requirement_is_in_attachment_scope(
            normalized_requirement["requirement"], attachment_scope
        ):
            normalized_requirements.append(normalized_requirement)
    normalized_summary = summary.strip()
    if _ATTACHMENT_EXPERIENCE_RE.search(normalized_summary):
        normalized_summary = "当前结论仅依据招标模板明确要求和可核验材料证据。"
    return {
        "semantic_match": {
            "status": semantic_status,
            "reason": semantic_reason.strip(),
        },
        "status": status,
        "summary": normalized_summary,
        "materials": normalized_materials,
        "requirements": normalized_requirements,
    }


def _aggregate_attachment_status(
    requirements: list[dict[str, Any]],
) -> str | None:
    statuses = [requirement.get("status") for requirement in requirements]
    if not statuses:
        return None
    if "fail" in statuses:
        return "fail"
    if "uncertain" in statuses:
        return "uncertain"
    return "pass"


def _materialize_uncertain_attachment_requirements(
    scope: list[str],
) -> list[dict[str, Any]]:
    return [
        {
            "requirement": clause,
            "status": "uncertain",
            "evidence_image_ids": [],
            "reason": (
                "招标模板明确包含该独立证明材料要求，但当前模型没有形成可验证的 requirement 结论；"
                "当前信息不足以确认材料是否满足要求或适用条件是否成立。"
            ),
        }
        for clause in scope
    ]


def _is_retryable_attachment_review_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    message = str(exc).casefold()
    if any(marker in message for marker in _RETRYABLE_ATTACHMENT_REVIEW_MARKERS):
        return True
    match = re.search(r"\bhttp\s+(\d{3})\b", message)
    if match is None:
        return False
    status_code = int(match.group(1))
    return status_code in {408, 429} or 500 <= status_code <= 599


def _attachment_result_base(
    template: dict[str, Any],
    bid_section: dict[str, Any],
    *,
    case_type: str,
    images: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "template_id": str(template.get("id", "")),
        "template_name": str(template.get("name", "")),
        "case_type": case_type,
        "bid_module_name": bid_section.get("title", ""),
        "bid_section_id": bid_section.get("section_id"),
        "image_ids": [str(image.get("image_id")) for image in images],
        "semantic_match": {
            "status": "uncertain",
            "reason": "尚未完成候选是否属于独立证明材料要求的语义确认。",
        },
        "status": "uncertain",
        "business_status": "not_run",
        "execution_status": "pending",
        "summary": "当前图片和文本不足以完成附件检查判断。",
        "materials": [],
        "requirements": [],
        "llm_elapsed_ms": None,
    }


def _review_one_attachment(
    template: dict[str, Any],
    bid_section: dict[str, Any],
    materialized: dict[str, Any],
    *,
    images: list[dict[str, Any]],
    llm: AttachmentReviewLLM,
    recorder: ComplianceExtractionRecorder | None,
    batch_index: int,
    batch_count: int,
) -> tuple[dict[str, Any] | None, int, int]:
    case_type = attachment_case_kind(template.get("name")) or ""
    attachment_scope = extract_attachment_requirements(template)
    result = _attachment_result_base(
        template,
        bid_section,
        case_type=case_type,
        images=images,
    )
    user_prompt = build_attachment_review_user_prompt(
        template,
        bid_module_name=str(bid_section.get("title", "")),
        bid_module_content=_section_content(materialized),
        images=images,
    )
    model = str(getattr(llm, "model", type(llm).__name__))
    total_elapsed_ms = 0
    for attempt in range(1, ATTACHMENT_REVIEW_MAX_ATTEMPTS + 1):
        call_id: str | None = None
        call_started_at = time.perf_counter()
        try:
            if recorder is not None:
                call_id = recorder.start_llm_call(
                    batch_index=batch_index,
                    batch_count=batch_count,
                    attempt=attempt,
                    model=model,
                    batch={
                        "template_id": result["template_id"],
                        "template_name": result["template_name"],
                        "case_type": case_type,
                        "bid_module_name": result["bid_module_name"],
                        "image_ids": result["image_ids"],
                    },
                )
                recorder.attach_llm_input(
                    call_id,
                    build_attachment_request_payload(
                        model=model,
                        system_prompt=ATTACHMENT_REVIEW_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                        images=images,
                    ),
                )
            raw_output = llm.review_attachment(
                ATTACHMENT_REVIEW_SYSTEM_PROMPT,
                user_prompt,
                images,
            )
            parsed = _parse_attachment_review_result(
                raw_output,
                allowed_image_ids=set(result["image_ids"]),
                attachment_scope=attachment_scope,
            )
            if (
                model == "local"
                and parsed["semantic_match"]["status"] == "uncertain"
                and not parsed["materials"]
                and not parsed["requirements"]
            ):
                elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
                total_elapsed_ms += elapsed_ms
                if recorder is not None and call_id is not None:
                    recorder.complete_llm_call(
                        call_id,
                        parsed_objects={
                            **result,
                            **parsed,
                            "execution_status": "skipped",
                        },
                        schema_valid=True,
                        elapsed_ms=elapsed_ms,
                    )
                return None, attempt, 1
            if parsed["semantic_match"]["status"] != "matched":
                parsed["materials"] = []
                parsed["requirements"] = []
                parsed["status"] = "uncertain"
                parsed["summary"] = (
                    "候选未通过独立证明材料语义确认，未执行附件业务检查。"
                )
                result.update(parsed)
                result.update(
                    {
                        "business_status": "not_run",
                        "execution_status": "semantic_skipped",
                    }
                )
                elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
                total_elapsed_ms += elapsed_ms
                result["llm_elapsed_ms"] = total_elapsed_ms
                if recorder is not None and call_id is not None:
                    recorder.complete_llm_call(
                        call_id,
                        parsed_objects=result,
                        schema_valid=True,
                        elapsed_ms=elapsed_ms,
                    )
                return result, attempt, 1
            if not parsed["requirements"] and model != "local":
                parsed["requirements"] = _materialize_uncertain_attachment_requirements(
                    attachment_scope
                )
            _apply_conditional_attachment_boundary(
                parsed,
                scope=attachment_scope,
                template_text=_template_requirement_text(template),
            )
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            parsed_status = _aggregate_attachment_status(parsed["requirements"])
            if parsed_status is None:
                if recorder is not None and call_id is not None:
                    recorder.complete_llm_call(
                        call_id,
                        parsed_objects={
                            **result,
                            **parsed,
                            "execution_status": "skipped",
                        },
                        schema_valid=True,
                        elapsed_ms=elapsed_ms,
                    )
                return None, attempt, 1
            parsed["status"] = parsed_status
            result.update(parsed)
            result["business_status"] = parsed_status
            result["execution_status"] = "completed"
            result["llm_elapsed_ms"] = total_elapsed_ms
            if recorder is not None and call_id is not None:
                recorder.complete_llm_call(
                    call_id,
                    parsed_objects=result,
                    schema_valid=True,
                    elapsed_ms=elapsed_ms,
                )
            return result, attempt, 1
        except Exception as exc:  # noqa: BLE001 - isolate one module from the batch
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            if recorder is not None and call_id is not None:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=elapsed_ms,
                )
            if attempt < ATTACHMENT_REVIEW_MAX_ATTEMPTS and _is_retryable_attachment_review_error(exc):
                continue
            result.update(
                {
                    "execution_status": "failed",
                    "business_status": "not_run",
                    "status": "uncertain",
                    "summary": "附件检查调用失败，未形成业务检查结论。",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "llm_elapsed_ms": total_elapsed_ms,
                }
            )
            return result, attempt, 0
    raise AssertionError("attachment review attempts unexpectedly exhausted")


def _attachment_stats(
    *,
    template_count: int,
    participating_template_count: int,
    navigation_excluded_templates: list[dict[str, Any]],
    navigation_excluded_bid_sections: list[dict[str, Any]],
    matched_template_count: int,
    code_candidate_count: int,
    no_bid_candidate_template_ids: list[str],
    results: list[dict[str, Any]],
    llm_total_calls: int,
    llm_completed_calls: int,
    total_elapsed_ms: int,
) -> dict[str, Any]:
    failed_count = sum(item.get("execution_status") == "failed" for item in results)
    semantic_completed = [
        item for item in results if item.get("execution_status") != "failed"
    ]
    business_results = [
        item
        for item in semantic_completed
        if item.get("business_status") in {
            "pass",
            "fail",
            "uncertain",
        }
    ]
    business_status_counts = {
        status: sum(item.get("business_status") == status for item in business_results)
        for status in ("pass", "fail", "uncertain")
    }
    matched_results = [
        item
        for item in semantic_completed
        if item.get("semantic_match", {}).get("status") == "matched"
    ]
    confirmed_requirements = [
        requirement
        for item in matched_results
        for requirement in item.get("requirements", [])
        if isinstance(requirement, dict)
    ]
    return {
        "template_count": template_count,
        "participating_template_count": participating_template_count,
        "navigation_excluded_template_count": len(navigation_excluded_templates),
        "navigation_excluded_bid_section_count": len(navigation_excluded_bid_sections),
        "matched_template_count": matched_template_count,
        "code_candidate_count": code_candidate_count,
        "selected_template_count": len(results),
        "semantic_matched_count": sum(
            item.get("semantic_match", {}).get("status") == "matched"
            for item in semantic_completed
        ),
        "semantic_mismatched_count": sum(
            item.get("semantic_match", {}).get("status") == "mismatched"
            for item in semantic_completed
        ),
        "semantic_uncertain_count": sum(
            item.get("semantic_match", {}).get("status") == "uncertain"
            for item in semantic_completed
        ),
        "confirmed_requirement_count": len(confirmed_requirements),
        "requirements_without_evidence_count": sum(
            not requirement.get("evidence_image_ids")
            for requirement in confirmed_requirements
        ),
        "no_bid_candidate_template_ids": no_bid_candidate_template_ids,
        "max_concurrency": ATTACHMENT_REVIEW_MAX_WORKERS,
        "llm_total_calls": llm_total_calls,
        "llm_completed_calls": llm_completed_calls,
        "llm_failed_count": failed_count,
        "llm_failed_calls": failed_count,
        "business_status_counts": business_status_counts,
        "pass_count": business_status_counts["pass"],
        "fail_count": business_status_counts["fail"],
        "uncertain_count": business_status_counts["uncertain"],
        "llm_elapsed_ms": sum(item.get("llm_elapsed_ms") or 0 for item in results),
        "total_elapsed_ms": total_elapsed_ms,
    }


def run_attachment_review(
    extraction_result: dict[str, Any],
    parsed_bid: dict[str, Any],
    *,
    llm: AttachmentReviewLLM,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    raw_templates = extraction_result.get("templates", [])
    templates = [item for item in raw_templates if isinstance(item, dict)] if isinstance(raw_templates, list) else []
    extracted_templates = templates
    templates, excluded_templates = filter_navigation_templates(extracted_templates)
    document, artifact_dir = _read_structured_document(parsed_bid)
    (
        _sections,
        sections_by_id,
        images_by_block_id,
        images_by_id,
    ) = _materialized_sections(document or {})
    sections, excluded_sections = filter_navigation_sections(_sections)
    comparisons = build_template_comparisons(templates, sections)
    matched_template_count = sum(
        comparison.get("status") == "matched" for comparison in comparisons
    )
    no_bid_candidate_template_ids: list[str] = []
    jobs: list[
        tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]
    ] = []
    for template, comparison in zip(templates, comparisons, strict=True):
        case_type = attachment_case_kind(template.get("name"))
        if (
            not case_type
            or not template_has_attachment_requirement(template)
            or is_complex_attachment_scope(template, comparison.get("bid") or {})
        ):
            continue
        if comparison.get("candidate_count", 0) == 0:
            no_bid_candidate_template_ids.append(str(template.get("id", "")))
        if comparison.get("status") != "matched":
            continue
        bid_section = comparison.get("bid")
        if (
            not isinstance(bid_section, dict)
        ):
            continue
        materialized = sections_by_id.get(str(bid_section.get("section_id")))
        if materialized is None:
            continue
        images = _collect_section_images(
            materialized,
            images_by_block_id=images_by_block_id,
            images_by_id=images_by_id,
            artifact_dir=artifact_dir,
        )
        jobs.append((template, bid_section, materialized, images))

    executions_by_index: list[tuple[dict[str, Any] | None, int, int] | None] = [None] * len(jobs)
    if jobs:
        with ThreadPoolExecutor(
            max_workers=ATTACHMENT_REVIEW_MAX_WORKERS,
            thread_name_prefix="attachment-review",
        ) as executor:
            future_positions = {
                executor.submit(
                    _review_one_attachment,
                    template,
                    bid_section,
                    materialized,
                    images=images,
                    llm=llm,
                    recorder=recorder,
                    batch_index=index + 1,
                    batch_count=len(jobs),
                ): index
                for index, (template, bid_section, materialized, images) in enumerate(jobs)
            }
            for future in as_completed(future_positions):
                index = future_positions[future]
                try:
                    executions_by_index[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - isolate one module from the batch
                    template, bid_section, _materialized, images = jobs[index]
                    failed_result = _attachment_result_base(
                        template,
                        bid_section,
                        case_type=attachment_case_kind(template.get("name")) or "",
                        images=images,
                    )
                    failed_result.update(
                        {
                            "execution_status": "failed",
                            "summary": "附件检查调用失败，未形成业务检查结论。",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "llm_elapsed_ms": 0,
                        }
                    )
                    executions_by_index[index] = (failed_result, 0, 0)

    executions = [item for item in executions_by_index if item is not None]
    results = [item[0] for item in executions if item[0] is not None]
    review_result = {
        "mode": "attachments",
        "attachment_reviews": results,
        "navigation_exclusions": {
            "tender_templates": excluded_templates,
            "bid_modules": excluded_sections,
        },
        "stats": _attachment_stats(
            template_count=len(extracted_templates),
            participating_template_count=len(templates),
            navigation_excluded_templates=excluded_templates,
            navigation_excluded_bid_sections=excluded_sections,
            matched_template_count=matched_template_count,
            code_candidate_count=len(jobs),
            no_bid_candidate_template_ids=no_bid_candidate_template_ids,
            results=results,
            llm_total_calls=sum(item[1] for item in executions),
            llm_completed_calls=sum(item[2] for item in executions),
            total_elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        ),
    }
    if recorder is not None:
        recorder.write_json("09_attachment_reviews.json", review_result)
        recorder.event(
            "attachment.review.end",
            status="complete",
            template_count=len(extracted_templates),
            participating_template_count=len(templates),
            navigation_excluded_template_count=len(excluded_templates),
            navigation_excluded_bid_section_count=len(excluded_sections),
            matched_template_count=matched_template_count,
            selected_template_count=len(results),
            llm_total_calls=review_result["stats"]["llm_total_calls"],
            llm_failed_count=review_result["stats"]["llm_failed_count"],
            llm_elapsed_ms=review_result["stats"]["llm_elapsed_ms"],
        )
    return review_result


def run_compliance_review_with_attachments(
    extraction_result: dict[str, Any],
    parsed_bid: dict[str, Any],
    *,
    template_review_llm: Any,
    attachment_review_llm: AttachmentReviewLLM,
    performance_text_llm: Any | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Compose the stable text review with the scoped attachment review."""

    from app.performance_review import run_performance_contract_review
    from app.template_text_review import run_template_text_review

    text_result = run_template_text_review(
        extraction_result,
        parsed_bid,
        llm=template_review_llm,
        recorder=recorder,
    )
    attachment_result = run_attachment_review(
        extraction_result,
        parsed_bid,
        llm=attachment_review_llm,
        recorder=recorder,
    )
    performance_result = run_performance_contract_review(
        extraction_result,
        parsed_bid,
        llm=attachment_review_llm,
        text_llm=performance_text_llm,
        recorder=recorder,
    )
    file_result = run_file_requirement_review(
        extraction_result,
        parsed_bid,
        recorder=recorder,
    )
    has_attachments = bool(attachment_result["attachment_reviews"])
    has_performance = bool(performance_result["performance_reviews"])
    has_file_requirements = bool(file_result["file_requirement_reviews"])
    if not has_attachments and not has_performance and not has_file_requirements:
        return text_result
    combined = dict(text_result)
    mode_parts = ["template_text"]
    if has_attachments:
        mode_parts.append("attachments")
    if has_performance:
        mode_parts.append("performance")
    if has_file_requirements:
        mode_parts.append("file_requirements")
    combined["mode"] = "_and_".join(mode_parts)
    if has_attachments:
        combined["attachment_reviews"] = attachment_result["attachment_reviews"]
        combined["attachment_stats"] = attachment_result["stats"]
    combined["performance_reviews"] = performance_result["performance_reviews"]
    combined["performance_stats"] = performance_result["stats"]
    if has_file_requirements:
        combined["file_requirement_reviews"] = file_result["file_requirement_reviews"]
        combined["file_requirement_stats"] = file_result["stats"]
        combined["file_requirement_original_file"] = file_result["original_file"]
    return combined
