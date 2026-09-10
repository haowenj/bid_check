from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import BidCheckTask, FileMetadata, StageName
from app.repository import BidCheckRepository

logger = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _result_summary(value: Any) -> str:
    if isinstance(value, list):
        return f"list_count={len(value)}"
    if isinstance(value, dict):
        return f"dict_keys={len(value)}"
    return f"result_type={type(value).__name__}"


@dataclass(frozen=True)
class BidCheckServices:
    extract: Callable[[FileMetadata], dict[str, Any]]
    parse: Callable[[FileMetadata], dict[str, Any]]
    review: Callable[
        [dict[str, Any], dict[str, Any]],
        dict[str, Any],
    ]
    extract_with_recorder: Callable[..., dict[str, Any]] | None = None
    extract_evaluation_with_recorder: Callable[..., dict[str, Any]] | None = None
    score_objective_with_recorder: Callable[..., dict[str, Any]] | None = None
    score_subjective_with_recorder: Callable[..., dict[str, Any]] | None = None
    execute_veto_with_recorder: Callable[..., dict[str, Any]] | None = None
    review_with_recorder: Callable[..., dict[str, Any]] | None = None


class BidCheckWorkflow:
    def __init__(
        self,
        repository: BidCheckRepository,
        services: BidCheckServices,
        *,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.repository = repository
        self.services = services
        self.executor = executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="bid-check",
        )
        self._owns_executor = executor is None

    def run(self, task_id: str, retry_from: StageName | None = None) -> None:
        if retry_from is not None:
            self._run_retry(task_id, retry_from)
            return
        started_at = time.perf_counter()
        logger.info("workflow.run.start task_id=%s", task_id)
        task = self.repository.get(task_id)
        if task is None:
            logger.error(
                "workflow.run.error task_id=%s error_type=KeyError elapsed_ms=%d",
                task_id,
                _elapsed_ms(started_at),
            )
            raise KeyError(task_id)

        recorder: ComplianceExtractionRecorder | None = None
        try:
            recorder = ComplianceExtractionRecorder.from_tender_path(
                Path(task.tender_file.storage_path)
            )
        except OSError as recorder_error:
            logger.warning(
                "artifact.workflow.init.error task_id=%s error_type=%s",
                task_id,
                type(recorder_error).__name__,
            )
        workflow_stats: dict[str, Any] = {
            "task_id": task_id,
            "tender_file": task.tender_file.filename,
            "bid_file": task.bid_file.filename,
            "requirements_elapsed_ms": None,
            "bid_parse_elapsed_ms": None,
            "review_elapsed_ms": None,
            "evaluation_rules_elapsed_ms": None,
            "evaluation_elapsed_ms": None,
            "objective_scoring_elapsed_ms": None,
            "subjective_scoring_elapsed_ms": None,
            "veto_rule_execution_elapsed_ms": None,
            "total_elapsed_ms": None,
        }
        if recorder is not None:
            workflow_stats["artifact_directory"] = str(recorder.artifact_dir)

        def record_event(event: str, **fields: Any) -> None:
            if recorder is None:
                return
            try:
                recorder.event(event, **fields)
            except Exception as recorder_error:
                logger.error(
                    "artifact.workflow.event.error event=%s error_type=%s",
                    event,
                    type(recorder_error).__name__,
                )

        def finalize_workflow(
            *,
            status: str,
            failed_stage: str | None = None,
            error: Exception | None = None,
        ) -> None:
            workflow_stats["total_elapsed_ms"] = _elapsed_ms(started_at)
            if recorder is None:
                return
            summary = {
                "status": status,
                "task_id": task_id,
                "failed_stage": failed_stage,
                "error_type": type(error).__name__ if error else None,
                "error_message": str(error) if error else None,
                "stats": workflow_stats,
            }
            try:
                recorder.write_workflow_summary(summary)
                recorder.event(
                    "workflow.run.end",
                    status=status,
                    task_id=task_id,
                    failed_stage=failed_stage,
                    elapsed_ms=workflow_stats["total_elapsed_ms"],
                )
            except Exception as recorder_error:
                logger.error(
                    "artifact.workflow.finalize.error task_id=%s error_type=%s",
                    task_id,
                    type(recorder_error).__name__,
                )

        record_event("workflow.run.start", task_id=task_id)

        if task.check_mode == "evaluation":
            self._run_evaluation_phase(
                task_id,
                task,
                recorder=recorder,
                workflow_stats=workflow_stats,
                record_event=record_event,
                finalize_workflow=finalize_workflow,
                started_at=started_at,
                mode="evaluation",
            )
            return

        stage_started_at = {
            "requirements": time.perf_counter(),
            "bid_parse": time.perf_counter(),
        }
        record_event(
            "workflow.stage.start",
            stage="requirements",
            task_id=task_id,
            file=task.tender_file.filename,
        )
        logger.info(
            "workflow.stage.start stage=requirements task_id=%s file=%s",
            task_id,
            task.tender_file.filename,
        )
        self.repository.update_stage(task_id, "requirements", "running")
        record_event(
            "workflow.stage.start",
            stage="bid_parse",
            task_id=task_id,
            file=task.bid_file.filename,
        )
        logger.info(
            "workflow.stage.start stage=bid_parse task_id=%s file=%s",
            task_id,
            task.bid_file.filename,
        )
        self.repository.update_stage(task_id, "bid_parse", "running")
        requirements_future = (
            self.executor.submit(
                self.services.extract_with_recorder,
                task.tender_file,
                recorder,
            )
            if self.services.extract_with_recorder is not None
            else self.executor.submit(self.services.extract, task.tender_file)
        )
        futures = {
            requirements_future: "requirements",
            self.executor.submit(self.services.parse, task.bid_file): "bid_parse",
        }
        outputs: dict[StageName, Any] = {}
        failed = False
        failed_stage: StageName | None = None
        failure: Exception | None = None

        for future in as_completed(futures):
            stage = futures[future]
            try:
                outputs[stage] = future.result()
                self.repository.update_stage(task_id, stage, "complete")
                self.repository.update_result(
                    task_id,
                    {stage: outputs[stage]},
                )
                stage_elapsed_ms = _elapsed_ms(stage_started_at[stage])
                workflow_stats[f"{stage}_elapsed_ms"] = stage_elapsed_ms
                record_event(
                    "workflow.stage.end",
                    stage=stage,
                    task_id=task_id,
                    status="complete",
                    result=_result_summary(outputs[stage]),
                    elapsed_ms=stage_elapsed_ms,
                )
                logger.info(
                    "workflow.stage.end stage=%s task_id=%s status=complete %s elapsed_ms=%d",
                    stage,
                    task_id,
                    _result_summary(outputs[stage]),
                    _elapsed_ms(stage_started_at[stage]),
                )
            except Exception as exc:
                failed = True
                failed_stage = failed_stage or stage
                failure = failure or exc
                self.repository.fail(task_id, stage, str(exc))
                stage_elapsed_ms = _elapsed_ms(stage_started_at[stage])
                workflow_stats[f"{stage}_elapsed_ms"] = stage_elapsed_ms
                record_event(
                    "workflow.stage.error",
                    stage=stage,
                    task_id=task_id,
                    status="failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=stage_elapsed_ms,
                )
                logger.error(
                    "workflow.stage.error stage=%s task_id=%s status=failed error_type=%s elapsed_ms=%d",
                    stage,
                    task_id,
                    type(exc).__name__,
                    _elapsed_ms(stage_started_at[stage]),
                )

        if failed:
            finalize_workflow(
                status="failed",
                failed_stage=failed_stage or "parallel",
                error=failure,
            )
            logger.info(
                "workflow.run.end status=failed task_id=%s failed_stage=parallel elapsed_ms=%d",
                task_id,
                _elapsed_ms(started_at),
            )
            return

        review_started_at = time.perf_counter()
        record_event("workflow.review.start", task_id=task_id)
        try:
            logger.info("workflow.review.start task_id=%s", task_id)
            self.repository.update_stage(task_id, "review", "running")
            bid_parse_for_review = (
                dict(outputs["bid_parse"])
                if isinstance(outputs["bid_parse"], dict)
                else {"parsed_bid": outputs["bid_parse"]}
            )
            # Keep the original upload metadata beside the parsed document. The
            # file-level checker reads this metadata and stats the original
            # storage path, never a MinerU cleaning artifact.
            bid_parse_for_review["original_file_metadata"] = task.bid_file.to_dict()
            if self.services.review_with_recorder is not None:
                review_result = self.services.review_with_recorder(
                    outputs["requirements"],
                    bid_parse_for_review,
                    recorder=recorder,
                )
            else:
                review_result = self.services.review(
                    outputs["requirements"],
                    bid_parse_for_review,
                )
            compliance_result = {
                **outputs["requirements"],
                "bid_parse": outputs["bid_parse"],
                "review_result": review_result,
            }
            self.repository.update_result(
                task_id,
                {"review_result": review_result},
            )
            if task.check_mode == "full":
                self.repository.update_stage(task_id, "review", "complete")
                self.repository.update_result(task_id, compliance_result)
                record_event(
                    "workflow.compliance.end",
                    task_id=task_id,
                    mode="full",
                    status="complete",
                    result=_result_summary(review_result),
                )
                self._run_evaluation_phase(
                    task_id,
                    task,
                    recorder=recorder,
                    workflow_stats=workflow_stats,
                    record_event=record_event,
                    finalize_workflow=finalize_workflow,
                    started_at=started_at,
                    mode="full",
                    initial_result=compliance_result,
                )
                return
            self.repository.complete(task_id, compliance_result)
            review_elapsed_ms = _elapsed_ms(review_started_at)
            workflow_stats["review_elapsed_ms"] = review_elapsed_ms
            record_event(
                "workflow.review.end",
                task_id=task_id,
                status="complete",
                result=_result_summary(review_result),
                elapsed_ms=review_elapsed_ms,
            )
            finalize_workflow(status="complete")
            logger.info(
                "workflow.review.end task_id=%s status=complete %s elapsed_ms=%d",
                task_id,
                _result_summary(review_result),
                _elapsed_ms(review_started_at),
            )
            logger.info(
                "workflow.run.end status=complete task_id=%s elapsed_ms=%d",
                task_id,
                _elapsed_ms(started_at),
            )
        except Exception as exc:
            failed_stage = "review"
            failure = exc
            self.repository.fail(task_id, "review", str(exc))
            review_elapsed_ms = _elapsed_ms(review_started_at)
            workflow_stats["review_elapsed_ms"] = review_elapsed_ms
            record_event(
                "workflow.review.error",
                task_id=task_id,
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
                elapsed_ms=review_elapsed_ms,
            )
            finalize_workflow(
                status="failed",
                failed_stage=failed_stage,
                error=failure,
            )
            logger.error(
                "workflow.review.error task_id=%s status=failed error_type=%s elapsed_ms=%d",
                task_id,
                type(exc).__name__,
                _elapsed_ms(review_started_at),
            )
            logger.info(
                "workflow.run.end status=failed task_id=%s failed_stage=review elapsed_ms=%d",
                task_id,
                _elapsed_ms(started_at),
            )

    def _load_saved_stage_result(
        self,
        task: BidCheckTask,
        result_key: str,
        artifact_name: str | None,
    ) -> dict[str, Any]:
        stored_result = task.result if isinstance(task.result, dict) else {}
        stored = stored_result.get(result_key)
        if isinstance(stored, dict):
            return dict(stored)

        artifact_dir = Path(task.tender_file.storage_path).parent / "compliance_extraction"
        if result_key == "bid_parse":
            cleaning_dir = artifact_dir.parent / "bid_document_cleaning"
            document_path = cleaning_dir / "structured_document.json"
            summary_path = cleaning_dir / "cleaning_summary.json"
            try:
                document = json.loads(document_path.read_text(encoding="utf-8"))
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("投标文件解析产物不可复用，请从头开始。") from exc
            if not isinstance(document, dict) or not isinstance(summary, dict):
                raise RuntimeError("投标文件解析产物不可复用，请从头开始。")
            return {
                "status": "success",
                "document_name": task.bid_file.filename,
                "artifact_dir": str(cleaning_dir),
                "artifacts": {
                    "structured_document": str(document_path),
                },
                "stats": summary.get("stats", document.get("stats", {})),
                "diagnostics": summary.get("diagnostics", {}),
            }

        if artifact_name is not None:
            artifact_path = artifact_dir / artifact_name
            try:
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"{artifact_name} 不可复用，请从头开始。"
                ) from exc
            if isinstance(artifact, dict):
                return artifact
        raise RuntimeError(f"{result_key} 产物不可复用，请从头开始。")

    def _run_retry(self, task_id: str, retry_from: StageName) -> None:
        started_at = time.perf_counter()
        task = self.repository.get(task_id)
        if task is None:
            raise KeyError(task_id)

        recorder: ComplianceExtractionRecorder | None = None
        try:
            recorder = ComplianceExtractionRecorder.from_tender_path(
                Path(task.tender_file.storage_path)
            )
        except OSError as recorder_error:
            logger.warning(
                "artifact.workflow.init.error task_id=%s error_type=%s",
                task_id,
                type(recorder_error).__name__,
            )

        workflow_stats: dict[str, Any] = {
            "task_id": task_id,
            "tender_file": task.tender_file.filename,
            "bid_file": task.bid_file.filename,
            "retry_from": retry_from,
            "requirements_elapsed_ms": None,
            "bid_parse_elapsed_ms": None,
            "review_elapsed_ms": None,
            "evaluation_rules_elapsed_ms": None,
            "evaluation_elapsed_ms": None,
            "objective_scoring_elapsed_ms": None,
            "subjective_scoring_elapsed_ms": None,
            "veto_rule_execution_elapsed_ms": None,
            "total_elapsed_ms": None,
        }
        if recorder is not None:
            workflow_stats["artifact_directory"] = str(recorder.artifact_dir)

        def record_event(event: str, **fields: Any) -> None:
            if recorder is None:
                return
            try:
                recorder.event(event, **fields)
            except Exception as recorder_error:
                logger.error(
                    "artifact.workflow.event.error event=%s error_type=%s",
                    event,
                    type(recorder_error).__name__,
                )

        def finalize_workflow(
            *,
            status: str,
            failed_stage: str | None = None,
            error: Exception | None = None,
        ) -> None:
            workflow_stats["total_elapsed_ms"] = _elapsed_ms(started_at)
            if recorder is None:
                return
            summary = {
                "status": status,
                "task_id": task_id,
                "retry_from": retry_from,
                "failed_stage": failed_stage,
                "error_type": type(error).__name__ if error else None,
                "error_message": str(error) if error else None,
                "stats": workflow_stats,
            }
            try:
                recorder.write_workflow_summary(summary)
                recorder.event(
                    "workflow.run.end",
                    status=status,
                    task_id=task_id,
                    failed_stage=failed_stage,
                    retry_from=retry_from,
                    elapsed_ms=workflow_stats["total_elapsed_ms"],
                )
            except Exception as recorder_error:
                logger.error(
                    "artifact.workflow.finalize.error task_id=%s error_type=%s",
                    task_id,
                    type(recorder_error).__name__,
                )

        record_event(
            "workflow.retry.start",
            task_id=task_id,
            retry_from=retry_from,
            previous_failed_stage=task.failed_stage,
        )
        stored_result = dict(task.result) if isinstance(task.result, dict) else {}
        try:
            if task.check_mode == "evaluation":
                self._run_evaluation_phase(
                    task_id,
                    task,
                    recorder=recorder,
                    workflow_stats=workflow_stats,
                    record_event=record_event,
                    finalize_workflow=finalize_workflow,
                    started_at=started_at,
                    mode="evaluation",
                    initial_result=stored_result,
                    retry_from=retry_from,
                )
                return
            self._run_compliance_retry(
                task_id,
                task,
                recorder=recorder,
                workflow_stats=workflow_stats,
                record_event=record_event,
                finalize_workflow=finalize_workflow,
                started_at=started_at,
                retry_from=retry_from,
            )
        except Exception as exc:
            failure_stage = retry_from if retry_from in {
                "requirements",
                "bid_parse",
                "review",
                "evaluation_rules",
                "objective_scoring",
                "subjective_scoring",
                "veto_rule_execution",
            } else "review"
            self.repository.fail(task_id, failure_stage, str(exc))
            finalize_workflow(
                status="failed",
                failed_stage=failure_stage,
                error=exc,
            )

    def _run_compliance_retry(
        self,
        task_id: str,
        task: BidCheckTask,
        *,
        recorder: ComplianceExtractionRecorder | None,
        workflow_stats: dict[str, Any],
        record_event: Callable[..., None],
        finalize_workflow: Callable[..., None],
        started_at: float,
        retry_from: StageName,
    ) -> None:
        if retry_from not in {
            "requirements",
            "bid_parse",
            "review",
            "evaluation_rules",
            "objective_scoring",
            "subjective_scoring",
            "veto_rule_execution",
        }:
            raise ValueError(f"unsupported retry stage: {retry_from}")
        if retry_from not in {"requirements", "bid_parse", "review"}:
            requirements = self._load_saved_stage_result(
                task, "requirements", "07_result.json"
            )
            bid_parse = self._load_saved_stage_result(task, "bid_parse", None)
            review_result = self._load_saved_stage_result(task, "review_result", None)
            initial_result = {
                **(task.result if isinstance(task.result, dict) else {}),
                "requirements": requirements,
                "bid_parse": bid_parse,
                "review_result": review_result,
            }
            self._run_evaluation_phase(
                task_id,
                task,
                recorder=recorder,
                workflow_stats=workflow_stats,
                record_event=record_event,
                finalize_workflow=finalize_workflow,
                started_at=started_at,
                mode="full",
                initial_result=initial_result,
                retry_from=retry_from,
            )
            return

        run_requirements = retry_from == "requirements" or (
            retry_from == "bid_parse" and task.requirements_status == "pending"
        )
        run_bid_parse = retry_from == "bid_parse" or (
            retry_from == "requirements" and task.bid_parse_status == "pending"
        )
        if retry_from == "review":
            run_requirements = False
            run_bid_parse = False

        outputs: dict[str, dict[str, Any]] = {}
        if not run_requirements:
            outputs["requirements"] = self._load_saved_stage_result(
                task, "requirements", "07_result.json"
            )
        if not run_bid_parse:
            outputs["bid_parse"] = self._load_saved_stage_result(
                task, "bid_parse", None
            )

        futures = {}
        stage_started_at: dict[str, float] = {}
        if run_requirements:
            stage_started_at["requirements"] = time.perf_counter()
            self.repository.update_stage(task_id, "requirements", "running")
            record_event(
                "workflow.stage.start",
                stage="requirements",
                task_id=task_id,
                retry=True,
            )
            requirement_future = (
                self.executor.submit(
                    self.services.extract_with_recorder,
                    task.tender_file,
                    recorder,
                )
                if self.services.extract_with_recorder is not None
                else self.executor.submit(self.services.extract, task.tender_file)
            )
            futures[requirement_future] = "requirements"
        if run_bid_parse:
            stage_started_at["bid_parse"] = time.perf_counter()
            self.repository.update_stage(task_id, "bid_parse", "running")
            record_event(
                "workflow.stage.start",
                stage="bid_parse",
                task_id=task_id,
                retry=True,
            )
            futures[self.executor.submit(self.services.parse, task.bid_file)] = "bid_parse"

        failure: Exception | None = None
        failed_stage: str | None = None
        for future in as_completed(futures):
            stage = futures[future]
            try:
                result = future.result()
                outputs[stage] = result
                self.repository.update_stage(task_id, stage, "complete")
                self.repository.update_result(task_id, {stage: result})
                elapsed_ms = _elapsed_ms(stage_started_at[stage])
                workflow_stats[f"{stage}_elapsed_ms"] = elapsed_ms
                record_event(
                    "workflow.stage.end",
                    stage=stage,
                    task_id=task_id,
                    status="complete",
                    retry=True,
                    elapsed_ms=elapsed_ms,
                )
            except Exception as exc:
                failure = failure or exc
                failed_stage = failed_stage or stage
                self.repository.fail(task_id, stage, str(exc))
                elapsed_ms = _elapsed_ms(stage_started_at[stage])
                workflow_stats[f"{stage}_elapsed_ms"] = elapsed_ms
                record_event(
                    "workflow.stage.error",
                    stage=stage,
                    task_id=task_id,
                    status="failed",
                    retry=True,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=elapsed_ms,
                )

        if failure is not None:
            finalize_workflow(
                status="failed",
                failed_stage=failed_stage,
                error=failure,
            )
            return

        review_started_at = time.perf_counter()
        self.repository.update_stage(task_id, "review", "running")
        record_event("workflow.review.start", task_id=task_id, retry=True)
        try:
            bid_parse_for_review = dict(outputs["bid_parse"])
            bid_parse_for_review["original_file_metadata"] = task.bid_file.to_dict()
            if self.services.review_with_recorder is not None:
                review_result = self.services.review_with_recorder(
                    outputs["requirements"],
                    bid_parse_for_review,
                    recorder=recorder,
                )
            else:
                review_result = self.services.review(
                    outputs["requirements"],
                    bid_parse_for_review,
                )
            self.repository.update_stage(task_id, "review", "complete")
            self.repository.update_result(task_id, {"review_result": review_result})
            workflow_stats["review_elapsed_ms"] = _elapsed_ms(review_started_at)
            record_event(
                "workflow.review.end",
                task_id=task_id,
                status="complete",
                retry=True,
                elapsed_ms=workflow_stats["review_elapsed_ms"],
            )
        except Exception as exc:
            self.repository.fail(task_id, "review", str(exc))
            workflow_stats["review_elapsed_ms"] = _elapsed_ms(review_started_at)
            record_event(
                "workflow.review.error",
                task_id=task_id,
                status="failed",
                retry=True,
                error_type=type(exc).__name__,
                error_message=str(exc),
                elapsed_ms=workflow_stats["review_elapsed_ms"],
            )
            finalize_workflow(status="failed", failed_stage="review", error=exc)
            return

        compliance_result = {
            **outputs["requirements"],
            "bid_parse": outputs["bid_parse"],
            "review_result": review_result,
        }
        if task.check_mode == "full":
            self._run_evaluation_phase(
                task_id,
                task,
                recorder=recorder,
                workflow_stats=workflow_stats,
                record_event=record_event,
                finalize_workflow=finalize_workflow,
                started_at=started_at,
                mode="full",
                initial_result=compliance_result,
            )
            return
        self.repository.complete(task_id, compliance_result)
        finalize_workflow(status="complete")

    def _run_evaluation_phase(
        self,
        task_id: str,
        task: BidCheckTask,
        *,
        recorder: ComplianceExtractionRecorder | None,
        workflow_stats: dict[str, Any],
        record_event: Callable[..., None],
        finalize_workflow: Callable[..., None],
        started_at: float,
        mode: str,
        initial_result: dict[str, Any] | None = None,
        retry_from: StageName | None = None,
    ) -> None:
        """Run the existing evaluation flow, optionally after compliance."""

        evaluation_started_at = time.perf_counter()
        standalone = mode == "evaluation"
        extraction_stage = "evaluation_rules"
        failure_stage: StageName = "evaluation_rules"
        elapsed_stat = "evaluation_rules_elapsed_ms"

        def set_evaluation_elapsed() -> int:
            elapsed_ms = _elapsed_ms(evaluation_started_at)
            workflow_stats["evaluation_elapsed_ms"] = elapsed_ms
            return elapsed_ms

        record_event("workflow.evaluation.start", task_id=task_id, mode=mode)
        run_extraction = retry_from in {
            None,
            "requirements",
            "bid_parse",
            "review",
            "evaluation_rules",
        }
        evaluation_result: dict[str, Any]
        if run_extraction:
            record_event(
                "workflow.stage.start",
                stage=extraction_stage,
                task_id=task_id,
                file=task.tender_file.filename,
                mode=mode,
            )
            logger.info(
                "workflow.stage.start stage=%s mode=%s task_id=%s file=%s",
                extraction_stage,
                mode,
                task_id,
                task.tender_file.filename,
            )
            self.repository.update_stage(task_id, "evaluation_rules", "running")
            try:
                if self.services.extract_evaluation_with_recorder is None:
                    raise RuntimeError("evaluation extractor not configured")
                evaluation_result = self.services.extract_evaluation_with_recorder(
                    task.tender_file,
                    recorder=recorder,
                )
                extraction_elapsed_ms = _elapsed_ms(evaluation_started_at)
                workflow_stats[elapsed_stat] = extraction_elapsed_ms
                if standalone:
                    workflow_stats["requirements_elapsed_ms"] = extraction_elapsed_ms
                    self.repository.update_stage(task_id, "bid_parse", "complete")
                    workflow_stats["bid_parse_elapsed_ms"] = 0
                    record_event(
                        "workflow.stage.skip",
                        stage="bid_parse",
                        task_id=task_id,
                        mode=mode,
                        reason="evaluation_mode_does_not_use_bid_file",
                    )
                self.repository.update_stage(task_id, "evaluation_rules", "complete")
                self.repository.update_result(
                    task_id,
                    {"evaluation_rules": evaluation_result},
                )
                record_event(
                    "workflow.stage.end",
                    stage=extraction_stage,
                    task_id=task_id,
                    mode=mode,
                    status="complete",
                    result=_result_summary(evaluation_result),
                    elapsed_ms=extraction_elapsed_ms,
                )
            except Exception as exc:
                extraction_elapsed_ms = _elapsed_ms(evaluation_started_at)
                workflow_stats[elapsed_stat] = extraction_elapsed_ms
                set_evaluation_elapsed()
                record_event(
                    "workflow.stage.error",
                    stage=extraction_stage,
                    task_id=task_id,
                    mode=mode,
                    status="failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=extraction_elapsed_ms,
                )
                self.repository.fail(task_id, failure_stage, str(exc))
                finalize_workflow(
                    status="failed",
                    failed_stage=failure_stage,
                    error=exc,
                )
                logger.error(
                    "workflow.stage.error stage=%s mode=%s task_id=%s error_type=%s elapsed_ms=%d",
                    extraction_stage,
                    mode,
                    task_id,
                    type(exc).__name__,
                    extraction_elapsed_ms,
                )
                return
        else:
            try:
                saved_result = initial_result or {}
                stored_rules = saved_result.get("evaluation_rules")
                if isinstance(stored_rules, dict):
                    evaluation_result = dict(stored_rules)
                else:
                    evaluation_result = self._load_saved_stage_result(
                        task,
                        "evaluation_rules",
                        "11_evaluation_rules.json",
                    )
                record_event(
                    "workflow.stage.skip",
                    stage=extraction_stage,
                    task_id=task_id,
                    mode=mode,
                    reason="reused_successful_stage",
                )
            except Exception as exc:
                self.repository.fail(task_id, "evaluation_rules", str(exc))
                finalize_workflow(
                    status="failed",
                    failed_stage="evaluation_rules",
                    error=exc,
                )
                return

        objective_scores: dict[str, Any] | None = None
        run_objective = retry_from in {
            None,
            "requirements",
            "bid_parse",
            "review",
            "evaluation_rules",
            "objective_scoring",
        }
        if run_objective and self.services.score_objective_with_recorder is not None:
            scoring_started_at = time.perf_counter()
            record_event(
                "workflow.stage.start",
                stage="objective_scoring",
                task_id=task_id,
                mode=mode,
                file=task.bid_file.filename,
            )
            self.repository.update_stage(task_id, "objective_scoring", "running")
            try:
                objective_scores = self.services.score_objective_with_recorder(
                    task.tender_file,
                    task.bid_file,
                    evaluation_result,
                    recorder=recorder,
                )
                scoring_elapsed_ms = _elapsed_ms(scoring_started_at)
                workflow_stats["objective_scoring_elapsed_ms"] = scoring_elapsed_ms
                self.repository.update_stage(task_id, "objective_scoring", "complete")
                record_event(
                    "workflow.stage.end",
                    stage="objective_scoring",
                    task_id=task_id,
                    mode=mode,
                    status="complete",
                    result=_result_summary(objective_scores),
                    elapsed_ms=scoring_elapsed_ms,
                )
                self.repository.update_result(
                    task_id,
                    {"objective_scores": objective_scores},
                )
            except Exception as scoring_error:
                scoring_elapsed_ms = _elapsed_ms(scoring_started_at)
                workflow_stats["objective_scoring_elapsed_ms"] = scoring_elapsed_ms
                set_evaluation_elapsed()
                record_event(
                    "workflow.stage.error",
                    stage="objective_scoring",
                    task_id=task_id,
                    mode=mode,
                    status="failed",
                    error_type=type(scoring_error).__name__,
                    error_message=str(scoring_error),
                    elapsed_ms=scoring_elapsed_ms,
                )
                self.repository.fail(task_id, "objective_scoring", str(scoring_error))
                finalize_workflow(
                    status="failed",
                    failed_stage="objective_scoring",
                    error=scoring_error,
                )
                logger.error(
                    "workflow.stage.error stage=objective_scoring mode=%s task_id=%s error_type=%s elapsed_ms=%d",
                    mode,
                    task_id,
                    type(scoring_error).__name__,
                    scoring_elapsed_ms,
                )
                return
        elif not run_objective and not standalone:
            try:
                saved_result = initial_result or {}
                stored_scores = saved_result.get("objective_scores")
                if isinstance(stored_scores, dict):
                    objective_scores = dict(stored_scores)
                else:
                    objective_scores = json.loads(
                        (
                            Path(task.tender_file.storage_path).parent
                            / "compliance_extraction"
                            / "objective_scores.json"
                        ).read_text(encoding="utf-8")
                    )
                    if not isinstance(objective_scores, dict):
                        raise TypeError(
                            "objective_scores.json must contain an object"
                        )
                record_event(
                    "workflow.stage.skip",
                    stage="objective_scoring",
                    task_id=task_id,
                    mode=mode,
                    reason="reused_successful_stage",
                )
            except Exception as exc:
                self.repository.fail(task_id, "objective_scoring", str(exc))
                finalize_workflow(
                    status="failed",
                    failed_stage="objective_scoring",
                    error=exc,
                )
                return

        subjective_scores: dict[str, Any] | None = None
        run_subjective = not standalone and retry_from in {
            None,
            "requirements",
            "bid_parse",
            "review",
            "evaluation_rules",
            "objective_scoring",
            "subjective_scoring",
        }
        if run_subjective:
            subjective_started_at = time.perf_counter()
            record_event(
                "workflow.stage.start",
                stage="subjective_scoring",
                task_id=task_id,
                mode=mode,
                file=task.bid_file.filename,
            )
            self.repository.update_stage(task_id, "subjective_scoring", "running")
            try:
                if self.services.score_subjective_with_recorder is None:
                    raise RuntimeError("subjective scoring service not configured")
                subjective_scores = self.services.score_subjective_with_recorder(
                    task.tender_file,
                    task.bid_file,
                    evaluation_result,
                    recorder=recorder,
                )
                subjective_elapsed_ms = _elapsed_ms(subjective_started_at)
                workflow_stats["subjective_scoring_elapsed_ms"] = (
                    subjective_elapsed_ms
                )
                self.repository.update_stage(task_id, "subjective_scoring", "complete")
                record_event(
                    "workflow.stage.end",
                    stage="subjective_scoring",
                    task_id=task_id,
                    mode=mode,
                    status="complete",
                    result=_result_summary(subjective_scores),
                    elapsed_ms=subjective_elapsed_ms,
                )
                self.repository.update_result(
                    task_id,
                    {"subjective_scores": subjective_scores},
                )
            except Exception as subjective_error:
                subjective_elapsed_ms = _elapsed_ms(subjective_started_at)
                workflow_stats["subjective_scoring_elapsed_ms"] = (
                    subjective_elapsed_ms
                )
                set_evaluation_elapsed()
                record_event(
                    "workflow.stage.error",
                    stage="subjective_scoring",
                    task_id=task_id,
                    mode=mode,
                    status="failed",
                    error_type=type(subjective_error).__name__,
                    error_message=str(subjective_error),
                    elapsed_ms=subjective_elapsed_ms,
                )
                self.repository.fail(task_id, "subjective_scoring", str(subjective_error))
                finalize_workflow(
                    status="failed",
                    failed_stage="subjective_scoring",
                    error=subjective_error,
                )
                logger.error(
                    "workflow.stage.error stage=subjective_scoring mode=%s task_id=%s error_type=%s elapsed_ms=%d",
                    mode,
                    task_id,
                    type(subjective_error).__name__,
                    subjective_elapsed_ms,
                )
                return
        elif not standalone:
            try:
                saved_result = initial_result or {}
                stored_scores = saved_result.get("subjective_scores")
                if isinstance(stored_scores, dict):
                    subjective_scores = dict(stored_scores)
                else:
                    subjective_scores = json.loads(
                        (
                            Path(task.tender_file.storage_path).parent
                            / "compliance_extraction"
                            / "subjective_scores.json"
                        ).read_text(encoding="utf-8")
                    )
                    if not isinstance(subjective_scores, dict):
                        raise TypeError(
                            "subjective_scores.json must contain an object"
                        )
                record_event(
                    "workflow.stage.skip",
                    stage="subjective_scoring",
                    task_id=task_id,
                    mode=mode,
                    reason="reused_successful_stage",
                )
            except Exception as exc:
                self.repository.fail(task_id, "subjective_scoring", str(exc))
                finalize_workflow(
                    status="failed",
                    failed_stage="subjective_scoring",
                    error=exc,
                )
                return

        veto_rule_reviews: dict[str, Any] | None = None
        run_veto = retry_from in {
            None,
            "requirements",
            "bid_parse",
            "review",
            "evaluation_rules",
            "objective_scoring",
            "subjective_scoring",
            "veto_rule_execution",
        }
        if run_veto and self.services.execute_veto_with_recorder is not None:
            veto_started_at = time.perf_counter()
            record_event(
                "workflow.stage.start",
                stage="veto_rule_execution",
                task_id=task_id,
                mode=mode,
                file=task.bid_file.filename,
            )
            self.repository.update_stage(task_id, "veto_rule_execution", "running")
            try:
                veto_rule_reviews = self.services.execute_veto_with_recorder(
                    task.tender_file,
                    task.bid_file,
                    evaluation_result,
                    objective_scores=objective_scores,
                    recorder=recorder,
                )
                veto_elapsed_ms = _elapsed_ms(veto_started_at)
                workflow_stats["veto_rule_execution_elapsed_ms"] = veto_elapsed_ms
                self.repository.update_stage(task_id, "veto_rule_execution", "complete")
                record_event(
                    "workflow.stage.end",
                    stage="veto_rule_execution",
                    task_id=task_id,
                    mode=mode,
                    status="complete",
                    result=_result_summary(veto_rule_reviews),
                    elapsed_ms=veto_elapsed_ms,
                )
                self.repository.update_result(
                    task_id,
                    {"veto_rule_reviews": veto_rule_reviews},
                )
            except Exception as veto_error:
                veto_elapsed_ms = _elapsed_ms(veto_started_at)
                workflow_stats["veto_rule_execution_elapsed_ms"] = veto_elapsed_ms
                set_evaluation_elapsed()
                record_event(
                    "workflow.stage.error",
                    stage="veto_rule_execution",
                    task_id=task_id,
                    mode=mode,
                    status="failed",
                    error_type=type(veto_error).__name__,
                    error_message=str(veto_error),
                    elapsed_ms=veto_elapsed_ms,
                )
                self.repository.fail(task_id, "veto_rule_execution", str(veto_error))
                finalize_workflow(
                    status="failed",
                    failed_stage="veto_rule_execution",
                    error=veto_error,
                )
                logger.error(
                    "workflow.stage.error stage=veto_rule_execution mode=%s task_id=%s error_type=%s elapsed_ms=%d",
                    mode,
                    task_id,
                    type(veto_error).__name__,
                    veto_elapsed_ms,
                )
                return

        final_result = dict(initial_result or {})
        final_result["evaluation_rules"] = evaluation_result
        if objective_scores is not None:
            final_result["objective_scores"] = objective_scores
        if subjective_scores is not None:
            final_result["subjective_scores"] = subjective_scores
        if veto_rule_reviews is not None:
            final_result["veto_rule_reviews"] = veto_rule_reviews
        self.repository.complete(task_id, final_result)
        if standalone:
            workflow_stats["review_elapsed_ms"] = 0
        set_evaluation_elapsed()
        if standalone:
            record_event(
                "workflow.review.skip",
                task_id=task_id,
                mode=mode,
                reason=(
                    "evaluation_mode_does_not_run_subjective_scoring"
                    if veto_rule_reviews is not None
                    else "evaluation_mode_does_not_run_subjective_or_veto_scoring"
                ),
            )
        record_event("workflow.evaluation.end", task_id=task_id, mode=mode)
        finalize_workflow(status="complete")
        logger.info(
            "workflow.run.end status=complete mode=%s task_id=%s elapsed_ms=%d",
            mode,
            task_id,
            _elapsed_ms(started_at),
        )

    def run_subjective(self, task_id: str) -> None:
        started_at = time.perf_counter()
        task = self.repository.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.check_mode != "evaluation":
            raise ValueError("主观评分仅支持评标任务。")
        if self.services.score_subjective_with_recorder is None:
            raise RuntimeError("subjective scoring service not configured")

        recorder: ComplianceExtractionRecorder | None = None
        try:
            recorder = ComplianceExtractionRecorder.from_tender_path(
                Path(task.tender_file.storage_path)
            )
        except OSError as recorder_error:
            logger.warning(
                "subjective.score.recorder.init.error task_id=%s error_type=%s",
                task_id,
                type(recorder_error).__name__,
            )

        evaluation_rules: dict[str, Any] | None = None
        stored_result = task.result if isinstance(task.result, dict) else {}
        stored_rules = stored_result.get("evaluation_rules")
        if isinstance(stored_rules, dict):
            evaluation_rules = dict(stored_rules)
        else:
            artifact_path = (
                Path(task.tender_file.storage_path).parent
                / "compliance_extraction"
                / "11_evaluation_rules.json"
            )
            try:
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as artifact_error:
                raise RuntimeError("11_evaluation_rules.json not available") from artifact_error
            if not isinstance(artifact, dict):
                raise TypeError("11_evaluation_rules.json must contain an object")
            evaluation_rules = artifact

        if recorder is not None:
            recorder.event("subjective.score.execution.start", task_id=task_id)
        logger.info("subjective.score.execution.start task_id=%s", task_id)
        try:
            result = self.services.score_subjective_with_recorder(
                task.tender_file,
                task.bid_file,
                evaluation_rules,
                recorder=recorder,
            )
            self.repository.update_result(task_id, {"subjective_scores": result})
            elapsed_ms = _elapsed_ms(started_at)
            if recorder is not None:
                recorder.event(
                    "subjective.score.execution.persisted",
                    task_id=task_id,
                    elapsed_ms=elapsed_ms,
                )
            logger.info(
                "subjective.score.execution.end task_id=%s elapsed_ms=%d",
                task_id,
                elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = _elapsed_ms(started_at)
            if recorder is not None:
                recorder.event(
                    "subjective.score.execution.error",
                    task_id=task_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    elapsed_ms=elapsed_ms,
                )
            logger.error(
                "subjective.score.execution.error task_id=%s error_type=%s elapsed_ms=%d",
                task_id,
                type(exc).__name__,
                elapsed_ms,
            )
            raise

    def shutdown(self, wait: bool = True) -> None:
        if self._owns_executor:
            self.executor.shutdown(wait=wait)
