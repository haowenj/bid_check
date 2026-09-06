from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.models import FileMetadata
from app.veto_rule_execution import run_veto_rule_execution


def make_rule(
    rule_id: str,
    name: str,
    trigger: str,
    *,
    original: str | None = None,
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
            "section": "初步评审",
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
