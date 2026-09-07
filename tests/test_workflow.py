from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Barrier

import pytest

from app.models import FileMetadata
from app.workflow import BidCheckServices, BidCheckWorkflow


def empty_objects():
    return {"templates": [], "project_requirements": [], "supplemental_materials": []}


def evaluation_result():
    return {
        "source_sections": [],
        "score_categories": [],
        "score_items": [],
        "veto_rules": [],
        "uncertain_rules": [],
        "stats": {"score_category_count": 0},
    }


def create_evaluation_task(repository, tmp_path):
    task_dir = tmp_path / "evaluation-task"
    task_dir.mkdir()
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    return repository.create(
        "evaluation-task",
        FileMetadata("招标文件.docx", tender_path.stat().st_size, str(tender_path)),
        FileMetadata("投标文件.docx", bid_path.stat().st_size, str(bid_path)),
        "evaluation",
    )


def create_full_task(repository, tmp_path):
    task_dir = tmp_path / "full-task"
    task_dir.mkdir()
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    return repository.create(
        "full-task",
        FileMetadata("招标文件.docx", tender_path.stat().st_size, str(tender_path)),
        FileMetadata("投标文件.docx", bid_path.stat().st_size, str(bid_path)),
        "full",
    )


def test_requirements_and_parse_enter_concurrently(task_repository):
    barrier = Barrier(2, timeout=2)
    entered: list[str] = []

    def extract(file_metadata):
        entered.append("requirements")
        barrier.wait()
        return empty_objects()

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


def test_workflow_passes_original_bid_file_metadata_into_review(task_repository):
    observed: list[dict] = []

    def review(requirements, parsed):
        observed.append(parsed)
        return {"mode": "mock"}

    workflow = BidCheckWorkflow(
        task_repository,
        BidCheckServices(
            extract=lambda file_metadata: empty_objects(),
            parse=lambda file_metadata: {"status": "success"},
            review=review,
        ),
    )
    try:
        workflow.run("task-001")
    finally:
        workflow.shutdown()

    assert observed[0]["original_file_metadata"] == {
        "filename": "投标文件.docx",
        "size": len(b"docx-bid"),
        "storage_path": observed[0]["original_file_metadata"]["storage_path"],
    }


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
        return empty_objects()

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
        extract=lambda file_metadata: empty_objects(),
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
        lambda file_metadata: empty_objects(),
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
        lambda file_metadata: empty_objects(),
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


def test_evaluation_workflow_extracts_tender_only_and_skips_bid_parse(
    task_repository, tmp_path
):
    calls = []

    def evaluate(tender_file, recorder=None):
        del recorder
        calls.append(("evaluate", tender_file.filename))
        return evaluation_result()

    def parse(_bid_file):
        calls.append(("parse", "unexpected"))
        raise AssertionError("evaluation mode must not parse the bid file")

    services = BidCheckServices(
        extract=lambda _: empty_objects(),
        parse=parse,
        review=lambda *_: (_ for _ in ()).throw(
            AssertionError("evaluation mode must skip review")
        ),
        extract_evaluation_with_recorder=evaluate,
    )
    task = create_evaluation_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    completed = task_repository.get(task.task_id)
    assert completed is not None
    assert completed.status == "complete"
    assert completed.result["evaluation_rules"] == evaluation_result()
    assert calls == [("evaluate", "招标文件.docx")]
    assert completed.bid_parse_status == "complete"
    assert completed.review_status == "complete"


def test_evaluation_workflow_runs_objective_scoring_after_rule_extraction(
    task_repository, tmp_path
):
    calls = []
    rules = evaluation_result()
    scores = {"score_items": [], "stats": {"objective_item_count": 0}}

    def evaluate(tender_file, recorder=None):
        calls.append(("evaluate", tender_file.filename))
        return rules

    def score(tender_file, bid_file, evaluation_rules, recorder=None):
        calls.append(("score", tender_file.filename, bid_file.filename))
        assert evaluation_rules is rules
        return scores

    services = BidCheckServices(
        extract=lambda _: empty_objects(),
        parse=lambda _: {"status": "unused"},
        review=lambda *_: {"status": "unused"},
        extract_evaluation_with_recorder=evaluate,
        score_objective_with_recorder=score,
    )
    task = create_evaluation_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    completed = task_repository.get(task.task_id)
    assert completed is not None
    assert completed.result["objective_scores"] == scores
    assert calls == [
        ("evaluate", "招标文件.docx"),
        ("score", "招标文件.docx", "投标文件.docx"),
    ]


def test_evaluation_workflow_runs_veto_execution_after_objective_scoring(
    task_repository, tmp_path
):
    calls = []
    rules = evaluation_result()
    scores = {"score_items": [], "stats": {"objective_item_count": 0}}
    veto = {"veto_rule_reviews": [], "stats": {"formal_rule_count": 0}}

    def evaluate(tender_file, recorder=None):
        del recorder
        calls.append("evaluate")
        return rules

    def score(tender_file, bid_file, evaluation_rules, recorder=None):
        del tender_file, bid_file, recorder
        calls.append("objective")
        assert evaluation_rules is rules
        return scores

    def execute(
        tender_file,
        bid_file,
        evaluation_rules,
        objective_scores=None,
        recorder=None,
    ):
        del tender_file, bid_file, recorder
        calls.append("veto")
        assert evaluation_rules is rules
        assert objective_scores is scores
        return veto

    services = BidCheckServices(
        extract=lambda _: empty_objects(),
        parse=lambda _: {"status": "unused"},
        review=lambda *_: {"status": "unused"},
        extract_evaluation_with_recorder=evaluate,
        score_objective_with_recorder=score,
        execute_veto_with_recorder=execute,
    )
    task = create_evaluation_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    completed = task_repository.get(task.task_id)
    assert completed is not None
    assert completed.result["veto_rule_reviews"] == veto
    assert calls == ["evaluate", "objective", "veto"]


