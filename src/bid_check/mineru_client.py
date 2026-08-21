"""HTTP client and raw artifact persistence for the MinerU service."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import requests


class MinerUClientError(RuntimeError):
    """Raised when MinerU cannot provide a usable parse artifact."""


@dataclass(frozen=True, slots=True)
class ParseArtifacts:
    run_dir: Path
    raw_dir: Path
    manifest_path: Path
    input_sha256: str
    extracted_files: tuple[Path, ...]


REQUEST_OPTIONS: dict[str, bool] = {
    "response_format_zip": True,
    "return_middle_json": True,
    "return_content_list": True,
    "return_images": True,
    "return_md": True,
    "return_model_output": False,
    "return_original_file": False,
    "formula_enable": True,
    "table_enable": True,
    "image_analysis": True,
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(zip_path: Path, destination: Path) -> tuple[Path, ...]:
    """Extract regular ZIP files without allowing traversal or symlinks."""

    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                normalized_name = info.filename.replace("\\", "/")
                member = PurePosixPath(normalized_name)
                if member.is_absolute() or ".." in member.parts:
                    raise MinerUClientError(
                        f"unsafe ZIP member: {info.filename!r}"
                    )
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise MinerUClientError(
                        f"unsafe ZIP member symlink: {info.filename!r}"
                    )
                target = (destination / Path(*member.parts)).resolve()
                if target != destination_resolved and destination_resolved not in target.parents:
                    raise MinerUClientError(
                        f"unsafe ZIP member: {info.filename!r}"
                    )
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted.append(target)
    except MinerUClientError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise MinerUClientError(f"cannot extract ZIP {zip_path}: {exc}") from exc
    return tuple(extracted)


def _write_json_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class MinerUClient:
    def __init__(self, base_url: str, timeout_seconds: float = 1800):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.base_url}/health", timeout=self.timeout_seconds
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise MinerUClientError(f"MinerU health request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise MinerUClientError("MinerU health response must be a JSON object")
        return payload

    def parse_docx(self, docx_path: Path, output_root: Path) -> ParseArtifacts:
        docx_path = Path(docx_path)
        if not docx_path.is_file():
            raise MinerUClientError(f"DOCX input does not exist: {docx_path}")
        if docx_path.suffix.lower() != ".docx":
            raise MinerUClientError(f"input must have .docx suffix: {docx_path}")

        input_sha256 = _sha256_file(docx_path)
        run_dir = self._create_run_dir(output_root, docx_path.stem, input_sha256)
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        response_zip = raw_dir / "response.zip"
        started_at = datetime.now(timezone.utc).isoformat()
        health = self.health()
        mineru = {
            "version": health.get("version"),
            "protocol_version": health.get("protocol_version"),
        }
        request_options = dict(REQUEST_OPTIONS)

        try:
            with docx_path.open("rb") as stream:
                response = requests.post(
                    f"{self.base_url}/file_parse",
                    files={
                        "files": (
                            docx_path.name,
                            stream,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                    },
                    data={key: str(value).lower() for key, value in request_options.items()},
                    timeout=self.timeout_seconds,
                    stream=True,
                )
                with response_zip.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
        except (requests.RequestException, OSError) as exc:
            self._write_failure_manifest(
                run_dir,
                docx_path,
                input_sha256,
                mineru,
                request_options,
                started_at,
                response_zip,
                str(exc),
            )
            raise MinerUClientError(
                f"MinerU DOCX parse request failed; raw output: {response_zip}: {exc}"
            ) from exc

        if not response_zip.read_bytes().startswith(b"PK"):
            message = f"MinerU response is not a ZIP (Content-Type={content_type!r})"
            self._write_failure_manifest(
                run_dir,
                docx_path,
                input_sha256,
                mineru,
                request_options,
                started_at,
                response_zip,
                message,
            )
            raise MinerUClientError(f"{message}; raw output: {response_zip}")

        try:
            extracted_files = _safe_extract_zip(response_zip, raw_dir)
        except MinerUClientError as exc:
            self._write_failure_manifest(
                run_dir,
                docx_path,
                input_sha256,
                mineru,
                request_options,
                started_at,
                response_zip,
                str(exc),
            )
            raise

        manifest = self._manifest(
            docx_path,
            input_sha256,
            mineru,
            request_options,
            started_at,
            response_zip,
            content_type,
            extracted_files,
            status="succeeded",
        )
        manifest_path = run_dir / "manifest.json"
        _write_json_atomic(manifest_path, manifest)
        return ParseArtifacts(
            run_dir=run_dir,
            raw_dir=raw_dir,
            manifest_path=manifest_path,
            input_sha256=input_sha256,
            extracted_files=extracted_files,
        )

    @staticmethod
    def _create_run_dir(output_root: Path, stem: str, input_sha256: str) -> Path:
        output_dir = Path(output_root).resolve() / stem
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        base = output_dir / f"{timestamp}-{input_sha256[:12]}"
        candidate = base
        suffix = 1
        while True:
            try:
                candidate.mkdir()
                return candidate
            except FileExistsError:
                candidate = output_dir / f"{base.name}-{suffix}"
                suffix += 1

    @staticmethod
    def _manifest(
        docx_path: Path,
        input_sha256: str,
        mineru: dict[str, Any],
        request_options: dict[str, bool],
        started_at: str,
        response_zip: Path,
        content_type: str,
        extracted_files: tuple[Path, ...],
        *,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        raw_dir = response_zip.parent
        payload: dict[str, Any] = {
            "status": status,
            "input": {
                "name": docx_path.name,
                "bytes": docx_path.stat().st_size,
            },
            "input_sha256": input_sha256,
            "mineru": mineru,
            "request": request_options,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "response": {
                "content_type": content_type,
                "raw_zip": str(response_zip.relative_to(raw_dir.parent)),
            },
            "extracted_files": [
                str(path.relative_to(raw_dir).as_posix()) for path in extracted_files
            ],
        }
        if error:
            payload["error"] = error
        return payload

    def _write_failure_manifest(
        self,
        run_dir: Path,
        docx_path: Path,
        input_sha256: str,
        mineru: dict[str, Any],
        request_options: dict[str, bool],
        started_at: str,
        response_zip: Path,
        error: str,
    ) -> None:
        manifest = self._manifest(
            docx_path,
            input_sha256,
            mineru,
            request_options,
            started_at,
            response_zip,
            "",
            tuple(),
            status="failed",
            error=error,
        )
        _write_json_atomic(run_dir / "manifest.json", manifest)
