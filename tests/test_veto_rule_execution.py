from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata
from app.veto_rule_execution import run_veto_rule_execution


def make_rule(
    rule_id: str,
    name: str,
    trigger: str,
    *,
    original: str | None = None,
    section: str = "初步评审",
) -> dict[str, Any]:
    source_text = original or trigger
    return {
        "id": rule_id,
        "name": name,
        "trigger_condition": trigger,
        "consequence": "否决其投标",
        "additional_consequence": None,
        "evidence_requirements": [],
        "original_rule": original or f"{trigger}，否决其投标。",
        "source": {
            "section": section,
            "block_ids": ["tender-b1"],
            "source_text": source_text,
        },
    }


def make_rules(
    *,
    veto_rules: list[dict[str, Any]],
    uncertain_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "source_sections": [],
        "score_categories": [],
        "score_items": [],
        "veto_rules": veto_rules,
        "uncertain_rules": uncertain_rules or [],
        "stats": {"veto_rule_count": len(veto_rules)},
    }


VETO_009_CONDITIONS = [
    "第二章“投标人须知”第1.8款规定的任何一种情形的",
    "不按照评标委员会要求澄清、说明或者补正",
    "投标文件未经投标单位盖章和单位负责人签字",
    "允许联合体投标的，投标联合体没有递交共同投标协议",
    "投标人不符合国家或者招标文件规定的资格条件",
    "同一投标人递交两个以上不同的投标文件或者投标报价，但招标文件要求递交备选投标的除外",
    "投标报价低于成本或者高于招标文件设定的最高投标限价",
    "投标文件没有对招标文件的实质性要求和条件做出响应",
    "投标人有串通投标、弄虚作假、行贿等违法行为",
    "投标人以他人名义投标",
    "没有按照招标文件要求提供投标担保或者所提供的投标担保有瑕疵",
    "投标文件载明的招标项目完成期限超过招标文件规定的期限",
    "明显不符合技术规格、技术标准的要求",
    "投标文件载明的货物包装方式、检验标准和方法等不符合招标文件的要求",
    "投标文件附有招标人不能接受的条件",
    "不符合招标文件中规定的其他实质性要求",
]


def veto_009_rule() -> dict[str, Any]:
    original = "3.1.2投标人有以下情形之一的，评标委员会应当否决其投标："
    original += "".join(
        f"（{index}）{condition}{'；' if index < 16 else '。'}"
        for index, condition in enumerate(VETO_009_CONDITIONS, start=1)
    )
    return make_rule(
        "veto_009",
        "否决投标情形（通用）",
        "投标人存在以下任一情形",
        original=original,
    )


def bid_file(tmp_path: Path, *, name: str = "bid.docx") -> FileMetadata:
    path = tmp_path / name
    if not path.exists():
        path.write_bytes(b"bid")
    return FileMetadata(name, path.stat().st_size, str(path))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_structured_document(path: Path, source_path: Path) -> None:
    write_json(
        path,
        {
            "source": {
                "filename": source_path.name,
                "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            },
            "blocks": [],
            "tables": [],
            "images": [],
            "sections": [],
            "stats": {},
        },
    )


def business_only_document() -> dict[str, Any]:
    return {
        "source": {},
        "sections": [
            {
                "section_id": "s1",
                "title": "商务投标文件",
                "path": ["商务投标文件"],
            }
        ],
        "blocks": [
            {"block_id": "bid-b1", "text": "商务响应内容"},
        ],
        "tables": [],
        "images": [],
    }


def document_with_text(text: str) -> dict[str, Any]:
    return {
        "source": {},
        "sections": [
            {
                "section_id": "s1",
                "title": "投标文件",
                "path": ["投标文件"],
            }
        ],
        "blocks": [{"block_id": "bid-b1", "text": text}],
        "tables": [],
        "images": [],
    }


def document_with_bid_image() -> dict[str, Any]:
    document = document_with_text("营业执照扫描件")
    document["blocks"] = [
        {"block_id": "bid-b1", "text": "营业执照扫描件"},
    ]
    document["images"] = [
        {
            "image_id": "bid-img-1",
            "block_id": "bid-b1",
            "img_path": "/tmp/bid-img-1.png",
        }
    ]
    return document


def test_veto_execution_keeps_each_formal_rule_and_excludes_uncertain_rules(tmp_path):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "初步评审不通过",
                    "有一项正式评审项不符合即否决",
                )
            ],
            uncertain_rules=[
                {"id": "uncertain_001", "description": "未结构化"}
            ],
        ),
        bid_file(tmp_path),
    )

    assert [item["id"] for item in result["veto_rule_reviews"]] == ["veto_001"]
    assert result["veto_rule_reviews"][0]["status"] == "evidence_insufficient"
    assert result["veto_rule_reviews"][0]["triggered"] is False
    assert result["stats"]["formal_rule_count"] == 1


