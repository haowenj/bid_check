
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import (
    DocumentParser,
    MinerUDocumentParser,
    StructuredBlock,
    _parsed_document_cache_key,
)
from app.models import (
    EvaluationSourceSection,
    ScoreCategory,
    ScoreItem,
    TenderEvaluationExtractionResult,
    UncertainRule,
    VetoRule,
)

logger = logging.getLogger(__name__)

EVALUATION_RULE_SCHEMA_VERSION = "evaluation-rules-v1"
EVALUATION_RULE_PROMPT_VERSION = "evaluation-rule-extraction-prompt-v1"
EVALUATION_RULE_CACHE_VERSION = (
    f"evaluation-rule-extraction-v9:{EVALUATION_RULE_PROMPT_VERSION}"
)
EVALUATION_LLM_MAX_CONCURRENCY = 5
_EVALUATION_LLM_SEMAPHORE = threading.BoundedSemaphore(
    EVALUATION_LLM_MAX_CONCURRENCY
)

_EVALUATION_TITLE_RE = re.compile(
    r"评标办法|评审办法|综合评估法|综合评分法|评分标准|评审标准|"
    r"初步评审|详细评审|资格审查|符合性审查|否决投标|废标|无效投标|"
    r"商务评分|技术评分|服务评分|价格评分|商务部分|技术部分|服务部分|价格部分"
)
_SCORE_SIGNAL_RE = re.compile(
    r"评分|分值|满分|得分|每个|每项|加分|扣分|最高|最低|权重|"
    r"商务|技术|服务|价格|业绩|证书|职称|方案|荣誉|报价"
)
_VETO_SIGNAL_RE = re.compile(
    r"否决其投标|否决投标|被否决|导致被否决|废标|无效投标|不通过资格审查|不通过符合性审查|"
    r"不进入下一阶段|不得进入评审|取消评审资格|失去评审资格|"
    r"不予接收投标文件|不接收投标文件|拒收投标文件|不予受理"
)
_EXPLICIT_VETO_CONSEQUENCE_RE = re.compile(
    r"否决(?:其|该|此)?(?:投标|响应)|"
    r"(?:投标|响应)(?:将|均将|应当)?被否决|"
    r"(?:投标|响应)无效|无效投标|废标|"
    r"不通过(?:资格|符合性)审查|"
    r"不进入(?:下一阶段|评审)|取消评审资格|失去评审资格|"
    r"不予接收(?:投标文件)?|不接收(?:投标文件)?|拒收(?:投标文件)?|不予受理"
)
_EVIDENCE_SIGNAL_RE = re.compile(
    r"提供|提交|附|上传|扫描件|证明材料|合同|证书|报告|复印件|原件"
)
_PURE_FLOW_RE = re.compile(
    r"评标委员会(?:完成评标后)?(?:形成|编制|提交)?评标报告|"
    r"评标委员会组成人数|评标委员会由|评标程序|评标原则|开标程序|"
    r"评标报告应当包括|评标委员会成员名单|"
    r"评标专家评分原始记录表和否决(?:投标|响应)的情况说明"
)
_MAJOR_CHAPTER_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百千万0-9０-９]+章|"
    r"[一二三四五六七八九十百千万]+、)"
)
_NON_EVALUATION_MAJOR_RE = re.compile(
    r"合同条款|技术规范书|技术需求|投标文件格式|其他附件|招标公告|投标人须知"
)


class EvaluationRuleExtractionError(RuntimeError):
    """Raised when an evaluation-rule result cannot remain source-grounded."""


@dataclass(frozen=True)
class EvaluationRegion:
    title: str
    section: str
    block_ids: list[str]
    blocks: list[StructuredBlock]
    text: str
    order: int
    kind: Literal["scoring", "veto", "mixed"]


@dataclass(frozen=True)
class EvaluationCandidate:
    block_ids: list[str]
    section: str
    title: str
    text: str
    order: int
    region_kind: Literal["scoring", "veto", "mixed"]
    table_block_ids: list[str]
    blocks: list[StructuredBlock] = field(default_factory=list)


@dataclass(frozen=True)
class _EvaluationBatchExecution:
    normalized: dict[str, list[dict[str, Any]]]
    llm_total_calls: int
    llm_completed_calls: int
    llm_failed_calls: int
    llm_retries: int
    llm_elapsed_ms: int
    llm_call_elapsed_ms: list[int]
    llm_prompt_tokens: int


@dataclass
class _EvaluationConcurrencyState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    inflight: int = 0
    max_inflight: int = 0


class _EvaluationBatchFailure(EvaluationRuleExtractionError):
    def __init__(
        self,
        message: str,
        execution: _EvaluationBatchExecution,
    ) -> None:
        super().__init__(message)
        self.execution = execution


class EvaluationRuleLLM(Protocol):
    model: str

    def extract(
        self, candidates: Sequence[EvaluationCandidate]
    ) -> dict[str, list[dict[str, Any]]]: ...


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", value)


def _heading_level(block: StructuredBlock) -> int | None:
    if block.heading_level is not None:
        return block.heading_level
    value = block.metadata.get("text_level")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_navigation_title(text: str) -> bool:
    compact = _compact(text)
    if re.search(r"资格审查资料|评审索引|评分索引|投标文件组成", compact):
        return True
    return bool(
        re.search(
            r"索引表|目录|对应页码|评审因素.*页码|"
            r"内容建议.*评标办法.*评审标准.*顺序.*罗列",
            compact,
        )
        and re.search(r"评审|评分|评标", compact)
    )


def _is_standalone_title(block: StructuredBlock) -> bool:
    if block.type == "heading":
        return True
    text = block.text.strip()
    if not text or len(text) > 100:
        return False
    if block.section.strip() == text:
        return True
    return not re.search(r"[。；;，,：:]", text)


def _title_kind(title: str) -> Literal["scoring", "veto", "mixed"]:
    compact = _compact(title)
    has_score = bool(_SCORE_SIGNAL_RE.search(compact))
    has_veto = bool(_VETO_SIGNAL_RE.search(compact))
    if has_score and has_veto:
        return "mixed"
    if has_veto or re.search(r"初步评审|资格审查|符合性审查|废标|无效投标", compact):
        return "veto"
    return "scoring"


def _is_evaluation_title(block: StructuredBlock) -> bool:
    return (
        _is_standalone_title(block)
        and bool(_EVALUATION_TITLE_RE.search(_compact(block.text)))
        and not _is_navigation_title(block.text)
    )


def _is_non_evaluation_major_boundary(block: StructuredBlock) -> bool:
    if block.type != "heading":
        return False
    compact = _compact(block.text)
    if not _MAJOR_CHAPTER_RE.search(compact):
        return False
    return bool(_NON_EVALUATION_MAJOR_RE.search(compact))


def _is_pure_flow_block(block: StructuredBlock) -> bool:
    if block.type != "paragraph":
        return False
    compact = _compact(block.text)
    if not _PURE_FLOW_RE.search(compact):
        return False
    # A report's required contents can mention scoring or vetoes without
    # creating an executable rule of its own.
    if re.search(
        r"评标报告应当包括|评标专家评分原始记录表和否决(?:投标|响应)的情况说明",
        compact,
    ):
        return True
    return not (_VETO_SIGNAL_RE.search(block.text) or _SCORE_SIGNAL_RE.search(block.text))


