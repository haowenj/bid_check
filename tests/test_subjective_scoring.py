from __future__ import annotations

import hashlib
import threading

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata
from app.subjective_scoring import (
    match_subjective_bid_content,
    run_subjective_scoring,
    select_subjective_items,
)


def test_select_subjective_items_excludes_non_subjective_rules():
    rules = {
        "score_items": [
            {"id": "s1", "evaluation_type": "subjective", "name": "主观一"},
            {"id": "o1", "evaluation_type": "objective", "name": "客观一"},
            {"id": "m1", "evaluation_type": "mixed", "name": "混合一"},
        ],
        "veto_rules": [{"id": "v1", "name": "不得否决"}],
    }

    assert [item["id"] for item in select_subjective_items(rules)] == ["s1"]


def test_match_subjective_bid_content_ignores_index_and_keeps_body_block():
    document = {
        "sections": [
            {"title": "3 商务评审索引表", "path": ["3 商务评审索引表"]},
            {"title": "13.5 评审要求承诺函", "path": ["13.5 评审要求承诺函"]},
        ],
        "blocks": [
            {
                "block_id": "index",
                "type": "table",
                "section": "3 商务评审索引表",
                "text": "投标文件编写质量的情况 | 对应页码 22",
                "order": 1,
            },
            {
                "block_id": "body",
                "type": "paragraph",
                "section": "13.5 评审要求承诺函",
                "text": "投标文件编写质量的情况良好，不存在材料缺失。",
                "order": 2,
            },
        ],
    }
    item = {
        "id": "score_item_001",
        "name": "投标文件编写质量的情况",
        "evidence_requirements": ["投标文件整体"],
    }

    result = match_subjective_bid_content(item, document, "商务投标文件部分.docx")

    assert result["status"] == "matched"
    assert [block["block_id"] for block in result["blocks"]] == ["body"]


def test_match_subjective_bid_content_returns_file_scope_missing_for_technical_item():
    document = {
        "sections": [{"title": "1 商务投标文件封面", "path": ["1 商务投标文件封面"]}],
        "blocks": [
            {
                "block_id": "b1",
                "type": "paragraph",
                "section": "1 商务投标文件封面",
                "text": "商务投标文件",
                "order": 1,
            }
        ],
    }
    item = {
        "id": "score_item_003",
        "name": "项目需求的分析及理解程度",
        "evidence_requirements": ["本项目需求的分析及理解描述"],
    }

    result = match_subjective_bid_content(item, document, "商务投标文件部分.docx")

    assert result["status"] == "file_scope_missing"
    assert result["blocks"] == []


def test_match_subjective_bid_content_does_not_treat_technical_word_in_case_as_scope():
    document = {
        "sections": [{"title": "21 业绩情况表", "path": ["21 业绩情况表"]}],
        "blocks": [
            {
                "block_id": "b1",
                "type": "paragraph",
                "section": "21 业绩情况表",
                "text": "项目为技术服务类业绩，合同已经提供。",
                "order": 1,
            }
        ],
    }
    item = {
        "id": "score_item_004",
        "name": "云网产品开发服务、大模型技术支持服务、云网产品测试服务支撑方案",
        "evidence_requirements": ["支撑方案"],
    }

    result = match_subjective_bid_content(item, document, "商务投标文件部分.docx")

    assert result["status"] == "file_scope_missing"


class RecordingSubjectiveLLM:
    model = "fixture"
    available = True

    def __init__(self, response):
        self.response = response
        self.calls = []

    def score(self, score_item, matched_bid_content, allowed_bands):
        self.calls.append(
            {
                "score_item": score_item,
                "content": matched_bid_content,
                "allowed_bands": allowed_bands,
            }
        )
        return self.response


def _hash_matched_bid(tmp_path, blocks):
    bid_path = tmp_path / "商务投标文件部分.docx"
    bid_path.write_bytes(b"business-bid")
    document = {
        "source": {"sha256": hashlib.sha256(b"business-bid").hexdigest()},
        "sections": [
            {
                "title": "13.5 评审要求承诺函",
                "path": ["13.5 评审要求承诺函"],
            }
        ],
        "blocks": blocks,
    }
    return bid_path, document