def test_ordinary_fail_is_not_automatically_a_veto(tmp_path):
    artifacts = {
        "08_template_text_reviews.json": {
            "template_text_reviews": [
                {
                    "status": "fail",
                    "issues": [
                        {"type": "missing_fill", "reason": "普通模板字段未填写"}
                    ],
                }
            ]
        }
    }
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "初步评审不通过",
                    "有一项正式评审项不符合即否决",
                )
            ]
        ),
        bid_file(tmp_path),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] != "triggered"
    assert "普通" in review["reason"] or "正式" in review["reason"]


def test_veto_execution_reuses_hash_verified_artifacts_and_writes_independent_json(
    tmp_path,
):
    bid = bid_file(tmp_path)
    cleaning_dir = tmp_path / "bid_document_cleaning"
    cleaning_dir.mkdir()
    write_structured_document(
        cleaning_dir / "structured_document.json",
        Path(bid.storage_path),
    )
    write_json(
        tmp_path / "compliance_extraction" / "09_attachment_reviews.json",
        {"attachment_reviews": [], "stats": {}},
    )
    recorder = ComplianceExtractionRecorder(tmp_path)

    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[make_rule("veto_001", "材料缺失", "未提供营业执照")]
        ),
        bid,
        recorder=recorder,
    )

    assert result["source"]["bid_document_hash_verified"] is True
    assert "09_attachment_reviews.json" in result["source"]["reused_artifacts"]
    assert result["source"]["evaluation_rules_artifact"] == "11_evaluation_rules.json"
    assert result["stats"]["llm_total_calls"] == 0
    assert (
        tmp_path / "compliance_extraction" / "veto_rule_reviews.json"
    ).is_file()
    review = result["veto_rule_reviews"][0]
    assert review["tender_rule_source"]["block_ids"] == ["tender-b1"]
    assert "related_artifacts" in review


def test_veto_execution_preserves_compiled_rule_source_fields_verbatim(tmp_path):
    rule = make_rule(
        "veto_compiled",
        "编译规则",
        "编译触发条件",
        original="编译原文",
    )
    rule["additional_consequence"] = "编译阶段附加后果"
    rule["evidence_requirements"] = ["编译阶段证据要求"]
    rule["source"] = {
        "section": "3.1初步评审",
        "block_ids": ["tender-b9"],
        "source_text": "3.1.3编译原文",
        "page": 42,
    }

    review = run_veto_rule_execution(
        make_rules(veto_rules=[rule]),
        bid_file(tmp_path),
    )["veto_rule_reviews"][0]

    assert review["id"] == rule["id"]
    assert review["name"] == rule["name"]
    assert review["original_rule"] == rule["original_rule"]
    assert review["trigger_condition"] == rule["trigger_condition"]
    assert review["consequence"] == rule["consequence"]
    assert review["additional_consequence"] == rule["additional_consequence"]
    assert review["evidence_requirements"] == rule["evidence_requirements"]
    assert review["tender_rule_source"] == rule["source"]


def test_business_only_file_does_not_trigger_star_rule(tmp_path):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "★技术条款",
                    "任一★技术条款不满足即否决",
                )
            ]
        ),
        bid_file(tmp_path, name="商务投标文件.docx"),
        bid_document=business_only_document(),
    )

    assert result["veto_rule_reviews"][0]["status"] == "file_scope_missing"