def _region_kind(blocks: Sequence[StructuredBlock]) -> Literal["scoring", "veto", "mixed"]:
    has_score = any(_SCORE_SIGNAL_RE.search(block.text) for block in blocks)
    has_veto = any(_VETO_SIGNAL_RE.search(block.text) for block in blocks)
    if has_score and has_veto:
        return "mixed"
    if has_veto:
        return "veto"
    return "scoring"


def identify_evaluation_regions(
    blocks: Iterable[StructuredBlock],
) -> list[EvaluationRegion]:
    ordered = sorted(blocks, key=lambda block: block.order)
    regions: list[EvaluationRegion] = []
    current: list[StructuredBlock] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        kind = _region_kind(current)
        if any(
            block.type == "table"
            or _SCORE_SIGNAL_RE.search(block.text)
            or _VETO_SIGNAL_RE.search(block.text)
            for block in current
        ):
            regions.append(
                EvaluationRegion(
                    title=current[0].text.strip(),
                    section=current[0].section or current[0].text.strip(),
                    block_ids=[block.block_id for block in current],
                    blocks=list(current),
                    text="\n".join(block.text for block in current),
                    order=current[0].order,
                    kind=kind,
                )
            )
        current = []

    for block in ordered:
        if _is_evaluation_title(block):
            if current:
                incoming_level = _heading_level(block)
                first_level = _heading_level(current[0])
                starts_top_level_region = incoming_level is not None and (
                    first_level is None or incoming_level <= first_level
                )
                if (
                    _is_non_evaluation_major_boundary(block)
                    or starts_top_level_region
                ):
                    flush()
            if not current:
                current = [block]
            else:
                current.append(block)
            continue
        if current:
            if _is_non_evaluation_major_boundary(block):
                flush()
            else:
                current.append(block)
    flush()
    return regions


def _candidate_blocks(region: EvaluationRegion) -> list[StructuredBlock]:
    concrete_indexes = [
        index
        for index, block in enumerate(region.blocks)
        if block.type == "table"
        or _SCORE_SIGNAL_RE.search(block.text)
        or _VETO_SIGNAL_RE.search(block.text)
    ]
    if not concrete_indexes:
        return []
    start = min(concrete_indexes)
    end = max(concrete_indexes)
    selected = list(region.blocks[start : end + 1])
    if start > 0:
        selected = list(region.blocks[: end + 1])
    # Keep evidence paragraphs adjacent to a scoring table/rule, but remove
    # pure descriptions of the committee's process and report preparation.
    return [block for block in selected if not _is_pure_flow_block(block)]


def build_evaluation_candidates(
    blocks: Iterable[StructuredBlock],
) -> list[EvaluationCandidate]:
    candidates: list[EvaluationCandidate] = []
    for region in identify_evaluation_regions(blocks):
        selected = _candidate_blocks(region)
        if not selected:
            continue
        table_ids = [block.block_id for block in selected if block.type == "table"]
        candidates.append(
        EvaluationCandidate(
                block_ids=[block.block_id for block in selected],
                section=region.section,
                title=region.title,
                text="\n".join(block.text for block in selected),
                order=region.order,
                region_kind=region.kind,
                table_block_ids=table_ids,
                blocks=selected,
            )
        )
    return candidates


def _candidate_serialized_chars(candidate: EvaluationCandidate) -> int:
    return len(
        f"[{candidate.region_kind}] [{candidate.section}] "
        f"[{candidate.title}] block_ids={','.join(candidate.block_ids)}\n"
        f"{candidate.text}"
    )


def build_evaluation_batches(
    candidates: Sequence[EvaluationCandidate],
    *,
    max_batches: int = 8,
    max_batch_chars: int = 8000,
) -> list[list[EvaluationCandidate]]:
    if max_batches < 1 or max_batches > 10:
        raise ValueError("max_batches 必须在 1 到 10 之间。")
    if max_batch_chars < 1:
        raise ValueError("max_batch_chars 必须大于 0。")
    if not candidates:
        return []
    batches: list[list[EvaluationCandidate]] = []
    current: list[EvaluationCandidate] = []
    current_chars = 0
    for candidate in candidates:
        candidate_chars = _candidate_serialized_chars(candidate)
        separator = 2 if current else 0
        if current and current_chars + separator + candidate_chars > max_batch_chars:
            batches.append(current)
            current = []
            current_chars = 0
            separator = 0
        current.append(candidate)
        current_chars += separator + candidate_chars
    if current:
        batches.append(current)
    if len(batches) > max_batches:
        raise EvaluationRuleExtractionError(
            "完整评标章节超过 LLM 批次预算，未截断候选内容。"
        )
    return batches


_EVALUATION_OUTPUT_KEYS = (
    "score_categories",
    "score_items",
    "veto_rules",
    "uncertain_rules",
)
_EVALUATION_ALLOWED_FIELDS: dict[str, set[str]] = {
    "score_categories": {
        "name",
        "parent_id",
        "full_score",
        "original_rule",
        "conditions",
        "structure_status",
        "source_block_ids",
    },
    "score_items": {
        "name",
        "category_id",
        "parent_item_id",
        "original_rule",
        "conditions",
        "scoring_method",
        "full_score",
        "evidence_requirements",
        "evaluation_type",
        "source_block_ids",
    },
    "veto_rules": {
        "name",
        "trigger_condition",
        "consequence",
        "non_substantive_response",
        "evidence_requirements",
        "original_rule",
        "source_block_ids",
    },
    "uncertain_rules": {
        "rule_type",
        "description",
        "original_rule",
        "uncertainty_reason",
        "source_block_ids",
    },
}


def _empty_llm_output() -> dict[str, list[dict[str, Any]]]:
    return {key: [] for key in _EVALUATION_OUTPUT_KEYS}


def _require_string(
    item: dict[str, Any],
    field_name: str,
    *,
    required: bool = True,
) -> str:
    value = item.get(field_name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or required and not value.strip():
        raise EvaluationRuleExtractionError(
            f"LLM Schema 校验失败：{field_name} 必须是非空字符串。"
        )
    return value.strip()


def _require_source_ids(item: dict[str, Any]) -> list[str]:
    source_ids = item.get("source_block_ids")
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or not all(isinstance(value, str) and value for value in source_ids)
    ):
        raise EvaluationRuleExtractionError(
            "LLM Schema 校验失败：source_block_ids 无效。"
        )
    return list(dict.fromkeys(source_ids))


