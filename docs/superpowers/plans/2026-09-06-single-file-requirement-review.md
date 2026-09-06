# 单份投标文件属性检查 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Web 合规性检查主流程中提取并确定性检查只依赖当前单份投标文件原始文件名、后缀和字节大小的文件属性要求，并生成独立 artifact 与统一页面结果。

**Architecture:** 在现有招标 MinerU blocks 上增加结构优先的完整候选区域裁剪和独立 `file_requirements` LLM 协议；通过来源 block 校验和范围过滤得到结构化规则。review 阶段新增单文件确定性执行器，读取 workflow 传入的原始上传文件 metadata 和存储文件 `stat()`，写入 `10_file_requirement_reviews.json`，再由现有 review 汇总和 Web 页面消费。

**Tech Stack:** Python 3.14、FastAPI、Jinja2、SQLite、pytest、现有 MinerU `/tasks` 适配器、现有 OpenAI-compatible LLM、`ComplianceExtractionRecorder`。

**Spec:** `docs/superpowers/specs/2026-09-06-single-file-requirement-review-design.md`

## Global Constraints

- 只检查约束对象 `single_bid_file`；不实现多文件、备份、文件组合、纸质/实体介质、密封或外部标记要求。
- 招标候选必须按结构保留完整章节/区域；不得使用 RAG 逐条召回或只按 `MB`/`PDF`/“文件”关键词截取文档块。
- “系统/平台最大支持上传”只能过滤，不得编译成投标文件强制上限。
- 文件大小必须使用原始上传存储路径的 `stat().st_size`；不得使用 MinerU 中间文件大小。
- 文件名/后缀必须使用用户上传时记录的原始 filename；任务内部固定存储名不能替代原始名。
- LLM 只提取和结构化规则；所有 pass/fail 判断由程序完成。
- 每条正式结果必须保留准确要求原文和真实来源 block ids；来源文本由后端从 blocks 回填。
- 独立产出物固定为 `compliance_extraction/10_file_requirement_reviews.json`，并纳入现有 review 汇总。
- 保留当前 Web 单文件 `.docx` 上传限制；本计划不扩展多文件上传或解析格式。
- 遵循 TDD：每个新增行为先写测试、确认测试因功能缺失失败，再写最小实现并运行回归测试。

---

### Task 1: 定义单文件规则协议与候选窗口边界

**Files:**
- Modify: `app/models.py`
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`
- Test: `tests/test_tender_requirement_simplification.py`

**Interfaces:**
- Produces `FileRequirement` TypedDict 和可选兼容的 `TenderExtractionResult["file_requirements"]`。
- Produces `CandidateWindow.kind == "file_requirements"`。
- Produces `build_file_requirement_candidates(blocks: Sequence[StructuredBlock]) -> list[CandidateWindow]`。

- [ ] **Step 1: Write the failing tests**

```python
def test_file_requirement_candidates_keep_front_table_as_one_complete_context():
    blocks = [
        block("b1", "heading", "投标人须知前附表", "第二章 投标人须知", 1),
        block("b2", "table", "文件格式 | PDF；大小 | 不得超过200MB；名称 | 包含项目名", "第二章 投标人须知", 2),
        block("b3", "heading", "投标文件格式", "第六章 投标文件格式", 3),
        block("b4", "paragraph", "模板正文", "第六章 投标文件格式", 4),
    ]

    candidates = build_file_requirement_candidates(blocks)

    assert len(candidates) == 1
    assert candidates[0].block_ids == ["b1", "b2"]
    assert "大小" in candidates[0].text
    assert "名称" in candidates[0].text


def test_file_requirement_candidates_include_semantic_bid_file_sections_without_fixed_chapter_number():
    blocks = [
        block("b1", "heading", "电子投标文件制作与上传说明", "说明", 1),
        block("b2", "paragraph", "电子投标文件不得超过200MB。", "说明", 2),
    ]

    candidates = build_file_requirement_candidates(blocks)

    assert [candidate.block_ids for candidate in candidates] == [["b1", "b2"]]


