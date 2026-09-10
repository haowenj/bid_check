# 标书检查任务失败重试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在原任务上增加“从头开始”或“从失败阶段开始”的人工重试能力，并严格复用已成功阶段的结果和产物。

**Architecture:** 扩展任务记录以保存评标执行阶段状态，并让数据库以单事务原子地准备重试；工作流入口接收可选重试起点，按检查模式计算需要执行的阶段，未选中的成功前置阶段从任务结果/已校验产物加载。任务列表通过重试 API 弹窗触发原任务恢复，保留原任务 ID、上传文件和成功阶段产物。

**Tech Stack:** Python 3.14、FastAPI、SQLite、Jinja2、原生 JavaScript、pytest。

**Spec:** `docs/superpowers/specs/2026-09-10-task-retry-design.md`

## Global Constraints

- 重试发生在原任务上，不创建新任务，不改变任务 ID，也不删除原始上传文件。
- 只增加用户主动触发的手动重试，不实现自动重试、定时重试或重试次数配置。
- 只允许 `failed` 任务重试；正在执行、等待执行和已完成任务返回 `409`。
- 失败阶段必须细化为 `requirements`、`bid_parse`、`review`、`evaluation_rules`、`objective_scoring`、`subjective_scoring`、`veto_rule_execution`。
- 从失败阶段开始时，起点之前的阶段状态、任务结果和已校验产物必须保留；起点及后续阶段的旧结果必须清除后重新生成。
- 保留现有三阶段字段和接口兼容行为；SQLite 初始化必须安全迁移已有数据库。
- 遵循当前工作区约束：直接在当前分支和当前工作区修改，不新建分支、不新建工作区、不使用子代理。

---

### Task 1: 扩展任务模型与原子重试状态重置

**Files:**
- Modify: `app/models.py:7-10,145-177` — 扩展逻辑阶段名、任务状态字段和 JSON 输出。
- Modify: `app/repository.py:1-290` — 新增评标阶段列、兼容迁移和 `prepare_retry`。
- Test: `tests/test_repository.py` — 先覆盖阶段状态、部分结果保存和重试重置。

**Interfaces:**
- Produces `StageName = Literal["requirements", "bid_parse", "review", "evaluation_rules", "objective_scoring", "subjective_scoring", "veto_rule_execution"]`。
- Produces `BidCheckTask.evaluation_rules_status`, `objective_scoring_status`, `subjective_scoring_status`, `veto_rule_execution_status`。
- Produces `BidCheckRepository.prepare_retry(task_id: str, retry_from: Literal["start", "failed_stage"]) -> BidCheckTask`。
- `prepare_retry` 只接受 `failed` 任务；失败起点由任务的 `failed_stage` 解析，返回重置后 `pending` 的原任务。

- [ ] **Step 1: Write the failing repository tests**

在 `tests/test_repository.py` 添加以下行为测试：

