# 模板文本检查全量并发接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or **superpowers:executing-plans** to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已验证的模板文本检查扩展到全部明确匹配模板，使用受控并发执行并完整汇总结果。

**Architecture:** 保留 `run_template_text_review` 的单模板提示词构造、LLM 调用和 JSON 解析逻辑，只新增全量匹配筛选、并发 worker、单次异常隔离和批次统计。共享现有 recorder 记录每个模板的真实输入、原始返回、解析结果和耗时，最终按招标模板原始顺序输出；页面通过匹配结果与文本检查结果按模板 ID 合并展示。

**Tech Stack:** Python 3.14、`concurrent.futures.ThreadPoolExecutor`、现有 `ComplianceExtractionRecorder`、FastAPI/Jinja2、pytest。

**Spec:** 当前对话中的“模板文本检查全量并发接入”需求。

## Global Constraints

- 模板文本检查 system prompt、user prompt、状态定义、issue 类型和检查边界保持不变。
- 一个模板只调用一次 LLM，不拆字段或条款。
- 只对匹配状态为 `matched` 且能读取完整模块内容的模板调用 LLM。
- 并发数固定为 3，并优先复用现有受控 LLM 调用机制。
- 单模板调用异常不得中断整批；调用失败必须与业务 `fail` 区分。
- 结果按招标模板原始顺序排列，不依赖并发完成顺序。
- 不接入附件、视觉、签章、project_requirements、CA、加密、平台状态或综合结论检查。

---

### Task 1: 全量筛选、并发顺序和失败隔离测试

**Files:**
- Modify: `tests/test_template_text_review.py`
- Modify: `tests/test_end_to_end.py`
- Modify: `tests/test_pages.py`

**Interfaces:**
- Consumes: 现有 `run_template_text_review`、`build_template_comparisons`、`RecordingReviewLLM` 测试夹具。
- Produces: 覆盖全部模板参与、未匹配跳过、并发上限 3、原始顺序、单次异常隔离和批次统计的回归测试。

- [x] **Step 1: Write the failing tests**

  在模板文本测试中加入 5 个以上模板，令其中部分匹配、部分候选不确定、部分未匹配；用带 barrier/延迟和指定异常的 LLM 夹具验证只执行明确匹配项，且输出顺序与输入顺序一致。

  断言批次统计至少包含 `template_count`、`matched_template_count`、`llm_total_calls`、`pass_count`、`fail_count`、`uncertain_count`、`not_applicable_count`、`llm_failed_count`、`llm_elapsed_ms` 和 `total_elapsed_ms`。

- [x] **Step 2: Run the focused tests to verify failure**

  Run: `uv run pytest tests/test_template_text_review.py -k "all_templates or concurrent or failure" -q`

  Expected: FAIL because当前实现只筛选固定三个模板且异常会中断批次。

- [x] **Step 3: Keep the test fixture isolated**

  测试 LLM 仅在内存中记录 prompt 和返回结果，不写 `data/tasks`；所有共享文档在用例内深拷贝。

- [x] **Step 4: Run the focused tests again after implementation**

  Run: `uv run pytest tests/test_template_text_review.py -q`

  Expected: PASS。

---

### Task 2: 实现全量模板并发调度与批次汇总

**Files:**
- Modify: `app/template_text_review.py`
- Test: `tests/test_template_text_review.py`

**Interfaces:**
- Consumes: `build_template_comparisons(extraction_templates, structured_sections)` 返回的匹配状态；现有 `build_template_text_review_user_prompt` 和 `_parse_review_result`。
- Produces: `run_template_text_review(...) -> dict[str, Any]` 返回按模板原始顺序排列的执行结果和批次统计；失败项增加明确的调用失败信息而不伪装成业务 `fail`。

- [x] **Step 1: Select every template with a reliable match**

  使用全部 `extraction_result["templates"]` 的原始顺序与 `build_template_comparisons` 对齐；仅当 comparison 为 `matched`、存在 section 且 materialized section 可读取时创建 worker。未匹配、候选不确定、内容不可读者不调用 LLM。

- [x] **Step 2: Extract the existing single-template worker**

  将当前构造 user prompt、创建 recorder call、调用 `llm.review_template`、解析 JSON 和完成 recorder 的代码收拢为一个 worker；worker 继续使用同一个 `TEMPLATE_TEXT_REVIEW_SYSTEM_PROMPT`、`build_template_text_review_user_prompt` 和 `_parse_review_result`。

- [x] **Step 3: Run workers with bounded concurrency**

  使用 `ThreadPoolExecutor(max_workers=3)` 或当前项目已有统一并发控制入口；收集 future 结果时按模板索引回填，不能按完成顺序追加。

