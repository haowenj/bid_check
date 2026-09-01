from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata, StageName
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
        dict[str, str],
    ]


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

    def run(self, task_id: str) -> None:
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
        futures = {
            self.executor.submit(self.services.extract, task.tender_file): (
                "requirements"
            ),
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
            review_result = self.services.review(
                outputs["requirements"],
                outputs["bid_parse"],
            )
            self.repository.complete(
                task_id,
                {
                    **outputs["requirements"],
                    "bid_parse": outputs["bid_parse"],
                    "review_result": review_result,
                },
            )
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

    def shutdown(self, wait: bool = True) -> None:
        if self._owns_executor:
            self.executor.shutdown(wait=wait)
