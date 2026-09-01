import json

import pytest

import app.compliance_extraction as extraction_module
from app.compliance_artifacts import ComplianceExtractionRecorder
from app.compliance_extraction import (
    CandidateWindow,
    ComplianceExtractionError,
    InMemoryRequirementCache,
    StructuredBlock,
    _coerce_raw_requirement,
    _normalize_requirements,
    extract_compliance_requirements_real,
)
from app.models import FileMetadata


def _new_requirement(*, name="身份证明", rule="需同时提供国徽面和人像面。", condition=None, ids=None):
    return {
        "name": name,
        "rule": rule,
        "condition": condition,
        "source_block_ids": ids or ["b0001"],
    }


def test_new_requirement_schema_forbids_execution_compilation_fields():
    normalized = _coerce_raw_requirement(_new_requirement())

    assert normalized == _new_requirement()
    with pytest.raises(ComplianceExtractionError, match="Schema"):
        _coerce_raw_requirement(
            {
                **_new_requirement(),
                "category": "attachment",
            }
        )

    with pytest.raises(ComplianceExtractionError, match="Schema"):
        _coerce_raw_requirement({**_new_requirement(), "rule": "   "})

    with pytest.raises(ComplianceExtractionError, match="Schema"):
        _coerce_raw_requirement({**_new_requirement(), "source_text": "模型伪造的来源"})


def test_normalization_preserves_condition_logic_and_merges_duplicate_sources():
    blocks = [
        StructuredBlock("b0001", "paragraph", "由委托代理人办理时", "授权委托书", 1),
        StructuredBlock(
            "b0002",
            "paragraph",
            "应提供授权委托书，并附代理人身份证明。",
            "授权委托书",
            2,
        ),
        StructuredBlock("b0003", "paragraph", "应提供授权委托书，并附代理人身份证明。", "授权委托书", 3),
    ]

    result = _normalize_requirements(
        [
            _new_requirement(
                name="授权委托书",
                rule="应提供授权委托书，并附代理人身份证明。",
                condition="由委托代理人办理时",
                ids=["b0001", "b0002"],
            ),
            _new_requirement(
                name="授权委托书",
                rule="应提供授权委托书，并附代理人身份证明。",
                condition="由委托代理人办理时",
                ids=["b0002", "b0003"],
            ),
        ],
        blocks,
    )

    assert result == [
        {
            "id": "tender_requirement_001",
            "name": "授权委托书",
            "rule": "应提供授权委托书，并附代理人身份证明。",
            "condition": "由委托代理人办理时",
            "source": {
                "section": "授权委托书",
                "block_ids": ["b0001", "b0002", "b0003"],
                "source_text": "由委托代理人办理时\n应提供授权委托书，并附代理人身份证明。\n应提供授权委托书，并附代理人身份证明。",
            },
        }
    ]


def test_normalization_restores_source_text_in_backend_from_ids():
    blocks = [StructuredBlock("b0001", "paragraph", "原始招标要求。", "格式", 1)]
    result = _normalize_requirements(
        [
            {
                "name": "要求",
                "rule": "压缩后的要求。",
                "condition": None,
                "source": {
                    "block_ids": ["b0001"],
                    "source_text": "模型伪造的来源。",
                },
            }
        ],
        blocks,
    )

    assert result[0]["source"]["source_text"] == "原始招标要求。"


def test_normalization_repairs_source_id_using_candidate_window_support():
    blocks = [
        StructuredBlock("b0001", "paragraph", "联合体投标的，联合体成员均不得存在上述任一情形。", "公告", 1),
        StructuredBlock(
            "b0002",
            "paragraph",
            "投标产品所涉知识产权应保证不存在权利瑕疵，并提供《知识产权不侵权承诺函》。",
            "公告",
            2,
        ),
    ]
    result = _normalize_requirements(
        [
            {
                "name": "知识产权不侵权承诺函",
                "rule": "投标产品所涉知识产权应保证不存在权利瑕疵，并提供《知识产权不侵权承诺函》。",
                "condition": None,
                "source_block_ids": ["b0001"],
            }
        ],
        blocks,
        candidate_windows=[CandidateWindow(["b0001", "b0002"], "公告", "", 1)],
    )

    assert result[0]["source"]["block_ids"] == ["b0002"]
    assert "知识产权" in result[0]["source"]["source_text"]


def test_deterministic_fallback_returns_object_without_invented_checks():
    llm = extraction_module.DeterministicComplianceLLM()
    result = llm.extract(
        [
            CandidateWindow(
                ["b0001"],
                "商务投标文件封面",
                "投标人名称：____\n日期：____",
                1,
            )
        ]
    )

    assert result == {
        "templates": [
            {
                "name": "商务投标文件封面",
                "source_block_ids": ["b0001"],
            }
        ],
        "project_requirements": [],
        "supplemental_materials": [],
    }


def test_openai_prompt_only_requests_tender_objects(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"templates": [], "project_requirements": [], "supplemental_materials": []}'
                            }
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    extraction_module.OpenAICompatibleLLM(api_key="test-key").extract(
        [CandidateWindow(["b0001"], "格式", "盖章 OR 签字。", 1)]
    )

    prompt = captured["payload"]["messages"][1]["content"]
    assert "templates" in prompt
    assert "project_requirements" in prompt
    assert "supplemental_materials" in prompt
    assert "不得生成 check_type" in prompt
    assert "scope" in prompt
    assert "evidence_type" in prompt
    assert "source_text" in prompt
    assert "自然语言规则" in prompt
    assert "name、rule、condition" not in prompt


