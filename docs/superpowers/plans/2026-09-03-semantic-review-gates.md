# LLM 语义确认闸门实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or **superpowers:executing-plans** to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在模板文本检查和普通附件检查的现有单次 LLM 调用中加入语义确认闸门，隔离误召回与正式业务结论，并用真实招投标文件重新测量两轮耗时。

**Architecture:** 保持现有匹配、候选生成、解析、图片收集、并发和重试流程不变，在两类 review worker 的同一次 LLM 响应中解析 `semantic_match`。只有语义状态为 `matched` 才进入已有业务结果校验和聚合；其他状态保留候选、理由和独立统计，业务状态置为 `not_run`，避免误召回形成业务 fail。页面按现有模板 ID 合并结果并展示语义跳过原因。

**Tech Stack:** Python 3.14、pytest、现有 ThreadPoolExecutor、OpenAI-compatible Chat Completions、Jinja2、ComplianceExtractionRecorder。

**Spec:** `docs/superpowers/specs/2026-09-03-semantic-gates-review-design.md`

## Global Constraints

- 每个模板文本候选仍只进行一次 LLM 调用；每个附件候选仍只进行一次多模态 LLM 调用。
- 不修改现有标题匹配、关键词匹配、候选生成、图片提取、表格内嵌图片提取、结构化解析、图片路径、image_id、并发和重试机制。
- 语义不匹配或不确定不得形成投标文件或附件业务不合规结论。
- 现有模板文本 issue 类型、业务状态、附件材料/事实/要求结构和条件适用性规则保持兼容。
- 当前工作区已有未提交改动属于用户既有工作，所有修改必须叠加在其上，不得 reset、checkout 或覆盖无关内容。
- 不创建子 agent、分支或 worktree；所有实现和验证在当前会话、当前工作区完成。

---

### Task 1: 模板文本语义闸门的失败测试与提示词契约

**Files:**
- Modify: `tests/test_template_text_review.py`
- Modify: `app/template_text_review.py`

**Interfaces:**
- Consumes: 现有 `run_template_text_review`、`_parse_review_result`、`RecordingReviewLLM` 和完整模板/投标模块 fixture。
- Produces: `semantic_match.status` 为 `matched/mismatched/uncertain` 的结果契约；语义跳过结果不产生 issues、不计入业务 fail。

- [x] **Step 1: Write the failing tests**

  在测试文件新增一个响应包含 `semantic_match` 的 fixture，并增加以下三个行为测试：

```python
def test_template_semantic_mismatch_skips_business_review_and_stats_as_candidate():
    llm = FixedTemplateReviewLLM({
        "status": "fail",
        "summary": "不应被采用的业务结论。",
        "issues": [{
            "type": "missing_fill",
            "requirement": "模板要求填写名称。",
            "actual": "未填写。",
            "reason": "业务判断不应执行。",
        }],
        "semantic_match": {
            "status": "mismatched",
            "reason": "投标模块只是标题相似，实际用途是另一类承诺函。",
        },
    })

    result = run_template_text_review(extraction, parsed_bid, llm=llm)
    review = result["template_text_reviews"][0]

    assert review["semantic_match"]["status"] == "mismatched"
    assert review["business_status"] == "not_run"
    assert review["execution_status"] == "semantic_skipped"
    assert review["issues"] == []
    assert result["stats"]["semantic_mismatched_count"] == 1
    assert result["stats"]["fail_count"] == 0
```

  再增加 `uncertain` 的同类测试，断言 `business_status == "not_run"`、`semantic_uncertain_count == 1`；增加 `matched` 测试，断言既有 `fail` 业务结论保留且 `business_status == "fail"`。在提示词断言中检查必须包含“语义对应”“标题相似”“关键词重复”“先判断”等闸门文案。

- [x] **Step 2: Run focused tests to verify they fail**

  Run: `uv run pytest tests/test_template_text_review.py -k "semantic or system_prompt" -q`

  Expected: FAIL，因为当前响应解析器不要求 `semantic_match`，且 worker 没有独立的 `business_status`/语义跳过处理。

