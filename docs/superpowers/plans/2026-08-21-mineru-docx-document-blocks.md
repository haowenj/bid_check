# MinerU DOCX → DocumentBlock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a small Python project that sends DOCX files to MinerU 3.4.4 at port 7100, preserves every raw parsing artifact, and converts actual Office-backend content blocks into stable `DocumentBlock` JSON with correct section paths and reports.

**Architecture:** A thin HTTP client owns upload, raw ZIP persistence, safe extraction, and manifests. A DOCX-specific normalizer selects the observed `content_list_v2` shape with a legacy fallback, maps one natural MinerU block to at most one `DocumentBlock`, and maintains a heading stack. Reporting and CLI orchestration remain separate and depend only on stable models.

**Tech Stack:** Python 3.11+, dataclasses, enum, pathlib, requests, python-docx, Pillow, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-mineru-docx-document-blocks-design.md`

## Global Constraints

- Only implement DOCX → MinerU raw outputs → `DocumentBlock`; do not add Agent, RAG, vector database, Web, or long-text splitting features.
- One MinerU natural block maps to at most one `DocumentBlock`; preserve source order and never merge adjacent blocks.
- Derive DOCX mappings from the live MinerU 3.4.4 Office output, never from assumed PDF fields.
- Prefer `content_list_v2.json`; fall back to `content_list.json` only when v2 is missing or structurally unsupported.
- Never align middle, legacy, and v2 blocks by array position alone.
- Preserve the raw ZIP before extraction and keep raw JSON/images available even when normalization fails.
- Use Python 3.11+, `src/` layout, standard-library dataclasses/enums, `pathlib.Path`, and UTF-8 JSON with `ensure_ascii=False` and two-space indentation.
- Do not install or invoke GitHub CLI, Gitee CLI, or any Git hosting desktop client.
- Follow red-green-refactor for every production behavior and run fresh verification before each completion claim or commit.

## Planned File Structure

```text
bid_check/
├── .gitignore
├── README.md
├── pyproject.toml
├── docs/
│   ├── mineru-docx-output-analysis.md
│   └── superpowers/
│       ├── plans/2026-08-21-mineru-docx-document-blocks.md
│       └── specs/2026-08-21-mineru-docx-document-blocks-design.md
├── scripts/
│   ├── generate_test_docx.py
│   └── parse_docx.py
├── src/bid_check/
│   ├── __init__.py
│   ├── mineru_client.py
│   ├── models.py
│   ├── normalizer.py
│   └── reporting.py
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── generated/mineru_docx_features.docx
    │   └── mineru_3_4_4_docx/
    │       ├── content_list.json
    │       ├── content_list_v2.json
    │       └── middle_excerpt.json
    ├── integration/test_mineru_docx.py
    ├── test_mineru_client.py
    ├── test_models.py
    ├── test_normalizer.py
    ├── test_parse_docx_cli.py
    └── test_reporting.py
```

Responsibilities are fixed: `models.py` owns stable schema; `mineru_client.py` owns transport/raw files/manifests; `normalizer.py` owns observed shape adapters/mapping/section paths; `reporting.py` owns statistics; scripts only generate fixtures or orchestrate public interfaces.

---

### Task 1: Project metadata and stable models

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `src/bid_check/__init__.py`
- Create: `src/bid_check/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces: `BlockType(str, Enum)` with every value from the design.
- Produces: `TableContent`, `ImageContent`, and `DocumentBlock` dataclasses.
- Produces: `DocumentBlock.to_dict() -> dict[str, object]` and `blocks_to_jsonable(blocks: Sequence[DocumentBlock]) -> list[dict[str, object]]`.

- [ ] **Step 1: Add packaging metadata**

Create `pyproject.toml` with `requires-python = ">=3.11"`, runtime `requests>=2.32,<3`, and a `dev` extra containing `pytest>=8,<9`, `python-docx>=1.2,<2`, and `Pillow>=11,<13`. Configure pytest with `pythonpath = ["src"]`, `testpaths = ["tests"]`, and marker `integration: requires a running MinerU service`.