def test_requirement_cache_version_is_new_and_parse_cache_key_is_stable():
    assert extraction_module.REQUIREMENT_CACHE_VERSION.startswith("tender-requirement-v4")
    assert extraction_module.PARSED_DOCUMENT_CACHE_VERSION.startswith("mineru-parse-v1")


def test_requirement_cache_key_changes_with_parser_and_llm_configuration(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"same tender")

    class ConfiguredParser:
        def __init__(self, command):
            self.command = command

    class ConfiguredLLM:
        def __init__(self, model):
            self.model = model

    parser_a = ConfiguredParser("mineru-a")
    parser_b = ConfiguredParser("mineru-b")
    llm_a = ConfiguredLLM("model-a")
    llm_b = ConfiguredLLM("model-b")

    key = extraction_module._requirement_cache_key
    assert key(tender, parser_a, llm_a, max_batches=8, max_batch_chars=12000) != key(
        tender, parser_b, llm_a, max_batches=8, max_batch_chars=12000
    )
    assert key(tender, parser_a, llm_a, max_batches=8, max_batch_chars=12000) != key(
        tender, parser_a, llm_b, max_batches=8, max_batch_chars=12000
    )


def test_requirement_cache_misses_when_parser_or_llm_changes(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"same tender")
    blocks = [StructuredBlock("b0001", "paragraph", "须提供营业执照。", "资格材料", 1)]

    class ConfiguredParser:
        def __init__(self, command):
            self.command = command
            self.calls = 0

        def parse(self, path):
            self.calls += 1
            return blocks

    class ConfiguredLLM:
        def __init__(self, model):
            self.model = model
            self.calls = 0

        def extract(self, batch):
            self.calls += 1
            return [_new_requirement(name="营业执照", rule="须提供营业执照。")]

    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    cache = InMemoryRequirementCache()
    parser_a, parser_b = ConfiguredParser("mineru-a"), ConfiguredParser("mineru-b")
    llm_a, llm_b = ConfiguredLLM("model-a"), ConfiguredLLM("model-b")

    extract_compliance_requirements_real(
        metadata, parser=parser_a, llm=llm_a, cache=cache
    )
    extract_compliance_requirements_real(
        metadata, parser=parser_b, llm=llm_a, cache=cache
    )
    extract_compliance_requirements_real(
        metadata, parser=parser_b, llm=llm_b, cache=cache
    )

    assert llm_a.calls == 2
    assert llm_b.calls == 1


def test_normalization_keeps_same_rule_in_distinct_sections():
    blocks = [
        StructuredBlock("b0001", "paragraph", "应填写投标人名称。", "商务标格式", 1),
        StructuredBlock("b0002", "paragraph", "应填写投标人名称。", "技术标格式", 2),
    ]
    raw = _new_requirement(name="投标人名称", rule="应填写投标人名称。")

    result = _normalize_requirements(
        [{**raw, "source_block_ids": ["b0001"]}, {**raw, "source_block_ids": ["b0002"]}],
        blocks,
    )

    assert len(result) == 2
    assert [item["source"]["section"] for item in result] == [
        "商务标格式",
        "技术标格式",
    ]


def test_openai_prompt_does_not_drop_long_candidate_text(monkeypatch):
    captured = {}
    marker = "候选内容末尾仍然必须可见。"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"templates": [], "project_requirements": [], "supplemental_materials": []}'
                            }
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(extraction_module.urllib.request, "urlopen", fake_urlopen)
    long_text = "候选内容开头。" + ("规则正文。" * 2500) + marker
    extraction_module.OpenAICompatibleLLM(api_key="test-key").extract(
        [CandidateWindow(["b0001"], "格式", long_text, 1)]
    )

    prompt = captured["payload"]["messages"][1]["content"]
    assert marker in prompt


def test_extractor_returns_only_simplified_fields_and_reuses_parse_cache(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [StructuredBlock("b0001", "paragraph", "须提供营业执照。", "资格材料", 1)]

    class FakeParser:
        def __init__(self):
            self.calls = 0

        def parse(self, path):
            self.calls += 1
            return blocks

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def extract(self, batch):
            self.calls += 1
            return [_new_requirement(name="营业执照", rule="须提供营业执照。")]

    parser = FakeParser()
    llm = FakeLLM()
    metadata = FileMetadata("招标文件.docx", tender.stat().st_size, str(tender))
    requirement_cache = InMemoryRequirementCache()
    parser_cache = InMemoryRequirementCache()
    recorder = ComplianceExtractionRecorder(tmp_path / "task-002")
    first = extract_compliance_requirements_real(
        metadata,
        parser=parser,
        llm=llm,
        parser_cache=parser_cache,
    )
    second = extract_compliance_requirements_real(
        metadata,
        parser=parser,
        llm=llm,
        cache=requirement_cache,
        parser_cache=parser_cache,
        recorder=recorder,
    )

    assert first == second
    assert set(first[0]) == {"id", "name", "rule", "condition", "source"}
    assert parser.calls == 1
    assert llm.calls == 2
    summary = json.loads((recorder.artifact_dir / "summary.json").read_text())
    assert summary["stats"]["parser_cache_hit"] is True