def test_full_workflow_runs_compliance_then_evaluation_and_merges_results(
    task_repository, tmp_path
):
    calls = []
    rules = evaluation_result()
    objective = {"score_items": [], "stats": {"objective_item_count": 0}}
    veto = {"veto_rule_reviews": [], "stats": {"formal_rule_count": 0}}

    def extract(_tender_file):
        calls.append("extract")
        return empty_objects()

    def parse(_bid_file):
        calls.append("parse")
        return {"status": "success"}

    def review(_requirements, _parsed_bid):
        calls.append("review")
        return {"mode": "compliance"}

    def evaluate(_tender_file, recorder=None):
        del recorder
        assert calls[-1] == "review"
        calls.append("evaluate")
        return rules

    def score(_tender_file, _bid_file, evaluation_rules, recorder=None):
        del recorder
        assert evaluation_rules is rules
        assert calls[-1] == "evaluate"
        calls.append("objective")
        return objective

    def execute(
        _tender_file,
        _bid_file,
        evaluation_rules,
        *,
        objective_scores=None,
        recorder=None,
    ):
        del recorder
        assert evaluation_rules is rules
        assert objective_scores is objective
        assert calls[-1] == "objective"
        calls.append("veto")
        return veto

    services = BidCheckServices(
        extract=extract,
        parse=parse,
        review=review,
        extract_evaluation_with_recorder=evaluate,
        score_objective_with_recorder=score,
        execute_veto_with_recorder=execute,
    )
    task = create_full_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    completed = task_repository.get(task.task_id)
    assert completed is not None
    assert completed.status == "complete"
    assert completed.requirements_status == "complete"
    assert completed.bid_parse_status == "complete"
    assert completed.review_status == "complete"
    assert completed.result["review_result"] == {"mode": "compliance"}
    assert completed.result["evaluation_rules"] is not None
    assert completed.result["objective_scores"] == objective
    assert completed.result["veto_rule_reviews"] == veto
    assert calls[-4:] == ["review", "evaluate", "objective", "veto"]


def test_full_workflow_does_not_start_evaluation_when_compliance_fails(
    task_repository, tmp_path
):
    calls = []

    def fail_review(_requirements, _parsed_bid):
        calls.append("review")
        raise RuntimeError("合规阶段失败")

    services = BidCheckServices(
        extract=lambda _file: empty_objects(),
        parse=lambda _file: {"status": "success"},
        review=fail_review,
        extract_evaluation_with_recorder=lambda *_args, **_kwargs: calls.append(
            "evaluate"
        ),
        score_objective_with_recorder=lambda *_args, **_kwargs: calls.append(
            "objective"
        ),
        execute_veto_with_recorder=lambda *_args, **_kwargs: calls.append("veto"),
    )
    task = create_full_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    failed = task_repository.get(task.task_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.failed_stage == "review"
    assert failed.error_message == "合规阶段失败"
    assert calls == ["review"]


def test_full_workflow_keeps_compliance_result_when_evaluation_fails(
    task_repository, tmp_path
):
    compliance_result = {"mode": "compliance", "issues": ["kept"]}

    def fail_score(*_args, **_kwargs):
        raise RuntimeError("评分阶段失败")

    services = BidCheckServices(
        extract=lambda _file: empty_objects(),
        parse=lambda _file: {"status": "success"},
        review=lambda _requirements, _parsed_bid: compliance_result,
        extract_evaluation_with_recorder=lambda _file, recorder=None: evaluation_result(),
        score_objective_with_recorder=fail_score,
    )
    task = create_full_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    failed = task_repository.get(task.task_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.failed_stage == "review"
    assert failed.error_message == "评分阶段失败"
    assert failed.result["review_result"] == compliance_result


def test_run_subjective_does_not_call_other_evaluation_stages(
    task_repository, tmp_path
):
    calls = []
    task = create_evaluation_task(task_repository, tmp_path)
    artifact_dir = Path(task.tender_file.storage_path).parent / "compliance_extraction"
    artifact_dir.mkdir(parents=True)
    rules = evaluation_result()
    (artifact_dir / "11_evaluation_rules.json").write_text(
        json.dumps(rules),
        encoding="utf-8",
    )

    def score_subjective(tender_file, bid_file, evaluation_rules, recorder=None):
        del tender_file, bid_file, recorder
        calls.append("subjective")
        assert evaluation_rules == rules
        return {"score_items": [], "stats": {"subjective_item_count": 0}}

    services = BidCheckServices(
        extract=lambda _: calls.append("extract"),
        parse=lambda _: calls.append("parse"),
        review=lambda *_: calls.append("review"),
        score_objective_with_recorder=lambda *args, **kwargs: calls.append("objective"),
        execute_veto_with_recorder=lambda *args, **kwargs: calls.append("veto"),
        score_subjective_with_recorder=score_subjective,
    )
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run_subjective(task.task_id)
    finally:
        workflow.shutdown()

    completed = task_repository.get(task.task_id)
    assert calls == ["subjective"]
    assert completed is not None
    assert completed.result["subjective_scores"]["score_items"] == []
