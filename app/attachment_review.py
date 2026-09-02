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

你的任务是：

根据给定的招标模板、投标文件模块文本以及该模块关联的图片，识别投标人实际提供了哪些证明材料，并提取能够从图片中直接确认的客观事实。

你必须严格区分：

1. 图片中能够直接看到或识别的事实；
2. 根据上下文推测的信息；
3. 当前图片无法确认的信息。

不得把推测当成事实。

【主要任务】

你需要判断：

1. 每张图片主要属于什么材料；
2. 多张图片是否属于同一份材料的不同页面或不同面；
3. 能够从图片中识别出的核心信息；
4. 招标模板要求的证明材料是否实际出现；
5. 材料的必要组成部分是否齐全；
6. 图片是否清晰到足以完成当前材料检查。

【重要原则】

一、只能依据当前提供的招标模板、投标模块文本和图片判断。

不得引用外部资料，不得自行增加招标文件没有提出的材料要求。

二、图片识别只报告能够确认的内容。

如果文字模糊、遮挡、裁切或无法可靠识别，应明确标记 uncertain，不要猜测。

三、不得判断证件、合同、证明文件的真实性。

例如：

可以判断“图片看起来是中华人民共和国居民身份证人像面”。

不能判断“该身份证真实有效”。

四、不得仅凭文件名判断图片内容。

图片内容本身才是主要依据。

五、同一模块存在多张图片时，需要结合全部图片整体判断，不要逐张孤立地形成最终结论。

六、如果招标要求居民身份证同时提供人像面和国徽面，应分别确认两面是否存在。

七、本次不负责：

- 文件大小；
- CA；
- 文件加密；
- 平台上传状态；
- 外部系统验证；
- 证明材料真实性；
- 与当前证明材料无关的模板文本完整性检查。

八、正文和表格只作为上下文和身份关联辅助。

例如正文中的“委托代理人姓名”可以帮助判断身份证是否明显对应同一人员，但不能覆盖图片中的视觉事实。

【结论原则】

pass：
当前材料能够明确满足招标模板中的该项证明材料要求。

fail：
能够明确确认要求的材料缺失、必要组成部分缺失或材料类型明显不符合要求。

uncertain：
图片存在但内容无法可靠识别，或者当前证据不足以判断材料是否满足要求。

【输出】

只输出合法 JSON，不要输出 Markdown 或其他解释性文字。
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

_ATTACHMENT_REQUIREMENT_RE = re.compile(
    r"(?:应|须|需|必须|请)?\s*"
    r"(?:附|提供|提交|递交|随附|一并提供)"
    r".{0,100}?"
    r"(?:复印件|扫描件|证明(?:文件|材料)?|相关资料|资料及证明|"
    r"证书|证照|使用权|身份证明|开户证明)",
    re.IGNORECASE,
)
_ATTACHMENT_FORMAT_RE = re.compile(r"(?:复印件|扫描件)(?:[。；;，,、\s]|$)")
_LEGACY_ATTACHMENT_TEXT_RE = re.compile(r"(?:身份证明|证明材料|开户|授权)")
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


def template_has_attachment_requirement(template: dict[str, Any]) -> bool:
    """Return whether the complete template explicitly requires material evidence."""

    text = _template_requirement_text(template)
    if _ATTACHMENT_REQUIREMENT_RE.search(text) or _ATTACHMENT_FORMAT_RE.search(text):
        return True
    # Keep the already-verified three cases compatible with older extracted
    # template bodies that retained the material words but lost the leading verb.
    legacy_case = _ATTACHMENT_CASE_ALIASES.get(
        normalize_module_title(template.get("name", ""))
    )
    return legacy_case is not None and bool(_LEGACY_ATTACHMENT_TEXT_RE.search(text))


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
    """Select only the three explicitly enabled attachment cases."""

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

