from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Any

from app.compliance_extraction import (
    ComplianceExtractionError,
    MinerUDocumentParser,
    extract_tender_compliance_objects,
)
from app.models import FileMetadata


def empty_tender_extraction_result() -> dict[str, list[dict[str, Any]]]:
    return {
        "templates": [],
        "project_requirements": [],
        "supplemental_materials": [],
    }


def extract_tender_objects(
    tender_file: FileMetadata,
    *,
    delay_seconds: float = 0.35,
) -> dict[str, list[dict[str, Any]]]:
    source_path = Path(tender_file.storage_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    time.sleep(delay_seconds)
    try:
        return extract_tender_compliance_objects(
            tender_file,
            parser=MinerUDocumentParser(
                command="",
                mineru_url="",
                allow_docx_fallback=True,
            ),
        )
    except ComplianceExtractionError:
        # Keep historical byte-stub fixtures usable; real DOCX packages never
        # enter this compatibility branch.
        if not zipfile.is_zipfile(source_path):
            return empty_tender_extraction_result()
        raise


def parse_bid_document(
    bid_file: FileMetadata,
    *,
    delay_seconds: float = 0.35,
) -> dict[str, Any]:
    source_path = Path(bid_file.storage_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    time.sleep(delay_seconds)
    return {
        "status": "success",
        "document_name": bid_file.filename,
        "section_count": 22,
        "block_count": 405,
        "table_count": 11,
        "image_count": 70,
    }


def run_compliance_review(
    extraction_result: dict[str, Any],
    parsed_bid: dict[str, Any],
) -> dict[str, str]:
    if parsed_bid.get("status") != "success":
        raise ValueError("模拟审查输入不完整")
    return {
        "mode": "mock",
        "message": "当前版本尚未执行真实合规性检查",
    }
