
# 评标规则结构化提取 Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox syntax for tracking.

Goal: 在现有标书检查 Web 工作流中开放 evaluation 模式，仅从招标文件的 MinerU 结构块中提取评分规则和明确否决性规则，生成独立、可追溯的结构化产出物。

Architecture: 新增 app/evaluation_rule_extraction.py 作为独立提取链路，复用现有 MinerU StructuredBlock、解析缓存和 artifact recorder，但不复用合规性对象过滤或 Schema。evaluation 模式在工作流中走单独分支，只执行招标文件规则提取并直接完成任务；结果页按层级展示评分大类、评分项、否决规则和不确定规则。直接在当前 main 工作区开发，不创建分支、worktree 或子代理。

Tech Stack: Python 3.14、FastAPI、现有 StructuredBlock/MinerU 解析器、OpenAI-compatible Chat Completions、JSON artifact recorder、SQLite repository、pytest。

Spec: docs/superpowers/specs/2026-09-06-evaluation-rule-extraction-design.md

## Global Constraints

- 只提取招标文件规则；本轮不计算投标文件得分、总分、排名、主观分数或执行实际否决。
- 评标候选必须按完整评审章节、完整评分表或完整否决条款组织，不按零散关键词截断，不拆评分表。
- 所有正式规则必须能追溯到当前输入中的真实 MinerU block_id；模型不得生成来源 ID。
- 评分规则必须保留评分层级、父子关系、分值、条件、计分方式、证明材料和客观/主观性质。
- 普通评标流程说明、评标委员会组成、目录索引、模板正文和无具体评分/否决后果的提醒不得进入正式规则。
- 语义不清、表格关系不可靠或材料绑定不明确的规则进入 uncertain_rules，不得猜测或静默丢失。
- evaluation 使用现有上传接口；full 继续返回 409；合规模式行为保持不变。
- 不读取、输出或提交 .env、API Key、令牌、凭据或私钥。
- 不创建新的工作区或分支，不使用子代理。

## Planned File Structure

- Create: app/evaluation_rule_extraction.py — 评标章节定位、候选窗口、Schema 校验、LLM 适配器、来源恢复、缓存和主提取函数。
- Modify: app/models.py — 增加评标结果 TypedDict，不改变已有合规对象类型。
- Modify: app/workflow.py — 增加评标提取服务回调和 evaluation-only 工作流分支。
- Modify: app/api.py — 构造评标 extractor/LLM/cache，开放 evaluation API，向模板传递评标结果。
- Modify: app/templates/bid_check_task.html — 增加 evaluation 模式结果渲染。
- Modify: app/templates/bid_check.html — 启用 evaluation 入口，保留 full 禁用。
- Modify: app/static/bid-check.css — 补充规则卡片、层级、统计、来源样式。
- Modify: README.md — 记录 evaluation 第一阶段范围、artifact 文件和真实验收数据。
- Create: tests/test_evaluation_rule_extraction.py — 单元、协议、缓存、产物和真实 artifact 读取测试。
- Modify: tests/test_workflow.py — evaluation 分支只提取招标文件、不解析投标文件、不调用 review。
- Modify: tests/test_api.py — evaluation 创建成功、full 仍拒绝、结果 JSON 和独立 artifact。
- Modify: tests/test_pages.py — evaluation 入口和结果页展示测试。
- Runtime only: data/tasks/<evaluation-task-id>/compliance_extraction/10_evaluation_rule_candidates.json、11_evaluation_rules.json、12_evaluation_filter_report.json 和 LLM call 文件；除非现有约定要求，不提交真实运行产物。

---

### Task 1: 建立评标规则数据协议与失败测试

Files:
- Modify: app/models.py
- Create: tests/test_evaluation_rule_extraction.py

Interfaces:
- Produces EvaluationSourceSection, ScoreCategory, ScoreItem, VetoRule, UncertainRule and TenderEvaluationExtractionResult TypedDict shapes.
- Result top-level keys are source_sections, score_categories, score_items, veto_rules, uncertain_rules, stats.

- [ ] Step 1: Write the failing contract test

    def test_evaluation_result_contract_has_score_veto_and_uncertain_collections():
        result = make_result()

        assert set(result) == {
            "source_sections", "score_categories", "score_items",
            "veto_rules", "uncertain_rules", "stats",
        }
        assert result["score_items"][0]["evaluation_type"] in {
            "objective", "subjective", "mixed",
        }
        assert result["score_items"][0]["category_id"] == "category_001"
        assert result["veto_rules"][0]["consequence"]