1. 识别当前模块实际提供了哪些证明材料；
2. 提取各图片能够直接确认的视觉事实；
3. 判断招标模板中的证明材料要求是否满足；
4. 无法从当前图片可靠确认的内容必须标记为 uncertain；
5. 不得判断材料真实性；
6. 不得增加招标文件没有提出的检查标准。

返回 JSON 对象，至少包含以下字段：
{{
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
) -> dict[str, Any]:
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, dict):
        raise AttachmentReviewError("附件检查响应不是 JSON 对象。")
    status = decoded.get("status")
    summary = decoded.get("summary")
    materials = decoded.get("materials")
    requirements = decoded.get("requirements")
    if status not in _RESULT_STATUS:
        raise AttachmentReviewError("附件检查响应包含无效 status。")
    if not isinstance(summary, str) or not summary.strip():
        raise AttachmentReviewError("附件检查响应缺少 summary。")
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
        normalized_requirements.append(
            {
                "requirement": requirement_text.strip(),
                "status": requirement_status,
                "evidence_image_ids": _evidence_ids(
                    requirement.get("evidence_image_ids"), allowed_image_ids
                ),
                "reason": reason.strip(),
            }
        )
    return {
        "status": status,
        "summary": summary.strip(),
        "materials": normalized_materials,
        "requirements": normalized_requirements,
    }


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
        "status": "uncertain",
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
) -> tuple[dict[str, Any], int, int]:
    case_type = attachment_case_kind(template.get("name")) or ""
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
            )
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            result.update(parsed)
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
    matched_template_count: int,
    results: list[dict[str, Any]],
    llm_total_calls: int,
    llm_completed_calls: int,
    total_elapsed_ms: int,
) -> dict[str, Any]:
    failed_count = sum(item.get("execution_status") == "failed" for item in results)
    completed = [item for item in results if item.get("execution_status") != "failed"]
    return {
        "template_count": template_count,
        "matched_template_count": matched_template_count,
        "selected_template_count": len(results),
        "max_concurrency": ATTACHMENT_REVIEW_MAX_WORKERS,
        "llm_total_calls": llm_total_calls,
        "llm_completed_calls": llm_completed_calls,
        "llm_failed_count": failed_count,
        "llm_failed_calls": failed_count,
        "pass_count": sum(item.get("status") == "pass" for item in completed),
        "fail_count": sum(item.get("status") == "fail" for item in completed),
        "uncertain_count": sum(item.get("status") == "uncertain" for item in completed),
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
    document, artifact_dir = _read_structured_document(parsed_bid)
    raw_sections = document.get("sections", []) if isinstance(document, dict) else []
    if not isinstance(raw_sections, list):
        raw_sections = []
    (
        _sections,
        sections_by_id,
        images_by_block_id,
        images_by_id,
    ) = _materialized_sections(document or {})
    comparisons = build_template_comparisons(templates, raw_sections)
    matched_template_count = sum(
        comparison.get("status") == "matched" for comparison in comparisons
    )
    jobs: list[
        tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]
    ] = []
    for template, comparison in zip(templates, comparisons, strict=True):
        if comparison.get("status") != "matched":
            continue
        case_type = attachment_case_kind(template.get("name"))
        bid_section = comparison.get("bid")
        if (
            not case_type
            or not isinstance(bid_section, dict)
            or not template_has_attachment_requirement(template)
            or is_complex_attachment_scope(template, bid_section)
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

    executions_by_index: list[tuple[dict[str, Any], int, int] | None] = [None] * len(jobs)
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
    results = [item[0] for item in executions]
    review_result = {
        "mode": "attachments",
        "attachment_reviews": results,
        "stats": _attachment_stats(
            template_count=len(templates),
            matched_template_count=matched_template_count,
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
            template_count=len(templates),
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
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Compose the stable text review with the scoped attachment review."""

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
    if not attachment_result["attachment_reviews"]:
        return text_result
    combined = dict(text_result)
    combined["mode"] = "template_text_and_attachments"
    combined["attachment_reviews"] = attachment_result["attachment_reviews"]
    combined["attachment_stats"] = attachment_result["stats"]
    return combined