def test_rule_applicability_precedes_bid_scope_for_project_specific_exclusions(tmp_path):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_limit",
                    "超过最高投标限价",
                    "投标报价超过最高投标限价",
                ),
                make_rule(
                    "veto_bond",
                    "未递交投标保证金",
                    "未递交投标保证金或者保证金有瑕疵",
                ),
            ]
        ),
        bid_file(tmp_path),
        tender_evidence={
            "artifact_path": "/tmp/tender/01_parsed_blocks.json",
            "blocks": [
                {
                    "block_id": "tender-limit",
                    "section": "投标人须知前附表",
                    "text": "3.3.3最高投标限价或者其计算方法：不设置最高投标限价",
                },
                {
                    "block_id": "tender-bond",
                    "section": "投标人须知前附表",
                    "text": "3.5.1投标保证金：无需递交投标保证金",
                },
            ],
        },
    )

    limit, bond = result["veto_rule_reviews"]
    assert limit["status"] == "not_applicable"
    assert bond["status"] == "not_applicable"
    assert limit["applicability"]["status"] == "not_applicable"
    assert bond["applicability"]["status"] == "not_applicable"
    assert limit["applicability"]["evidence"][0]["block_ids"] == ["tender-limit"]
    assert bond["applicability"]["evidence"][0]["block_ids"] == ["tender-bond"]
    assert limit["triggered"] is False
    assert bond["triggered"] is False


def test_conditional_rule_without_tender_applicability_fact_is_uncertain(tmp_path):
    review = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_limit",
                    "超过最高投标限价",
                    "投标报价超过最高投标限价",
                )
            ]
        ),
        bid_file(tmp_path),
    )["veto_rule_reviews"][0]

    assert review["status"] == "evidence_insufficient"
    assert review["applicability"]["status"] == "applicability_uncertain"


def test_veto_009_executes_all_sixteen_subconditions_and_aggregates_dependencies(
    tmp_path,
):
    result = run_veto_rule_execution(
        make_rules(veto_rules=[veto_009_rule()]),
        bid_file(tmp_path, name="商务投标文件.docx"),
        bid_document=business_only_document(),
        tender_evidence={
            "artifact_path": "/tmp/tender/01_parsed_blocks.json",
            "blocks": [
                {
                    "block_id": "tender-facts",
                    "section": "投标人须知前附表",
                    "text": (
                        "本次招标不接受联合体投标；本项目不设置最高投标限价；"
                        "本项目无需递交投标保证金；本项目为服务项目，不涉及货物包装、"
                        "检验标准和方法。"
                    ),
                }
            ],
        },
    )

    review = result["veto_rule_reviews"][0]
    subconditions = review["sub_conditions"]
    by_index = {item["index"]: item for item in subconditions}

    assert len(subconditions) == 16
    assert [item["index"] for item in subconditions] == list(range(1, 17))
    assert [item["original_condition"] for item in subconditions] == VETO_009_CONDITIONS
    for item in subconditions:
        assert {
            "index",
            "condition",
            "original_condition",
            "status",
            "triggered",
            "facts_required",
            "confirmed_facts",
            "reason",
            "evidence",
            "related_artifacts",
            "dependencies",
        }.issubset(item)

    assert by_index[1]["status"] == "external_data_required"
    assert by_index[2]["status"] == "manual_review_required"
    assert by_index[4]["status"] == "not_applicable"
    assert by_index[6]["status"] == "other_bidder_data_required"
    assert by_index[7]["status"] == "manual_review_required"
    assert {branch["status"] for branch in by_index[7]["branches"]} == {
        "not_applicable",
        "manual_review_required",
    }
    assert by_index[8]["status"] == "file_scope_missing"
    assert by_index[9]["status"] == "evidence_insufficient"
    assert {branch["status"] for branch in by_index[9]["branches"]} == {
        "other_bidder_data_required",
        "evidence_insufficient",
        "external_data_required",
    }
    assert by_index[11]["status"] == "not_applicable"
    assert by_index[13]["status"] == "file_scope_missing"
    assert by_index[14]["status"] == "not_applicable"

    assert review["status"] == "evidence_insufficient"
    assert review["triggered"] is False
    assert review["subcondition_summary"]["total"] == 16
    assert review["subcondition_summary"]["blocking_subcondition_indices"]
    assert review["dependencies"]["external_data_required"] is True
    assert review["dependencies"]["other_bidder_data_required"] is True
    assert review["dependencies"]["manual_review_required"] is True
    assert result["stats"]["subcondition_count"] == 16
    assert result["stats"]["dependency_counts"]["external_data_required"] >= 2
    assert result["stats"]["dependency_counts"]["other_bidder_data_required"] >= 2
    assert result["stats"]["dependency_counts"]["manual_review_required"] >= 2