def _optional_score(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationRuleExtractionError(
            "LLM Schema 校验失败：full_score 必须是数字或 null。"
        )
    return float(value)


def _coerce_structured_field(item: dict[str, Any], field_name: str) -> dict[str, Any]:
    value = item.get(field_name, {})
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return {"items": list(value)}
    if isinstance(value, str):
        return {"description": value.strip()}
    raise EvaluationRuleExtractionError(
        f"LLM Schema 校验失败：{field_name} 必须是对象、数组、字符串或 null。"
    )


def _coerce_evaluation_output(
    value: Any,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise EvaluationRuleExtractionError(
            "LLM Schema 校验失败：对象结果不是 JSON 对象。"
        )
    if set(value) - set(_EVALUATION_OUTPUT_KEYS):
        raise EvaluationRuleExtractionError(
            "LLM Schema 校验失败：包含未允许的顶层字段。"
        )
    result = _empty_llm_output()
    for kind, allowed_fields in _EVALUATION_ALLOWED_FIELDS.items():
        items = value.get(kind, [])
        if not isinstance(items, list):
            raise EvaluationRuleExtractionError(
                f"LLM Schema 校验失败：{kind} 必须是数组。"
            )
        for raw_item in items:
            if not isinstance(raw_item, dict):
                raise EvaluationRuleExtractionError(
                    f"LLM Schema 校验失败：{kind} 条目不是对象。"
                )
            if set(raw_item) - allowed_fields:
                raise EvaluationRuleExtractionError(
                    f"LLM Schema 校验失败：{kind} 包含未允许字段。"
                )
            item = dict(raw_item)
            _require_source_ids(item)
            if kind == "score_categories":
                _require_string(item, "name")
                _require_string(item, "original_rule")
                if item.get("parent_id") is not None:
                    _require_string(item, "parent_id")
                item["full_score"] = _optional_score(item.get("full_score"))
                item["conditions"] = _coerce_structured_field(item, "conditions")
                if item.get("structure_status") is None:
                    item["structure_status"] = "explicit"
                if not isinstance(item.get("structure_status", "explicit"), str):
                    raise EvaluationRuleExtractionError(
                        "LLM Schema 校验失败：structure_status 无效。"
                    )
                item.setdefault("conditions", {})
                item.setdefault("structure_status", "explicit")
            elif kind == "score_items":
                _require_string(item, "name")
                _require_string(item, "original_rule")
                for field_name in ("category_id", "parent_item_id"):
                    if item.get(field_name) is not None:
                        _require_string(item, field_name)
                for field_name in ("conditions", "scoring_method"):
                    item[field_name] = _coerce_structured_field(item, field_name)
                evidence = item.get("evidence_requirements", [])
                if evidence is None:
                    evidence = []
                elif isinstance(evidence, str):
                    evidence = [evidence]
                if not isinstance(evidence, list) or not all(
                    isinstance(value, str) for value in evidence
                ):
                    raise EvaluationRuleExtractionError(
                        "LLM Schema 校验失败：evidence_requirements 无效。"
                    )
                item["evidence_requirements"] = evidence
                evaluation_type = item.get("evaluation_type")
                if evaluation_type not in {"objective", "subjective", "mixed"}:
                    raise EvaluationRuleExtractionError(
                        "LLM Schema 校验失败：evaluation_type 无效。"
                    )
                item["full_score"] = _optional_score(item.get("full_score"))
                item.setdefault("category_id", None)
                item.setdefault("parent_item_id", None)
                item.setdefault("conditions", {})
                item.setdefault("scoring_method", {})
            elif kind == "veto_rules":
                _require_string(item, "name")
                _require_string(item, "trigger_condition")
                _require_string(item, "consequence")
                _require_string(item, "original_rule")
                additional_consequence = item.get("non_substantive_response")
                if additional_consequence is not None and not isinstance(
                    additional_consequence, str
                ):
                    raise EvaluationRuleExtractionError(
                        "LLM Schema 校验失败：non_substantive_response 无效。"
                    )
                item["additional_consequence"] = (
                    additional_consequence.strip()
                    if isinstance(additional_consequence, str)
                    else None
                )
                evidence = item.get("evidence_requirements", [])
                if evidence is None:
                    evidence = []
                elif isinstance(evidence, str):
                    evidence = [evidence]
                if not isinstance(evidence, list) or not all(
                    isinstance(value, str) for value in evidence
                ):
                    raise EvaluationRuleExtractionError(
                        "LLM Schema 校验失败：evidence_requirements 无效。"
                    )
                item["evidence_requirements"] = evidence
            else:
                for field_name in (
                    "rule_type",
                    "description",
                    "original_rule",
                    "uncertainty_reason",
                ):
                    _require_string(item, field_name)
            item["source_block_ids"] = _require_source_ids(item)
            result[kind].append(item)
    return result


def _compact_rule_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]*>", "", value))
    return re.sub(r"[\s，。；、：:（）()【】“”\"'《》…,.!?！？\-—_]", "", value)


def _source_supports_rule(rule: str, source_text: str) -> bool:
    rule_key = _compact_rule_text(rule)
    source_key = _compact_rule_text(source_text)
    if not rule_key:
        return False
    if rule_key in source_key:
        return True
    return any(
        len(_compact_rule_text(clause)) >= 8
        and _compact_rule_text(clause) in source_key
        for clause in re.split(r"[，。；、：:,.!?！？…]+", rule)
    )


def _has_explicit_veto_consequence(text: str) -> bool:
    if re.search(r"可能(?:会)?导致[^。；\n]{0,30}(?:被否决|废标|无效)", text):
        return False
    return bool(_EXPLICIT_VETO_CONSEQUENCE_RE.search(text))


def _source_for_ids(
    source_ids: Sequence[str],
    candidates: Sequence[EvaluationCandidate],
) -> tuple[EvaluationCandidate, list[str], str]:
    source_set = set(source_ids)
    matching = [
        candidate
        for candidate in candidates
        if source_set.issubset(set(candidate.block_ids))
    ]
    if not matching:
        raise EvaluationRuleExtractionError(
            "评标规则来源 block_id 不存在或跨越多个完整候选章节。"
        )
    candidate = min(matching, key=lambda item: item.order)
    ordered_ids = [
        block.block_id
        for block in candidate.blocks
        if block.block_id in source_set
    ]
    if not ordered_ids:
        ordered_ids = [block_id for block_id in candidate.block_ids if block_id in source_set]
    blocks_by_id = {
        block.block_id: block
        for block in candidate.blocks
        if block.block_id in source_set
    }
    source_text = "\n".join(
        blocks_by_id[block_id].text
        for block_id in ordered_ids
        if block_id in blocks_by_id
    )
    if not source_text:
        source_text = candidate.text
    return candidate, ordered_ids, source_text


def _rule_source_ids(
    rule: str,
    candidate: EvaluationCandidate,
    hinted_ids: Sequence[str],
) -> list[str]:
    hinted_set = set(hinted_ids)
    hinted_source = "\n".join(
        block.text for block in candidate.blocks if block.block_id in hinted_set
    )
    # A model occasionally returns every block in a candidate. Preserve a
    # deliberately small set (for example, a table row plus its evidence
    # paragraph), but rebind a whole-candidate hint to the block containing
    # the actual rule text whenever possible.
    is_broad_hint = hinted_set == set(candidate.block_ids) or len(hinted_ids) > 4
    if not is_broad_hint and len(hinted_ids) <= 8 and _source_supports_rule(
        rule, hinted_source
    ):
        return list(hinted_ids)
    rule_key = _compact_rule_text(rule)
    clauses = [
        _compact_rule_text(clause)
        for clause in re.split(r"[，。；、：:,.!?！？…]+", rule)
        if len(_compact_rule_text(clause)) >= 8
    ]
    if not candidate.blocks or not clauses and not rule_key:
        return list(hinted_ids)
    exact_matches = [
        block.block_id
        for block in candidate.blocks
        if rule_key and rule_key in _compact_rule_text(block.text)
    ]
    if exact_matches:
        return exact_matches[:1]
    clause_matches = [
        block.block_id
        for block in candidate.blocks
        if any(clause in _compact_rule_text(block.text) for clause in clauses)
    ]
    return clause_matches or list(hinted_ids)


def _source_for_rule_ids(
    rule: str,
    hinted_ids: Sequence[str],
    candidates: Sequence[EvaluationCandidate],
) -> tuple[EvaluationCandidate, list[str], str]:
    candidate, _, _ = _source_for_ids(hinted_ids, candidates)
    resolved_ids = _rule_source_ids(rule, candidate, hinted_ids)
    if resolved_ids == list(hinted_ids):
        return _source_for_ids(hinted_ids, candidates)
    return _source_for_ids(resolved_ids, [candidate])


def _source_payload(
    candidate: EvaluationCandidate,
    source_ids: Sequence[str],
    source_text: str,
) -> dict[str, Any]:
    return {
        "section": candidate.section,
        "block_ids": list(source_ids),
        "source_text": source_text,
    }


def _uncertain_from_item(
    item: dict[str, Any],
    *,
    candidate: EvaluationCandidate,
    source_ids: Sequence[str],
    source_text: str,
    reason: str,
    description: str,
    rule_type: str,
) -> dict[str, Any]:
    return {
        "rule_type": rule_type,
        "description": description,
        "original_rule": item.get("original_rule", ""),
        "uncertainty_reason": reason,
        "source": _source_payload(candidate, source_ids, source_text),
    }


def _resolve_ref(
    ref: Any,
    *,
    ids: Sequence[str],
    names: Sequence[str],
) -> str | None:
    if ref is None:
        return None
    if not isinstance(ref, str):
        return None
    if ref in ids:
        return ref
    compact_ref = _compact_rule_text(ref)
    for item_id, name in zip(ids, names, strict=False):
        if compact_ref and compact_ref == _compact_rule_text(name):
            return item_id
    return None


def _normalize_evaluation_sources(
    output: dict[str, list[dict[str, Any]]],
    candidates: Sequence[EvaluationCandidate],
) -> dict[str, list[dict[str, Any]]]:
    normalized = _empty_llm_output()
    category_ids = [
        f"category_{index:03d}"
        for index in range(1, len(output["score_categories"]) + 1)
    ]
    category_names = [
        item["name"] for item in output["score_categories"]
    ]
    item_ids: list[str] = []
    item_names: list[str] = []
    for index, raw_item in enumerate(output["score_categories"], start=1):
        candidate, source_ids, source_text = _source_for_rule_ids(
            raw_item["original_rule"], raw_item["source_block_ids"], candidates
        )
        if not _source_supports_rule(raw_item["original_rule"], source_text):
            normalized["uncertain_rules"].append(
                _uncertain_from_item(
                    raw_item,
                    candidate=candidate,
                    source_ids=source_ids,
                    source_text=source_text,
                    reason="source_text_not_supported",
                    description=raw_item["name"],
                    rule_type="score_category",
                )
            )
            continue
        parent_id = _resolve_ref(
            raw_item.get("parent_id"),
            ids=category_ids,
            names=category_names,
        )
        normalized["score_categories"].append(
            {
                "id": category_ids[index - 1],
                "name": raw_item["name"].strip(),
                "parent_id": parent_id,
                "full_score": raw_item.get("full_score"),
                "original_rule": raw_item["original_rule"].strip(),
                "conditions": dict(raw_item.get("conditions", {})),
                "structure_status": raw_item.get("structure_status", "explicit"),
                "source": _source_payload(candidate, source_ids, source_text),
            }
        )

    for index, raw_item in enumerate(output["score_items"], start=1):
        candidate, source_ids, source_text = _source_for_rule_ids(
            raw_item["original_rule"], raw_item["source_block_ids"], candidates
        )
        item_id = f"score_item_{index:03d}"
        category_id = _resolve_ref(
            raw_item.get("category_id"),
            ids=category_ids,
            names=category_names,
        )
        parent_item_id = _resolve_ref(
            raw_item.get("parent_item_id"),
            ids=item_ids,
            names=item_names,
        )
        if raw_item.get("category_id") is not None and category_id is None:
            normalized["uncertain_rules"].append(
                _uncertain_from_item(
                    raw_item,
                    candidate=candidate,
                    source_ids=source_ids,
                    source_text=source_text,
                    reason="category_reference_not_resolved",
                    description=raw_item["name"],
                    rule_type="score_item",
                )
            )
            continue
        if raw_item.get("parent_item_id") is not None and parent_item_id is None:
            normalized["uncertain_rules"].append(
                _uncertain_from_item(
                    raw_item,
                    candidate=candidate,
                    source_ids=source_ids,
                    source_text=source_text,
                    reason="parent_item_reference_not_resolved",
                    description=raw_item["name"],
                    rule_type="score_item",
                )
            )
            continue
        if not _source_supports_rule(raw_item["original_rule"], source_text):
            normalized["uncertain_rules"].append(
                _uncertain_from_item(
                    raw_item,
                    candidate=candidate,
                    source_ids=source_ids,
                    source_text=source_text,
                    reason="source_text_not_supported",
                    description=raw_item["name"],
                    rule_type="score_item",
                )
            )
            continue
        normalized["score_items"].append(
            {
                "id": item_id,
                "name": raw_item["name"].strip(),
                "category_id": category_id,
                "parent_item_id": parent_item_id,
                "original_rule": raw_item["original_rule"].strip(),
                "conditions": dict(raw_item.get("conditions", {})),
                "scoring_method": dict(raw_item.get("scoring_method", {})),
                "full_score": raw_item.get("full_score"),
                "evidence_requirements": list(raw_item.get("evidence_requirements", [])),
                "evaluation_type": raw_item["evaluation_type"],
                "source": _source_payload(candidate, source_ids, source_text),
            }
        )
        item_ids.append(item_id)
        item_names.append(raw_item["name"])

    for index, raw_item in enumerate(output["veto_rules"], start=1):
        candidate, source_ids, source_text = _source_for_rule_ids(
            raw_item["original_rule"], raw_item["source_block_ids"], candidates
        )
        if not _source_supports_rule(raw_item["original_rule"], source_text):
            normalized["uncertain_rules"].append(
                _uncertain_from_item(
                    raw_item,
                    candidate=candidate,
                    source_ids=source_ids,
                    source_text=source_text,
                    reason="source_text_not_supported",
                    description=raw_item["name"],
                    rule_type="veto_rule",
                )
            )
            continue
        if not _has_explicit_veto_consequence(raw_item["original_rule"]):
            normalized["uncertain_rules"].append(
                _uncertain_from_item(
                    raw_item,
                    candidate=candidate,
                    source_ids=source_ids,
                    source_text=source_text,
                    reason="no_explicit_veto_consequence",
                    description=raw_item["name"],
                    rule_type="veto_rule",
                )
            )
            continue
        normalized["veto_rules"].append(
            {
                "id": f"veto_{index:03d}",
                "name": raw_item["name"].strip(),
                "trigger_condition": raw_item["trigger_condition"].strip(),
                "consequence": raw_item["consequence"].strip(),
                "additional_consequence": raw_item.get("additional_consequence"),
                "evidence_requirements": list(raw_item.get("evidence_requirements", [])),
                "original_rule": raw_item["original_rule"].strip(),
                "source": _source_payload(candidate, source_ids, source_text),
            }
        )

    for raw_item in output["uncertain_rules"]:
        candidate, source_ids, source_text = _source_for_rule_ids(
            raw_item["original_rule"], raw_item["source_block_ids"], candidates
        )
        normalized["uncertain_rules"].append(
            {
                "id": "",
                "rule_type": raw_item["rule_type"].strip(),
                "description": raw_item["description"].strip(),
                "original_rule": raw_item["original_rule"].strip(),
                "uncertainty_reason": raw_item["uncertainty_reason"].strip(),
                "source": _source_payload(candidate, source_ids, source_text),
            }
        )

    for index, item in enumerate(normalized["uncertain_rules"], start=1):
        item["id"] = f"uncertain_{index:03d}"
    return normalized


class DeterministicEvaluationRuleLLM:
    model = "deterministic"

    def extract(
        self, candidates: Sequence[EvaluationCandidate]
    ) -> dict[str, list[dict[str, Any]]]:
        result = _empty_llm_output()
        for candidate in candidates:
            if not (
                _SCORE_SIGNAL_RE.search(candidate.text)
                or _VETO_SIGNAL_RE.search(candidate.text)
            ):
                continue
            result["uncertain_rules"].append(
                {
                    "rule_type": candidate.region_kind,
                    "description": candidate.title,
                    "original_rule": candidate.text,
                    "uncertainty_reason": (
                        "未配置 LLM，无法可靠确认评分表层级、分值或否决条件关系。"
                    ),
                    "source_block_ids": list(candidate.block_ids),
                }
            )
        return result


class OpenAICompatibleEvaluationRuleLLM:
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
        self._last_usage = threading.local()

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

    def _call_context_value(self) -> dict[str, Any]:
        return getattr(self._call_context, "value", {})

    def extract(
        self, candidates: Sequence[EvaluationCandidate]
    ) -> dict[str, list[dict[str, Any]]]:
        source = "\n\n".join(
            f"[{candidate.region_kind}] [{candidate.section}] "
            f"[{candidate.title}] block_ids={','.join(candidate.block_ids)}\n"
            f"{candidate.text}"
            for candidate in candidates
        )
        prompt = (
            "你是招标文件评标规则结构化提取器。只处理下面已经按完整章节和完整评分表"
            "整理的招标文件上下文，不得扫描上下文之外的文档，不得猜测缺失的表格行列或规则。"
            "必须保留评分大类与评分项的父子关系、完整原文、满分、评分条件、计分方式、"
            "分档、时间/数量/金额/比例/上下限、证明材料及特殊要求。"
            "明确数量/金额/日期/固定分值/公式可标 objective；需要评委判断的内容标 subjective；"
            "两者同时存在标 mixed。只提取有实际评分效果或明确否决/废标/无效/不予接收后果的规则。"
            "普通评标流程、评标委员会组成、评标报告、开标说明和没有明确后果的提醒不要进入正式规则。"
            "不确定的表格关系必须写入 uncertain_rules，不得强行配对。"
            "只返回 JSON 对象，顶层只能包含 score_categories、score_items、veto_rules、uncertain_rules。"
            "每项必须包含 source_block_ids，且只能复制输入中的真实 block_id；不得返回 id 或 source_text。"
            "source_block_ids 只返回实际包含该条原文的最少 block，禁止复制整个候选章节的 block_id 列表；"
            "单条规则通常不超过 8 个来源 block。"
            "score_categories 字段为 name、parent_id、full_score、original_rule、conditions、"
            "structure_status、source_block_ids；score_items 字段为 name、category_id、"
            "parent_item_id、original_rule、conditions、scoring_method、full_score、"
            "evidence_requirements、evaluation_type、source_block_ids；"
            "veto_rules 字段为 name、trigger_condition、consequence、evidence_requirements、"
            "original_rule、source_block_ids；uncertain_rules 字段为 rule_type、description、"
            "original_rule、uncertainty_reason、source_block_ids。关系引用请使用同批次中"
            "相应的大类/评分项编号 category_001、score_item_001；无法确认就留 null 或写 uncertain_rules。"
            "\n\n"
            + source
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "enable_thinking": False,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你只输出来源受限的评标规则 JSON。"},
                {"role": "user", "content": prompt},
            ],
        }
        context = self._call_context_value()
        recorder = context.get("recorder")
        call_id = context.get("call_id")
        if recorder is not None and call_id is not None:
            recorder.attach_llm_input(call_id, payload)
        started_at = time.perf_counter()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Connection": "close",
                    },
                    method="POST",
                ),
                timeout=self.timeout_seconds,
            ) as response:
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
            if not isinstance(decoded, dict):
                raise ValueError("evaluation result must be an object")
            return decoded
        except TimeoutError as exc:
            raise EvaluationRuleExtractionError(
                "LLM 评标规则提取失败：请求超时。"
            ) from exc
        except urllib.error.HTTPError as exc:
            raise EvaluationRuleExtractionError(
                f"LLM 评标规则提取失败：HTTP {exc.code}。"
            ) from exc
        except urllib.error.URLError as exc:
            raise EvaluationRuleExtractionError(
                "LLM 评标规则提取失败：网络连接错误。"
            ) from exc
        except json.JSONDecodeError as exc:
            raise EvaluationRuleExtractionError(
                "LLM 评标规则提取失败：模型响应不是有效 JSON。"
            ) from exc
        except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise EvaluationRuleExtractionError(
                "LLM 评标规则提取失败：响应结构异常。"
            ) from exc
        finally:
            logger.info(
                "evaluation.llm.call elapsed_ms=%d model=%s candidates=%d",
                int((time.perf_counter() - started_at) * 1000),
                self.model,
                len(candidates),
            )


