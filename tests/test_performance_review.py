from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path

from app.attachment_review import run_compliance_review_with_attachments
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.performance_review import (
    PERFORMANCE_CASE_TITLE,
    _select_performance_candidates,
    run_performance_contract_review,
)
from app.template_text_review import DeterministicTemplateTextReviewLLM


class RecordingPerformanceLLM:
    model = "test-model"

    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, str, list[dict]]] = []

    def review_attachment(self, system_prompt, user_prompt, images):
        self.calls.append((system_prompt, user_prompt, copy.deepcopy(images)))
        return copy.deepcopy(self.response)


class RecordingPerformanceTextLLM:
    model = "test-text-model"

    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def review_template(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return copy.deepcopy(self.response)


class RecordingMultiPerformanceTextLLM:
    model = "test-text-model"

    def __init__(self, responses_by_image_id: dict[str, dict]):
        self.responses_by_image_id = responses_by_image_id
        self.calls: list[tuple[str, str]] = []

    def review_template(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        match = re.search(r"=== (i21[1-6]) ===", user_prompt)
        if match is None:
            raise AssertionError("文本模型输入没有当前 21.x 的 image_id")
        return copy.deepcopy(self.responses_by_image_id[match.group(1)])


class RecordingMultiPerformanceVisionLLM:
    model = "test-vision-model"

    def __init__(self):
        self.calls: list[tuple[str, str, list[dict]]] = []

    def review_attachment(self, system_prompt, user_prompt, images):
        self.calls.append((system_prompt, user_prompt, copy.deepcopy(images)))
        image_id = images[0]["image_id"]
        return {
            "signature_page": {
                "status": "pass",
                "reason": "测试图片显示双方签字盖章。",
                "evidence_image_ids": [image_id],
            },
            "signature_date": {
                "status": "pass",
                "reason": "测试图片显示已填写日期。",
                "evidence_image_ids": [image_id],
            },
        }


class ConcurrencyTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(5)
        self.active = 0
        self.max_active = 0
        self.events: dict[str, list[str]] = {}

    def enter(self, case_id: str, stage: str):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.events.setdefault(case_id, []).append(stage)
        try:
            self.barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.01)
        with self.lock:
            self.active -= 1


class ConcurrentMultiPerformanceTextLLM:
    model = "test-text-model"

    def __init__(self, responses_by_image_id: dict[str, dict], tracker):
        self.responses_by_image_id = responses_by_image_id
        self.tracker = tracker
        self.calls: list[tuple[str, str]] = []

    def review_template(self, system_prompt, user_prompt):
        match = re.search(r"=== (i21[1-6]) ===", user_prompt)
        if match is None:
            raise AssertionError("文本模型输入没有当前 21.x 的 image_id")
        image_id = match.group(1)
        with self.tracker.lock:
            self.calls.append((system_prompt, user_prompt))
        self.tracker.enter(image_id, "text")
        return copy.deepcopy(self.responses_by_image_id[image_id])


class ConcurrentMultiPerformanceVisionLLM:
    model = "test-vision-model"

    def __init__(self, tracker):
        self.tracker = tracker
        self.calls: list[tuple[str, str, list[dict]]] = []

    def review_attachment(self, system_prompt, user_prompt, images):
        image_id = images[0]["image_id"]
        case_id = re.sub(r"^i21", "i21", image_id)
        with self.tracker.lock:
            self.calls.append((system_prompt, user_prompt, copy.deepcopy(images)))
        self.tracker.enter(case_id, "visual")
        return {
            "signature_page": {
                "status": "pass",
                "reason": "测试图片显示双方签字盖章。",
                "evidence_image_ids": [image_id],
            },
            "signature_date": {
                "status": "pass",
                "reason": "测试图片显示已填写日期。",
                "evidence_image_ids": [image_id],
            },
        }


def _template() -> dict:
    body = (
        "21 业绩情况表\n"
        "编制要求：证明文件顺序应与“业绩情况表”一一对应；"
        "合同关键页须包括合同服务内容、实施时间、合同金额、合同签页，"
        "如果提供的是框架合同，需提供对应框架合同结算的发票或订单或结算单据。"
    )
    return {
        "id": "tender_template_021",
        "name": "业绩情况表",
        "body": body,
        "attachments": [body],
        "source": {"source_text": body},
    }


def _document() -> dict:
    return {
        "sections": [
            {
                "section_id": "s21",
                "parent_section_id": None,
                "level": 1,
                "title": "21 业绩情况表",
                "path": ["21 业绩情况表"],
                "start_order": 1,
                "end_order": 8,
                "direct_block_ids": ["b21-heading", "b21-table"],
            },
            {
                "section_id": "s211",
                "parent_section_id": "s21",
                "level": 2,
                "title": "21.1 信达旺大厦云平台运营及维护服务",
                "path": ["21 业绩情况表", "21.1 信达旺大厦云平台运营及维护服务"],
                "start_order": 4,
                "end_order": 6,
                "direct_block_ids": ["b211-heading", "b211-image-1", "b211-image-2"],
            },
            {
                "section_id": "s212",
                "parent_section_id": "s21",
                "level": 2,
                "title": "其他业绩合同",
                "path": ["21 业绩情况表", "其他业绩合同"],
                "start_order": 7,
                "end_order": 8,
                "direct_block_ids": ["b212-heading", "b212-image"],
            },
        ],
        "blocks": [
            {
                "block_id": "b21-heading",
                "type": "heading",
                "text": "21 业绩情况表",
                "order": 1,
            },
            {
                "block_id": "b21-table",
                "type": "table",
                "text": "序号 | 项目名称 | 销售金额 | 证明文件所在页码",
                "order": 2,
            },
            {
                "block_id": "b211-heading",
                "type": "heading",
                "text": "21.1 信达旺大厦云平台运营及维护服务",
                "order": 4,
            },
            {"block_id": "b211-image-1", "type": "image", "text": "[MinerU image]", "order": 5},
            {"block_id": "b211-image-2", "type": "image", "text": "[MinerU image]", "order": 6},
            {
                "block_id": "b212-heading",
                "type": "heading",
                "text": "21.2 其他业绩合同",
                "order": 7,
            },
            {"block_id": "b212-image", "type": "image", "text": "[MinerU image]", "order": 8},
        ],
        "tables": [
            {
                "table_id": "t21",
                "block_id": "b21-table",
                "section_id": "s21",
                "rows": [
                    ["序号", "项目名称", "销售金额", "证明文件所在页码"],
                    ["1", "信达旺大厦云平台运营及维护服务", "96", "38~50"],
                    ["2", "其他业绩合同", "10", "51~62"],
                ],
            }
        ],
        "images": [
            {
                "image_id": "i211-1",
                "block_id": "b211-image-1",
                "section_id": "s211",
                "section_path": ["21 业绩情况表", "21.1 信达旺大厦云平台运营及维护服务"],
                "img_path": "images/contract-1.png",
                "asset_status": "ready",
            },
            {
                "image_id": "i211-2",
                "block_id": "b211-image-2",
                "section_id": "s211",
                "section_path": ["21 业绩情况表", "21.1 信达旺大厦云平台运营及维护服务"],
                "img_path": "images/contract-2.png",
                "asset_status": "ready",
            },
            {
                "image_id": "i212-1",
                "block_id": "b212-image",
                "section_id": "s212",
                "section_path": ["21 业绩情况表", "21.2 其他业绩合同"],
                "img_path": "images/other-contract.png",
                "asset_status": "ready",
            },
        ],
    }


def _response(*, framework_status: str = "no") -> dict:
    return {
        "status": "pass",
        "summary": "合同关键页均有证据。",
        "framework_contract": {
            "status": framework_status,
            "reason": "图片显示为普通服务合同。",
            "evidence_image_ids": ["i211-1"],
        },
        "materials": [
            {
                "material_id": "m1",
                "material_type": "服务合同",
                "role": "contract",
                "image_ids": ["i211-1", "i211-2"],
                "facts": [
                    {
                        "name": "合同名称",
                        "status": "present",
                        "value": "信达旺大厦云平台运营及维护服务合同",
                        "evidence_image_ids": ["i211-1"],
                    }
                ],
            }
        ],
        "checks": [
            {
                "key": "service_content",
                "status": "pass",
                "reason": "合同正文明确写明云平台运营及维护服务。",
                "evidence_image_ids": ["i211-1"],
            },
            {
                "key": "implementation_time",
                "status": "pass",
                "reason": "合同中可识别服务实施时间。",
                "evidence_image_ids": ["i211-1"],
            },
            {
                "key": "contract_amount",
                "status": "pass",
                "reason": "合同中可识别合同金额。",
                "evidence_image_ids": ["i211-2"],
            },
            {
                "key": "signature_page",
                "status": "pass",
                "reason": "合同签页可识别双方签署信息。",
                "evidence_image_ids": ["i211-2"],
            },
            {
                "key": "signature_date",
                "status": "pass",
                "reason": "合同签署页可识别签署日期。",
                "evidence_image_ids": ["i211-2"],
            },
        ],
    }


def _text_response() -> dict:
    return {
        "summary": "完整 OCR 文本识别到一份普通服务合同。",
        "facts": {
            "project_name": [
                {
                    "value": "信达旺大厦云平台运营及维护服务合同",
                    "image_id": "i211-1",
                    "evidence_text": "信达旺大厦云平台运营及维护服务合同协议书",
                }
            ],
            "parties": [
                {
                    "name": "甲方",
                    "value": "甲方公司",
                    "image_id": "i211-1",
                    "evidence_text": "甲方：甲方公司；乙方：乙方公司",
                },
                {
                    "name": "乙方",
                    "value": "乙方公司",
                    "image_id": "i211-1",
                    "evidence_text": "甲方：甲方公司；乙方：乙方公司",
                },
            ],
            "contract_amount": [
                {
                    "value": "人民币玖拾陆万元整",
                    "image_id": "i211-3",
                    "evidence_text": "第二条 合同金额：人民币玖拾陆万元整。",
                }
            ],
            "implementation_time": [
                {
                    "value": "2025年1月1日至2026年1月1日",
                    "image_id": "i211-4",
                    "evidence_text": "服务期限：2025年1月1日至2026年1月1日。",
                }
            ],
            "service_content": [
                {
                    "value": "云平台运营及维护及日常管理",
                    "image_id": "i211-2",
                    "evidence_text": "第一条 合同服务内容：云平台运营及维护及日常管理。",
                }
            ],
        },
        "framework_contract": {
            "status": "no",
            "reason": "全文未发现框架合同及配套结算材料的可靠证据。",
            "evidence": [
                {
                    "image_id": "i211-1",
                    "evidence_text": "信达旺大厦云平台运营及维护服务合同协议书",
                }
            ],
        },
        "signature_page_candidates": [
            {
                "image_id": "i211-5",
                "reason": "OCR 同页出现甲乙方、签字盖章和日期栏信号。",
                "evidence_text": "甲方（盖章） 乙方（盖章） 法定代表人签字 年 月 日",
            }
        ],
        "signature_date_candidates": [
            {
                "image_id": "i211-5",
                "reason": "OCR 同页出现签署日期栏。",
                "evidence_text": "甲方（盖章） 乙方（盖章） 法定代表人签字 年 月 日",
            }
        ],
    }


def _document_with_ocr_distractors() -> dict:
    document = copy.deepcopy(_document())
    case = next(section for section in document["sections"] if section["section_id"] == "s211")
    for index in range(3, 8):
        block_id = f"b211-image-{index}"
        image_id = f"i211-{index}"
        order = 6 + index
        case["direct_block_ids"].append(block_id)
        document["blocks"].append(
            {
                "block_id": block_id,
                "type": "image",
                "text": "[MinerU image]",
                "order": order,
            }
        )
        document["images"].append(
            {
                "image_id": image_id,
                "block_id": block_id,
                "section_id": "s211",
                "section_path": [
                    "21 业绩情况表",
                    "21.1 信达旺大厦云平台运营及维护服务",
                ],
                "img_path": f"images/contract-{index}.png",
                "asset_status": "ready",
            }
        )
    case["end_order"] = 13
    return document


def _six_case_document(*, mismatched_amount_case: str | None = None) -> dict:
    titles = [
        "21.1 信达旺大厦云平台运营及维护服务",
        "21.2 新圩二中龙湖东路停车场充电站运营及维护合同",
        "21.3 惠阳区教育路停车场充电站运营及维护合同",
        "21.4 沙田高速路停车场充电站充电合作协议",
        "21.5 惠阳区50MW/300MWh独立新型储能电站项目",
        "21.6 惠阳区屋顶光伏资源存量资产盘活特许经营设计、施工及采购、运营总承包项目",
    ]
    clients = [f"客户{i}" for i in range(1, 7)]
    amounts = ["96", "35", "2.4", "45", "745.5", "128"]
    document = {
        "sections": [
            {
                "section_id": "s21",
                "parent_section_id": None,
                "level": 1,
                "title": "21 业绩情况表",
                "path": ["21 业绩情况表"],
                "start_order": 1,
                "end_order": 20,
                "direct_block_ids": ["b21-heading", "b21-table"],
            }
        ],
        "blocks": [
            {"block_id": "b21-heading", "type": "heading", "text": "21 业绩情况表", "order": 1},
            {"block_id": "b21-table", "type": "table", "text": "业绩表", "order": 2},
        ],
        "tables": [
            {
                "table_id": "t21",
                "block_id": "b21-table",
                "section_id": "s21",
                "rows": [
                    ["序号", "项目名称", "投标产品", "最终用户", "供货数量", "销售金额（万元）"],
                    *[
                        [
                            str(index),
                            title,
                            "运维服务",
                            client,
                            "1年期服务",
                            "999" if title == mismatched_amount_case else amount,
                        ]
                        for index, (title, client, amount) in enumerate(
                            zip(titles, clients, amounts), start=1
                        )
                    ],
                ],
            }
        ],
        "images": [],
    }
    for index, (title, client, amount) in enumerate(
        zip(titles, clients, amounts), start=1
    ):
        section_id = f"s21{index}"
        block_id = f"b21{index}-image"
        image_id = f"i21{index}"
        document["sections"].append(
            {
                "section_id": section_id,
                "parent_section_id": "s21",
                "level": 2,
                "title": title,
                "path": ["21 业绩情况表", title],
                "start_order": index * 3,
                "end_order": index * 3 + 1,
                "direct_block_ids": [block_id],
            }
        )
        document["blocks"].append(
            {"block_id": block_id, "type": "image", "text": "[MinerU image]", "order": index * 3 + 1}
        )
        document["images"].append(
            {
                "image_id": image_id,
                "block_id": block_id,
                "section_id": section_id,
                "section_path": ["21 业绩情况表", title],
                "img_path": f"images/{image_id}.png",
                "asset_status": "ready",
            }
        )
    return document


def _multi_case_text_response(
    *,
    image_id: str,
    title: str,
    client: str,
    amount: str,
    unavailable: bool = False,
) -> dict:
    if unavailable:
        return {
            "summary": "当前图片 OCR 不可用。",
            "facts": {},
            "framework_contract": {"status": "uncertain", "reason": "OCR 不可用。"},
        }
    text = (
        f"合同名称：{title}\n"
        f"甲方：{client}\n"
        f"合同金额：人民币{amount}万元\n"
        "服务期限：2025年1月1日至2026年1月1日\n"
        "合同服务内容：云平台运营及维护服务\n"
        "甲方（盖章）乙方（盖章）法人签字 年 月 日"
    )
    fact = lambda value: [{"value": value, "image_id": image_id, "evidence_text": text}]
    return {
        "summary": "识别到一份独立业绩合同。",
        "facts": {
            "project_name": fact(title),
            "parties": [
                {"name": "甲方", "value": client, "image_id": image_id, "evidence_text": text},
                {"name": "乙方", "value": "投标人", "image_id": image_id, "evidence_text": text},
            ],
            "contract_amount": fact(f"人民币{amount}万元"),
            "implementation_time": fact("2025年1月1日至2026年1月1日"),
            "service_content": fact("云平台运营及维护服务"),
        },
        "framework_contract": {
            "status": "no",
            "reason": "具体期限和金额的执行合同。",
            "evidence": [{"image_id": image_id, "evidence_text": text}],
        },
        "signature_page_candidates": [
            {"image_id": image_id, "reason": "包含双方签章和日期栏。", "evidence_text": text}
        ],
        "signature_date_candidates": [
            {"image_id": image_id, "reason": "包含签署日期栏。", "evidence_text": text}
        ],
    }


def _attach_structured_ocr(
    document: dict, texts_by_image_id: dict[str, list[str]]
) -> dict:
    document = copy.deepcopy(document)
    for image in document["images"]:
        image_id = image["image_id"]
        texts = texts_by_image_id.get(image_id, [])
        image["ocr_status"] = "available" if texts else "unavailable"
        image["ocr_source"] = "mineru_image_ocr"
        image["ocr_blocks"] = [
            {
                "block_id": f"ocr-{image_id}-{index}",
                "text": text,
                "order": index,
                "type": "paragraph",
            }
            for index, text in enumerate(texts, start=1)
        ]
        image["ocr_text"] = "\n".join(texts)
        image["ocr_text_block_count"] = len(texts)
    return document


def test_performance_review_uses_only_211_materials_and_maps_order_to_row_one():
    llm = RecordingPerformanceLLM(_response())

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": _document()},
        llm=llm,
    )

    review = result["performance_reviews"][0]
    assert review["bid_section_id"] == "s211"
    assert review["image_ids"] == ["i211-1", "i211-2"]
    assert review["status"] == "pass"
    assert review["framework_contract"]["status"] == "no"
    assert review["checks_by_key"]["order_alignment"]["status"] == "pass"
    assert review["checks_by_key"]["order_alignment"]["evidence"][0]["table_id"] == "t21"
    assert len(llm.calls) == 1
    assert [image["image_id"] for image in llm.calls[0][2]] == ["i211-1", "i211-2"]
    assert "21.2" not in llm.calls[0][1]


