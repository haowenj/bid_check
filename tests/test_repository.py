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
