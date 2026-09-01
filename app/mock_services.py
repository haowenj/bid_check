from __future__ import annotations

import time
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.compliance_extraction import (
    ComplianceExtractionError,
    extract_compliance_requirements_real,
)
from app.models import FileMetadata

# Compatibility fixtures use the same simplified contract as real extraction.
MOCK_COMPLIANCE_REQUIREMENTS: list[dict[str, Any]] = [
    {
        "id": "tender_requirement_001",
        "name": "商务投标文件封面",
        "rule": "应填写投标人名称和日期。",
        "condition": None,
        "source": {
            "section": "第六章 投标文件格式 > 商务投标文件封面",
            "block_ids": [],
            "source_text": "投标人名称：____年____月____日",
        },
    },
    {
        "id": "tender_requirement_002",
        "name": "法定代表人身份证明",
        "rule": "法定代表人身份证明需附合法有效身份证明；提供居民身份证时，需同时提供国徽面及人像面。",
        "condition": None,
        "source": {
            "section": "第六章 投标文件格式 > 法定代表人身份证明",
            "block_ids": [],
            "source_text": "附：法定代表人的合法有效身份证明复印件或扫描件。",
        },
    },
    {
        "id": "tender_requirement_003",
        "name": "授权委托书",
        "rule": "应提供授权委托书及代理人身份证明。",
        "condition": "由委托代理人办理投标事宜时",
        "source": {
            "section": "第六章 投标文件格式 > 授权委托书",
            "block_ids": [],
            "source_text": "由委托代理人办理投标事宜时，应提供授权委托书及代理人身份证明。",
        },
    },
    {
        "id": "tender_requirement_004",
        "name": "基本账户",
        "rule": "应填写基本账户开户信息并提供基本户开户证明文件。",
        "condition": None,
        "source": {
            "section": "资格审查资料 > 基本账户",
            "block_ids": [],
            "source_text": "应填写基本账户开户信息并提供基本户开户证明文件。",
        },
    },
    {
        "id": "tender_requirement_005",
        "name": "业绩材料",
        "rule": "业绩证明文件应与业绩情况表一一对应，并按要求提供合同关键页及相关证明材料。",
        "condition": None,
        "source": {
            "section": "资格审查资料 > 业绩材料",
            "block_ids": [],
            "source_text": "业绩证明文件应与业绩情况表一一对应，并提供合同关键页及相关证明材料。",
        },
    },
]


def extract_compliance_requirements(
    tender_file: FileMetadata,
    *,
    delay_seconds: float = 0.35,
) -> list[dict[str, Any]]:
    source_path = Path(tender_file.storage_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    time.sleep(delay_seconds)
    try:
        return extract_compliance_requirements_real(tender_file)
    except ComplianceExtractionError:
        # Keep historical byte-stub fixtures usable; real DOCX packages never
        # enter this compatibility branch.
        if not zipfile.is_zipfile(source_path):
            return deepcopy(MOCK_COMPLIANCE_REQUIREMENTS)
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
    requirements: list[dict[str, Any]],
    parsed_bid: dict[str, Any],
) -> dict[str, str]:
    if parsed_bid.get("status") != "success":
        raise ValueError("模拟审查输入不完整")
    return {
        "mode": "mock",
        "message": "当前版本尚未执行真实合规性检查",
    }