def test_framework_contract_adds_settlement_check_and_missing_check_is_uncertain():
    response = _response(framework_status="yes")
    response["checks"] = [check for check in response["checks"] if check["key"] != "contract_amount"]
    llm = RecordingPerformanceLLM(response)

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": _document()},
        llm=llm,
    )

    review = result["performance_reviews"][0]
    assert review["framework_contract"]["status"] == "yes"
    assert review["checks_by_key"]["contract_amount"]["status"] == "uncertain"
    assert review["checks_by_key"]["framework_settlement_material"]["status"] == "uncertain"
    assert review["status"] == "uncertain"


def test_non_framework_contract_does_not_require_settlement_material():
    llm = RecordingPerformanceLLM(_response(framework_status="no"))

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": _document()},
        llm=llm,
    )

    checks = result["performance_reviews"][0]["checks_by_key"]
    assert "framework_settlement_material" not in checks
    assert result["performance_reviews"][0]["status"] == "pass"


def test_performance_review_requires_signature_date_as_separate_check():
    response = _response(framework_status="no")
    signature_date_check = next(
        check for check in response["checks"] if check["key"] == "signature_date"
    )
    signature_date_check.update(
        {
            "status": "fail",
            "reason": "合同签署页的年、月、日栏为空。",
        }
    )
    llm = RecordingPerformanceLLM(response)

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": _document()},
        llm=llm,
    )

    review = result["performance_reviews"][0]
    assert review["checks_by_key"]["signature_date"]["status"] == "fail"
    assert review["status"] == "fail"


