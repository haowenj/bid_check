from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.models import FileMetadata
from app.objective_scoring import run_objective_scoring


def _source(block_id: str = "b0277") -> dict[str, Any]:
    return {
        "section": "评标办法",
        "block_ids": [block_id],
        "source_text": "原始评标规则",
    }


def _item(
    item_id: str,
    *,
    name: str,
    evaluation_type: str,
    full_score: float | None = 5,
    category_id: str = "category-1",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "category_id": category_id,
        "parent_item_id": None,
        "original_rule": f"{name}评分规则",
        "conditions": {},
        "scoring_method": {},
        "full_score": full_score,
        "evidence_requirements": [],
        "evaluation_type": evaluation_type,
        "source": _source(),
    }


def make_rules_with_objective_subjective_and_veto() -> dict[str, Any]:
    return {
        "source_sections": [],
        "score_categories": [
            {
                "id": "category-1",
                "name": "商务部分",
                "parent_id": None,
                "full_score": 5,
                "original_rule": "商务部分",
                "conditions": {},
                "structure_status": "clear",
                "source": _source(),
            }
        ],
        "score_items": [
            _item(
                "objective-1",
                name="技术标准和要求的偏离情况",
                evaluation_type="objective",
            ),
            _item(
                "subjective-1",
                name="技术方案",
                evaluation_type="subjective",
                category_id="category-1",
            ),
        ],
        "veto_rules": [
            {
                "id": "veto-1",
                "name": "资格不通过",
                "trigger_condition": "缺少资格材料",
                "consequence": "否决投标",
                "additional_consequence": None,
                "evidence_requirements": [],
                "original_rule": "缺少资格材料的，否决投标。",
                "source": _source("b0300"),
            }
        ],
        "uncertain_rules": [],
        "stats": {},
    }


def make_team_count_rule(*, full_score: float = 5) -> dict[str, Any]:
    return {
        "source_sections": [],
        "score_categories": [
            {
                "id": "category-1",
                "name": "商务部分",
                "parent_id": None,
                "full_score": full_score,
                "original_rule": "商务部分",
                "conditions": {},
                "structure_status": "clear",
                "source": _source(),
            }
        ],
        "score_items": [
            {
                **_item(
                    "score_item_008",
                    name="团队成员情况",
                    evaluation_type="objective",
                    full_score=full_score,
                ),
                "original_rule": (
                    "团队成员（不含项目经理）达到35人及以上得5分，25-34人得3分，"
                    "15-24人得1分，14人及以下得0分。"
                ),
                "conditions": {
                    "member_count_brackets": [
                        {"min": 35, "max": None, "score": 5},
                        {"min": 25, "max": 34, "score": 3},
                        {"min": 15, "max": 24, "score": 1},
                        {"min": None, "max": 14, "score": 0},
                    ]
                },
                "evidence_requirements": ["身份证", "社保", "缴费单位"],
            }
        ],
        "veto_rules": [],
        "uncertain_rules": [],
        "stats": {},
    }


def _single_item_rules(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_sections": [],
        "score_categories": [],
        "score_items": [item],
        "veto_rules": [],
        "uncertain_rules": [],
        "stats": {},
    }


def _named_item(
    item_id: str,
    name: str,
    original_rule: str = "评分规则",
    *,
    full_score: float = 5,
) -> dict[str, Any]:
    return {
        **_item(item_id, name=name, evaluation_type="objective", full_score=full_score),
        "original_rule": original_rule,
    }


def make_performance_rules() -> dict[str, Any]:
    return _single_item_rules(
        _named_item("score_item_010", "类似案例1", "每个在资格业绩之外的有效类似业绩得1分，最高5分。")
    ) | {
        "score_items": [
            _named_item("score_item_010", "类似案例1", "每个在资格业绩之外的有效类似业绩得1分，最高5分。"),
            _named_item("score_item_011", "类似案例2", "剔除资格要求业绩金额后按累计金额评分。"),
        ]
    }