def test_veto_009_is_triggered_only_when_a_subcondition_has_auditable_failure(
    tmp_path,
):
    artifacts = {
        "09_attachment_reviews.json": {
            "attachment_reviews": [
                {
                    "semantic_match": {"status": "matched"},
                    "status": "fail",
                    "execution_status": "completed",
                    "requirements": [
                        {
                            "requirement": "投标文件未经投标单位盖章和单位负责人签字",
                            "status": "fail",
                            "reason": "投标文件缺少单位负责人签字",
                            "evidence_image_ids": ["bid-img-1"],
                        }
                    ],
                }
            ],
            "stats": {},
        }
    }
    result = run_veto_rule_execution(
        make_rules(veto_rules=[veto_009_rule()]),
        bid_file(tmp_path),
        bid_document=document_with_bid_image(),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    subcondition = next(item for item in review["sub_conditions"] if item["index"] == 3)
    assert subcondition["status"] == "triggered"
    assert subcondition["triggered"] is True
    assert review["status"] == "triggered"
    assert review["triggered"] is True
    assert review["triggered_by"] == ["veto_009_03"]
    assert review["bid_evidence"]["image_ids"] == ["bid-img-1"]


def test_veto_009_does_not_promote_unmatched_material_failure_to_subcondition_trigger(
    tmp_path,
):
    artifacts = {
        "09_attachment_reviews.json": {
            "attachment_reviews": [
                {
                    "status": "fail",
                    "execution_status": "completed",
                    "requirements": [
                        {
                            "requirement": "投标文件未经投标单位盖章和单位负责人签字",
                            "status": "fail",
                            "reason": "普通附件检查未完成正式评审项匹配",
                            "evidence_image_ids": ["bid-img-1"],
                        }
                    ],
                }
            ],
            "stats": {},
        }
    }
    result = run_veto_rule_execution(
        make_rules(veto_rules=[veto_009_rule()]),
        bid_file(tmp_path),
        bid_document=document_with_bid_image(),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    subcondition = next(item for item in review["sub_conditions"] if item["index"] == 3)
    assert subcondition["status"] != "triggered"
    assert review["status"] != "triggered"


def test_low_price_requires_evaluation_process_and_is_not_auto_triggered(tmp_path):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "低于成本报价",
                    "投标报价可能低于成本且不能合理说明的，否决投标",
                )
            ]
        ),
        bid_file(tmp_path),
        bid_document=document_with_text("投标报价为最低价"),
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] == "manual_review_required"
    assert review["triggered"] is False


def test_collusion_requires_other_bidder_data(tmp_path):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "串通投标",
                    "投标文件异常一致的，否决投标",
                )
            ]
        ),
        bid_file(tmp_path),
    )

    assert result["veto_rule_reviews"][0]["status"] == "other_bidder_data_required"


def test_external_supplier_record_requires_external_data(tmp_path):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "不良行为",
                    "存在供应商不良行为记录的，否决投标",
                )
            ]
        ),
        bid_file(tmp_path),
    )

    assert result["veto_rule_reviews"][0]["status"] == "external_data_required"


def test_inconsistent_performance_amount_is_not_fraud_veto(tmp_path):
    artifacts = {
        "10_performance_reviews.json": {
            "performance_reviews": [
                {
                    "status": "fail",
                    "checks_by_key": {
                        "table_amount_consistency": {
                            "status": "fail",
                            "reason": "业绩表金额与合同金额不一致",
                        }
                    },
                }
            ],
            "stats": {},
        }
    }
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "弄虚作假",
                    "提供虚假业绩材料的，否决投标",
                )
            ]
        ),
        bid_file(tmp_path),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] in {"evidence_insufficient", "manual_review_required"}
    assert review["triggered"] is False


