from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import build_default_workflow, create_app
from app.config import Settings
from app.mock_services import (
    extract_tender_objects,
    parse_bid_document,
    run_compliance_review,
)
from app.models import FileMetadata
from app.repository import BidCheckRepository


@pytest.fixture
def task_repository(tmp_path):
    repository = BidCheckRepository(tmp_path / "bid_check.db")
    task_dir = tmp_path / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"docx-tender")
    bid_path.write_bytes(b"docx-bid")
    repository.create(
        "task-001",
        FileMetadata(
            "招标文件.docx",
            tender_path.stat().st_size,
            str(tender_path),
        ),
        FileMetadata(
            "投标文件.docx",
            bid_path.stat().st_size,
            str(bid_path),
        ),
        "compliance",
    )
    return repository


@pytest.fixture
def settings(tmp_path):
    data_dir = tmp_path / "data"
    return Settings(
        project_dir=tmp_path,
        data_dir=data_dir,
        database_path=data_dir / "bid_check.db",
        tasks_dir=data_dir / "tasks",
        mock_delay_seconds=0,
    )


@pytest.fixture
def repository(settings):
    return BidCheckRepository(settings.database_path)


@pytest.fixture
def workflow(settings, repository):
    return build_default_workflow(settings, repository)


@pytest.fixture
def client(settings, repository, workflow):
    with TestClient(
        create_app(
            settings=settings,
            repository=repository,
            workflow=workflow,
        )
    ) as test_client:
        yield test_client


@pytest.fixture
def stored_task(settings, repository):
    task_dir = settings.tasks_dir / "stored-task"
    task_dir.mkdir(parents=True)
    tender_path = task_dir / "tender.docx"
    bid_path = task_dir / "bid.docx"
    tender_path.write_bytes(b"tender")
    bid_path.write_bytes(b"bid")
    return repository.create(
        "stored-task",
        FileMetadata("招标文件.docx", 6, str(tender_path)),
        FileMetadata("投标文件.docx", 3, str(bid_path)),
        "compliance",
    )


@pytest.fixture
def mock_complete_result(stored_task):
    extraction_result = extract_tender_objects(
        stored_task.tender_file,
        delay_seconds=0,
    )
    bid_parse = parse_bid_document(
        stored_task.bid_file,
        delay_seconds=0,
    )
    return {
        **extraction_result,
        "bid_parse": bid_parse,
        "review_result": run_compliance_review(extraction_result, bid_parse),
    }
