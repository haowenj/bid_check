# 主观评标评分执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于已有 `11_evaluation_rules.json` 和已解析投标文件内容，独立生成可审计的 `subjective_scores.json`，只处理主观评分项。

**Architecture:** 新增 `app/subjective_scoring.py` 作为唯一主观评分执行器，先复用并校验既有结构化投标产物，再按评分项范围裁剪 block；只有有范围且有证据的项目才各自调用一次 LLM，并发执行。通过 `BidCheckWorkflow.run_subjective()` 和独立 API 触发，不改变现有评标模式的客观评分/否决链路。

**Tech Stack:** Python 3.11+、标准库 `concurrent.futures`、`urllib.request`、现有 `ComplianceExtractionRecorder`、pytest、FastAPI。

**Spec:** `docs/superpowers/specs/2026-09-06-subjective-evaluation-scoring-design.md`

## Global Constraints

- 本阶段只处理 `evaluation_type = subjective` 的评分项，不重复执行客观评分，不执行否决规则，不计算总分、排名或中标结果。
- 优先复用现有 `structured_document.json` 和已有 `08/09/10` 检查产物，不启动 MinerU、OCR 或新的整份投标文件解析。
- 当前仅有商务标时，技术标、技术响应、技术方案、服务方案等当前文件范围不存在的内容必须返回 `file_scope_missing`，不得调用模型，也不得按 0 分处理。
- 模型只能依据评分项 `original_rule`、`conditions`、`scoring_method` 和匹配投标 block 判断，不得增加招标文件没有的评价维度。
- 每条评分理由必须对应具体投标内容和实际送模的 `block_id`；证据不足时不生成建议分。
- 主观项之间最多使用 5 个并发 worker；每个可评分项目最多一次必要的模型调用，不做自动重试。
- `recommended_score` 明确属于 AI 辅助建议分，不是确定性客观分。

### Task 1: 建立主观项筛选、文件范围和 block 匹配的可测试核心

**Files:**
- Create: `tests/test_subjective_scoring.py`
- Create: `app/subjective_scoring.py`

**Interfaces:**
- Produces `select_subjective_items(evaluation_rules) -> list[dict[str, Any]]`。
- Produces `match_subjective_bid_content(item, bid_document, bid_filename) -> dict[str, Any]`，返回 `status`、`reason`、`blocks` 和 `scope`。
- Produces评分档解析结果，供后续 LLM 响应校验使用。

- [ ] **Step 1: Write the failing tests for filtering and navigation exclusion**

```python
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
            {"block_id": "index", "type": "table", "section": "3 商务评审索引表",
             "text": "投标文件编写质量的情况 | 对应页码 22", "order": 1},
            {"block_id": "body", "type": "paragraph", "section": "13.5 评审要求承诺函",
             "text": "投标文件编写质量的情况良好，不存在材料缺失。", "order": 2},
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
        "blocks": [{"block_id": "b1", "type": "paragraph", "section": "1 商务投标文件封面",
                     "text": "商务投标文件", "order": 1}],
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
        "blocks": [{"block_id": "b1", "type": "paragraph", "section": "21 业绩情况表",
                     "text": "项目为技术服务类业绩，合同已经提供。", "order": 1}],
    }
    item = {
        "id": "score_item_004",
        "name": "云网产品开发服务、大模型技术支持服务、云网产品测试服务支撑方案",
        "evidence_requirements": ["支撑方案"],
    }

    result = match_subjective_bid_content(item, document, "商务投标文件部分.docx")

    assert result["status"] == "file_scope_missing"

```

- [ ] **Step 2: Run the focused tests and verify the expected missing-symbol failures**

Run: `pytest tests/test_subjective_scoring.py -q`

Expected: FAIL because `app.subjective_scoring` and its public functions do not exist.

- [ ] **Step 3: Implement the minimal selector and matcher**