def test_performance_review_ocr_prefilter_sends_only_ranked_candidates():
    response = _response(framework_status="no")
    document = _attach_structured_ocr(
        _document_with_ocr_distractors(),
        {
            "i211-1": [
                "信达旺大厦云平台运营及维护合同协议书",
                "甲方：甲方公司；乙方：乙方公司",
            ],
            "i211-2": ["第一条 合同服务内容：云平台运营及维护及日常管理。"],
            "i211-3": ["第二条 合同金额：人民币玖拾陆万元整。"],
            "i211-4": ["服务期限：2025年1月1日至2026年1月1日。"],
            "i211-5": ["甲方（盖章） 乙方（盖章） 法定代表人签字 年 月 日"],
            "i211-6": ["与合同关键事实无关的其他说明。"],
            "i211-7": ["与合同关键事实无关的附件。"],
        },
    )
    llm = RecordingPerformanceLLM(response)

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": document},
        llm=llm,
    )

    review = result["performance_reviews"][0]
    assert result["stats"]["ocr_image_count"] == 7
    assert result["stats"]["ocr_available_image_count"] == 7
    assert len(review["model_image_ids"]) < len(review["image_ids"])
    assert len(llm.calls) == 1
    assert "与合同关键事实无关" not in llm.calls[0][1]
    assert review["candidate_evidence"]["contract_amount"]["text_blocks"][0][
        "image_id"
    ] == "i211-3"
    assert review["candidate_evidence"]["implementation_time"]["text_blocks"][0][
        "image_id"
    ] == "i211-4"
    assert "i211-5" in review["candidate_evidence"]["signature_page"]["image_ids"]
    assert review["ocr_facts"]["contract_amount"][0]["value"] == "人民币玖拾陆万元"


