from __future__ import annotations

import pytest

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