Implement `select_subjective_items` by iterating the source `score_items` list and retaining only mappings whose `evaluation_type` equals `subjective`, preserving source order. Implement the matcher with these exact rules: read only `blocks` and section metadata; normalize whitespace for matching; exclude section/path titles containing `索引`、`目录`、`对应页码`; classify a filename containing `商务` with no technical/service-response section as business-only; decide missing technical/service scope before body keyword matching; score exact item-name and evidence-requirement matches; return at most 12 highest-scoring blocks in source order. Return `evidence_insufficient` when a non-business document has no matching blocks.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `pytest tests/test_subjective_scoring.py -q`

Expected: 4 passed.

- [ ] **Step 5: Commit the core matcher**

```bash
git add app/subjective_scoring.py tests/test_subjective_scoring.py
git commit -m "feat: match subjective scoring evidence"
```

### Task 2: Add score-band parsing, strict LLM validation, concurrency, and artifact writing

**Files:**
- Modify: `tests/test_subjective_scoring.py`
- Modify: `app/subjective_scoring.py`

**Interfaces:**
- Produces `SubjectiveScoreLLM.score(score_item, matched_bid_content, allowed_bands) -> Mapping[str, Any]`。
- Produces `OpenAICompatibleSubjectiveScoreLLM` and `DeterministicSubjectiveScoreLLM`。
- Produces `run_subjective_scoring(evaluation_rules, bid_file, *, subjective_llm, bid_document=None, artifact_dir=None, existing_artifacts=None, recorder=None) -> dict[str, Any]`，并写入 `subjective_scores.json`。

- [ ] **Step 1: Write failing tests for bands, evidence binding, concurrency, and no-call scope handling**

Add a `RecordingSubjectiveLLM` fixture with `model = "fixture"`, `available = True`, a `calls` list, and a `score()` method returning a configured response. Build a hash-matched structured document with one body block for `score_item_001` and a rules object containing one deduction item plus one technical item. Assert that `run_subjective_scoring` returns two records, one `ai_scored` and one `file_scope_missing`, makes exactly one LLM call, sends one compact block instead of the full document, and writes `schema_version == "subjective-score-v1"`.

Add a test whose fixture response uses `score_band == "[4,5]"`, `recommended_score == 6`, and an evidence block id not present in the matched content. Assert `status == "llm_error"`, `recommended_score is None`, and the uncertainty notes mention the rejected block id.

Add a barrier-backed fixture with two matched subjective items, assert both calls enter before either leaves, and assert each item is called exactly once. Add a failing fixture call and assert no retry occurs.

- [ ] **Step 2: Run focused tests and verify they fail for missing runner/validator behavior**

Run: `pytest tests/test_subjective_scoring.py -q`

Expected: FAIL because `run_subjective_scoring`, band parsing, and response validation are not implemented.

- [ ] **Step 3: Implement exact score-band parsing and response normalization**

Parse interval expressions from `original_rule`, `conditions`, and `scoring_method`, preserving labels `[4,5]`, `[2,4)`, `(0,2)` and `0`. Represent a deduction-only rule as `扣分规则`. Reject non-finite scores, scores outside `0..max_score`, interval violations, unrecognized labels, blank reasons, and evidence blocks outside the matched block set. Normalize all records to the artifact keys and set `recommended_score` to `None` for non-`ai_scored` statuses.

- [ ] **Step 4: Implement the JSON LLM client and safe local fallback**

Implement `OpenAICompatibleSubjectiveScoreLLM.score()` with one JSON request per item, `temperature=0`, `response_format={"type":"json_object"}`, and a prompt containing only the rule fields and matched blocks. Require `score_band`, `recommended_score`, `reason`, `evidence`, and `uncertainty`. Use recorder context to persist request, raw response, usage, finish reason, and elapsed time. Convert timeout, HTTP, URL, JSON, and response-shape failures to the module's local scoring exception.

Implement `DeterministicSubjectiveScoreLLM` with `available = False`; the runner must mark a scoreable item `llm_error` without calling or counting it as an external LLM call.

- [ ] **Step 5: Implement the concurrent runner and independent artifact**