- [x] **Step 4: Isolate per-template errors**

  worker 捕获 LLM 请求、超时和 JSON 解析异常，写入 recorder 的失败 call，并返回带 `execution_status: "failed"`、`error_type`、`error_message` 的结果；业务 `status` 不设为 `fail`，其余 worker 继续执行。

- [x] **Step 5: Aggregate counts and elapsed times**

  统计模板总数、明确匹配数、实际调用数、四类业务状态数、调用失败数、LLM 总耗时和批次总耗时，并保持现有 `mode: "template_text"` 与 `template_text_reviews` 结构兼容。

- [x] **Step 6: Run focused tests**

  Run: `uv run pytest tests/test_template_text_review.py -q`

  Expected: PASS。

---

### Task 3: 保持正式 workflow 和 recorder 的全量结果可追踪

**Files:**
- Modify: `app/workflow.py` only if recorder/context propagation requires it
- Modify: `app/compliance_artifacts.py` only if existing recorder lacks safe concurrent writes
- Modify: `tests/test_workflow.py`
- Modify: `tests/test_compliance_artifacts.py`

**Interfaces:**
- Consumes: 正式 workflow 已传入的 task-local recorder。
- Produces: 每个模板的输入、原始返回、解析结果、耗时和失败原因可按 call ID 查询，且并发写入不会覆盖或串写。

- [x] **Step 1: Add a concurrent recorder regression test if needed**

  使用两个并发模板 worker 写同一个 recorder，断言 `llm/call_NNN_input.json` 和 output 文件按唯一 call ID 保存，失败调用也留下可追踪错误信息。

- [x] **Step 2: Reuse or minimally protect existing recorder synchronization**

  仅在现有 recorder 不是线程安全时增加最小锁；不创建独立于现有 LLM 并发控制的第二套业务池。

- [x] **Step 3: Run focused workflow/artifact tests**

  Run: `uv run pytest tests/test_workflow.py tests/test_compliance_artifacts.py -q`

  Expected: PASS。

---

### Task 4: 页面按模板 ID 合并展示匹配状态和文本检查状态

**Files:**
- Modify: `app/templates/bid_check_task.html`
- Modify: `tests/test_pages.py`

**Interfaces:**
- Consumes: 全量 `template_comparisons` 和 `review_result.template_text_reviews`。
- Produces: 模板名称、对应投标模块、匹配状态、文本检查状态、检查结论；fail issues 可展开；未执行项不显示为文本检查失败。

- [x] **Step 1: Add failing page assertions**

  构造 matched/pass、matched/fail、matched/call-failed、candidate、unmatched 五类行，断言 call-failed 显示“调用失败”，candidate/unmatched 显示匹配状态且不显示“不合规”。

- [x] **Step 2: Merge by `template_id` in page context or template**

  优先在 API 组装稳定的 `template_text_review_by_id` 映射，模板只负责展示；没有 review 的模板使用匹配状态作为文本检查显示，不生成失败结论。

- [x] **Step 3: Render fail issues behind existing simple expansion**

  仅对 `status == "fail"` 的执行项展示 issues；调用失败显示错误原因，不作为业务问题列表。

- [x] **Step 4: Run page tests**

  Run: `uv run pytest tests/test_pages.py -q`

  Expected: PASS。

---

### Task 5: 全量真实任务验证与最终回归

**Files:**
- No production file changes expected.
- Read: 当前真实任务的模板提取、结构化投标文档和匹配结果。

**Interfaces:**
- Consumes: 当前真实招标文件和投标文件既有阶段产物。
- Produces: 一次真实全量模板文本检查统计及所有非 `pass` 模板明细，不覆盖前一轮正式结果，除非用户明确要求。

- [x] **Step 1: Run the full test suite**

  Run: `uv run pytest`

  Expected: 全部测试通过。

- [x] **Step 2: Run the real full template review**

  使用正式 workflow/recorder 和当前真实文件执行一次全量模板文本检查，确认并发数为 3、结果按模板原始顺序、每个调用可追踪。

- [x] **Step 3: Verify final artifacts and report**

  检查总模板数、明确匹配数、调用次数、并发数、耗时、四类状态数、调用失败数、所有非 pass 项和是否存在单次失败但批次完成；不执行附件或其他检查。

- [x] **Step 4: Run final checks**

  Run: `uv run pytest`, `uv run ruff check tests/test_template_text_review.py tests/test_pages.py`, `uv run python -m compileall -q app tests`。

  Expected: 测试和目标文件静态检查通过；全仓库既有 lint 问题单独说明。
