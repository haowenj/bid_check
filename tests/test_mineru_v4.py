from __future__ import annotations

import hashlib
import io
import json
import zipfile

import httpx
import pytest

from app.bid_document import BidDocumentCleaningError, MinerUBidDocumentParser
from app.compliance_extraction import ComplianceExtractionError, MinerUDocumentParser


def _archive(*, ocr=False, middle=True):
    payload = {
        "pages": [{"page_idx": 2, "blocks": [
            {"type": "paragraph_title", "level": 1, "content": "投标文件格式", "bbox": [1, 2, 3, 4]},
            {"type": "text", "content": "投标人名称：____"},
            {"type": "table", "content": "| 字段 | 值 |\n| --- | --- |\n| 项目 | 测试 |", "captions": [{"content": "项目表"}]},
            {"type": "image", "image_source": "images/proof.png", "captions": [{"content": "证明材料"}]},
        ]}],
        "metadata": {"producer": {"version": "4.0.3"}},
    }
    if ocr:
        payload["pages"] = [{"page_idx": 0, "blocks": [{"type": "text", "content": "合同OCR正文"}]}]
    raw = json.dumps(payload, ensure_ascii=False).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("results/structured_content.json", raw)
        if not ocr:
            archive.writestr("results/images/proof.png", b"proof image")
            if middle:
                middle_payload = {"pages": [{"page_idx": 2, "blocks": [
                    {"type": "paragraph_title", "level": 1, "content": [{"type": "text", "content": "投标文件格式"}]},
                    {"type": "text", "content": [{"type": "text", "content": "投标人名称：____", "styles": ["underline"]}]},
                    {"type": "table", "content": [{"type": "table_body", "content": "<table><tr><td>项目</td><td>测试</td></tr></table>"}]},
                    {"type": "image", "image_source": "images/proof.png"},
                ]}]}
                archive.writestr("results/middle_json.json", json.dumps(middle_payload, ensure_ascii=False))
    return buffer.getvalue(), raw


def _v4_handler(*, prefix="", model="mineru4", reused=False, job_status="completed", middle=True):
    requests = []
    uploads = {}
    polls = {}

    def handler(request):
        requests.append(request)
        assert request.url.path.startswith(f"{prefix}/v1/")
        route = request.url.path.removeprefix(prefix)
        assert request.headers.get("Authorization") == "Bearer test-key"
        assert request.headers.get("X-GPUStack-Model") == model
        if route == "/v1/uploads":
            data = json.loads(request.content)
            assert data["sha256sum"] and data["bytes"] > 0
            assert data["purpose"] == "parse"
            number = len(uploads) + 1
            uploads[str(number)] = data
            response = {"id": f"upload-{number}", "status": "completed" if reused else "pending"}
            if reused:
                response["file"] = {"id": f"input-{number}"}
            else:
                response.update(upload_url="http://wrong-worker.example/incorrect-upload", upload_method="PUT", upload_headers={"Content-Type": "application/octet-stream"})
            return httpx.Response(200, json=response)
        if request.method == "PUT":
            number = route.split("/")[3].split("-")[-1]
            assert hashlib.sha256(request.content).hexdigest() == uploads[number]["sha256sum"]
            return httpx.Response(200, json={})
        if route.endswith("/complete"):
            number = route.split("/")[3].split("-")[-1]
            return httpx.Response(200, json={"status": "completed", "file": {"id": f"input-{number}"}})
        if route == "/v1/parse/jobs":
            data = json.loads(request.content)
            assert data["output_formats"] == ["zip"]
            assert data["tier"] == "standard"
            assert "backend" not in data and "server_url" not in data
            number = data["files"][0]["source"]["file_id"].split("-")[-1]
            assert data["ocr_mode"] == ("ocr" if uploads[number]["mime_type"].startswith("image/") else "auto")
            return httpx.Response(202, json={"job_id": f"job-{number}", "status": "queued"})
        if route.startswith("/v1/parse/jobs/"):
            number = route.split("-")[-1]
            count = polls.get(number, 0)
            polls[number] = count + 1
            status = "running" if count == 0 else job_status
            return httpx.Response(200, json={"status": status, "files": [{"status": "completed", "output_files": {"zip": {"file_id": f"output-{number}"}}}]})
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        if route.endswith("/content"):
            number = route.split("/")[3].split("-")[-1]
            return httpx.Response(200, content=_archive(ocr=uploads[number]["mime_type"].startswith("image/"), middle=middle)[0])
        raise AssertionError(f"Unexpected request: {request.method} {route}")

    return handler, requests


def _parser(kind, client, prefix=""):
    cls = MinerUDocumentParser if kind == "tender" else MinerUBidDocumentParser
    return cls(f"https://mineru.example{prefix}", mineru_api_key="test-key", http_client=client, poll_interval_seconds=0)


