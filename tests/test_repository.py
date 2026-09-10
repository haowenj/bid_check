import json
import sqlite3

import pytest

from app.models import FileMetadata
from app.repository import BidCheckRepository


def make_repository(tmp_path):
    return BidCheckRepository(tmp_path / "bid_check.db")


def make_files():
    return (
        FileMetadata(
            filename="招标文件.docx",
            size=12,
            storage_path="tasks/tender.docx",
        ),
        FileMetadata(
            filename="投标文件.docx",
            size=34,
            storage_path="tasks/bid.docx",
        ),
    )


def test_create_persists_required_task_fields(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()

    task = repository.create(
        task_id="task-001",
        tender_file=tender_file,
        bid_file=bid_file,
        check_mode="compliance",
    )

    assert task.task_id == "task-001"
    assert task.status == "pending"
    assert task.requirements_status == "pending"
    assert task.bid_parse_status == "pending"
    assert task.review_status == "pending"
    assert task.tender_file.filename == "招标文件.docx"
    assert repository.get("task-001") == task


def test_list_tasks_returns_newest_first(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")
    repository.create("task-002", tender_file, bid_file, "compliance")

    tasks = repository.list_tasks()

    assert [task.task_id for task in tasks] == ["task-002", "task-001"]


def test_delete_task_removes_task_record_and_returns_deleted_task(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    created = repository.create("task-001", tender_file, bid_file, "compliance")

    deleted = repository.delete_task("task-001")

    assert deleted == created
    assert repository.get("task-001") is None


def test_update_stage_preserves_other_stage_states(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")

    updated = repository.update_stage("task-001", "requirements", "running")

    assert updated.status == "running"
    assert updated.requirements_status == "running"
    assert updated.bid_parse_status == "pending"
    assert updated.review_status == "pending"


def test_complete_persists_structured_result(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")
    result = {
        "requirements": [{"id": "compliance_001"}],
        "bid_parse": {"status": "success"},
        "review_result": {
            "mode": "mock",
            "message": "当前版本尚未执行真实合规性检查",
        },
    }

    completed = repository.complete("task-001", result)

    assert completed.status == "complete"
    assert completed.review_status == "complete"
    assert completed.result == result
    assert repository.get("task-001").result == result


def test_fail_records_failed_stage_and_message(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")

    failed = repository.fail(
        "task-001",
        "bid_parse",
        "模拟投标文件解析失败",
    )

    assert failed.status == "failed"
    assert failed.bid_parse_status == "failed"
    assert failed.failed_stage == "bid_parse"
    assert failed.error_message == "模拟投标文件解析失败"


def test_prepare_retry_from_failed_stage_preserves_previous_results(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "full")
    repository.update_stage("task-001", "requirements", "complete")
    repository.update_stage("task-001", "bid_parse", "complete")
    repository.update_stage("task-001", "review", "complete")
    repository.update_stage("task-001", "evaluation_rules", "complete")
    repository.update_result(
        "task-001",
        {
            "requirements": {"id": "requirements-ok"},
            "bid_parse": {"id": "bid-parse-ok"},
            "review_result": {"id": "review-ok"},
            "evaluation_rules": {"id": "rules-old"},
            "objective_scores": {"id": "scores-old"},
        },
    )
    repository.fail("task-001", "objective_scoring", "评分服务失败")

    retried = repository.prepare_retry("task-001", "failed_stage")

    assert retried.task_id == "task-001"
    assert retried.status == "pending"
    assert retried.requirements_status == "complete"
    assert retried.bid_parse_status == "complete"
    assert retried.review_status == "complete"
    assert retried.evaluation_rules_status == "complete"
    assert retried.objective_scoring_status == "pending"
    assert retried.result == {
        "requirements": {"id": "requirements-ok"},
        "bid_parse": {"id": "bid-parse-ok"},
        "review_result": {"id": "review-ok"},
        "evaluation_rules": {"id": "rules-old"},
    }
    assert retried.failed_stage is None
    assert retried.error_message is None


def test_prepare_retry_from_full_review_preserves_requirements_status(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "full")
    repository.update_stage("task-001", "requirements", "complete")
    repository.update_stage("task-001", "bid_parse", "complete")
    repository.update_result(
        "task-001",
        {
            "requirements": {"id": "requirements-ok"},
            "bid_parse": {"id": "bid-parse-ok"},
        },
    )
    repository.fail("task-001", "review", "合规性检查失败")

    retried = repository.prepare_retry("task-001", "failed_stage")

    assert retried.requirements_status == "complete"
    assert retried.bid_parse_status == "complete"
    assert retried.review_status == "pending"
    assert retried.evaluation_rules_status == "pending"


def test_prepare_retry_from_start_resets_all_derived_states_and_results(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "full")
    for stage in (
        "requirements",
        "bid_parse",
        "review",
        "evaluation_rules",
        "objective_scoring",
        "subjective_scoring",
        "veto_rule_execution",
    ):
        repository.update_stage("task-001", stage, "complete")
    repository.update_result("task-001", {"review_result": {"old": True}})
    repository.fail("task-001", "veto_rule_execution", "否决规则失败")

    retried = repository.prepare_retry("task-001", "start")

    assert retried.status == "pending"
    assert retried.result == {}
    assert retried.failed_stage is None
    assert retried.requirements_status == "pending"
    assert retried.bid_parse_status == "pending"
    assert retried.evaluation_rules_status == "pending"


def test_prepare_retry_from_failed_stage_resets_both_failed_parallel_branches(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "full")
    repository.fail("task-001", "requirements", "要求提取失败")
    repository.fail("task-001", "bid_parse", "投标文件解析失败")

    retried = repository.prepare_retry("task-001", "failed_stage")

    assert retried.status == "pending"
    assert retried.requirements_status == "pending"
    assert retried.bid_parse_status == "pending"
    assert retried.review_status == "pending"
    assert retried.failed_stage is None


def test_prepare_retry_rejects_non_failed_task(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")

    with pytest.raises(ValueError, match="failed"):
        repository.prepare_retry("task-001", "start")


def test_repository_migrates_legacy_schema_to_evaluation_stage_columns(tmp_path):
    database_path = tmp_path / "legacy.db"
    tender_file, bid_file = make_files()
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE bid_check_tasks (
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
    connection.execute(
        """
        INSERT INTO bid_check_tasks (
            task_id, tender_file_json, bid_file_json, check_mode, status,
            requirements_status, bid_parse_status, review_status,
            failed_stage, error_message, result_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'evaluation', 'complete', 'complete', 'complete',
                  'complete', NULL, NULL, ?, 'created', 'updated')
        """,
        (
            "legacy-task",
            json.dumps(tender_file.to_dict()),
            json.dumps(bid_file.to_dict()),
            json.dumps({"evaluation_rules": {"old": True}}),
        ),
    )
    connection.commit()
    connection.close()

    repository = BidCheckRepository(database_path)
    migrated = repository.get("legacy-task")

    assert migrated is not None
    assert migrated.evaluation_rules_status == "complete"
    assert migrated.objective_scoring_status == "pending"
    repository.fail("legacy-task", "objective_scoring", "评分失败")
    assert repository.get("legacy-task").failed_stage == "objective_scoring"


def test_update_result_merges_subjective_scores_without_changing_stage_states(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    created = repository.create("task-001", tender_file, bid_file, "evaluation")

    updated = repository.update_result(
        "task-001",
        {"subjective_scores": {"score_items": []}},
    )

    assert updated.result == {"subjective_scores": {"score_items": []}}
    assert updated.status == created.status
    assert updated.requirements_status == created.requirements_status
    assert updated.bid_parse_status == created.bid_parse_status
    assert updated.review_status == created.review_status
