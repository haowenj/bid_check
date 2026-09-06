# 否决性规则执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于 `11_evaluation_rules.json` 和已有投标文件检查产物，逐条执行正式否决规则并生成可审计的 `veto_rule_reviews.json`，不误把普通问题升级为否决。

**Architecture:** 新增一个无 LLM、无 OCR、无文档解析副作用的确定性执行模块，复用 `objective_scoring.load_reusable_bid_evidence` 读取哈希校验后的投标结构化文档和 `08/09/10` 系列检查产物。执行模块按规则原文识别证据依赖、按具体事实执行保守谓词，并通过 `ComplianceExtractionRecorder` 写入独立产出物；评标工作流在客观评分后调用它，默认 API 只在确实没有可复用解析产物时沿用一次现有解析回退。

**Tech Stack:** Python 3.14、pytest、FastAPI、现有 `BidCheckWorkflow`、`ComplianceExtractionRecorder`、JSON 产出物。

**Spec:** `docs/superpowers/specs/2026-09-06-veto-rule-execution-design.md`

## Global Constraints

- 在当前 `main` 工作区直接修改，不创建分支、不创建新工作区、不使用子代理。
- 只执行 `11_evaluation_rules.json` 中正式 `veto_rules`；不做主观评分、最终总分、排名或中标候选人推荐。
- 只有明确证据证明触发条件成立时才返回 `triggered`；证据不足不得默认为 `not_triggered`。
- 不把任何上游 `overall_status = fail` 直接映射为否决；必须消费与当前规则直接对应的具体 issue/fact。
- 优先复用 `structured_document.json`、`08_template_text_reviews.json`、`09_attachment_reviews.json`、`10_file_requirement_reviews.json`、`10_performance_reviews.json` 和 `objective_scores.json`。
- 本阶段不新增 LLM、OCR、外部网站、供应商系统、其他投标人数据或页面展示能力。
- 产出物必须保留规则原文、招标来源 block、投标 block/image、事实、上游产物引用、依赖标记和审计原因。

---

### Task 1: 建立否决执行器的失败测试和公共产出契约

**Files:**
- Create: `tests/test_veto_rule_execution.py`
- Modify: `app/models.py` only if a public type alias is useful; otherwise leave unchanged.

**Interfaces:**
- Tests define the public function:
  `run_veto_rule_execution(evaluation_rules: Mapping[str, Any], bid_file: FileMetadata, *, bid_document: Mapping[str, Any] | None = None, artifact_dir: Path | None = None, existing_artifacts: Mapping[str, Any] | None = None, objective_scores: Mapping[str, Any] | None = None, recorder: ComplianceExtractionRecorder | None = None) -> dict[str, Any]`.
- The result has `schema_version`, `source`, `veto_rule_reviews`, and `stats`.
- Each review has original rule fields, `tender_rule_source`, `status`, `triggered`, `facts_required`, `confirmed_facts`, `reason`, `evidence`, `bid_evidence`, `related_artifacts`, `dependencies`, `parent_rule_ids`, and `triggered_by`.

- [ ] **Step 1: Write the failing tests for one-record-per-formal-rule and safe defaults**

```python
def test_veto_execution_keeps_each_formal_rule_and_excludes_uncertain_rules(tmp_path):
    result = run_veto_rule_execution(
        make_rules(
            veto_rules=[make_rule("veto_001", "初步评审不通过", "有一项正式评审项不符合即否决")],
            uncertain_rules=[{"id": "uncertain_001", "description": "未结构化"}],
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
                {"status": "fail", "issues": [{"type": "missing_fill", "reason": "普通模板字段未填写"}]}
            ]
        }
    }
    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "初步评审不通过", "有一项正式评审项不符合即否决")]),
        bid_file(tmp_path),
        existing_artifacts=artifacts,
    )

    review = result["veto_rule_reviews"][0]
    assert review["status"] != "triggered"
    assert "普通" in review["reason"] or "正式" in review["reason"]
```

- [ ] **Step 2: Run the focused tests and verify the failure is due to the missing module/function**

Run: `pytest -q tests/test_veto_rule_execution.py`

Expected: FAIL with an import or missing-function error for `app.veto_rule_execution.run_veto_rule_execution`.

- [ ] **Step 3: Add fixture helpers that model only real structured evidence**

In `tests/test_veto_rule_execution.py`, define helpers with these exact shapes:

```python
def make_rule(rule_id: str, name: str, trigger: str, *, original: str | None = None) -> dict[str, Any]:
    return {
        "id": rule_id,
        "name": name,
        "trigger_condition": trigger,
        "consequence": "否决其投标",
        "additional_consequence": None,
        "evidence_requirements": [],
        "original_rule": original or f"{trigger}，否决其投标。",
        "source": {"section": "初步评审", "block_ids": ["tender-b1"], "source_text": original or trigger},
    }


def make_rules(*, veto_rules: list[dict[str, Any]], uncertain_rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "source_sections": [],
        "score_categories": [],
        "score_items": [],
        "veto_rules": veto_rules,
        "uncertain_rules": uncertain_rules or [],
        "stats": {"veto_rule_count": len(veto_rules)},
    }
```

- [ ] **Step 4: Commit the red tests**

```bash
git add tests/test_veto_rule_execution.py
git commit -m "test: define veto rule execution safety contract"
```

### Task 2: Implement evidence loading, common review records, and independent artifact output

**Files:**
- Create: `app/veto_rule_execution.py`
- Modify: `tests/test_veto_rule_execution.py`

**Interfaces:**
- Import `load_reusable_bid_evidence` from `app.objective_scoring` instead of duplicating hash/path loading logic.
- Define `VETO_RULE_REVIEW_ARTIFACT = "veto_rule_reviews.json"` and `VETO_STATUSES` containing exactly `triggered`, `not_triggered`, `evidence_insufficient`, `file_scope_missing`, `external_data_required`, `other_bidder_data_required`, and `manual_review_required`.
- `run_veto_rule_execution` never invokes a parser, LLM, OCR engine, network client, or `overall_status` aggregator.

- [ ] **Step 1: Add failing tests for artifact reuse and audit fields**

```python
def test_veto_execution_reuses_hash_verified_artifacts_and_writes_independent_json(tmp_path):
    bid = bid_file(tmp_path)
    cleaning_dir = tmp_path / "bid_document_cleaning"
    cleaning_dir.mkdir()
    write_structured_document(cleaning_dir / "structured_document.json", Path(bid.storage_path))
    write_json(
        tmp_path / "compliance_extraction" / "09_attachment_reviews.json",
        {"attachment_reviews": [], "stats": {}},
    )
    recorder = ComplianceExtractionRecorder(tmp_path)

    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "材料缺失", "未提供营业执照")]),
        bid,
        recorder=recorder,
    )

    assert result["source"]["bid_document_hash_verified"] is True
    assert "09_attachment_reviews.json" in result["source"]["reused_artifacts"]
    assert result["source"]["evaluation_rules_artifact"] == "11_evaluation_rules.json"
    assert result["stats"]["llm_total_calls"] == 0
    assert (tmp_path / "compliance_extraction" / "veto_rule_reviews.json").is_file()
    review = result["veto_rule_reviews"][0]
    assert review["tender_rule_source"]["block_ids"] == ["tender-b1"]
    assert "related_artifacts" in review
```

- [ ] **Step 2: Run the test and confirm it fails before implementation**

Run: `pytest -q tests/test_veto_rule_execution.py::test_veto_execution_reuses_hash_verified_artifacts_and_writes_independent_json`

Expected: FAIL because the executor and artifact schema do not exist.

- [ ] **Step 3: Implement the minimal common result and source loader**

Implement `_review_base(rule)` to copy every extracted rule field and initialize:

```python
{
    "status": "evidence_insufficient",
    "triggered": False,
    "facts_required": [],
    "confirmed_facts": [],
    "reason": "",
    "evidence": [],
    "bid_evidence": {"block_ids": [], "image_ids": []},
    "related_artifacts": [],
    "dependencies": {
        "external_data_required": False,
        "other_bidder_data_required": False,
        "manual_review_required": False,
    },
    "parent_rule_ids": [],
    "triggered_by": [],
}
```

Normalize source values through a helper that always returns `section`, `block_ids`, and `source_text`. Build top-level `source` from the evidence loader, include `objective_scores_artifact` only when objective results were supplied, and include missing artifact names. Set `stats` with formal count, triggered count, status counts, `llm_total_calls=0`, `ocr_reused=True` when evidence was loaded from an existing artifact, `duplicate_parse=False`, and elapsed time.

- [ ] **Step 4: Run the focused test and the whole new test file**

Run: `pytest -q tests/test_veto_rule_execution.py`

Expected: PASS for rule preservation, ordinary-fail safety, provenance, and independent artifact writing.

- [ ] **Step 5: Commit the artifact core**

```bash
git add app/veto_rule_execution.py tests/test_veto_rule_execution.py
git commit -m "feat: add veto rule review artifact core"
```

