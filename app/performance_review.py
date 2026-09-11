from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.attachment_review import (
    ATTACHMENT_REVIEW_MAX_ATTEMPTS,
    AttachmentReviewError,
    AttachmentReviewLLM,
    _collect_section_images,
    _is_retryable_attachment_review_error,
    _materialized_sections,
    _read_structured_document,
    _section_content,
    build_attachment_request_payload,
)
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.llm_protocol import build_thinking_params
from app.template_matching import normalize_module_title

PERFORMANCE_CASE_TITLE = "21.1 信达旺大厦云平台运营及维护服务"
PERFORMANCE_CASE_TYPE = PERFORMANCE_CASE_TITLE
PERFORMANCE_CANDIDATE_TOP_K = 3
PERFORMANCE_CASE_MAX_WORKERS = 5
PERFORMANCE_CASE_NUMBER_RE = re.compile(r"^21\.([1-6])(?:\s|$)")

PERFORMANCE_TEXT_REVIEW_SYSTEM_PROMPT = """你是一个招投标文件业绩合同 OCR 文本检查器。

你只能依据本次输入的当前 21.x 业绩章节、21 业绩情况表中用于关联的当前行，以及当前 21.x 归属图片的完整 OCR 文本进行判断。其他 21.x 业绩章节不是本次证据，禁止使用或补足当前业绩的缺失。

本次输入按原始图片顺序提供了当前 21.x 的全部 OCR 文本。不得因为关键词、页码、候选分数或个人经验删除或忽略正文；应在完整文本范围内统一识别：项目名称/合同名称、甲乙方、合同金额、实施时间/服务期限、合同服务内容、是否为框架合同，以及最可能的合同签署页和签署日期页。若正文明确出现委托事项、服务范围、工作内容或具体服务条款，必须返回至少一条服务内容事实及其连续 OCR 原文；文本不足以可靠确定时返回 uncertain，不得猜测。

每一个事实和每一个签署页候选都必须返回属于本次输入的 image_id，并复制对应图片 OCR 中实际出现的原文作为 evidence_text。不得生成输入中不存在的 image_id 或 OCR 证据。签署日期候选即使 OCR 显示“年 月 日”为空，也应定位该日期栏所在的签署页，交给视觉模型判断是否实际填写；不要因为日期为空而省略日期页候选。框架合同不能仅凭“框架合同”四个字硬判，应综合合同用途、订单/结算/发票等上下文；配套结算材料也必须在当前 21.x 的 OCR 中有证据。

只输出合法 JSON，不要输出 Markdown。"""

PERFORMANCE_VISUAL_REVIEW_SYSTEM_PROMPT = """你是一个招投标文件 21.x 业绩合同签署页视觉检查器。

本次输入只包含文本模型从当前 21.x 完整 OCR 中定位的少量签署候选图片，以及 OCR unavailable 时必须保留的视觉兜底图片。只能查看这些图片，不能使用其他 21.x 业绩章节或未提供的图片。

只判断两个视觉事实：
1. 合同签署页是否实际可见甲乙双方真实签字和/或盖章；
2. 合同签署日期栏是否实际填写且日期可辨认。只有“年 月 日”空栏、日期缺失、模糊到无法辨认或仅凭 OCR 推测日期时，signature_date 不能通过。

图片模糊、裁切、遮挡或无法可靠判断时返回 uncertain，不得凭文件名、OCR、候选理由或通常经验补足。每个结论必须返回实际查看图片的 image_id。

只输出合法 JSON：
{
  "signature_page": {"status": "pass | fail | uncertain", "reason": "...", "evidence_image_ids": ["..."]},
  "signature_date": {"status": "pass | fail | uncertain", "reason": "...", "evidence_image_ids": ["..."]}
}
"""

PERFORMANCE_REVIEW_SYSTEM_PROMPT = """你是一个招投标文件 21.x 业绩合同材料检查器。

你只能依据本次输入的当前 21.x 业绩章节、该章节下的图片和 21 业绩情况表中用于关联的当前行，识别合同或证明材料并检查明确的合同关键页要求。其他 21.x 业绩章节不是本次证据，禁止使用或补足当前业绩的缺失。

必须分别判断：
1. 合同服务内容；
2. 实施时间；
3. 合同金额；
4. 合同签页上的双方签署/盖章；
5. 合同签署日期；合同签署页必须同时有双方签署/盖章以及可辨认的签署日期。只有签章没有日期，或年、月、日栏明确为空，不能通过；
6. 如果图片能够可靠判断是框架合同，再检查是否存在对应框架合同结算的发票、订单或结算单据等证明材料。

只能把图片能够直接确认的内容作为证据。程序提供的 MinerU OCR 文本只用于定位候选和提供明确文字事实；签字、盖章、签署日期等视觉事实必须结合本次提供的候选图片判断。图片模糊、裁切、遮挡或无法可靠判断时使用 uncertain，不得凭文件名、章节标题或通常经验补足。不得判断合同或发票等材料的真实性，不得把 21 业绩情况表中的金额、日期或项目名称直接当作合同关键页证据；该表只能用于确认材料顺序和项目关联。

framework_contract.status 必须是 yes、no 或 uncertain。checks 必须使用固定 key：service_content、implementation_time、contract_amount、signature_page、signature_date、framework_settlement_material。framework_settlement_material 只有在 framework_contract.status 为 yes 或 uncertain 时才需要返回；无法确认时返回 uncertain。缺少任何必须的 check 时，调用方会按 uncertain 处理。

只输出合法 JSON，不要输出 Markdown。"""

_CHECK_DEFINITIONS = {
    "service_content": "合同服务内容",
    "implementation_time": "实施时间",
    "contract_amount": "合同金额",
    "signature_page": "合同签页",
    "signature_date": "合同签署日期",
    "framework_settlement_material": "框架合同对应的发票、订单或结算单据等证明材料",
    "table_project_name_consistency": "业绩情况表项目名称与合同项目/合同名称一致性",
    "table_counterparty_consistency": "业绩情况表最终用户与合同相对方一致性",
    "table_amount_consistency": "业绩情况表销售金额与合同金额一致性",
    "table_time_consistency": "业绩情况表服务期限与合同实施时间/服务期限一致性",
}
_CHECK_KEYS = tuple(_CHECK_DEFINITIONS)
_CHECK_STATUS = {"pass", "fail", "uncertain"}
_FRAMEWORK_STATUS = {"yes", "no", "uncertain"}