- [x] **Step 3: Implement the minimal template semantic contract**

  在 `TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT` 和 `build_template_text_review_user_prompt` 中增加同一次调用内的先行判断及 JSON 字段说明；扩展 `_parse_review_result` 校验并规范化：

```python
"semantic_match": {
    "status": "matched | mismatched | uncertain",
    "reason": "非空字符串",
}
```

  在 `_review_matched_template` 中，解析后先写入 `semantic_match`。当状态不是 `matched` 时清空 issues，设置 `business_status="not_run"`、`execution_status="semantic_skipped"`、兼容顶层 `status="uncertain"`，并返回；状态为 `matched` 时保留现有 status/issues，同时设置 `business_status` 为该 status、`execution_status="completed"`。调用异常设置语义状态 `uncertain`、业务状态 `not_run` 和 `execution_status="failed"`。

- [x] **Step 4: Run focused tests to verify they pass**

  Run: `uv run pytest tests/test_template_text_review.py -k "semantic or system_prompt" -q`

  Expected: PASS。

- [x] **Step 5: Commit only if the existing user worktree policy allows it**

  Do not commit the user’s existing dirty files. If a checkpoint is needed, inspect the exact diff first and commit only the new template-gate test/implementation hunks; otherwise continue without a commit.

### Task 2: 模板文本统计、漏召回监控字段和旧测试迁移

**Files:**
- Modify: `app/template_text_review.py`
- Modify: `tests/test_template_text_review.py`
- Modify: `tests/test_end_to_end.py`

**Interfaces:**
- Consumes: Task 1 的 `semantic_match`、`business_status`、`execution_status`。
- Produces: `stats.code_candidate_count`、语义三类计数、业务状态计数、无候选/无可靠文本模板 ID，同时保持既有统计字段和结果顺序。

- [x] **Step 1: Write failing stats and compatibility assertions**

  更新所有已有模板 review LLM fixture 响应，使每个合法业务响应显式带有 `semantic_match.status="matched"`；新增断言：

```python
assert result["stats"]["code_candidate_count"] == result["stats"]["matched_template_count"]
assert result["stats"]["semantic_matched_count"] == expected_matched_calls
assert result["stats"]["business_status_counts"]["fail"] == result["stats"]["fail_count"]
assert "no_bid_candidate_template_ids" in result["stats"]
assert "candidate_without_reliable_bid_text_template_ids" in result["stats"]
```

  保留现有并发、重试、失败隔离、稳定顺序和 recorder 断言；对调用失败项断言不计入业务状态计数。

- [x] **Step 2: Run focused tests to verify the new assertions fail**

  Run: `uv run pytest tests/test_template_text_review.py tests/test_end_to_end.py -q`

  Expected: FAIL，原因是 fixture 尚未提供语义字段或统计尚未实现。

- [x] **Step 3: Implement template stats without changing candidate generation**

  在 `run_template_text_review` 保持现有 `build_template_comparisons` 和 job 筛选不变；将进入 worker 的 job 数记录为 `code_candidate_count`。由 comparisons 生成 `no_bid_candidate_template_ids`，由匹配但被 `_section_has_reliable_text` 排除的模板生成 `candidate_without_reliable_bid_text_template_ids`。在 `_review_stats` 中按 `semantic_match.status` 统计三类数量，并仅按 `business_status` 统计 pass/fail/uncertain/not_applicable；保留原有字段，新增 `business_status_counts`。

- [x] **Step 4: Run the complete focused suites**

  Run: `uv run pytest tests/test_template_text_review.py tests/test_end_to_end.py -q`

  Expected: PASS。

### Task 3: 普通附件语义闸门与业务结论隔离

**Files:**
- Modify: `tests/test_attachment_review.py`
- Modify: `app/attachment_review.py`

**Interfaces:**
- Consumes: 现有 `run_attachment_review`、`_parse_attachment_review_result`、多模态 request payload、附件 scope 和条件适用性逻辑。
- Produces: 附件响应中的 `semantic_match`；语义不匹配/不确定时保留候选记录但不执行 materials/requirements 业务结论。

- [x] **Step 1: Write failing attachment gate tests**

  新增两个多模态 fixture：一个返回 `semantic_match.mismatched` 但故意带有 `status="fail"`、requirements 和 materials；另一个返回 `semantic_match.uncertain`。断言：

