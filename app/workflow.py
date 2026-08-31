from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import logging
import time
from typing import Any

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
    extract: Callable[[FileMetadata], list[dict[str, Any]]]
    parse: Callable[[FileMetadata], dict[str, Any]]
    review: Callable[
        [list[dict[str, Any]], dict[str, Any]],
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

        stage_started_at = {
            "requirements": time.perf_counter(),
            "bid_parse": time.perf_counter(),
        }
        logger.info(
            "workflow.stage.start stage=requirements task_id=%s file=%s",
            task_id,
            task.tender_file.filename,
        )
        self.repository.update_stage(task_id, "requirements", "running")
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

        for future in as_completed(futures):
            stage = futures[future]
            try:
                outputs[stage] = future.result()
                self.repository.update_stage(task_id, stage, "complete")
                logger.info(
                    "workflow.stage.end stage=%s task_id=%s status=complete %s elapsed_ms=%d",
                    stage,
                    task_id,
                    _result_summary(outputs[stage]),
                    _elapsed_ms(stage_started_at[stage]),
                )
            except Exception as exc:
                failed = True
                self.repository.fail(task_id, stage, str(exc))
                logger.error(
                    "workflow.stage.error stage=%s task_id=%s status=failed error_type=%s elapsed_ms=%d",
                    stage,
                    task_id,
                    type(exc).__name__,
                    _elapsed_ms(stage_started_at[stage]),
                )

        if failed:
            logger.info(
                "workflow.run.end status=failed task_id=%s failed_stage=parallel elapsed_ms=%d",
                task_id,
                _elapsed_ms(started_at),
            )
            return

        review_started_at = time.perf_counter()
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
                    "requirements": outputs["requirements"],
                    "bid_parse": outputs["bid_parse"],
                    "review_result": review_result,
                },
            )
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
            self.repository.fail(task_id, "review", str(exc))
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