### Task 3: Implement conservative rule classification, evidence predicates, and parent-child relations

**Files:**
- Modify: `app/veto_rule_execution.py`
- Modify: `tests/test_veto_rule_execution.py`

**Interfaces:**
- Internal dispatch is based on rule text (`name`, `trigger_condition`, `original_rule`, and source section), not hard-coded real-file rule IDs.
- Helpers return `(status, reason, facts, evidence, related_artifacts, dependency_flags)` and never infer a fail from missing data.
- A `triggered` result must have at least one concrete confirmed fact, one related artifact, one tender source block, and one bid evidence block or image reference; otherwise downgrade to `evidence_insufficient`.

- [ ] **Step 1: Write failing tests for every high-risk status boundary**

```python
def test_business_only_file_does_not_trigger_star_rule(tmp_path):
    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "★技术条款", "任一★技术条款不满足即否决")]),
        bid_file(tmp_path, name="商务投标文件.docx"),
        bid_document=business_only_document(),
    )
    assert result["veto_rule_reviews"][0]["status"] == "file_scope_missing"


def test_low_price_requires_evaluation_process_and_is_not_auto_triggered(tmp_path):
    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "低于成本报价", "投标报价可能低于成本且不能合理说明的，否决投标")]),
        bid_file(tmp_path),
        bid_document=document_with_text("投标报价为最低价"),
    )
    assert result["veto_rule_reviews"][0]["status"] == "manual_review_required"
    assert result["veto_rule_reviews"][0]["triggered"] is False


def test_collusion_requires_other_bidder_data(tmp_path):
    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "串通投标", "投标文件异常一致的，否决投标")]),
        bid_file(tmp_path),
    )
    assert result["veto_rule_reviews"][0]["status"] == "other_bidder_data_required"


def test_external_supplier_record_requires_external_data(tmp_path):
    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "不良行为", "存在供应商不良行为记录的，否决投标")]),
        bid_file(tmp_path),
    )
    assert result["veto_rule_reviews"][0]["status"] == "external_data_required"


def test_inconsistent_performance_amount_is_not_fraud_veto(tmp_path):
    artifacts = {
        "10_performance_reviews.json": {
            "performance_reviews": [{
                "status": "fail",
                "checks_by_key": {"table_amount_consistency": {"status": "fail", "reason": "业绩表金额与合同金额不一致"}},
            }],
            "stats": {},
        }
    }
    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "弄虚作假", "提供虚假业绩材料的，否决投标")]),
        bid_file(tmp_path),
        existing_artifacts=artifacts,
    )
    assert result["veto_rule_reviews"][0]["status"] in {"evidence_insufficient", "manual_review_required"}
    assert result["veto_rule_reviews"][0]["triggered"] is False


def test_non_substantive_threshold_counts_only_explicit_failures(tmp_path):
    artifacts = {"08_template_text_reviews.json": {"template_text_reviews": [
        {"status": "fail", "issues": [{"type": "non_substantive_deviation", "status": "fail", "reason": "非实质性条款第1项不满足"}]},
        {"status": "uncertain", "issues": [{"type": "non_substantive_deviation", "status": "uncertain", "reason": "第2项待确认"}]},
    ]}}
    result = run_veto_rule_execution(
        make_rules(veto_rules=[make_rule("veto_001", "非实质性条款阈值", "非实质性条款超过10项不满足的，视为实质性不满足")]),
        bid_file(tmp_path),
        existing_artifacts=artifacts,
    )
    review = result["veto_rule_reviews"][0]
    assert review["status"] != "triggered"
    assert review["confirmed_facts"][0]["counted_failure_count"] == 1
```

- [ ] **Step 2: Run these tests and confirm they fail for missing classification/predicate behavior**

Run: `pytest -q tests/test_veto_rule_execution.py -k "star or low_price or collusion or external or fraud or threshold"`

Expected: FAIL with conservative default results rather than the required specific statuses or facts.

- [ ] **Step 3: Implement text-signaled dependency classification**

Check explicit signals in the combined rule text in this order:

1. Other-bidder signals: `串通投标`, `异常一致`, `关联投标`, `投标人之间`, `多个投标人`.
2. External signals: `信用中国`, `裁判文书`, `供应商不良行为`, `外部系统`, `处罚记录`.
3. Manual/process signals: `低于成本`, `算术修正`, `接受修正`, `评标委员会认定`, `澄清说明`, `现场认定`.
4. Technical scope signals: `技术标`, `技术规范`, `技术响应`, `★`.
5. Threshold signals: `非实质性`, `超过` plus a parseable Arabic or Chinese number.
6. Preliminary aggregate signals: `初步评审`, `资格审查`, `形式评审`, `响应性评审`, plus `有一项`, `任一项`, `不通过`.
7. False-material signals: `虚假`, `弄虚作假`, `伪造`.