```python
def test_prepare_retry_from_failed_stage_preserves_previous_results(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "full")
    repository.update_stage("task-001", "requirements", "complete")
    repository.update_stage("task-001", "bid_parse", "complete")
    repository.update_stage("task-001", "review", "complete")
    repository.update_stage("task-001", "evaluation_rules", "complete")
    repository.update_result(
        "task-001",
        {
            "requirements": {"id": "requirements-ok"},
            "bid_parse": {"id": "bid-parse-ok"},
            "review_result": {"id": "review-ok"},
            "evaluation_rules": {"id": "rules-old"},
            "objective_scores": {"id": "scores-old"},
        },
    )
    repository.fail("task-001", "objective_scoring", "评分服务失败")

    retried = repository.prepare_retry("task-001", "failed_stage")

    assert retried.task_id == "task-001"
    assert retried.status == "pending"
    assert retried.requirements_status == "complete"
    assert retried.bid_parse_status == "complete"
    assert retried.review_status == "complete"
    assert retried.evaluation_rules_status == "complete"
    assert retried.objective_scoring_status == "pending"
    assert retried.result == {
        "requirements": {"id": "requirements-ok"},
        "bid_parse": {"id": "bid-parse-ok"},
        "review_result": {"id": "review-ok"},
    }
    assert retried.failed_stage is None
    assert retried.error_message is None


def test_prepare_retry_from_start_resets_all_derived_states_and_results(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "full")
    for stage in (
        "requirements",
        "bid_parse",
        "review",
        "evaluation_rules",
        "objective_scoring",
        "subjective_scoring",
        "veto_rule_execution",
    ):
        repository.update_stage("task-001", stage, "complete")
    repository.update_result("task-001", {"review_result": {"old": True}})
    repository.fail("task-001", "veto_rule_execution", "否决规则失败")

    retried = repository.prepare_retry("task-001", "start")

    assert retried.status == "pending"
    assert retried.result == {}
    assert retried.failed_stage is None
    assert retried.requirements_status == "pending"
    assert retried.bid_parse_status == "pending"
    assert retried.evaluation_rules_status == "pending"


def test_prepare_retry_rejects_non_failed_task(tmp_path):
    repository = make_repository(tmp_path)
    tender_file, bid_file = make_files()
    repository.create("task-001", tender_file, bid_file, "compliance")

    with pytest.raises(ValueError, match="failed"):
        repository.prepare_retry("task-001", "start")
```

把测试需要的 `pytest` 导入补到文件顶部，并在 `fail` 测试中使用新增的评标阶段名。

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
pytest tests/test_repository.py -q
```

Expected: 新增测试因任务字段和 `prepare_retry` 尚不存在而失败；现有测试应继续收集成功。

- [ ] **Step 3: Implement the model and repository changes**

在 `app/models.py` 中增加四个阶段状态字段，并让 `to_dict()` 返回它们。为避免旧数据库启动失败，在 `BidCheckRepository._initialize()` 后增加 `_ensure_schema()`：读取 `PRAGMA table_info(bid_check_tasks)`，对不存在的四列执行 `ALTER TABLE ... ADD COLUMN ... TEXT NOT NULL DEFAULT 'pending'`，再把 `check_mode='evaluation'` 的 `evaluation_rules_status` 从原 `requirements_status` 回填。

在 `repository.py` 中：

1. 扩展 `STAGE_COLUMNS`，并为 `requirements`/`bid_parse`/`review` 保留原列名；
2. 在 `create()` 的 INSERT 中为新增列写入 `pending`；
3. 在 `_record_from_row()` 读取新增列；对极旧数据库缺失列的情况使用 `row.keys()` 判断并回退为 `pending`；
4. 将 `update_stage()` 扩展为新增阶段可写；写 `evaluation_rules` 时同步更新评标模式的 `requirements_status` 兼容别名；
5. 将 `fail()` 扩展为精确失败阶段，并清除旧 `failed_stage/error_message` 只在新一次运行开始时处理；
6. 增加 `prepare_retry()`，在 `_write_lock` 和同一连接事务中执行条件更新。使用 `WHERE task_id=? AND status='failed'`，更新前先读任务并根据 `retry_from` 计算保留结果键和需要置 `pending` 的阶段。结果键顺序固定为：`requirements`、`bid_parse`、`review_result`、`evaluation_rules`、`objective_scores`、`subjective_scores`、`veto_rule_reviews`；从失败阶段开始保留其前面的键，从头开始全部清空；任务状态设为 `pending`、失败字段清空、更新时间刷新。

阶段状态重置规则写成显式映射，不依赖字符串排序：

```python
FULL_STAGE_ORDER = (
    "requirements", "bid_parse", "review", "evaluation_rules",
    "objective_scoring", "subjective_scoring", "veto_rule_execution",
)
```

并行阶段若 `requirements_status` 和 `bid_parse_status` 都是 `failed`，`failed_stage` 只记录首个失败阶段，但 `prepare_retry(..., "failed_stage")` 必须把这两个失败分支都置为 `pending`；前置成功分支继续保留。

- [ ] **Step 4: Run repository tests to verify they pass**

Run:

```bash
pytest tests/test_repository.py -q
```

Expected: 新增重试状态测试和所有既有 repository 测试通过。

- [ ] **Step 5: Commit the data-layer deliverable**

```bash
git add app/models.py app/repository.py tests/test_repository.py
git commit -m "feat: add atomic task retry state reset"
```

### Task 2: 让工作流按阶段执行并复用成功产物

**Files:**
- Modify: `app/workflow.py:25-710` — 增加可选重试起点、阶段计划、已保存结果加载和每阶段持久化。
- Modify: `tests/test_workflow.py` — 覆盖各阶段跳过、复用和精确失败。

**Interfaces:**
- Consumes `BidCheckRepository.prepare_retry()` 的重置状态和 `BidCheckTask` 新字段。
- Produces `BidCheckWorkflow.run(task_id: str, retry_from: str | None = None) -> None`。
- Produces `BidCheckWorkflow._load_saved_stage_result(task: BidCheckTask, result_key: str, artifact_name: str | None) -> dict[str, Any]`，所有重试跳过阶段都通过统一读取逻辑获取数据。

- [ ] **Step 1: Write failing workflow tests**

在 `tests/test_workflow.py` 添加以下测试骨架，使用现有 `BidCheckServices` 的可观察调用列表：

```python
def test_retry_from_failed_requirements_reuses_successful_bid_parse(task_repository):
    calls = []
    task_repository.update_stage("task-001", "bid_parse", "complete")
    task_repository.update_result(
        "task-001",
        {"bid_parse": {"status": "saved", "artifact_dir": "/tmp/bid"}},
    )
    task_repository.fail("task-001", "requirements", "要求提取失败")

    def extract(file_metadata, recorder=None):
        calls.append("requirements")
        return empty_objects()

    def parse(_file_metadata):
        calls.append("bid_parse")
        raise AssertionError("成功的 bid_parse 不应被重跑")

    workflow = BidCheckWorkflow(
        task_repository,
        BidCheckServices(
            extract=extract,
            parse=parse,
            review=lambda requirements, parsed: {"ok": True},
        ),
    )
    try:
        workflow.run("task-001", retry_from="requirements")
    finally:
        workflow.shutdown()

    assert calls == ["requirements"]
    assert task_repository.get("task-001").status == "complete"


