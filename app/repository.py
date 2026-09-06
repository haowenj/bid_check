from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.models import (
    BidCheckTask,
    CheckMode,
    FileMetadata,
    StageName,
    TaskStatus,
)

TASK_STATUSES = frozenset({"pending", "running", "complete", "failed"})
CHECK_MODES = frozenset({"compliance", "evaluation", "full"})
STAGE_COLUMNS: dict[StageName, str] = {
    "requirements": "requirements_status",
    "bid_parse": "bid_parse_status",
    "review": "review_status",
}


class BidCheckRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bid_check_tasks (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    tender_file_json TEXT NOT NULL,
                    bid_file_json TEXT NOT NULL,
                    check_mode TEXT NOT NULL CHECK (
                        check_mode IN ('compliance', 'evaluation', 'full')
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'running', 'complete', 'failed')
                    ),
                    requirements_status TEXT NOT NULL CHECK (
                        requirements_status IN (
                            'pending', 'running', 'complete', 'failed'
                        )
                    ),
                    bid_parse_status TEXT NOT NULL CHECK (
                        bid_parse_status IN (
                            'pending', 'running', 'complete', 'failed'
                        )
                    ),
                    review_status TEXT NOT NULL CHECK (
                        review_status IN (
                            'pending', 'running', 'complete', 'failed'
                        )
                    ),
                    failed_stage TEXT CHECK (
                        failed_stage IS NULL OR failed_stage IN (
                            'requirements', 'bid_parse', 'review'
                        )
                    ),
                    error_message TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _record_from_row(row: sqlite3.Row | None) -> BidCheckTask | None:
        if row is None:
            return None
        tender = json.loads(row["tender_file_json"])
        bid = json.loads(row["bid_file_json"])
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return BidCheckTask(
            task_id=row["task_id"],
            tender_file=FileMetadata(**tender),
            bid_file=FileMetadata(**bid),
            check_mode=cast(CheckMode, row["check_mode"]),
            status=cast(TaskStatus, row["status"]),
            requirements_status=cast(TaskStatus, row["requirements_status"]),
            bid_parse_status=cast(TaskStatus, row["bid_parse_status"]),
            review_status=cast(TaskStatus, row["review_status"]),
            failed_stage=cast(StageName | None, row["failed_stage"]),
            error_message=row["error_message"],
            result=result,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _get_required(self, task_id: str) -> BidCheckTask:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def create(
        self,
        task_id: str,
        tender_file: FileMetadata,
        bid_file: FileMetadata,
        check_mode: CheckMode,
    ) -> BidCheckTask:
        if check_mode not in CHECK_MODES:
            raise ValueError(f"unsupported check mode: {check_mode}")
        timestamp = self._now()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bid_check_tasks (
                    task_id, tender_file_json, bid_file_json, check_mode,
                    status, requirements_status, bid_parse_status,
                    review_status, failed_stage, error_message, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 'pending', 'pending',
                          'pending', NULL, NULL, NULL, ?, ?)
                """,
                (
                    task_id,
                    json.dumps(tender_file.to_dict(), ensure_ascii=False),
                    json.dumps(bid_file.to_dict(), ensure_ascii=False),
                    check_mode,
                    timestamp,
                    timestamp,
                ),
            )
        return self._get_required(task_id)

    def get(self, task_id: str) -> BidCheckTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bid_check_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._record_from_row(row)

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM bid_check_tasks"
            ).fetchone()
        return int(row["count"])

    def list_tasks(self) -> list[BidCheckTask]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bid_check_tasks ORDER BY sequence DESC"
            ).fetchall()
        return [
            task
            for row in rows
            if (task := self._record_from_row(row)) is not None
        ]

    def delete_task(self, task_id: str) -> BidCheckTask:
        with self._write_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bid_check_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            task = self._record_from_row(row)
            if task is None:
                raise KeyError(task_id)
            connection.execute(
                "DELETE FROM bid_check_tasks WHERE task_id = ?",
                (task_id,),
            )
        return task

    def update_stage(
        self,
        task_id: str,
        stage: StageName,
        status: TaskStatus,
    ) -> BidCheckTask:
        if stage not in STAGE_COLUMNS or status not in TASK_STATUSES:
            raise ValueError("unsupported stage or status")
        column = STAGE_COLUMNS[stage]
        total_status = "running" if status == "running" else None
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE bid_check_tasks
                SET {column} = ?, status = COALESCE(?, status), updated_at = ?
                WHERE task_id = ?
                """,
                (status, total_status, self._now(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self._get_required(task_id)

    def complete(self, task_id: str, result: dict[str, Any]) -> BidCheckTask:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE bid_check_tasks
                SET status = 'complete', review_status = 'complete',
                    result_json = ?, failed_stage = NULL,
                    error_message = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False),
                    self._now(),
                    task_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self._get_required(task_id)

    def update_result(
        self,
        task_id: str,
        result_patch: Mapping[str, Any],
    ) -> BidCheckTask:
        if not isinstance(result_patch, Mapping):
            raise TypeError("result patch must be a mapping")
        with self._write_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM bid_check_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            existing = json.loads(row["result_json"]) if row["result_json"] else {}
            if not isinstance(existing, dict):
                existing = {}
            existing.update(dict(result_patch))
            connection.execute(
                """
                UPDATE bid_check_tasks
                SET result_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(existing, ensure_ascii=False),
                    self._now(),
                    task_id,
                ),
            )
        return self._get_required(task_id)

    def fail(
        self,
        task_id: str,
        stage: StageName,
        message: str,
    ) -> BidCheckTask:
        if stage not in STAGE_COLUMNS:
            raise ValueError(f"unsupported stage: {stage}")
        column = STAGE_COLUMNS[stage]
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE bid_check_tasks
                SET {column} = 'failed', status = 'failed',
                    failed_stage = COALESCE(failed_stage, ?),
                    error_message = COALESCE(error_message, ?),
                    updated_at = ?
                WHERE task_id = ?
                """,
                (stage, message, self._now(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self._get_required(task_id)
