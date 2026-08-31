# Real Compliance Requirement Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed compliance-requirement mock with a source-grounded extraction pipeline while leaving bid parsing and review mocked.

**Architecture:** Parse the tender DOCX through a MinerU-compatible adapter (configured command when available, deterministic DOCX structural fallback for local development), select a small number of compliance-focused windows, call an injectable structured LLM adapter per window, validate and normalize its output, and restore source text from parsed blocks before persistence.

**Tech Stack:** Python standard library XML/ZIP parsing, FastAPI, Pydantic (already transitively available), SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-bid-check-web-workflow-design.md` plus the approved in-chat design for this iteration.

## Global Constraints

- Only the tender-file requirements stage changes; bid parsing and review remain mock services.
- Candidate batches are capped at 8 by default and never exceed 10.
- Scoring, evaluation, and expected-result language is excluded from compliance requirements.
- Every returned requirement must contain source block ids and source text recovered from the parsed document.
- Missing external MinerU/LLM configuration must not silently return the old fixed five-item mock.
- All behavior changes use test-first red/green cycles.

### Task 1: Structured document parsing and candidate batching

**Files:** Create `app/compliance_extraction.py`, modify `app/config.py`, test `tests/test_compliance_extraction.py`.

- [x] Write tests for DOCX XML parsing, compliance filtering, scoring exclusion, and the 8/10 batch cap.
- [x] Run the focused tests and confirm they fail because the new module is absent.
- [x] Implement block parsing, candidate windows, and bounded batching with no model calls.
- [x] Run focused tests and the full existing suite.

### Task 2: Schema-validated LLM extraction and source restoration

**Files:** Modify `app/compliance_extraction.py`, test `tests/test_compliance_extraction.py`.

- [x] Add injectable parser/LLM protocols and a strict Pydantic requirement schema.
- [x] Test malformed model output, unsupported categories, source restoration, deterministic ids, and de-duplication.
- [x] Implement validation, source lookup, normalization, and stable sorting.
- [x] Run focused tests.

### Task 3: Wire the real tender extractor into the workflow

**Files:** Modify `app/api.py`, `app/config.py`, `app/mock_services.py`, `tests/test_api.py`, `tests/test_end_to_end.py`, and `README.md`.

- [x] Add tests proving an uploaded valid DOCX produces requirements derived from its text while bid parsing/review stay mock.
- [x] Run those tests red.
- [x] Wire the new extractor through `build_default_workflow`; retain the old mock helper only for compatibility tests and invalid legacy fixtures.
- [x] Document MinerU/LLM environment configuration and local deterministic fallback.
- [x] Run all tests and inspect the final diff.