def test_retry_from_objective_scoring_skips_successful_prefix(task_repository, tmp_path):
    task = create_full_task(task_repository, tmp_path)
    task_repository.update_stage(task.task_id, "requirements", "complete")
    task_repository.update_stage(task.task_id, "bid_parse", "complete")
    task_repository.update_stage(task.task_id, "review", "complete")
    task_repository.update_stage(task.task_id, "evaluation_rules", "complete")
    task_repository.update_result(
        task.task_id,
        {
            "requirements": empty_objects(),
            "bid_parse": {"status": "success"},
            "review_result": {"ok": True},
            "evaluation_rules": evaluation_result(),
        },
    )
    task_repository.fail(task.task_id, "objective_scoring", "客观评分失败")
    calls = []

    def evaluate(*args, **kwargs):
        calls.append("evaluation_rules")
        raise AssertionError("评标规则提取不应被重跑")

    def score(*args, **kwargs):
        calls.append("objective_scoring")
        return {"score_items": []}

    services = BidCheckServices(
        extract=lambda _: empty_objects(),
        parse=lambda _: {"status": "unused"},
        review=lambda *_: {"unused": True},
        extract_evaluation_with_recorder=evaluate,
        score_objective_with_recorder=score,
        score_subjective_with_recorder=lambda *args, **kwargs: {"score_items": []},
        execute_veto_with_recorder=lambda *args, **kwargs: {"veto_rule_reviews": []},
    )
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id, retry_from="objective_scoring")
    finally:
        workflow.shutdown()

    assert calls == ["objective_scoring"]


