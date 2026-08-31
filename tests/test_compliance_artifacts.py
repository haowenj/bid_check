from __future__ import annotations

import json

import pytest

from app.compliance_artifacts import ComplianceExtractionRecorder


def test_recorder_writes_task_local_json_and_append_only_events(tmp_path):
    recorder = ComplianceExtractionRecorder(tmp_path / "task-001")

    artifact_path = recorder.write_json(
        "01_parsed_blocks.json",
        {"blocks": [{"block_id": "b0001", "text": "须提供证明材料"}]},
    )
    recorder.event("document.parse.end", blocks=1, elapsed_ms=12)
    recorder.event("candidate.filter.end", selected_blocks=1, windows=1)

    assert (
        artifact_path
        == tmp_path / "task-001" / "compliance_extraction" / "01_parsed_blocks.json"
    )
    assert (
        json.loads(artifact_path.read_text(encoding="utf-8"))["blocks"][0]["block_id"]
        == "b0001"
    )
    events = [
        json.loads(line)
        for line in (recorder.artifact_dir / "execution.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event"] for event in events] == [
        "document.parse.end",
        "candidate.filter.end",
    ]
    assert events[0]["elapsed_ms"] == 12


def test_recorder_persists_llm_input_output_and_metadata(tmp_path):
    recorder = ComplianceExtractionRecorder(tmp_path / "task-001")
    call_id = recorder.start_llm_call(
        batch_index=1,
        batch_count=2,
        attempt=1,
        model="test-model",
        batch=[{"block_ids": ["b0001"], "text": "须提供证明材料"}],
    )
    recorder.attach_llm_input(
        call_id,
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "须提供证明材料"}],
            "headers": {"Authorization": "Bearer should-not-be-saved"},
        },
    )
    recorder.complete_llm_call(
        call_id,
        raw_response={"choices": [{"message": {"content": "raw"}}]},
        parsed_requirements=[{"name": "主体资格"}],
        finish_reason="stop",
        usage={"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29},
        schema_valid=True,
        elapsed_ms=345,
    )

    input_path = recorder.artifact_dir / "llm" / f"{call_id}_input.json"
    output_path = recorder.artifact_dir / "llm" / f"{call_id}_output.json"
    input_payload = json.loads(input_path.read_text(encoding="utf-8"))
    output_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert input_payload["request_payload"]["model"] == "test-model"
    assert "Authorization" not in json.dumps(input_payload)
    assert output_payload["finish_reason"] == "stop"
    assert output_payload["usage"]["total_tokens"] == 29
    assert output_payload["parsed_requirements"] == [{"name": "主体资格"}]
    assert output_payload["schema_valid"] is True


def test_recorder_preserves_prior_artifacts_when_call_fails(tmp_path):
    recorder = ComplianceExtractionRecorder(tmp_path / "task-001")
    recorder.write_json("01_parsed_blocks.json", {"blocks": []})
    call_id = recorder.start_llm_call(
        batch_index=1,
        batch_count=1,
        attempt=2,
        model="test-model",
        batch=[],
    )
    recorder.fail_llm_call(
        call_id,
        error_type="TimeoutError",
        error_message="请求超时",
        elapsed_ms=90000,
    )
    recorder.finalize(
        status="failed",
        stats={"llm_total_calls": 1, "final_requirements": 0},
        failed_stage="llm",
    )

    assert (recorder.artifact_dir / "01_parsed_blocks.json").is_file()
    summary = json.loads(
        (recorder.artifact_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "failed"
    assert summary["failed_stage"] == "llm"
    events = (recorder.artifact_dir / "execution.jsonl").read_text(encoding="utf-8")
    assert "llm.call.error" in events
    assert "request_payload" not in events


def test_recorder_rejects_artifact_path_escape(tmp_path):
    recorder = ComplianceExtractionRecorder(tmp_path / "task-001")

    with pytest.raises(ValueError):
        recorder.write_json("../outside.json", {})