def make_performance_reviews(
    *,
    qualification_case_status: str,
    scoring_case_statuses: list[str],
) -> dict[str, Any]:
    rows = [
        ("1", "资格要求业绩", qualification_case_status, 100),
        *[
            (str(index + 2), "评分业绩", status, 100 + index * 100)
            for index, status in enumerate(scoring_case_statuses)
        ],
    ]
    reviews = []
    for number, remark, status, amount in rows:
        reviews.append(
            {
                "status": status,
                "table_row": {
                    "序号": number,
                    "项目名称": f"项目{number}",
                    "备注": remark,
                },
                "checks_by_key": {
                    "contract_amount": {
                        "status": "pass" if status == "pass" else "uncertain",
                        "amount_value": amount,
                        "reason": "固定金额已确认" if status == "pass" else "金额不确定",
                        "evidence": [],
                    }
                },
            }
        )
    return {"performance_reviews": reviews, "stats": {}}


def make_price_rule() -> dict[str, Any]:
    return _single_item_rules(
        _named_item(
            "score_item_014",
            "报价评分",
            "根据所有有效投标人的经评审评标价和基准价P0计算，满分40分。",
            full_score=40,
        )
    )


def make_project_manager_rule() -> dict[str, Any]:
    return _single_item_rules(
        _named_item(
            "score_item_007",
            "项目经理资质",
            "学历、专业、工作年限、项目管理经验、能力证明和社保全部满足得5分，否则不得分。",
        )
    )


def make_technical_deviation_rule() -> dict[str, Any]:
    return _single_item_rules(
        _named_item(
            "score_item_002",
            "技术标准和要求的偏离情况",
            "全部满足得5分，一项不满足或部分满足扣1分。",
        )
    )


def make_bid_document_with_text(text: str) -> dict[str, Any]:
    return {
        "source": {},
        "sections": [{"section_id": "s1", "title": "商务投标文件", "path": ["商务投标文件"]}],
        "blocks": [{"block_id": "b1", "text": text}],
        "tables": [],
        "images": [],
    }