- [ ] Step 2: Run it and verify the expected failure

    Run: uv run pytest tests/test_evaluation_rule_extraction.py::test_evaluation_result_contract_has_score_veto_and_uncertain_collections -q
    Expected: FAIL because the evaluation result contract and test helper do not exist.

- [ ] Step 3: Add the minimal TypedDict contracts

    class EvaluationSourceSection(TypedDict):
        section: str
        title: str
        block_ids: list[str]
        source_text: str

    class ScoreCategory(TypedDict):
        id: str
        name: str
        parent_id: str | None
        full_score: float | None
        original_rule: str
        conditions: dict[str, Any]
        structure_status: str
        source: TenderSource

    class ScoreItem(TypedDict):
        id: str
        name: str
        category_id: str | None
        parent_item_id: str | None
        original_rule: str
        conditions: dict[str, Any]
        scoring_method: dict[str, Any]
        full_score: float | None
        evidence_requirements: list[str]
        evaluation_type: Literal["objective", "subjective", "mixed"]
        source: TenderSource

    class VetoRule(TypedDict):
        id: str
        name: str
        trigger_condition: str
        consequence: str
        evidence_requirements: list[str]
        original_rule: str
        source: TenderSource

    class UncertainRule(TypedDict):
        id: str
        rule_type: str
        description: str
        original_rule: str
        uncertainty_reason: str
        source: TenderSource

    class TenderEvaluationExtractionResult(TypedDict):
        source_sections: list[EvaluationSourceSection]
        score_categories: list[ScoreCategory]
        score_items: list[ScoreItem]
        veto_rules: list[VetoRule]
        uncertain_rules: list[UncertainRule]
        stats: dict[str, Any]

- [ ] Step 4: Run the focused test and verify it passes

    Run: uv run pytest tests/test_evaluation_rule_extraction.py::test_evaluation_result_contract_has_score_veto_and_uncertain_collections -q
    Expected: PASS.

- [ ] Step 5: Commit

    git add app/models.py tests/test_evaluation_rule_extraction.py
    git commit -m "feat: define evaluation rule extraction contract"

### Task 2: 实现完整评标章节定位和候选窗口

Files:
- Create/Modify: app/evaluation_rule_extraction.py
- Modify: tests/test_evaluation_rule_extraction.py

Interfaces:
- Consumes app.compliance_extraction.StructuredBlock.
- Produces EvaluationRegion, EvaluationCandidate, identify_evaluation_regions(blocks), build_evaluation_candidates(blocks), and build_evaluation_batches(candidates, max_batches, max_batch_chars).

