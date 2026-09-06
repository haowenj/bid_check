from __future__ import annotations

import importlib.util
import json

import pytest

import app.models as models
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import InMemoryRequirementCache, StructuredBlock
from app.models import FileMetadata


def test_evaluation_result_contract_has_score_veto_and_uncertain_collections():
    assert hasattr(models, "TenderEvaluationExtractionResult")

    result = {
        "source_sections": [],
        "score_categories": [
            {
                "id": "category_001",
                "name": "商务评分",
                "parent_id": None,
                "full_score": 30,
                "original_rule": "商务部分满分30分。",
                "conditions": {},
                "structure_status": "explicit",
                "source": {
                    "section": "评标办法",
                    "block_ids": ["b1"],
                    "source_text": "商务部分满分30分。",
                },
            }
        ],
        "score_items": [
            {
                "id": "score_item_001",
                "name": "企业业绩",
                "category_id": "category_001",
                "parent_item_id": None,
                "original_rule": "每个业绩得2分，最高10分。",
                "conditions": {},
                "scoring_method": {"points_per_unit": 2},
                "full_score": 10,
                "evidence_requirements": ["合同关键页"],
                "evaluation_type": "objective",
                "source": {
                    "section": "评标办法",
                    "block_ids": ["b2"],
                    "source_text": "每个业绩得2分，最高10分。",
                },
            }
        ],
        "veto_rules": [
            {
                "id": "veto_001",
                "name": "不符合初步评审标准",
                "trigger_condition": "有一项不符合评审标准",
                "consequence": "否决其投标",
                "evidence_requirements": [],
                "original_rule": "有一项不符合评审标准的，评标委员会应当否决其投标。",
                "source": {
                    "section": "初步评审",
                    "block_ids": ["b3"],
                    "source_text": "有一项不符合评审标准的，评标委员会应当否决其投标。",
                },
            }
        ],
        "uncertain_rules": [],
        "stats": {},
    }

    assert set(result) == {
        "source_sections",
        "score_categories",
        "score_items",
        "veto_rules",
        "uncertain_rules",
        "stats",
    }
    assert result["score_items"][0]["evaluation_type"] in {
        "objective",
        "subjective",
        "mixed",
    }
    assert result["score_items"][0]["category_id"] == "category_001"
    assert result["veto_rules"][0]["consequence"]


def _block(
    block_id: str,
    block_type: str,
    text: str,
    section: str,
    order: int,
    heading_level: int | None = None,
) -> StructuredBlock:
    return StructuredBlock(
        block_id,
        block_type,  # type: ignore[arg-type]
        text,
        section,
        order,
        heading_level=heading_level,
    )


def test_evaluation_candidates_keep_complete_score_table_and_veto_context():
    assert importlib.util.find_spec("app.evaluation_rule_extraction") is not None
    from app.evaluation_rule_extraction import build_evaluation_candidates

    blocks = [
        _block("b1", "heading", "第三章 评标办法", "第三章 评标办法", 1, 1),
        _block("b2", "heading", "评标办法前附表", "第三章 评标办法", 2, 2),
        _block(
            "b3",
            "paragraph",
            "总分100分，其中商务30分，技术50分，价格20分。",
            "评标办法前附表",
            3,
        ),
        _block(
            "b4",
            "table",
            "评分因素 | 评分标准 | 分值\n企业业绩 | 每个业绩2分，最高10分 | 10",
            "评标办法前附表",
            4,
        ),
        _block(
            "b5",
            "paragraph",
            "须提供合同关键页扫描件。",
            "评标办法前附表",
            5,
        ),
        _block("b6", "heading", "3.1 初步评审", "第三章 评标办法", 6, 2),
        _block(
            "b7",
            "paragraph",
            "有一项不符合评审标准的，评标委员会应当否决其投标。",
            "3.1 初步评审",
            7,
        ),
        _block(
            "b8",
            "paragraph",
            "评标委员会完成评标后形成评标报告，报告应当包括评标专家评分原始记录表和否决投标的情况说明。",
            "3.1 初步评审",
            8,
        ),
        _block("b9", "heading", "第四章 合同条款", "第四章 合同条款", 9, 1),
        _block("b10", "paragraph", "合同付款方式。", "第四章 合同条款", 10),
    ]

    candidates = build_evaluation_candidates(blocks)
    text = "\n".join(candidate.text for candidate in candidates)

    assert len(candidates) == 1
    assert "企业业绩" in text
    assert "每个业绩2分，最高10分" in text
    assert "合同关键页扫描件" in text
    assert "有一项不符合评审标准的" in text
    assert "评标委员会完成评标后形成评标报告" not in text
    assert "合同付款方式" not in text


