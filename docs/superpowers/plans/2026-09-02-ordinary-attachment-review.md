# 普通附件全量检查 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 3 类附件检查扩展为全部已匹配普通附件模板，并在当前真实招标/投标任务上完成可追踪的全量执行。

**Architecture:** 在 `app/attachment_review.py` 增加确定性的模板附件候选筛选器，保留现有多模态 prompt、JSON 解析、图片章节索引、并发和重试执行器；运行器只把“匹配成功且存在普通材料要求”的模板转换为检查 job，并以原始模板顺序收集结果。页面继续消费现有 `attachment_reviews` 结果，仅去除固定 3 类文案并补充证据图片数量。

**Tech Stack:** Python 3.11、pytest、FastAPI/Jinja2、现有 `ThreadPoolExecutor`、OpenAI-compatible multimodal client、任务级 JSON recorder 和 SQLite repository。

**Spec:** `docs/superpowers/specs/2026-09-02-ordinary-attachment-review-design.md`

## Global Constraints

- 使用完整模板 `body/source.source_text` 作为附件要求主要依据，`attachments` 只作辅助信息。
- 不修改招标解析、投标解析、模板匹配流程，不重写现有附件检查 system/user prompt 或 JSON schema。
- 独立图片和表格 HTML 内嵌图片只能通过结构化章节关联进入检查器，不重新扫描整份投标文件。
- 明确排除 `21` 及 `21.1`～`21.6` 业绩复杂附件，不实现合同内部审查。
- 不执行 project requirements、文件大小、CA、加密、平台提交状态、真实性或最终综合合规判断。
- 保持 3 路并发、单模板一次调用、单项失败隔离、现有一次重试策略和模板原始顺序。
- 测试负样本只能使用临时输入图片集合，不能修改真实 structured document、图片资产或正式结果。

---

### Task 1: Build generic attachment-candidate selection

**Files:**
- Modify: `app/attachment_review.py:150-175, 938-981`
- Test: `tests/test_attachment_review.py`

**Interfaces:**
- Produces `template_has_attachment_requirement(template: dict[str, Any]) -> bool` for deterministic candidate selection.
- Produces `is_complex_attachment_scope(template: dict[str, Any], bid_section: dict[str, Any] | None) -> bool` for the explicit 21/21.x exclusion.
- Keeps `attachment_case_kind(template_name)` as a compatibility helper, but allows ordinary template names instead of a three-name allowlist.

- [ ] **Step 1: Write failing tests for selection semantics**

Add tests that call the candidate helpers and the existing runner with a recording fake LLM:

```python
def test_attachment_candidate_uses_full_template_body_when_attachments_are_empty():
    template = _template("软件配置", "应提供相关软件的合法使用权证明。")
    template["attachments"] = []
    assert template_has_attachment_requirement(template) is True


def test_attachment_candidate_does_not_use_attachment_placeholder_alone():
    template = _template("投标函", "本页填写投标函正文。")
    template["attachments"] = ["身份证复印件"]
    assert template_has_attachment_requirement(template) is False


def test_conditional_attachment_requirement_is_a_candidate_without_local_fail_rule():
    template = _template("软件说明", "如采用第三方软件，应提供合法使用权证明；不涉及则无需提供。")
    assert template_has_attachment_requirement(template) is True


def test_21_and_21_x_are_excluded_from_attachment_jobs():
    assert is_complex_attachment_scope({}, {"title": "21 业绩情况表"}) is True
    assert is_complex_attachment_scope({}, {"title": "21.3 合同关键页"}) is True
    assert is_complex_attachment_scope({}, {"title": "20 基本开户银行情况"}) is False
```

Expected first run: FAIL because the generic helpers do not exist and the current runner only recognizes the three aliases.

- [ ] **Step 2: Run the selection tests and verify the failure is about missing generic behavior**

Run:

```bash
pytest -q -o filterwarnings='' tests/test_attachment_review.py -k 'candidate or complex_attachment_scope'
```

Expected: FAIL with missing helper/import or the current three-case selection not selecting the generic template.

- [ ] **Step 3: Implement deterministic full-body candidate detection**

Add a small set of compiled Chinese material-requirement patterns and a helper that reads both `template["body"]` and `template["source"]["source_text"]`, removes duplicate text, and checks phrases such as `应附`, `应提供`, `提供复印件`, `提供扫描件`, `提供证明文件`, `提供相关资料及证明`, `提供使用权证明`, `提供身份证明`, and `提供开户证明`. Use `attachments` only when composing the existing prompt; do not make an attachments-only template a candidate.

Implement the explicit scope boundary by normalizing the matched bid section title and returning true for a leading section number `21` or `21.<number>`; do not inspect or enumerate material names.

Update `attachment_case_kind` so a non-empty template name returns its normalized name when it is not one of the existing aliases, while retaining existing alias normalization.

- [ ] **Step 4: Run the selection tests and the existing attachment tests**