def test_full_workflow_records_subjective_failure_as_subjective_scoring(
    task_repository, tmp_path
):
    task = create_full_task(task_repository, tmp_path)
    def evaluate(_tender_file, recorder=None):
        del recorder
        return evaluation_result()

    def score(_tender_file, _bid_file, _rules, recorder=None):
        del recorder
        return {"score_items": []}

    def subjective(_tender_file, _bid_file, _rules, recorder=None):
        del recorder
        raise RuntimeError("主观评分服务失败")

    def veto(_tender_file, _bid_file, _rules, objective_scores=None, recorder=None):
        del objective_scores, recorder
        return {"veto_rule_reviews": []}

    services = BidCheckServices(
        extract=lambda _file: empty_objects(),
        parse=lambda _file: {"status": "success"},
        review=lambda *_args: {"review": True},
        extract_evaluation_with_recorder=evaluate,
        score_objective_with_recorder=score,
        score_subjective_with_recorder=subjective,
        execute_veto_with_recorder=veto,
    )
    workflow = BidCheckWorkflow(task_repository, services)
    try:
        workflow.run(task.task_id)
    finally:
        workflow.shutdown()

    failed = task_repository.get(task.task_id)
    assert failed.status == "failed"
    assert failed.failed_stage == "subjective_scoring"
    assert failed.subjective_scoring_status == "failed"
