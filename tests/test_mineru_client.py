from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bid_check.mineru_client import MinerUClient, MinerUClientError, _safe_extract_zip


def _zip_bytes(*, unsafe: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escaped.json" if unsafe else "nested/item.json", "{}")
        archive.writestr("images/sample.png", b"png")
    return output.getvalue()


def test_safe_extract_rejects_zip_path_traversal(tmp_path: Path):
    archive_path = tmp_path / "unsafe.zip"
    archive_path.write_bytes(_zip_bytes(unsafe=True))

    with pytest.raises(MinerUClientError, match="unsafe ZIP member"):
        _safe_extract_zip(archive_path, tmp_path / "raw")

    assert not (tmp_path / "escaped.json").exists()


def test_safe_extract_returns_valid_members_under_destination(tmp_path: Path):
    archive_path = tmp_path / "valid.zip"
    archive_path.write_bytes(_zip_bytes())

    extracted = _safe_extract_zip(archive_path, tmp_path / "raw")

    assert [path.relative_to(tmp_path / "raw").as_posix() for path in extracted] == [
        "nested/item.json",
        "images/sample.png",
    ]
    assert (tmp_path / "raw/nested/item.json").read_text() == "{}"
    assert (tmp_path / "raw/images/sample.png").read_bytes() == b"png"


class _MineruHandler(BaseHTTPRequestHandler):
    zip_payload = _zip_bytes()
    requests: list[bytes] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(
            {"status": "healthy", "version": "3.4.4", "protocol_version": 2}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/file_parse":
            self.send_error(404)
            return
        length = int(self.headers["Content-Length"])
        self.__class__.requests.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(self.zip_payload)))
        self.end_headers()
        self.wfile.write(self.zip_payload)


def _running_server():
    _MineruHandler.requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MineruHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_parse_docx_preserves_zip_and_manifest_for_each_run(tmp_path: Path):
    docx_path = tmp_path / "sample.docx"
    docx_path.write_bytes(b"PK fake docx input")
    output_root = tmp_path / "outputs"
    server, thread = _running_server()

    try:
        client = MinerUClient(f"http://127.0.0.1:{server.server_port}", 30)
        health = client.health()
        first = client.parse_docx(docx_path, output_root)
        time.sleep(0.002)
        second = client.parse_docx(docx_path, output_root)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert health["version"] == "3.4.4"
    assert first.run_dir != second.run_dir
    assert first.raw_dir == first.run_dir / "raw"
    assert (first.raw_dir / "response.zip").read_bytes() == _MineruHandler.zip_payload
    assert (first.raw_dir / "nested/item.json").read_text() == "{}"
    assert (first.raw_dir / "images/sample.png").read_bytes() == b"png"

    manifest = json.loads(first.manifest_path.read_text())
    assert manifest["input_sha256"]
    assert manifest["mineru"] == {"version": "3.4.4", "protocol_version": 2}
    assert manifest["response"]["content_type"] == "application/zip"
    assert manifest["request"]["response_format_zip"] is True
    assert manifest["request"]["return_middle_json"] is True
    assert manifest["request"]["return_content_list"] is True
    assert manifest["request"]["return_images"] is True
    assert manifest["request"]["return_md"] is True
    assert manifest["extracted_files"] == [
        "nested/item.json",
        "images/sample.png",
    ]

    body = _MineruHandler.requests[0]
    for field in (
        b"response_format_zip",
        b"return_middle_json",
        b"return_content_list",
        b"return_images",
        b"return_md",
    ):
        assert field in body
