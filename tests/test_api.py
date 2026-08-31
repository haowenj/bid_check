from __future__ import annotations

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


@pytest.mark.parametrize("mode", ["evaluation", "full"])
def test_development_modes_do_not_create_tasks(client, repository, mode):
    response = client.post(
        "/api/bid-check/tasks",
        files=docx_files(),
        data={"check_mode": mode},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "该校验方式正在开发中。"
    assert repository.count() == 0


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
    assert repository.get(payload["task_id"]).status == "complete"


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
