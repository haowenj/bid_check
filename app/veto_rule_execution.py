from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata
from app.objective_scoring import load_reusable_bid_evidence

VETO_RULE_REVIEW_ARTIFACT = "veto_rule_reviews.json"
VETO_STATUSES = frozenset(
    {
        "triggered",
        "not_triggered",
        "evidence_insufficient",
        "file_scope_missing",
        "external_data_required",
        "other_bidder_data_required",
        "manual_review_required",
    }
)

_REUSABLE_ARTIFACT_NAMES = (
    "08_template_text_reviews.json",
    "09_attachment_reviews.json",
    "10_file_requirement_reviews.json",
    "10_performance_reviews.json",
)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _copy_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"section": "", "block_ids": [], "source_text": ""}
    block_ids = value.get("block_ids", [])
    return {
        "section": _as_text(value.get("section")),
        "block_ids": [str(item) for item in block_ids]
        if isinstance(block_ids, list)
        else [],
        "source_text": _as_text(value.get("source_text")),
    }


def _rule_text(rule: Mapping[str, Any]) -> str:
    return " ".join(
        value
        for value in (
            _as_text(rule.get("name")),
            _as_text(rule.get("trigger_condition")),
            _as_text(rule.get("original_rule")),
            _as_text(_copy_source(rule.get("source")).get("source_text")),
        )
        if value
    )


def _review_base(rule: Mapping[str, Any]) -> dict[str, Any]:
    source = _copy_source(rule.get("source"))
    evidence_requirements = rule.get("evidence_requirements", [])
    return {
        "id": _as_text(rule.get("id")),
        "name": _as_text(rule.get("name")),
        "original_rule": _as_text(rule.get("original_rule")),
        "trigger_condition": _as_text(rule.get("trigger_condition")),
        "consequence": _as_text(rule.get("consequence")),
        "additional_consequence": rule.get("additional_consequence"),
        "evidence_requirements": (
            [str(item) for item in evidence_requirements]
            if isinstance(evidence_requirements, list)
            else []
        ),
        "tender_rule_source": source,
        "status": "evidence_insufficient",
        "triggered": False,
        "facts_required": [],
        "confirmed_facts": [],
        "reason": "",
        "evidence": [],
        "bid_evidence": {"block_ids": [], "image_ids": []},
        "related_artifacts": [],
        "dependencies": {
            "external_data_required": False,
            "other_bidder_data_required": False,
            "manual_review_required": False,
        },
        "parent_rule_ids": [],
        "triggered_by": [],
    }


def _has_ordinary_fail(evidence: Mapping[str, Any]) -> bool:
    artifacts = evidence.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        return False
    for payload in artifacts.values():
        if not isinstance(payload, Mapping):
            continue
        for key in (
            "template_text_reviews",
            "attachment_reviews",
            "performance_reviews",
            "requirements",
            "file_requirement_reviews",
        ):
            entries = payload.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                if _as_text(entry.get("status")) == "fail":
                    return True
                issues = entry.get("issues")
                if isinstance(issues, list) and any(
                    isinstance(issue, Mapping)
                    and _as_text(issue.get("status")) == "fail"
                    for issue in issues
                ):
                    return True
    return False


def _default_review(
    review: dict[str, Any],
    *,
    ordinary_fail_present: bool,
) -> dict[str, Any]:
    if ordinary_fail_present:
        review["reason"] = (
            "现有产物仅提供普通合规检查问题，未证明该问题属于当前规则对应的正式评审项；"
            "不能把普通 fail 直接升级为否决。"
        )
    else:
        review["reason"] = (
            "当前已有投标文件事实和检查产物不足以将该规则的触发条件与明确证据对应，"
            "不能据此判定触发或未触发。"
        )
    return review


def _source_payload(
    evidence: Mapping[str, Any],
    *,
    objective_scores: Mapping[str, Any] | None,
) -> dict[str, Any]:
    artifacts = evidence.get("artifacts", {})
    artifact_paths = evidence.get("artifact_paths", {})
    source: dict[str, Any] = {
        "evaluation_rules_artifact": "11_evaluation_rules.json",
        "bid_filename": evidence.get("bid_filename"),
        "bid_path": evidence.get("bid_path"),
        "bid_document_artifact": evidence.get("bid_document_artifact"),
        "bid_document_source": evidence.get("bid_document_source"),
        "bid_document_hash_verified": bool(
            evidence.get("bid_document_hash_verified")
        ),
        "reused_artifacts": sorted(
            name
            for name in artifacts
            if name in _REUSABLE_ARTIFACT_NAMES
        ),
        "missing_artifacts": sorted(
            str(name) for name in evidence.get("missing_artifacts", [])
        ),
        "artifact_paths": {
            str(name): str(path)
            for name, path in artifact_paths.items()
            if name in _REUSABLE_ARTIFACT_NAMES
        },
    }
    if isinstance(objective_scores, Mapping):
        source["objective_scores_artifact"] = "objective_scores.json"
    return source


def _stats(
    reviews: list[dict[str, Any]],
    *,
    evidence: Mapping[str, Any],
    started_at: float,
) -> dict[str, Any]:
    status_counts = {status: 0 for status in sorted(VETO_STATUSES)}
    for review in reviews:
        status = _as_text(review.get("status"))
        if status in VETO_STATUSES:
            status_counts[status] += 1
    return {
        "formal_rule_count": len(reviews),
        "triggered_count": status_counts["triggered"],
        "status_counts": status_counts,
        "llm_total_calls": 0,
        "ocr_reused": bool(evidence.get("bid_document_hash_verified")),
        "duplicate_parse": False,
        "bid_parse_reused": bool(evidence.get("bid_document_hash_verified")),
        "missing_artifact_count": len(evidence.get("missing_artifacts", [])),
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
    }


def run_veto_rule_execution(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    existing_artifacts: Mapping[str, Any] | None = None,
    objective_scores: Mapping[str, Any] | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Execute formal veto rules from reusable structured evidence only."""

    started_at = time.perf_counter()
    evidence = load_reusable_bid_evidence(
        bid_file,
        bid_document=bid_document,
        artifact_dir=artifact_dir,
        existing_artifacts=existing_artifacts,
    )
    raw_rules = evaluation_rules.get("veto_rules", [])
    rules = [rule for rule in raw_rules if isinstance(rule, Mapping)] \
        if isinstance(raw_rules, list) else []
    ordinary_fail_present = _has_ordinary_fail(evidence)
    reviews = [
        _default_review(
            _review_base(rule),
            ordinary_fail_present=ordinary_fail_present,
        )
        for rule in rules
    ]
    result: dict[str, Any] = {
        "schema_version": "veto-rule-review-v1",
        "source": _source_payload(
            evidence,
            objective_scores=objective_scores,
        ),
        "veto_rule_reviews": reviews,
        "stats": _stats(
            reviews,
            evidence=evidence,
            started_at=started_at,
        ),
    }
    if recorder is not None:
        recorder.write_json(VETO_RULE_REVIEW_ARTIFACT, result)
        recorder.event(
            "veto.rule.execution.end",
            status="complete",
            formal_rule_count=result["stats"]["formal_rule_count"],
            triggered_count=result["stats"]["triggered_count"],
            llm_total_calls=0,
            elapsed_ms=result["stats"]["elapsed_ms"],
        )
    return result
