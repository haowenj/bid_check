# 投标文件 MinerU 数据清洗与结构整理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. 本次按用户要求使用当前会话内联执行，不创建分支/worktree，不使用子 agent。

**Goal:** 将投标文件 MinerU 原始结果转换为保守清洗、章节清晰、表格/图片独立且来源可追溯的结构化文档产物，并接入现有 `bid_parse` 阶段。

**Architecture:** 新增 `app/bid_document.py`，把 MinerU `/tasks` 获取、ZIP 内容/资源保存、清洗、跨页正文整理和 `StructuredBlock` 结构化封装在一个投标文件专用适配层内。保留现有 `StructuredBlock` 作为 block 协议，原始 content list、清洗列表、合并列表和结构化文档分别落盘；`app/api.py` 只负责注入配置好的 parser，现有审查 mock 不读取清洗内容。

**Tech Stack:** Python 3.14+, `httpx`, `pytest`, `zipfile`, `xml.etree`-free JSON normalization, existing FastAPI workflow and `StructuredBlock`.

**Spec:** `docs/superpowers/specs/2026-09-01-bid-document-cleaning-design.md`

## Global Constraints

- 不创建 Git 分支或 worktree；所有代码在当前 `main` 工作区完成。
- 不调用或安装 `gh`、Gitee CLI、插件、连接器或子 agent。
- 不修改或覆盖已经提交的招标文件要求提取逻辑，除非为类型/依赖注入所必需。
- 不加入模板匹配、fields/attachments 校验、RAG、LLM 判断、签字盖章识别或最终检查规则。
- 原始 MinerU content list 字节单独保存，清洗结果不得替代原始结果。
- 表格和图片必须作为独立 block 保留；未知对象和缺失资源不得静默丢弃。

## 文件结构

- Create: `app/bid_document.py` — 投标文件 MinerU 拉取、清洗、合并、章节整理、产物写入和 CLI。
- Modify: `app/api.py` — 默认 workflow 注入真实投标文件 parser，保留 parser 依赖注入。
- Modify: `tests/conftest.py` — 给 workflow 测试注入无网络 fixture parser。
- Create: `tests/test_bid_document.py` — 纯清洗、结构化、归档资源和 parser 协议测试。
- Modify: `README.md` — 记录 bid_parse 真实产物和本轮范围。
- Create during manual verification only: `data/tasks/<task-id>/bid_document_cleaning/` 下的真实样本产物；`data/` 已被 `.gitignore` 忽略。

### Task 1: 保守展平与噪声清洗

**Files:**
- Create: `tests/test_bid_document.py`
- Create: `app/bid_document.py`

**Interfaces:**
- Produces `flatten_mineru_content_list(payload: Any) -> list[Any]`。
- Produces `clean_items(items: Sequence[Any]) -> tuple[list[Any], list[dict[str, Any]]]`。
- Each flattened dictionary carries `_bid_source.source_path`, `_bid_source.raw_item_index`, and optional parent/child indexes.

- [ ] **Step 1: Write the failing test**

  Create a fixture payload containing a v2 envelope, nested title/paragraph/list/table/image, a `page_number`, a `header`, whitespace text, one punctuation-only text, a `footer`, an unknown object type, and a non-dict value. Assert flattening preserves order and source paths, cleaning removes only the four deterministic noise cases, and table/image/footer/unknown/non-dict values remain.

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: FAIL with `ModuleNotFoundError: No module named 'app.bid_document'`.

- [ ] **Step 3: Write minimal implementation**

  Implement the flattening cases from the current MinerU shapes (`title`, `paragraph`, `list`, `table`, `image`/`figure`) and preserve the untouched raw payload for later artifact writing. Implement `_should_remove` with only `page_number`, `header`, blank body text, and one punctuation body text. Keep `footer`, nonstandard types, table and image objects.

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: PASS for the new cleaning tests.

- [ ] **Step 5: Commit**

  Run: `git add app/bid_document.py tests/test_bid_document.py && git commit -m "feat: add conservative bid content cleaning"`.

### Task 2: 跨页正文合并与来源链

