from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import build_default_workflow, create_app
from app.compliance_extraction import (
    StructuredBlock,
)
from app.mineru_v1 import MINERU_V1_PROTOCOL_LABEL, MINERU_V1_PROTOCOL_VERSION
from app.config import Settings
from app.mock_services import (
    extract_tender_objects,
    parse_bid_document,
    run_compliance_review,
)
from app.models import FileMetadata
from app.repository import BidCheckRepository


@pytest.fixture
def mineru_archive_handler():
    """Serve hand-built result ZIPs through the real 4.x HTTP boundary."""
    def build(archive_bytes, *, image_archive=None, submitted_jobs=None):
        uploads = {}

        def handler(request):
            route = request.url.path
            assert route.startswith("/v1/")
            if request.method == "POST" and route == "/v1/uploads":
                number = str(len(uploads) + 1)
                uploads[number] = json.loads(request.content)
                return httpx.Response(200, json={"id": f"upload-{number}", "status": "pending"})
            if request.method == "PUT":
                return httpx.Response(200, json={})
            if route.endswith("/complete"):
                number = route.split("/")[3].rsplit("-", 1)[-1]
                return httpx.Response(200, json={"status": "completed", "file": {"id": f"input-{number}"}})
            if request.method == "POST" and route == "/v1/parse/jobs":
                data = json.loads(request.content)
                number = data["files"][0]["source"]["file_id"].rsplit("-", 1)[-1]
                if submitted_jobs is not None:
                    submitted_jobs.append(f"task-{number}")
                return httpx.Response(202, json={"job_id": f"task-{number}", "status": "queued"})
            if route.startswith("/v1/parse/jobs/"):
                number = route.rsplit("-", 1)[-1]
                return httpx.Response(200, json={
                    "status": "completed", "files": [{"status": "completed", "output_files": {"zip": {"file_id": f"output-{number}"}}}],
                })
            if request.method == "DELETE":
                return httpx.Response(200, json={"deleted": True})
            if route.startswith("/v1/files/") and route.endswith("/content"):
                number = route.split("/")[3].rsplit("-", 1)[-1]
                content = image_archive if image_archive is not None and uploads[number]["mime_type"].startswith("image/") else archive_bytes
                return httpx.Response(200, content=content)
            raise AssertionError(f"Unexpected MinerU request: {request.method} {route}")

        return handler

    return build


class FixtureMineruParser:
    parser_name = "mineru"

    @property
    def cache_descriptor(self):
        return {
            "parser": "mineru",
            "transport": MINERU_V1_PROTOCOL_LABEL,
            "protocol": MINERU_V1_PROTOCOL_VERSION,
            "url": "https://fixture-mineru.example",
            "gpustack_model": "mineru4",
        }

    def __init__(self):
        self.parse_diagnostics = {
            "parser": "mineru",
            "mineru_called": True,
            "service_protocol": MINERU_V1_PROTOCOL_LABEL,
            "elapsed_ms": 0,
        }

    def parse(self, path):
        if path.stat().st_size < 100:
            return []
        section = "第六章 投标文件格式"
        return [
            StructuredBlock("b1", "heading", section, section, 1),
            StructuredBlock("b2", "heading", "投标函", section, 2),
            StructuredBlock("b3", "paragraph", "投标人名称：____", section, 3),
            StructuredBlock(
                "b4",
                "paragraph",
                "法定代表人应签字并加盖公章。",
                section,
                4,
            ),
            StructuredBlock("b5", "heading", "法定代表人身份证明", section, 5),
            StructuredBlock(
                "b6",
                "paragraph",
                "姓名：____，身份证明附国徽面和人像面。",
                section,
                6,
            ),
            StructuredBlock("b7", "heading", "投标人须知前附表", section, 7),
            StructuredBlock("b8", "paragraph", "投标有效期 | 90 天", section, 8),
            StructuredBlock("b9", "heading", "投标人资格要求", section, 9),
            StructuredBlock(
                "b10",
                "paragraph",
                "须随投标文件提供营业执照或事业单位法人证书。",
                section,
                10,
            ),
            StructuredBlock("b11", "paragraph", "商务评分满分 20 分。", section, 11),
        ]


class FixtureBidDocumentParser:
    def parse(self, path, *, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "status": "success",
            "document_name": path.name,
            "artifact_dir": str(output_dir),
            "stats": {
                "structured_block_count": 1,
                "section_count": 1,
                "table_count": 0,
                "image_count": 0,
            },
        }


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
    return build_default_workflow(
        settings,
        repository,
        document_parser=FixtureMineruParser(),
        bid_document_parser=FixtureBidDocumentParser(),
    )


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