```

测试中的固定产物数据必须放在 pytest 的 `tmp_path` 下，不要依赖真实 `/tmp` 文件；上面的 `artifact_dir` 仅表达调用结果形状，实际测试辅助函数应使用任务目录路径。

- [ ] **Step 2: Run workflow tests to verify they fail**

Run:

```bash
pytest tests/test_workflow.py -q
```

Expected: 新增测试因 `run()` 不接受重试起点、评标阶段状态不存在或仍统一记录为 `review` 而失败。

- [ ] **Step 3: Implement explicit stage plans and saved-result loading**

在 `workflow.py` 顶部增加检查模式的阶段计划和结果键映射。将当前 `run()` 拆成几个内部边界：

```python
def run(self, task_id: str, retry_from: StageName | None = None) -> None: ...
def _run_compliance_stages(..., retry_from: StageName | None) -> dict[str, Any] | None: ...
def _run_evaluation_stages(..., retry_from: StageName | None, initial_result: dict[str, Any] | None) -> None: ...
def _saved_stage_result(self, task: BidCheckTask, key: str, filename: str | None) -> dict[str, Any]: ...
```

实现顺序：

1. 初次运行保持现在的并行执行行为；重试从 `requirements` 时只提交要求提取，重试从 `bid_parse` 时只提交投标解析；其他前置分支从 `task.result` 加载。
2. 要求提取成功后立即 `repository.update_result(task_id, {"requirements": output})`；投标解析成功后立即写入 `{"bid_parse": output}`；合规检查成功后立即写入 `{"review_result": output}`。
3. 全面校验依次运行并持久化 `evaluation_rules`、`objective_scores`、`subjective_scores`、`veto_rule_reviews`，每个阶段分别调用 `update_stage(stage, "running")`、成功 `complete`、失败 `fail`。
4. 从 `evaluation_rules` 或更后阶段重试时，要求、解析、合规结果从任务 `result` 加载；如果结果缺失，读取 `07_result.json`、`bid_document_cleaning/structured_document.json` 或 `11_evaluation_rules.json`，读取失败则抛出明确的 `RuntimeError`，由 API 层在开始重试前阻止该路径。
5. 从 `review` 开始时，构造 `bid_parse_for_review` 的逻辑与初次运行一致，并补入原始文件元数据。
6. 从评分/否决阶段开始时，将前置评标结果直接传给 service；`score_objective` 和 `execute_veto` 继续通过现有 `load_reusable_*` 逻辑验证结构化投标产物。
7. 调用 `finalize_workflow()` 时写入 `retry_from` 和真实 `failed_stage`；日志在重试入口追加 `workflow.retry.start`。
8. 对 evaluation-only 保持不解析投标文件、跳过主观评分的现有语义；新增状态字段只记录实际执行的规则提取、客观评分和否决规则阶段。

为避免旧结果污染，工作流只把当前成功阶段结果合并到 repository 已经保留的前缀结果；最终完成时再构造完整 `final_result`。不要在 stage failure 时调用 `complete()`。

- [ ] **Step 4: Run workflow tests and existing API-adjacent tests**

Run:

```bash
pytest tests/test_workflow.py tests/test_repository.py -q
```

Expected: 新增复用/精确失败测试和既有工作流、repository 测试通过；若既有测试依赖 `review_status` 的兼容语义，保留该字段的旧断言。

- [ ] **Step 5: Commit the workflow deliverable**

```bash
git add app/workflow.py tests/test_workflow.py
git commit -m "feat: resume workflow from failed stage"
```

### Task 3: 增加重试 API 和重试前置校验

**Files:**
- Modify: `app/api.py:1-70,476-505,746-940` — 增加请求模型、阶段标签、重试路由和列表数据。
- Test: `tests/test_api.py` — 覆盖成功、非法状态、非法选项、前置产物缺失和原 ID 保持。

**Interfaces:**
- Consumes `BidCheckRepository.prepare_retry()` and `BidCheckWorkflow.run(task_id, retry_from=...)`。
- Produces `POST /api/bid-check/tasks/{task_id}/retry` with JSON `{"retry_from": "start" | "failed_stage"}`。
- Produces `STAGE_LABELS` entries for all logical stages。

- [ ] **Step 1: Write failing API tests**

在 `tests/test_api.py` 添加：

```python
def test_retry_failed_task_from_failed_stage_returns_same_task_and_202(
    client, repository, stored_task
):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.update_result(
        stored_task.task_id,
        {"requirements": {"saved": True}, "bid_parse": {"saved": True}},
    )
    repository.fail(stored_task.task_id, "review", "合规检查失败")

    response = client.post(
        f"/api/bid-check/tasks/{stored_task.task_id}/retry",
        json={"retry_from": "failed_stage"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["task_id"] == stored_task.task_id
    assert payload["status"] in {"pending", "running", "complete"}
    assert repository.get(stored_task.task_id).task_id == stored_task.task_id


def test_retry_rejects_running_task(client, repository, stored_task):
    repository.update_stage(stored_task.task_id, "requirements", "running")

    response = client.post(
        f"/api/bid-check/tasks/{stored_task.task_id}/retry",
        json={"retry_from": "start"},
    )

    assert response.status_code == 409
    assert "失败" in response.json()["detail"]


def test_retry_rejects_invalid_choice(client, repository, stored_task):
    repository.fail(stored_task.task_id, "requirements", "要求提取失败")

    response = client.post(
        f"/api/bid-check/tasks/{stored_task.task_id}/retry",
        json={"retry_from": "middle"},
    )

    assert response.status_code == 422


def test_retry_from_failed_stage_rejects_missing_prefix_artifact(
    client, repository, stored_task
):
    repository.fail(stored_task.task_id, "review", "合规检查失败")

    response = client.post(
        f"/api/bid-check/tasks/{stored_task.task_id}/retry",
        json={"retry_from": "failed_stage"},
    )

    assert response.status_code == 409
    assert "从头开始" in response.json()["detail"]
```

由于 `TestClient` 会执行 FastAPI background task，成功测试应使用现有 fixture 的确定性服务；若测试只验证接收状态，可注入一个不执行外部服务的 fake workflow 并断言 `run` 参数。

- [ ] **Step 2: Run API tests to verify they fail**

Run:

```bash
pytest tests/test_api.py -q
```

Expected: 新增路由不存在或响应码不符合预期而失败。

- [ ] **Step 3: Implement the route and validations**

在 `app/api.py` 中定义：

```python
class RetryTaskRequest(BaseModel):
    retry_from: Literal["start", "failed_stage"]
```

把新增阶段标签加入 `STAGE_LABELS`，并在 `_build_task_list_rows()` 返回 `failed_stage_label` 与 `retry_available`。

新增路由逻辑：

1. 查询任务，不存在抛 `404`；
2. 对 `failed_stage` 模式调用一个纯读校验函数，确认失败阶段存在、前缀阶段状态为 `complete`、结果键或最小产物存在；缺失时抛 `409` 并带“请改用从头开始”；
3. 调用 `active_repository.prepare_retry(task_id, request.retry_from)`，捕获 `KeyError` 为 `404`、非 failed 状态为 `409`；
4. 将 `active_workflow.run(task_id, retry_from=resolved_stage)` 加入 `BackgroundTasks`。`start` 需要传入该检查模式的首阶段，`failed_stage` 传入任务原失败阶段；双并行失败由 workflow/repository 的阶段重置映射处理；
5. 返回重置后的 `task.to_dict()`，状态应为 `pending`。

前置校验和 `prepare_retry` 都要防竞态；即使两个请求同时到达，只有条件更新成功的请求能加入后台任务。不要在 API 层创建新文件、复制任务目录或改变任务 ID。

- [ ] **Step 4: Run API and page regression tests**

Run:

```bash
pytest tests/test_api.py tests/test_pages.py -q
```

Expected: 新增 API 测试、已有创建/查询/删除测试和页面测试通过。

- [ ] **Step 5: Commit the API deliverable**

```bash
git add app/api.py tests/test_api.py
git commit -m "feat: expose failed task retry endpoint"
```

### Task 4: 在任务列表实现重试弹窗和状态更新

**Files:**
- Modify: `app/templates/bid_check_tasks.html:25-150` — 失败任务行重试按钮和弹窗脚本。
- Modify: `app/templates/bid_check_task.html:1580-1645,1640+` — 新增评标阶段状态卡片、重试阶段标签数据和轮询字段。
- Modify: `app/static/task.js:1-55` — 轮询新增阶段状态。
- Modify: `app/static/bid-check.css:220-285` — 重试按钮、弹窗和阶段布局样式。
- Modify: `tests/test_pages.py` — 验证失败任务入口、两个选项和精确阶段文案。

**Interfaces:**
- Consumes task-list row keys `retry_available` and `failed_stage_label`。
- Consumes retry API JSON and returns the same task ID.
- UI uses native `dialog` and `fetch('/api/bid-check/tasks/{id}/retry')`; no new frontend dependency。

- [ ] **Step 1: Write failing page tests**

在 `tests/test_pages.py` 扩展失败任务测试：

```python
def test_failed_task_list_has_retry_options(client, repository, stored_task):
    repository.update_stage(stored_task.task_id, "requirements", "complete")
    repository.update_stage(stored_task.task_id, "bid_parse", "complete")
    repository.fail(stored_task.task_id, "subjective_scoring", "主观评分失败")

    response = client.get("/bid-check/tasks")

    assert response.status_code == 200
    assert 'data-retry-task="stored-task"' in response.text
    assert "从头开始" in response.text
    assert "从失败阶段开始" in response.text
    assert "主观评分" in response.text
    assert "会复用之前成功阶段产物" in response.text
```

- [ ] **Step 2: Run page tests to verify they fail**

Run:

```bash
pytest tests/test_pages.py::test_failed_task_list_has_retry_options -q
```

Expected: 失败，因为当前任务列表没有重试按钮和弹窗选项。

- [ ] **Step 3: Implement the task-list interaction**

在失败任务的 `task-list-side` 中增加：

```html
<button
  class="task-retry-button"
  type="button"
  data-retry-task="{{ task.task_id }}"
  data-retry-task-name="{{ task.bid_file.filename }}"
  data-retry-stage="{{ task.failed_stage }}"
  data-retry-stage-label="{{ row.failed_stage_label }}"
>重试</button>
```

增加一个 `data-retry-dialog`，包含两个 radio：

```html
<label><input type="radio" name="retry_from" value="failed_stage" checked> 从失败阶段开始</label>
<label><input type="radio" name="retry_from" value="start"> 从头开始</label>
```

脚本行为：点击行内按钮只打开 dialog，不触发任务链接；默认选择失败阶段；提交时 `fetch` POST JSON；成功关闭 dialog 并 `window.location.reload()`；失败显示 `detail` 文案并恢复按钮；取消、Escape、重复点击都不能遗留锁定状态。保留当前删除脚本行为。

详情页将阶段卡片按任务模式增加评标阶段卡片，使用新增状态字段和 `status_badge()`；评标模式仍显示跳过的合规阶段。`task.js` 的 `pollTask()` 在运行中更新七个逻辑阶段对应的 DOM，任何一个完成/失败仍通过整体状态决定刷新。

为避免缓存旧 CSS，更新 `base.html` 的静态资源版本号。

- [ ] **Step 4: Run page tests and inspect rendered markup**

Run:

```bash
pytest tests/test_pages.py tests/test_api.py -q
```

并用一次失败任务的页面响应检查：按钮只出现在 `failed` 行，完成/运行任务没有可点击重试按钮，弹窗两个选项和失败阶段文案完整。

- [ ] **Step 5: Commit the UI deliverable**

```bash
git add app/templates/bid_check_tasks.html app/templates/bid_check_task.html app/static/task.js app/static/bid-check.css app/templates/base.html tests/test_pages.py
git commit -m "feat: add retry controls to task list"
```

### Task 5: 完整回归、迁移检查与收尾

**Files:**
- Modify only if verification finds a concrete regression: `app/models.py`, `app/repository.py`, `app/workflow.py`, `app/api.py`, templates/static files, or tests。
- Test: all files under `tests/`。

**Interfaces:**
- Verifies the committed design at `docs/superpowers/specs/2026-09-10-task-retry-design.md`。

- [ ] **Step 1: Run the full test suite**

Run:

```bash
pytest -q
```

Expected: exit code `0` and zero failures/errors.

- [ ] **Step 2: Run syntax and diff checks**

Run:

```bash
python -m compileall app tests
git diff --check HEAD~4..HEAD
git status --short
```

Expected: Python compilation succeeds, diff has no whitespace errors, and only the user-existing `Dockerfile` modification remains unstaged/uncommitted aside from the feature commits.

- [ ] **Step 3: Verify migration behavior against a legacy schema**

Use a temporary SQLite file with the pre-feature `bid_check_tasks` schema, instantiate `BidCheckRepository`, and assert `get()` returns a task with the four new statuses defaulted/backfilled without changing the old three statuses. Keep this check in `tests/test_repository.py` as a regression test if it is not already covered.

- [ ] **Step 4: Verify requirement checklist from the spec**

Check each item explicitly:

- Original task ID and uploaded file paths are unchanged after retry.
- `start` executes all applicable stages.
- `failed_stage` executes only the failed stage and later applicable stages.
- Successful parallel sibling is skipped; two failed siblings both rerun.
- Evaluation-stage failures expose exact stage labels.
- Missing prefix artifacts produce `409` with a start-over instruction.
- Retry failure returns task to `failed` and allows a second retry.
- Complete/running/pending tasks cannot retry.
- UI shows both options and defaults to failed-stage retry.

- [ ] **Step 5: Commit only concrete verification fixes**

If the preceding steps found and fixed a regression, run the focused test and then:

```bash
git add app/models.py app/repository.py app/workflow.py app/api.py app/templates/bid_check_tasks.html app/templates/bid_check_task.html app/static/task.js app/static/bid-check.css app/templates/base.html tests
git commit -m "fix: harden failed task retry flow"
```

Do not stage or alter the pre-existing `Dockerfile` change.