Run:

```bash
pytest -q -o filterwarnings='' tests/test_attachment_review.py -k 'candidate or complex_attachment_scope or attachment_review'
```

Expected: PASS, with no change yet to the real execution count beyond the tests covered by the next task.

- [ ] **Step 5: Commit the selection unit**

```bash
git add app/attachment_review.py tests/test_attachment_review.py
git commit -m "feat: detect ordinary attachment requirements generically"
```

### Task 2: Expand the runner and add negative/conditional regression coverage

**Files:**
- Modify: `app/attachment_review.py:938-1048`
- Test: `tests/test_attachment_review.py`

**Interfaces:**
- `run_attachment_review` continues to return a dictionary with `mode="attachments"`, an ordered `attachment_reviews` list, and the existing `stats` dictionary with the existing per-template result fields.
- The job builder consumes matched comparisons and the generic candidate/scope helpers; `_review_one_attachment` remains the single-template multimodal call boundary.

- [ ] **Step 1: Write failing tests for all ordinary jobs, stable order, conditions, and failures**

Add a temporary document fixture with matched sections for generic evidence modules, a conditional software module, a pure-text module, and 21/21.3 modules. Add a fake LLM that records calls and returns schema-valid results, plus a fake that raises for one template. Assert:

```python
def test_runner_checks_all_matched_ordinary_templates_in_template_order():
    result = run_attachment_review(
        {"templates": templates},
        {"structured_document": document, "artifact_dir": str(tmp_path)},
        llm=RecordingAttachmentLLM(),
    )
    assert [item["template_name"] for item in result["attachment_reviews"]] == [
        "营业执照材料", "软件使用权说明", "开户证明材料",
    ]
    assert result["stats"]["selected_template_count"] == 3


def test_runner_does_not_call_model_for_pure_text_or_21_x_templates():
    llm = RecordingAttachmentLLM()
    result = run_attachment_review(
        {"templates": templates},
        {"structured_document": document, "artifact_dir": str(tmp_path)},
        llm=llm,
    )
    assert all("投标函" not in call[1] for call in llm.calls)
    assert all("业绩" not in item["template_name"] for item in result["attachment_reviews"])


def test_runner_keeps_one_model_failure_isolated_from_other_attachment_jobs():
    result = run_attachment_review(
        {"templates": templates},
        {"structured_document": document, "artifact_dir": str(tmp_path)},
        llm=FailOneAttachmentLLM("营业执照材料"),
    )
    failed = next(item for item in result["attachment_reviews"] if item["template_name"] == "营业执照材料")
    assert failed["execution_status"] == "failed"
    assert result["stats"]["llm_failed_count"] == 1
    assert len(result["attachment_reviews"]) == 3
```

For conditional requirements, assert the candidate is called with the complete template and module text, and that the runner does not synthesize a `fail` merely because its image list is empty; the fake response controls the business status.

- [ ] **Step 2: Run these tests to verify the current three-case allowlist fails**

Run:

```bash
pytest -q -o filterwarnings='' tests/test_attachment_review.py -k 'runner_checks_all or runner_does_not_call or runner_keeps_one_model_failure or conditional'
```

Expected: FAIL because generic ordinary templates are currently skipped and the runner does not apply the 21/21.x scope boundary.

- [ ] **Step 3: Change only job selection in `run_attachment_review`**

For each template/comparison pair in the existing template order, require `comparison["status"] == "matched"`, a materialized bid section, `template_has_attachment_requirement(template)`, and `not is_complex_attachment_scope(template, bid_section)`. Use the template’s normalized name as `case_type`; do not change `build_attachment_review_user_prompt`, `_parse_attachment_review_result`, `_review_one_attachment`, retry markers, or `ATTACHMENT_REVIEW_MAX_WORKERS`.

Keep `executions_by_index` indexed by job order so `as_completed` cannot reorder output. Keep empty image lists valid inputs to the existing prompt and model call. Preserve `execution_status=failed` for call failures and keep business `status=fail` reserved for validated model conclusions.

- [ ] **Step 4: Run the focused runner tests and all attachment tests**

Run:

```bash
pytest -q -o filterwarnings='' tests/test_attachment_review.py -k 'runner_checks_all or runner_does_not_call or runner_keeps_one_model_failure or conditional'
pytest -q -o filterwarnings='' tests/test_attachment_review.py
```

Expected: PASS; the fake should observe one call per selected template, not one call per image, and results must remain in template order.

- [ ] **Step 5: Add and run temporary negative evidence tests**

Deep-copy the fixture document inside each test, remove one identity image from the copy and remove the corresponding ID from the module’s temporary image set; use a fake that returns a schema-valid `fail` with a requirement describing the missing identity side. In a separate test remove the bank table’s `image_ids` and its unified image record from the copy; assert the forced bank-proof requirement returns `fail`. Do not write either copy to the real task directory.

Run:

```bash
pytest -q -o filterwarnings='' tests/test_attachment_review.py -k 'missing_identity_side or missing_bank_proof'
```

Expected: PASS with explicit missing-component/material evidence in `requirements`, and no mutation of the original fixture document.

- [ ] **Step 6: Commit the runner unit**

```bash
git add app/attachment_review.py tests/test_attachment_review.py
git commit -m "feat: run all ordinary attachment reviews"
```

### Task 3: Generalize the existing result page without redesigning it

**Files:**
- Modify: `app/templates/bid_check_task.html:90-100, 345-421`
- Test: `tests/test_pages.py`

**Interfaces:**
- Page continues to receive `comparison["attachment_review"]` from `_attach_attachment_reviews` and the existing `task.result.review_result.attachment_reviews` list.
- No new API endpoint or prompt field is introduced.

- [ ] **Step 1: Write failing page assertions**

Add a page fixture whose review result contains a generic “营业执照材料” review and a pure-text template with no attachment review. Assert the rendered HTML includes the generic review, its status, material type, and evidence image IDs/count, does not contain the old fixed phrase “只检查法定代表人/负责人身份证明、授权委托书和基本开户银行情况”, and does not render “附件检查失败” for the pure-text template.

- [ ] **Step 2: Run the page tests and verify the fixed copy causes failure**

Run:

```bash
pytest -q -o filterwarnings='' tests/test_pages.py -k 'attachment'
```

Expected: FAIL because the page currently contains the fixed three-category description and does not show a dedicated evidence-image count.

- [ ] **Step 3: Update only the page copy and compact evidence metadata**

Replace the fixed description with “仅对已匹配且明确存在普通证明材料要求的模板执行附件检查；业绩合同等复杂附件另行处理。” Add a small `证据图片：N 张` label beside each attachment review’s existing `image_ids`, leaving its materials/facts/requirements table cells and failure expansion intact.

- [ ] **Step 4: Run page tests and the full suite**

Run:

```bash
pytest -q -o filterwarnings='' tests/test_pages.py -k 'attachment'
pytest -q -o filterwarnings=''
```

Expected: PASS with all existing tests green.

- [ ] **Step 5: Commit the page unit**

```bash
git add app/templates/bid_check_task.html tests/test_pages.py
git commit -m "feat: show generic attachment review results"
```

### Task 4: Run the real full ordinary-attachment verification and close the loop

**Files:**
- Modify: `data/tasks/5f77e206-6aca-4151-a949-35d3b509be26/compliance_extraction/09_attachment_reviews.json` (generated artifact)
- Modify: `data/tasks/5f77e206-6aca-4151-a949-35d3b509be26/bid_document_cleaning/structured_document.json` only if regeneration confirms the prior table-image fix is needed
- Test: `tests/test_attachment_review.py`, full test suite

**Interfaces:**
- Use the existing task’s `07_result.json`, regenerated structured document, artifact directory, configured OpenAI-compatible attachment LLM, and `ComplianceExtractionRecorder`.
- Keep output JSON ordering equal to the original template ordering and preserve all recorder traces.

- [ ] **Step 1: Run the complete automated verification before the real call**

```bash
pytest -q -o filterwarnings=''
git diff --check
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m compileall -q app
```

Expected: all tests pass, no whitespace errors, and Python compilation succeeds.

- [ ] **Step 2: Regenerate the current structured document from the saved previous MinerU data**

Replay the existing raw/merged content through the current structural cleaner, retaining the table HTML image extractor and the existing task asset directory. Verify the current `b0349` table has a non-empty `image_ids` list and its image keeps `section_id`, `section_path`, `source_table_id`, and ready asset status.

- [ ] **Step 3: Execute the real ordinary attachment batch once**

Call `run_attachment_review` with the configured formal multimodal client and the existing `07_result.json`; use the existing recorder and repository merge path so only the attachment portion of the combined result is replaced. Set the task-specific CA bundle only if the current interpreter requires it; do not change project configuration or expose the API key.

- [ ] **Step 4: Verify and summarize the real artifact**

Read `09_attachment_reviews.json`, the structured document, and the database row. Report template count, matched count, selected ordinary count, actual LLM calls, concurrency, elapsed time, pass/fail/uncertain/execution-failed counts, every non-pass reason, image IDs per item, table-image participation, and whether 21/21.x items were excluded.

- [ ] **Step 5: Re-run the negative tests and final full suite**

```bash
pytest -q -o filterwarnings='' tests/test_attachment_review.py -k 'missing_identity_side or missing_bank_proof or candidate or runner'
pytest -q -o filterwarnings=''
```

Expected: negative tests catch missing material, real artifact remains unchanged by the negative tests, and the full suite is green.

- [ ] **Step 6: Commit only source/tests/docs changes that belong to this rollout**

Review `git status` and `git diff` so unrelated prior user changes are not staged. Commit the rollout source/tests/docs as one final commit after the real artifact verification; do not stage credentials or unrelated generated files.