def _bid_file(tmp_path: Path, *, name: str = "bid.docx") -> FileMetadata:
    path = tmp_path / name
    if not path.exists():
        path.write_bytes(b"bid")
    return FileMetadata(name, path.stat().st_size, str(path))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_structured_document(path: Path, source_path: Path) -> None:
    _write_json(
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


def test_objective_scoring_excludes_subjective_items_and_veto_rules(tmp_path):
    result = run_objective_scoring(
        make_rules_with_objective_subjective_and_veto(),
        _bid_file(tmp_path),
    )

    assert [item["id"] for item in result["score_items"]] == ["objective-1"]
    assert result["stats"]["objective_item_count"] == 1
    assert result["stats"]["veto_rule_count"] == 1


def test_missing_evidence_is_not_converted_to_zero(tmp_path):
    result = run_objective_scoring(
        make_team_count_rule(),
        _bid_file(tmp_path, name="商务投标文件.docx"),
    )
    item = result["score_items"][0]

    assert item["status"] in {"file_scope_missing", "evidence_insufficient"}
    assert item["score"] is None
    assert item["reason"]


def test_reuses_resolved_bid_artifact_and_records_related_artifacts(tmp_path):
    bid_path = tmp_path / "bid.docx"
    bid_path.write_bytes(b"bid")
    artifact_dir = tmp_path / "bid_document_cleaning"
    artifact_dir.mkdir()
    _write_structured_document(artifact_dir / "structured_document.json", bid_path)
    _write_json(
        tmp_path / "compliance_extraction" / "10_performance_reviews.json",
        {"reviews": [], "stats": {}},
    )

    result = run_objective_scoring(
        {
            **make_rules_with_objective_subjective_and_veto(),
            "score_items": [
                _item(
                    "score_item_010",
                    name="类似案例1",
                    evaluation_type="objective",
                )
            ],
        },
        _bid_file(tmp_path),
    )

    assert result["source"]["bid_document_artifact"]
    assert "10_performance_reviews.json" in result["source"]["reused_artifacts"]
    assert result["score_items"][0]["tender_rule_source"]["block_ids"] == ["b0277"]


def test_team_scoring_uses_verified_member_count_not_roster_count(tmp_path):
    result = run_objective_scoring(
        make_team_count_rule(),
        _bid_file(tmp_path, name="商务投标文件.docx"),
        bid_document=make_bid_document_with_text("团队成员名单共35人，但未提供身份证、社保和缴费单位核验事实。"),
    )

    item = result["score_items"][0]
    assert item["status"] == "evidence_insufficient"
    assert item["score"] is None
    assert item["facts"]["roster_count_not_used"] is True


def test_team_scoring_calculates_the_verified_member_bracket(tmp_path):
    result = run_objective_scoring(
        make_team_count_rule(),
        _bid_file(tmp_path, name="商务投标文件.docx"),
        bid_document=make_bid_document_with_text(
            "有效团队成员人数：28；已核验身份证、社保和缴费单位均为投标人。"
        ),
    )

    item = result["score_items"][0]
    assert item["status"] == "auto_scored"
    assert item["score"] == 3
    assert item["calculation"]["matched_bracket"] == {"min": 25, "max": 34, "score": 3}


def test_performance_scoring_excludes_qualification_case_and_requires_valid_extra_cases(tmp_path):
    result = run_objective_scoring(
        make_performance_rules(),
        _bid_file(tmp_path),
        existing_artifacts={
            "10_performance_reviews.json": make_performance_reviews(
                qualification_case_status="fail",
                scoring_case_statuses=["uncertain", "pass"],
            )
        },
    )

    for item in result["score_items"]:
        assert item["status"] == "evidence_insufficient"
        assert item["score"] is None
        assert "资格" in item["reason"]


def test_performance_scoring_counts_only_valid_extra_cases_and_excludes_qualification(tmp_path):
    rules = make_performance_rules()
    artifacts = {
        "10_performance_reviews.json": make_performance_reviews(
            qualification_case_status="pass",
            scoring_case_statuses=["pass", "pass"],
        )
    }

    result = run_objective_scoring(
        rules,
        _bid_file(tmp_path),
        existing_artifacts=artifacts,
    )

    assert result["score_items"][0]["status"] == "auto_scored"
    assert result["score_items"][0]["score"] == 2
    assert result["score_items"][0]["calculation"]["excluded_qualification_case_numbers"] == ["1"]
    assert result["score_items"][1]["status"] == "auto_scored"
    assert result["score_items"][1]["score"] == 0


def test_performance_amount_scoring_uses_only_extra_case_amounts(tmp_path):
    reviews = make_performance_reviews(
        qualification_case_status="pass",
        scoring_case_statuses=["pass", "pass"],
    )
    reviews["performance_reviews"][1]["checks_by_key"]["contract_amount"]["amount_value"] = 1000
    reviews["performance_reviews"][2]["checks_by_key"]["contract_amount"]["amount_value"] = 600

    result = run_objective_scoring(
        _single_item_rules(
            _named_item("score_item_011", "类似案例2", "剔除资格要求业绩金额后累计金额评分。")
        ),
        _bid_file(tmp_path),
        existing_artifacts={"10_performance_reviews.json": reviews},
    )

    item = result["score_items"][0]
    assert item["status"] == "auto_scored"
    assert item["score"] == 3
    assert item["calculation"]["scoring_amount"] == 1600


def test_price_scoring_requires_other_valid_bidders(tmp_path):
    result = run_objective_scoring(make_price_rule(), _bid_file(tmp_path))

    item = result["score_items"][0]
    assert item["status"] == "other_bidder_data_required"
    assert item["score"] is None


def test_project_manager_requires_all_conditions(tmp_path):
    result = run_objective_scoring(
        make_project_manager_rule(),
        _bid_file(tmp_path, name="商务投标文件.docx"),
        bid_document=make_bid_document_with_text("项目经理持有PMP证书。"),
    )

    item = result["score_items"][0]
    assert item["status"] == "evidence_insufficient"
    assert item["score"] is None
    assert item["facts"]["single_certificate_not_sufficient"] is True


def test_technical_deviation_does_not_assume_business_file_is_technical_file(tmp_path):
    result = run_objective_scoring(
        make_technical_deviation_rule(),
        _bid_file(tmp_path, name="商务投标文件.docx"),
    )

    item = result["score_items"][0]
    assert item["status"] == "file_scope_missing"
    assert item["score"] is None


def test_external_and_unsupported_rules_are_explicitly_not_scored(tmp_path):
    rules = _single_item_rules(
        _named_item("score_item_013", "中国电信供应商不良行为处理结果执行", full_score=0)
    )
    rules["score_items"].append(
        _named_item("score_item_999", "未来新增客观评分项")
    )

    result = run_objective_scoring(rules, _bid_file(tmp_path))

    assert [item["status"] for item in result["score_items"]] == [
        "external_data_required",
        "unsupported",
    ]
    assert all(item["score"] is None for item in result["score_items"])