`run_subjective_scoring` must call `load_reusable_bid_evidence` once, select subjective items in source order, finish scope/evidence failures before scheduling calls, schedule eligible items using at most five workers, call each eligible item once, continue after an item failure, and write `subjective_scores.json`. The artifact stats must include actual call counts and elapsed times, `new_parse_calls == 0`, `new_ocr_calls == 0`, `new_mineru_calls == 0`, `duplicate_parse == false`, `total_score_computed == false`, `ranking_computed == false`, and `veto_executed == false`. Model payloads must contain compact matched blocks only.

- [ ] **Step 6: Run focused tests and verify they pass**

Run: `pytest tests/test_subjective_scoring.py -q`

Expected: all focused tests pass, including exactly one model call for each eligible item and zero calls for scope-missing items.

- [ ] **Step 7: Commit the scorer and artifact implementation**

```bash
git add app/subjective_scoring.py tests/test_subjective_scoring.py
git commit -m "feat: execute auditable subjective scores"
```

### Task 3: Add an independent workflow method without changing the existing evaluation workflow

**Files:**
- Modify: `app/repository.py`
- Modify: `app/workflow.py`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_workflow.py`

**Interfaces:**
- Produces `BidCheckRepository.update_result(task_id, result_patch) -> BidCheckTask` without changing task stage/status columns.
- Produces `BidCheckWorkflow.run_subjective(task_id) -> None`。
- Consumes `BidCheckServices.score_subjective_with_recorder`。

- [ ] **Step 1: Write the failing repository and workflow tests**

Add a repository test that creates an evaluation task, calls `update_result("task", {"subjective_scores": {"score_items": []}})`, and asserts the new result is present while `status`, `requirements_status`, `bid_parse_status`, and `review_status` remain unchanged.

Add a workflow test with a valid `11_evaluation_rules.json` in the task artifact directory and services that append `extract`, `parse`, `review`, `objective`, `veto`, or `subjective` to a list. Call `run_subjective(task_id)` and assert the list is exactly `["subjective"]` and the repository result contains the returned `subjective_scores` object.

- [ ] **Step 2: Run focused repository/workflow tests and verify they fail**

Run: `pytest tests/test_repository.py tests/test_workflow.py -q`

Expected: FAIL because `update_result`, `score_subjective_with_recorder`, and `run_subjective` do not exist.

- [ ] **Step 3: Implement `update_result` and `run_subjective`**

Add an atomic repository update that reads the current `result_json`, merges the supplied top-level mapping, writes the JSON, and changes only `updated_at`. In `run_subjective`, create the existing recorder from the tender directory, read `evaluation_rules` from the task result or `compliance_extraction/11_evaluation_rules.json`, call only `score_subjective_with_recorder`, merge `{"subjective_scores": output}`, and emit start/end/error events. Do not call or inspect objective/veto service results and do not modify stage columns.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `pytest tests/test_repository.py tests/test_workflow.py -q`

Expected: all repository and workflow tests pass, including existing tests that assert the original evaluation workflow ordering.

- [ ] **Step 5: Commit the independent workflow entry**

```bash
git add app/repository.py app/workflow.py tests/test_repository.py tests/test_workflow.py
git commit -m "feat: add independent subjective scoring workflow"
```

### Task 4: Wire the default service and independent API result access

**Files:**
- Modify: `app/api.py`
- Modify: `tests/test_api.py`
- Modify: `app/models.py` only if a public result type is needed; keep the JSON artifact contract otherwise

**Interfaces:**
- Produces `BidCheckServices.score_subjective_with_recorder` in `build_default_workflow`.
- Produces `POST /api/bid-check/tasks/{task_id}/subjective-score` as an independent trigger.
- Produces task GET responses containing `subjective_scores` when `subjective_scores.json` exists.

- [ ] **Step 1: Write failing API tests**

Add a non-evaluation task test that posts to `/api/bid-check/tasks/task-001/subjective-score` and asserts HTTP 409 with a detail mentioning the evaluation mode. Add a task GET test that writes a minimal `subjective_scores.json` into the task's `compliance_extraction` directory and asserts the response includes its `schema_version` and `score_items`.

Add an evaluation-task fixture for the accepted trigger and assert the background call reaches `run_subjective` without invoking the existing objective/veto callbacks.

- [ ] **Step 2: Run focused API tests and verify they fail**

Run: `pytest tests/test_api.py -q`

Expected: FAIL because the new route, service wiring, and artifact loader are absent.

- [ ] **Step 3: Wire the default subjective LLM and service**

In `build_default_workflow`, construct `OpenAICompatibleSubjectiveScoreLLM` when `settings.llm_api_key` is configured and `DeterministicSubjectiveScoreLLM` otherwise. Add `score_subjective` that loads reusable bid evidence once and passes the existing `bid_document` and artifact directory into `run_subjective_scoring`; it must never call `clean_bid_document` as a fallback.

- [ ] **Step 4: Add the API trigger and artifact loader**

Add `POST /api/bid-check/tasks/{task_id}/subjective-score` that returns 404 for unknown tasks, 409 for non-evaluation tasks, and schedules `active_workflow.run_subjective(task_id)` for valid evaluation tasks. Add a read-only loader for `compliance_extraction/subjective_scores.json` and merge it into the existing task GET payload without replacing stored evaluation/objective/veto results.

- [ ] **Step 5: Run focused API tests and verify they pass**

Run: `pytest tests/test_api.py -q`

Expected: all API tests pass and an evaluation task can trigger or expose subjective scoring independently.

- [ ] **Step 6: Commit API integration**

```bash
git add app/api.py tests/test_api.py
git commit -m "feat: expose independent subjective scoring"
```

### Task 5: Run full verification and the real six-item acceptance

**Files:**
- Modify: none unless verification finds a test-backed defect
- Verify: `app/subjective_scoring.py`, `app/workflow.py`, `app/api.py`, `app/repository.py`
- Verify artifact: current task's `compliance_extraction/subjective_scores.json`

**Interfaces:**
- Consumes the current `11_evaluation_rules.json` and existing business-bid `structured_document.json`.
- Produces a real independently written `subjective_scores.json` and an acceptance report from its contents and execution log.

- [ ] **Step 1: Run the complete automated test suite**

Run: `pytest -q`

Expected: exit code 0 with zero failures and zero errors.

- [ ] **Step 2: Run repository lint checks**

Run: `ruff check app tests`

Expected: no lint errors.

- [ ] **Step 3: Execute the real acceptance using existing artifacts only**

Use the existing tender rule artifact and business-bid structured artifact as inputs to `run_subjective_scoring`, supplying the configured LLM implementation if available. Do not pass a parser and do not create a parser fallback. Confirm the output has six records in source order and that each record has `score_item_id`, `rule_name`, `max_score`, `status`, `score_band`, `recommended_score`, `reason`, `matched_bid_content`, `evidence`, `block_ids`, and `uncertainty`.

- [ ] **Step 4: Inspect the six-item result and execution stats**

Confirm the acceptance output reports the successful AI-scored items with selected rule band, suggested score, reason, and evidence block; each `file_scope_missing` item caused by the business-only scope with `recommended_score: null`; actual LLM call count, per-call elapsed time, total elapsed time, and no retry calls; `new_parse_calls == 0`, `new_ocr_calls == 0`, `new_mineru_calls == 0`, `duplicate_parse == false`, `total_score_computed == false`, `ranking_computed == false`, and `veto_executed == false`; and no objective item or veto rule in `subjective_scores.json`.

- [ ] **Step 5: Inspect the final diff and artifact paths**

Run: `git status --short && git diff --check`

Expected: only intended implementation/test changes remain, with no secret values or full-document LLM payloads in tracked files.

- [ ] **Step 6: Commit the verified implementation if the working tree is cleanly scoped**

```bash
git add app tests
git commit -m "feat: complete subjective evaluation scoring"
```