def test_evaluation_candidates_exclude_appendix_index_tables():
    from app.evaluation_rule_extraction import build_evaluation_candidates

    candidates = build_evaluation_candidates(
        [
            _block(
                "b1",
                "paragraph",
                "【此部分内容建议按照第三章评标办法中的评审标准的顺序一一罗列】",
                "初步评审索引表",
                1,
            ),
            _block(
                "b2",
                "table",
                "评审因素 | 投标文件组成 | 对应页码 | 备注或者说明",
                "初步评审索引表",
                2,
            ),
            _block(
                "b3",
                "heading",
                "18.1.13 ★资格审查资料",
                "投标文件格式",
                3,
                1,
            ),
            _block(
                "b4",
                "table",
                "资格审查资料 | 评审因素 | 对应页码",
                "投标文件格式",
                4,
            ),
        ]
    )

    assert candidates == []


def test_evaluation_regions_split_top_level_sections_without_splitting_nested_rules():
    from app.evaluation_rule_extraction import build_evaluation_candidates

    candidates = build_evaluation_candidates(
        [
            _block("b1", "paragraph", "3. 资格审查方法", "招标公告", 1),
            _block("b2", "paragraph", "未通过资格后审的投标人，其投标将被否决。", "招标公告", 2),
            _block("b3", "heading", "第三章 评标办法", "第三章 评标办法", 3, 1),
            _block("b4", "heading", "评标办法前附表", "第三章 评标办法", 4, 2),
            _block("b5", "table", "企业业绩 | 每个业绩2分，最高10分 | 10", "评标办法前附表", 5),
            _block("b6", "heading", "第四章 合同条款", "第四章 合同条款", 6, 1),
        ]
    )

    assert len(candidates) == 2
    assert candidates[0].block_ids == ["b1", "b2"]
    assert candidates[1].table_block_ids == ["b5"]


def test_evaluation_batching_never_splits_a_candidate_or_table():
    from app.evaluation_rule_extraction import EvaluationCandidate, build_evaluation_batches

    candidates = [
        EvaluationCandidate(
            block_ids=["c1"],
            section="评标办法",
            title="评分表",
            text="table-row-a" * 20,
            order=1,
            region_kind="scoring",
            table_block_ids=["c1"],
        ),
        EvaluationCandidate(
            block_ids=["c2"],
            section="评标办法",
            title="否决条款",
            text="veto-rule" * 20,
            order=2,
            region_kind="veto",
            table_block_ids=[],
        ),
    ]

    batches = build_evaluation_batches(
        candidates,
        max_batches=2,
        max_batch_chars=50,
    )

    assert [
        item.block_ids for batch in batches for item in batch
    ] == [["c1"], ["c2"]]


def _candidate(
    block_id: str,
    title: str,
    text: str,
    order: int = 1,
):
    from app.evaluation_rule_extraction import EvaluationCandidate

    return EvaluationCandidate(
        block_ids=[block_id],
        section="第三章 评标办法",
        title=title,
        text=text,
        order=order,
        region_kind="mixed",
        table_block_ids=[],
    )


def test_evaluation_output_rejects_extra_fields_and_normalizer_rejects_unknown_ids():
    from app.evaluation_rule_extraction import (
        _coerce_evaluation_output,
        _normalize_evaluation_sources,
    )

    raw = {
        "score_categories": [],
        "score_items": [
            {
                "name": "企业业绩",
                "category_id": None,
                "parent_item_id": None,
                "original_rule": "每个业绩得2分，最高10分",
                "conditions": {},
                "scoring_method": {},
                "full_score": 10,
                "evidence_requirements": [],
                "evaluation_type": "objective",
                "source_block_ids": ["not-in-input"],
                "extra": "reject",
            }
        ],
        "veto_rules": [],
        "uncertain_rules": [],
    }

    with pytest.raises(Exception, match="未允许字段"):
        _coerce_evaluation_output(raw)

    del raw["score_items"][0]["extra"]
    raw["score_items"][0]["evidence_requirements"] = None
    raw["score_items"][0]["conditions"] = ["每个业绩2分", "最高10分"]
    raw["score_items"][0]["scoring_method"] = "按有效业绩数量计分"
    output = _coerce_evaluation_output(raw)
    assert output["score_items"][0]["evidence_requirements"] == []
    assert output["score_items"][0]["conditions"] == {
        "items": ["每个业绩2分", "最高10分"]
    }
    assert output["score_items"][0]["scoring_method"] == {
        "description": "按有效业绩数量计分"
    }
    with pytest.raises(Exception, match="来源 block_id"):
        _normalize_evaluation_sources(output, [_candidate("b1", "评分表", "企业业绩")])