def test_rule_classifier_does_not_use_unrelated_neighbor_text_from_source_block(
    tmp_path,
):
    rule = make_rule(
        "veto_001",
        "低于成本价的情况",
        "投标人不能合理说明低于成本价的，否决投标",
    )
    rule["source"]["source_text"] = "★技术条款不满足的，均将被否决。"

    result = run_veto_rule_execution(
        make_rules(veto_rules=[rule]),
        bid_file(tmp_path),
    )

    assert result["veto_rule_reviews"][0]["status"] == "manual_review_required"


def test_generic_aggregate_with_fraud_word_is_not_treated_as_direct_fraud_finding(
    tmp_path,
):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "否决投标情形（通用）",
                    "投标人存在以下任一情形：投标人有串通投标、弄虚作假等违法行为",
                )
            ]
        ),
        bid_file(tmp_path),
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] == "evidence_insufficient"
    assert "汇总规则" in review["reason"]


def test_non_substantive_threshold_counts_only_explicit_failures(tmp_path):
    artifacts = {
        "08_template_text_reviews.json": {
            "template_text_reviews": [
                {
                    "status": "fail",
                    "issues": [
                        {
                            "type": "non_substantive_deviation",
                            "status": "fail",
                            "reason": "非实质性条款第1项不满足",
                        }
                    ],
                },
                {
                    "status": "uncertain",
                    "issues": [
                        {
                            "type": "non_substantive_deviation",
                            "status": "uncertain",
                            "reason": "第2项待确认",
                        }
                    ],
                },
            ]
        }
    }
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "非实质性条款阈值",
                    "非实质性条款超过10项不满足的，视为实质性不满足",
                )
            ]
        ),
        bid_file(tmp_path),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] != "triggered"
    assert review["confirmed_facts"][0]["counted_failure_count"] == 1


def test_non_substantive_threshold_triggers_only_after_complete_coverage(tmp_path):
    artifacts = {
        "08_template_text_reviews.json": {
            "stats": {"coverage_complete": True},
            "template_text_reviews": [
                {
                    "status": "fail",
                    "issues": [
                        {
                            "type": "non_substantive_deviation",
                            "status": "fail",
                            "reason": "非实质性条款第1项不满足",
                            "evidence_image_ids": ["bid-img-1"],
                        },
                        {
                            "type": "non_substantive_deviation",
                            "status": "fail",
                            "reason": "非实质性条款第2项不满足",
                            "evidence_image_ids": ["bid-img-1"],
                        },
                    ],
                }
            ],
        }
    }
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "非实质性条款阈值",
                    "非实质性条款超过1项不满足的，视为实质性不满足",
                )
            ]
        ),
        bid_file(tmp_path),
        bid_document=document_with_bid_image(),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] == "triggered"
    assert review["confirmed_facts"][0]["coverage_complete"] is True
    assert review["confirmed_facts"][0]["counted_failure_count"] == 2


def test_matching_attachment_failure_triggers_with_auditable_evidence(tmp_path):
    artifacts = {
        "09_attachment_reviews.json": {
            "attachment_reviews": [
                {
                    "semantic_match": {"status": "matched"},
                    "status": "fail",
                    "business_status": "fail",
                    "execution_status": "completed",
                    "image_ids": ["bid-img-1"],
                    "requirements": [
                        {
                            "requirement": "投标文件须提供营业执照",
                            "status": "fail",
                            "reason": "未提供营业执照",
                            "evidence_image_ids": ["bid-img-1"],
                        }
                    ],
                }
            ],
            "stats": {},
        }
    }
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "营业执照缺失",
                    "未提供营业执照的，否决其投标",
                )
            ]
        ),
        bid_file(tmp_path),
        bid_document=document_with_bid_image(),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] == "triggered"
    assert review["triggered"] is True
    assert review["bid_evidence"]["image_ids"] == ["bid-img-1"]
    assert review["related_artifacts"] == ["09_attachment_reviews.json"]
    assert review["confirmed_facts"]
    assert review["tender_rule_source"]["block_ids"] == ["tender-b1"]


