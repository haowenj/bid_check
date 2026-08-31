from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from app.models import FileMetadata, StageName
from app.repository import BidCheckRepository


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
        task = self.repository.get(task_id)
        if task is None:
            raise KeyError(task_id)

        self.repository.update_stage(task_id, "requirements", "running")
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
            except Exception as exc:
                failed = True
                self.repository.fail(task_id, stage, str(exc))

        if failed:
            return

        try:
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
        except Exception as exc:
            self.repository.fail(task_id, "review", str(exc))

    def shutdown(self, wait: bool = True) -> None:
        if self._owns_executor:
            self.executor.shutdown(wait=wait)