def _serialize_candidate(candidate: EvaluationCandidate) -> dict[str, Any]:
    return {
        "block_ids": list(candidate.block_ids),
        "section": candidate.section,
        "title": candidate.title,
        "text": candidate.text,
        "order": candidate.order,
        "region_kind": candidate.region_kind,
        "table_block_ids": list(candidate.table_block_ids),
    }


def _deserialize_evaluation_blocks(value: Any) -> list[StructuredBlock] | None:
    if not isinstance(value, list):
        return None
    result: list[StructuredBlock] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        try:
            block_id = str(raw["block_id"])
            block_type = raw["type"]
            text = str(raw.get("text", ""))
            section = str(raw.get("section", ""))
            order = int(raw["order"])
            metadata = raw.get("metadata", {})
            heading_level = raw.get("heading_level")
            if not isinstance(metadata, dict):
                return None
            if heading_level is not None:
                heading_level = int(heading_level)
            if block_type not in {"heading", "paragraph", "table", "image"}:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        result.append(
            StructuredBlock(
                block_id=block_id,
                type=block_type,
                text=text,
                section=section,
                order=order,
                metadata=dict(metadata),
                heading_level=heading_level,
            )
        )
    return result


def _evaluation_component_descriptor(component: Any) -> str:
    descriptor = {
        "type": f"{type(component).__module__}.{type(component).__qualname__}",
        "model": getattr(component, "model", None),
        "base_url": getattr(component, "base_url", None),
    }
    custom = getattr(component, "cache_descriptor", None)
    if callable(custom):
        descriptor["custom"] = custom()
    return json.dumps(descriptor, ensure_ascii=False, sort_keys=True)


