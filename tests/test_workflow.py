from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Barrier

import pytest

from app.workflow import BidCheckServices, BidCheckWorkflow


def test_requirements_and_parse_enter_concurrently(task_repository):
    barrier = Barrier(2, timeout=2)
    entered: list[str] = []

    def extract(file_metadata):
        entered.append("requirements")
        barrier.wait()
        return [{"id": "compliance_001"}]

    def parse(file_metadata):
        entered.append("bid_parse")
        barrier.wait()
        return {"status": "success"}

    workflow = BidCheckWorkflow(
        task_repository,
        BidCheckServices(
            extract=extract,
            parse=parse,
            review=lambda requirements, parsed: {
                "mode": "mock",
                "message": "当前版本尚未执行真实合规性检查",
            },
        ),
    )

    try:
        workflow.run("task-001")
    finally:
        workflow.shutdown()

    assert set(entered) == {"requirements", "bid_parse"}
    task = task_repository.get("task-001")
    assert task.status == "complete"
    assert task.requirements_status == "complete"
    assert task.bid_parse_status == "complete"
    assert task.review_status == "complete"


def make_workflow(repository, extract, parse, review_calls):
    def review(requirements, parsed):
        review_calls.append((requirements, parsed))
        return {
            "mode": "mock",
            "message": "当前版本尚未执行真实合规性检查",
        }

    return BidCheckWorkflow(
        repository,
        BidCheckServices(extract=extract, parse=parse, review=review),
    )


@pytest.mark.parametrize(
    ("failing_service", "failed_stage", "message"),
    [
        ("extract", "requirements", "模拟合规性要求提取失败"),
        ("parse", "bid_parse", "模拟投标文件解析失败"),
    ],
)
def test_parallel_stage_failure_is_persisted(
    task_repository,
    failing_service,
    failed_stage,
    message,
):
    def extract(file_metadata):
        if failing_service == "extract":
            raise RuntimeError(message)
        return [{"id": "compliance_001"}]

    def parse(file_metadata):
        if failing_service == "parse":
            raise RuntimeError(message)
        return {"status": "success"}

    review_calls: list[object] = []
    workflow = make_workflow(
        task_repository,
        extract,
        parse,
        review_calls,
    )
    try:
        workflow.run("task-001")
    finally:
        workflow.shutdown()

    task = task_repository.get("task-001")
    assert task.status == "failed"
    assert task.failed_stage == failed_stage
    assert task.error_message == message
    assert task.review_status == "pending"
    assert review_calls == []
    artifact_dir = Path(task.tender_file.storage_path).parent / "compliance_extraction"
    workflow_summary = json.loads(
        (artifact_dir / "workflow_summary.json").read_text(encoding="utf-8")
    )
    assert workflow_summary["status"] == "failed"
    assert workflow_summary["failed_stage"] == failed_stage
    assert "workflow.stage.error" in (artifact_dir / "execution.jsonl").read_text()


def test_both_parallel_failures_are_recorded_without_overwriting_primary_error(
    task_repository,
):
    barrier = Barrier(2, timeout=2)

    def extract(file_metadata):
        barrier.wait()
        raise RuntimeError("要求失败")

    def parse(file_metadata):
        barrier.wait()
        raise RuntimeError("解析失败")

    workflow = BidCheckWorkflow(
        task_repository,
        BidCheckServices(
            extract=extract,
            parse=parse,
            review=lambda requirements, parsed: {},
        ),
    )
    try:
        workflow.run("task-001")
    finally:
        workflow.shutdown()

    task = task_repository.get("task-001")
    assert task.status == "failed"
    assert task.requirements_status == "failed"
    assert task.bid_parse_status == "failed"
    assert (task.failed_stage, task.error_message) in {
        ("requirements", "要求失败"),
        ("bid_parse", "解析失败"),
    }


def test_review_failure_is_persisted(task_repository):
    services = BidCheckServices(
        extract=lambda file_metadata: [{"id": "compliance_001"}],
        parse=lambda file_metadata: {"status": "success"},
        review=lambda requirements, parsed: (_ for _ in ()).throw(
            RuntimeError("模拟合规性检查失败")
        ),
    )
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run("task-001")
    finally:
        workflow.shutdown()

    task = task_repository.get("task-001")
    assert task.status == "failed"
    assert task.requirements_status == "complete"
    assert task.bid_parse_status == "complete"
    assert task.review_status == "failed"
    assert task.failed_stage == "review"
    assert task.error_message == "模拟合规性检查失败"


def test_workflow_logs_stage_boundaries_and_final_status(task_repository, caplog):
    caplog.set_level(logging.INFO, logger="app.workflow")
    workflow = make_workflow(
        task_repository,
        lambda file_metadata: [{"id": "compliance_001"}],
        lambda file_metadata: {"status": "success"},
        [],
    )
    try:
        workflow.run("task-001")
    finally:
        workflow.shutdown()

    messages = [record.getMessage() for record in caplog.records]
    for event in (
        "workflow.run.start",
        "workflow.stage.start stage=requirements",
        "workflow.stage.end stage=requirements",
        "workflow.stage.start stage=bid_parse",
        "workflow.stage.end stage=bid_parse",
        "workflow.review.start",
        "workflow.review.end",
        "workflow.run.end status=complete",
    ):
        assert any(event in message for message in messages), event


def test_workflow_persists_task_execution_log_and_summary(task_repository):
    workflow = make_workflow(
        task_repository,
        lambda file_metadata: [{"id": "compliance_001"}],
        lambda file_metadata: {"status": "success"},
        [],
    )
    try:
        workflow.run("task-001")
    finally:
        workflow.shutdown()

    task = task_repository.get("task-001")
    assert task is not None
    artifact_dir = Path(task.tender_file.storage_path).parent / "compliance_extraction"
    events = [
        json.loads(line)
        for line in (artifact_dir / "execution.jsonl").read_text().splitlines()
    ]
    event_names = [event["event"] for event in events]
    assert "workflow.run.start" in event_names
    assert "workflow.stage.end" in event_names
    assert "workflow.review.end" in event_names
    assert "workflow.run.end" in event_names
    summary = json.loads(
        (artifact_dir / "workflow_summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "complete"
    assert summary["stats"]["requirements_elapsed_ms"] is not None
    assert summary["stats"]["bid_parse_elapsed_ms"] is not None
    assert summary["stats"]["review_elapsed_ms"] is not None
    assert summary["stats"]["total_elapsed_ms"] is not None