def _structured_image_ocr_blocks(image: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    sources = [image]
    for key in ("ocr", "metadata", "mineru_item"):
        value = image.get(key)
        if isinstance(value, dict):
            sources.append(value)
    for source in sources:
        raw_blocks = source.get("ocr_blocks")
        source_block_count = 0
        if isinstance(raw_blocks, list):
            for index, raw_block in enumerate(raw_blocks, start=1):
                if not isinstance(raw_block, dict):
                    continue
                text = raw_block.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                blocks.append(
                    {
                        "block_id": str(
                            raw_block.get("block_id")
                            or f"structured-ocr-{index:04d}"
                        ),
                        "text": text.strip(),
                        "order": raw_block.get("order", index),
                        "type": str(raw_block.get("type", "paragraph")),
                    }
                )
                source_block_count += 1
        if source_block_count == 0:
            for key in ("ocr_text", "text"):
                text = source.get(key)
                if (
                    isinstance(text, str)
                    and text.strip()
                    and not text.strip().startswith("[MinerU ")
                ):
                    blocks.append(
                        {
                            "block_id": f"structured-ocr-{len(blocks) + 1:04d}",
                            "text": text.strip(),
                            "order": len(blocks) + 1,
                            "type": "paragraph",
                        }
                    )
                    source_block_count += 1
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for block in blocks:
        key = (str(block.get("block_id")), str(block.get("text")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(block)
    return unique


def _enrich_images_with_ocr(
    images: list[dict[str, Any]],
    *,
    raw_images_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read OCR persisted by the DOCX MinerU parse.

    The performance-review stage deliberately has no OCR engine fallback.  A
    missing OCR result is reported as unavailable so that this stage cannot
    silently submit the same image to MinerU a second time.
    """

    entries: list[dict[str, Any]] = []
    started_at = time.perf_counter()
    for image in images:
        image_id = str(image.get("image_id"))
        structured_image = raw_images_by_id.get(image_id, image)
        structured_blocks = _structured_image_ocr_blocks(structured_image)
        if structured_blocks:
            entries.append(
                {
                    "image_id": image_id,
                    "status": "available",
                    "source": "structured_document",
                    "cache_hit": False,
                    "blocks": structured_blocks,
                    "elapsed_ms": 0,
                }
            )
        else:
            entries.append(
                {
                    "image_id": image_id,
                    "status": "unavailable",
                    "source": "structured_document_unavailable",
                    "cache_hit": False,
                    "blocks": [],
                    "elapsed_ms": 0,
                    "error_message": "structured_document 中没有可用的图片 OCR。",
                }
            )
    available_count = sum(
        entry["status"] == "available" for entry in entries
    )
    return entries, {
        "status": (
            "complete"
            if available_count == len(entries)
            else "partial"
            if available_count
            else "unavailable"
        ),
        "image_count": len(images),
        "available_image_count": available_count,
        "image_results": [
            {
                "image_id": entry["image_id"],
                "status": entry["status"],
                "source": entry["source"],
                "cache_hit": entry["cache_hit"],
                "text_block_count": len(entry["blocks"]),
                "text_length": sum(len(block["text"]) for block in entry["blocks"]),
                "elapsed_ms": entry["elapsed_ms"],
                **(
                    {
                        "error_type": entry.get("error_type", "OCRUnavailable"),
                        "error_message": entry.get("error_message"),
                    }
                    if entry.get("error_message")
                    else {}
                ),
            }
            for entry in entries
        ],
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        "engine_call_count": 0,
        "processed_image_count": 0,
        "precomputed_image_count": available_count,
        "cache_hit_count": 0,
        "failed_count": sum(
            int(entry["status"] == "unavailable") for entry in entries
        ),
    }


def _build_full_ocr_contract_text(
    images: list[dict[str, Any]],
    ocr_entries: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Build the complete, ordered OCR transcript for one performance case.

    This is intentionally a presentation helper, not a retrieval layer. Every
    image in the scoped section is represented once, including images without
    available OCR, so the text model sees the same evidence boundary as the
    visual stage.
    """

    entries_by_image_id = {
        str(entry.get("image_id")): entry for entry in ocr_entries
    }
    parts: list[str] = []
    image_ranges: list[dict[str, Any]] = []
    for image in images:
        image_id = str(image.get("image_id"))
        entry = entries_by_image_id.get(image_id, {})
        blocks = [
            block
            for block in entry.get("blocks", [])
            if isinstance(block, dict) and str(block.get("text", "")).strip()
        ]
        blocks.sort(
            key=lambda block: (
                block.get("order", 0),
                str(block.get("block_id", "")),
            )
        )
        image_text = "\n".join(str(block["text"]).strip() for block in blocks)
        if not image_text:
            image_text = "（该图片没有可用 OCR 文本）"
        parts.extend([f"=== {image_id} ===", image_text])
        image_ranges.append(
            {
                "image_id": image_id,
                "block_ids": [str(block.get("block_id")) for block in blocks],
                "text": image_text,
                "character_count": len(image_text),
                "status": entry.get("status", "unavailable"),
                "source": entry.get("source", "structured_document_unavailable"),
            }
        )
    full_text = "\n".join(parts)
    character_count = len(full_text)
    non_whitespace_character_count = sum(
        not character.isspace() for character in full_text
    )
    return full_text, image_ranges, {
        "image_count": len(images),
        "character_count": character_count,
        "token_count": non_whitespace_character_count,
        "estimated_token_count": non_whitespace_character_count,
        "token_count_is_estimate": True,
        "token_count_source": "conservative_estimate_no_tokenizer",
        "token_count_formula": "one token per non-whitespace character",
        "non_whitespace_character_count": non_whitespace_character_count,
        "ocr_unavailable_image_ids": [
            str(item.get("image_id"))
            for item in image_ranges
            if item.get("status") != "available"
        ],
        "image_ranges": image_ranges,
    }


def _ocr_text_by_image_id(
    image_ranges: list[dict[str, Any]],
) -> dict[str, str]:
    return {
        str(item.get("image_id")): (
            str(item.get("text", ""))
            if item.get("status") == "available"
            else ""
        )
        for item in image_ranges
    }


def _text_matches_ocr(evidence_text: str, ocr_text: str) -> bool:
    evidence = str(evidence_text or "").strip()
    source = str(ocr_text or "").strip()
    if not evidence or not source:
        return False
    if evidence in source:
        return True
    # OCR may insert line breaks between table cells. Preserve the model's
    # returned evidence verbatim while allowing only whitespace normalization
    # for source validation.
    return _compact_text(evidence) in _compact_text(source)


def _raw_json_object(raw: Any, *, error_message: str) -> dict[str, Any]:
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, dict):
        raise AttachmentReviewError(error_message)
    return decoded


def _text_evidence_items(
    value: Any,
    *,
    allowed_image_ids: set[str],
    ocr_text_by_image_id: dict[str, str],
) -> list[dict[str, Any]]:
    """Normalize and validate exact OCR evidence returned by the text model."""

    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            image_ids = item.get("evidence_image_ids")
            if image_ids is None and item.get("image_id") is not None:
                image_ids = [item.get("image_id")]
            if not isinstance(image_ids, list):
                image_ids = []
            evidence_text = item.get("evidence_text")
            if evidence_text is None:
                evidence_text = item.get("text")
            if isinstance(evidence_text, str):
                evidence_texts = [evidence_text]
            elif isinstance(item.get("evidence_texts"), list):
                evidence_texts = [
                    text
                    for text in item["evidence_texts"]
                    if isinstance(text, str)
                ]
            else:
                evidence_texts = []
            nested = item.get("evidence")
            if isinstance(nested, list):
                for nested_item in nested:
                    if not isinstance(nested_item, dict):
                        continue
                    nested_image_id = nested_item.get("image_id")
                    if nested_image_id is not None:
                        image_ids.append(nested_image_id)
                    nested_text = nested_item.get("evidence_text")
                    if nested_text is None:
                        nested_text = nested_item.get("text")
                    if isinstance(nested_text, str):
                        evidence_texts.append(nested_text)
            for index, raw_image_id in enumerate(image_ids):
                image_id = str(raw_image_id)
                if image_id not in allowed_image_ids:
                    raise AttachmentReviewError(
                        f"业绩合同文本模型响应引用了不属于当前 21.x 的 image_id：{image_id}。"
                    )
                if not evidence_texts:
                    continue
                evidence_text = evidence_texts[min(index, len(evidence_texts) - 1)]
                if not _text_matches_ocr(
                    evidence_text, ocr_text_by_image_id.get(image_id, "")
                ):
                    continue
                candidates.append(
                    {
                        "image_id": image_id,
                        "evidence_text": evidence_text.strip(),
                    }
                )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = (item["image_id"], item["evidence_text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _text_fact_records(
    value: Any,
    *,
    key: str,
    allowed_image_ids: set[str],
    ocr_text_by_image_id: dict[str, str],
) -> list[dict[str, Any]]:
    records = [value] if isinstance(value, dict) else value
    if not isinstance(records, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw_record in records:
        if not isinstance(raw_record, dict):
            continue
        evidence = _text_evidence_items(
            raw_record,
            allowed_image_ids=allowed_image_ids,
            ocr_text_by_image_id=ocr_text_by_image_id,
        )
        image_ids = list(dict.fromkeys(item["image_id"] for item in evidence))
        status = raw_record.get("status", "present")
        if status not in {"present", "absent", "uncertain"}:
            status = "uncertain"
        if not evidence:
            status = "uncertain"
        item: dict[str, Any] = {
            "field": key,
            "status": status,
            "value": str(raw_record.get("value") or "").strip(),
            "image_id": image_ids[0] if image_ids else None,
            "evidence_image_ids": image_ids,
            "evidence": evidence,
            "reason": str(raw_record.get("reason") or "").strip(),
        }
        if raw_record.get("name") is not None:
            item["name"] = str(raw_record.get("name"))
        normalized.append(item)
    return normalized


def _normalize_text_model_result(
    raw: Any,
    *,
    allowed_image_ids: set[str],
    image_ranges: list[dict[str, Any]],
) -> dict[str, Any]:
    decoded = _raw_json_object(
        raw, error_message="业绩合同文本模型响应不是 JSON 对象。"
    )
    summary = str(decoded.get("summary") or "完整 OCR 文本检查已返回结果。")
    ocr_text_by_image_id = _ocr_text_by_image_id(image_ranges)
    raw_facts = decoded.get("facts", {})
    if not isinstance(raw_facts, dict):
        raw_facts = {}
    facts = {
        key: _text_fact_records(
            raw_facts.get(key, []),
            key=key,
            allowed_image_ids=allowed_image_ids,
            ocr_text_by_image_id=ocr_text_by_image_id,
        )
        for key in (
            "project_name",
            "parties",
            "contract_amount",
            "implementation_time",
            "service_content",
        )
    }
    framework = decoded.get("framework_contract", {})
    if not isinstance(framework, dict):
        framework = {}
    framework_status = framework.get("status")
    if framework_status not in _FRAMEWORK_STATUS:
        framework_status = "uncertain"
    framework_evidence = _text_evidence_items(
        framework.get("evidence")
        or framework.get("evidence_items")
        or {
            "evidence_image_ids": framework.get("evidence_image_ids", []),
            "evidence_texts": framework.get("evidence_texts", []),
        },
        allowed_image_ids=allowed_image_ids,
        ocr_text_by_image_id=ocr_text_by_image_id,
    )
    framework_result = {
        "status": framework_status,
        "reason": str(framework.get("reason") or "无法从完整 OCR 可靠确认合同类型。"),
        "evidence_image_ids": list(
            dict.fromkeys(item["image_id"] for item in framework_evidence)
        ),
        "evidence": framework_evidence,
    }

    def normalize_candidates(field_name: str) -> list[dict[str, Any]]:
        candidate_value = decoded.get(field_name, [])
        records = [candidate_value] if isinstance(candidate_value, dict) else candidate_value
        if not isinstance(records, list):
            return []
        result: list[dict[str, Any]] = []
        for raw_candidate in records:
            if not isinstance(raw_candidate, dict):
                continue
            evidence = _text_evidence_items(
                raw_candidate,
                allowed_image_ids=allowed_image_ids,
                ocr_text_by_image_id=ocr_text_by_image_id,
            )
            image_ids = list(dict.fromkeys(item["image_id"] for item in evidence))
            if not image_ids:
                continue
            result.append(
                {
                    "image_id": image_ids[0],
                    "evidence_image_ids": image_ids,
                    "evidence": evidence,
                    "reason": str(raw_candidate.get("reason") or "文本模型定位的签署候选页。"),
                }
            )
        return result

    materials: list[dict[str, Any]] = []
    raw_materials = decoded.get("contract_materials", decoded.get("materials", []))
    if isinstance(raw_materials, list):
        for index, raw_material in enumerate(raw_materials, start=1):
            if not isinstance(raw_material, dict):
                continue
            material_evidence = _text_evidence_items(
                raw_material.get("evidence")
                or {
                    "evidence_image_ids": raw_material.get("image_ids", []),
                    "evidence_texts": raw_material.get("evidence_texts", []),
                },
                allowed_image_ids=allowed_image_ids,
                ocr_text_by_image_id=ocr_text_by_image_id,
            )
            image_ids = list(
                dict.fromkeys(item["image_id"] for item in material_evidence)
            )
            if not image_ids:
                continue
            materials.append(
                {
                    "material_id": str(raw_material.get("material_id") or f"m{index:03d}"),
                    "material_type": str(raw_material.get("material_type") or "其他合同/证明材料"),
                    "role": str(raw_material.get("role") or "other"),
                    "image_ids": image_ids,
                    "evidence": material_evidence,
                }
            )
    return {
        "summary": summary.strip(),
        "facts": facts,
        "framework_contract": framework_result,
        "materials": materials,
        "signature_page_candidates": normalize_candidates("signature_page_candidates"),
        "signature_date_candidates": normalize_candidates("signature_date_candidates"),
    }


_CANDIDATE_KEYWORDS = {
    "project_name": (
        ("合同", 2),
        ("协议", 2),
        ("项目", 2),
        ("名称", 2),
    ),
    "parties": (
        ("甲方", 5),
        ("乙方", 5),
        ("委托方", 4),
        ("受托方", 4),
        ("法定代表人", 3),
    ),
    "contract_amount": (
        ("合同金额", 7),
        ("总金额", 6),
        ("合同价", 5),
        ("总价", 4),
        ("价款", 4),
        ("人民币", 3),
        ("万元", 3),
        ("元", 2),
    ),
    "implementation_time": (
        ("服务期限", 7),
        ("合同期限", 6),
        ("实施时间", 6),
        ("履行期限", 5),
        ("服务期", 4),
        ("合同生效", 3),
    ),
    "service_content": (
        ("服务内容", 8),
        ("委托事项", 7),
        ("委托内容", 7),
        ("服务范围", 6),
        ("工作内容", 5),
        ("运维", 4),
        ("运营", 3),
        ("维护", 3),
        ("管理", 2),
    ),
    "signature_page": (
        ("甲方", 4),
        ("乙方", 4),
        ("法人", 4),
        ("法定代表人", 5),
        ("授权代表", 5),
        ("签字", 5),
        ("签章", 5),
        ("盖章", 5),
        ("公章", 5),
    ),
    "signature_date": (
        ("签署日期", 8),
        ("签订日期", 8),
        ("合同日期", 6),
        ("签署", 3),
        ("年", 2),
        ("月", 2),
        ("日", 2),
    ),
    "framework_contract": (
        ("框架合同", 8),
        ("框架协议", 8),
        ("框架", 5),
        ("采购订单", 5),
        ("订单", 3),
        ("结算", 4),
        ("发票", 3),
        ("结算单据", 5),
    ),
}


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _candidate_terms(key: str, case_title: str) -> tuple[tuple[str, int], ...]:
    terms = list(_CANDIDATE_KEYWORDS[key])
    title = re.sub(r"^\s*21(?:\.\d+)*\s*", "", case_title)
    compact_title = _compact_text(title)
    if compact_title:
        terms.insert(0, (compact_title, 8))
    for token in re.findall(r"[\u4e00-\u9fff]{2,}", title):
        compact_token = _compact_text(token)
        if compact_token and all(compact_token != item[0] for item in terms):
            terms.append((compact_token, 3))
    return tuple(terms)


def _score_text_candidate(
    key: str,
    text: str,
    *,
    case_title: str,
    position: int,
    total_positions: int,
) -> tuple[int, list[str]]:
    compact = _compact_text(text)
    score = 0
    signals: list[str] = []
    for term, weight in _candidate_terms(key, case_title):
        if term and term in compact:
            score += weight
            signals.append(term)
    if key in {"amount", "contract_amount"} and re.search(
        r"(?:人民币|[¥￥])?\s*[0-9][0-9,]*(?:\.\d+)?\s*(?:万元|元)",
        text,
    ):
        score += 6
        signals.append("金额数字")
    date_count = len(
        re.findall(
            r"(?:19|20)\d{2}\s*(?:年|[./-])\s*\d{1,2}\s*(?:月|[./-])\s*\d{1,2}",
            text,
        )
    )
    if key == "implementation_time" and (
        date_count >= 2 or re.search(r"(?:至|到|起止|期间)", text)
    ):
        score += 7
        signals.append("日期范围")
    if key == "parties" and "甲方" in text and "乙方" in text:
        score += 6
        signals.append("甲乙方共现")
    if key in {"signature_page", "signature_date"}:
        signature_terms = sum(
            term in compact
            for term in ("甲方", "乙方", "签字", "盖章", "公章", "法人", "授权代表")
        )
        if signature_terms >= 2:
            score += 6
            signals.append("签署信号共现")
        if signature_terms >= 1 and total_positions > 1 and position / (
            total_positions - 1
        ) >= 0.75:
            score += 6
            signals.append("合同尾部位置")
        party_signals = sum(term in compact for term in ("甲方", "乙方"))
        execution_signals = sum(
            term in compact
            for term in ("签字", "签章", "盖章", "公章", "法人", "授权代表")
        )
        date_signals = sum(term in compact for term in ("年", "月", "日"))
        near_tail = (
            total_positions > 1
            and position / (total_positions - 1) >= 0.75
        )
        if party_signals >= 2 and execution_signals >= 2:
            score += 12
            signals.append("双方及签署要素共现")
        if (
            key == "signature_page"
            and party_signals >= 2
            and execution_signals >= 2
            and date_signals >= 3
        ):
            score += 10
            signals.append("双方签署日期栏共现")
        if (
            key == "signature_date"
            and party_signals >= 2
            and date_signals >= 3
            and near_tail
        ):
            score += 10
            signals.append("双方日期栏共现")
        if key == "signature_date" and date_signals >= 3 and near_tail:
            score += 8
            signals.append("完整日期栏")
    if key == "project_name" and total_positions > 1 and position / (
        total_positions - 1
    ) <= 0.25:
        score += 3
        signals.append("合同前部位置")
    if key == "service_content" and re.search(
        r"(?:^|\n)\s*(?:第[一二三四五六七八九十百千万0-9]+条|[0-9]+(?:\.[0-9]+)+)",
        text,
    ):
        score += 4
        signals.append("条款标题")
    if key == "framework_contract" and (
        "框架" in compact and any(term in compact for term in ("订单", "结算", "发票"))
    ):
        score += 7
        signals.append("框架及结算信号共现")
    return score, signals


def _fallback_candidate_image_ids(
    key: str,
    images: list[dict[str, Any]],
) -> list[str]:
    if not images:
        return []
    if key in {"signature_page", "signature_date"}:
        selected = images[-PERFORMANCE_CANDIDATE_TOP_K :]
    elif key == "project_name":
        selected = images[:PERFORMANCE_CANDIDATE_TOP_K]
    elif key == "framework_contract":
        selected = images[:1]
    else:
        selected = [
            *images[: max(1, PERFORMANCE_CANDIDATE_TOP_K - 1)],
            *images[-1:],
        ]
    return list(dict.fromkeys(str(image.get("image_id")) for image in selected))


def _select_performance_candidates(
    images: list[dict[str, Any]],
    ocr_entries: list[dict[str, Any]],
    *,
    case_title: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    image_positions = {
        str(image.get("image_id")): index
        for index, image in enumerate(images)
    }
    text_candidates_by_key: dict[str, list[dict[str, Any]]] = {}
    selected_image_ids_by_key: dict[str, list[str]] = {}
    text_keys = tuple(_CANDIDATE_KEYWORDS)
    total_positions = max(len(images), 1)
    for key in text_keys:
        text_candidates: list[dict[str, Any]] = []
        image_scores: dict[str, tuple[int, list[str]]] = {}
        for entry in ocr_entries:
            image_id = str(entry["image_id"])
            position = image_positions.get(image_id, 0)
            page_text = "\n".join(
                block["text"] for block in entry["blocks"] if block.get("text")
            )
            if page_text:
                page_score, page_signals = _score_text_candidate(
                    key,
                    page_text,
                    case_title=case_title,
                    position=position,
                    total_positions=total_positions,
                )
                if page_score > 0:
                    image_scores[image_id] = (page_score, page_signals)
            for block in entry["blocks"]:
                score, signals = _score_text_candidate(
                    key,
                    block["text"],
                    case_title=case_title,
                    position=position,
                    total_positions=total_positions,
                )
                if score <= 0:
                    continue
                candidate = {
                    "image_id": image_id,
                    "ocr_block_id": block["block_id"],
                    "text": block["text"],
                    "score": score,
                    "signals": signals,
                    "source": entry["source"],
                }
                text_candidates.append(candidate)
                previous = image_scores.get(image_id)
                if previous is None or score > previous[0]:
                    image_scores[image_id] = (score, signals)
        text_candidates.sort(
            key=lambda item: (
                -item["score"],
                image_positions.get(item["image_id"], 0),
                item["ocr_block_id"],
            )
        )
        selected_image_ids = [
            image_id
            for image_id, _score_and_signals in sorted(
                image_scores.items(),
                key=lambda item: (
                    -item[1][0],
                    image_positions.get(item[0], 0),
                ),
            )[:PERFORMANCE_CANDIDATE_TOP_K]
        ]
        if not selected_image_ids:
            selected_image_ids = _fallback_candidate_image_ids(key, images)
        ranked_selected_text = [
            candidate
            for candidate in text_candidates
            if candidate["image_id"] in selected_image_ids
        ]
        selected_text: list[dict[str, Any]] = []
        selected_block_keys: set[tuple[str, str]] = set()
        # Keep at least one high-scoring text block from each selected page when
        # possible. OCR often puts the party labels, stamps, and date in
        # separate blocks on the same signature page.
        for image_id in selected_image_ids:
            candidate = next(
                (
                    item
                    for item in ranked_selected_text
                    if item["image_id"] == image_id
                ),
                None,
            )
            if candidate is None:
                continue
            block_key = (candidate["image_id"], candidate["ocr_block_id"])
            selected_text.append(candidate)
            selected_block_keys.add(block_key)
        for candidate in ranked_selected_text:
            if len(selected_text) >= PERFORMANCE_CANDIDATE_TOP_K:
                break
            block_key = (candidate["image_id"], candidate["ocr_block_id"])
            if block_key in selected_block_keys:
                continue
            selected_text.append(candidate)
            selected_block_keys.add(block_key)
        selected_text = selected_text[:PERFORMANCE_CANDIDATE_TOP_K]
        text_candidates_by_key[key] = selected_text
        selected_image_ids_by_key[key] = selected_image_ids

    candidate_evidence: dict[str, dict[str, Any]] = {}
    for key in text_keys:
        selected_text = text_candidates_by_key[key]
        selected_image_ids = selected_image_ids_by_key[key]
        if not selected_image_ids:
            selected_image_ids = _fallback_candidate_image_ids(key, images)
        candidate_evidence[key] = {
            "text_blocks": selected_text,
            "image_ids": list(dict.fromkeys(selected_image_ids)),
            "selection_reason": (
                "按关键词共现、条款/合同位置和候选分数保留 Top-K。"
                if selected_text
                else "当前没有可用 OCR 命中，按合同前部/尾部位置保留有限候选。"
            ),
        }
    selected_ids = {
        image_id
        for evidence in candidate_evidence.values()
        for image_id in evidence["image_ids"]
    }
    selected_images = [
        image
        for image in images
        if str(image.get("image_id")) in selected_ids
    ]
    if not selected_images and images:
        selected_images = images[:PERFORMANCE_CANDIDATE_TOP_K]
    return candidate_evidence, selected_images


_DATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*(?:年|[./-])\s*\d{1,2}\s*(?:月|[./-])\s*\d{1,2}"
)
_AMOUNT_RE = re.compile(
    r"(?:人民币\s*(?:[零〇一二三四五六七八九十百千万亿拾佰仟壹贰叁肆伍陆柒捌玖]+(?:元|万元)"
    r"|[¥￥]\s*\d[\d,]*(?:\.\d+)?)"
    r"|[¥￥]\s*\d[\d,]*(?:\.\d+)?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:万元|元))"
)


def _ocr_fact(
    *,
    name: str,
    value: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "present",
        "value": value,
        "source": "mineru_ocr",
        "text_block_ids": [candidate["ocr_block_id"]],
        "evidence_image_ids": [candidate["image_id"]],
    }


def _extract_ocr_facts(
    candidate_evidence: dict[str, dict[str, Any]],
    *,
    ocr_available: bool,
    ocr_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "project_name": [],
        "parties": [],
        "contract_amount": [],
        "implementation_time": [],
        "framework_signal": {
            "status": "uncertain" if not ocr_available else "not_detected",
            "reason": (
                "没有可用 OCR 文本，无法进行框架合同预筛。"
                if not ocr_available
                else "OCR 候选中未发现足以单独认定框架合同的共现信号。"
            ),
            "text_block_ids": [],
            "evidence_image_ids": [],
        },
    }
    project_candidates = candidate_evidence["project_name"]["text_blocks"]
    for candidate in project_candidates:
        text = candidate["text"]
        if "合同" in text or "协议" in text or "项目" in text:
            facts["project_name"].append(
                _ocr_fact(name="项目名称/合同名称候选", value=text, candidate=candidate)
            )
    party_candidates = list(candidate_evidence["parties"]["text_blocks"])
    party_image_ids = set(candidate_evidence["parties"].get("image_ids", []))
    if ocr_entries:
        known_party_blocks = {
            (str(candidate.get("image_id")), str(candidate.get("ocr_block_id")))
            for candidate in party_candidates
        }
        for entry in ocr_entries:
            image_id = str(entry.get("image_id"))
            if image_id not in party_image_ids:
                continue
            for block in entry.get("blocks", []):
                block_id = str(block.get("block_id"))
                block_key = (image_id, block_id)
                if block_key in known_party_blocks:
                    continue
                party_candidates.append(
                    {
                        "image_id": image_id,
                        "ocr_block_id": block_id,
                        "text": block.get("text", ""),
                        "source": entry.get("source", "unknown"),
                    }
                )
                known_party_blocks.add(block_key)
    party_pattern = re.compile(r"(甲方|乙方)\s*[:：]\s*[【\[]?([^；;\]】\n]+)")
    for index, candidate in enumerate(party_candidates):
        candidate_texts = [candidate["text"]]
        if candidate["text"].strip().endswith((":", "：")):
            next_candidate = next(
                (
                    item
                    for item in party_candidates[index + 1 :]
                    if item.get("image_id") == candidate.get("image_id")
                ),
                None,
            )
            if next_candidate is not None:
                candidate_texts.append(
                    f"{candidate['text']}{next_candidate.get('text', '')}"
                )
        for candidate_text in candidate_texts:
            for party, value in party_pattern.findall(candidate_text):
                fact_candidate = {
                    **candidate,
                    "text": candidate_text,
                }
                party_fact = _ocr_fact(
                    name=party,
                    value=value.strip(),
                    candidate=fact_candidate,
                )
                if party_fact not in facts["parties"]:
                    facts["parties"].append(party_fact)
    for candidate in candidate_evidence["contract_amount"]["text_blocks"]:
        match = _AMOUNT_RE.search(candidate["text"])
        if match:
            facts["contract_amount"].append(
                _ocr_fact(
                    name="合同金额",
                    value=match.group(0).strip(),
                    candidate=candidate,
                )
            )
    for candidate in candidate_evidence["implementation_time"]["text_blocks"]:
        dates = _DATE_RE.findall(candidate["text"])
        if len(dates) >= 2 or ("至" in candidate["text"] and dates):
            facts["implementation_time"].append(
                _ocr_fact(
                    name="实施时间/服务期限",
                    value=candidate["text"],
                    candidate=candidate,
                )
            )
    framework_candidates = candidate_evidence["framework_contract"]["text_blocks"]
    for candidate in framework_candidates:
        compact = _compact_text(candidate["text"])
        if "框架" in compact and any(
            term in compact for term in ("订单", "结算", "发票")
        ):
            signal = facts["framework_signal"]
            signal["status"] = "suspected"
            signal["reason"] = "OCR 中同时出现框架合同信号及订单/结算/发票信号，需模型复核。"
            signal["text_block_ids"].append(candidate["ocr_block_id"])
            signal["evidence_image_ids"].append(candidate["image_id"])
    return facts


def _performance_template(extraction_result: dict[str, Any]) -> dict[str, Any] | None:
    templates = extraction_result.get("templates", [])
    if not isinstance(templates, list):
        return None
    target = normalize_module_title("业绩情况表")
    for template in templates:
        if not isinstance(template, dict):
            continue
        if normalize_module_title(template.get("name", "")) == target:
            return template
    return None


def _find_performance_sections(
    document: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    sections, sections_by_id, _images_by_block_id, _images_by_id = _materialized_sections(
        document
    )
    root_candidates = [
        section
        for section in sections
        if section.get("parent_section_id") is None
        and normalize_module_title(section.get("title", ""))
        == normalize_module_title("业绩情况表")
    ]
    if len(root_candidates) != 1:
        return None, None, None
    root = root_candidates[0]
    root_id = str(root.get("section_id", ""))
    children = sorted(
        [
            section
            for section in sections
            if str(section.get("parent_section_id")) == root_id
        ],
        key=lambda section: (
            section.get("start_order", section.get("order", 0)),
            str(section.get("section_id", "")),
        ),
    )
    case_candidates = [
        section
        for section in children
        if normalize_module_title(section.get("title", ""))
        == normalize_module_title(PERFORMANCE_CASE_TITLE)
    ]
    if len(case_candidates) != 1:
        return root, None, None
    return root, case_candidates[0], sections_by_id


def _find_all_performance_sections(
    document: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    sections, sections_by_id, _images_by_block_id, _images_by_id = _materialized_sections(
        document
    )
    root_candidates = [
        section
        for section in sections
        if section.get("parent_section_id") is None
        and normalize_module_title(section.get("title", ""))
        == normalize_module_title("21 业绩情况表")
    ]
    if len(root_candidates) != 1:
        return None, [], sections_by_id
    root = root_candidates[0]
    root_id = str(root.get("section_id", ""))
    cases = [
        section
        for section in sections
        if str(section.get("parent_section_id")) == root_id
        and PERFORMANCE_CASE_NUMBER_RE.match(str(section.get("title", "")))
    ]
    cases.sort(
        key=lambda section: (
            section.get("start_order", section.get("order", 0)),
            str(section.get("section_id", "")),
        )
    )
    return root, cases, sections_by_id


def _table_row_for_case(
    document: dict[str, Any],
    root: dict[str, Any],
    *,
    case_title: str = PERFORMANCE_CASE_TITLE,
) -> tuple[dict[str, Any] | None, int | None]:
    direct_block_ids = root.get("direct_block_ids", [])
    if not isinstance(direct_block_ids, list):
        direct_block_ids = []
    tables = document.get("tables", [])
    if not isinstance(tables, list):
        return None, None
    target = normalize_module_title(case_title)
    candidates: list[tuple[dict[str, Any], int]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        if table.get("section_id") != root.get("section_id") and table.get(
            "block_id"
        ) not in direct_block_ids:
            continue
        rows = table.get("rows", [])
        if not isinstance(rows, list):
            continue
        for row_index, row in enumerate(rows[1:], start=1):
            if not isinstance(row, list):
                continue
            row_text = " ".join(str(cell) for cell in row)
            if target in normalize_module_title(row_text):
                candidates.append((table, row_index))
    if len(candidates) != 1:
        return None, None
    return candidates[0]


def _table_row_number(table: dict[str, Any], row_index: int) -> int:
    rows = table.get("rows", [])
    row = rows[row_index] if isinstance(rows, list) and row_index < len(rows) else []
    if isinstance(row, list) and row:
        try:
            return int(str(row[0]).strip())
        except ValueError:
            pass
    return row_index


def _order_alignment_check(
    document: dict[str, Any],
    root: dict[str, Any],
    case: dict[str, Any],
    *,
    case_title: str | None = None,
) -> dict[str, Any]:
    current_case_title = case_title or str(
        case.get("title") or PERFORMANCE_CASE_TITLE
    )
    table, row_index = _table_row_for_case(
        document,
        root,
        case_title=current_case_title,
    )
    row_number = _table_row_number(table, row_index) if table and row_index else None
    sections = document.get("sections", [])
    direct_children = sorted(
        [
            section
            for section in sections
            if isinstance(section, dict)
            and str(section.get("parent_section_id")) == str(root.get("section_id"))
        ],
        key=lambda section: (
            section.get("start_order", section.get("order", 0)),
            str(section.get("section_id", "")),
        ),
    )
    try:
        child_position = next(
            index + 1
            for index, section in enumerate(direct_children)
            if str(section.get("section_id")) == str(case.get("section_id"))
        )
    except StopIteration:
        child_position = None

    evidence: list[dict[str, Any]] = []
    if table is not None:
        evidence.append(
            {
                "kind": "structured_table_row",
                "section_id": root.get("section_id"),
                "table_id": table.get("table_id"),
                "block_id": table.get("block_id"),
                "row_index": row_index,
                "description": f"21 业绩情况表中与 {current_case_title} 项目名称匹配的当前行",
            }
        )
    evidence.append(
        {
            "kind": "structured_section",
            "section_id": case.get("section_id"),
            "section_path": case.get("path", []),
            "description": f"{current_case_title} 业绩材料子章节边界",
        }
    )
    if table is None or child_position is None:
        status = "uncertain"
        reason = (
            f"无法从 21 业绩情况表行和 {current_case_title} 子章节边界可靠确认一一对应关系。"
        )
    elif row_number == child_position:
        status = "pass"
        reason = (
            f"业绩情况表第 {row_number} 行对应 {current_case_title} 子章节第 "
            f"{child_position} 项。"
        )
    else:
        status = "fail"
        reason = (
            f"业绩情况表行号 {row_number} 与 {current_case_title} 子章节顺序 "
            f"{child_position} 不一致。"
        )
    return {
        "key": "order_alignment",
        "requirement": "业绩证明文件顺序应与业绩情况表一一对应",
        "status": status,
        "reason": reason,
        "evidence_image_ids": [],
        "evidence": evidence,
    }


def _table_row_fields(
    table: dict[str, Any] | None,
    row_index: int | None,
) -> dict[str, Any]:
    if table is None or row_index is None:
        return {}
    rows = table.get("rows", [])
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        return {}
    if row_index >= len(rows) or not isinstance(rows[row_index], list):
        return {}
    headers = [str(value).strip() for value in rows[0]]
    row = rows[row_index]
    return {
        header: row[index]
        for index, header in enumerate(headers)
        if header and index < len(row)
    }


def _table_field_value(fields: dict[str, Any], field: str) -> str:
    for header, value in fields.items():
        if field in normalize_module_title(header):
            return str(value or "").strip()
    return ""


_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
    "貮": 2,
}
_CHINESE_UNITS = {
    "十": 10,
    "拾": 10,
    "百": 100,
    "佰": 100,
    "千": 1000,
    "仟": 1000,
    "万": 10000,
    "萬": 10000,
    "亿": 100000000,
    "億": 100000000,
}


def _parse_chinese_number(value: str) -> Decimal | None:
    text = re.sub(r"[^零〇一二两三四五六七八九十百千万亿拾佰仟壹贰叁肆伍陆柒捌玖貮萬億]", "", value)
    if not text:
        return None
    total = 0
    section = 0
    number = 0
    for character in text:
        if character in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[character]
            continue
        unit = _CHINESE_UNITS.get(character)
        if unit is None:
            continue
        if unit >= 10000:
            section += number
            total += section * unit
            section = 0
            number = 0
        else:
            if number == 0:
                number = 1
            section += number * unit
            number = 0
    return Decimal(total + section + number)


def _parse_amount_to_wan(value: Any, *, default_unit: str | None = None) -> Decimal | None:
    text = str(value or "").replace(",", "").replace("，", "")
    number_pattern = r"[0-9]+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿拾佰仟壹贰叁肆伍陆柒捌玖貮萬億]+"
    match = re.search(rf"({number_pattern})\s*(万元|萬元|元|万|萬)?", text)
    if match is None:
        return None
    raw_number = match.group(1)
    try:
        number = Decimal(raw_number)
    except InvalidOperation:
        number = _parse_chinese_number(raw_number)
    if number is None:
        return None
    unit = match.group(2) or ("元" if re.search(r"[¥￥]", text) else default_unit)
    if unit == "元":
        return number / Decimal(10000)
    if unit in {"万元", "萬元", "万", "萬"}:
        return number
    return number


def _fixed_amount_is_comparable(value: Any) -> bool:
    """Whether an OCR/model amount is a fixed amount that can be compared.

    A percentage, per-gun fee, or other pricing formula is a valid contract
    fact but is not comparable to the performance table's total sales amount.
    It must therefore lead to ``uncertain`` rather than a false mismatch.
    """

    text = str(value or "")
    if not text.strip() or "%" in text or "％" in text:
        return False
    return _parse_amount_to_wan(text) is not None


def _amount_values_match(table_value: Any, contract_value: Any) -> bool | None:
    if not _fixed_amount_is_comparable(contract_value):
        return None
    table_amount = _parse_amount_to_wan(table_value, default_unit="万元")
    contract_amount = _parse_amount_to_wan(contract_value)
    if table_amount is None or contract_amount is None:
        return None
    return abs(table_amount - contract_amount) <= Decimal("0.01")


def _duration_values_match(table_value: Any, contract_value: Any) -> bool | None:
    table_duration = _parse_duration_months(table_value)
    contract_duration = _parse_duration_months(contract_value)
    if table_duration is None or contract_duration is None:
        return None
    return abs(table_duration - contract_duration) <= Decimal("0.5")


def _name_tokens(value: Any) -> set[str]:
    text = re.sub(r"^21\.[1-6]\s*", "", str(value or ""))
    compact = _compact_text(text)
    return set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", compact))


def _semantic_name_match(left: Any, right: Any) -> bool:
    left_text = _compact_text(left)
    right_text = _compact_text(right)
    if not left_text or not right_text:
        return False
    if left_text in right_text or right_text in left_text:
        return True
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    return overlap >= 1 and overlap / min(len(left_tokens), len(right_tokens)) >= 0.5


def _parse_duration_months(value: Any) -> Decimal | None:
    text = str(value or "")
    dates = _DATE_RE.findall(text)
    if len(dates) >= 2:
        parsed_dates: list[date] = []
        for date_text in dates[:2]:
            match = re.search(r"(\d{4})\s*(?:年|[./-])\s*(\d{1,2})\s*(?:月|[./-])\s*(\d{1,2})", date_text)
            if match is None:
                continue
            try:
                parsed_dates.append(
                    date(
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                    )
                )
            except ValueError:
                continue
        if len(parsed_dates) >= 2 and parsed_dates[1] > parsed_dates[0]:
            return Decimal((parsed_dates[1] - parsed_dates[0]).days) / Decimal("30.4375")
    duration_match = re.search(
        r"([0-9]+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿拾佰仟壹贰叁肆伍陆柒捌玖貮萬億]+)\s*(年|个月|月)",
        text,
    )
    if duration_match:
        raw_number = duration_match.group(1)
        try:
            number = Decimal(raw_number)
        except InvalidOperation:
            number = _parse_chinese_number(raw_number)
        if number is not None:
            return number * (Decimal(12) if duration_match.group(2) == "年" else Decimal(1))
    return None


def _table_cell_evidence(
    table: dict[str, Any] | None,
    row_index: int | None,
    field_name: str,
) -> list[dict[str, Any]]:
    fields = _table_row_fields(table, row_index)
    for column_index, (header, value) in enumerate(fields.items()):
        if field_name in normalize_module_title(header):
            return [
                {
                    "kind": "structured_table_cell",
                    "table_id": table.get("table_id") if table else None,
                    "row_index": row_index,
                    "column_index": column_index,
                    "header": header,
                    "value": value,
                    "description": "21 业绩情况表当前业绩行字段",
                }
            ]
    return []


def _table_consistency_check(
    key: str,
    *,
    table: dict[str, Any] | None,
    row_index: int | None,
    table_field_name: str,
    contract_records: list[dict[str, Any]],
    images_by_id: dict[str, dict[str, Any]],
    matcher: Any,
) -> dict[str, Any]:
    table_value = _table_field_value(
        _table_row_fields(table, row_index), table_field_name
    )
    contract_records = [
        record
        for record in contract_records
        if isinstance(record, dict)
        and record.get("status") == "present"
        and record.get("evidence")
    ]
    table_evidence = _table_cell_evidence(table, row_index, table_field_name)
    contract_evidence = _text_fact_evidence(
        contract_records,
        images_by_id=images_by_id,
    )
    evidence = [*table_evidence, *contract_evidence]
    evidence_image_ids = list(
        dict.fromkeys(item["image_id"] for item in contract_evidence)
    )
    definition = _CHECK_DEFINITIONS[key]
    if not table_value:
        status = "uncertain"
        reason = "业绩情况表当前行没有可读取的对应字段。"
    elif not contract_records:
        status = "uncertain"
        reason = "合同文本模型没有提供该字段的可核验 OCR 原文证据。"
    else:
        comparisons = [
            matcher(table_value, record.get("value"))
            for record in contract_records
        ]
        if any(comparison is True for comparison in comparisons):
            status = "pass"
            reason = f"业绩表值“{table_value}”与合同事实语义/数值一致。"
        elif any(comparison is None for comparison in comparisons):
            status = "uncertain"
            values = "；".join(str(record.get("value")) for record in contract_records[:3])
            reason = (
                f"业绩表值“{table_value}”与合同事实“{values}”缺少可直接比较的同口径信息，"
                "不能可靠判定一致或不一致。"
            )
        else:
            status = "fail"
            values = "；".join(str(record.get("value")) for record in contract_records[:3])
            reason = f"业绩表值“{table_value}”与合同事实“{values}”明显不一致。"
    return {
        "key": key,
        "requirement": definition,
        "status": status,
        "reason": reason,
        "evidence_image_ids": evidence_image_ids,
        "evidence": evidence,
        "table_value": table_value,
        "contract_values": [record.get("value") for record in contract_records],
        "source": "structured_table_and_text_llm_full_ocr",
    }


def _table_consistency_checks(
    *,
    table: dict[str, Any] | None,
    row_index: int | None,
    text_facts: dict[str, Any],
    images_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        "project_name": _table_consistency_check(
            "table_project_name_consistency",
            table=table,
            row_index=row_index,
            table_field_name="项目名称",
            contract_records=text_facts.get("project_name", []),
            images_by_id=images_by_id,
            matcher=_semantic_name_match,
        ),
        "counterparty": _table_consistency_check(
            "table_counterparty_consistency",
            table=table,
            row_index=row_index,
            table_field_name="最终用户",
            contract_records=text_facts.get("parties", []),
            images_by_id=images_by_id,
            matcher=_semantic_name_match,
        ),
        "contract_amount": _table_consistency_check(
            "table_amount_consistency",
            table=table,
            row_index=row_index,
            table_field_name="销售金额",
            contract_records=text_facts.get("contract_amount", []),
            images_by_id=images_by_id,
            matcher=_amount_values_match,
        ),
        "implementation_time": _table_consistency_check(
            "table_time_consistency",
            table=table,
            row_index=row_index,
            table_field_name="供货数量",
            contract_records=text_facts.get("implementation_time", []),
            images_by_id=images_by_id,
            matcher=_duration_values_match,
        ),
    }


def _evidence_ids(value: Any, allowed_image_ids: set[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AttachmentReviewError("业绩合同检查响应中的 evidence_image_ids 不是数组。")
    result: list[str] = []
    for item in value:
        image_id = str(item)
        if image_id not in allowed_image_ids:
            raise AttachmentReviewError(
                f"业绩合同检查响应引用了不属于 21.1/current 21.x 的 image_id：{image_id}。"
            )
        if image_id not in result:
            result.append(image_id)
    return result


def _image_evidence(
    image_ids: list[str],
    images_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "kind": "image",
            "image_id": image_id,
            "block_id": images_by_id[image_id].get("block_id"),
            "section_id": images_by_id[image_id].get("section_id"),
            "section_path": images_by_id[image_id].get("section_path", []),
            "description": (
                f"{images_by_id[image_id].get('section_path', [image_id])[-1]} "
                "子章节中的合同/证明材料图片"
            ),
        }
        for image_id in image_ids
        if image_id in images_by_id
    ]


def _normalize_materials(
    value: Any,
    *,
    allowed_image_ids: set[str],
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AttachmentReviewError("业绩合同检查响应中的 materials 不是数组。")
    materials: list[dict[str, Any]] = []
    for index, material in enumerate(value, start=1):
        if not isinstance(material, dict):
            raise AttachmentReviewError("业绩合同检查 materials 项不是 JSON 对象。")
        material_type = material.get("material_type")
        if not isinstance(material_type, str) or not material_type.strip():
            raise AttachmentReviewError("业绩合同检查 material_type 无效。")
        image_ids = _evidence_ids(material.get("image_ids"), allowed_image_ids)
        facts = material.get("facts", [])
        if not isinstance(facts, list):
            raise AttachmentReviewError("业绩合同检查材料缺少 facts 数组。")
        normalized_facts: list[dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict):
                raise AttachmentReviewError("业绩合同检查 fact 不是 JSON 对象。")
            name = fact.get("name")
            status = fact.get("status")
            if not isinstance(name, str) or not name.strip() or status not in {"present", "absent", "uncertain"}:
                raise AttachmentReviewError("业绩合同检查 fact 字段无效。")
            normalized_fact = {
                "name": name.strip(),
                "status": status,
                "evidence_image_ids": _evidence_ids(
                    fact.get("evidence_image_ids"), allowed_image_ids
                ),
            }
            for optional_key in ("value", "reason"):
                optional_value = fact.get(optional_key)
                if optional_value is not None:
                    if not isinstance(optional_value, str):
                        raise AttachmentReviewError(
                            "业绩合同检查 fact 可选字段类型无效。"
                        )
                    normalized_fact[optional_key] = optional_value
            normalized_facts.append(normalized_fact)
        materials.append(
            {
                "material_id": str(material.get("material_id") or f"m{index:03d}"),
                "material_type": material_type.strip(),
                "role": str(material.get("role") or "other"),
                "image_ids": image_ids,
                "facts": normalized_facts,
            }
        )
    return materials


def _uncertain_check(key: str, reason: str) -> dict[str, Any]:
    return {
        "key": key,
        "requirement": _CHECK_DEFINITIONS[key],
        "status": "uncertain",
        "reason": reason,
        "evidence_image_ids": [],
        "evidence": [],
    }


def _ocr_fact_check(
    key: str,
    *,
    ocr_facts: dict[str, Any],
    images_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if key not in {"contract_amount", "implementation_time"}:
        return None
    facts = ocr_facts.get(key)
    if not isinstance(facts, list) or not facts:
        return None
    evidence_image_ids: list[str] = []
    values: list[str] = []
    text_block_ids: list[str] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        value = fact.get("value")
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        for image_id in fact.get("evidence_image_ids", []):
            if image_id not in evidence_image_ids:
                evidence_image_ids.append(image_id)
        for block_id in fact.get("text_block_ids", []):
            if block_id not in text_block_ids:
                text_block_ids.append(block_id)
    if not values:
        return None
    return {
        "key": key,
        "requirement": _CHECK_DEFINITIONS[key],
        "status": "pass",
        "reason": f"MinerU OCR 在合同关键候选文本中识别到：{'；'.join(values[:2])}。",
        "evidence_image_ids": evidence_image_ids,
        "evidence": _image_evidence(evidence_image_ids, images_by_id),
        "source": "mineru_ocr",
        "text_block_ids": text_block_ids,
    }


def _normalize_model_result(
    raw: Any,
    *,
    allowed_image_ids: set[str],
    images_by_id: dict[str, dict[str, Any]],
    order_check: dict[str, Any],
    ocr_facts: dict[str, Any],
) -> dict[str, Any]:
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, dict):
        raise AttachmentReviewError("业绩合同检查响应不是 JSON 对象。")
    summary = decoded.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AttachmentReviewError("业绩合同检查响应缺少 summary。")
    framework = decoded.get("framework_contract")
    if not isinstance(framework, dict):
        framework = {}
    framework_status = framework.get("status")
    if framework_status not in _FRAMEWORK_STATUS:
        framework_status = "uncertain"
    framework_evidence_ids = _evidence_ids(
        framework.get("evidence_image_ids"), allowed_image_ids
    )
    framework_result = {
        "status": framework_status,
        "reason": str(framework.get("reason") or "无法从当前图片可靠确认合同类型。"),
        "evidence_image_ids": framework_evidence_ids,
        "evidence": _image_evidence(framework_evidence_ids, images_by_id),
    }

    raw_checks = decoded.get("checks", [])
    if not isinstance(raw_checks, list):
        raise AttachmentReviewError("业绩合同检查响应中的 checks 不是数组。")
    checks_by_key: dict[str, dict[str, Any]] = {}
    for raw_check in raw_checks:
        if not isinstance(raw_check, dict):
            raise AttachmentReviewError("业绩合同检查 check 不是 JSON 对象。")
        key = raw_check.get("key")
        if key not in _CHECK_KEYS:
            continue
        if key in checks_by_key:
            continue
        status = raw_check.get("status")
        if status not in _CHECK_STATUS:
            status = "uncertain"
        evidence_image_ids = _evidence_ids(
            raw_check.get("evidence_image_ids"), allowed_image_ids
        )
        checks_by_key[key] = {
            "key": key,
            "requirement": _CHECK_DEFINITIONS[key],
            "status": status,
            "reason": str(raw_check.get("reason") or "当前信息不足以形成可靠判断。"),
            "evidence_image_ids": evidence_image_ids,
            "evidence": _image_evidence(evidence_image_ids, images_by_id),
        }

    checks: list[dict[str, Any]] = [order_check]
    for key in (
        "service_content",
        "implementation_time",
        "contract_amount",
        "signature_page",
        "signature_date",
    ):
        check = checks_by_key.get(key)
        if check is None or check["status"] == "uncertain":
            check = _ocr_fact_check(
                key,
                ocr_facts=ocr_facts,
                images_by_id=images_by_id,
            ) or check
        checks.append(
            check or _uncertain_check(key, "模型未提供该项可核验结论。")
        )
    if framework_status in {"yes", "uncertain"}:
        if framework_status == "uncertain":
            checks.append(
                _uncertain_check(
                    "framework_settlement_material",
                    "合同是否为框架合同无法可靠确认，因此无法确认是否需要对应结算材料。",
                )
            )
        else:
            checks.append(
                checks_by_key.get("framework_settlement_material")
                or _uncertain_check(
                    "framework_settlement_material",
                    "已识别为框架合同，但模型未提供对应发票、订单或结算单据的可核验结论。",
                )
            )

    status_values = [check["status"] for check in checks]
    status = "fail" if "fail" in status_values else (
        "uncertain" if "uncertain" in status_values else "pass"
    )
    return {
        "status": status,
        "summary": summary.strip(),
        "materials": _normalize_materials(
            decoded.get("materials"), allowed_image_ids=allowed_image_ids
        ),
        "framework_contract": framework_result,
        "checks": checks,
        "checks_by_key": {check["key"]: check for check in checks},
    }


def _base_result(
    template: dict[str, Any],
    case: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    root: dict[str, Any],
) -> dict[str, Any]:
    case_title = str(case.get("title") or PERFORMANCE_CASE_TITLE)
    return {
        "case_type": case_title,
        "template_id": str(template.get("id", "")),
        "template_name": str(template.get("name", "")),
        "bid_module_name": case.get("title", ""),
        "bid_section_id": case.get("section_id"),
        "performance_section_path": case.get("path", []),
        "evidence_scope": {
            "root_section_id": root.get("section_id"),
            "case_section_id": case.get("section_id"),
            "case_title": case_title,
            "image_ids": [str(image.get("image_id")) for image in images],
        },
        "image_ids": [str(image.get("image_id")) for image in images],
        "model_image_ids": [],
        "candidate_evidence": {},
        "ocr_facts": {},
        "ocr_scan": {},
        "table_row": {},
        "table_consistency": {},
        "status": "uncertain",
        "business_status": "not_run",
        "execution_status": "pending",
        "summary": "当前信息不足以完成该 21.x 业绩合同材料检查。",
        "materials": [],
        "framework_contract": {
            "status": "uncertain",
            "reason": "尚未完成合同类型判断。",
            "evidence_image_ids": [],
            "evidence": [],
        },
        "checks": [],
        "checks_by_key": {},
        "llm_elapsed_ms": None,
    }


def _text_fact_evidence(
    records: list[dict[str, Any]],
    *,
    images_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for record in records:
        for item in record.get("evidence", []):
            image_id = str(item.get("image_id"))
            if image_id not in images_by_id:
                continue
            evidence.append(
                {
                    **_image_evidence([image_id], images_by_id)[0],
                    "kind": "ocr_text",
                    "ocr_text": item.get("evidence_text", ""),
                    "source": "structured_document",
                }
            )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        key = (str(item.get("image_id")), str(item.get("ocr_text")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _candidate_evidence_from_text_result(
    text_result: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    facts = text_result.get("facts", {})
    candidate_evidence: dict[str, dict[str, Any]] = {}
    for key in (
        "project_name",
        "parties",
        "contract_amount",
        "implementation_time",
        "service_content",
    ):
        records = facts.get(key, []) if isinstance(facts, dict) else []
        text_blocks: list[dict[str, Any]] = []
        image_ids: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            for evidence in record.get("evidence", []):
                image_id = str(evidence.get("image_id"))
                if image_id not in image_ids:
                    image_ids.append(image_id)
                text_blocks.append(
                    {
                        "image_id": image_id,
                        "ocr_block_id": None,
                        "text": evidence.get("evidence_text", ""),
                        "source": "text_llm_full_ocr",
                    }
                )
        candidate_evidence[key] = {
            "text_blocks": text_blocks,
            "image_ids": image_ids,
            "selection_reason": "完整 OCR 文本由文本模型统一定位，未先按关键词过滤。",
        }
    for key in ("signature_page", "signature_date"):
        candidates = text_result.get(f"{key}_candidates", [])
        text_blocks = []
        image_ids = []
        for candidate in candidates:
            for evidence in candidate.get("evidence", []):
                image_id = str(evidence.get("image_id"))
                if image_id not in image_ids:
                    image_ids.append(image_id)
                text_blocks.append(
                    {
                        "image_id": image_id,
                        "ocr_block_id": None,
                        "text": evidence.get("evidence_text", ""),
                        "reason": candidate.get("reason", ""),
                        "source": "text_llm_full_ocr",
                    }
                )
        candidate_evidence[key] = {
            "text_blocks": text_blocks,
            "image_ids": image_ids,
            "selection_reason": "文本模型基于完整 OCR 定位的签署候选页。",
        }
    return candidate_evidence


def _text_fact_check(
    key: str,
    *,
    facts: dict[str, Any],
    images_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    records = facts.get(key, []) if isinstance(facts, dict) else []
    present_records = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("status") == "present"
        and record.get("evidence")
    ]
    if key == "contract_amount":
        comparable_records = [
            record
            for record in present_records
            if _fixed_amount_is_comparable(record.get("value"))
        ]
        if not comparable_records:
            return _uncertain_check(
                key,
                "文本模型识别到的是费率、按设备计费或其他定价公式，未提供可直接核验的固定合同金额。",
            )
        present_records = comparable_records
    if not present_records:
        return _uncertain_check(
            key,
            f"文本模型未能从当前 21.x 完整 OCR 中提供可核验的{_CHECK_DEFINITIONS[key]}原文证据。",
        )
    values = [
        str(record.get("value"))
        for record in present_records
        if str(record.get("value") or "").strip()
    ]
    evidence = _text_fact_evidence(present_records, images_by_id=images_by_id)
    return {
        "key": key,
        "requirement": _CHECK_DEFINITIONS[key],
        "status": "pass",
        "reason": (
            f"文本模型从当前 21.x 完整 OCR 识别到{_CHECK_DEFINITIONS[key]}："
            f"{'；'.join(values[:2]) or '有明确原文证据'}。"
        ),
        "evidence_image_ids": list(
            dict.fromkeys(item["image_id"] for item in evidence)
        ),
        "evidence": evidence,
        "source": "text_llm_full_ocr",
    }


def _framework_settlement_check_from_text(
    *,
    text_result: dict[str, Any],
    images_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    framework_status = text_result["framework_contract"]["status"]
    if framework_status == "no":
        raise AssertionError("settlement check is not required for non-framework contract")
    settlement_materials = [
        material
        for material in text_result.get("materials", [])
        if material.get("role") == "settlement"
        or re.search(r"发票|订单|结算", material.get("material_type", ""))
    ]
    if not settlement_materials:
        return {
            **_uncertain_check(
                "framework_settlement_material",
                (
                    "文本模型识别为框架合同，但未从当前 21.x 完整 OCR 可靠识别到对应发票、"
                    "订单或结算单据。"
                    if framework_status == "yes"
                    else "合同类型本身仍 uncertain，当前 21.x 完整 OCR 也未提供可核验的结算材料。"
                ),
            ),
            "status": "fail" if framework_status == "yes" else "uncertain",
        }
    evidence = []
    for material in settlement_materials:
        evidence.extend(
            item
            for item in material.get("evidence", [])
            if item.get("image_id") in images_by_id
        )
    image_ids = list(dict.fromkeys(item["image_id"] for item in evidence))
    return {
        "key": "framework_settlement_material",
        "requirement": _CHECK_DEFINITIONS["framework_settlement_material"],
        "status": "pass" if evidence else "uncertain",
        "reason": "当前 21.x 中存在框架合同对应的结算材料 OCR 证据。"
        if evidence
        else "已识别结算材料类型，但缺少可核验 OCR 原文证据。",
        "evidence_image_ids": image_ids,
        "evidence": evidence,
        "source": "text_llm_full_ocr",
    }


def _build_full_ocr_user_prompt(
    *,
    template: dict[str, Any],
    root: dict[str, Any],
    case: dict[str, Any],
    table: dict[str, Any] | None,
    row_index: int | None,
    images: list[dict[str, Any]],
    full_ocr_text: str,
    ocr_stats: dict[str, Any],
) -> str:
    case_title = str(case.get("title") or PERFORMANCE_CASE_TITLE)
    row_text = "（未找到与当前 21.x 匹配的业绩情况表行）"
    if table is not None and row_index is not None:
        rows = table.get("rows", [])
        row = rows[row_index] if isinstance(rows, list) and row_index < len(rows) else []
        row_text = (
            f"table_id={table.get('table_id')}，block_id={table.get('block_id')}，"
            f"row_index={row_index}，内容={row}"
        )
    image_lines = "\n".join(
        f"- image_id={image.get('image_id')}，block_id={image.get('block_id')}，"
        f"章节={' / '.join(str(value) for value in image.get('section_path', []))}"
        for image in images
    ) or "（当前 21.x 子章节没有可读取图片）"
    return f"""请独立检查一份业绩合同材料：{case_title}。

=== 招标要求与关联上下文 ===
招标模板：{template.get('name', '')}
业绩情况表根章节：{root.get('title', '')}（section_id={root.get('section_id')}）
当前 21.x 对应的业绩情况表行：{row_text}
该行只用于确认顺序和项目关联，不是合同服务内容、实施时间、金额或签页的证据。

=== 当前 21.x 子章节 ===
章节名称：{case.get('title', '')}
章节路径：{' / '.join(str(value) for value in case.get('path', []))}
章节文本：
<<<PERFORMANCE_CASE_TEXT
{_section_content(case)}
PERFORMANCE_CASE_TEXT>>>

=== 当前 21.x 全部归属图片 ===
本次完整 OCR 对应的图片范围如下；必须把它们全部视为本次合同证据范围：
{image_lines}

=== 当前 21.x 完整 OCR 合同文本 ===
OCR 图片数：{ocr_stats['image_count']}
OCR 字符数：{ocr_stats['character_count']}
token 数：{ocr_stats['token_count']}（{ocr_stats['token_count_source']}；{ocr_stats['token_count_formula']}）
以下内容按图片原始顺序完整提供，不是程序按关键词筛选出的候选；不得先过滤或删除任何正文：
<<<FULL_PERFORMANCE_OCR
{full_ocr_text}
FULL_PERFORMANCE_OCR>>>

=== 输出要求 ===
只返回 JSON 对象。每条事实和每个签署候选必须有 image_id，并在 evidence_text 中逐字复制对应图片 OCR 中实际存在的原文；没有可核验原文时不要返回该事实或候选。字段格式：
{{
  "summary": "总体说明",
  "facts": {{
    "project_name": [{{"value": "", "image_id": "", "evidence_text": ""}}],
    "parties": [{{"name": "甲方或乙方", "value": "", "image_id": "", "evidence_text": ""}}],
    "contract_amount": [{{"value": "", "image_id": "", "evidence_text": ""}}],
    "implementation_time": [{{"value": "", "image_id": "", "evidence_text": ""}}],
    "service_content": [{{"value": "", "image_id": "", "evidence_text": ""}}]
  }},
  "framework_contract": {{"status": "yes | no | uncertain", "reason": "", "evidence": [{{"image_id": "", "evidence_text": ""}}]}},
  "contract_materials": [{{"material_type": "服务合同/发票/订单/结算单据等", "role": "contract | settlement | other", "image_ids": [""], "evidence_texts": ["与材料类型对应的原文"]}}],
  "signature_page_candidates": [{{"image_id": "", "reason": "", "evidence_text": ""}}],
  "signature_date_candidates": [{{"image_id": "", "reason": "", "evidence_text": ""}}]
}}
"""


def _build_signature_visual_prompt(
    *,
    case: dict[str, Any],
    candidates: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> str:
    case_title = str(case.get("title") or PERFORMANCE_CASE_TITLE)
    candidate_lines = "\n".join(
        f"- image_id={candidate.get('image_id')}，reason={candidate.get('reason')}，"
        f"OCR evidence={'; '.join(item.get('evidence_text', '') for item in candidate.get('evidence', []))}"
        for candidate in candidates
    ) or "（文本模型没有定位到带有可靠 OCR 证据的签署候选页）"
    image_lines = "\n".join(
        f"- image_id={image.get('image_id')}，资源状态={image.get('asset_status') or 'unknown'}"
        for image in images
    ) or "（没有签署候选图片）"
    return f"""请只对 {case_title} 的签署候选图片做视觉复核。

章节：{' / '.join(str(value) for value in case.get('path', []))}

文本模型从完整 OCR 定位的候选及原文证据：
{candidate_lines}

本次实际提供给视觉模型的图片：
{image_lines}

请只输出 signature_page 和 signature_date 两项 JSON。signature_page 检查双方是否实际签字和/或盖章；signature_date 检查日期栏是否实际填写且可辨认。不能仅凭 OCR 或候选理由通过日期。每项 evidence_image_ids 只能引用本次实际图片。"""


def _build_user_prompt(
    *,
    template: dict[str, Any],
    root: dict[str, Any],
    case: dict[str, Any],
    table: dict[str, Any] | None,
    row_index: int | None,
    images: list[dict[str, Any]],
    candidate_evidence: dict[str, dict[str, Any]],
    ocr_facts: dict[str, Any],
) -> str:
    case_title = str(case.get("title") or PERFORMANCE_CASE_TITLE)
    row_text = "（未找到与当前 21.x 匹配的业绩情况表行）"
    if table is not None and row_index is not None:
        rows = table.get("rows", [])
        row = rows[row_index] if isinstance(rows, list) and row_index < len(rows) else []
        row_text = (
            f"table_id={table.get('table_id')}，block_id={table.get('block_id')}，"
            f"row_index={row_index}，内容={row}"
        )
    image_lines = "\n".join(
        f"- image_id={image.get('image_id')}，block_id={image.get('block_id')}，"
        f"章节={image.get('section_path') or case.get('path', [])}，"
        f"资源状态={image.get('asset_status') or 'unknown'}"
        for image in images
    ) or "（当前 21.x 子章节没有可读取图片）"
    candidate_text_lines: list[str] = []
    for key, evidence in candidate_evidence.items():
        for candidate in evidence.get("text_blocks", []):
            candidate_text_lines.append(
                f"- check={key}，image_id={candidate.get('image_id')}，"
                f"ocr_block_id={candidate.get('ocr_block_id')}，"
                f"score={candidate.get('score')}，"
                f"text={candidate.get('text')}"
            )
    candidate_text = "\n".join(candidate_text_lines) or "（没有可用 OCR 候选文本）"
    direct_fact_lines: list[str] = []
    for key in ("project_name", "parties", "contract_amount", "implementation_time"):
        for fact in ocr_facts.get(key, []):
            if isinstance(fact, dict):
                direct_fact_lines.append(
                    f"- {key}：{fact.get('value')}；"
                    f"image_id={','.join(fact.get('evidence_image_ids', []))}；"
                    f"text_block_id={','.join(fact.get('text_block_ids', []))}"
                )
    direct_facts = "\n".join(direct_fact_lines) or "（没有可直接采用的 OCR 事实）"
    return f"""请独立检查一份业绩合同材料：{case_title}。

=== 招标要求与关联上下文 ===
招标模板：{template.get('name', '')}
业绩情况表根章节：{root.get('title', '')}（section_id={root.get('section_id')}）
{case_title} 对应的业绩情况表行：{row_text}
该行只用于确认顺序和项目关联，不是合同服务内容、实施时间、金额或签页的图片证据。

=== 当前 21.x 子章节文本 ===
章节名称：{case.get('title', '')}
章节路径：{' / '.join(str(value) for value in case.get('path', []))}
章节文本：
<<<PERFORMANCE_CASE_TEXT
{_section_content(case)}
PERFORMANCE_CASE_TEXT>>>

=== 当前 21.x 子章节图片 ===
本次消息只提供程序筛选后的当前 21.x 候选图片，禁止使用其他章节材料。
{image_lines}

=== MinerU OCR 候选文本 ===
MinerU 已扫描当前 21.x 归属图片；以下文本是程序按各检查项结合关键词共现、条款标题和图片位置保留的少量候选，不代表程序已经完成合同语义判断。
{candidate_text}

=== 可直接采用的 OCR 事实候选 ===
金额、时间、主体和项目名称只可在下列 OCR 文本确实支持时采用；签字、盖章和签署日期不能仅凭 OCR 文本通过，必须查看候选图片。
{direct_facts}

=== 输出要求 ===
返回 JSON 对象：
{{
  "status": "pass | fail | uncertain",
  "summary": "总体结论",
  "framework_contract": {{
    "status": "yes | no | uncertain",
    "reason": "判断依据",
    "evidence_image_ids": ["仅使用本次 image_id"]
  }},
  "materials": [
    {{
      "material_id": "m1",
      "material_type": "识别到的合同或证明材料类型",
      "role": "contract | settlement | other",
      "image_ids": ["属于该材料的 image_id"],
      "facts": [
        {{
          "name": "客观事实",
          "status": "present | absent | uncertain",
          "value": "可靠识别的值",
          "evidence_image_ids": ["支持事实的 image_id"]
        }}
      ]
    }}
  ],
  "checks": [
    {{"key": "service_content", "status": "pass | fail | uncertain", "reason": "依据", "evidence_image_ids": []}},
    {{"key": "implementation_time", "status": "pass | fail | uncertain", "reason": "依据", "evidence_image_ids": []}},
    {{"key": "contract_amount", "status": "pass | fail | uncertain", "reason": "依据", "evidence_image_ids": []}},
    {{"key": "signature_page", "status": "pass | fail | uncertain", "reason": "依据", "evidence_image_ids": []}},
    {{"key": "signature_date", "status": "pass | fail | uncertain", "reason": "签署页必须有可辨认的签署日期", "evidence_image_ids": []}},
    {{"key": "framework_settlement_material", "status": "pass | fail | uncertain", "reason": "仅在框架合同时填写", "evidence_image_ids": []}}
  ]
}}
"""


def _review_case(
    *,
    template: dict[str, Any],
    root: dict[str, Any],
    case: dict[str, Any],
    table: dict[str, Any] | None,
    row_index: int | None,
    images: list[dict[str, Any]],
    model_images: list[dict[str, Any]],
    candidate_evidence: dict[str, dict[str, Any]],
    ocr_facts: dict[str, Any],
    ocr_scan: dict[str, Any],
    llm: AttachmentReviewLLM,
    recorder: ComplianceExtractionRecorder | None,
) -> tuple[dict[str, Any], int, int]:
    result = _base_result(template, case, images, root=root)
    case_title = str(case.get("title") or PERFORMANCE_CASE_TITLE)
    result.update(
        {
            "model_image_ids": [
                str(image.get("image_id")) for image in model_images
            ],
            "candidate_evidence": candidate_evidence,
            "ocr_facts": ocr_facts,
            "ocr_scan": ocr_scan,
        }
    )
    user_prompt = _build_user_prompt(
        template=template,
        root=root,
        case=case,
        table=table,
        row_index=row_index,
        images=model_images,
        candidate_evidence=candidate_evidence,
        ocr_facts=ocr_facts,
    )
    model = str(getattr(llm, "model", type(llm).__name__))
    total_elapsed_ms = 0
    for attempt in range(1, ATTACHMENT_REVIEW_MAX_ATTEMPTS + 1):
        call_id: str | None = None
        call_started_at = time.perf_counter()
        try:
            if recorder is not None:
                call_id = recorder.start_llm_call(
                    batch_index=1,
                    batch_count=1,
                    attempt=attempt,
                    model=model,
                    batch={
                        "case_type": PERFORMANCE_CASE_TYPE,
                        "template_id": result["template_id"],
                        "template_name": result["template_name"],
                        "bid_module_name": result["bid_module_name"],
                        "bid_section_id": result["bid_section_id"],
                        "image_ids": result["model_image_ids"],
                    },
                )
                recorder.attach_llm_input(
                    call_id,
                    build_attachment_request_payload(
                        model=model,
                        system_prompt=PERFORMANCE_REVIEW_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                        images=model_images,
                        provider=getattr(llm, "provider", "dashscope"),
                        enable_thinking=getattr(llm, "enable_thinking", False),
                    ),
                )
            raw_output = llm.review_attachment(
                PERFORMANCE_REVIEW_SYSTEM_PROMPT,
                user_prompt,
                model_images,
            )
            parsed = _normalize_model_result(
                raw_output,
                allowed_image_ids=set(result["model_image_ids"]),
                images_by_id={
                    str(image.get("image_id")): image
                    for image in model_images
                    if image.get("image_id")
                },
                order_check=_order_alignment_check(
                    {"tables": [table] if table else [], "sections": [root, case]},
                    root,
                    case,
                ),
                ocr_facts=ocr_facts,
            )
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            total_elapsed_ms += elapsed_ms
            result.update(parsed)
            result.update(
                {
                    "business_status": parsed["status"],
                    "execution_status": "completed",
                    "llm_elapsed_ms": total_elapsed_ms,
                }
            )
            if recorder is not None and call_id is not None:
                recorder.complete_llm_call(
                    call_id,
                    parsed_objects=result,
                    schema_valid=True,
                    elapsed_ms=elapsed_ms,
                )
            return result, attempt, 1
        except Exception as exc:  # noqa: BLE001 - isolate this case
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
                    "status": "uncertain",
                    "business_status": "not_run",
                    "execution_status": "failed",
                    "summary": f"{case_title} 业绩合同检查调用失败，未形成可靠业务结论。",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "llm_elapsed_ms": total_elapsed_ms,
                }
            )
            failed_order_check = _order_alignment_check(
                {"tables": [table] if table else [], "sections": [root, case]},
                root,
                case,
            )
            failed_checks = [failed_order_check] + [
                _uncertain_check(
                    key,
                    "LLM 调用失败，当前没有形成该项的可核验证据。",
                )
                for key in (
                    "service_content",
                    "implementation_time",
                    "contract_amount",
                    "signature_page",
                    "signature_date",
                    "framework_settlement_material",
                )
            ]
            result["framework_contract"] = {
                "status": "uncertain",
                "reason": "LLM 调用失败，无法可靠判断是否为框架合同。",
                "evidence_image_ids": [],
                "evidence": [],
            }
            result["checks"] = failed_checks
            result["checks_by_key"] = {
                check["key"]: check for check in failed_checks
            }
            return result, attempt, 0
    raise AssertionError("performance review attempts unexpectedly exhausted")


def _normalize_visual_model_result(
    raw: Any,
    *,
    candidate_image_ids: set[str],
    images_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    decoded = _raw_json_object(
        raw, error_message="业绩合同签署页视觉模型响应不是 JSON 对象。"
    )
    normalized: dict[str, dict[str, Any]] = {}
    for key in ("signature_page", "signature_date"):
        raw_check = decoded.get(key, {})
        if not isinstance(raw_check, dict):
            raw_check = {}
        evidence_image_ids = _evidence_ids(
            raw_check.get("evidence_image_ids"), candidate_image_ids
        )
        status = raw_check.get("status")
        if status not in _CHECK_STATUS:
            status = "uncertain"
        normalized[key] = {
            "key": key,
            "requirement": _CHECK_DEFINITIONS[key],
            "status": status,
            "reason": str(raw_check.get("reason") or "当前签署图片不足以形成可靠判断。"),
            "evidence_image_ids": evidence_image_ids,
            "evidence": _image_evidence(evidence_image_ids, images_by_id),
            "source": "visual_llm_signature_candidates",
        }
    return normalized


def _full_ocr_case_failure_checks(
    *,
    order_check: dict[str, Any],
    reason: str,
) -> list[dict[str, Any]]:
    return [order_check] + [
        _uncertain_check(key, reason)
        for key in (
            "service_content",
            "implementation_time",
            "contract_amount",
            "signature_page",
            "signature_date",
            "framework_settlement_material",
        )
    ]


def _review_case_with_full_ocr(
    *,
    document: dict[str, Any],
    template: dict[str, Any],
    root: dict[str, Any],
    case: dict[str, Any],
    table: dict[str, Any] | None,
    row_index: int | None,
    images: list[dict[str, Any]],
    ocr_entries: list[dict[str, Any]],
    ocr_scan: dict[str, Any],
    text_llm: Any,
    visual_llm: AttachmentReviewLLM,
    recorder: ComplianceExtractionRecorder | None,
) -> tuple[dict[str, Any], int, int, int, int]:
    """Review one case with one full-OCR text call and a scoped visual call."""

    result = _base_result(template, case, images, root=root)
    case_title = str(case.get("title") or PERFORMANCE_CASE_TITLE)
    full_ocr_text, image_ranges, full_ocr_stats = _build_full_ocr_contract_text(
        images, ocr_entries
    )
    ocr_scan = {
        **ocr_scan,
        "full_text": full_ocr_text,
        "image_ranges": image_ranges,
        **full_ocr_stats,
    }
    allowed_image_ids = {
        str(image.get("image_id")) for image in images if image.get("image_id")
    }
    images_by_id = {
        str(image.get("image_id")): image
        for image in images
        if image.get("image_id")
    }
    text_prompt = _build_full_ocr_user_prompt(
        template=template,
        root=root,
        case=case,
        table=table,
        row_index=row_index,
        images=images,
        full_ocr_text=full_ocr_text,
        ocr_stats=full_ocr_stats,
    )
    text_model = str(getattr(text_llm, "model", type(text_llm).__name__))
    text_attempts = 0
    text_completed_calls = 0
    text_elapsed_ms = 0
    text_result: dict[str, Any] | None = None
    text_error: Exception | None = None
    for attempt in range(1, ATTACHMENT_REVIEW_MAX_ATTEMPTS + 1):
        text_attempts = attempt
        call_id: str | None = None
        call_started_at = time.perf_counter()
        try:
            if recorder is not None:
                call_id = recorder.start_llm_call(
                    batch_index=1,
                    batch_count=2,
                    attempt=attempt,
                    model=text_model,
                    batch={
                        "case_type": case_title,
                        "stage": "full_ocr_text_extraction",
                        "template_id": result["template_id"],
                        "bid_section_id": result["bid_section_id"],
                        "image_ids": [str(image.get("image_id")) for image in images],
                        "ocr_character_count": full_ocr_stats["character_count"],
                        "ocr_token_count": full_ocr_stats["token_count"],
                        "ocr_token_count_source": full_ocr_stats[
                            "token_count_source"
                        ],
                    },
                )
                recorder.attach_llm_input(
                    call_id,
                    {
                        "model": text_model,
                        "temperature": 0,
                        **build_thinking_params(
                            getattr(text_llm, "provider", "dashscope"),
                            getattr(text_llm, "enable_thinking", False),
                        ),
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {
                                "role": "system",
                                "content": PERFORMANCE_TEXT_REVIEW_SYSTEM_PROMPT,
                            },
                            {"role": "user", "content": text_prompt},
                        ],
                    },
                )
            raw_output = text_llm.review_template(
                PERFORMANCE_TEXT_REVIEW_SYSTEM_PROMPT,
                text_prompt,
            )
            text_result = _normalize_text_model_result(
                raw_output,
                allowed_image_ids=allowed_image_ids,
                image_ranges=image_ranges,
            )
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            text_elapsed_ms += elapsed_ms
            text_completed_calls = 1
            if recorder is not None and call_id is not None:
                recorder.attach_llm_response(call_id, raw_response=raw_output)
                recorder.complete_llm_call(
                    call_id,
                    parsed_objects=text_result,
                    schema_valid=True,
                    elapsed_ms=elapsed_ms,
                )
            break
        except Exception as exc:  # noqa: BLE001 - isolate this case
            elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            text_elapsed_ms += elapsed_ms
            text_error = exc
            if recorder is not None and call_id is not None:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=elapsed_ms,
                )
            if attempt < ATTACHMENT_REVIEW_MAX_ATTEMPTS and _is_retryable_attachment_review_error(exc):
                continue
            break

    if text_result is None:
        text_result = {
            "summary": "完整 OCR 文本模型调用失败，未形成可靠文字结论。",
            "facts": {
                key: []
                for key in (
                    "project_name",
                    "parties",
                    "contract_amount",
                    "implementation_time",
                    "service_content",
                )
            },
            "framework_contract": {
                "status": "uncertain",
                "reason": "文本模型调用失败，无法可靠判断是否为框架合同。",
                "evidence_image_ids": [],
                "evidence": [],
            },
            "materials": [],
            "signature_page_candidates": [],
            "signature_date_candidates": [],
        }

    unavailable_image_ids = list(ocr_scan.get("ocr_unavailable_image_ids", []))
    text_result["ocr_unavailable_image_ids"] = unavailable_image_ids
    candidate_evidence = _candidate_evidence_from_text_result(text_result)
    ocr_facts = dict(text_result.get("facts", {}))
    ocr_facts["framework_signal"] = text_result["framework_contract"]
    result.update(
        {
            "candidate_evidence": candidate_evidence,
            "ocr_facts": ocr_facts,
            "ocr_scan": ocr_scan,
            "text_model_extraction": text_result,
        }
    )

    order_check = _order_alignment_check(document, root, case)
    checks = [order_check]
    for key in (
        "service_content",
        "implementation_time",
        "contract_amount",
    ):
        checks.append(
            _text_fact_check(
                key,
                facts=text_result["facts"],
                images_by_id=images_by_id,
            )
        )

    table_consistency = _table_consistency_checks(
        table=table,
        row_index=row_index,
        text_facts=text_result["facts"],
        images_by_id=images_by_id,
    )
    result["table_consistency"] = table_consistency
    result["table_row"] = _table_row_fields(table, row_index)
    checks.extend(table_consistency.values())

    framework_result = text_result["framework_contract"]
    result["framework_contract"] = framework_result
    if framework_result["status"] in {"yes", "uncertain"}:
        checks.append(
            _framework_settlement_check_from_text(
                text_result=text_result,
                images_by_id=images_by_id,
            )
        )

    signature_candidate_records = [
        *text_result.get("signature_page_candidates", []),
        *text_result.get("signature_date_candidates", []),
    ]
    unavailable_candidates = [
        {
            "image_id": image_id,
            "evidence_image_ids": [],
            "evidence": [],
            "reason": "该图片 OCR unavailable，必须保留为视觉兜底候选。",
        }
        for image_id in unavailable_image_ids
    ]
    text_result["ocr_unavailable_visual_candidates"] = unavailable_candidates
    signature_candidate_records.extend(unavailable_candidates)
    for key in ("signature_page", "signature_date"):
        evidence = candidate_evidence[key]
        for candidate in unavailable_candidates:
            image_id = candidate["image_id"]
            if image_id not in evidence["image_ids"]:
                evidence["image_ids"].append(image_id)
            evidence["text_blocks"].append(
                {
                    "image_id": image_id,
                    "ocr_block_id": None,
                    "text": "（该图片 OCR unavailable，未取得正常 OCR 原文）",
                    "reason": candidate["reason"],
                    "source": "ocr_unavailable_visual_fallback",
                }
            )
        if unavailable_candidates:
            evidence["selection_reason"] += " OCR unavailable 图片已追加为视觉兜底候选。"
    candidate_image_ids: list[str] = []
    for candidate in signature_candidate_records:
        image_id = str(candidate.get("image_id"))
        if image_id in allowed_image_ids and image_id not in candidate_image_ids:
            candidate_image_ids.append(image_id)
    candidate_image_ids = candidate_image_ids[: PERFORMANCE_CANDIDATE_TOP_K * 2]
    for image_id in unavailable_image_ids:
        if image_id in allowed_image_ids and image_id not in candidate_image_ids:
            candidate_image_ids.append(image_id)
    model_images = [
        image
        for image in images
        if str(image.get("image_id")) in candidate_image_ids
    ]
    result["model_image_ids"] = [str(image.get("image_id")) for image in model_images]

    visual_attempts = 0
    visual_completed_calls = 0
    visual_elapsed_ms = 0
    visual_error: Exception | None = None
    visual_result: dict[str, dict[str, Any]] = {}
    if model_images:
        visual_candidates = [
            candidate
            for candidate in signature_candidate_records
            if candidate.get("image_id") in set(result["model_image_ids"])
        ]
        visual_prompt = _build_signature_visual_prompt(
            case=case,
            candidates=visual_candidates,
            images=model_images,
        )
        visual_model = str(
            getattr(visual_llm, "model", type(visual_llm).__name__)
        )
        for attempt in range(1, ATTACHMENT_REVIEW_MAX_ATTEMPTS + 1):
            visual_attempts = attempt
            call_id = None
            call_started_at = time.perf_counter()
            try:
                if recorder is not None:
                    call_id = recorder.start_llm_call(
                        batch_index=2,
                        batch_count=2,
                        attempt=attempt,
                        model=visual_model,
                        batch={
                        "case_type": case_title,
                            "stage": "signature_visual_review",
                            "template_id": result["template_id"],
                            "bid_section_id": result["bid_section_id"],
                            "image_ids": result["model_image_ids"],
                        },
                    )
                    recorder.attach_llm_input(
                        call_id,
                        build_attachment_request_payload(
                            model=visual_model,
                            system_prompt=PERFORMANCE_VISUAL_REVIEW_SYSTEM_PROMPT,
                            user_prompt=visual_prompt,
                            images=model_images,
                            provider=getattr(visual_llm, "provider", "dashscope"),
                            enable_thinking=getattr(
                                visual_llm, "enable_thinking", False
                            ),
                        ),
                    )
                raw_output = visual_llm.review_attachment(
                    PERFORMANCE_VISUAL_REVIEW_SYSTEM_PROMPT,
                    visual_prompt,
                    model_images,
                )
                visual_result = _normalize_visual_model_result(
                    raw_output,
                    candidate_image_ids=set(result["model_image_ids"]),
                    images_by_id=images_by_id,
                )
                elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
                visual_elapsed_ms += elapsed_ms
                visual_completed_calls = 1
                if recorder is not None and call_id is not None:
                    recorder.attach_llm_response(call_id, raw_response=raw_output)
                    recorder.complete_llm_call(
                        call_id,
                        parsed_objects=visual_result,
                        schema_valid=True,
                        elapsed_ms=elapsed_ms,
                    )
                break
            except Exception as exc:  # noqa: BLE001 - isolate this case
                elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
                visual_elapsed_ms += elapsed_ms
                visual_error = exc
                if recorder is not None and call_id is not None:
                    recorder.fail_llm_call(
                        call_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        elapsed_ms=elapsed_ms,
                    )
                if attempt < ATTACHMENT_REVIEW_MAX_ATTEMPTS and _is_retryable_attachment_review_error(exc):
                    continue
                break

    for key in ("signature_page", "signature_date"):
        checks.append(
            visual_result.get(key)
            or _uncertain_check(
                key,
                "文本模型没有提供可供视觉复核的签署候选图片，或视觉模型未形成可靠结论。",
            )
        )
    if text_error is not None:
        error = text_error
    else:
        error = visual_error
    status_values = [check["status"] for check in checks]
    status = "fail" if "fail" in status_values else (
        "uncertain" if "uncertain" in status_values else "pass"
    )
    result.update(
        {
            "status": status,
            "business_status": status,
            "execution_status": "failed" if error is not None else "completed",
            "summary": text_result["summary"],
            "materials": text_result["materials"],
            "checks": checks,
            "checks_by_key": {check["key"]: check for check in checks},
            "llm_elapsed_ms": text_elapsed_ms + visual_elapsed_ms,
            "text_llm_elapsed_ms": text_elapsed_ms,
            "visual_llm_elapsed_ms": visual_elapsed_ms,
        }
    )
    if error is not None:
        result.update(
            {
                "error_type": type(error).__name__,
                "error_message": str(error),
            }
        )
    return (
        result,
        text_attempts,
        text_completed_calls,
        visual_attempts,
        visual_completed_calls,
    )


def _empty_performance_result(reason: str) -> dict[str, Any]:
    return {
        "mode": "performance_contracts",
        "performance_reviews": [],
        "stats": {
            "selected_case_count": 0,
            "case_concurrency_limit": PERFORMANCE_CASE_MAX_WORKERS,
            "case_concurrency_workers": 0,
            "case_execution_mode": "parallel_cases_serial_llm",
            "image_count": 0,
            "candidate_image_count": 0,
            "ocr_image_count": 0,
            "ocr_available_image_count": 0,
            "ocr_failed_count": 0,
            "ocr_engine_call_count": 0,
            "ocr_character_count": 0,
            "ocr_token_count": 0,
            "ocr_estimated_token_count": 0,
            "llm_total_calls": 0,
            "llm_completed_calls": 0,
            "text_llm_total_calls": 0,
            "visual_llm_total_calls": 0,
            "llm_failed_count": 0,
            "status_counts": {status: 0 for status in ("pass", "fail", "uncertain")},
            "reason": reason,
        },
    }


def _case_stats(
    *,
    review: dict[str, Any],
    images: list[dict[str, Any]],
    text_attempts: int,
    text_completed_calls: int,
    visual_attempts: int,
    visual_completed_calls: int,
) -> dict[str, Any]:
    scan = review.get("ocr_scan", {})
    return {
        "image_count": len(images),
        "ocr_image_count": scan.get("image_count", len(images)),
        "ocr_available_image_count": scan.get("available_image_count", 0),
        "ocr_failed_count": scan.get("failed_count", 0),
        "ocr_unavailable_image_ids": list(scan.get("ocr_unavailable_image_ids", [])),
        "ocr_character_count": scan.get("character_count", 0),
        "ocr_token_count": scan.get("token_count", 0),
        "ocr_estimated_token_count": scan.get("estimated_token_count", 0),
        "ocr_token_count_source": scan.get("token_count_source"),
        "text_llm_total_calls": text_attempts,
        "text_llm_completed_calls": text_completed_calls,
        "visual_llm_total_calls": visual_attempts,
        "visual_llm_completed_calls": visual_completed_calls,
        "text_llm_elapsed_ms": review.get("text_llm_elapsed_ms", 0),
        "visual_llm_elapsed_ms": review.get("visual_llm_elapsed_ms", 0),
        "total_elapsed_ms": review.get("llm_elapsed_ms", 0),
    }


def run_performance_contract_review(
    extraction_result: dict[str, Any],
    parsed_bid: dict[str, Any],
    *,
    llm: AttachmentReviewLLM,
    text_llm: Any | None = None,
    # Kept as an ignored compatibility keyword for callers from the previous
    # implementation. OCR is now produced only during the DOCX MinerU parse.
    ocr_engine: Any | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Check each available 21.1--21.6 performance-contract evidence unit."""

    pipeline_started_at = time.perf_counter()
    document, artifact_dir = _read_structured_document(parsed_bid)
    template = _performance_template(extraction_result)
    if document is None or template is None:
        result = _empty_performance_result(
            "缺少 21 业绩情况表模板或投标文件 structured_document。"
        )
        if recorder is not None:
            recorder.write_json("10_performance_reviews.json", result)
        return result

    root, cases, _sections_by_id = _find_all_performance_sections(document)
    if root is None or not cases:
        result = _empty_performance_result(
            "未找到 21 业绩情况表或 21.1～21.6 业绩子章节。"
        )
        if recorder is not None:
            recorder.write_json("10_performance_reviews.json", result)
        return result

    _sections, _sections_by_id, images_by_block_id, images_by_id = _materialized_sections(
        document
    )
    active_text_llm = text_llm or getattr(llm, "review_template", None)
    use_full_ocr_flow = callable(getattr(active_text_llm, "review_template", None))
    reviews: list[dict[str, Any]] = []
    aggregate = {
        "selected_case_count": 0,
        "case_concurrency_limit": PERFORMANCE_CASE_MAX_WORKERS,
        "case_concurrency_workers": min(PERFORMANCE_CASE_MAX_WORKERS, len(cases)),
        "case_execution_mode": "parallel_cases_serial_llm",
        "image_count": 0,
        "candidate_image_count": 0,
        "candidate_text_block_count": 0,
        "ocr_image_count": 0,
        "ocr_available_image_count": 0,
        "ocr_failed_count": 0,
        "ocr_engine_call_count": 0,
        "ocr_processed_image_count": 0,
        "ocr_precomputed_image_count": 0,
        "ocr_cache_hit_count": 0,
        "ocr_elapsed_ms": 0,
        "ocr_character_count": 0,
        "ocr_token_count": 0,
        "ocr_estimated_token_count": 0,
        "llm_total_calls": 0,
        "llm_completed_calls": 0,
        "text_llm_total_calls": 0,
        "text_llm_completed_calls": 0,
        "visual_llm_total_calls": 0,
        "visual_llm_completed_calls": 0,
        "llm_failed_count": 0,
        "status_counts": {status: 0 for status in ("pass", "fail", "uncertain")},
        "text_llm_elapsed_ms": 0,
        "visual_llm_elapsed_ms": 0,
    }

    def review_one_case(
        case: dict[str, Any],
    ) -> tuple[dict[str, Any], int, int, int, int]:
        case_title = str(case.get("title") or PERFORMANCE_CASE_TITLE)
        table, row_index = _table_row_for_case(
            document,
            root,
            case_title=case_title,
        )
        images = _collect_section_images(
            case,
            images_by_block_id=images_by_block_id,
            images_by_id=images_by_id,
            artifact_dir=artifact_dir,
        )
        ocr_entries, ocr_scan = _enrich_images_with_ocr(
            images,
            raw_images_by_id=images_by_id,
        )
        if use_full_ocr_flow:
            (
                review,
                text_attempts,
                text_completed_calls,
                visual_attempts,
                visual_completed_calls,
            ) = _review_case_with_full_ocr(
                document=document,
                template=template,
                root=root,
                case=case,
                table=table,
                row_index=row_index,
                images=images,
                ocr_entries=ocr_entries,
                ocr_scan=ocr_scan,
                text_llm=active_text_llm,
                visual_llm=llm,
                recorder=recorder,
            )
        else:
            candidate_evidence, model_images = _select_performance_candidates(
                images,
                ocr_entries,
                case_title=case_title,
            )
            ocr_facts = _extract_ocr_facts(
                candidate_evidence,
                ocr_available=ocr_scan["available_image_count"] > 0,
                ocr_entries=ocr_entries,
            )
            review, legacy_attempts, legacy_completed_calls = _review_case(
                template=template,
                root=root,
                case=case,
                table=table,
                row_index=row_index,
                images=images,
                model_images=model_images,
                candidate_evidence=candidate_evidence,
                ocr_facts=ocr_facts,
                ocr_scan=ocr_scan,
                llm=llm,
                recorder=recorder,
            )
            visual_attempts = legacy_attempts
            text_attempts = 0
            text_completed_calls = 0
            visual_completed_calls = legacy_completed_calls
        review["case_stats"] = _case_stats(
            review=review,
            images=images,
            text_attempts=text_attempts,
            text_completed_calls=text_completed_calls,
            visual_attempts=visual_attempts,
            visual_completed_calls=visual_completed_calls,
        )

        return (
            review,
            text_attempts,
            text_completed_calls,
            visual_attempts,
            visual_completed_calls,
        )

    case_results: dict[int, tuple[dict[str, Any], int, int, int, int]] = {}
    with ThreadPoolExecutor(
        max_workers=aggregate["case_concurrency_workers"],
        thread_name_prefix="performance-case",
    ) as executor:
        future_to_index = {
            executor.submit(review_one_case, case): index
            for index, case in enumerate(cases)
        }
        for future in as_completed(future_to_index):
            case_results[future_to_index[future]] = future.result()

    for index in range(len(cases)):
        review, text_attempts, text_completed_calls, visual_attempts, visual_completed_calls = (
            case_results[index]
        )
        reviews.append(review)
        scan = review.get("ocr_scan", {})
        aggregate["selected_case_count"] += 1
        aggregate["image_count"] += len(review.get("image_ids", []))
        aggregate["candidate_image_count"] += len(review.get("model_image_ids", []))
        aggregate["candidate_text_block_count"] += sum(
            len(evidence.get("text_blocks", []))
            for evidence in review.get("candidate_evidence", {}).values()
            if isinstance(evidence, dict)
        )
        for key in (
            "image_count",
            "available_image_count",
            "failed_count",
            "engine_call_count",
            "processed_image_count",
            "precomputed_image_count",
            "cache_hit_count",
            "elapsed_ms",
        ):
            aggregate_key = {
                "image_count": "ocr_image_count",
                "available_image_count": "ocr_available_image_count",
                "failed_count": "ocr_failed_count",
                "engine_call_count": "ocr_engine_call_count",
                "processed_image_count": "ocr_processed_image_count",
                "precomputed_image_count": "ocr_precomputed_image_count",
                "cache_hit_count": "ocr_cache_hit_count",
                "elapsed_ms": "ocr_elapsed_ms",
            }[key]
            aggregate[aggregate_key] += int(scan.get(key, 0) or 0)
        aggregate["ocr_character_count"] += int(scan.get("character_count", 0) or 0)
        aggregate["ocr_token_count"] += int(scan.get("token_count", 0) or 0)
        aggregate["ocr_estimated_token_count"] += int(
            scan.get("estimated_token_count", 0) or 0
        )
        aggregate["llm_total_calls"] += text_attempts + visual_attempts
        aggregate["llm_completed_calls"] += text_completed_calls + visual_completed_calls
        aggregate["text_llm_total_calls"] += text_attempts
        aggregate["text_llm_completed_calls"] += text_completed_calls
        aggregate["visual_llm_total_calls"] += visual_attempts
        aggregate["visual_llm_completed_calls"] += visual_completed_calls
        aggregate["llm_failed_count"] += int(review.get("execution_status") == "failed")
        aggregate["status_counts"][review.get("status", "uncertain")] += 1
        aggregate["text_llm_elapsed_ms"] += int(review.get("text_llm_elapsed_ms", 0) or 0)
        aggregate["visual_llm_elapsed_ms"] += int(review.get("visual_llm_elapsed_ms", 0) or 0)

    aggregate["llm_elapsed_ms"] = (
        aggregate["text_llm_elapsed_ms"] + aggregate["visual_llm_elapsed_ms"]
    )
    aggregate["total_elapsed_ms"] = int(
        (time.perf_counter() - pipeline_started_at) * 1000
    )
    aggregate["ocr_token_count_source"] = (
        "conservative_estimate_no_tokenizer" if use_full_ocr_flow else "legacy_flow"
    )
    aggregate["ocr_token_count_is_estimate"] = use_full_ocr_flow
    result = {
        "mode": "performance_contracts",
        "performance_reviews": reviews,
        "stats": aggregate,
    }
    if recorder is not None:
        recorder.write_json("10_performance_reviews.json", result)
        recorder.event(
            "performance.review.end",
            status="complete",
            selected_case_count=len(reviews),
            image_count=aggregate["image_count"],
            llm_total_calls=aggregate["llm_total_calls"],
            llm_failed_count=aggregate["llm_failed_count"],
            llm_elapsed_ms=aggregate["llm_elapsed_ms"],
            ocr_image_count=aggregate["ocr_image_count"],
            ocr_available_image_count=aggregate["ocr_available_image_count"],
            ocr_engine_call_count=aggregate["ocr_engine_call_count"],
            ocr_cache_hit_count=aggregate["ocr_cache_hit_count"],
            candidate_image_count=aggregate["candidate_image_count"],
        )
    return result
