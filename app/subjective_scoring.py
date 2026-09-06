from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata
from app.objective_scoring import load_reusable_bid_evidence

logger = logging.getLogger(__name__)

SUBJECTIVE_SCORE_ARTIFACT = "subjective_scores.json"
SUBJECTIVE_STATUSES = frozenset(
    {"ai_scored", "file_scope_missing", "evidence_insufficient", "llm_error"}
)
SUBJECTIVE_MAX_WORKERS = 5


class SubjectiveScoringError(RuntimeError):
    """Raised when a subjective scoring call or response is unusable."""


class SubjectiveScoreLLM(Protocol):
    model: str
    available: bool

    def score(
        self,
        score_item: Mapping[str, Any],
        matched_bid_content: Sequence[Mapping[str, Any]],
        allowed_bands: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...

_NAVIGATION_TERMS = ("索引", "目录", "对应页码", "页码")
_TECHNICAL_SCOPE_TERMS = (
    "技术标",
    "技术投标文件",
    "技术响应",
    "技术方案",
    "技术标准和要求响应",
)
_SERVICE_SCOPE_TERMS = (
    "服务方案",
    "服务质量保障措施",
    "质量服务保障措施",
    "服务支撑方案",
)
_TECHNICAL_ITEM_TERMS = (
    "项目需求的分析及理解",
    "云网产品",
    "大模型技术支持",
    "技术标准和要求",
    "技术响应",
)
_SERVICE_ITEM_TERMS = (
    "质量服务保障",
    "服务质量保障",
    "服务方案",
)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _normalized(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", _as_text(value)).lower()


def _document_sections(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    raw_sections = document.get("sections", [])
    if not isinstance(raw_sections, list):
        return []
    return [dict(section) for section in raw_sections if isinstance(section, Mapping)]


def _document_blocks(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    raw_blocks = document.get("blocks", [])
    if not isinstance(raw_blocks, list):
        return []
    return [dict(block) for block in raw_blocks if isinstance(block, Mapping)]


def _section_texts(document: Mapping[str, Any] | None) -> list[str]:
    values: list[str] = []
    for section in _document_sections(document):
        values.append(_as_text(section.get("title")))
        path = section.get("path")
        if isinstance(path, list):
            values.extend(_as_text(part) for part in path)
    return [value for value in values if value.strip()]


def _is_navigation_block(block: Mapping[str, Any]) -> bool:
    values = [
        _as_text(block.get("section")),
        _as_text(block.get("section_title")),
    ]
    path = block.get("section_path", block.get("path"))
    if isinstance(path, list):
        values.extend(_as_text(part) for part in path)
    return any(term in " ".join(values) for term in _NAVIGATION_TERMS)


def _is_business_only(
    document: Mapping[str, Any] | None,
    filename: str,
) -> bool:
    section_text = "\n".join(_section_texts(document))
    has_business_signal = "商务" in filename or "商务" in section_text
    has_other_scope = any(
        term in filename or term in section_text
        for term in (*_TECHNICAL_SCOPE_TERMS, *_SERVICE_SCOPE_TERMS)
    )
    return has_business_signal and not has_other_scope


def _item_scope(item: Mapping[str, Any]) -> str | None:
    item_id = _as_text(item.get("id"))
    item_name = _as_text(item.get("name"))
    combined = f"{item_id} {item_name}"
    if item_id in {"score_item_003", "score_item_004", "score_item_005", "score_item_006"}:
        return "technical"
    if item_id == "score_item_009":
        return "service"
    if any(term in combined for term in _TECHNICAL_ITEM_TERMS):
        return "technical"
    if any(term in combined for term in _SERVICE_ITEM_TERMS):
        return "service"
    if "投标文件编写质量" in combined:
        return "business"
    return None


def _scope_available(
    scope: str | None,
    document: Mapping[str, Any] | None,
    filename: str,
) -> bool:
    if scope in {None, "business"}:
        return True
    scope_terms = (
        _TECHNICAL_SCOPE_TERMS if scope == "technical" else _SERVICE_SCOPE_TERMS
    )
    filename_text = _normalized(filename)
    section_text = _normalized("\n".join(_section_texts(document)))
    return any(
        _normalized(term) in filename_text or _normalized(term) in section_text
        for term in scope_terms
    )


def _keyword_phrases(item: Mapping[str, Any]) -> list[str]:
    phrases: list[str] = []
    for value in [_as_text(item.get("name"))]:
        if value.strip():
            phrases.append(value)
    evidence_requirements = item.get("evidence_requirements", [])
    if isinstance(evidence_requirements, list):
        phrases.extend(
            _as_text(value)
            for value in evidence_requirements
            if _as_text(value).strip()
        )
    return list(dict.fromkeys(_normalized(value) for value in phrases if value.strip()))


def _compact_block(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": _as_text(block.get("block_id")),
        "section": _as_text(block.get("section")),
        "type": _as_text(block.get("type")) or "paragraph",
        "text": _as_text(block.get("text")),
        "order": block.get("order"),
    }


def select_subjective_items(
    evaluation_rules: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_items = evaluation_rules.get("score_items", [])
    if not isinstance(raw_items, list):
        return []
    return [
        dict(item)
        for item in raw_items
        if isinstance(item, Mapping)
        and item.get("evaluation_type") == "subjective"
    ]


def match_subjective_bid_content(
    item: Mapping[str, Any],
    bid_document: Mapping[str, Any] | None,
    bid_filename: str,
) -> dict[str, Any]:
    scope = _item_scope(item)
    if scope in {"technical", "service"} and not _scope_available(
        scope, bid_document, bid_filename
    ):
        return {
            "status": "file_scope_missing",
            "scope": scope,
            "reason": (
                "当前商务投标文件范围未包含该评分项所需的技术标/技术响应内容。"
                if scope == "technical"
                else "当前投标文件范围未包含该评分项所需的服务方案或质量服务保障措施。"
            ),
            "blocks": [],
        }

    phrases = _keyword_phrases(item)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, raw_block in enumerate(_document_blocks(bid_document)):
        if _is_navigation_block(raw_block):
            continue
        block = _compact_block(raw_block)
        if not block["block_id"] or not block["text"].strip():
            continue
        haystack = _normalized(
            f"{block['section']} {block['text']}"
        )
        matched_phrases = [phrase for phrase in phrases if phrase in haystack]
        if not matched_phrases:
            continue
        score = max(
            100 if phrase == _normalized(_as_text(item.get("name"))) else 80
            for phrase in matched_phrases
        )
        score += min(15, len(matched_phrases) * 3)
        order = block["order"] if isinstance(block["order"], int) else index
        ranked.append((score, order, block))

    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    selected = [entry[2] for entry in ranked[:12]]
    selected.sort(
        key=lambda block: block["order"]
        if isinstance(block["order"], int)
        else 0
    )
    if not selected:
        return {
            "status": "evidence_insufficient",
            "scope": scope,
            "reason": "当前已解析投标文件中没有定位到该评分项对应的正文 block。",
            "blocks": [],
        }
    return {
        "status": "matched",
        "scope": scope,
        "reason": "已从现有结构化投标内容定位到评分项对应 block。",
        "blocks": selected,
    }


_INTERVAL_RE = re.compile(
    r"([\[\(])\s*([0-9]+(?:\.[0-9]+)?)\s*[,，]\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*([\]\)])"
)


def _numeric(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(float(value)) else None


def _full_score(item: Mapping[str, Any]) -> float | int | None:
    return _numeric(item.get("full_score"))


def _rule_text(item: Mapping[str, Any]) -> str:
    values = [
        _as_text(item.get("original_rule")),
        _as_text(item.get("conditions")),
        _as_text(item.get("scoring_method")),
    ]
    for key in ("conditions", "scoring_method"):
        value = item.get(key)
        if isinstance(value, (Mapping, list)):
            values.append(json.dumps(value, ensure_ascii=False))
    return " ".join(value for value in values if value)


def _band_number(value: str) -> float | int:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _allowed_score_bands(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    text = _rule_text(item)
    bands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _INTERVAL_RE.finditer(text):
        label = f"{match.group(1)}{match.group(2)},{match.group(3)}{match.group(4)}"
        if label in seen:
            continue
        seen.add(label)
        bands.append(
            {
                "label": label,
                "min": _band_number(match.group(2)),
                "max": _band_number(match.group(3)),
                "min_inclusive": match.group(1) == "[",
                "max_inclusive": match.group(4) == "]",
            }
        )
    if "未提供" in text or re.search(r"(?:^|[^0-9])0\s*分", text):
        bands.append(
            {
                "label": "0",
                "min": 0,
                "max": 0,
                "min_inclusive": True,
                "max_inclusive": True,
            }
        )
    if "扣" in text:
        bands.append(
            {
                "label": "扣分规则",
                "min": 0,
                "max": _full_score(item),
                "min_inclusive": True,
                "max_inclusive": True,
            }
        )
    if not bands:
        max_score = _full_score(item)
        bands.append(
            {
                "label": "规则范围",
                "min": 0,
                "max": max_score,
                "min_inclusive": True,
                "max_inclusive": True,
            }
        )
    return bands


def _band_contains(band: Mapping[str, Any], score: float) -> bool:
    minimum = _numeric(band.get("min"))
    maximum = _numeric(band.get("max"))
    if minimum is not None:
        if band.get("min_inclusive", False):
            if score < float(minimum):
                return False
        elif score <= float(minimum):
            return False
    if maximum is not None:
        if band.get("max_inclusive", False):
            if score > float(maximum):
                return False
        elif score >= float(maximum):
            return False
    return True


def _uncertainty(value: Any, *, default_note: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        level = _as_text(value.get("level")) or "medium"
        notes = value.get("notes", [])
        normalized_notes = (
            [_as_text(note) for note in notes if _as_text(note).strip()]
            if isinstance(notes, list)
            else [_as_text(notes)] if _as_text(notes).strip() else []
        )
        if default_note:
            normalized_notes.append(default_note)
        return {"level": level, "notes": normalized_notes}
    notes = [default_note] if default_note else []
    return {"level": "medium", "notes": notes}


def _base_result(
    item: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    matched_blocks: Sequence[Mapping[str, Any]] = (),
    uncertainty: Any = None,
) -> dict[str, Any]:
    if status not in SUBJECTIVE_STATUSES:
        raise ValueError(f"unknown subjective score status: {status}")
    compact_blocks = [dict(block) for block in matched_blocks]
    block_ids = [
        _as_text(block.get("block_id"))
        for block in compact_blocks
        if _as_text(block.get("block_id"))
    ]
    return {
        "score_item_id": _as_text(item.get("id")),
        "rule_name": _as_text(item.get("name")),
        "max_score": _full_score(item),
        "status": status,
        "score_band": None,
        "recommended_score": None,
        "reason": reason,
        "matched_bid_content": compact_blocks,
        "evidence": [],
        "block_ids": block_ids,
        "uncertainty": _uncertainty(uncertainty, default_note=reason),
    }


def _normalize_evidence(
    value: Any,
    allowed_blocks: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SubjectiveScoringError("模型未返回可追溯的 evidence 列表")
    evidence: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise SubjectiveScoringError("模型 evidence 条目结构异常")
        block_id = _as_text(entry.get("block_id"))
        quote = _as_text(entry.get("quote")).strip()
        relation = _as_text(entry.get("relation")).strip()
        if block_id not in allowed_blocks:
            raise SubjectiveScoringError(
                f"模型 evidence 引用了未送模 block_id: {block_id or '<empty>'}"
            )
        if not quote or not relation:
            raise SubjectiveScoringError("模型 evidence 缺少 quote 或 relation")
        source_text = _as_text(allowed_blocks[block_id].get("text"))
        if _normalized(quote) not in _normalized(source_text):
            raise SubjectiveScoringError(
                f"模型 evidence quote 不在 block_id {block_id} 的投标原文中"
            )
        evidence.append(
            {"block_id": block_id, "quote": quote, "relation": relation}
        )
    return evidence


def _validate_llm_result(
    item: Mapping[str, Any],
    matched_blocks: Sequence[Mapping[str, Any]],
    allowed_bands: Sequence[Mapping[str, Any]],
    raw_result: Any,
) -> dict[str, Any]:
    if not isinstance(raw_result, Mapping):
        raise SubjectiveScoringError("模型返回结果不是 JSON 对象")
    score_band = _as_text(raw_result.get("score_band")).strip()
    bands_by_label = {
        _as_text(band.get("label")): band
        for band in allowed_bands
        if _as_text(band.get("label"))
    }
    if score_band not in bands_by_label:
        raise SubjectiveScoringError(f"模型返回了规则之外的评分档: {score_band or '<empty>'}")
    score = _numeric(raw_result.get("recommended_score"))
    if score is None:
        raise SubjectiveScoringError("模型未返回有限的 recommended_score")
    score_as_float = float(score)
    max_score = _full_score(item)
    if max_score is not None and not 0 <= score_as_float <= float(max_score):
        raise SubjectiveScoringError("recommended_score 超出 0 到 max_score 范围")
    if not _band_contains(bands_by_label[score_band], score_as_float):
        raise SubjectiveScoringError(
            f"recommended_score 不落在评分档 {score_band} 的边界内"
        )
    reason = _as_text(raw_result.get("reason")).strip()
    if not reason:
        raise SubjectiveScoringError("模型未返回评分理由")
    blocks_by_id = {
        _as_text(block.get("block_id")): block
        for block in matched_blocks
        if _as_text(block.get("block_id"))
    }
    evidence = _normalize_evidence(raw_result.get("evidence"), blocks_by_id)
    return {
        "score_item_id": _as_text(item.get("id")),
        "rule_name": _as_text(item.get("name")),
        "max_score": max_score,
        "status": "ai_scored",
        "score_band": score_band,
        "recommended_score": score,
        "reason": reason,
        "matched_bid_content": [dict(block) for block in matched_blocks],
        "evidence": evidence,
        "block_ids": sorted(blocks_by_id),
        "uncertainty": _uncertainty(raw_result.get("uncertainty")),
    }


class OpenAICompatibleSubjectiveScoreLLM:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout_seconds: float = 180,
        max_tokens: int = 2048,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max(256, min(max_tokens, 8192))
        self._call_context = threading.local()
        self._last_usage = threading.local()
        self.available = True

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return getattr(self._last_usage, "value", None)

    @last_usage.setter
    def last_usage(self, value: dict[str, Any] | None) -> None:
        self._last_usage.value = value

    def set_call_context(
        self,
        *,
        recorder: ComplianceExtractionRecorder | None,
        call_id: str | None,
    ) -> None:
        self._call_context.value = {"recorder": recorder, "call_id": call_id}

    def _context(self) -> dict[str, Any]:
        return getattr(self._call_context, "value", {})

    def score(
        self,
        score_item: Mapping[str, Any],
        matched_bid_content: Sequence[Mapping[str, Any]],
        allowed_bands: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        rule_payload = {
            key: score_item.get(key)
            for key in (
                "id",
                "name",
                "full_score",
                "original_rule",
                "conditions",
                "scoring_method",
                "evidence_requirements",
            )
        }
        prompt = (
            "你是招标文件主观评分辅助器。只能依据本评分项原始规则和输入中的投标 block"
            "进行建议评分，不得增加招标文件没有的评价维度，不得使用其他评分项、否决规则、"
            "整份投标文件或输入之外的信息。必须先选择 allowed_bands 中的原始评分档，"
            "再给出属于该档的 recommended_score。每条 evidence 必须引用输入中的真实 block_id"
            "和直接摘录；无法确认时不要猜测，返回 uncertainty 并保持证据边界。"
            "只返回 JSON 对象，字段必须为 score_band、recommended_score、reason、evidence、uncertainty。\n\n"
            f"评分项规则：{json.dumps(rule_payload, ensure_ascii=False)}\n"
            f"允许的评分档：{json.dumps(list(allowed_bands), ensure_ascii=False)}\n"
            f"匹配投标内容：{json.dumps(list(matched_bid_content), ensure_ascii=False)}"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "enable_thinking": False,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你只输出来源受限的主观评分 JSON。"},
                {"role": "user", "content": prompt},
            ],
        }
        context = self._context()
        recorder = context.get("recorder")
        call_id = context.get("call_id")
        if recorder is not None and call_id is not None:
            recorder.attach_llm_input(call_id, payload)
        started_at = time.perf_counter()
        try:
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
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            choice = response_payload.get("choices", [{}])[0]
            self.last_usage = response_payload.get("usage")
            if recorder is not None and call_id is not None:
                recorder.attach_llm_response(
                    call_id,
                    raw_response=response_payload,
                    finish_reason=choice.get("finish_reason"),
                    usage=self.last_usage,
                )
            content = choice["message"]["content"]
            decoded = json.loads(content) if isinstance(content, str) else content
            if not isinstance(decoded, Mapping):
                raise TypeError("subjective scoring result must be an object")
            return decoded
        except TimeoutError as exc:
            raise SubjectiveScoringError("主观评分 LLM 请求超时。") from exc
        except urllib.error.HTTPError as exc:
            raise SubjectiveScoringError(
                f"主观评分 LLM 请求失败：HTTP {exc.code}。"
            ) from exc
        except urllib.error.URLError as exc:
            raise SubjectiveScoringError("主观评分 LLM 网络连接失败。") from exc
        except json.JSONDecodeError as exc:
            raise SubjectiveScoringError("主观评分 LLM 响应不是有效 JSON。") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SubjectiveScoringError("主观评分 LLM 响应结构异常。") from exc
        finally:
            logger.info(
                "subjective_scoring.llm.call elapsed_ms=%d model=%s item=%s",
                int((time.perf_counter() - started_at) * 1000),
                self.model,
                _as_text(score_item.get("id")),
            )


class DeterministicSubjectiveScoreLLM:
    model = "deterministic"
    available = False

    def score(
        self,
        score_item: Mapping[str, Any],
        matched_bid_content: Sequence[Mapping[str, Any]],
        allowed_bands: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        del score_item, matched_bid_content, allowed_bands
        return {}


def _llm_error_result(
    item: Mapping[str, Any],
    matched_blocks: Sequence[Mapping[str, Any]],
    reason: str,
) -> dict[str, Any]:
    return _base_result(
        item,
        status="llm_error",
        reason=reason,
        matched_blocks=matched_blocks,
        uncertainty={"level": "high", "notes": [reason]},
    )


def _execute_subjective_item(
    item: Mapping[str, Any],
    matched_blocks: Sequence[Mapping[str, Any]],
    allowed_bands: Sequence[Mapping[str, Any]],
    *,
    subjective_llm: SubjectiveScoreLLM,
    recorder: ComplianceExtractionRecorder | None,
    batch_index: int,
    batch_count: int,
) -> tuple[dict[str, Any], bool, bool, int]:
    if not getattr(subjective_llm, "available", True):
        reason = "未配置可用的主观评分 LLM，未生成 AI 辅助建议分。"
        return (
            _llm_error_result(item, matched_blocks, reason),
            False,
            False,
            0,
        )

    started_at = time.perf_counter()
    call_id: str | None = None
    raw_result: Mapping[str, Any] | None = None
    if recorder is not None:
        call_id = recorder.start_llm_call(
            batch_index=batch_index,
            batch_count=batch_count,
            attempt=1,
            model=getattr(subjective_llm, "model", type(subjective_llm).__name__),
            batch={
                "score_item_id": _as_text(item.get("id")),
                "block_ids": [
                    _as_text(block.get("block_id")) for block in matched_blocks
                ],
            },
        )
    set_context = getattr(subjective_llm, "set_call_context", None)
    if callable(set_context):
        set_context(recorder=recorder, call_id=call_id)
    try:
        raw_result = subjective_llm.score(item, matched_blocks, allowed_bands)
        normalized = _validate_llm_result(
            item,
            matched_blocks,
            allowed_bands,
            raw_result,
        )
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        if recorder is not None and call_id is not None:
            recorder.complete_llm_call(
                call_id,
                raw_response=dict(raw_result),
                parsed_objects=dict(raw_result),
                usage=getattr(subjective_llm, "last_usage", None),
                schema_valid=True,
                elapsed_ms=elapsed_ms,
            )
        return normalized, True, True, elapsed_ms
    except Exception as exc:  # noqa: BLE001 - isolate one item model failure
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        message = str(exc) or "主观评分 LLM 调用失败。"
        if recorder is not None and call_id is not None:
            if raw_result is None:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=message,
                    elapsed_ms=elapsed_ms,
                )
            else:
                recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=message,
                    elapsed_ms=elapsed_ms,
                    raw_response=dict(raw_result),
                )
        return (
            _llm_error_result(item, matched_blocks, message),
            True,
            False,
            elapsed_ms,
        )
    finally:
        if callable(set_context):
            set_context(recorder=None, call_id=None)


def _subjective_source(
    evidence: Mapping[str, Any],
    bid_file: FileMetadata,
) -> dict[str, Any]:
    artifacts = evidence.get("artifacts", {})
    reused_artifacts = (
        sorted(artifacts)
        if isinstance(artifacts, Mapping)
        else []
    )
    return {
        "evaluation_rules_artifact": "11_evaluation_rules.json",
        "bid_filename": bid_file.filename,
        "bid_path": evidence.get("bid_path"),
        "bid_document_artifact": evidence.get("bid_document_artifact"),
        "bid_document_hash_verified": bool(
            evidence.get("bid_document_hash_verified")
        ),
        "reused_artifacts": reused_artifacts,
        "missing_artifacts": sorted(evidence.get("missing_artifacts", [])),
    }


def run_subjective_scoring(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    subjective_llm: SubjectiveScoreLLM,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Execute only subjective score rules from reusable bid evidence."""

    started_at = time.perf_counter()
    evidence = load_reusable_bid_evidence(
        bid_file,
        bid_document=bid_document,
        artifact_dir=artifact_dir,
        existing_artifacts=existing_artifacts,
    )
    items = select_subjective_items(evaluation_rules)
    records: dict[int, dict[str, Any]] = {}
    eligible: list[tuple[int, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for index, item in enumerate(items):
        match = match_subjective_bid_content(
            item,
            evidence.get("bid_document"),
            bid_file.filename,
        )
        blocks = match.get("blocks", [])
        blocks = [dict(block) for block in blocks if isinstance(block, Mapping)]
        if match.get("status") != "matched":
            records[index] = _base_result(
                item,
                status=_as_text(match.get("status")) or "evidence_insufficient",
                reason=_as_text(match.get("reason")) or "未定位到评分证据。",
                matched_blocks=blocks,
            )
            continue
        eligible.append((index, item, blocks, _allowed_score_bands(item)))

    call_stats: dict[int, tuple[bool, bool, int]] = {}
    if eligible:
        worker_count = min(SUBJECTIVE_MAX_WORKERS, len(eligible))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="subjective-score",
        ) as executor:
            futures = {
                executor.submit(
                    _execute_subjective_item,
                    item,
                    blocks,
                    bands,
                    subjective_llm=subjective_llm,
                    recorder=recorder,
                    batch_index=index + 1,
                    batch_count=len(eligible),
                ): index
                for index, item, blocks, bands in eligible
            }
            for future in as_completed(futures):
                index = futures[future]
                item, blocks = next(
                    (item, blocks)
                    for eligible_index, item, blocks, _ in eligible
                    if eligible_index == index
                )
                try:
                    record, called, completed, elapsed_ms = future.result()
                except Exception as exc:  # noqa: BLE001 - continue other items
                    reason = str(exc) or "主观评分 worker 执行失败。"
                    record = _llm_error_result(item, blocks, reason)
                    called, completed, elapsed_ms = True, False, 0
                records[index] = record
                call_stats[index] = (called, completed, elapsed_ms)

    score_items = [records[index] for index in range(len(items))]
    status_counts = {status: 0 for status in sorted(SUBJECTIVE_STATUSES)}
    for record in score_items:
        status = record["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    llm_total_calls = sum(called for called, _, _ in call_stats.values())
    llm_completed_calls = sum(completed for _, completed, _ in call_stats.values())
    llm_failed_calls = llm_total_calls - llm_completed_calls
    call_elapsed = [
        elapsed_ms
        for index, (called, _, elapsed_ms) in sorted(call_stats.items())
        if called
    ]
    result: dict[str, Any] = {
        "schema_version": "subjective-score-v1",
        "source": _subjective_source(evidence, bid_file),
        "score_items": score_items,
        "stats": {
            "subjective_item_count": len(score_items),
            "ai_scored_count": status_counts.get("ai_scored", 0),
            "file_scope_missing_count": status_counts.get("file_scope_missing", 0),
            "evidence_insufficient_count": status_counts.get(
                "evidence_insufficient", 0
            ),
            "llm_error_count": status_counts.get("llm_error", 0),
            "status_counts": status_counts,
            "llm_total_calls": llm_total_calls,
            "llm_completed_calls": llm_completed_calls,
            "llm_failed_calls": llm_failed_calls,
            "llm_elapsed_ms": sum(call_elapsed),
            "llm_call_elapsed_ms": call_elapsed,
            "bid_parse_reused": bool(evidence.get("bid_document_hash_verified")),
            "new_parse_calls": 0,
            "new_ocr_calls": 0,
            "new_mineru_calls": 0,
            "duplicate_parse": False,
            "total_score_computed": False,
            "ranking_computed": False,
            "veto_executed": False,
            "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
        },
    }
    if recorder is not None:
        recorder.write_json(SUBJECTIVE_SCORE_ARTIFACT, result)
        recorder.event(
            "subjective.score.execution.end",
            status="complete",
            subjective_item_count=len(score_items),
            ai_scored_count=status_counts.get("ai_scored", 0),
            llm_total_calls=llm_total_calls,
            elapsed_ms=result["stats"]["elapsed_ms"],
        )
    return result
