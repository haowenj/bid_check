# Compliance Requirement Enums and Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Constrain extracted compliance requirements to fixed structural enums and remove rules that cannot be checked from the submitted bid files themselves.

**Architecture:** Keep the existing candidate → batched LLM → normalization pipeline. Tighten the prompt and Pydantic boundary, canonicalize only explicitly recognized legacy aliases, derive evidence types from check types, then apply deterministic project-aware scope filters before source reconstruction and final persistence.

**Tech Stack:** Python 3.14, Pydantic, pytest, existing JSON artifact recorder, OpenAI-compatible Chat Completions.

**Spec:** Current user request (fixed enums, bid-file-checkable boundary, source traceability, and real-tender acceptance criteria).

## Global Constraints

- Do not add a second LLM summarization pass.
- Do not change投标文件解析 or review execution.
- Preserve raw LLM outputs and source block IDs; reconstruct source text only from parsed blocks.
- Reject unknown enum values rather than emitting them in normalized output.
- Keep API credentials out of logs and artifacts.

### Task 1: Add failing tests for enums and deterministic boundary filters

**Files:**
- Modify: `tests/test_compliance_extraction.py`
- Test: `tests/test_compliance_extraction.py`

- [x] Add tests asserting canonical aliases become fixed enum values and evidence is derived from `check_type`.
- [x] Add tests asserting unknown enum values raise `ComplianceExtractionError`.
- [x] Add tests asserting external-system, future-contract, and project-conflict rules are removed while ordinary template/attachment rules remain.
- [x] Run the focused tests and confirm they fail against the current implementation.

### Task 2: Implement strict enum schema and canonicalization

**Files:**
- Modify: `app/compliance_extraction.py`

- [x] Define fixed literals for applicability type, target scope, check type, and evidence type.
- [x] Map only recognized legacy English/Chinese labels to canonical values; reject unknown labels.
- [x] Always derive evidence type from canonical check type.
- [x] Preserve the existing legacy object/string shape compatibility without allowing free enum values through.

### Task 3: Tighten model prompt and deterministic executable-boundary filtering

**Files:**
- Modify: `app/compliance_extraction.py`

- [x] Enumerate every allowed value in the LLM prompt and require source IDs to be copied from input blocks.
- [x] Add deterministic filters for external system state, future contract/履约 requirements without explicit current-bid submission language, and project-disallowed joint/alternative bid rules.
- [x] Keep source reconstruction from block IDs and avoid model-provided source text.

### Task 4: Persist filter diagnostics and update tests

**Files:**
- Modify: `app/compliance_extraction.py`
- Modify: `tests/test_compliance_extraction.py`
- Modify: `README.md`

- [x] Persist filtered requirement counts/reasons alongside final normalized requirements and summary stats.
- [x] Ensure cache-hit artifacts retain canonical normalized shape.
- [x] Run the full test suite and inspect enum distributions in fixtures.

### Task 5: Run the current real tender acceptance check

**Files:**
- Runtime artifact: `data/tasks/<new-task-id>/compliance_extraction/`

- [x] Run extraction only on the current real tender DOCX with the configured model and no bid review execution.
- [x] Inspect final count, filter reasons, enum distributions, illegal enum scan, and core template/attachment retention.
- [x] Report timing/status and any remaining boundary gaps without changing投标文件解析 or审查流程.