Create `.gitignore`:

```gitignore
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.coverage
outputs/
artifacts/
```

Create the isolated environment and install the editable project before the first test run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

All later `python3` commands in this plan mean the interpreter from the activated `.venv`; when executing commands in independent shells, call `.venv/bin/python` explicitly.

- [ ] **Step 2: Write failing model tests**

Create `tests/test_models.py` with this representative test and a companion image test that verifies all nullable top-level keys remain present:

```python
from bid_check.models import BlockType, DocumentBlock, TableContent


def test_document_block_serializes_nested_dataclasses_and_enum_values():
    block = DocumentBlock(
        id="doc_abcdef123456_b000000",
        block_type=BlockType.TABLE,
        text="人员配置表",
        title_level=None,
        section_path=["第三章 技术要求"],
        page_idx=None,
        anchor=None,
        source_object_index=None,
        source_type="table",
        table=TableContent(markdown="|角色|要求|", caption=["人员配置表"]),
        image=None,
        prev_id=None,
        next_id=None,
        metadata={"source_format": "docx"},
    )
    result = block.to_dict()
    assert result["block_type"] == "table"
    assert result["table"]["markdown"] == "|角色|要求|"
    assert result["table"]["cells"] is None
    assert result["image"] is None
```

- [ ] **Step 3: Verify RED**

Run `python3 -m pytest tests/test_models.py -q`.

Expected: fail because `bid_check.models` does not exist.

- [ ] **Step 4: Implement minimal schema models**