The classifier must not treat a generic `fail`, `不一致`, `未找到`, or `可能` as a direct veto signal.

- [ ] **Step 4: Implement the specific safe predicates**

Implement these concrete results:

- Other-bidder rules return `other_bidder_data_required` with `dependencies.other_bidder_data_required=True` and no evidence-based trigger.
- External rules return `external_data_required` with `dependencies.external_data_required=True`.
- Low-cost and arithmetic-correction rules return `manual_review_required` when the required post-upload/committee process facts are absent.
- Star rules return `file_scope_missing` when `_is_business_only` is true; otherwise require a complete technical-response review and an explicit matching fail before triggering.
- Threshold rules count only issue entries whose type/reason explicitly identifies a non-substantive deviation and whose status is `fail`; any missing or uncertain coverage prevents `not_triggered` and prevents a trigger unless completeness is explicit.
- False-material rules treat inconsistency as a fact only; absent a direct explicit false-material finding with traceable evidence, return `evidence_insufficient` or `manual_review_required`.
- Generic direct material/signature/qualification rules can trigger only when the rule text names the same material/condition and an existing attachment or file-requirement entry has a matching explicit `fail` with evidence.

- [ ] **Step 5: Implement preliminary aggregate and duplicate-safe relation logic**

Build a stable relation map from rule text/source section. A rule is a child candidate when its condition names a concrete preliminary-review item; an aggregate rule is a parent candidate when it says any/one preliminary item fails. Populate `parent_rule_ids` and `triggered_by` only for high-confidence textual matches. The parent can be `triggered` only when a child review is already `triggered`; if any required formal review area is uncovered, keep `evidence_insufficient`; never produce a second independent evidence cause.

- [ ] **Step 6: Add the audit-chain invariant and run the focused suite**

Before returning each review, enforce:

```python
if review["status"] == "triggered":
    assert review["confirmed_facts"]
    assert review["related_artifacts"]
    assert review["tender_rule_source"]["block_ids"]
    assert review["bid_evidence"]["block_ids"] or review["bid_evidence"]["image_ids"]
```

If an invariant is not met, return `evidence_insufficient` with an explicit audit-chain reason. Run: `pytest -q tests/test_veto_rule_execution.py`.

Expected: PASS for all status boundaries, no false fraud/low-price/collusion/star triggers, threshold counting, and parent-child relation fields.

- [ ] **Step 7: Commit deterministic rule execution**

```bash
git add app/veto_rule_execution.py tests/test_veto_rule_execution.py
git commit -m "feat: execute veto rules conservatively"
```

### Task 4: Integrate the executor into evaluation workflow and default API services

**Files:**
- Modify: `app/workflow.py`
- Modify: `app/api.py`
- Modify: `tests/test_workflow.py`
- Modify: `tests/test_api.py` only for direct default-service wiring coverage.

**Interfaces:**
- Extend `BidCheckServices` with optional `execute_veto_with_recorder: Callable[..., dict[str, Any]] | None = None`.
- Callback signature: `callback(tender_file, bid_file, evaluation_result, objective_scores=None, recorder=recorder) -> dict[str, Any]`.
- Evaluation-mode completion includes `evaluation_rules`, optional `objective_scores`, and optional `veto_rule_reviews`.

- [ ] **Step 1: Write a failing workflow test for ordering and persistence**

```python
def test_evaluation_workflow_runs_veto_execution_after_objective_scoring(
    task_repository, tmp_path
):
    calls = []
    rules = evaluation_result()
    scores = {"score_items": [], "stats": {"objective_item_count": 0}}
    veto = {"veto_rule_reviews": [], "stats": {"formal_rule_count": 0}}

    def evaluate(tender_file, recorder=None):
        calls.append("evaluate")
        return rules

    def score(tender_file, bid_file, evaluation_rules, recorder=None):
        calls.append("objective")
        return scores

    def execute(tender_file, bid_file, evaluation_rules, objective_scores=None, recorder=None):
        calls.append("veto")
        assert evaluation_rules is rules
        assert objective_scores is scores
        return veto

    services = BidCheckServices(
        extract=lambda _: empty_objects(),
        parse=lambda _: {"status": "unused"},
        review=lambda *_: {"status": "unused"},
        extract_evaluation_with_recorder=evaluate,
        score_objective_with_recorder=score,
        execute_veto_with_recorder=execute,
    )
    task = create_evaluation_task(task_repository, tmp_path)
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    completed = task_repository.get(task.task_id)
    assert completed.result["veto_rule_reviews"] == veto
    assert calls == ["evaluate", "objective", "veto"]
```