def test_performance_review_sends_full_ocr_to_text_llm_and_only_signature_candidates_to_vision():
    document = _attach_structured_ocr(
        _document_with_ocr_distractors(),
        {
            "i211-1": [
                "信达旺大厦云平台运营及维护服务合同协议书",
                "甲方：甲方公司；乙方：乙方公司",
            ],
            "i211-2": ["第一条 合同服务内容：云平台运营及维护及日常管理。"],
            "i211-3": ["第二条 合同金额：人民币玖拾陆万元整。"],
            "i211-4": ["服务期限：2025年1月1日至2026年1月1日。"],
            "i211-5": ["甲方（盖章） 乙方（盖章） 法定代表人签字 年 月 日"],
            "i211-6": ["与合同关键事实无关的其他说明。"],
            "i211-7": ["与合同关键事实无关的附件。"],
        },
    )
    text_llm = RecordingPerformanceTextLLM(_text_response())
    vision_llm = RecordingPerformanceLLM(
        {
            "signature_page": {
                "status": "pass",
                "reason": "双方签字盖章真实可见。",
                "evidence_image_ids": ["i211-5"],
            },
            "signature_date": {
                "status": "uncertain",
                "reason": "日期栏是否实际填写需结合图片判断。",
                "evidence_image_ids": ["i211-5"],
            },
        }
    )

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": document},
        text_llm=text_llm,
        llm=vision_llm,
    )

    review = result["performance_reviews"][0]
    assert len(text_llm.calls) == 1
    text_prompt = text_llm.calls[0][1]
    for image_id in (f"i211-{index}" for index in range(1, 8)):
        assert f"=== {image_id} ===" in text_prompt
    assert "与合同关键事实无关的其他说明。" in text_prompt
    assert result["stats"]["ocr_image_count"] == 7
    assert result["stats"]["ocr_character_count"] > 0
    assert result["stats"]["ocr_engine_call_count"] == 0
    assert len(vision_llm.calls) == 1
    assert [image["image_id"] for image in vision_llm.calls[0][2]] == ["i211-5"]
    assert review["model_image_ids"] == ["i211-5"]
    assert review["text_model_extraction"]["facts"]["contract_amount"][0][
        "image_id"
    ] == "i211-3"