**Files:**
- Modify: `tests/test_bid_document.py`
- Modify: `app/bid_document.py`

**Interfaces:**
- Produces `merge_items(items: Sequence[Any]) -> tuple[list[Any], list[dict[str, Any]]]`。
- A merged item includes `source_item_indices`, `source_page_indices`, `source_bboxes`, `start_page_idx`, `end_page_idx`, and `merged_cross_page=True`.

- [ ] **Step 1: Write the failing test**

  Add a positive case where an unfinished bottom-of-page paragraph joins a top-of-next-page paragraph, and assert the original list is unchanged, the merged text is ordered, all source indexes/pages/bboxes are present, and the merge log stores both input objects. Add negative cases for headings, tables/images between blocks, complete sentence endings, colon endings, non-edge positions, and blocks with heading levels.

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: FAIL because `merge_items` is missing.

- [ ] **Step 3: Write minimal implementation**

  Adapt the contract project’s geometric and punctuation guards to the bid stream, but keep `_bid_source` provenance and never merge across a non-text item. Compute page bounds from the provided items, use a 20% edge threshold, and deep-copy all results/logs.

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: PASS with merge positive and boundary tests green.

- [ ] **Step 5: Commit**

  Run: `git add app/bid_document.py tests/test_bid_document.py && git commit -m "feat: preserve bid cross-page source chains"`.

### Task 3: 章节、表格、图片结构化

**Files:**
- Modify: `tests/test_bid_document.py`
- Modify: `app/bid_document.py`

**Interfaces:**
- Produces `structure_content_list(items: Sequence[Any], *, source_filename: str, source_sha256: str, parser_diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]`。
- Returns top-level `schema_version`, `document`, `stats`, `sections`, `blocks`, `tables`, `images`, and `relationships`.
- Uses existing `StructuredBlock` fields in every `blocks[]` entry.

- [ ] **Step 1: Write the failing test**

  Add a mixed stream with level-1/level-2 headings, paragraph, table HTML, image with `img_path`, and a paragraph after the image. Assert the output has hierarchical section paths, every mixed item has a section id/path, table and image appear as independent types and indexes, source metadata includes raw indexes/pages/bboxes, and image adjacency identifies the nearby paragraph/block ids.

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: FAIL because `structure_content_list` is missing.

- [ ] **Step 3: Write minimal implementation**

  Build a heading stack from MinerU levels or limited numbering inference, assign immediate `section` plus metadata `section_id`/`section_path`, serialize `StructuredBlock` without changing its established field names, and derive independent table/image indexes. Use stable `s0001` and `b0001`-style ids based on document order and preserve merged source lists. Add previous/next relationships for each block.

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: PASS with section and mixed-content tests green.

- [ ] **Step 5: Commit**

  Run: `git add app/bid_document.py tests/test_bid_document.py && git commit -m "feat: structure bid sections tables and images"`.

### Task 4: MinerU ZIP、图片资源和完整产物

**Files:**
- Modify: `tests/test_bid_document.py`
- Modify: `app/bid_document.py`

**Interfaces:**
- Produces `MinerUBidDocumentParser.parse(path: Path, *, output_dir: Path) -> dict[str, Any]`。
- Parser calls `/tasks`, polls the trusted task URL, downloads a ZIP, saves raw content bytes, safely extracts referenced assets, and writes the seven JSON artifacts from the spec.

- [ ] **Step 1: Write the failing test**

  Add an `httpx.MockTransport` case returning `202` submission, `completed` status, and a ZIP containing `bid_content_list.json`, a valid table/image asset, and an unsafe `../escape.jpg` reference. Call the parser with a temporary DOCX-like file and `poll_interval_seconds=0`. Assert the raw JSON bytes equal the ZIP member bytes, the safe asset is written below `images/`, the unsafe asset is not written, all artifact files exist, stats include raw/cleaned/merged/structured counts, and the returned result points to the artifact paths.

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: FAIL because `MinerUBidDocumentParser` is missing.