def test_file_requirement_candidates_exclude_large_unrelated_template_region():
    blocks = [
        block("b1", "heading", "合同条款", "合同条款", 1),
        block("b2", "paragraph", "合同履约内容。", "合同条款", 2),
        block("b3", "heading", "投标文件递交", "投标文件递交", 3),
        block("b4", "paragraph", "电子投标文件格式为PDF。", "投标文件递交", 4),
    ]

    candidates = build_file_requirement_candidates(blocks)

    assert [candidate.block_ids for candidate in candidates] == [["b3", "b4"]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_compliance_extraction.py -k 'file_requirement_candidates' -v`

Expected: FAIL because `build_file_requirement_candidates` and the new candidate kind do not exist.

- [ ] **Step 3: Write the minimal implementation**

Add the typed rule fields and extend the candidate kind without changing the old three-object protocol. Implement structural region grouping using heading levels/section boundaries, positive semantic title signals, negative unrelated-region signals, overlap deduplication, source-order sorting, and full front-table preservation. Keep candidate text as the complete joined block text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_compliance_extraction.py -k 'file_requirement_candidates' -v`

Expected: PASS with all new candidate tests green and no unrelated test failure.

- [ ] **Step 5: Run the extraction regression tests**

Run: `uv run pytest tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py -q`

Expected: existing three-collection behavior remains green.

### Task 2: Add the scoped LLM extraction protocol and deterministic filtering

**Files:**
- Modify: `app/compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`
- Test: `tests/test_tender_requirement_simplification.py`

**Interfaces:**
- Produces `DeterministicComplianceLLM.extract_file_requirements(candidates) -> dict[str, list[dict[str, Any]]]`.
- Produces `OpenAICompatibleLLM.extract_file_requirements(candidates) -> dict[str, list[dict[str, Any]]]`.
- Produces `_coerce_file_requirement_output(value) -> list[dict[str, Any]]`.
- Produces `normalize_file_requirements(raw_items, blocks, candidates) -> tuple[list[FileRequirement], list[dict[str, Any]]]` where the second value is debug-only filtered records.

- [ ] **Step 1: Write the failing tests**

```python
def test_file_requirement_schema_keeps_size_extension_and_filename_rules_from_one_cell():
    raw = {
        "file_requirements": [
            {
                "name": "文件大小",
                "requirement": "电子投标文件不得超过200MB",
                "target": "single_bid_file",
                "requirement_type": "size",
                "constraint_status": "explicit_constraint",
                "parameters": {"operator": "max", "value": 200, "unit": "MB"},
                "auto_checkable": True,
                "support_reason": "字节数可比较",
                "source_block_ids": ["b2"],
            },
            {
                "name": "文件格式",
                "requirement": "电子投标文件采用PDF格式",
                "target": "single_bid_file",
                "requirement_type": "extension",
                "constraint_status": "explicit_constraint",
                "parameters": {"allowed_extensions": [".pdf"]},
                "auto_checkable": True,
                "support_reason": "后缀可比较",
                "source_block_ids": ["b2"],
            },
            {
                "name": "文件名称",
                "requirement": "文件名称应包含投标文件-华北公司",
                "target": "single_bid_file",
                "requirement_type": "filename",
                "constraint_status": "explicit_constraint",
                "parameters": {"required_literals": ["投标文件", "华北公司"]},
                "auto_checkable": True,
                "support_reason": "文件名字符串可比较",
                "source_block_ids": ["b2"],
            },
        ]
    }

    output = _coerce_file_requirement_output(raw)

    assert len(output) == 3


def test_platform_upload_capacity_is_filtered_from_formal_file_requirements():
    blocks = [StructuredBlock("b2", "paragraph", "系统最大支持上传500MB文件", "前附表", 2)]
    candidates = [CandidateWindow(["b2"], "前附表", blocks[0].text, 2, kind="file_requirements")]
    raw = {
        "file_requirements": [{
            "name": "平台容量",
            "requirement": "系统最大支持上传500MB文件",
            "target": "single_bid_file",
            "requirement_type": "size",
            "constraint_status": "platform_capability",
            "parameters": {"operator": "max", "value": 500, "unit": "MB"},
            "auto_checkable": True,
            "support_reason": "平台说明",
            "source_block_ids": ["b2"],
        }]
    }

    formal, filtered = normalize_file_requirements(raw["file_requirements"], blocks, candidates)

    assert formal == []
    assert filtered[0]["reason"] == "platform_capability"


def test_non_single_file_submission_rules_are_filtered():
    blocks = [StructuredBlock("b2", "paragraph", "商务标和技术标应分别上传", "前附表", 2)]
    candidates = [CandidateWindow(["b2"], "前附表", blocks[0].text, 2, kind="file_requirements")]
    raw_item = {
        "name": "分别提交",
        "requirement": "商务标和技术标应分别上传",
        "target": "file_collection",
        "requirement_type": "other",
        "constraint_status": "explicit_constraint",
        "parameters": {},
        "auto_checkable": False,
        "support_reason": "需要多个文件关系",
        "source_block_ids": ["b2"],
    }

    formal, filtered = normalize_file_requirements([raw_item], blocks, candidates)

    assert formal == []
    assert filtered[0]["reason"] == "out_of_scope_target"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py -k 'file_requirement_schema or platform_upload_capacity or non_single_file' -v`

Expected: FAIL because the new schema/coercer/filter functions do not exist.

- [ ] **Step 3: Write the minimal implementation**

Add a strict independent schema for `file_requirements`; reject unknown fields, invalid target/type/status, missing source ids, and malformed parameters. Add a dedicated OpenAI prompt that explicitly lists the in-scope types and out-of-scope examples. Add deterministic fallback extraction for normative single-file size/extension/name statements, with negative platform-capability checks. Rebuild source text from real blocks and normalize size units, suffixes, and filename literal parameters. Do not change `_coerce_object_output` for the old collections.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py -k 'file_requirement_schema or platform_upload_capacity or non_single_file' -v`

Expected: PASS.

- [ ] **Step 5: Verify the prompt boundary**

Run: `uv run pytest tests/test_tender_requirement_simplification.py -k 'file_requirement_prompt' -v`

Expected: PASS, asserting the prompt mentions single-file metadata and explicitly excludes multi-file, backup, paper, physical media, sealing, combinations, and platform capability descriptions.

### Task 3: Integrate extraction, caching, provenance, and extraction artifacts

**Files:**
- Modify: `app/compliance_extraction.py`
- Modify: `app/models.py`
- Test: `tests/test_compliance_extraction.py`
- Test: `tests/test_tender_requirement_simplification.py`

**Interfaces:**
- `extract_tender_compliance_objects(...)` returns `file_requirements` in addition to the existing three collections.
- `07_result.json` includes `file_requirements`; `summary.json.stats` includes candidate character and rule counts.
- Existing parser/result cache keys invalidate when the file-rule prompt/schema changes.

- [ ] **Step 1: Write the failing tests**

```python
def test_main_extractor_returns_file_requirements_and_source_artifact(tmp_path):
    tender = tmp_path / "tender.docx"
    tender.write_bytes(b"tender")
    blocks = [
        StructuredBlock("b1", "heading", "投标人须知前附表", "前附表", 1),
        StructuredBlock("b2", "table", "大小：电子投标文件不得超过200MB；格式：PDF", "前附表", 2),
    ]

    class Parser:
        def parse(self, path):
            return blocks

    class LLM:
        model = "test-model"
        def extract(self, batch):
            return {"templates": [], "project_requirements": [], "supplemental_materials": []}
        def extract_file_requirements(self, candidates):
            return {"file_requirements": [{
                "name": "文件大小",
                "requirement": "电子投标文件不得超过200MB",
                "target": "single_bid_file",
                "requirement_type": "size",
                "constraint_status": "explicit_constraint",
                "parameters": {"operator": "max", "value": 200, "unit": "MB"},
                "auto_checkable": True,
                "support_reason": "可比较",
                "source_block_ids": ["b2"],
            }]}

    recorder = ComplianceExtractionRecorder(tmp_path / "task")
    result = extract_tender_compliance_objects(
        FileMetadata("招标.docx", tender.stat().st_size, str(tender)),
        parser=Parser(), llm=LLM(), recorder=recorder,
    )

    assert result["file_requirements"][0]["parameters"]["limit_bytes"] > 0
    artifact = json.loads((recorder.artifact_dir / "07_result.json").read_text())
    assert artifact["file_requirements"][0]["source"]["block_ids"] == ["b2"]
    summary = json.loads((recorder.artifact_dir / "summary.json").read_text())
    assert summary["stats"]["file_requirement_candidate_chars"] > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py -k 'main_extractor_returns_file_requirements' -v`

Expected: FAIL because extraction does not yet call the file-rule protocol or persist its stats.

- [ ] **Step 3: Write the minimal implementation**

Extend the empty/result/normalization/cache validity paths to support an optional fourth collection. Run file candidate selection after blocks are parsed, send the complete candidate list once through the dedicated method, normalize and filter returned rules, and persist them in `07_result.json`. Add file candidate count, candidate chars, request chars, extracted count, filtered count and filtered-reason counts to extraction stats. Bump `REQUIREMENT_PROMPT_VERSION`/cache version. Keep result-cache compatibility by treating cached results without `file_requirements` as an empty fourth collection only when they were produced by the new cache version.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py -k 'main_extractor_returns_file_requirements' -v`

Expected: PASS.

- [ ] **Step 5: Run the complete extraction regression suite**

Run: `uv run pytest tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py -q`

Expected: all extraction and cache tests pass, including old three-collection and source-normalization cases.

### Task 4: Implement raw original-file metadata inspection and deterministic checks

**Files:**
- Create: `app/file_requirement_review.py`
- Test: `tests/test_file_requirement_review.py`

**Interfaces:**
- Produces `inspect_original_bid_file(file_metadata: FileMetadata) -> dict[str, Any]`.
- Produces `check_file_requirement(requirement: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]`.
- Produces `run_file_requirement_review(extraction_result, parsed_bid, recorder=None) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_size_check_uses_original_storage_bytes_not_cleaning_artifact_size(tmp_path):
    original = tmp_path / "bid.docx"
    original.write_bytes(b"x" * 216)
    cleaning = tmp_path / "bid_document_cleaning"
    cleaning.mkdir()
    (cleaning / "structured_document.json").write_text("{}", encoding="utf-8")

    actual = inspect_original_bid_file(
        FileMetadata("投标文件.docx", 216, str(original))
    )
    result = check_file_requirement(
        {"id": "r1", "name": "大小", "requirement": "不得超过200B", "requirement_type": "size",
         "parameters": {"operator": "max", "limit_bytes": 200}, "auto_checkable": True},
        actual,
    )

    assert actual["size_bytes"] == 216
    assert result["status"] == "fail"


def test_extension_check_is_case_insensitive_and_uses_original_filename(tmp_path):
    original = tmp_path / "stored.docx"
    original.write_bytes(b"data")
    actual = inspect_original_bid_file(FileMetadata("投标响应.PDF", 4, str(original)))

    result = check_file_requirement(
        {"id": "r1", "name": "格式", "requirement": "采用PDF格式", "requirement_type": "extension",
         "parameters": {"allowed_extensions": [".pdf"]}, "auto_checkable": True},
        actual,
    )

    assert actual["extension"] == ".pdf"
    assert result["status"] == "pass"


def test_dynamic_filename_rule_is_retained_as_not_supported():
    actual = {"filename": "投标文件-华北公司.docx", "extension": ".docx", "size_bytes": 4}
    result = check_file_requirement(
        {"id": "r1", "name": "命名", "requirement": "文件名称应包含项目名称和投标人名称",
         "requirement_type": "filename", "parameters": {"dynamic_components": ["项目名称", "投标人名称"]},
         "auto_checkable": False, "support_reason": "缺少可比较的具体字面值"},
        actual,
    )

    assert result["status"] == "not_supported"
    assert result["status_label"] == "无法自动检查"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_file_requirement_review.py -v`

Expected: FAIL because the new review module and deterministic metadata functions do not exist.

- [ ] **Step 3: Write the minimal implementation**

Read `Path(file_metadata.storage_path).stat()` for the actual size, retain `Path(file_metadata.filename).name` as the original display name, derive a normalized suffix and MIME type, and compute SHA-256 for audit. Implement only data-driven comparisons: size operators, allowed suffixes, exact/prefix/suffix/required-literal filename checks. Return `not_supported` with a reason for `auto_checkable=false`, unsupported types, missing concrete filename literals, missing actual files, or malformed parameters. Never invoke an LLM in this module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_file_requirement_review.py -v`

Expected: PASS for size, extension, filename and unsupported-rule tests.

- [ ] **Step 5: Add boundary cases and rerun**

Add tests for MB/MiB byte normalization, uppercase suffixes, exact file name, forbidden filename literals, minimum size, and a missing storage path. Run `uv run pytest tests/test_file_requirement_review.py -q` and expect PASS.

### Task 5: Write the independent artifact and merge it into the existing review result

**Files:**
- Modify: `app/file_requirement_review.py`
- Modify: `app/attachment_review.py`
- Modify: `app/workflow.py`
- Test: `tests/test_file_requirement_review.py`
- Test: `tests/test_workflow.py`
- Test: `tests/test_end_to_end.py`

**Interfaces:**
- `10_file_requirement_reviews.json` is written by `run_file_requirement_review` for every completed review attempt, including an empty rule list.
- `run_compliance_review_with_attachments(...)` returns `file_requirement_reviews` and `file_requirement_stats` alongside existing result collections.
- `BidCheckWorkflow` passes `task.bid_file.to_dict()` to the review input as `original_file_metadata` while preserving existing two-stage concurrency.

- [ ] **Step 1: Write the failing tests**

```python
def test_file_review_writes_complete_independent_artifact(tmp_path):
    original = tmp_path / "bid.docx"
    original.write_bytes(b"x" * 216)
    recorder = ComplianceExtractionRecorder(tmp_path / "task")
    result = run_file_requirement_review(
        {"file_requirements": [{
            "id": "file_requirement_001", "name": "大小", "requirement": "不得超过200B",
            "requirement_type": "size", "target": "single_bid_file",
            "parameters": {"operator": "max", "limit_bytes": 200},
            "auto_checkable": True, "source": {"section": "前附表", "block_ids": ["b1"], "source_text": "不得超过200B"},
        }]},
        {"original_file_metadata": FileMetadata("原始投标文件.docx", 216, str(original)).to_dict()},
        recorder=recorder,
    )

    artifact = json.loads((recorder.artifact_dir / "10_file_requirement_reviews.json").read_text())
    assert artifact["file_metadata"]["filename"] == "原始投标文件.docx"
    assert artifact["file_requirement_reviews"][0]["actual"]["size_bytes"] == 216
    assert artifact["file_requirement_reviews"][0]["status"] == "fail"
    assert result["stats"]["failed_count"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_file_requirement_review.py -k 'independent_artifact' -v`

Expected: FAIL because the artifact writer and workflow metadata handoff do not exist.

- [ ] **Step 3: Write the minimal implementation**

Build the artifact payload with schema version, extraction candidate stats, every formal rule, source data, actual file metadata, required/actual values, status labels, reasons, and aggregate counts. Add Recorder events for start/end and use atomic `write_json`. In `BidCheckWorkflow`, copy the bid task metadata into the review input; do not replace the original `bid_parse` artifact. Call the new review after existing template/attachment/performance reviews and merge its fields into the same `review_result` object. Use a mode label that reflects file requirements only when rules exist.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_file_requirement_review.py tests/test_workflow.py tests/test_end_to_end.py -q`

Expected: PASS, including existing two-stage concurrency and completed-task persistence tests.

- [ ] **Step 5: Verify cache and artifact compatibility**

Run: `uv run pytest tests/test_tender_requirement_simplification.py tests/test_performance_review.py -q`

Expected: PASS with no changes to old attachment/performance artifacts or result fields.

### Task 6: Make final aggregation read the file artifact

**Files:**
- Modify: `app/api.py`
- Test: `tests/test_api.py`
- Test: `tests/test_pages.py`

**Interfaces:**
- Produces `_load_file_requirement_reviews_for_page(task: BidCheckTask) -> list[dict[str, Any]]`.
- `_task_issue_counts` includes a `file` count.
- `bid_check_task_page` supplies `file_requirement_reviews` from `10_file_requirement_reviews.json`, falling back to `review_result` only when the artifact is unavailable.

- [ ] **Step 1: Write the failing tests**

```python
def test_task_issue_counts_include_not_supported_file_rules_from_artifact(client, stored_task, settings, repository):
    artifact_dir = settings.tasks_dir / stored_task.task_id / "compliance_extraction"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "10_file_requirement_reviews.json").write_text(json.dumps({
        "file_requirement_reviews": [{"status": "not_supported"}],
    }), encoding="utf-8")
    stored_task_result = {"review_result": {"template_text_reviews": [], "attachment_reviews": [], "performance_reviews": []}}
    stored_task_result["review_result"]["file_requirement_reviews"] = []
    repository.complete(stored_task.task_id, stored_task_result)

    assert _task_issue_counts(repository.get(stored_task.task_id))["file"] == 1
```

```python
def test_task_page_renders_all_file_requirement_statuses(client, stored_task, settings):
    # Persist a completed review result and a matching 10_ artifact containing
    # pass, fail, and not_supported rows, then request the HTML page.
    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")
    assert response.status_code == 200
    assert "文件属性检查" in response.text
    assert "无法自动检查" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api.py tests/test_pages.py -k 'file_requirement' -v`

Expected: FAIL because the API does not read the new artifact or expose a file issue count.

- [ ] **Step 3: Write the minimal implementation**

Read and validate the artifact JSON by task-relative path, normalize missing/non-list values to an empty list, and use the task result fallback only if reading fails. Add the file category to list-page counts and detail-page context. Keep the existing template/attachment/performance counting semantics unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api.py tests/test_pages.py -k 'file_requirement' -v`

Expected: PASS.

- [ ] **Step 5: Run all API/page tests**

Run: `uv run pytest tests/test_api.py tests/test_pages.py -q`

Expected: PASS with existing task creation, deletion, status, and page rendering tests unchanged.

### Task 7: Add unified file-property result rendering in all existing Web styles

**Files:**
- Modify: `app/templates/bid_check_task.html`
- Modify: `app/static/bid-check.css`
- Test: `tests/test_pages.py`

**Interfaces:**
- Adds a file-property section to workbench, dashboard, and report views.
- Default detailed table renders every rule, not only failures.
- Status labels are exactly `合规`、`不合规`、`无法自动检查`.

- [ ] **Step 1: Write the failing tests**

```python
def test_complete_page_shows_file_property_requirements_actual_metadata_and_source(client, stored_task):
    # Complete the task with one pass, one fail, and one unsupported file rule.
    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")

    assert "文件属性检查" in response.text
    assert "招标要求" in response.text
    assert "实际文件" in response.text
    assert "原文依据" in response.text
    assert "无法自动检查" in response.text
```

```python
def test_complete_page_file_property_problem_count_excludes_pass_rows(client, stored_task):
    response = client.get(f"/bid-check/tasks/{stored_task.task_id}")
    assert "1 项问题" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pages.py -k 'file_property' -v`

Expected: FAIL because the result template has no file-property data or section.

- [ ] **Step 3: Write the minimal implementation**

Add a Jinja macro for file requirement cards/rows; render requirement name/type, original tender wording, parameters, actual filename/extension/size, status label, source block ids/source text, and reason. Extend workbench metrics and final issue counts; add one dashboard navigation/metric/panel and one report index/stat/section. Reuse existing `comparison-status-*`, `review-results-table`, `final-issue-card`, and responsive styles. Add only small file-property metadata/table styles where needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pages.py -k 'file_property' -v`

Expected: PASS and HTML contains all three result states, actual metadata, and source evidence.

- [ ] **Step 5: Run all page tests**

Run: `uv run pytest tests/test_pages.py -q`

Expected: PASS for all existing three-style, hidden-debug, navigation, attachment, performance, and template result tests.

### Task 8: Update documentation and add the real-task acceptance runner

**Files:**
- Modify: `README.md`
- Create: `tests/test_file_requirement_real_task.py`

**Interfaces:**
- README documents candidate source sections, single-file metadata source, artifact name, status boundary, and current `.docx` upload limitation.
- Real-task test/runner reads the existing task `5f77e206-6aca-4151-a949-35d3b509be26` only when its files and services are available, and prints a compact audit summary without exposing secrets.

- [ ] **Step 1: Write the failing acceptance test**

```python
def test_real_task_artifact_contract_is_readable_when_existing_task_is_available():
    task_dir = Path("data/tasks/5f77e206-6aca-4151-a949-35d3b509be26")
    if not (task_dir / "tender.docx").is_file() or not (task_dir / "bid.docx").is_file():
        pytest.skip("现有真实任务文件不可用")
    artifact = task_dir / "compliance_extraction" / "10_file_requirement_reviews.json"
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert isinstance(payload.get("file_requirement_reviews"), list)
    assert isinstance(payload.get("file_metadata"), dict)
```

- [ ] **Step 2: Run the test to verify the expected failure before integration**

Run: `uv run pytest tests/test_file_requirement_real_task.py -v`

Expected before implementation: FAIL on the missing `10_file_requirement_reviews.json` artifact when the existing task is available.

- [ ] **Step 3: Write documentation and the read-only audit runner**

Document the exact scope exclusions and the full workflow. Add a small test helper/runner that reads the generated artifact, reports candidate chars, extracted rule names, automatic/unsupported counts, original filename/size, and final statuses. It must not print API keys or raw LLM credentials and must not mutate the real task files.

- [ ] **Step 4: Run the real-task audit after the full workflow is wired**

Run: `uv run pytest tests/test_file_requirement_real_task.py -v`

Expected: PASS if the existing task has been rerun through the new workflow; otherwise the test reports the artifact is stale and the implementation pass must execute the task workflow once before completion.

- [ ] **Step 5: Validate the artifact contents manually**

Run: `uv run python -c 'import json; from pathlib import Path; p=Path("data/tasks/5f77e206-6aca-4151-a949-35d3b509be26/compliance_extraction/10_file_requirement_reviews.json"); d=json.loads(p.read_text()); print(json.dumps({"candidate_chars": d.get("candidate_context", {}).get("candidate_text_chars"), "rules": [{"name": r.get("name"), "status": r.get("status"), "requirement": r.get("requirement"), "actual": r.get("actual")} for r in d.get("file_requirement_reviews", [])], "file_metadata": d.get("file_metadata")}, ensure_ascii=False, indent=2))'`

Expected: compact JSON showing the actual original bid filename/size, each retained requirement, and its status.

### Task 9: Full verification and completion review

**Files:**
- Test: all existing tests and new file-level tests
- Inspect: `git diff --stat`, `10_file_requirement_reviews.json`, rendered task page

- [ ] **Step 1: Run the focused test suite**

Run: `uv run pytest tests/test_file_requirement_review.py tests/test_compliance_extraction.py tests/test_tender_requirement_simplification.py tests/test_api.py tests/test_pages.py tests/test_workflow.py tests/test_end_to_end.py -q`

Expected: 0 failures.

- [ ] **Step 2: Run the full regression suite**

Run: `uv run pytest -q`

Expected: all tests pass with no unexpected warnings or errors.

- [ ] **Step 3: Inspect the final diff and artifact**

Run: `git diff --stat && git status --short && sed -n '1,260p' data/tasks/5f77e206-6aca-4151-a949-35d3b509be26/compliance_extraction/10_file_requirement_reviews.json`

Expected: only the planned code/docs/tests are changed in addition to pre-existing user changes; the artifact contains formal single-file rules, source blocks, real metadata, actual values, and statuses.

- [ ] **Step 4: Verify Web output**

Run: `uv run pytest tests/test_pages.py -q` and open the completed task page at `/bid-check/tasks/5f77e206-6aca-4151-a949-35d3b509be26` when the local app is running.

Expected: the file-property category appears in the same result flow and all rows expose requirement, actual value, status, and source evidence.
