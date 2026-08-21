from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures/mineru_3_4_4_docx"


def _contract_zip(*, include_content_lists: bool = True) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if include_content_lists:
            archive.writestr(
                "fixture/office/fixture_content_list_v2.json",
                (FIXTURES / "content_list_v2.json").read_bytes(),
            )
            archive.writestr(
                "fixture/office/fixture_content_list.json",
                (FIXTURES / "content_list.json").read_bytes(),
            )
            archive.writestr(
                "fixture/office/fixture_middle.json",
                (FIXTURES / "middle_excerpt.json").read_bytes(),
            )
            archive.writestr(
                "fixture/office/images/dc37d7cc9f1f551e3fbefcdab47207aa55d6fd3bfcf501243f23000b50e823d1.jpg",
                b"image",
            )
        else:
            archive.writestr("fixture/office/readme.txt", "no content list")
    return output.getvalue()


class _CliMineruHandler(BaseHTTPRequestHandler):
    payload = _contract_zip()
    content_type = "application/zip"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        body = b'{"status":"healthy","version":"3.4.4","protocol_version":2}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


def _server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CliMineruHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_cli_success_writes_blocks_report_and_prints_statistics(tmp_path: Path):
    input_path = tmp_path / "fixture.docx"
    input_path.write_bytes(b"PK fake docx")
    output_root = tmp_path / "outputs"
    server, thread = _server()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/parse_docx.py",
                str(input_path),
                "--mineru-url",
                f"http://127.0.0.1:{server.server_port}",
                "--output-dir",
                str(output_root),
                "--timeout",
                "30",
                "--top-longest",
                "3",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stderr
    run_dir = next(output_root.glob("fixture/*"))
    blocks_path = run_dir / "document_blocks.json"
    report_path = run_dir / "report.json"
    manifest_path = run_dir / "manifest.json"
    assert blocks_path.is_file()
    assert report_path.is_file()
    assert manifest_path.is_file()
    blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(blocks, list)
    assert report["block_count"] == len(blocks)
    assert manifest["normalization"]["source_json"] == "content_list_v2"
    assert "原始结果目录" in result.stdout
    assert "标准化后的 document_blocks.json" in result.stdout
    assert "各 block_type 数量" in result.stdout
    assert "文本长度" in result.stdout
    assert "标题层级" in result.stdout
    assert "section_path 示例" in result.stdout
    assert "最长文本块" in result.stdout


def test_cli_rejects_missing_input_with_nonzero_exit(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "scripts/parse_docx.py", str(tmp_path / "missing.docx")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "DOCX input does not exist" in result.stderr


def test_cli_keeps_raw_response_when_zip_has_no_supported_content_list(tmp_path: Path):
    input_path = tmp_path / "fixture.docx"
    input_path.write_bytes(b"PK fake docx")
    output_root = tmp_path / "outputs"
    original_payload = _CliMineruHandler.payload
    _CliMineruHandler.payload = _contract_zip(include_content_lists=False)
    server, thread = _server()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/parse_docx.py",
                str(input_path),
                "--mineru-url",
                f"http://127.0.0.1:{server.server_port}",
                "--output-dir",
                str(output_root),
                "--timeout",
                "30",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        _CliMineruHandler.payload = original_payload
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode != 0
    assert "supported content list" in result.stderr
    response_zips = list(output_root.glob("fixture/*/raw/response.zip"))
    assert len(response_zips) == 1
