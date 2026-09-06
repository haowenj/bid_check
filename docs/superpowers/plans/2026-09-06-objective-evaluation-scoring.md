# 客观评标规则执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于已有评标规则和投标文件检查产出物，确定性执行 8 个客观评分项并生成可追溯的 `objective_scores.json`，不执行主观评分、否决判定或总分汇总。

**Architecture:** 新增一个只负责客观评分编排、证据加载和确定性规则计算的模块；它消费 `11_evaluation_rules.json`、投标文件 `structured_document.json` 及现有专项检查产出物。评标工作流在规则提取（含缓存命中）后调用该模块，并通过现有 `ComplianceExtractionRecorder` 写入独立产出物；缺少事实时输出显式状态而不是猜测分数。

**Tech Stack:** Python 3、pytest、现有 `BidCheckWorkflow`、`ComplianceExtractionRecorder`、JSON 产出物、现有 MinerU 投标文件解析器。

**Spec:** `docs/superpowers/specs/2026-09-06-objective-evaluation-scoring-design.md`

## Global Constraints

- 不创建工作区、分支或子代理，所有修改在当前 `main` 工作区完成。
- 只处理 `evaluation_type = objective` 的评分项，不处理主观评分、否决规则、总分汇总、排名或中标候选人。
- 优先复用已有 `structured_document.json`、`08_template_text_reviews.json`、`09_attachment_reviews.json`、`10_file_requirement_reviews.json`、`10_performance_reviews.json`。
- 证据不足不得默认记 0 分；只有规则条件已被可靠事实明确判定不满足时才可得到 0 分。
- 不新增 LLM 调用，不改变已有评标规则提取提示词、Schema、批次和合并逻辑。
- 产出物使用现有 `ComplianceExtractionRecorder`，评分项顺序和证据顺序保持确定性。

---

### Task 1: 建立客观评分核心接口和状态模型的失败测试

**Files:**
- Create: `tests/test_objective_scoring.py`
- Modify: `app/models.py` only if the implementation needs a public `ObjectiveScoreStatus` type; otherwise keep the existing model layer unchanged.

**Interfaces:**
- The tests define the public function expected by later tasks:
  `run_objective_scoring(evaluation_rules: Mapping[str, Any], bid_file: FileMetadata, *, bid_document: Mapping[str, Any] | None = None, artifact_dir: Path | None = None, recorder: ComplianceExtractionRecorder | None = None) -> dict[str, Any]`.
- The result contains `score_items`, `stats`, and each score item has `status`, `score`, `calculation`, `facts`, `evidence`, `related_artifacts`, and `tender_rule_source`.

- [x] **Step 1: Write failing unit tests for objective-item selection and safe status handling**

```python
def test_objective_scoring_excludes_subjective_items_and_veto_rules(tmp_path):
    rules = make_rules_with_objective_subjective_and_veto()
    bid_file = FileMetadata("bid.docx", 1, str(tmp_path / "bid.docx"))

    result = run_objective_scoring(rules, bid_file)

    assert [item["id"] for item in result["score_items"]] == ["objective-1"]
    assert result["stats"]["objective_item_count"] == 1
    assert result["stats"]["veto_rule_count"] == 1


def test_missing_evidence_is_not_converted_to_zero(tmp_path):
    rules = make_team_count_rule(full_score=5)
    bid_file = FileMetadata("商务投标文件.docx", 1, str(tmp_path / "bid.docx"))

    result = run_objective_scoring(rules, bid_file)
    item = result["score_items"][0]

    assert item["status"] in {"file_scope_missing", "evidence_insufficient"}
    assert item["score"] is None
    assert item["reason"]
```

- [x] **Step 2: Run the focused tests and verify they fail because the module/function is absent**

Run: `pytest -q tests/test_objective_scoring.py`

Expected: FAIL with an import or missing-function error for `app.objective_scoring.run_objective_scoring`.

- [x] **Step 3: Commit the red tests**

```bash
git add tests/test_objective_scoring.py
git commit -m "test: define objective scoring safety contract"
```

### Task 2: 实现证据加载、通用产出物和当前 8 个规则的安全状态判定

**Files:**
- Create: `app/objective_scoring.py`
- Modify: `tests/test_objective_scoring.py`