def _evaluation_filter_report(
    blocks: Sequence[StructuredBlock],
    candidates: Sequence[EvaluationCandidate],
) -> list[dict[str, Any]]:
    candidate_block_ids = {
        block_id for candidate in candidates for block_id in candidate.block_ids
    }
    report: list[dict[str, Any]] = []
    for block in sorted(blocks, key=lambda item: item.order):
        if block.block_id in candidate_block_ids:
            continue
        compact = _compact(block.text)
        reason = None
        if _is_pure_flow_block(block):
            reason = "pure_evaluation_flow"
        elif _is_navigation_title(block.text):
            reason = "navigation_or_index_content"
        elif _is_non_evaluation_major_boundary(block):
            reason = "non_evaluation_major_boundary"
        elif (
            _EVALUATION_TITLE_RE.search(compact)
            or _SCORE_SIGNAL_RE.search(compact)
            or _VETO_SIGNAL_RE.search(compact)
        ):
            reason = "evaluation_signal_outside_candidate"
        if reason:
            report.append(
                {
                    "block_id": block.block_id,
                    "type": block.type,
                    "section": block.section,
                    "reason": reason,
                    "text": block.text,
                }
            )
    return report


def _evaluation_cache_key(
    path,
    parser: DocumentParser,
    llm: EvaluationRuleLLM,
    *,
    max_batches: int,
    max_batch_chars: int,
) -> str:
    descriptor = {
        "parser": _evaluation_component_descriptor(parser),
        "llm": _evaluation_component_descriptor(llm),
        "max_batches": max_batches,
        "max_batch_chars": max_batch_chars,
    }
    return hashlib.sha256(
        EVALUATION_RULE_CACHE_VERSION.encode("utf-8")
        + b"\0"
        + json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\0"
        + path.read_bytes()
    ).hexdigest()