- [ ] Step 1: Write the failing boundary test

    def test_evaluation_candidates_keep_complete_score_table_and_veto_context():
        blocks = [
            block("b1", "heading", "第三章 评标办法", "第三章 评标办法", 1, 1),
            block("b2", "heading", "评标办法前附表", "第三章 评标办法", 2, 2),
            block("b3", "paragraph", "总分100分，其中商务30分，技术50分，价格20分。",
                  "评标办法前附表", 3),
            block("b4", "table", "评分因素 | 评分标准 | 分值\n企业业绩 | 每个业绩2分，最高10分 | 10",
                  "评标办法前附表", 4),
            block("b5", "paragraph", "须提供合同关键页扫描件。", "评标办法前附表", 5),
            block("b6", "heading", "3.1 初步评审", "第三章 评标办法", 6, 2),
            block("b7", "paragraph", "有一项不符合评审标准的，评标委员会应当否决其投标。",
                  "3.1 初步评审", 7),
            block("b8", "paragraph", "评标委员会完成评标后形成评标报告。",
                  "3.1 初步评审", 8),
            block("b9", "heading", "第四章 合同条款", "第四章 合同条款", 9, 1),
            block("b10", "paragraph", "合同付款方式。", "第四章 合同条款", 10),
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

- [ ] Step 2: Run the test and verify it fails

    Run: uv run pytest tests/test_evaluation_rule_extraction.py::test_evaluation_candidates_keep_complete_score_table_and_veto_context -q
    Expected: FAIL because the evaluation region/candidate functions do not exist.

- [ ] Step 3: Implement region and candidate dataclasses

    @dataclass(frozen=True)
    class EvaluationRegion:
        title: str
        section: str
        block_ids: list[str]
        blocks: list[StructuredBlock]
        text: str
        order: int
        kind: Literal["scoring", "veto", "mixed"]

    @dataclass(frozen=True)
    class EvaluationCandidate:
        block_ids: list[str]
        section: str
        title: str
        text: str
        order: int
        region_kind: Literal["scoring", "veto", "mixed"]
        table_block_ids: list[str]

    A title is an actual heading or a short standalone section label matching:
    评标办法|评审办法|综合评估法|综合评分法|评分标准|评审标准|初步评审|详细评审|资格审查|符合性审查|否决投标|废标|无效投标.
    Use heading levels to stop at the next non-evaluation heading at the same or higher level. Keep nested evaluation headings in the current region. Use metadata table_body for table text while retaining the table block as one unit.

    Drop only navigation/index tables, pure 评标委员会组成/评标程序/评标报告/开标流程/评标原则 text, and regions without any concrete scoring or trigger/consequence signal. Return filter records with block IDs and reasons.

- [ ] Step 4: Add batch-boundary test

    def test_evaluation_batching_never_splits_a_candidate_or_table():
        candidates = [
            candidate("c1", "评分表", "table-row-a" * 20, 1),
            candidate("c2", "否决条款", "veto-rule" * 20, 2),
        ]

        batches = build_evaluation_batches(
            candidates, max_batches=2, max_batch_chars=50
        )

        assert [item.block_ids for batch in batches for item in batch] == [
            ["c1"], ["c2"],
        ]

- [ ] Step 5: Run focused candidate tests

    Run: uv run pytest tests/test_evaluation_rule_extraction.py -k "candidate or batching" -q
    Expected: PASS.

- [ ] Step 6: Commit

    git add app/evaluation_rule_extraction.py tests/test_evaluation_rule_extraction.py
    git commit -m "feat: locate complete evaluation rule sections"

### Task 3: 实现严格 LLM 协议、来源恢复和确定性 fallback

Files:
- Modify: app/evaluation_rule_extraction.py
- Modify: tests/test_evaluation_rule_extraction.py

Interfaces:
- Consumes EvaluationCandidate batches.
- Produces EvaluationRuleLLM, DeterministicEvaluationRuleLLM, OpenAICompatibleEvaluationRuleLLM, _coerce_evaluation_output, and _normalize_evaluation_sources.

- [ ] Step 1: Write failing Schema/provenance/objectivity tests

    def test_llm_output_rejects_unknown_source_block_ids_and_extra_fields():
        with pytest.raises(EvaluationRuleExtractionError, match="source_block_ids"):
            _coerce_evaluation_output({
                "score_categories": [],
                "score_items": [{
                    "name": "企业业绩",
                    "category_id": "category_001",
                    "parent_item_id": None,
                    "original_rule": "每个业绩得2分，最高10分",
                    "conditions": {},
                    "scoring_method": {},
                    "full_score": 10,
                    "evidence_requirements": [],
                    "evaluation_type": "objective",
                    "source_block_ids": ["not-in-input"],
                    "extra": "reject",
                }],
                "veto_rules": [],
                "uncertain_rules": [],
            })

    def test_deterministic_fallback_preserves_candidate_as_uncertain_rule():
        output = DeterministicEvaluationRuleLLM().extract([
            candidate("b1", "评标办法", "评分表关系无法确认，企业业绩 | 10分", 1)
        ])

        assert output["uncertain_rules"][0]["source_block_ids"] == ["b1"]
        assert output["score_items"] == []

- [ ] Step 2: Run tests and verify they fail

    Run: uv run pytest tests/test_evaluation_rule_extraction.py -k "schema or fallback" -q
    Expected: FAIL because the evaluation LLM protocol and validator do not exist.

- [ ] Step 3: Implement strict output validation and source normalization

    Allow only top-level score_categories, score_items, veto_rules, uncertain_rules.
    Every item must contain the fields defined in the spec; source IDs must be a non-empty
    list of strings. Validate evaluation_type against objective|subjective|mixed,
    numeric full_score values, and non-empty veto trigger_condition/consequence.
    Reject model id, source_text, arbitrary extra fields, and unknown enum values.

    Normalize accepted objects against the candidate block map. Every referenced ID must
    exist in the candidate. If original_rule has no meaningful support in the cited
    source text, move a compact copy to uncertain_rules with reason
    source_text_not_supported. Rebuild source using the candidate section, ordered block
    IDs, and exact original MinerU text joined in order. Generate stable local IDs
    category_001, score_item_001, veto_001, uncertain_001 after merge.

    DeterministicEvaluationRuleLLM is conservative: candidates containing clear numeric
    scoring or explicit veto consequence become raw uncertain_rules; it returns no guessed
    categories/items. It must not infer table mappings.

- [ ] Step 4: Add prompt contract test

    def test_openai_evaluation_prompt_requires_full_rows_and_forbids_guessing(monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({
                "choices": [{"message": {"content":
                    '{"score_categories":[],"score_items":[],"veto_rules":[],"uncertain_rules":[]}'
                }}]
            })

        monkeypatch.setattr(
            "app.evaluation_rule_extraction.urllib.request.urlopen",
            fake_urlopen,
        )
        OpenAICompatibleEvaluationRuleLLM(api_key="test-key").extract([
            candidate("b1", "评标办法", "企业业绩 | 每个2分，最高10分 | 10", 1)
        ])

        prompt = captured["payload"]["messages"][1]["content"]
        assert "完整评分表" in prompt
        assert "不得猜测" in prompt
        assert "objective" in prompt
        assert "veto_rules" in prompt

- [ ] Step 5: Run protocol tests

    Run: uv run pytest tests/test_evaluation_rule_extraction.py -k "schema or fallback or prompt" -q
    Expected: PASS.

- [ ] Step 6: Commit

    git add app/evaluation_rule_extraction.py tests/test_evaluation_rule_extraction.py
    git commit -m "feat: add structured evaluation rule llm protocol"

### Task 4: 实现主提取器、缓存和独立 artifact

Files:
- Modify: app/evaluation_rule_extraction.py
- Modify: tests/test_evaluation_rule_extraction.py
- Modify app/compliance_artifacts.py only if an existing recorder helper is actually required.

Interfaces:
- Produces extract_tender_evaluation_rules(tender_file, parser=None, llm=None, cache=None, parser_cache=None, recorder=None, max_batches=8, max_batch_chars=12000, max_retries=2) -> TenderEvaluationExtractionResult.
- Formal artifact path: compliance_extraction/11_evaluation_rules.json.

- [ ] Step 1: Write failing one-call/artifact integration test

    def test_evaluation_extractor_writes_independent_artifacts_and_stats(tmp_path):
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        tender = task_dir / "tender.docx"
        tender.write_bytes(b"tender")
        recorder = ComplianceExtractionRecorder(task_dir)

        result = extract_tender_evaluation_rules(
            FileMetadata("招标文件.docx", tender.stat().st_size, str(tender)),
            parser=ParserWithEvaluationBlocks(),
            llm=StructuredEvaluationLLM(),
            recorder=recorder,
        )

        artifact_dir = task_dir / "compliance_extraction"
        artifact = json.loads(
            (artifact_dir / "11_evaluation_rules.json").read_text()
        )
        candidates = json.loads(
            (artifact_dir / "10_evaluation_rule_candidates.json").read_text()
        )

        assert artifact["score_categories"][0]["full_score"] == 30
        assert artifact["score_items"][0]["scoring_method"]["points_per_unit"] == 2
        assert artifact["score_items"][0]["source"]["block_ids"] == [
            "b3", "b4", "b5"
        ]
        assert artifact["stats"]["llm_total_calls"] == 1
        assert artifact["stats"]["llm_completed_calls"] == 1
        assert candidates["candidate_count"] == 1
        assert candidates["candidate_chars"] > 0

- [ ] Step 2: Run it and verify it fails

    Run: uv run pytest tests/test_evaluation_rule_extraction.py::test_evaluation_extractor_writes_independent_artifacts_and_stats -q
    Expected: FAIL because the evaluator extractor and artifacts do not exist.

- [ ] Step 3: Implement orchestration

    Use separate EVALUATION_RULE_CACHE_VERSION and prompt version. The extractor must:
    1. Validate the tender path.
    2. Read evaluation result cache first; on a valid hit write the formal artifact and summary stats without calling MinerU/LLM.
    3. Reuse parser_cache for StructuredBlock objects or call the provided DocumentParser; copy parser diagnostics into stats.
    4. Write 10_evaluation_rule_candidates.json with raw_text_chars, candidate_chars, prompt_chars, candidate_count, complete candidate metadata, and block ranges.
    5. Submit all candidates in one batch when serialized candidates fit max_batch_chars; otherwise pack whole candidates into max_batches batches. Record one recorder llm call file pair per attempt and elapsed milliseconds.
    6. Coerce, normalize, merge by source order, and preserve unresolvable relationships in uncertain_rules.
    7. Write 12_evaluation_filter_report.json and 11_evaluation_rules.json, cache the normalized result, and finalize stats in finally.

    The formal artifact includes schema_version, source_sections, all five rule collections,
    stats, and artifact_paths. Stats includes parser/source, raw/candidate/prompt chars,
    usage prompt tokens when available, estimated prompt tokens otherwise, total calls,
    completed/failed calls, retries, each call elapsed, total LLM elapsed, total elapsed,
    category/item/veto/uncertain counts, objective/subjective/mixed counts, filtered count,
    and uncertainty count.

- [ ] Step 4: Add failure and cache tests

    def test_evaluation_llm_failure_keeps_candidate_artifact_and_failed_summary(tmp_path):
        recorder, metadata = make_task_recorder_and_metadata(tmp_path)

        with pytest.raises(EvaluationRuleExtractionError):
            extract_tender_evaluation_rules(
                metadata,
                parser=ParserWithEvaluationBlocks(),
                llm=FailingEvaluationLLM(),
                recorder=recorder,
                max_retries=0,
            )

        artifact_dir = tmp_path / "task-001" / "compliance_extraction"
        assert (artifact_dir / "10_evaluation_rule_candidates.json").is_file()
        summary = json.loads((artifact_dir / "summary.json").read_text())
        assert summary["status"] == "failed"

    def test_evaluation_cache_is_independent_from_compliance_cache(tmp_path):
        first = extract_tender_evaluation_rules(
            metadata, parser=ParserWithEvaluationBlocks(),
            llm=StructuredEvaluationLLM(), cache=InMemoryRequirementCache(),
        )
        second = extract_tender_evaluation_rules(
            metadata, parser=ParserWithEvaluationBlocks(),
            llm=StructuredEvaluationLLM(), cache=InMemoryRequirementCache(),
        )
        assert first == second

- [ ] Step 5: Run focused integration tests

    Run: uv run pytest tests/test_evaluation_rule_extraction.py -q
    Expected: PASS.

- [ ] Step 6: Commit

    git add app/evaluation_rule_extraction.py tests/test_evaluation_rule_extraction.py
    git commit -m "feat: persist structured evaluation rules"

### Task 5: 接入 evaluation-only 工作流和 API

Files:
- Modify: app/workflow.py
- Modify: app/api.py
- Modify: tests/test_workflow.py
- Modify: tests/test_api.py

Interfaces:
- BidCheckServices gains extract_evaluation_with_recorder: Callable[..., dict[str, Any]] | None.
- BidCheckWorkflow.run invokes the evaluation extractor when task.check_mode == evaluation, skips bid parsing and review, and persists evaluation_rules in task result.

- [ ] Step 1: Write failing workflow/API tests

    def test_evaluation_workflow_extracts_tender_only_and_skips_bid_parse(repository, tmp_path):
        calls = []

        def evaluate(tender_file, recorder=None):
            calls.append(("evaluate", tender_file.filename))
            return make_evaluation_result()

        def parse(_bid_file):
            calls.append(("parse", "unexpected"))
            raise AssertionError("evaluation mode must not parse the bid file")

        services = BidCheckServices(
            extract=lambda _: {},
            parse=parse,
            review=lambda *_: (_ for _ in ()).throw(
                AssertionError("review skipped")
            ),
            extract_evaluation_with_recorder=evaluate,
        )
        task = create_task(repository, tmp_path, check_mode="evaluation")
        BidCheckWorkflow(repository, services).run(task.task_id)

        completed = repository.get(task.task_id)
        assert completed.status == "complete"
        assert completed.result["evaluation_rules"] == make_evaluation_result()
        assert calls == [("evaluate", "招标文件.docx")]

    def test_evaluation_api_accepts_mode_and_full_remains_disabled(client, repository):
        response = client.post(
            "/api/bid-check/tasks", files=docx_files(),
            data={"check_mode": "evaluation"},
        )
        assert response.status_code == 202
        task = repository.get(response.json()["task_id"])
        assert task.status == "complete"
        assert "evaluation_rules" in task.result

        full = client.post(
            "/api/bid-check/tasks", files=docx_files(),
            data={"check_mode": "full"},
        )
        assert full.status_code == 409

- [ ] Step 2: Run tests and verify they fail

    Run: uv run pytest tests/test_workflow.py -k evaluation -q
    Run: uv run pytest tests/test_api.py -k "evaluation or development_modes" -q
    Expected: FAIL because evaluation is rejected and the service/branch do not exist.

- [ ] Step 3: Implement the evaluation workflow branch

    Before the current parallel compliance stages, branch on task.check_mode == evaluation.
    Mark requirements running, call extract_evaluation_with_recorder(task.tender_file,
    recorder=recorder), mark requirements complete, mark bid_parse complete with a
    workflow.stage.skip event and reason evaluation_mode_does_not_use_bid_file, then call
    repository.complete(task_id, {"evaluation_rules": output}). Set review elapsed to 0,
    finalize workflow stats, and return. On extractor error mark requirements failed and
    retain candidate/LLM artifacts. If the callback is absent, fail with
    evaluation extractor not configured instead of executing compliance extraction.

    In build_default_workflow create a separate JsonRequirementCache at
    settings.data_dir / evaluation_rule_cache, reuse the existing parser_cache, and
    instantiate DeterministicEvaluationRuleLLM or OpenAICompatibleEvaluationRuleLLM
    according to settings. Pass the callback through BidCheckServices.

    In create_bid_check_task permit compliance and evaluation only; keep full rejected.
    Do not change the two-file upload validation.

- [ ] Step 4: Run focused workflow/API tests

    Run: uv run pytest tests/test_workflow.py tests/test_api.py -k "evaluation or development_modes" -q
    Expected: PASS.

- [ ] Step 5: Commit

    git add app/workflow.py app/api.py tests/test_workflow.py tests/test_api.py
    git commit -m "feat: enable evaluation extraction workflow"

### Task 6: 展示独立评标产出物并更新文档

Files:
- Modify: app/templates/bid_check.html
- Modify: app/templates/bid_check_task.html
- Modify: app/static/bid-check.css
- Modify: tests/test_pages.py
- Modify: README.md

Interfaces:
- Evaluation task page consumes task.result.evaluation_rules and falls back to compliance_extraction/11_evaluation_rules.json if task JSON lacks it.
- UI must never render actual bid score, ranking, veto execution or 预计得分 language.

- [ ] Step 1: Write failing page tests

    def test_evaluation_upload_option_is_enabled_but_full_is_disabled(client):
        page = client.get("/bid-check")
        assert 'value="evaluation"' in page.text
        assert 'value="evaluation" disabled' not in page.text
        assert 'value="full" disabled' in page.text

    def test_evaluation_result_page_renders_hierarchy_sources_and_uncertain_rules(
        client, stored_evaluation_task
    ):
        page = client.get(f"/bid-check/tasks/{stored_evaluation_task.task_id}")
        assert "评标规则提取结果" in page.text
        assert "商务评分" in page.text
        assert "企业业绩" in page.text
        assert "每个类似项目得2分，最高10分" in page.text
        assert "客观评分" in page.text
        assert "来源 block" in page.text
        assert "表格关系无法确认" in page.text
        assert "预计得分" not in page.text
        assert "最终排名" not in page.text

- [ ] Step 2: Run page tests and verify they fail

    Run: uv run pytest tests/test_pages.py -k evaluation -q
    Expected: FAIL because evaluation is disabled and no result-page branch exists.

- [ ] Step 3: Implement rendering without mixing compliance markup

    Enable the evaluation radio with description:
    仅提取招标文件评分与否决规则，不执行投标文件评分；keep full disabled.

    In bid_check_task.html branch on task.check_mode == evaluation before the
    compliance result layout. Render execution stats, used sections, category cards,
    nested score items, conditions, scoring method, evidence requirements,
    evaluation type badges, source block IDs, veto rules, and uncertain rules.
    Render full_score only when present and show 未明确 for null. Parent IDs are already
    normalized by the extractor; do not infer hierarchy in Jinja. Add compact CSS classes
    for category nesting, objective/subjective badges, uncertain cards and details sources.
    Do not modify compliance result semantics.

- [ ] Step 4: Update README

    Document evaluation as executable, full as disabled, tender-only extraction,
    10_evaluation_rule_candidates.json, 11_evaluation_rules.json,
    12_evaluation_filter_report.json, and llm/call_NNN_*.
    State that 11_evaluation_rules.json is an extraction artifact, not a score result.

- [ ] Step 5: Run page/regression tests

    Run: uv run pytest tests/test_pages.py tests/test_end_to_end.py -q
    Expected: PASS.

- [ ] Step 6: Commit

    git add app/templates/bid_check.html app/templates/bid_check_task.html app/static/bid-check.css tests/test_pages.py README.md
    git commit -m "feat: show evaluation rule extraction results"

### Task 7: 真实招标文件验收与全量验证

Files:
- Runtime only: data/tasks/<new-evaluation-task-id>/...
- Modify README.md or tests/test_evaluation_rule_extraction.py only if the measured acceptance contract needs an explicit documentation/test adjustment.

Interfaces:
- Acceptance uses the real tender file at data/tasks/5f77e206-6aca-4151-a949-35d3b509be26/tender.docx and an existing .docx bid file only to satisfy upload compatibility.

- [ ] Step 1: Run the complete suite before live acceptance

    Run: uv run pytest -q
    Expected: all tests pass with exit code 0. If a test fails, add a focused regression test,
    follow the TDD red-green cycle, and fix before live acceptance.

- [ ] Step 2: Execute one evaluation task against the configured MinerU/LLM services

    Use the existing API or direct workflow invocation with the real tender file.
    Do not print .env or credentials. Use the existing real bid file only for the required
    upload field. Record task ID and artifact directory. If the evaluation result cache
    prevents a fresh LLM call, invalidate only the evaluation cache by its explicit version
    or use a new task/fresh evaluation cache; do not delete unrelated artifacts.

- [ ] Step 3: Inspect the generated artifact read-only

    Read 11_evaluation_rules.json, 10_evaluation_rule_candidates.json,
    12_evaluation_filter_report.json, summary.json, and llm/call_*_output.json.
    Report actual sections and block ranges; raw/candidate/prompt chars; usage or
    estimated tokens; call count, each elapsed and total elapsed; category/item/veto
    counts; objective/subjective/mixed item names; uncertain rules and reasons.

    Validate every category/item parent ID, full score, table row alignment, conditions,
    evidence materials, source block IDs and veto consequences. Confirm ordinary flow
    text did not become a veto rule.

- [ ] Step 4: Add/run real-artifact contract test

    Add a read-only helper that skips when the acceptance task is absent and otherwise
    verifies the formal artifact exists, has all required collections, source sections,
    and stats keys. Run:
    uv run pytest tests/test_evaluation_rule_extraction.py -q
    Expected: PASS and the artifact is independently readable without the task database.

- [ ] Step 5: Final verification and diff inspection

    Run:
    uv run pytest -q
    git diff --check
    git status --short --branch
    git diff HEAD~6..HEAD --stat

    Expected: the suite exits 0, diff check emits no errors, and only planned source/tests/docs
    changes are present plus any intentionally untracked real acceptance artifact. Read all
    output before claiming completion.

- [ ] Step 6: Commit final source/test/docs changes if needed

    git add README.md tests/test_evaluation_rule_extraction.py
    git commit -m "test: verify real evaluation rule artifact"

    Do not stage generated data/tasks artifacts unless the repository convention requires it;
    report their absolute paths separately.

## Plan Self-Review

- Spec coverage: Tasks 1–3 cover protocol, complete candidate windows, table integrity, strict LLM schema, provenance and objective/subjective labels. Task 4 covers artifacts, stats, caching and failure preservation. Task 5 covers evaluation-only workflow/API. Task 6 covers UI/documentation. Task 7 covers automated and real-file acceptance.
- No placeholders: every implementation task names files, interfaces, commands, expected failures/pass conditions and concrete behavior.
- Type consistency: workflow callback returns TenderEvaluationExtractionResult as a dict; task result stores it under evaluation_rules; UI reads the same key or the formal artifact.
- Boundary check: no task adds bid scoring, total calculation, ranking, veto execution or full-mode behavior.
- Operational check: implementation stays in the current main worktree, creates no branch/worktree, uses no subagent, and does not expose secrets.