def test_evaluation_output_preserves_known_veto_consequence_detail():
    from app.evaluation_rule_extraction import _coerce_evaluation_output

    output = _coerce_evaluation_output(
        {
            "score_categories": [],
            "score_items": [],
            "veto_rules": [
                {
                    "name": "报价不完整",
                    "trigger_condition": "未提供详细报价",
                    "consequence": "没有实质性响应",
                    "non_substantive_response": "视为没有实质性响应招标文件。",
                    "evidence_requirements": None,
                    "original_rule": "未提供详细报价将视为没有实质性响应招标文件。",
                    "source_block_ids": ["b1"],
                }
            ],
            "uncertain_rules": [],
        }
    )

    assert output["veto_rules"][0]["additional_consequence"] == "视为没有实质性响应招标文件。"


def test_normalizer_rebinds_imprecise_model_source_to_matching_block():
    from app.evaluation_rule_extraction import (
        _coerce_evaluation_output,
        _normalize_evaluation_sources,
    )

    candidate = _candidate("b1", "评标办法", "表头")
    candidate = candidate.__class__(
        **{
            **candidate.__dict__,
            "block_ids": ["b1", "b2"],
            "text": "表头\n企业业绩每个有效业绩得2分，最高10分。",
            "blocks": [
                _block("b1", "heading", "评标办法前附表", "评标办法", 1),
                _block("b2", "paragraph", "企业业绩每个有效业绩得2分，最高10分。", "评标办法", 2),
            ],
        }
    )
    output = _coerce_evaluation_output(
        {
            "score_categories": [],
            "score_items": [
                {
                    "name": "企业业绩",
                    "category_id": None,
                    "parent_item_id": None,
                    "original_rule": "企业业绩每个有效业绩得2分，最高10分。",
                    "conditions": {},
                    "scoring_method": {},
                    "full_score": 10,
                    "evidence_requirements": [],
                    "evaluation_type": "objective",
                    "source_block_ids": ["b1"],
                }
            ],
            "veto_rules": [],
            "uncertain_rules": [],
        }
    )

    normalized = _normalize_evaluation_sources(output, [candidate])

    assert normalized["score_items"][0]["source"]["block_ids"] == ["b2"]


def test_normalizer_rebinds_whole_candidate_hint_and_keeps_unexplicit_veto_uncertain():
    from app.evaluation_rule_extraction import (
        _coerce_evaluation_output,
        _normalize_evaluation_sources,
    )

    candidate = _candidate("b1", "评标办法", "表头")
    candidate = candidate.__class__(
        **{
            **candidate.__dict__,
            "block_ids": ["b1", "b2", "b3"],
            "text": "表头\n评分因素 | 评分标准 | 分值\n非实质性条款超过10项不满足的则视为实质性不满足招标文件要求\n1.8.1投标人不得存在下列情形之一",
            "blocks": [
                _block("b1", "heading", "评标办法前附表", "评标办法", 1),
                _block(
                    "b2",
                    "table",
                    "评分因素 | 评分标准 | 分值\n非实质性条款超过10项不满足的则视为实质性不满足招标文件要求",
                    "评标办法",
                    2,
                ),
                _block(
                    "b3",
                    "paragraph",
                    "1.8.1投标人不得存在下列情形之一",
                    "评标办法",
                    3,
                ),
            ],
        }
    )
    output = _coerce_evaluation_output(
        {
            "score_categories": [],
            "score_items": [],
            "veto_rules": [
                {
                    "name": "非实质性条款偏离",
                    "trigger_condition": "非实质性条款超过10项不满足",
                    "consequence": "视为实质性不满足招标文件要求",
                    "evidence_requirements": [],
                    "original_rule": "非实质性条款超过10项不满足的则视为实质性不满足招标文件要求",
                    "source_block_ids": ["b1", "b2", "b3"],
                }
            ],
            "uncertain_rules": [],
        }
    )

    normalized = _normalize_evaluation_sources(output, [candidate])

    assert normalized["veto_rules"] == []
    assert normalized["uncertain_rules"][0]["source"]["block_ids"] == ["b2"]
    assert normalized["uncertain_rules"][0]["uncertainty_reason"] == (
        "no_explicit_veto_consequence"
    )