```python
review = result["attachment_reviews"][0]
assert review["semantic_match"]["status"] == "mismatched"
assert review["business_status"] == "not_run"
assert review["execution_status"] == "semantic_skipped"
assert review["materials"] == []
assert review["requirements"] == []
assert review["status"] != "fail"
assert result["stats"]["semantic_mismatched_count"] == 1
assert result["stats"]["fail_count"] == 0
```

  增加 prompt 断言，要求先确认独立证明材料范围，并明确排除模板正文、表格、承诺函、签字盖章和未来义务。

- [x] **Step 2: Run focused tests to verify they fail**

  Run: `uv run pytest tests/test_attachment_review.py -k "semantic or prompt" -q`

  Expected: FAIL，因为附件解析器当前没有语义字段和语义跳过分支。

- [x] **Step 3: Implement the minimal attachment gate**

  扩展 `ATTACHMENT_REVIEW_SYSTEM_PROMPT` 和 user prompt 的输出契约；在 `_parse_attachment_review_result` 先验证并规范化 `semantic_match`。在 `_review_one_attachment` 解析后先处理语义状态：非 `matched` 时清空 materials/requirements，设置 `business_status="not_run"`、`execution_status="semantic_skipped"`、顶层 `status="uncertain"`，直接完成 recorder 调用并返回；只有 `matched` 才进入当前 scope 过滤、空 requirement materialize、条件边界和 `_aggregate_attachment_status`。调用失败沿用重试/隔离，但增加语义不确定和 `business_status="not_run"`。

- [x] **Step 4: Update existing attachment fixtures and run focused suite**

  为所有合法附件 fixture 增加 `semantic_match.status="matched"`；运行：

  `uv run pytest tests/test_attachment_review.py -q`

  Expected: PASS，现有图片、表格内嵌图片、条件附件、证据 image_id、并发和失败隔离测试均保留原结论。

### Task 4: 附件统计、漏召回监控字段和组合结果

**Files:**
- Modify: `app/attachment_review.py`
- Modify: `tests/test_attachment_review.py`

**Interfaces:**
- Consumes: Task 3 的附件语义结果和现有 requirement 聚合结果。
- Produces: `code_candidate_count`、语义分类计数、`confirmed_requirement_count`、无投标候选模板 ID 和无证据 requirement 计数；组合 review 继续兼容模板文本结果。

- [x] **Step 1: Write failing stats assertions**

  在现有多模板运行测试中加入一个语义不匹配候选和一个语义不确定候选，断言：

```python
assert stats["code_candidate_count"] == 3
assert stats["semantic_matched_count"] == 1
assert stats["semantic_mismatched_count"] == 1
assert stats["semantic_uncertain_count"] == 1
assert stats["confirmed_requirement_count"] == 1
assert stats["fail_count"] == 0
assert "no_bid_candidate_template_ids" in stats
assert "requirements_without_evidence_count" in stats
```

  现有 matched 附件要求的 pass/fail/uncertain 统计必须保持业务语义。

- [x] **Step 2: Run the new stats test to verify it fails**

  Run: `uv run pytest tests/test_attachment_review.py -k "stats or runner or semantic" -q`

  Expected: FAIL，原因是当前统计没有语义字段和候选追踪字段。

- [x] **Step 3: Implement attachment statistics**

  将进入 `_review_one_attachment` 的 job 数记录为 `code_candidate_count`；按结果的 `semantic_match.status` 统计三类；`confirmed_requirement_count` 为语义 matched 且最终保留的 requirements 数量总和；`requirements_without_evidence_count` 统计正式 requirement 结论中 evidence_image_ids 为空的数量；业务状态只使用 `business_status`。补充无候选模板 ID，保留现有 `selected_template_count`、LLM 次数和耗时字段。

- [x] **Step 4: Run attachment and combined review tests**

  Run: `uv run pytest tests/test_attachment_review.py -q`

  Expected: PASS。

### Task 5: 页面展示语义状态与业务状态分离

**Files:**
- Modify: `app/templates/bid_check_task.html`
- Modify: `tests/test_pages.py`