def test_performance_review_runs_all_six_cases_independently_and_checks_table_consistency():
    titles = [
        "21.1 信达旺大厦云平台运营及维护服务",
        "21.2 新圩二中龙湖东路停车场充电站运营及维护合同",
        "21.3 惠阳区教育路停车场充电站运营及维护合同",
        "21.4 沙田高速路停车场充电站充电合作协议",
        "21.5 惠阳区50MW/300MWh独立新型储能电站项目",
        "21.6 惠阳区屋顶光伏资源存量资产盘活特许经营设计、施工及采购、运营总承包项目",
    ]
    clients = [f"客户{i}" for i in range(1, 7)]
    amounts = ["96", "35", "2.4", "45", "745.5", "128"]
    document = _six_case_document(mismatched_amount_case=titles[1])
    document = _attach_structured_ocr(
        document,
        {
            f"i21{index}": [
                f"合同名称：{title}",
                f"甲方：{client}",
                f"合同金额：人民币{amount}万元",
                "服务期限：2025年1月1日至2026年1月1日",
                "合同服务内容：云平台运营及维护服务",
                "甲方（盖章）乙方（盖章）法人签字 年 月 日",
            ]
            for index, (title, client, amount) in enumerate(
                zip(titles, clients, amounts), start=1
            )
            if index != 3
        },
    )
    responses = {
        f"i21{index}": _multi_case_text_response(
            image_id=f"i21{index}",
            title=title,
            client=client,
            amount=amount,
            unavailable=index == 3,
        )
        for index, (title, client, amount) in enumerate(
            zip(titles, clients, amounts), start=1
        )
    }
    text_llm = RecordingMultiPerformanceTextLLM(responses)
    vision_llm = RecordingMultiPerformanceVisionLLM()

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": document},
        text_llm=text_llm,
        llm=vision_llm,
    )

    reviews = result["performance_reviews"]
    assert len(reviews) == 6
    assert result["stats"]["image_count"] == 6
    assert result["stats"]["ocr_available_image_count"] == 5
    assert result["stats"]["ocr_failed_count"] == 1
    assert result["stats"]["ocr_engine_call_count"] == 0
    assert result["stats"]["text_llm_total_calls"] == 6
    assert result["stats"]["visual_llm_total_calls"] == 6
    assert result["stats"]["ocr_token_count_source"] == "conservative_estimate_no_tokenizer"
    expected_token_count = sum(
        sum(not character.isspace() for character in review["ocr_scan"]["full_text"])
        for review in reviews
    )
    assert result["stats"]["ocr_token_count"] == expected_token_count
    assert len(text_llm.calls) == 6
    assert len(vision_llm.calls) == 6
    prompts_by_title = {
        next(title for title in titles if title in prompt): prompt
        for _system_prompt, prompt in text_llm.calls
    }
    for title in titles:
        prompt = prompts_by_title[title]
        assert title in prompt
        other_titles = [other for other in titles if other != title]
        assert all(other not in prompt for other in other_titles)

    review_by_title = {review["bid_module_name"]: review for review in reviews}
    assert (
        review_by_title[titles[1]]["table_consistency"]["contract_amount"]["status"]
        == "fail"
    )
    assert review_by_title[titles[1]]["status"] == "fail"
    unavailable_review = review_by_title[titles[2]]
    assert unavailable_review["ocr_scan"]["ocr_unavailable_image_ids"] == ["i213"]
    assert unavailable_review["model_image_ids"] == ["i213"]
    assert unavailable_review["ocr_scan"]["image_ranges"][0]["status"] == "unavailable"