Use `@dataclass(slots=True)` and explicit optional fields. `TableContent` fields are `html`, `markdown`, `cells`, `caption`, `footnote`, `image_path`. `ImageContent` fields are `path`, `caption`, `footnote`, `alt_text`. `DocumentBlock` contains exactly the approved top-level schema. Use `dataclasses.asdict()` plus recursive enum-to-value conversion. Do not put parsing or validation in this module.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python3 -m pytest tests/test_models.py -q
python3 -c "from bid_check.models import DocumentBlock; print(DocumentBlock.__name__)"
```

Expected: tests pass and the import prints `DocumentBlock`.

- [ ] **Step 6: Commit Task 1**

```bash
git add .gitignore pyproject.toml src/bid_check tests/test_models.py
git commit -m "feat: define stable document block models"
```

---

### Task 2: MinerU HTTP client and raw-output preservation

**Files:**
- Create: `src/bid_check/mineru_client.py`
- Create: `tests/test_mineru_client.py`

**Interfaces:**
- Produces: `ParseArtifacts(run_dir: Path, raw_dir: Path, manifest_path: Path, input_sha256: str, extracted_files: tuple[Path, ...])`.
- Produces: `MinerUClient(base_url: str, timeout_seconds: float)`.
- Produces: `MinerUClient.health() -> dict[str, object]`.
- Produces: `MinerUClient.parse_docx(docx_path: Path, output_root: Path) -> ParseArtifacts`.
- Produces: `MinerUClientError`.

- [ ] **Step 1: Write failing safe-extraction tests**

Create an in-memory ZIP containing `../escaped.json`, call `_safe_extract_zip(zip_path, raw_dir)`, and assert `MinerUClientError` contains `unsafe ZIP member`. Add a valid ZIP test asserting nested JSON and image files are extracted below `raw_dir` and returned in archive order.

- [ ] **Step 2: Verify RED for extraction**

Run `python3 -m pytest tests/test_mineru_client.py -k safe_extract -q`.

Expected: fail because the client module does not exist.

- [ ] **Step 3: Implement safe extraction and hashing**

Implement `_sha256_file(path: Path) -> str` and `_safe_extract_zip(zip_path: Path, destination: Path) -> tuple[Path, ...]`. Reject absolute members and resolved targets outside `destination`; copy each member using `ZipFile.open()` and `shutil.copyfileobj()` after validating every path. Never use `extractall()`.

- [ ] **Step 4: Verify GREEN for extraction**

Run `python3 -m pytest tests/test_mineru_client.py -k safe_extract -q`.

- [ ] **Step 5: Write a failing client contract test**

Run `ThreadingHTTPServer` on an ephemeral localhost port. Return `{"status":"healthy","version":"3.4.4","protocol_version":2}` from `/health`; capture multipart bytes at `/file_parse`; return a ZIP with three JSON files and `images/sample.png`. Assert request fields `response_format_zip`, `return_middle_json`, `return_content_list`, `return_images`, and `return_md` are `true`; raw ZIP and extracted files exist; manifest records checksum/version/protocol/request parameters/Content-Type/file list; and repeated calls use distinct directories.

- [ ] **Step 6: Verify RED for HTTP behavior**

Run `python3 -m pytest tests/test_mineru_client.py -k parse_docx -q`.

Expected: fail because `MinerUClient.parse_docx` is missing.

- [ ] **Step 7: Implement the client**

Validate an existing regular `.docx` path. Upload with a `requests.Session` and the approved MIME type/form fields. Use run ID `<UTC YYYYMMDDTHHMMSSffffffZ>-<sha256[:12]>`. Stream every HTTP body first to `raw/response.zip`, then validate status, ZIP signature/integrity, and extract. Write manifest atomically through a sibling temporary file and `Path.replace()`.

Wrap request, HTTP, invalid ZIP, and filesystem failures in `MinerUClientError` with endpoint and reason. Never include secrets or full binary response bodies in errors.

- [ ] **Step 8: Verify all client tests**

Run `python3 -m pytest tests/test_mineru_client.py -q`.

Expected: all pass without warnings.

- [ ] **Step 9: Commit Task 2**

```bash
git add src/bid_check/mineru_client.py tests/test_mineru_client.py
git commit -m "feat: preserve MinerU DOCX raw outputs"
```

---

### Task 3: Generate a comprehensive DOCX and capture the actual MinerU contract

**Files:**
- Create: `scripts/generate_test_docx.py`
- Create: `tests/conftest.py`
- Create: `tests/fixtures/generated/mineru_docx_features.docx`
- Create: `tests/integration/test_mineru_docx.py`
- Create: `tests/fixtures/mineru_3_4_4_docx/content_list.json`
- Create: `tests/fixtures/mineru_3_4_4_docx/content_list_v2.json`
- Create: `tests/fixtures/mineru_3_4_4_docx/middle_excerpt.json`
- Create: `docs/mineru-docx-output-analysis.md`

**Interfaces:**
- Consumes: `MinerUClient.parse_docx()`.
- Produces: `generate_fixture(output_path: Path) -> Path`.
- Produces: faithful MinerU 3.4.4 Office contract fixtures for Task 4.

- [ ] **Step 1: Write a failing generator test**

Call `generate_fixture(tmp_path / "features.docx")`, reopen with `python-docx`, and assert exact marker text for heading levels 1–3, same-level replacement, upper-level reset, normal paragraphs, bullet and numbered list styles, a table with at least three rows, at least one inline shape, an OMML math element, and blank paragraphs. Add `scripts/` to test import paths only in `tests/conftest.py`.

- [ ] **Step 2: Verify RED**

Run `python3 -m pytest tests/integration/test_mineru_docx.py -k generate_fixture -q`.

Expected: fail because the generator does not exist.

- [ ] **Step 3: Implement the fixture generator**

Use Heading 1–3 styles, `List Bullet`, `List Number`, a 3×2 table, a Pillow-generated labeled PNG with caption, and a minimal valid OMML `m:oMathPara/m:oMath` equation containing `E = mc²`. Include both empty and whitespace-only paragraphs. Add CLI option `--output` and guarded `main()`.

- [ ] **Step 4: Verify and create the committed DOCX**

```bash
python3 -m pytest tests/integration/test_mineru_docx.py -k generate_fixture -q
python3 scripts/generate_test_docx.py --output tests/fixtures/generated/mineru_docx_features.docx
```

- [ ] **Step 5: Write the live integration test**

Mark `test_live_mineru_returns_docx_office_artifacts` with `@pytest.mark.integration`. Call the service at port 7100 and first assert only evidence-neutral requirements: the ZIP is retained, one each of `*_middle.json`, `*_content_list.json`, and `*_content_list_v2.json` loads as JSON, and an extracted image exists. Do not assert PDF shapes.

- [ ] **Step 6: Run MinerU and retain the live output**

Run:

```bash
python3 -m pytest tests/integration/test_mineru_docx.py -m integration -k live_mineru -vv -s
```

If an expected artifact is absent, retain and document the exact response; do not fabricate it.

- [ ] **Step 7: Inspect the actual JSON shapes**

Use short read-only scripts to print top-level types/sizes; middle root/page/para/line/span keys and type inventory; v2 group/item/content keys and types; legacy item keys/types; and exact locations/types for title level, anchor, page index, object index, table data, image references, lists, and formulas.

Create minimal contract fixtures preserving actual nesting, keys, value types, and path format. Long synthetic text may be shortened only when structure remains identical. `middle_excerpt.json` must keep root metadata plus representative para blocks.

- [ ] **Step 8: Write the output analysis**

Document version, request flags, fixture SHA-256, three JSON structures, representative snippets, observed type inventory, reliable/non-reliable relationships, DOCX/PDF differences, and a field matrix categorized as `stable in this Office output`, `conditional`, `derived by this project`, or `not observed/not stable`. Explicitly record absent formula/page/anchor/object-index fields.

- [ ] **Step 9: Strengthen and rerun the live contract test**

Add only assertions supported by Step 7, including the exact observed Office backend/version and container types. Run `python3 -m pytest tests/integration/test_mineru_docx.py -m integration -vv -s`.

- [ ] **Step 10: Commit Task 3**

```bash
git add scripts/generate_test_docx.py tests/conftest.py tests/fixtures tests/integration docs/mineru-docx-output-analysis.md
git commit -m "test: capture MinerU 3.4.4 DOCX contract"
```

---

### Task 4: Normalize observed DOCX blocks and build section paths

**Files:**
- Create: `src/bid_check/normalizer.py`
- Create: `tests/test_normalizer.py`

**Interfaces:**
- Consumes: Task 1 models and Task 3 fixtures.
- Produces: `NormalizationResult(blocks: list[DocumentBlock], source_json: str, warnings: list[str])`.
- Produces: `normalize_docx_output(raw_dir: Path, document_sha256: str) -> NormalizationResult`.
- Produces: `NormalizationError`.
- Internal signatures: `_discover_source(raw_dir: Path) -> tuple[Literal["content_list_v2", "content_list"], Path]`; `_iter_v2_items(payload: object) -> Iterator[SourceItem]`; `_map_v2_item(item: SourceItem) -> CandidateBlock`; `_apply_section_path(candidate: CandidateBlock, headings: dict[int, str]) -> list[str]`.

- [ ] **Step 1: Write failing v2 title/path/order tests**

Build a minimal input in the exact observed v2 shape containing headings `第三章 技术要求` → `3.1 总体要求` → `3.1.2 人员要求`, paragraph `项目经理应具有……`, same-level heading `3.1.3 其他人员要求`, then level-1 heading `第四章 商务要求`. Assert headings include themselves, paragraphs inherit the stack, same-level replacement and upper-level cleanup work, and output text order equals input order.

- [ ] **Step 2: Verify RED**

Run `python3 -m pytest tests/test_normalizer.py -k section_path -q`.

- [ ] **Step 3: Implement observed v2 traversal and heading stack**

Use private `SourceItem` and `CandidateBlock` dataclasses and implement the helpers with the exact signatures in this task's Interfaces section. Implement only Task 3's observed v2 structure. Record group/item/flat positions. Remove heading levels `>= L` before inserting level `L`; missing/invalid levels leave the stack unchanged and add a warning.

- [ ] **Step 4: Verify GREEN for section paths**

Run `python3 -m pytest tests/test_normalizer.py -k section_path -q`.

- [ ] **Step 5: Write failing mapping/filter tests**

Using actual fixture field names, assert list order/newline text; faithful table/image content; safe relative image paths; meaningful unknown preservation; empty paragraph/list/unknown removal; exact `source_type`; `source_object_index=None` when absent upstream; and pre-filter `metadata.source_position.flat_index`. For formulas, test only an actually observed formula shape; if none was observed, assert the invented shape stays unknown rather than pretending support.

- [ ] **Step 6: Verify RED for mapping/filtering**

Run `python3 -m pytest tests/test_normalizer.py -k 'table or image or list or empty or unknown or formula' -q`.

- [ ] **Step 7: Implement observed mappings and meaningful-block filtering**

Map only observed source types. Move non-empty unconsumed keys to `metadata.unmapped_fields`. Normalize line endings and outer whitespace only. Reject traversal image references; retain a safe missing reference with a warning. Never merge or reorder blocks.

- [ ] **Step 8: Write failing deterministic-ID and neighbor tests**

For hash prefix `abcdef123456`, assert post-filter IDs `doc_abcdef123456_b000000`, `doc_abcdef123456_b000001`, and `doc_abcdef123456_b000002`, with exact first/middle/last `prev_id` and `next_id`.

- [ ] **Step 9: Implement IDs and neighbor linking after filtering**

Build candidates, filter, assign all IDs in final order, then perform a second pass for reciprocal links.

- [ ] **Step 10: Write failing legacy/error tests**

Use Task 3's legacy fixture. Assert fallback only when v2 is absent or structurally unsupported; invalid JSON names its file; and absence of both sources lists discovered candidates in `NormalizationError`.

- [ ] **Step 11: Implement the legacy adapter**

Use only actual legacy Office keys. Share final candidate-to-block logic but not source field extractors when v2 and legacy containers differ.

- [ ] **Step 12: Verify all normalization behavior**

Run `python3 -m pytest tests/test_normalizer.py -q`.

Expected: all path, order, links, structures, filtering, v2, and legacy cases pass.

- [ ] **Step 13: Commit Task 4**

```bash
git add src/bid_check/normalizer.py tests/test_normalizer.py
git commit -m "feat: normalize MinerU DOCX blocks"
```

---

### Task 5: Reporting and JSON output

**Files:**
- Create: `src/bid_check/reporting.py`
- Create: `tests/test_reporting.py`

**Interfaces:**
- Consumes: `Sequence[DocumentBlock]` only.
- Produces: `build_report(blocks: Sequence[DocumentBlock], top_longest: int = 5) -> dict[str, object]`.
- Produces: `format_report(report: Mapping[str, object]) -> str`.
- Produces: `write_json(path: Path, value: object) -> None`.

- [ ] **Step 1: Write a failing exact-statistics test**

Create blocks with text lengths `0`, `3`, `9`, titles at levels 1 and 2, repeated paths, one unknown source type, and repeated warnings. Assert block-type counts; total `12`; average `4.0`; median `3`; maximum `9`; string title-level keys; first-occurrence unique section paths; length-descending/source-order longest blocks; deterministic warning counts.

- [ ] **Step 2: Verify RED**

Run `python3 -m pytest tests/test_reporting.py -q`.

- [ ] **Step 3: Implement deterministic reporting**

Use `Counter` and `statistics.median`, return JSON primitives, and truncate previews at 120 Unicode code points with `…` only when needed. `write_json` writes UTF-8 with `ensure_ascii=False`, `indent=2`, trailing newline, and atomic sibling-temp replacement.

- [ ] **Step 4: Verify GREEN**

Run `python3 -m pytest tests/test_reporting.py -q`.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/bid_check/reporting.py tests/test_reporting.py
git commit -m "feat: report document block statistics"
```