def test_run_subjective_scoring_scores_only_matched_item_and_writes_artifact(tmp_path):
    bid_path, document = _hash_matched_bid(
        tmp_path,
        [
            {
                "block_id": "b0218",
                "type": "paragraph",
                "section": "13.5 评审要求承诺函",
                "text": "投标文件编写质量的情况良好，不存在未按规定制作、内容错误、模糊、材料缺失和阅读困难。",
                "order": 1,
            }
        ],
    )
    rules = {
        "score_items": [
            {
                "id": "score_item_001",
                "name": "投标文件编写质量的情况",
                "full_score": 5,
                "original_rule": "每具备一项扣1分，扣完为止。",
                "conditions": {"items": ["未提供"]},
                "scoring_method": {"description": "每具备一项扣1分，扣完为止"},
                "evidence_requirements": ["投标文件整体"],
                "evaluation_type": "subjective",
            },
            {
                "id": "score_item_003",
                "name": "项目需求的分析及理解程度",
                "full_score": 5,
                "original_rule": "未提供不得分。",
                "conditions": {"items": ["未提供"]},
                "scoring_method": {"description": "[4,5]、[2,4)、(0,2)、0分"},
                "evidence_requirements": ["本项目需求的分析及理解描述"],
                "evaluation_type": "subjective",
            },
        ]
    }
    llm = RecordingSubjectiveLLM(
        {
            "score_band": "扣分规则",
            "recommended_score": 5,
            "reason": "b0218 明确承诺不存在评分标准列出的五类问题。",
            "evidence": [
                {
                    "block_id": "b0218",
                    "quote": "不存在未按规定制作、内容错误、模糊、材料缺失和阅读困难",
                }
            ],
            "uncertainty": {"level": "low", "notes": []},
        }
    )
    recorder = ComplianceExtractionRecorder(tmp_path)

    result = run_subjective_scoring(
        rules,
        FileMetadata("商务投标文件部分.docx", bid_path.stat().st_size, str(bid_path)),
        subjective_llm=llm,
        bid_document=document,
        recorder=recorder,
    )

    assert result["schema_version"] == "subjective-score-v1"
    assert result["stats"]["subjective_item_count"] == 2
    assert result["stats"]["ai_scored_count"] == 1
    assert result["stats"]["file_scope_missing_count"] == 1
    assert result["stats"]["llm_total_calls"] == 1
    assert len(llm.calls) == 1
    assert len(llm.calls[0]["content"]) == 1
    assert result["score_items"][0]["status"] == "ai_scored"
    assert result["score_items"][0]["recommended_score"] == 5
    assert result["score_items"][0]["evidence"][0]["relation"] == (
        "对应评分规则判断"
    )
    assert result["score_items"][1]["status"] == "file_scope_missing"
    assert result["score_items"][1]["recommended_score"] is None
    assert (tmp_path / "compliance_extraction" / "subjective_scores.json").is_file()
    assert result["stats"]["total_score_computed"] is False
    assert result["stats"]["ranking_computed"] is False
    assert result["stats"]["veto_executed"] is False


def test_run_subjective_scoring_rejects_out_of_band_or_untrusted_evidence(tmp_path):
    bid_path, document = _hash_matched_bid(
        tmp_path,
        [
            {
                "block_id": "b1",
                "type": "paragraph",
                "section": "质量服务保障措施",
                "text": "质量服务保障措施：针对本项目提出措施。",
                "order": 1,
            }
        ],
    )
    document["sections"] = [
        {"title": "质量服务保障措施", "path": ["质量服务保障措施"]}
    ]
    rules = {
        "score_items": [
            {
                "id": "score_item_009",
                "name": "质量服务保障措施",
                "full_score": 5,
                "original_rule": "针对性强得[4,5]分，针对性弱得[2,4)分，针对性差得(0,2)分，未提供不得分。",
                "conditions": {"items": ["针对性强", "针对性弱", "针对性差", "未提供"]},
                "scoring_method": {"description": "[4,5]、[2,4)、(0,2)、0分"},
                "evidence_requirements": ["质量服务保障措施"],
                "evaluation_type": "subjective",
            }
        ]
    }
    llm = RecordingSubjectiveLLM(
        {
            "score_band": "[4,5]",
            "recommended_score": 5,
            "reason": "b1 内容支持高档。",
            "evidence": [
                {
                    "block_id": "not-sent",
                    "quote": "不在输入中",
                    "relation": "错误证据",
                }
            ],
            "uncertainty": {"level": "low", "notes": []},
        }
    )

    result = run_subjective_scoring(
        rules,
        FileMetadata("bid.docx", bid_path.stat().st_size, str(bid_path)),
        subjective_llm=llm,
        bid_document=document,
    )

    item = result["score_items"][0]
    assert item["status"] == "llm_error"
    assert item["recommended_score"] is None
    assert any("not-sent" in note for note in item["uncertainty"]["notes"])