def test_performance_cases_run_in_parallel_but_each_case_keeps_text_then_vision_order():
    titles = [
        "21.1 信达旺大厦云平台运营及维护服务",
        "21.2 新圩二中龙湖东路停车场充电站运营及维护合同",
        "21.3 惠阳区教育路停车场充电站运营及维护合同",
        "21.4 沙田高速路停车场充电站充电合作协议",
        "21.5 惠阳区50MW/300MWh独立新型储能电站项目",
        "21.6 惠阳区屋顶光伏资源存量资产盘活特许经营设计、施工及采购、运营总承包项目",
    ]
    clients = [f"客户{i}" for i in range(1, 7)]
    amounts = ["96", "35", "2.4", "45", "745.5", "128"]
    document = _attach_structured_ocr(
        _six_case_document(),
        {
            f"i21{index}": [
                f"合同名称：{title}",
                f"甲方：{client}",
                f"合同金额：人民币{amount}万元",
                "服务期限：2025年1月1日至2026年1月1日",
                "合同服务内容：云平台运营及维护服务",
                "甲方（盖章）乙方（盖章）法人签字 年 月 日",
            ]
            for index, (title, client, amount) in enumerate(
                zip(titles, clients, amounts), start=1
            )
        },
    )
    responses = {
        f"i21{index}": _multi_case_text_response(
            image_id=f"i21{index}",
            title=title,
            client=client,
            amount=amount,
        )
        for index, (title, client, amount) in enumerate(
            zip(titles, clients, amounts), start=1
        )
    }
    tracker = ConcurrencyTracker()
    text_llm = ConcurrentMultiPerformanceTextLLM(responses, tracker)
    vision_llm = ConcurrentMultiPerformanceVisionLLM(tracker)

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": document},
        text_llm=text_llm,
        llm=vision_llm,
    )

    assert tracker.max_active >= 2
    assert tracker.max_active <= 5
    assert all(events == ["text", "visual"] for events in tracker.events.values())
    assert result["stats"]["case_concurrency_limit"] == 5
    assert result["stats"]["case_concurrency_workers"] == 5
    assert [review["bid_module_name"] for review in result["performance_reviews"]] == titles