**Interfaces:**
- `run_objective_scoring(...)` returns a JSON-serializable dictionary and never performs an LLM call.
- `load_reusable_bid_evidence(bid_file: FileMetadata, *, artifact_dir: Path | None = None) -> dict[str, Any]` resolves symlinks, loads `bid_document_cleaning/structured_document.json` and sibling compliance artifacts, and records missing artifacts without treating them as failures.
- `run_objective_scoring` accepts an optional already-loaded `bid_document` and `artifact_dir` so the API can reuse a parser result without parsing twice.

- [x] **Step 1: Add failing tests for source/hash-checked artifact reuse and output provenance**

```python
def test_reuses_resolved_bid_artifact_and_records_related_artifacts(tmp_path):
    bid_path = tmp_path / "bid.docx"
    bid_path.write_bytes(b"bid")
    artifact_dir = tmp_path / "bid_document_cleaning"
    artifact_dir.mkdir()
    write_structured_document(artifact_dir / "structured_document.json", bid_path)
    write_json(artifact_dir.parent / "compliance_extraction" / "10_performance_reviews.json", {
        "reviews": [], "stats": {}
    })

    result = run_objective_scoring(
        make_performance_rule(),
        FileMetadata("bid.docx", bid_path.stat().st_size, str(bid_path)),
    )

    assert result["source"]["bid_document_artifact"]
    assert "10_performance_reviews.json" in result["source"]["reused_artifacts"]
    assert result["score_items"][0]["tender_rule_source"]["block_ids"] == ["b0277"]
```

- [x] **Step 2: Run the new tests to verify the expected failure**

Run: `pytest -q tests/test_objective_scoring.py::test_reuses_resolved_bid_artifact_and_records_related_artifacts`

Expected: FAIL because evidence loading and the objective scoring module are not implemented.

- [x] **Step 3: Implement the minimal core module**

Implement these concrete behaviors in `app/objective_scoring.py`:

```python
OBJECTIVE_SCORE_ARTIFACT = "objective_scores.json"
OBJECTIVE_STATUSES = {
    "auto_scored",
    "evidence_insufficient",
    "file_scope_missing",
    "other_bidder_data_required",
    "external_data_required",
    "unsupported",
}

def run_objective_scoring(
    evaluation_rules: Mapping[str, Any],
    bid_file: FileMetadata,
    *,
    bid_document: Mapping[str, Any] | None = None,
    artifact_dir: Path | None = None,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    # select objective score_items in source order, load reusable evidence,
    # dispatch only known rule handlers, and preserve an explicit unsupported
    # result for any future objective rule.
    ...
```

The loader must compare `structured_document.source.sha256` with the actual bid hash when both exist. It must inspect the resolved real parent directory so symlinked task files can reuse the existing compliance task’s parse artifacts. It must preserve missing-path diagnostics instead of raising for ordinary missing evidence.

The common item shape must include the original rule fields and use `score=None` for every non-`auto_scored` status. The output stats must include objective item count, per-status counts, `llm_total_calls=0`, `duplicate_parse=False`, and `score_sum=None` when any objective item is not automatically scored. It must not expose a final total score.

- [x] **Step 4: Run focused tests and then the full unit test file**

Run: `pytest -q tests/test_objective_scoring.py`

Expected: PASS for selection, provenance, and safe missing-evidence behavior.

- [x] **Step 5: Commit the core module**

```bash
git add app/objective_scoring.py tests/test_objective_scoring.py
git commit -m "feat: add objective scoring artifact core"
```

### Task 3: 实现 8 个当前客观评分项的确定性计算和证据边界

**Files:**
- Modify: `app/objective_scoring.py`
- Modify: `tests/test_objective_scoring.py`

**Interfaces:**
- Rule dispatch is based on stable score-item IDs when present (`score_item_002`, `007`, `008`, `010`, `011`, `012`, `013`, `014`) and falls back to normalized names only for fixture/backward compatibility.
- Each handler returns a common partial result with `status`, `score`, `facts`, `calculation`, `evidence`, `related_artifacts`, and `reason`.

- [x] **Step 1: Write failing tests for the business-critical rules**