class BarrierSubjectiveLLM:
    model = "fixture"
    available = True

    def __init__(self):
        self.barrier = threading.Barrier(2)
        self.calls = []

    def score(self, score_item, matched_bid_content, allowed_bands):
        block_id = matched_bid_content[0]["block_id"]
        self.calls.append(score_item["id"])
        self.barrier.wait(timeout=2)
        return {
            "score_band": "扣分规则",
            "recommended_score": 5,
            "reason": f"{block_id} 支持按规则给出建议分。",
            "evidence": [
                {
                    "block_id": block_id,
                    "quote": matched_bid_content[0]["text"],
                    "relation": "对应评分规则",
                }
            ],
            "uncertainty": {"level": "medium", "notes": []},
        }


def test_run_subjective_scoring_runs_eligible_items_concurrently_once(tmp_path):
    bid_path, document = _hash_matched_bid(
        tmp_path,
        [
            {
                "block_id": "b1",
                "type": "paragraph",
                "section": "方案一",
                "text": "主观一已提供实施内容。",
                "order": 1,
            },
            {
                "block_id": "b2",
                "type": "paragraph",
                "section": "方案二",
                "text": "主观二已提供实施内容。",
                "order": 2,
            },
        ],
    )
    rules = {
        "score_items": [
            {
                "id": "s1",
                "name": "主观一",
                "full_score": 5,
                "original_rule": "每具备一项扣1分，扣完为止。",
                "conditions": {},
                "scoring_method": {"description": "每具备一项扣1分"},
                "evidence_requirements": ["实施内容"],
                "evaluation_type": "subjective",
            },
            {
                "id": "s2",
                "name": "主观二",
                "full_score": 5,
                "original_rule": "每具备一项扣1分，扣完为止。",
                "conditions": {},
                "scoring_method": {"description": "每具备一项扣1分"},
                "evidence_requirements": ["实施内容"],
                "evaluation_type": "subjective",
            },
        ]
    }
    llm = BarrierSubjectiveLLM()

    result = run_subjective_scoring(
        rules,
        FileMetadata("商务投标文件部分.docx", bid_path.stat().st_size, str(bid_path)),
        subjective_llm=llm,
        bid_document=document,
    )

    assert sorted(llm.calls) == ["s1", "s2"]
    assert result["stats"]["llm_total_calls"] == 2
    assert [item["status"] for item in result["score_items"]] == [
        "ai_scored",
        "ai_scored",
    ]


class FailingSubjectiveLLM:
    model = "fixture"
    available = True

    def __init__(self):
        self.calls = 0

    def score(self, score_item, matched_bid_content, allowed_bands):
        self.calls += 1
        raise RuntimeError("fixture model failure")


def test_run_subjective_scoring_does_not_retry_failed_model_call(tmp_path):
    bid_path, document = _hash_matched_bid(
        tmp_path,
        [
            {
                "block_id": "b1",
                "type": "paragraph",
                "section": "方案一",
                "text": "主观一已提供实施内容。",
                "order": 1,
            }
        ],
    )
    rules = {
        "score_items": [
            {
                "id": "s1",
                "name": "主观一",
                "full_score": 5,
                "original_rule": "每具备一项扣1分，扣完为止。",
                "conditions": {},
                "scoring_method": {"description": "每具备一项扣1分"},
                "evidence_requirements": ["实施内容"],
                "evaluation_type": "subjective",
            }
        ]
    }
    llm = FailingSubjectiveLLM()

    result = run_subjective_scoring(
        rules,
        FileMetadata("商务投标文件部分.docx", bid_path.stat().st_size, str(bid_path)),
        subjective_llm=llm,
        bid_document=document,
    )

    assert llm.calls == 1
    assert result["score_items"][0]["status"] == "llm_error"
    assert result["stats"]["llm_total_calls"] == 1
    assert result["stats"]["llm_failed_calls"] == 1
