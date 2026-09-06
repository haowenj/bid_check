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