def test_performance_review_reuses_structured_document_ocr_without_mineru_call():
    document = _attach_structured_ocr(
        _document(),
        {
            "i211-1": ["合同服务内容：云平台运营及维护。"],
            "i211-2": ["合同金额：人民币96万元。"],
        },
    )
    llm = RecordingPerformanceLLM(_response())

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": document},
        llm=llm,
    )

    assert result["stats"]["ocr_engine_call_count"] == 0
    assert result["stats"]["ocr_processed_image_count"] == 0
    assert result["stats"]["ocr_available_image_count"] == 2
    assert result["stats"]["ocr_precomputed_image_count"] == 2
    assert all(
        item["source"] == "structured_document"
        for item in result["performance_reviews"][0]["ocr_scan"]["image_results"]
        if item["status"] == "available"
    )


def test_signature_candidate_aggregates_split_ocr_signals_on_one_page():
    images = [{"image_id": f"i211-{index}"} for index in range(1, 8)]
    ocr_entries = [
        {
            "image_id": "i211-6",
            "source": "test_mineru_ocr",
            "blocks": [
                {
                    "block_id": "ocr-distractor",
                    "text": "甲方乙方 法定代表人 授权代表 签字盖章 合同签署页",
                }
            ],
        },
        {
            "image_id": "i211-7",
            "source": "test_mineru_ocr",
            "blocks": [
                {"block_id": "ocr-party-a-label", "text": "甲方："},
                {"block_id": "ocr-party-a-name", "text": "甲方公司"},
                {"block_id": "ocr-party-a-sign", "text": "法人或授权代表：张三"},
                {"block_id": "ocr-party-a-stamp", "text": "（盖章）"},
                {"block_id": "ocr-party-a-date", "text": "年 月 日"},
                {"block_id": "ocr-party-b-label", "text": "乙方："},
                {"block_id": "ocr-party-b-name", "text": "乙方公司"},
                {"block_id": "ocr-party-b-sign", "text": "法人或授权代表：李四"},
                {"block_id": "ocr-party-b-stamp", "text": "（盖章）"},
                {"block_id": "ocr-party-b-date", "text": "年 月 日"},
            ],
        },
    ]

    evidence, _selected_images = _select_performance_candidates(
        images,
        ocr_entries,
        case_title=PERFORMANCE_CASE_TITLE,
    )

    assert "i211-7" in evidence["signature_page"]["image_ids"]
    assert "i211-7" in evidence["signature_date"]["image_ids"]


