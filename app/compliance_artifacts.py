from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_API_KEY_VALUE_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_EVENT_LOCK = threading.RLock()


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for item_key, item_value in value.items():
            normalized_key = str(item_key)
            if _SENSITIVE_KEY_RE.fullmatch(normalized_key.replace("-", "_")):
                continue
            sanitized[normalized_key] = _sanitize(item_value)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _API_KEY_VALUE_RE.sub(
            "<redacted-api-key>",
            _BEARER_RE.sub("Bearer <redacted>", value),
        )
    return value


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class ComplianceExtractionRecorder:
    """Persist task-local extraction artifacts and structured execution events."""

    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)
        self.artifact_dir = self.task_dir / "compliance_extraction"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.execution_log_path = self.artifact_dir / "execution.jsonl"
        self._lock = threading.RLock()
        self._call_sequence = 0
        self._calls: dict[str, dict[str, Any]] = {}
        self.started_at = _timestamp()

    @classmethod
    def from_tender_path(cls, tender_path: Path) -> ComplianceExtractionRecorder:
        return cls(Path(tender_path).parent)

    def _resolve_artifact_path(self, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute():
            raise ValueError("artifact name must be relative")
        root = self.artifact_dir.resolve()
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise ValueError("artifact name escapes task artifact directory")
        return target

    def write_json(self, name: str, payload: Any) -> Path:
        target = self._resolve_artifact_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            _sanitize(payload),
            ensure_ascii=False,
            indent=2,
        )
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with self._lock:
            temporary.write_text(encoded, encoding="utf-8")
            os.replace(temporary, target)
        return target

    def event(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": _timestamp(),
            "event": event,
            **_sanitize(fields),
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with _EVENT_LOCK:
            self.execution_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.execution_log_path.open("a", encoding="utf-8") as stream:
                stream.write(line)

    def start_llm_call(
        self,
        *,
        batch_index: int,
        batch_count: int,
        attempt: int,
        model: str,
        batch: Any,
    ) -> str:
        with self._lock:
            self._call_sequence += 1
            call_id = f"call_{self._call_sequence:03d}"
            started_at = _timestamp()
            state = {
                "call_id": call_id,
                "batch_index": batch_index,
                "batch_count": batch_count,
                "attempt": attempt,
                "model": model,
                "started_at": started_at,
                "input_path": f"llm/{call_id}_input.json",
                "output_path": f"llm/{call_id}_output.json",
            }
            self._calls[call_id] = state
            self.write_json(
                state["input_path"],
                {
                    **state,
                    "retry": attempt > 1,
                    "batch": batch,
                },
            )
        self.event(
            "llm.call.start",
            call_id=call_id,
            batch_index=batch_index,
            batch_count=batch_count,
            attempt=attempt,
            retry=attempt > 1,
            model=model,
        )
        return call_id

    def attach_llm_input(self, call_id: str, request_payload: Any) -> None:
        with self._lock:
            state = self._calls[call_id]
            input_payload = {
                **state,
                "retry": state["attempt"] > 1,
                "request_payload": request_payload,
            }
            self.write_json(state["input_path"], input_payload)

    def attach_llm_response(
        self,
        call_id: str,
        *,
        raw_response: Any,
        finish_reason: str | None = None,
        usage: Any = None,
    ) -> None:
        with self._lock:
            state = self._calls[call_id]
            state["raw_response"] = raw_response
            state["finish_reason"] = finish_reason
            state["usage"] = usage

    def complete_llm_call(
        self,
        call_id: str,
        *,
        raw_response: Any = None,
        parsed_objects: Any = None,
        finish_reason: str | None = None,
        usage: Any = None,
        schema_valid: bool | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        with self._lock:
            state = self._calls[call_id]
            if raw_response is None:
                raw_response = state.get("raw_response", parsed_objects)
            if finish_reason is None:
                finish_reason = state.get("finish_reason")
            if usage is None:
                usage = state.get("usage")
            ended_at = _timestamp()
            output_payload = {
                **state,
                "status": "success",
                "ended_at": ended_at,
                "elapsed_ms": elapsed_ms,
                "finish_reason": finish_reason,
                "usage": usage,
                "schema_valid": schema_valid,
                "raw_response": raw_response,
                "parsed_objects": parsed_objects,
            }
            self.write_json(state["output_path"], output_payload)
        self.event(
            "llm.call.end",
            call_id=call_id,
            batch_index=state["batch_index"],
            attempt=state["attempt"],
            model=state["model"],
            status="success",
            elapsed_ms=elapsed_ms,
            finish_reason=finish_reason,
            usage=usage,
            schema_valid=schema_valid,
            object_counts=(
                {
                    key: len(value)
                    for key, value in parsed_objects.items()
                    if isinstance(value, list)
                }
                if isinstance(parsed_objects, dict)
                else None
            ),
        )

    def fail_llm_call(
        self,
        call_id: str,
        *,
        error_type: str,
        error_message: str,
        elapsed_ms: int | None = None,
        raw_response: Any = None,
    ) -> None:
        with self._lock:
            state = self._calls[call_id]
            if raw_response is None:
                raw_response = state.get("raw_response")
            ended_at = _timestamp()
            output_payload = {
                **state,
                "status": "failed",
                "ended_at": ended_at,
                "elapsed_ms": elapsed_ms,
                "finish_reason": None,
                "usage": None,
                "schema_valid": False,
                "raw_response": raw_response,
                "parsed_objects": None,
                "error_type": error_type,
                "error_message": error_message,
            }
            self.write_json(state["output_path"], output_payload)
        self.event(
            "llm.call.error",
            call_id=call_id,
            batch_index=state["batch_index"],
            attempt=state["attempt"],
            model=state["model"],
            status="failed",
            elapsed_ms=elapsed_ms,
            error_type=error_type,
            error_message=error_message,
        )

    def mark_llm_schema(
        self,
        call_id: str,
        *,
        valid: bool,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            state = self._calls[call_id]
            output_path = self._resolve_artifact_path(state["output_path"])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            payload["schema_valid"] = valid
            if error_message:
                payload["schema_error"] = error_message
            self.write_json(state["output_path"], payload)
        self.event(
            "llm.schema",
            call_id=call_id,
            valid=valid,
            error_message=error_message,
        )

    def write_summary(self, summary: Mapping[str, Any]) -> Path:
        return self.write_json("summary.json", dict(summary))

    def write_workflow_summary(self, summary: Mapping[str, Any]) -> Path:
        return self.write_json("workflow_summary.json", dict(summary))

    def finalize(
        self,
        *,
        status: str,
        stats: Mapping[str, Any],
        failed_stage: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        elapsed_ms: int | None = None,
    ) -> Path:
        summary = {
            "status": status,
            "started_at": self.started_at,
            "ended_at": _timestamp(),
            "elapsed_ms": elapsed_ms,
            "failed_stage": failed_stage,
            "error_type": error_type,
            "error_message": error_message,
            "stats": dict(stats),
        }
        path = self.write_summary(summary)
        self.event(
            "compliance.extract.finalize",
            status=status,
            failed_stage=failed_stage,
            error_type=error_type,
            elapsed_ms=elapsed_ms,
            stats=dict(stats),
        )
        return path