```python
def test_team_scoring_uses_verified_member_count_not_roster_count(tmp_path):
    rules = make_team_count_rule()
    bid_document = make_bid_document_with_team_roster(
        roster_count=35,
        verified_member_count=None,
        sections=["商务投标文件"],
    )

    result = run_objective_scoring(rules, bid_file(tmp_path), bid_document=bid_document)

    assert result["score_items"][0]["status"] == "evidence_insufficient"
    assert result["score_items"][0]["score"] is None


def test_performance_scoring_excludes_qualification_case_and_requires_valid_extra_cases(tmp_path):
    rules = make_performance_rules()
    artifacts = {
        "10_performance_reviews.json": make_performance_reviews(
            qualification_case_status="fail",
            scoring_case_statuses=["uncertain", "pass"],
        )
    }

    result = run_objective_scoring(
        rules,
        bid_file(tmp_path),
        existing_artifacts=artifacts,
    )

    assert result["score_items"][0]["status"] == "evidence_insufficient"
    assert "资格" in result["score_items"][0]["reason"]


def test_price_scoring_requires_other_valid_bidders(tmp_path):
    result = run_objective_scoring(make_price_rule(), bid_file(tmp_path))

    assert result["score_items"][0]["status"] == "other_bidder_data_required"
    assert result["score_items"][0]["score"] is None


def test_project_manager_requires_all_conditions(tmp_path):
    result = run_objective_scoring(
        make_project_manager_rule(),
        bid_file(tmp_path),
        bid_document=make_bid_document_with_only_pmp_certificate(),
    )

    assert result["score_items"][0]["status"] == "evidence_insufficient"
    assert result["score_items"][0]["score"] is None


def test_technical_deviation_does_not_assume_business_file_is_technical_file(tmp_path):
    result = run_objective_scoring(
        make_technical_deviation_rule(),
        FileMetadata("商务投标文件.docx", 1, str(tmp_path / "bid.docx")),
    )

    assert result["score_items"][0]["status"] == "file_scope_missing"
    assert result["score_items"][0]["score"] is None
```

- [x] **Step 2: Run the tests and confirm each fails before adding handlers**

Run: `pytest -q tests/test_objective_scoring.py -k "team or performance or price or project_manager or technical"`

Expected: FAIL with missing handler behavior or incorrect placeholder statuses.

- [x] **Step 3: Implement the minimal deterministic handlers**

Implement:

- Team bracket formula: `>=35 -> 5`, `25..34 -> 3`, `15..24 -> 1`, `<=14 -> 0`, only from verified member facts.
- Similar case 1: require qualification rows to be explicitly identified and valid; exclude them; count only valid additional cases, capped at 5.
- Similar case 2: require the same validity/exclusion mapping; sum only valid non-qualification amounts and apply `>=1900 -> 5`, `>=1500 -> 3`, `>=1000 -> 1`, otherwise `0`.
- Project manager: require all named conditions; only all confirmed pass yields 5, any confirmed fail yields 0, unresolved required evidence yields `evidence_insufficient` with no score.
- Technical deviation: require an explicit technical response artifact and verified deviation count; otherwise `file_scope_missing` or `evidence_insufficient`.
- Personnel stability: require the requested commitment and verified attrition ratio; apply the original thresholds only when the fact is complete.
- Bad behavior: `external_data_required`, no invented zero score.
- Price: `other_bidder_data_required` unless a complete multi-bidder input is explicitly supplied; no simulated bidders.

The current real business bid does not contain technical, project-manager, team, stability, or price evidence. Those items must remain unavailable for their specific status reasons. Existing performance evidence must be attached to items 010 and 011 without summing uncertain/failing contracts.

- [x] **Step 4: Run focused tests and all tests for the module**

Run: `pytest -q tests/test_objective_scoring.py`

Expected: PASS, including the assertions that evidence insufficiency never becomes zero and the qualification-performance mapping is retained.

- [x] **Step 5: Commit the deterministic handlers**

```bash
git add app/objective_scoring.py tests/test_objective_scoring.py
git commit -m "feat: execute objective evaluation rules safely"
```

### Task 4: 接入评标工作流并验证产出物写入

**Files:**
- Modify: `app/workflow.py`
- Modify: `app/api.py`
- Modify: `tests/test_workflow.py`
- Modify: `tests/test_api.py` only if the API default workflow wiring needs a direct assertion.

**Interfaces:**
- Extend `BidCheckServices` with optional `score_objective_with_recorder: Callable[..., dict[str, Any]] | None = None`.
- The callback signature is `callback(tender_file, bid_file, evaluation_result, recorder=recorder) -> dict[str, Any]`.
- Evaluation mode calls the callback after successful or cached rule extraction, then completes the repository with both `evaluation_rules` and `objective_scores`.

- [x] **Step 1: Write a failing workflow test**