---

### Task 6: CLI orchestration and failure behavior

**Files:**
- Create: `scripts/parse_docx.py`
- Create: `tests/test_parse_docx_cli.py`

**Interfaces:**
- Consumes: client, normalizer, models, and reporting public functions.
- Produces: exit `0` plus `document_blocks.json`/`report.json`/updated manifest on success; nonzero exit and actionable stderr on expected failures.

- [ ] **Step 1: Write a failing CLI success test**

Use a localhost server returning a ZIP built from the actual minimized fixtures. Construct the subprocess argv with `server.server_port`, `tmp_path / "fixture.docx"`, and `tmp_path / "outputs"`, followed by `--timeout 30 --top-longest 3`. Assert exit `0`; all outputs exist; blocks JSON is a top-level list; report counts match; stdout labels raw directory, normalized file, block counts, text stats, title levels, section paths, and longest blocks.

- [ ] **Step 2: Verify RED**

Run `python3 -m pytest tests/test_parse_docx_cli.py -k success -q`.

- [ ] **Step 3: Implement thin CLI orchestration**

Implement `main(argv: Sequence[str] | None = None) -> int` and approved defaults. Parse, normalize, write blocks/report, update manifest atomically with selected flavor/warning summary/output paths/completion time, then print paths and report. Guard with `raise SystemExit(main())`.