def _valid_evaluation_result(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(key), list)
        for key in (
            "source_sections",
            "score_categories",
            "score_items",
            "veto_rules",
            "uncertain_rules",
        )
    ) and isinstance(value.get("stats"), dict)


def _merge_normalized_outputs(
    outputs: Sequence[dict[str, list[dict[str, Any]]]],
) -> dict[str, list[dict[str, Any]]]:
    merged = _empty_llm_output()
    category_id_map: dict[str, str] = {}
    item_id_map: dict[str, str] = {}
    for output in outputs:
        for category in output["score_categories"]:
            old_id = category["id"]
            new_id = f"category_{len(merged['score_categories']) + 1:03d}"
            category_id_map[old_id] = new_id
            category = dict(category)
            category["id"] = new_id
            if category.get("parent_id"):
                category["parent_id"] = category_id_map.get(category["parent_id"])
            merged["score_categories"].append(category)
        for item in output["score_items"]:
            old_id = item["id"]
            new_id = f"score_item_{len(merged['score_items']) + 1:03d}"
            item_id_map[old_id] = new_id
            item = dict(item)
            item["id"] = new_id
            if item.get("category_id"):
                item["category_id"] = category_id_map.get(item["category_id"])
            if item.get("parent_item_id"):
                item["parent_item_id"] = item_id_map.get(item["parent_item_id"])
            merged["score_items"].append(item)
        for kind in ("veto_rules", "uncertain_rules"):
            merged[kind].extend(dict(item) for item in output[kind])
    for index, item in enumerate(merged["veto_rules"], start=1):
        item["id"] = f"veto_{index:03d}"
    for index, item in enumerate(merged["uncertain_rules"], start=1):
        item["id"] = f"uncertain_{index:03d}"
    return merged


def _is_transient_evaluation_error(error: EvaluationRuleExtractionError) -> bool:
    cause = error.__cause__
    return isinstance(
        cause,
        (TimeoutError, ConnectionError, OSError, urllib.error.URLError),
    )


def _run_evaluation_batch(
    *,
    batch_index: int,
    batch_count: int,
    batch: Sequence[EvaluationCandidate],
    active_llm: EvaluationRuleLLM,
    active_recorder: ComplianceExtractionRecorder | None,
    max_retries: int,
    concurrency_state: _EvaluationConcurrencyState,
) -> _EvaluationBatchExecution:
    total_calls = 0
    completed_calls = 0
    failed_calls = 0
    retries = 0
    elapsed_ms_total = 0
    call_elapsed_ms: list[int] = []
    prompt_tokens = 0

    for attempt in range(1, max_retries + 2):
        call_id = (
            active_recorder.start_llm_call(
                batch_index=batch_index,
                batch_count=batch_count,
                attempt=attempt,
                model=getattr(active_llm, "model", type(active_llm).__name__),
                batch=[_serialize_candidate(candidate) for candidate in batch],
            )
            if active_recorder is not None
            else None
        )
        total_calls += 1
        if isinstance(active_llm, OpenAICompatibleEvaluationRuleLLM):
            active_llm.set_call_context(
                recorder=active_recorder,
                call_id=call_id,
            )
        attempt_started_at = time.perf_counter()
        semaphore_acquired = False
        try:
            _EVALUATION_LLM_SEMAPHORE.acquire()
            semaphore_acquired = True
            with concurrency_state.lock:
                concurrency_state.inflight += 1
                concurrency_state.max_inflight = max(
                    concurrency_state.max_inflight,
                    concurrency_state.inflight,
                )
            try:
                raw_output = active_llm.extract(batch)
            finally:
                with concurrency_state.lock:
                    concurrency_state.inflight -= 1
                _EVALUATION_LLM_SEMAPHORE.release()
                semaphore_acquired = False

            output = _coerce_evaluation_output(raw_output)
            normalized = _normalize_evaluation_sources(output, batch)
            elapsed_ms = int((time.perf_counter() - attempt_started_at) * 1000)
            elapsed_ms_total += elapsed_ms
            call_elapsed_ms.append(elapsed_ms)
            completed_calls += 1
            usage = getattr(active_llm, "last_usage", None)
            if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
                prompt_tokens += usage["prompt_tokens"]
            if active_recorder is not None and call_id is not None:
                active_recorder.complete_llm_call(
                    call_id,
                    parsed_objects=output,
                    schema_valid=True,
                    elapsed_ms=elapsed_ms,
                )
            return _EvaluationBatchExecution(
                normalized=normalized,
                llm_total_calls=total_calls,
                llm_completed_calls=completed_calls,
                llm_failed_calls=failed_calls,
                llm_retries=retries,
                llm_elapsed_ms=elapsed_ms_total,
                llm_call_elapsed_ms=call_elapsed_ms,
                llm_prompt_tokens=prompt_tokens,
            )
        except Exception as exc:
            if semaphore_acquired:
                with concurrency_state.lock:
                    concurrency_state.inflight -= 1
                _EVALUATION_LLM_SEMAPHORE.release()
                semaphore_acquired = False
            elapsed_ms = int((time.perf_counter() - attempt_started_at) * 1000)
            elapsed_ms_total += elapsed_ms
            call_elapsed_ms.append(elapsed_ms)
            failed_calls += 1
            if active_recorder is not None and call_id is not None:
                active_recorder.fail_llm_call(
                    call_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=elapsed_ms,
                )
            error = (
                exc
                if isinstance(exc, EvaluationRuleExtractionError)
                else EvaluationRuleExtractionError(str(exc))
            )
            if attempt <= max_retries and _is_transient_evaluation_error(error):
                retries += 1
                continue
            raise _EvaluationBatchFailure(
                f"评标规则第{batch_index}/{batch_count}批提取失败：{error}",
                _EvaluationBatchExecution(
                    normalized=_empty_llm_output(),
                    llm_total_calls=total_calls,
                    llm_completed_calls=completed_calls,
                    llm_failed_calls=failed_calls,
                    llm_retries=retries,
                    llm_elapsed_ms=elapsed_ms_total,
                    llm_call_elapsed_ms=call_elapsed_ms,
                    llm_prompt_tokens=prompt_tokens,
                ),
            ) from error
    raise EvaluationRuleExtractionError(
        f"评标规则第{batch_index}/{batch_count}批提取未完成。"
    )