def test_unrepresented_veto_signal_is_retained_for_manual_review():
    from app.evaluation_rule_extraction import (
        _append_unrepresented_veto_signals,
        build_evaluation_candidates,
    )

    candidate = build_evaluation_candidates(
        [
            _block("b1", "heading", "3.1 初步评审", "评标办法", 1, 2),
            _block(
                "b2",
                "paragraph",
                "没有按照要求提供材料的投标将可能被否决。",
                "3.1 初步评审",
                2,
            ),
        ]
    )[0]
    result = {
        "score_categories": [],
        "score_items": [],
        "veto_rules": [],
        "uncertain_rules": [],
    }

    _append_unrepresented_veto_signals(result, [candidate])

    assert len(result["uncertain_rules"]) == 1
    assert result["uncertain_rules"][0]["source"]["block_ids"] == ["b2"]
    assert result["uncertain_rules"][0]["rule_type"] == (
        "unrepresented_veto_signal"
    )


def test_normalizer_accepts_explicit_document_rejection_consequence():
    from app.evaluation_rule_extraction import (
        _coerce_evaluation_output,
        _normalize_evaluation_sources,
    )

    candidate = _candidate(
        "b1",
        "开标",
        "出现下列情形时，招标人/招标代理机构不予接收投标文件：逾期送达。",
    )
    output = _coerce_evaluation_output(
        {
            "score_categories": [],
            "score_items": [],
            "veto_rules": [
                {
                    "name": "逾期送达",
                    "trigger_condition": "逾期送达",
                    "consequence": "不予接收投标文件",
                    "evidence_requirements": [],
                    "original_rule": candidate.text,
                    "source_block_ids": ["b1"],
                }
            ],
            "uncertain_rules": [],
        }
    )

    normalized = _normalize_evaluation_sources(output, [candidate])

    assert len(normalized["veto_rules"]) == 1
    assert normalized["veto_rules"][0]["consequence"] == "不予接收投标文件"


def test_normalizer_keeps_possible_veto_consequence_uncertain():
    from app.evaluation_rule_extraction import (
        _coerce_evaluation_output,
        _normalize_evaluation_sources,
    )

    candidate = _candidate(
        "b1",
        "评审提醒",
        "未按要求递交资料可能导致其投标被否决。",
    )
    output = _coerce_evaluation_output(
        {
            "score_categories": [],
            "score_items": [],
            "veto_rules": [
                {
                    "name": "资料不全",
                    "trigger_condition": "未按要求递交资料",
                    "consequence": "可能导致其投标被否决",
                    "evidence_requirements": [],
                    "original_rule": candidate.text,
                    "source_block_ids": ["b1"],
                }
            ],
            "uncertain_rules": [],
        }
    )

    normalized = _normalize_evaluation_sources(output, [candidate])

    assert normalized["veto_rules"] == []
    assert normalized["uncertain_rules"][0]["uncertainty_reason"] == (
        "no_explicit_veto_consequence"
    )


def test_deterministic_fallback_preserves_candidate_as_uncertain_rule():
    from app.evaluation_rule_extraction import DeterministicEvaluationRuleLLM

    output = DeterministicEvaluationRuleLLM().extract([
        _candidate("b1", "评标办法", "评分表关系无法确认，企业业绩 | 10分")
    ])

    assert output["uncertain_rules"][0]["source_block_ids"] == ["b1"]
    assert output["score_items"] == []


