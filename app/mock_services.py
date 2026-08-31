from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.models import FileMetadata


MOCK_COMPLIANCE_REQUIREMENTS: list[dict[str, Any]] = [
    {
        "id": "compliance_001",
        "name": "商务投标文件封面完整性",
        "category": "required_field",
        "target": {
            "name": "商务投标文件封面",
            "scope": "single_section",
        },
        "checks": [
            {
                "id": "compliance_001_01",
                "requirement": "投标人名称应填写完整",
                "check_type": "required_field",
                "evidence_type": "text",
            },
            {
                "id": "compliance_001_02",
                "requirement": "日期应填写完整",
                "check_type": "date",
                "evidence_type": "text",
            },
        ],
        "applicability": {"type": "always", "condition": None},
        "source": {
            "section": "第六章 投标文件格式 > 商务投标文件封面",
            "block_ids": [],
            "source_text": "投标人名称：____年____月____日",
        },
    },
    {
        "id": "compliance_002",
        "name": "法定代表人身份证明完整性",
        "category": "attachment",
        "target": {
            "name": "法定代表人/负责人身份证明",
            "scope": "single_section",
        },
        "checks": [
            {
                "id": "compliance_002_01",
                "requirement": "法定代表人基本信息应填写完整",
                "check_type": "required_field",
                "evidence_type": "text",
            },
            {
                "id": "compliance_002_02",
                "requirement": "应提供法定代表人身份证明",
                "check_type": "attachment_exists",
                "evidence_type": "structure",
            },
            {
                "id": "compliance_002_03",
                "requirement": "居民身份证应包含人像面和国徽面",
                "check_type": "attachment_content",
                "evidence_type": "vision",
            },
        ],
        "applicability": {"type": "always", "condition": None},
        "source": {
            "section": "第六章 投标文件格式 > 法定代表人/负责人身份证明",
            "block_ids": [],
            "source_text": "附：法定代表人/负责人的合法有效身份证明复印件或扫描件。",
        },
    },
    {
        "id": "compliance_003",
        "name": "授权委托书完整性",
        "category": "required_field",
        "target": {
            "name": "法定代表人/负责人授权委托书",
            "scope": "single_section",
        },
        "checks": [
            {
                "id": "compliance_003_01",
                "requirement": "委托代理人信息应填写完整",
                "check_type": "required_field",
                "evidence_type": "text",
            },
            {
                "id": "compliance_003_02",
                "requirement": "授权内容应填写完整",
                "check_type": "required_field",
                "evidence_type": "text",
            },
            {
                "id": "compliance_003_03",
                "requirement": "日期应填写完整",
                "check_type": "date",
                "evidence_type": "text",
            },
            {
                "id": "compliance_003_04",
                "requirement": "要求的签字不得缺失",
                "check_type": "signature",
                "evidence_type": "vision",
            },
            {
                "id": "compliance_003_05",
                "requirement": "要求的盖章不得缺失",
                "check_type": "seal",
                "evidence_type": "vision",
            },
        ],
        "applicability": {"type": "always", "condition": None},
        "source": {
            "section": "第六章 投标文件格式 > 法定代表人/负责人授权委托书",
            "block_ids": [],
            "source_text": "委托代理人信息、授权内容、签字盖章及日期应按模板要求填写。",
        },
    },
    {
        "id": "compliance_004",
        "name": "函件及承诺书完整性",
        "category": "required_field",
        "target": {"name": "函件及承诺书", "scope": "each_section"},
        "checks": [
            {
                "id": "compliance_004_01",
                "requirement": "投标人名称不得缺失",
                "check_type": "required_field",
                "evidence_type": "text",
            },
            {
                "id": "compliance_004_02",
                "requirement": "日期不得缺失",
                "check_type": "date",
                "evidence_type": "text",
            },
            {
                "id": "compliance_004_03",
                "requirement": "模板占位符不得残留",
                "check_type": "placeholder",
                "evidence_type": "text",
            },
        ],
        "applicability": {"type": "always", "condition": None},
        "source": {
            "section": "第六章 投标文件格式",
            "block_ids": [],
            "source_text": "各函件及承诺书应按照招标文件提供的格式填写完整。",
        },
    },
    {
        "id": "compliance_005",
        "name": "人员材料完整性",
        "category": "attachment",
        "target": {"name": "项目人员材料", "scope": "each_person"},
        "checks": [
            {
                "id": "compliance_005_01",
                "requirement": "人员基本信息应填写完整",
                "check_type": "required_field",
                "evidence_type": "text",
            },
            {
                "id": "compliance_005_02",
                "requirement": "要求提供的身份证明不得缺失",
                "check_type": "attachment_exists",
                "evidence_type": "structure",
            },
            {
                "id": "compliance_005_03",
                "requirement": "要求提供的社保证明不得缺失",
                "check_type": "attachment_exists",
                "evidence_type": "structure",
            },
            {
                "id": "compliance_005_04",
                "requirement": "要求提供的资格证书不得缺失",
                "check_type": "attachment_exists",
                "evidence_type": "structure",
            },
        ],
        "applicability": {"type": "always", "condition": None},
        "source": {
            "section": "招标文件相关人员材料要求",
            "block_ids": [],
            "source_text": "应按照招标文件要求提供人员名单及相应证明材料。",
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
    return deepcopy(MOCK_COMPLIANCE_REQUIREMENTS)


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
    if not requirements or parsed_bid.get("status") != "success":
        raise ValueError("模拟审查输入不完整")
    return {
        "mode": "mock",
        "message": "当前版本尚未执行真实合规性检查",
    }