```python
def test_evaluation_workflow_runs_objective_scoring_after_rule_extraction(
    task_repository, tmp_path
):
    calls = []
    rules = evaluation_result()
    scores = {"score_items": [], "stats": {"objective_item_count": 0}}

    def evaluate(tender_file, recorder=None):
        calls.append(("evaluate", tender_file.filename))
        return rules

    def score(tender_file, bid_file, evaluation_rules, recorder=None):
        calls.append(("score", tender_file.filename, bid_file.filename))
        assert evaluation_rules is rules
        return scores

    services = BidCheckServices(
        extract=lambda _: empty_objects(),
        parse=lambda _: {"status": "unused"},
        review=lambda *_: {"status": "unused"},
        extract_evaluation_with_recorder=evaluate,
        score_objective_with_recorder=score,
    )
    task = create_evaluation_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    completed = task_repository.get(task.task_id)
    assert completed.result["objective_scores"] == scores
    assert calls == [
        ("evaluate", "招标文件.docx"),
        ("score", "招标文件.docx", "投标文件.docx"),
    ]
```

- [x] **Step 2: Run the workflow test and verify it fails because no scoring callback is invoked**

Run: `pytest -q tests/test_workflow.py::test_evaluation_workflow_runs_objective_scoring_after_rule_extraction`

Expected: FAIL because `objective_scores` is absent and the score callback is not called.

- [x] **Step 3: Implement workflow and API wiring**

In `app/workflow.py`, invoke the optional scorer after rule extraction and record a dedicated `objective_scoring` stage event. Keep old evaluation-only test behavior when the callback is not configured by storing no fabricated score result. When configured, include the result in `repository.complete` and record elapsed milliseconds in workflow stats.

In `app/api.py`, wire a callback that:

1. Resolves the real bid path and checks for existing `bid_document_cleaning/structured_document.json`.
2. Reuses the existing artifact when its source hash matches.
3. Calls the existing `clean_bid_document` adapter once only when no valid artifact exists, writing into the resolved bid directory.
4. Calls `run_objective_scoring` with the loaded document and recorder.

Do not modify page templates or the rule extraction prompt/schema. The scorer records `duplicate_parse=False` when it reused the existing artifact and records a parser fallback only when it was genuinely required.

- [x] **Step 4: Run workflow/API regression tests**

Run: `pytest -q tests/test_workflow.py tests/test_api.py tests/test_objective_scoring.py`

Expected: PASS, including the pre-existing assertion that evaluation mode does not parse the bid when no objective scorer is configured.

- [x] **Step 5: Commit workflow integration**

```bash
git add app/workflow.py app/api.py tests/test_workflow.py tests/test_api.py
git commit -m "feat: integrate objective scoring into evaluation workflow"
```

### Task 5: 真实文件验收、回归测试和结果记录

**Files:**
- Modify: `docs/superpowers/plans/2026-09-06-objective-evaluation-scoring.md` to check completed steps.
- Create: no additional production files; write runtime artifacts only under the existing task artifact directory.

**Interfaces:**
- Use the current real tender and bid task files and their resolved existing compliance artifacts.
- Read `objective_scores.json`, `workflow_summary.json`, and `execution.jsonl` as the acceptance evidence.

- [x] **Step 1: Run all automated tests**

Run: `pytest -q`

Expected: PASS with no regressions in extraction, artifact recording, workflow, API, template, attachment, performance, and bid parsing tests.

- [x] **Step 2: Run the evaluation workflow on the same real tender and bid**

Use the existing task files under the current task data directory, preserving the file-hash-linked `structured_document.json` and compliance artifacts. Do not delete or overwrite unrelated artifacts. Confirm the evaluation rules cache may be hit but the objective scorer still executes.

- [x] **Step 3: Verify the objective artifact**

Check that `objective_scores.json` contains exactly 8 objective items, category/name/full-score fields, original rules, explicit statuses, facts, calculations, evidence, related artifacts, and `b0277` rule source blocks where present. Check that no subjective score or veto execution result was added.

- [x] **Step 4: Verify business-critical acceptance conditions**

Confirm from the artifact and logs:

- qualification performance is not counted as an additional case or amount;
- project-manager scoring does not rely on PMP alone;
- team scoring does not rely on roster row count;
- missing technical bid does not receive 5 points;
- single-bid price is not calculated;
- evidence insufficiency is not written as 0;
- no extra LLM call or duplicate MinerU parse occurred.

- [x] **Step 5: Record the final evidence in the completion response**

Report the 8 item names and statuses, any safely calculated scores, exact reasons for unavailable scores, reused artifacts, LLM call count, duplicate-parse result, test command/result, and the absolute path to the complete `objective_scores.json`.