class _FakeLLMResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def test_openai_evaluation_prompt_requires_full_rows_and_forbids_guessing(monkeypatch):
    from app.evaluation_rule_extraction import OpenAICompatibleEvaluationRuleLLM

    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeLLMResponse({
            "choices": [{
                "message": {
                    "content": (
                        '{"score_categories":[],"score_items":[],'
                        '"veto_rules":[],"uncertain_rules":[]}'
                    )
                }
            }]
        })

    monkeypatch.setattr(
        "app.evaluation_rule_extraction.urllib.request.urlopen",
        fake_urlopen,
    )
    OpenAICompatibleEvaluationRuleLLM(api_key="test-key").extract([
        _candidate("b1", "评标办法", "企业业绩 | 每个2分，最高10分 | 10")
    ])

    prompt = captured["payload"]["messages"][1]["content"]
    assert "完整评分表" in prompt
    assert "不得猜测" in prompt
    assert "objective" in prompt
    assert "veto_rules" in prompt


def _evaluation_blocks() -> list[StructuredBlock]:
    return [
        _block("b1", "heading", "第三章 评标办法", "第三章 评标办法", 1, 1),
        _block("b2", "heading", "评标办法前附表", "第三章 评标办法", 2, 2),
        _block(
            "b3",
            "paragraph",
            "总分100分，其中商务30分，技术50分，价格20分。",
            "评标办法前附表",
            3,
        ),
        _block(
            "b4",
            "table",
            "评分因素 | 评分标准 | 分值\n企业业绩 | 每个业绩2分，最高10分 | 10",
            "评标办法前附表",
            4,
        ),
        _block(
            "b5",
            "paragraph",
            "须提供合同关键页扫描件。",
            "评标办法前附表",
            5,
        ),
        _block("b6", "heading", "3.1 初步评审", "第三章 评标办法", 6, 2),
        _block(
            "b7",
            "paragraph",
            "有一项不符合评审标准的，评标委员会应当否决其投标。",
            "3.1 初步评审",
            7,
        ),
        _block(
            "b8",
            "paragraph",
            "评标委员会完成评标后形成评标报告。",
            "3.1 初步评审",
            8,
        ),
        _block("b9", "heading", "第四章 合同条款", "第四章 合同条款", 9, 1),
        _block("b10", "paragraph", "合同付款方式。", "第四章 合同条款", 10),
    ]


class _EvaluationParser:
    def parse(self, path):
        del path
        return _evaluation_blocks()


class _StructuredEvaluationLLM:
    model = "test-model"

    def extract(self, candidates):
        assert len(candidates) == 1
        return {
            "score_categories": [
                {
                    "name": "综合评分",
                    "parent_id": None,
                    "full_score": 100,
                    "original_rule": "总分100分，其中商务30分，技术50分，价格20分。",
                    "conditions": {},
                    "structure_status": "explicit",
                    "source_block_ids": ["b3"],
                }
            ],
            "score_items": [
                {
                    "name": "企业业绩",
                    "category_id": "category_001",
                    "parent_item_id": None,
                    "original_rule": "企业业绩 | 每个业绩2分，最高10分 | 10",
                    "conditions": {"max_count": 5},
                    "scoring_method": {"points_per_unit": 2},
                    "full_score": 10,
                    "evidence_requirements": ["合同关键页扫描件"],
                    "evaluation_type": "objective",
                    "source_block_ids": ["b4", "b5"],
                }
            ],
            "veto_rules": [
                {
                    "name": "初步评审不通过",
                    "trigger_condition": "有一项不符合评审标准",
                    "consequence": "否决其投标",
                    "evidence_requirements": [],
                    "original_rule": "有一项不符合评审标准的，评标委员会应当否决其投标。",
                    "source_block_ids": ["b7"],
                }
            ],
            "uncertain_rules": [],
        }