def test_signature_candidate_keeps_high_scoring_page_when_its_blocks_are_split():
    images = [{"image_id": f"i211-{index}"} for index in range(1, 8)]
    ocr_entries = [
        {
            "image_id": image_id,
            "source": "test_mineru_ocr",
            "blocks": [
                {
                    "block_id": f"ocr-{image_id}",
                    "text": (
                        "甲方 乙方 法定代表人 授权代表 签字盖章"
                        if image_id in {"i211-4", "i211-5", "i211-6"}
                        else ""
                    ),
                }
            ],
        }
        for image_id in ("i211-4", "i211-5", "i211-6")
    ]
    ocr_entries.append(
        {
            "image_id": "i211-7",
            "source": "test_mineru_ocr",
            "blocks": [
                {"block_id": "ocr-label-a", "text": "甲方："},
                {"block_id": "ocr-label-b", "text": "乙方："},
                {"block_id": "ocr-sign-a", "text": "法人或授权代表：张三"},
                {"block_id": "ocr-stamp-a", "text": "（盖章）"},
                {"block_id": "ocr-date-a", "text": "年 月 日"},
            ],
        }
    )

    evidence, selected_images = _select_performance_candidates(
        images,
        ocr_entries,
        case_title=PERFORMANCE_CASE_TITLE,
    )

    selected_ids = {image["image_id"] for image in selected_images}
    assert "i211-7" in selected_ids
    assert "i211-7" in evidence["signature_page"]["image_ids"]
    assert "i211-7" in evidence["signature_date"]["image_ids"]


def test_performance_review_rejects_evidence_from_sibling_case():
    response = _response()
    response["checks"][0]["evidence_image_ids"] = ["i212-1"]
    llm = RecordingPerformanceLLM(response)

    result = run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": _document()},
        llm=llm,
    )

    review = result["performance_reviews"][0]
    assert review["execution_status"] == "failed"
    assert review["status"] == "uncertain"
    assert "不属于 21.1" in review["error_message"]


def test_performance_review_writes_traceable_artifact_and_event(tmp_path: Path):
    task_dir = tmp_path / "task"
    recorder = ComplianceExtractionRecorder(task_dir)

    run_performance_contract_review(
        {"templates": [_template()]},
        {"structured_document": _document(), "artifact_dir": str(tmp_path / "bid")},
        llm=RecordingPerformanceLLM(_response()),
        recorder=recorder,
    )

    artifact_path = task_dir / "compliance_extraction" / "10_performance_reviews.json"
    assert artifact_path.is_file()
    saved = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert saved["performance_reviews"][0]["bid_section_id"] == "s211"
    events = [
        json.loads(line)
        for line in recorder.execution_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "performance.review.end" in [event["event"] for event in events]


def test_composed_compliance_review_includes_performance_review_without_other_21_cases():
    llm = RecordingPerformanceLLM(_response())

    result = run_compliance_review_with_attachments(
        {"templates": [_template()]},
        {"structured_document": _document()},
        template_review_llm=DeterministicTemplateTextReviewLLM(),
        attachment_review_llm=llm,
    )

    assert result["performance_reviews"][0]["bid_section_id"] == "s211"
    assert result["performance_stats"]["selected_case_count"] == 1
    assert result["mode"] == "template_text_and_performance"
    assert len(llm.calls) == 1