def test_matching_attachment_pass_is_not_triggered_only_with_complete_evidence(tmp_path):
    artifacts = {
        "09_attachment_reviews.json": {
            "attachment_reviews": [
                {
                    "semantic_match": {"status": "matched"},
                    "status": "pass",
                    "business_status": "pass",
                    "execution_status": "completed",
                    "requirements": [
                        {
                            "requirement": "投标文件须提供营业执照",
                            "status": "pass",
                            "reason": "已提供营业执照",
                            "evidence_image_ids": ["bid-img-1"],
                        }
                    ],
                }
            ],
            "stats": {},
        }
    }
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[
                make_rule(
                    "veto_001",
                    "营业执照缺失",
                    "未提供营业执照的，否决其投标",
                )
            ]
        ),
        bid_file(tmp_path),
        bid_document=document_with_bid_image(),
        existing_artifacts=artifacts,
    )

    assert result["veto_rule_reviews"][0]["status"] == "not_triggered"
    assert result["veto_rule_reviews"][0]["triggered"] is False


def test_preliminary_aggregate_links_to_a_triggered_child_without_duplicate_cause(
    tmp_path,
):
    artifacts = {
        "09_attachment_reviews.json": {
            "attachment_reviews": [
                {
                    "semantic_match": {"status": "matched"},
                    "status": "fail",
                    "business_status": "fail",
                    "execution_status": "completed",
                    "requirements": [
                        {
                            "requirement": "投标文件须提供营业执照",
                            "status": "fail",
                            "reason": "未提供营业执照",
                            "evidence_image_ids": ["bid-img-1"],
                        }
                    ],
                }
            ],
            "stats": {},
        }
    }
    rules = make_rules(
        veto_rules=[
            make_rule(
                "veto_001",
                "资格材料缺失",
                "未提供营业执照的，否决其投标",
                section="资格审查",
            ),
            make_rule(
                "veto_002",
                "初步评审不通过",
                "初步评审中有一项不符合评审标准的，否决其投标",
                section="初步评审",
            ),
        ]
    )
    result = run_veto_rule_execution(
        rules,
        bid_file(tmp_path),
        bid_document=document_with_bid_image(),
        existing_artifacts=artifacts,
    )

    child, parent = result["veto_rule_reviews"]
    assert child["status"] == "triggered"
    assert parent["status"] == "triggered"
    assert parent["triggered_by"] == ["veto_001"]
    assert child["parent_rule_ids"] == ["veto_002"]


def test_parent_relations_use_semantic_rule_anchors_and_skip_delivery_parent(tmp_path):
    rules = make_rules(
        veto_rules=[
            make_rule(
                "veto_006",
                "逾期送达或未按要求密封",
                "出现下列情形之一：逾期送达或者未按要求密封，不予接收投标文件",
                original="4.1.5出现下列情形之一时不予接收投标文件：逾期送达或者未按要求密封。",
            ),
            make_rule(
                "veto_009",
                "否决投标情形（通用）",
                "投标人有以下情形之一的，评标委员会应当否决其投标",
                original=(
                    "3.1.2投标人有以下情形之一的，评标委员会应当否决其投标："
                    "投标报价高于最高投标限价；没有按照要求提供投标担保；"
                    "投标报价低于成本。"
                ),
            ),
            make_rule(
                "veto_limit",
                "超过最高投标限价",
                "投标报价超过最高投标限价",
            ),
            make_rule(
                "veto_bond",
                "未递交投标保证金",
                "没有按照招标文件要求提供投标担保或者担保有瑕疵",
            ),
            make_rule(
                "veto_cost",
                "低于成本价投标否决",
                "投标报价低于成本且不能合理说明",
            ),
            make_rule(
                "veto_validity",
                "投标有效期不满足要求",
                "投标有效期不满足招标文件要求",
            ),
        ]
    )

    reviews = run_veto_rule_execution(rules, bid_file(tmp_path))["veto_rule_reviews"]
    by_id = {item["id"]: item for item in reviews}

    assert by_id["veto_limit"]["parent_rule_ids"] == ["veto_009"]
    assert by_id["veto_bond"]["parent_rule_ids"] == ["veto_009"]
    assert by_id["veto_cost"]["parent_rule_ids"] == ["veto_009"]
    assert by_id["veto_validity"]["parent_rule_ids"] == []
    assert by_id["veto_006"]["parent_rule_ids"] == []