def test_evaluation_extractor_writes_independent_artifacts_and_stats(tmp_path):
    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    tender = task_dir / "tender.docx"
    tender.write_bytes(b"tender")
    recorder = ComplianceExtractionRecorder(task_dir)

    from app.evaluation_rule_extraction import extract_tender_evaluation_rules

    result = extract_tender_evaluation_rules(
        FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
        parser=_EvaluationParser(),
        llm=_StructuredEvaluationLLM(),
        cache=InMemoryRequirementCache(),
        recorder=recorder,
    )

    artifact_dir = task_dir / "compliance_extraction"
    artifact = json.loads((artifact_dir / "11_evaluation_rules.json").read_text())
    candidates = json.loads(
        (artifact_dir / "10_evaluation_rule_candidates.json").read_text()
    )
    filter_report = json.loads(
        (artifact_dir / "12_evaluation_filter_report.json").read_text()
    )

    assert result["score_categories"][0]["full_score"] == 100
    assert artifact["score_items"][0]["scoring_method"]["points_per_unit"] == 2
    assert artifact["score_items"][0]["source"]["block_ids"] == ["b4", "b5"]
    assert artifact["veto_rules"][0]["consequence"] == "否决其投标"
    assert artifact["stats"]["llm_total_calls"] == 1
    assert artifact["stats"]["llm_completed_calls"] == 1
    assert artifact["stats"]["total_elapsed_ms"] is not None
    assert candidates["candidate_count"] == 1
    assert candidates["candidate_chars"] > 0
    assert any(
        item["block_id"] == "b8" and item["reason"] == "pure_evaluation_flow"
        for item in filter_report["items"]
    )


class _FailingEvaluationLLM:
    model = "failing-model"

    def extract(self, candidates):
        del candidates
        raise RuntimeError("simulated evaluation LLM failure")


def test_evaluation_llm_failure_keeps_candidate_artifact_and_failed_summary(tmp_path):
    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    tender = task_dir / "tender.docx"
    tender.write_bytes(b"tender")
    recorder = ComplianceExtractionRecorder(task_dir)

    from app.evaluation_rule_extraction import extract_tender_evaluation_rules

    with pytest.raises(Exception, match="simulated evaluation LLM failure"):
        extract_tender_evaluation_rules(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=_EvaluationParser(),
            llm=_FailingEvaluationLLM(),
            recorder=recorder,
            max_retries=0,
        )

    artifact_dir = task_dir / "compliance_extraction"
    assert (artifact_dir / "10_evaluation_rule_candidates.json").is_file()
    summary = json.loads((artifact_dir / "summary.json").read_text())
    assert summary["status"] == "failed"


def test_evaluation_result_cache_is_independent_and_reused(tmp_path):
    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    tender = task_dir / "tender.docx"
    tender.write_bytes(b"tender")
    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    cache = InMemoryRequirementCache()

    from app.evaluation_rule_extraction import extract_tender_evaluation_rules

    parser = _EvaluationParser()
    llm = _StructuredEvaluationLLM()
    first = extract_tender_evaluation_rules(
        metadata,
        parser=parser,
        llm=llm,
        cache=cache,
    )

    original_parse = parser.parse
    original_extract = llm.extract
    parser.parse = lambda path: (_ for _ in ()).throw(
        AssertionError("evaluation result cache was not reused")
    )
    llm.extract = lambda candidates: (_ for _ in ()).throw(
        AssertionError("evaluation result cache was not reused")
    )

    second = extract_tender_evaluation_rules(
        metadata,
        parser=parser,
        llm=llm,
        cache=cache,
    )

    parser.parse = original_parse
    llm.extract = original_extract

    for key in (
        "source_sections",
        "score_categories",
        "score_items",
        "veto_rules",
        "uncertain_rules",
    ):
        assert second[key] == first[key]
    assert second["stats"]["cache_hit"] is True


def test_evaluation_reuses_shared_mineru_document_cache(tmp_path):
    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    tender = task_dir / "tender.docx"
    tender.write_bytes(b"tender")
    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    parser_cache = InMemoryRequirementCache()

    from app.evaluation_rule_extraction import (
        DeterministicEvaluationRuleLLM,
        extract_tender_evaluation_rules,
    )

    parser = _EvaluationParser()
    extract_tender_evaluation_rules(
        metadata,
        parser=parser,
        llm=DeterministicEvaluationRuleLLM(),
        parser_cache=parser_cache,
    )
    parser.parse = lambda path: (_ for _ in ()).throw(
        AssertionError("shared MinerU parser cache was not reused")
    )

    second = extract_tender_evaluation_rules(
        metadata,
        parser=parser,
        llm=DeterministicEvaluationRuleLLM(),
        parser_cache=parser_cache,
    )

    assert second["stats"]["parser_cache_hit"] is True
    assert second["stats"]["parser_execution_source"] == "parser_cache"