**Interfaces:**
- Consumes: 模板/附件 review 中的 `semantic_match`、`business_status`、`execution_status` 和统计字段。
- Produces: 页面明确显示语义不匹配/不确定、判断理由和“未执行业务检查”，不显示业务不合规。

- [x] **Step 1: Write failing page assertions**

  构造三类 review 行：semantic mismatched、semantic uncertain、semantic matched + business fail；断言页面包含“语义不匹配”“语义匹配不确定”“未执行业务检查”和对应理由，并且语义跳过行不包含“检查不通过”。

- [x] **Step 2: Run page tests to verify they fail**

  Run: `uv run pytest tests/test_pages.py -q`

  Expected: FAIL，因为模板目前只渲染顶层业务 status/summary。

- [x] **Step 3: Implement compatible page rendering**

  在文本检查状态和附件检查状态区域优先判断 `execution_status == "semantic_skipped"`，按语义状态显示独立标签；在结论区域显示 semantic reason 和“未执行业务检查”，不渲染 issues。对 `business_status == "fail"` 的 matched 结果保留现有问题展示；对旧结果没有新字段时保留原页面行为。

- [x] **Step 4: Run page tests**

  Run: `uv run pytest tests/test_pages.py -q`

  Expected: PASS。

### Task 6: 回归验证、真实文件两轮重跑与耗时记录

**Files:**
- Modify: `README.md` only if the user-facing description or artifact field documentation needs the new stats names.
- Read/execute: `data/tasks/5f77e206-6aca-4151-a949-35d3b509be26/tender.docx`
- Read/execute: `data/tasks/5f77e206-6aca-4151-a949-35d3b509be26/bid.docx`
- Read: generated `compliance_extraction/08_template_text_reviews.json`, `09_attachment_reviews.json`, `workflow_summary.json`

**Interfaces:**
- Consumes: 两条检查链完整实现、当前真实任务的招标/投标文件及既有结构化产物。
- Produces: 新语义统计、非 pass 明细、真实模板文本/附件批次墙钟耗时和 LLM 累计耗时。

- [x] **Step 1: Run focused and full automated tests**

  Run:

  ```bash
  uv run pytest tests/test_template_text_review.py tests/test_attachment_review.py tests/test_pages.py tests/test_end_to_end.py -q
  uv run pytest -q
  uv run python -m compileall -q app tests
  ```

  Expected: 全部测试通过，compileall 返回 0。

- [x] **Step 2: Run the real template-text and attachment review chains**

  使用当前真实任务的 `tender.docx`、`bid.docx` 和现有结构化/提取产物，通过正式配置创建 task-local recorder 快照，分别执行 `run_template_text_review` 和 `run_attachment_review`。不重新调用 MinerU，不修改匹配器、解析器或图片资产；将输出写入新的审计目录或用户明确的当前任务结果位置，避免丢失旧的 08/09 结果。调用前后记录 `time.perf_counter()` 墙钟时间，不把并发 LLM 累计耗时误当作批次耗时。

- [x] **Step 3: Inspect real artifacts and produce a metrics report**

  从新生成的 JSON 中核对：

```text
模板：code_candidate_count、semantic_matched_count、semantic_mismatched_count、semantic_uncertain_count、pass/fail/uncertain/not_applicable、llm_elapsed_ms、total_elapsed_ms
附件：code_candidate_count、semantic_matched_count、semantic_mismatched_count、semantic_uncertain_count、confirmed_requirement_count、pass/fail/uncertain、llm_elapsed_ms、total_elapsed_ms
```

  同时列出语义跳过候选的 template_id/name/reason、正式业务非 pass 项、未找到投标候选的模板 ID 和无证据 requirement 数量。

- [x] **Step 4: Run final verification commands**

  Run:

  ```bash
  uv run pytest -q
  uv run ruff check app/template_text_review.py app/attachment_review.py tests/test_template_text_review.py tests/test_attachment_review.py tests/test_pages.py
  uv run python -m compileall -q app tests
  git diff --check
  git status --short
  ```

  Expected: pytest、目标文件 ruff、compileall 和 diff check 均返回 0；最终报告必须区分本轮改动与工作区原有未提交改动。
