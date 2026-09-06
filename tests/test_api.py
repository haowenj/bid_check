from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def docx_files():
    return {
        "tender_file": ("招标文件.docx", b"PK\x03\x04tender", DOCX_MIME),
        "bid_file": ("投标文件.docx", b"PK\x03\x04bid", DOCX_MIME),
    }


def test_create_task_requires_both_files(client):
    response = client.post(
        "/api/bid-check/tasks",
        files={"tender_file": docx_files()["tender_file"]},
        data={"check_mode": "compliance"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["tender_file", "bid_file"])
def test_create_task_rejects_non_docx(client, field):
    files = docx_files()
    files[field] = ("不支持.txt", b"text", "text/plain")

    response = client.post(
        "/api/bid-check/tasks",
        files=files,
        data={"check_mode": "compliance"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "当前仅支持 .docx 文件。"


@pytest.mark.parametrize("mode", ["full"])
def test_development_modes_do_not_create_tasks(client, repository, mode):
    response = client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": mode},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "该校验方式正在开发中。"
    assert repository.count() == 0


def test_evaluation_mode_creates_tender_rule_task(client, repository, settings):
    response = client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": "evaluation"},
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    task = repository.get(task_id)
    assert task is not None
    assert task.status == "complete"
    assert task.check_mode == "evaluation"
    assert task.result["evaluation_rules"]["score_items"] == []
    artifact = (
        settings.tasks_dir
        / task_id
        / "compliance_extraction"
        / "11_evaluation_rules.json"
    )
    assert artifact.is_file()


def test_create_task_rejects_empty_file(client):
    files = docx_files()
    files["bid_file"] = (files["bid_file"][0], b"", files["bid_file"][2])

    response = client.post(
        "/api/bid-check/tasks",
        files=files,
        data={"check_mode": "compliance"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "上传文件不能为空。"


def test_create_task_rejects_unknown_mode(client):
    response = client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": "unknown"},
    )

    assert response.status_code == 422


def test_get_unknown_task_returns_404(client):
    response = client.get("/api/bid-check/tasks/not-found")

    assert response.status_code == 404
    assert response.json()["detail"] == "标书检查任务不存在。"


def test_delete_task_removes_database_record_and_task_artifacts(
    client,
    repository,
    settings,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.complete(stored_task.task_id, {})
    task_dir = settings.tasks_dir / stored_task.task_id
    artifact = task_dir / "structured_document.json"
    artifact.write_text("{}", encoding="utf-8")

    response = client.delete(f"/api/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert response.json() == {
        "task_id": stored_task.task_id,
        "deleted": True,
    }
    assert repository.get(stored_task.task_id) is None
    assert not task_dir.exists()


def test_delete_running_task_is_rejected_without_removing_data(
    client,
    repository,
    settings,
    stored_task,
):
    repository.update_stage(stored_task.task_id, "requirements", "running")

    response = client.delete(f"/api/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 409
    assert response.json()["detail"] == "任务正在执行，无法删除，请稍后重试。"
    assert repository.get(stored_task.task_id) is not None
    assert (settings.tasks_dir / stored_task.task_id).exists()


def test_upload_write_failure_returns_500_without_task(
    client,
    repository,
    settings,
):
    with patch("app.api.Path.write_bytes", side_effect=OSError("disk full")):
        response = client.post(
            "/api/bid-check/tasks",
            files=docx_files(),
            data={"check_mode": "compliance"},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "创建任务失败，请稍后重试。"
    assert repository.count() == 0
    assert not settings.tasks_dir.exists() or list(settings.tasks_dir.iterdir()) == []


def test_repository_create_failure_cleans_saved_files(
    client,
    repository,
    settings,
):
    with patch.object(
        repository,
        "create",
        side_effect=RuntimeError("db unavailable"),
    ):
        response = client.post(
            "/api/bid-check/tasks",
            files=docx_files(),
            data={"check_mode": "compliance"},
        )

    assert response.status_code == 500
    assert repository.count() == 0
    assert not settings.tasks_dir.exists() or list(settings.tasks_dir.iterdir()) == []


def test_create_task_stores_files_and_runs_workflow(
    client,
    settings,
    repository,
):
    response = client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": "compliance"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["check_mode"] == "compliance"
    assert payload["tender_file"]["filename"] == "招标文件.docx"
    assert payload["bid_file"]["filename"] == "投标文件.docx"
    task_dir = settings.tasks_dir / payload["task_id"]
    assert (task_dir / "tender.docx").read_bytes() == b"PK\x03\x04tender"
    assert (task_dir / "bid.docx").read_bytes() == b"PK\x03\x04bid"
    task = repository.get(payload["task_id"])
    assert task.status == "complete"
    assert task.result["bid_parse"]["stats"]["structured_block_count"] == 1


def test_get_task_returns_stable_payload(client, stored_task):
    response = client.get(f"/api/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    assert set(response.json()) >= {
        "task_id",
        "tender_file",
        "bid_file",
        "check_mode",
        "status",
        "requirements_status",
        "bid_parse_status",
        "review_status",
        "failed_stage",
        "error_message",
        "created_at",
        "updated_at",
    }


def test_get_task_reads_empty_file_review_artifact(client, stored_task):
    artifact_dir = Path(stored_task.tender_file.storage_path).parent / "compliance_extraction"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "10_file_requirement_reviews.json").write_text(
        json.dumps(
            {
                "mode": "file_requirements",
                "source": "original_uploaded_file",
                "original_file": {
                    "filename": "投标文件.docx",
                    "extension": ".docx",
                    "size_bytes": 3,
                    "size_display": "3 B",
                },
                "requirements": [],
                "stats": {
                    "requirement_count": 0,
                    "pass_count": 0,
                    "fail_count": 0,
                    "not_supported_count": 0,
                    "issue_count": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/bid-check/tasks/{stored_task.task_id}")

    assert response.status_code == 200
    review_result = response.json()["review_result"]
    assert review_result["file_requirement_reviews"] == []
    assert review_result["file_requirement_original_file"]["size_bytes"] == 3
