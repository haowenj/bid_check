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
    RetryFrom,
    StageName,
    TaskStatus,
)

TASK_STATUSES = frozenset({"pending", "running", "complete", "failed"})
CHECK_MODES = frozenset({"compliance", "evaluation", "full"})
STAGE_COLUMNS: dict[StageName, str] = {
    "requirements": "requirements_status",
    "bid_parse": "bid_parse_status",
    "review": "review_status",
    "evaluation_rules": "evaluation_rules_status",
    "objective_scoring": "objective_scoring_status",
    "subjective_scoring": "subjective_scoring_status",
    "veto_rule_execution": "veto_rule_execution_status",
}
FULL_STAGE_ORDER: tuple[StageName, ...] = (
    "requirements",
    "bid_parse",
    "review",
    "evaluation_rules",
    "objective_scoring",
    "subjective_scoring",
    "veto_rule_execution",
)
MODE_STAGE_ORDER: dict[CheckMode, tuple[StageName, ...]] = {
    "compliance": ("requirements", "bid_parse", "review"),
    "evaluation": (
        "evaluation_rules",
        "objective_scoring",
        "veto_rule_execution",
    ),
    "full": FULL_STAGE_ORDER,
}
RESULT_KEY_BY_STAGE: dict[StageName, str] = {
    "requirements": "requirements",
    "bid_parse": "bid_parse",
    "review": "review_result",
    "evaluation_rules": "evaluation_rules",
    "objective_scoring": "objective_scores",
    "subjective_scoring": "subjective_scores",
    "veto_rule_execution": "veto_rule_reviews",
}
EVALUATION_STATUS_COLUMNS = (
    "evaluation_rules_status",
    "objective_scoring_status",
    "subjective_scoring_status",
    "veto_rule_execution_status",
)


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
                    evaluation_rules_status TEXT NOT NULL CHECK (
                        evaluation_rules_status IN (
                            'pending', 'running', 'complete', 'failed'
                        )
                    ),
                    objective_scoring_status TEXT NOT NULL CHECK (
                        objective_scoring_status IN (
                            'pending', 'running', 'complete', 'failed'
                        )
                    ),
                    subjective_scoring_status TEXT NOT NULL CHECK (
                        subjective_scoring_status IN (
                            'pending', 'running', 'complete', 'failed'
                        )
                    ),
                    veto_rule_execution_status TEXT NOT NULL CHECK (
                        veto_rule_execution_status IN (
                            'pending', 'running', 'complete', 'failed'
                        )
                    ),
                    failed_stage TEXT CHECK (
                        failed_stage IS NULL OR failed_stage IN (
                            'requirements', 'bid_parse', 'review',
                            'evaluation_rules', 'objective_scoring',
                            'subjective_scoring', 'veto_rule_execution'
                        )
                    ),
                    error_message TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_schema(connection)

    @staticmethod
    def _extended_schema_sql() -> str:
        return """
            CREATE TABLE bid_check_tasks_migrated (
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
                evaluation_rules_status TEXT NOT NULL CHECK (
                    evaluation_rules_status IN (
                        'pending', 'running', 'complete', 'failed'
                    )
                ),
                objective_scoring_status TEXT NOT NULL CHECK (
                    objective_scoring_status IN (
                        'pending', 'running', 'complete', 'failed'
                    )
                ),
                subjective_scoring_status TEXT NOT NULL CHECK (
                    subjective_scoring_status IN (
                        'pending', 'running', 'complete', 'failed'
                    )
                ),
                veto_rule_execution_status TEXT NOT NULL CHECK (
                    veto_rule_execution_status IN (
                        'pending', 'running', 'complete', 'failed'
                    )
                ),
                failed_stage TEXT CHECK (
                    failed_stage IS NULL OR failed_stage IN (
                        'requirements', 'bid_parse', 'review',
                        'evaluation_rules', 'objective_scoring',
                        'subjective_scoring', 'veto_rule_execution'
                    )
                ),
                error_message TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(bid_check_tasks)"
            ).fetchall()
        }
        for column in EVALUATION_STATUS_COLUMNS:
            if column not in columns:
                connection.execute(
                    f"""
                    ALTER TABLE bid_check_tasks
                    ADD COLUMN {column} TEXT NOT NULL DEFAULT 'pending'
                    CHECK ({column} IN ('pending', 'running', 'complete', 'failed'))
                    """
                )

        table_sql_row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'bid_check_tasks'
            """
        ).fetchone()
        table_sql = str(table_sql_row[0] or "") if table_sql_row else ""
        if "'veto_rule_execution'" not in table_sql:
            connection.execute(self._extended_schema_sql())
            connection.execute(
                """
                INSERT INTO bid_check_tasks_migrated (
                    sequence, task_id, tender_file_json, bid_file_json,
                    check_mode, status, requirements_status,
                    bid_parse_status, review_status, evaluation_rules_status,
                    objective_scoring_status, subjective_scoring_status,
                    veto_rule_execution_status, failed_stage, error_message,
                    result_json, created_at, updated_at
                )
                SELECT sequence, task_id, tender_file_json, bid_file_json,
                       check_mode, status, requirements_status,
                       bid_parse_status, review_status,
                       CASE WHEN check_mode = 'evaluation'
                            THEN requirements_status
                            ELSE evaluation_rules_status END,
                       objective_scoring_status, subjective_scoring_status,
                       veto_rule_execution_status, failed_stage, error_message,
                       result_json, created_at, updated_at
                FROM bid_check_tasks
                """
            )
            connection.execute("DROP TABLE bid_check_tasks")
            connection.execute(
                "ALTER TABLE bid_check_tasks_migrated RENAME TO bid_check_tasks"
            )
        else:
            connection.execute(
                """
                UPDATE bid_check_tasks
                SET evaluation_rules_status = requirements_status
                WHERE check_mode = 'evaluation'
                  AND evaluation_rules_status = 'pending'
                  AND requirements_status != 'pending'
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
            evaluation_rules_status=cast(
                TaskStatus,
                row["evaluation_rules_status"]
                if "evaluation_rules_status" in row.keys()
                else "pending",
            ),
            objective_scoring_status=cast(
                TaskStatus,
                row["objective_scoring_status"]
                if "objective_scoring_status" in row.keys()
                else "pending",
            ),
            subjective_scoring_status=cast(
                TaskStatus,
                row["subjective_scoring_status"]
                if "subjective_scoring_status" in row.keys()
                else "pending",
            ),
            veto_rule_execution_status=cast(
                TaskStatus,
                row["veto_rule_execution_status"]
                if "veto_rule_execution_status" in row.keys()
                else "pending",
            ),
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
                    review_status, evaluation_rules_status,
                    objective_scoring_status, subjective_scoring_status,
                    veto_rule_execution_status, failed_stage, error_message,
                    result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 'pending', 'pending',
                          'pending', 'pending', 'pending', 'pending',
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
        assignments = [f"{column} = ?", "status = COALESCE(?, status)"]
        parameters: list[Any] = [status, total_status]
        if stage == "evaluation_rules":
            assignments.append("requirements_status = ?")
            parameters.append(status)
        assignments.extend(["updated_at = ?"])
        parameters.extend([self._now(), task_id])
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE bid_check_tasks
                SET {', '.join(assignments)}
                WHERE task_id = ?
                """,
                parameters,
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

    @staticmethod
    def _retry_reset_stages(
        task: BidCheckTask,
        start_stage: StageName,
        retry_from: RetryFrom,
    ) -> set[StageName]:
        stage_order = MODE_STAGE_ORDER[task.check_mode]
        if retry_from == "start":
            return set(stage_order)

        if start_stage not in stage_order:
            raise ValueError("failed stage is not applicable to this task")
        stage_index = stage_order.index(start_stage)
        if start_stage in {"requirements", "bid_parse"}:
            reset_stages = {
                stage
                for stage in ("requirements", "bid_parse")
                if stage in stage_order
                and (
                    stage == start_stage
                    or getattr(task, STAGE_COLUMNS[stage]) == "failed"
                )
            }
            reset_stages.update(stage_order[2:])
            return reset_stages
        return set(stage_order[stage_index:])

    def prepare_retry(
        self,
        task_id: str,
        retry_from: RetryFrom,
    ) -> BidCheckTask:
        if retry_from not in {"start", "failed_stage"}:
            raise ValueError("unsupported retry mode")

        with self._write_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bid_check_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            task = self._record_from_row(row)
            if task is None:
                raise KeyError(task_id)
            if task.status != "failed":
                raise ValueError("only failed tasks can be retried")

            start_stage = task.failed_stage
            if retry_from == "start":
                start_stage = MODE_STAGE_ORDER[task.check_mode][0]
            elif task.check_mode == "evaluation" and start_stage == "requirements":
                start_stage = "evaluation_rules"
            if start_stage is None:
                raise ValueError("failed stage is unavailable")

            reset_stages = self._retry_reset_stages(
                task,
                start_stage,
                retry_from,
            )
            existing_result = task.result if isinstance(task.result, dict) else {}
            retry_result = {
                key: value
                for key, value in existing_result.items()
                if not any(
                    key == RESULT_KEY_BY_STAGE[stage]
                    for stage in reset_stages
                )
            }

            assignments = ["status = 'pending'"]
            parameters: list[Any] = []
            for stage in FULL_STAGE_ORDER:
                if stage in reset_stages:
                    column = STAGE_COLUMNS[stage]
                    assignments.append(f"{column} = ?")
                    parameters.append("pending")
            if "evaluation_rules" in reset_stages:
                assignments.append("requirements_status = ?")
                parameters.append("pending")
            assignments.extend(
                [
                    "failed_stage = NULL",
                    "error_message = NULL",
                    "result_json = ?",
                    "updated_at = ?",
                ]
            )
            parameters.extend(
                [
                    json.dumps(retry_result, ensure_ascii=False),
                    self._now(),
                    task_id,
                ]
            )
            cursor = connection.execute(
                f"""
                UPDATE bid_check_tasks
                SET {', '.join(assignments)}
                WHERE task_id = ? AND status = 'failed'
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                raise ValueError("only failed tasks can be retried")
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