@pytest.mark.parametrize("kind", ["tender", "bid"])
@pytest.mark.parametrize("prefix", ["", "/model/proxy", "/custom/base"])
def test_v4_direct_call_preserves_document_and_ocr(tmp_path, kind, prefix):
    path = tmp_path / "document.docx"
    path.write_bytes(b"document bytes")
    handler, requests = _v4_handler(prefix=prefix)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parser = _parser(kind, client, prefix)
        if kind == "tender":
            blocks = parser.parse(path)
            assert [b.type for b in blocks] == ["heading", "paragraph", "table", "image"]
            assert blocks[0].heading_level == 1
            assert blocks[0].metadata["page_idx"] == 2
            assert blocks[0].metadata["bbox"] == [1, 2, 3, 4]
            assert blocks[1].metadata["mineru_nested_content"][0]["style"] == ["underline"]
            assert blocks[2].text.startswith("<table>")
            assert blocks[3].metadata["img_path"] == "images/proof.png"
            diagnostics = parser.parse_diagnostics
        else:
            output_dir = tmp_path / "cleaning"
            result = parser.parse(path, output_dir=output_dir)
            diagnostics = result["diagnostics"]
            document = json.loads((output_dir / "structured_document.json").read_text())
            assert document["blocks"][0]["metadata"]["mineru_source"]["source_path"] == ["pages", 0, "blocks", 0]
            assert document["tables"][0]["table_body"].startswith("<table>")
            assert document["images"][0]["ocr_status"] == "available"
            assert "合同OCR正文" in document["images"][0]["ocr_text"]
            assert (output_dir / "images/proof.png").read_bytes() == b"proof image"
            assert (output_dir / "raw_content_list.json").read_bytes() == _archive()[1]
        assert diagnostics["service_protocol"] == "mineru_v1"
        assert diagnostics["task_id"] == "job-1"
        assert not client.is_closed
    assert any(r.method == "DELETE" and r.url.path == f"{prefix}/v1/files/output-1" for r in requests)
    assert any(r.method == "DELETE" and r.url.path == f"{prefix}/v1/files/input-1" for r in requests)


@pytest.mark.parametrize("kind", ["tender", "bid"])
@pytest.mark.parametrize("status", [401, 403, 404, 405, 429, 500])
def test_v4_submission_errors_fail_without_other_protocol_requests(tmp_path, kind, status):
    path = tmp_path / "document.docx"
    path.write_bytes(b"doc")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parser = _parser(kind, client)
        error = ComplianceExtractionError if kind == "tender" else BidDocumentCleaningError
        with pytest.raises(error, match=f"HTTP {status}"):
            if kind == "tender":
                parser.parse(path)
            else:
                parser.parse(path, output_dir=tmp_path / "out")
    assert [r.url.path for r in requests] == ["/v1/uploads"]


@pytest.mark.parametrize("status", ["failed", "canceled", "partial", "unexpected"])
def test_v4_incomplete_jobs_fail_explicitly(tmp_path, status):
    path = tmp_path / "document.docx"
    path.write_bytes(b"doc")
    handler, requests = _v4_handler(job_status=status)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parser = _parser("tender", client)
        with pytest.raises(ComplianceExtractionError, match=status):
            parser.parse(path)
    assert not any(r.method == "GET" and "/v1/files/" in r.url.path for r in requests)
    assert any(r.method == "DELETE" and r.url.path == "/v1/files/input-1" for r in requests)


def test_v4_reused_upload_does_not_delete_existing_input(tmp_path):
    path = tmp_path / "document.docx"
    path.write_bytes(b"doc")
    handler, requests = _v4_handler(reused=True, middle=False)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        blocks = _parser("tender", client).parse(path)
    assert blocks[2].text.startswith("| 字段")
    assert blocks[2].metadata["table_caption"] == ["项目表"]
    assert not any(r.method == "PUT" or r.url.path.endswith("/complete") for r in requests)
    assert not any(r.method == "DELETE" and "input-" in r.url.path for r in requests)


def test_structured_zip_rejects_multiple_documents():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ["a/structured_content.json", "b/structured_content.json"]:
            archive.writestr(name, json.dumps({"pages": []}))
    with pytest.raises(BidDocumentCleaningError, match="多个"):
        MinerUBidDocumentParser._content_list_from_zip(buffer.getvalue())


def test_v4_polling_recovers_from_transient_connection_loss(tmp_path):
    path = tmp_path / "document.docx"
    path.write_bytes(b"doc")
    handler, _ = _v4_handler()
    disconnected = False

    def flaky(request):
        nonlocal disconnected
        if request.url.path.startswith("/v1/parse/jobs/") and not disconnected:
            disconnected = True
            raise httpx.ReadError("Connection interrupted", request=request)
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(flaky)) as client:
        parser = _parser("tender", client)
        assert len(parser.parse(path)) == 4
        assert parser.parse_diagnostics["service_protocol"] == "mineru_v1"
    assert disconnected


def test_v4_polling_http_error_is_not_retried(tmp_path):
    path = tmp_path / "document.docx"
    path.write_bytes(b"doc")
    handler, _ = _v4_handler()
    polls = []

    def failing(request):
        if request.url.path.startswith("/v1/parse/jobs/"):
            polls.append(request)
            return httpx.Response(500)
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(failing)) as client:
        with pytest.raises(ComplianceExtractionError, match="HTTP 500"):
            _parser("tender", client).parse(path)
    assert len(polls) == 1


@pytest.mark.parametrize("changed_setting", ["model", "url"])
def test_image_ocr_cache_is_scoped_to_endpoint_and_model(tmp_path, changed_setting):
    image = tmp_path / "proof.png"
    image.write_bytes(b"image bytes")
    cache_dir = tmp_path / "ocr_cache"
    first_handler, _ = _v4_handler()
    with httpx.Client(transport=httpx.MockTransport(first_handler)) as client:
        first = _parser("bid", client)._ocr_one_image({}, image, cache_dir)
    assert first["status"] == "available" and first["cache_hit"] is False

    prefix = "/new-service" if changed_setting == "url" else ""
    model = "mineru4-new" if changed_setting == "model" else "mineru4"
    handler, requests = _v4_handler(prefix=prefix, model=model)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        parser = MinerUBidDocumentParser(
            f"https://mineru.example{prefix}", mineru_api_key="test-key",
            mineru_gpustack_model=model, http_client=client, poll_interval_seconds=0,
        )
        refreshed = parser._ocr_one_image({}, image, cache_dir)
        cached = parser._ocr_one_image({}, image, cache_dir)
    assert refreshed["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert [r.method for r in requests].count("PUT") == 1
