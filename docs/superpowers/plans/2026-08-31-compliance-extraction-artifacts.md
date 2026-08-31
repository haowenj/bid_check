# Compliance Extraction Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist source-grounded compliance-extraction intermediates and a complete per-task execution record without changing extraction decisions, batching, retries, or normalization semantics.

**Architecture:** Derive the artifact directory from the existing tender task directory and place all extraction artifacts under `compliance_extraction/`. A single recorder owns atomic JSON writes and append-only structured execution events. The existing extractor reports stage data to the recorder, while the OpenAI-compatible adapter optionally reports the redacted request payload and raw response metadata through thread-local call context.

**Tech Stack:** Python standard library (`json`, `pathlib`, `datetime`, `threading`, `logging`), existing FastAPI workflow, existing Pydantic models, pytest.

**Spec:** User request in the current conversation.

## Global Constraints

- Do not change candidate-selection, batch construction, retry, LLM prompt semantics, schema coercion, normalization, or returned requirement shapes.
- Reuse each uploaded task's existing `data/tasks/{task_id}` directory; add only a task-local `compliance_extraction/` artifact directory.
- Persist successful stage outputs immediately and preserve them if a later stage fails.
- Persist no API key, Authorization header, or other authentication material.
- Keep LLM request/response capture optional and backward-compatible for injected test adapters.
- Run a real extraction once after implementation and report artifact paths and statistics without exposing secrets.

### Task 1: Central task artifact recorder

**Files:** Create `app/compliance_artifacts.py`; test `tests/test_compliance_artifacts.py`.

- [x] Write failing tests for task-local paths, atomic JSON artifacts, append-only execution events, LLM call files, and sensitive-field redaction.
- [x] Run the focused tests and confirm they fail because the recorder does not exist.
- [x] Implement `ComplianceExtractionRecorder` with `event`, `write_json`, `start_llm_call`, `attach_llm_input`, `complete_llm_call`, `fail_llm_call`, `write_summary`, and `finalize` methods.
- [x] Run focused recorder tests and verify successful artifacts remain after simulated failure.

### Task 2: Instrument the extraction pipeline

**Files:** Modify `app/compliance_extraction.py`; test `tests/test_compliance_extraction.py`.

- [x] Write failing tests proving parsed blocks, candidates, batches, per-call input/output, aggregate raw requirements, normalized requirements, summary counts, and failure events are persisted under the tender task directory.
- [x] Run the focused tests and verify the new persistence assertions fail.
- [x] Add an optional recorder to `extract_compliance_requirements_real`, derive one by default from the tender path, and report each existing stage without changing its outputs or control flow.
- [x] Record retries, schema status, result counts, cache status, durations, and final summary in `finally` so earlier artifacts survive errors.
- [x] Run focused extraction tests and the full test suite.

### Task 3: Capture actual OpenAI-compatible calls

**Files:** Modify `app/compliance_extraction.py`; test `tests/test_compliance_extraction.py`.

- [x] Write failing tests for redacted request payload, raw response, finish reason, token usage, and failed-call metadata.
- [x] Run the focused tests and verify they fail before the adapter hook exists.
- [x] Add thread-local call context to `OpenAICompatibleLLM`; report payload without headers, raw response, finish reason, and usage to the recorder while preserving the existing return value and exceptions.
- [x] Run focused adapter tests and the full suite.

### Task 4: Workflow integration and real-run verification

**Files:** Modify `app/api.py`, `README.md`; test `tests/test_end_to_end.py` or a focused integration test.

- [x] Write a failing integration assertion that a workflow task exposes the artifact directory and persisted summary after completion.
- [x] Run the focused test red.
- [x] Ensure the default workflow passes the task-local recorder path naturally through the existing tender storage path and document the artifact layout.
- [x] Run all tests, execute one real configured extraction, inspect artifacts and execution events, then commit the implementation.