- [ ] **Step 3: Write minimal implementation**

  Implement the existing project `/tasks` protocol with current `MINERU_URL` conventions, strict same-host task URLs, ZIP path traversal rejection, v2-member preference, atomic JSON writes, exact raw-byte persistence, safe asset extraction, cleaning/merge/structure calls, summary/log generation, and non-secret parser diagnostics. Keep missing assets as `asset_status="missing"` in image/table metadata instead of removing their blocks.

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/test_bid_document.py -q`

  Expected: PASS for parser, ZIP safety, artifact, and statistics tests.

- [ ] **Step 5: Commit**

  Run: `git add app/bid_document.py tests/test_bid_document.py && git commit -m "feat: persist bid MinerU cleaning artifacts"`.

### Task 5: 默认工作流接入与测试隔离

**Files:**
- Modify: `app/api.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_end_to_end.py`
- Modify: `tests/test_workflow.py` only if parser injection coverage requires it
- Modify: `app/bid_document.py`

**Interfaces:**
- `build_default_workflow(..., bid_document_parser: BidDocumentParser | None = None)` injects the real parser by default.
- `parse_bid_document(bid_file: FileMetadata, *, parser: BidDocumentParser) -> dict[str, Any]` derives the per-task `bid_document_cleaning` directory and returns status/name/stats/artifact paths.

- [ ] **Step 1: Write the failing test**

  Add a fixture parser that records the received source path/output directory and returns a small structured result. Assert the default workflow passes the bid file to it, the API response includes the real bid parse stats/artifact directory, and the existing review result remains the current mock message. Keep the legacy `mock_services.parse_bid_document` unit tests unchanged as compatibility-helper tests.

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/test_api.py tests/test_end_to_end.py tests/test_workflow.py -q`

  Expected: FAIL because `build_default_workflow` has no bid parser injection and still wires the fixed mock parser.

- [ ] **Step 3: Write minimal implementation**

  Import the new parser/function in `app/api.py`, construct it from existing `Settings` fields, add the optional injected parser parameter, and wire `parse=partial(...)`. Update `tests/conftest.py` to inject the fixture parser for API/workflow tests; do not make tests depend on a live MinerU service.

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/test_api.py tests/test_end_to_end.py tests/test_workflow.py -q`

  Expected: PASS with all existing workflow/API behavior preserved and the new parser result protocol asserted.

- [ ] **Step 5: Commit**

  Run: `git add app/api.py app/bid_document.py tests/conftest.py tests/test_api.py tests/test_end_to_end.py tests/test_workflow.py && git commit -m "feat: run bid parse through MinerU cleaner"`.

### Task 6: 文档、全量验证和真实投标文件运行

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Write the failing test**

  No new production behavior is introduced in this task; use the existing artifact/CLI tests as the executable acceptance contract.

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest -q` before documentation changes only if the prior task did not already run it; expected baseline is at least the prior 120 tests plus the new cleaning tests.

- [ ] **Step 3: Write minimal implementation**

  Document the new `bid_document_cleaning/` artifact layout, conservative rules, source trace fields, and explicit exclusion of later checks. Do not claim live MinerU success in README.

- [ ] **Step 4: Run tests and execute the real sample**

  Run: `uv run pytest -q` and then run `uv run python -m app.bid_document data/tasks/f5a91fe3-a4f1-4a49-b2bc-788c618d590e/bid.docx --output-dir data/tasks/f5a91fe3-a4f1-4a49-b2bc-788c618d590e/bid_document_cleaning`.

  Expected: full test suite passes; live run either produces raw/cleaned/merged/structured/summary artifacts and real counts or exits with a clear MinerU error without fabricated counts.

- [ ] **Step 5: Commit**

  Run: `git add README.md && git commit -m "docs: describe bid cleaning artifacts"`.

## Final self-review checklist

- The spec requirements map to Tasks 1–4 and workflow/acceptance requirements map to Tasks 5–6.
- No placeholder steps, invented external services, fixed chapter numbers, or LLM/RAG behavior are present.
- Existing `StructuredBlock` field names remain the block protocol.
- Raw source index, source page/bbox, section path, table body, image path, and asset status are all covered by tests.
- Full tests and the real `bid.docx` run are required before reporting completion.