- [ ] **Step 4: Write failing CLI error tests**

Assert nonzero exit and actionable stderr for missing input, non-DOCX input, unavailable service, non-ZIP response, and ZIP without supported lists. For received responses, assert `raw/response.zip` remains.

- [ ] **Step 5: Implement expected-error handling**

Catch only `MinerUClientError`, `NormalizationError`, `OSError`, and `ValueError` at the CLI boundary. Print class and message without traceback. Never catch `BaseException` or hide programmer errors.

- [ ] **Step 6: Verify CLI tests**

Run `python3 -m pytest tests/test_parse_docx_cli.py -q`.

- [ ] **Step 7: Commit Task 6**

```bash
git add scripts/parse_docx.py tests/test_parse_docx_cli.py
git commit -m "feat: add DOCX parsing command"
```

---

### Task 7: README and fresh end-to-end evidence

**Files:**
- Create: `README.md`
- Modify: `docs/mineru-docx-output-analysis.md`
- Modify: `tests/integration/test_mineru_docx.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: setup/run/test/schema/stability documentation and a freshly verified real output.

- [ ] **Step 1: Write README from verified behavior**

Document the exact tree and roles; venv/install commands; fixture generation; ordinary and integration test commands; port-7100 CLI example; output layout; final schema example; stable/conditional/derived/unreliable field table; and explicit exclusions. Do not claim stability without live evidence and a test.

- [ ] **Step 2: Run syntax and whitespace checks**

```bash
python3 -m compileall -q src scripts tests
git diff --check
```

- [ ] **Step 3: Run ordinary tests**

Run `python3 -m pytest -m "not integration" -q`.

Expected: zero failures and warnings.

- [ ] **Step 4: Run live MinerU integration tests**

Run `python3 -m pytest tests/integration -m integration -vv -s`.

Expected: port 7100 parses the generated DOCX and required raw/normalized structures pass.

- [ ] **Step 5: Run the user-facing command**

```bash
python3 scripts/parse_docx.py tests/fixtures/generated/mineru_docx_features.docx --output-dir outputs --top-longest 5
```

Load all JSON outputs with `python3 -m json.tool`. Confirm the marker paragraph has the three-element `section_path`, table/image blocks remain, empty blocks are absent, and every neighbor link is reciprocal.

- [ ] **Step 6: Reconcile docs with evidence**

Compare output/test logs against all ten acceptance criteria in the spec. Correct any overstatement in README/analysis and record every newly observed MinerU discrepancy. Do not weaken correct tests to accommodate faulty code.

- [ ] **Step 7: Run fresh final verification**

```bash
python3 -m compileall -q src scripts tests
python3 -m pytest -m "not integration" -q
python3 -m pytest tests/integration -m integration -vv -s
git diff --check
git status --short
```

Expected: compile/diff commands exit `0`; both suites have zero failures; status contains only intended Task 7 files.

- [ ] **Step 8: Commit Task 7**

```bash
git add README.md docs/mineru-docx-output-analysis.md tests/integration/test_mineru_docx.py
git commit -m "docs: document MinerU DOCX normalization"
```

- [ ] **Step 9: Verify final repository state**

```bash
git status --short --branch
git log --oneline --decorate -8
```

Expected: clean `main` worktree with design, plan, and seven task commits visible.