def _evaluation_stats(
    result: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    evaluation_types = {
        "objective": 0,
        "subjective": 0,
        "mixed": 0,
    }
    for item in result["score_items"]:
        evaluation_types[item["evaluation_type"]] += 1
    return {
        "score_category_count": len(result["score_categories"]),
        "score_item_count": len(result["score_items"]),
        "veto_rule_count": len(result["veto_rules"]),
        "uncertain_rule_count": len(result["uncertain_rules"]),
        "objective_score_item_count": evaluation_types["objective"],
        "subjective_score_item_count": evaluation_types["subjective"],
        "mixed_score_item_count": evaluation_types["mixed"],
    }


def _append_unrepresented_veto_signals(
    result: dict[str, list[dict[str, Any]]],
    candidates: Sequence[EvaluationCandidate],
) -> None:
    """Keep veto-looking source blocks visible when the model omits them."""

    covered_block_ids = {
        block_id
        for kind in ("score_categories", "score_items", "veto_rules", "uncertain_rules")
        for item in result[kind]
        for block_id in item.get("source", {}).get("block_ids", [])
    }
    for candidate in candidates:
        for block in candidate.blocks:
            if block.block_id in covered_block_ids or not _VETO_SIGNAL_RE.search(
                block.text
            ):
                continue
            result["uncertain_rules"].append(
                {
                    "id": "",
                    "rule_type": "unrepresented_veto_signal",
                    "description": "包含否决相关表述但未被模型可靠结构化的文档块",
                    "original_rule": block.text.strip(),
                    "uncertainty_reason": (
                        "文档块包含否决/无效后果信号，但模型输出未引用该来源块；"
                        "保留原文供人工核验，禁止据此自动执行否决。"
                    ),
                    "source": _source_payload(candidate, [block.block_id], block.text),
                }
            )
            covered_block_ids.add(block.block_id)
    for index, item in enumerate(result["uncertain_rules"], start=1):
        item["id"] = f"uncertain_{index:03d}"


def extract_tender_evaluation_rules(
    tender_file,
    *,
    parser: DocumentParser | None = None,
    llm: EvaluationRuleLLM | None = None,
    cache: Any | None = None,
    parser_cache: Any | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
    max_batches: int = 8,
    max_batch_chars: int = 8000,
    max_retries: int = 2,
) -> TenderEvaluationExtractionResult:
    started_at = time.perf_counter()
    path = Path(tender_file.storage_path)
    active_parser = parser or MinerUDocumentParser()
    active_llm: EvaluationRuleLLM = llm or DeterministicEvaluationRuleLLM()
    active_recorder = recorder
    if active_recorder is None and path.parent.exists():
        try:
            active_recorder = ComplianceExtractionRecorder.from_tender_path(path)
        except OSError:
            active_recorder = None

    stats: dict[str, Any] = {
        "schema_version": EVALUATION_RULE_SCHEMA_VERSION,
        "filename": tender_file.filename,
        "file_size": tender_file.size,
        "cache_enabled": cache is not None,
        "cache_hit": False,
        "parser_cache_enabled": parser_cache is not None,
        "parser_cache_hit": False,
        "parser": type(active_parser).__name__,
        "actual_parser": getattr(active_parser, "parser_name", type(active_parser).__name__),
        "parser_execution_source": None,
        "parser_elapsed_ms": None,
        "parsed_block_count": 0,
        "raw_text_chars": 0,
        "candidate_count": 0,
        "candidate_chars": 0,
        "prompt_chars": 0,
        "estimated_prompt_tokens": None,
        "estimated_prompt_tokens_is_approximate": True,
        "llm_model": getattr(active_llm, "model", None),
        "llm_total_calls": 0,
        "llm_completed_calls": 0,
        "llm_failed_calls": 0,
        "llm_retries": 0,
        "llm_elapsed_ms": 0,
        "llm_call_elapsed_ms": [],
        "llm_prompt_tokens": 0,
        "llm_concurrency_limit": EVALUATION_LLM_MAX_CONCURRENCY,
        "llm_worker_count": 0,
        "llm_max_concurrency": 0,
        "llm_wall_clock_ms": 0,
        "filtered_count": 0,
        "uncertain_rule_count": 0,
        "score_category_count": 0,
        "score_item_count": 0,
        "veto_rule_count": 0,
        "objective_score_item_count": 0,
        "subjective_score_item_count": 0,
        "mixed_score_item_count": 0,
        "total_elapsed_ms": None,
    }
    run_status = "failed"
    failure: Exception | None = None
    failed_stage: str | None = None
    result: TenderEvaluationExtractionResult | None = None

    def event(name: str, **fields: Any) -> None:
        if active_recorder is None:
            return
        try:
            active_recorder.event(name, **fields)
        except Exception:
            logger.exception("evaluation.artifact.event.error event=%s", name)

    def write_artifact(name: str, payload: Any) -> None:
        if active_recorder is None:
            return
        try:
            active_recorder.write_json(name, payload)
        except Exception:
            logger.exception("evaluation.artifact.write.error name=%s", name)

    def write_formal_artifact(result: TenderEvaluationExtractionResult) -> None:
        payload = {
            "schema_version": EVALUATION_RULE_SCHEMA_VERSION,
            "source_sections": result["source_sections"],
            "score_categories": result["score_categories"],
            "score_items": result["score_items"],
            "veto_rules": result["veto_rules"],
            "uncertain_rules": result["uncertain_rules"],
            "stats": result["stats"],
            "artifact_paths": {
                "candidates": "10_evaluation_rule_candidates.json",
                "rules": "11_evaluation_rules.json",
                "filter_report": "12_evaluation_filter_report.json",
            },
        }
        write_artifact("11_evaluation_rules.json", payload)

    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数。")
        cache_key = _evaluation_cache_key(
            path,
            active_parser,
            active_llm,
            max_batches=max_batches,
            max_batch_chars=max_batch_chars,
        )
        cached = cache.get(cache_key) if cache is not None else None
        if _valid_evaluation_result(cached):
            result = dict(cached)
            stats.update(result.get("stats", {}))
            stats["cache_hit"] = True
            result["stats"] = stats
            write_artifact(
                "10_evaluation_rule_candidates.json",
                {
                    "source": "result_cache",
                    "candidate_count": stats.get("candidate_count", 0),
                    "raw_text_chars": stats.get("raw_text_chars", 0),
                    "candidate_chars": stats.get("candidate_chars", 0),
                    "prompt_chars": stats.get("prompt_chars", 0),
                    "candidates": [],
                },
            )
            write_artifact("12_evaluation_filter_report.json", {"source": "result_cache", "items": []})
            write_formal_artifact(result)
            run_status = "complete"
            return result

        parser_cache_key = _parsed_document_cache_key(path, active_parser)
        parser_cache_value = (
            parser_cache.get(parser_cache_key) if parser_cache is not None else None
        )
        blocks = _deserialize_evaluation_blocks(parser_cache_value)
        if blocks is not None:
            stats["parser_cache_hit"] = True
            stats["parser_execution_source"] = "parser_cache"
        else:
            parser_started_at = time.perf_counter()
            blocks = active_parser.parse(path)
            stats["parser_elapsed_ms"] = int((time.perf_counter() - parser_started_at) * 1000)
            stats["parser_execution_source"] = "parse"
            diagnostics = getattr(active_parser, "parse_diagnostics", {})
            if isinstance(diagnostics, dict):
                stats["actual_parser"] = diagnostics.get(
                    "parser", stats["actual_parser"]
                )
                stats["parser_transport"] = diagnostics.get(
                    "service_protocol"
                )
                stats["mineru_called"] = bool(diagnostics.get("mineru_called"))
            if parser_cache is not None:
                parser_cache.set(
                    parser_cache_key,
                    [
                        {
                            "block_id": block.block_id,
                            "type": block.type,
                            "text": block.text,
                            "section": block.section,
                            "order": block.order,
                            "metadata": block.metadata,
                            "heading_level": block.heading_level,
                        }
                        for block in blocks
                    ],
                )
        stats["parsed_block_count"] = len(blocks)
        stats["raw_text_chars"] = sum(len(block.text) for block in blocks)
        candidates = build_evaluation_candidates(blocks)
        stats["candidate_count"] = len(candidates)
        stats["candidate_chars"] = sum(len(candidate.text) for candidate in candidates)
        stats["prompt_chars"] = sum(_candidate_serialized_chars(candidate) for candidate in candidates)
        stats["estimated_prompt_tokens"] = max(1, round(stats["prompt_chars"] / 4)) if candidates else 0
        filter_report = _evaluation_filter_report(blocks, candidates)
        stats["filtered_count"] = len(filter_report)
        write_artifact(
            "10_evaluation_rule_candidates.json",
            {
                "source": "parsed_document",
                "raw_text_chars": stats["raw_text_chars"],
                "candidate_count": len(candidates),
                "candidate_chars": stats["candidate_chars"],
                "prompt_chars": stats["prompt_chars"],
                "candidates": [_serialize_candidate(candidate) for candidate in candidates],
            },
        )
        if not candidates:
            result = {
                "source_sections": [],
                "score_categories": [],
                "score_items": [],
                "veto_rules": [],
                "uncertain_rules": [],
                "stats": stats,
            }
            write_artifact(
                "12_evaluation_filter_report.json",
                {"filtered_count": len(filter_report), "items": filter_report},
            )
            write_formal_artifact(result)
            if cache is not None:
                cache.set(cache_key, result)
            run_status = "complete"
            return result

        if isinstance(active_llm, DeterministicEvaluationRuleLLM):
            raw_batches = []
        else:
            raw_batches = build_evaluation_batches(
                candidates,
                max_batches=max_batches,
                max_batch_chars=max_batch_chars,
            )
        normalized_batches: list[dict[str, list[dict[str, Any]]]] = []
        if isinstance(active_llm, DeterministicEvaluationRuleLLM):
            raw_output = active_llm.extract(candidates)
            output = _coerce_evaluation_output(raw_output)
            normalized_batches.append(_normalize_evaluation_sources(output, candidates))
        else:
            batch_count = len(raw_batches)
            stats["llm_worker_count"] = min(
                EVALUATION_LLM_MAX_CONCURRENCY,
                batch_count,
            )
            concurrency_state = _EvaluationConcurrencyState()
            llm_wall_started_at = time.perf_counter()
            event(
                "evaluation.llm.parallel.start",
                batch_count=batch_count,
                worker_count=stats["llm_worker_count"],
                concurrency_limit=EVALUATION_LLM_MAX_CONCURRENCY,
            )
            batch_futures = {}
            batch_executions: dict[int, _EvaluationBatchExecution] = {}
            batch_errors: dict[int, Exception] = {}
            with ThreadPoolExecutor(
                max_workers=stats["llm_worker_count"],
                thread_name_prefix="evaluation-llm",
            ) as executor:
                for batch_index, batch in enumerate(raw_batches, start=1):
                    batch_futures[batch_index] = executor.submit(
                        _run_evaluation_batch,
                        batch_index=batch_index,
                        batch_count=batch_count,
                        batch=batch,
                        active_llm=active_llm,
                        active_recorder=active_recorder,
                        max_retries=max_retries,
                        concurrency_state=concurrency_state,
                    )
                for batch_index in sorted(batch_futures):
                    try:
                        batch_executions[batch_index] = batch_futures[
                            batch_index
                        ].result()
                    except Exception as exc:
                        batch_errors[batch_index] = exc

            stats["llm_wall_clock_ms"] = int(
                (time.perf_counter() - llm_wall_started_at) * 1000
            )
            stats["llm_max_concurrency"] = concurrency_state.max_inflight
            event(
                "evaluation.llm.parallel.end",
                batch_count=batch_count,
                worker_count=stats["llm_worker_count"],
                concurrency_limit=EVALUATION_LLM_MAX_CONCURRENCY,
                max_concurrency=stats["llm_max_concurrency"],
                wall_clock_ms=stats["llm_wall_clock_ms"],
                failed_batches=sorted(batch_errors),
            )

            for batch_index in sorted(batch_futures):
                execution = batch_executions.get(batch_index)
                if execution is None:
                    error = batch_errors[batch_index]
                    execution = getattr(error, "execution", None)
                if execution is None:
                    continue
                stats["llm_total_calls"] += execution.llm_total_calls
                stats["llm_completed_calls"] += execution.llm_completed_calls
                stats["llm_failed_calls"] += execution.llm_failed_calls
                stats["llm_retries"] += execution.llm_retries
                stats["llm_elapsed_ms"] += execution.llm_elapsed_ms
                stats["llm_call_elapsed_ms"].extend(execution.llm_call_elapsed_ms)
                stats["llm_prompt_tokens"] += execution.llm_prompt_tokens
                if batch_index in batch_executions:
                    normalized_batches.append(execution.normalized)

            if batch_errors:
                raise batch_errors[min(batch_errors)]

        merged = _merge_normalized_outputs(normalized_batches)
        _append_unrepresented_veto_signals(merged, candidates)
        source_sections = [
            {
                "section": candidate.section,
                "title": candidate.title,
                "block_ids": list(candidate.block_ids),
                "source_text": candidate.text,
            }
            for candidate in candidates
        ]
        stats.update(_evaluation_stats(merged))
        stats["uncertain_rule_count"] = len(merged["uncertain_rules"])
        stats["filtered_count"] = len(filter_report)
        if stats["llm_prompt_tokens"]:
            stats["estimated_prompt_tokens"] = stats["llm_prompt_tokens"]
            stats["estimated_prompt_tokens_is_approximate"] = False
        result = {
            "source_sections": source_sections,
            "score_categories": merged["score_categories"],
            "score_items": merged["score_items"],
            "veto_rules": merged["veto_rules"],
            "uncertain_rules": merged["uncertain_rules"],
            "stats": stats,
        }
        write_artifact(
            "12_evaluation_filter_report.json",
            {"filtered_count": len(filter_report), "items": filter_report},
        )
        write_formal_artifact(result)
        if cache is not None:
            cache.set(cache_key, result)
        run_status = "complete"
        event(
            "evaluation.extract.end",
            status="complete",
            score_categories=stats["score_category_count"],
            score_items=stats["score_item_count"],
            veto_rules=stats["veto_rule_count"],
            uncertain_rules=stats["uncertain_rule_count"],
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )
        return result
    except Exception as exc:
        failure = exc
        failed_stage = failed_stage or "evaluation_extraction"
        event(
            "evaluation.extract.error",
            status="failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )
        raise
    finally:
        stats["total_elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
        if active_recorder is not None:
            try:
                if result is not None:
                    write_formal_artifact(result)
                active_recorder.finalize(
                    status=run_status,
                    stats=stats,
                    failed_stage=failed_stage,
                    error_type=type(failure).__name__ if failure else None,
                    error_message=str(failure) if failure else None,
                    elapsed_ms=stats["total_elapsed_ms"],
                )
            except Exception:
                logger.exception("evaluation.artifact.finalize.error")