- [ ] **Step 2: Run the test and verify it fails because the callback is not wired**

Run: `pytest -q tests/test_workflow.py::test_evaluation_workflow_runs_veto_execution_after_objective_scoring`

Expected: FAIL because the service field and workflow invocation do not exist.

- [ ] **Step 3: Implement workflow invocation and stats/events**

In evaluation mode, after the optional objective callback succeeds, invoke the optional veto callback with the objective result. Record `workflow.stage.start/end` using `stage="veto_rule_execution"`, store `veto_rule_execution_elapsed_ms`, and include `veto_rule_reviews` in `repository.complete`. Preserve current behavior when the callback is absent. On callback failure, fail the task at the review stage and write the workflow summary.

- [ ] **Step 4: Add the default API callback without duplicate parsing**

In `app/api.py`, import `run_veto_rule_execution`. Add a callback that:

1. Calls `load_reusable_bid_evidence(bid_file)`.
2. If the verified document exists, passes it and its artifact directory to the executor.
3. If it does not exist, calls the existing `clean_bid_document` adapter once, reloads the evidence, and passes the fallback flag/diagnostics through the result source.
4. Passes `objective_scores` to the executor so the relation/evidence source can cite `objective_scores.json` without treating score status as a veto.
5. Never invokes an LLM or OCR path directly.

Do not modify `app/templates/bid_check_task.html` or CSS in this task.

- [ ] **Step 5: Run workflow/API regression tests**

Run: `pytest -q tests/test_workflow.py tests/test_api.py tests/test_veto_rule_execution.py tests/test_objective_scoring.py`

Expected: PASS, including the old evaluation test where no scorer/callback is configured, objective scoring ordering, veto artifact persistence, and no duplicate parse when artifacts exist.

- [ ] **Step 6: Commit workflow integration**

```bash
git add app/workflow.py app/api.py tests/test_workflow.py tests/test_api.py
git commit -m "feat: integrate veto rule execution into evaluation workflow"
```

### Task 5: Complete full verification and current-real-artifact acceptance

**Files:**
- Modify: `docs/superpowers/plans/2026-09-06-veto-rule-execution.md` to check completed steps and record verification evidence.
- Create/modify runtime files only under the existing task artifact directory when a real task is available.

**Interfaces:**
- The final independent artifact is `compliance_extraction/veto_rule_reviews.json` beside `11_evaluation_rules.json` and `objective_scores.json`.
- No page/UI files are changed in this phase.

- [ ] **Step 1: Run the complete test suite**

Run: `pytest -q`

Expected: exit code 0 and zero failures.

- [ ] **Step 2: Locate the existing real tender/bid artifacts without changing files**

Search only known task/data locations for `11_evaluation_rules.json`, `structured_document.json`, `08_template_text_reviews.json`, `09_attachment_reviews.json`, `10_file_requirement_reviews.json`, and `10_performance_reviews.json`. Do not create a synthetic “real” task if the current real task artifacts are unavailable.

- [ ] **Step 3: Execute the same real tender and business bid through the existing evaluation workflow**

Confirm the artifact contains exactly the 12 formal rules, one review per rule, explicit status distribution, and no page changes. Capture for each `triggered` item the full tender-rule → fact → upstream artifact → block/image chain.

- [ ] **Step 4: Verify the acceptance checklist from the request**

Check and report:

```text
formal rule count = 12
triggered count and every triggered evidence chain
automatic / evidence-insufficient / file-scope-missing / external / other-bidder / manual counts
ordinary fail was not upgraded
missing file scope was not treated as non-compliance
performance inconsistency was not treated as fraud
low price was not treated as below-cost
single bid was not used for collusion
incomplete ★ coverage was not treated as satisfied
preliminary aggregate relation is explicit and not double-counted
reused artifacts, new LLM calls, OCR calls, and parse calls
```

- [ ] **Step 5: Run final diff and status checks before claiming completion**

Run: `git diff --check && git status --short && git log -5 --oneline`

Expected: no whitespace errors; only the intended implementation, tests, plan, and prior design commit are present; no branch/worktree/subagent artifacts were created.

