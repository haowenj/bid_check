"""MinerU 4.x upload/job transport and structured-content compatibility."""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import time
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

MINERU_V1_PROTOCOL_LABEL = "mineru_v1"
MINERU_V1_PROTOCOL_VERSION = "mineru-api-v1"


def run_v1_task(
    client: httpx.Client,
    path: Path,
    *,
    base_url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    poll_interval_seconds: float,
    error_type: type[RuntimeError],
) -> tuple[bytes, str]:
    def request(method: str, route: str, label: str, **kwargs) -> httpx.Response:
        try:
            response = client.request(
                method, f"{base_url}{route}", headers=headers,
                follow_redirects=False, **kwargs,
            )
        except httpx.HTTPError as exc:
            raise error_type(f"MinerU 4.x {label}失败：{type(exc).__name__}。") from exc
        if not 200 <= response.status_code < 300:
            raise error_type(f"MinerU 4.x {label}失败：HTTP {response.status_code}。")
        return response

    def json_request(method: str, route: str, label: str, **kwargs) -> dict[str, Any]:
        response = request(method, route, label, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise error_type(f"MinerU 4.x {label}响应不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise error_type(f"MinerU 4.x {label}响应必须是 JSON 对象。")
        return payload

    def identifier(payload: Any, key: str, label: str) -> str:
        value = payload.get(key) if isinstance(payload, dict) else None
        if (
            not isinstance(value, str) or not value.strip()
            or any(c in value for c in "/\\?#")
        ):
            raise error_type(f"MinerU 4.x 返回了无效 {label}。")
        return value

    try:
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        size = path.stat().st_size
    except OSError as exc:
        raise error_type(f"MinerU 上传文件读取失败：{type(exc).__name__}。") from exc

    input_file_id = None
    output_file_id = None
    remove_input = False
    try:
        upload = json_request(
            "POST", "/v1/uploads", "创建上传",
            json={
                "filename": path.name,
                "bytes": size,
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "purpose": "parse",
                "sha256sum": digest,
            },
        )
        upload_id = identifier(upload, "id", "upload id")
        if upload.get("status") == "pending":
            url = upload.get("upload_url") or f"/v1/uploads/{upload_id}/content"
            if not isinstance(url, str) or upload.get("upload_method", "PUT") != "PUT":
                raise error_type("MinerU 4.x 返回了无效上传 URL 或方法。")
            url = urllib.parse.urljoin(f"{base_url}/", url)
            base = urllib.parse.urlsplit(base_url)
            target = urllib.parse.urlsplit(url)
            def origin(value: urllib.parse.SplitResult) -> tuple[str, str | None, int]:
                port = value.port or (443 if value.scheme == "https" else 80)
                return value.scheme, value.hostname, port

            if origin(base) != origin(target) or target.fragment or target.username or target.password:
                raise error_type("MinerU 返回了不可信上传 URL。")
            upload_headers = upload.get("upload_headers") or {"Content-Type": "application/octet-stream"}
            if not isinstance(upload_headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in upload_headers.items()
            ):
                raise error_type("MinerU 4.x 返回了无效上传 headers。")
            try:
                with path.open("rb") as source:
                    response = client.put(
                        url, content=source, headers={**upload_headers, **headers},
                        follow_redirects=False,
                    )
            except (OSError, httpx.HTTPError) as exc:
                raise error_type(f"MinerU 4.x 上传内容失败：{type(exc).__name__}。") from exc
            if not 200 <= response.status_code < 300:
                raise error_type(f"MinerU 4.x 上传内容失败：HTTP {response.status_code}。")
            upload = json_request(
                "POST", f"/v1/uploads/{upload_id}/complete", "完成上传",
                json={"sha256sum": digest},
            )
            remove_input = True
        if upload.get("status") != "completed":
            raise error_type("MinerU 4.x 返回了无效上传状态。")
        input_file_id = identifier(upload.get("file"), "id", "input file id")
        job = json_request(
            "POST", "/v1/parse/jobs", "提交解析任务",
            json={
                "files": [{"source": {"type": "file_id", "file_id": input_file_id}}],
                "tier": "standard",
                "ocr_mode": "auto",
                "output_formats": ["zip"],
            },
        )
        job_id = identifier(job, "job_id", "job id")
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = job.get("status")
            if status == "completed":
                break
            if status not in {"queued", "running"}:
                raise error_type(f"MinerU 4.x 解析任务未完成：{status!r}。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise error_type("MinerU 4.x 任务等待超时。")
            if poll_interval_seconds:
                time.sleep(min(poll_interval_seconds, remaining))
            try:
                job = json_request("GET", f"/v1/parse/jobs/{job_id}", "查询任务状态")
            except error_type as exc:
                # A dropped read does not cancel the job. Poll its existing ID
                # again within the deadline; never resubmit a parse job.
                if isinstance(exc.__cause__, httpx.TransportError):
                    continue
                raise
        files = job.get("files")
        if (
            not isinstance(files, list) or len(files) != 1
            or not isinstance(files[0], dict) or files[0].get("status") != "completed"
        ):
            raise error_type("MinerU 4.x 任务没有唯一的完整成功文件。")
        outputs = files[0].get("output_files")
        output_file_id = identifier(
            outputs.get("zip") if isinstance(outputs, dict) else None,
            "file_id", "ZIP output file id",
        )
        response = request("GET", f"/v1/files/{output_file_id}/content", "下载结果")
        return response.content, job_id
    finally:
        # Reused input files belong to an earlier upload; only delete our own.
        for file_id in [output_file_id, input_file_id if remove_input else None]:
            if file_id:
                try:
                    client.delete(
                        f"{base_url}/v1/files/{file_id}", headers=headers,
                        follow_redirects=False,
                    )
                except httpx.HTTPError:
                    pass


def read_structured_archive(
    archive: zipfile.ZipFile,
    safe_members: list[zipfile.ZipInfo],
    error_type: type[RuntimeError],
) -> tuple[list[dict[str, Any]], bytes, str]:
    members = [
        info for info in safe_members
        if PurePosixPath(info.filename.replace("\\", "/")).name == "structured_content.json"
    ]
    if len(members) != 1:
        message = "返回了多个 structured_content" if members else "未返回 content list 或 structured_content"
        raise error_type(f"MinerU 结果 ZIP {message}。")
    selected = members[0]
    raw = archive.read(selected)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        raise error_type("MinerU structured_content 缺少 pages。")
    middle_name = str(PurePosixPath(selected.filename.replace("\\", "/")).with_name("middle_json.json"))
    middle_info = next(
        (info for info in safe_members if info.filename.replace("\\", "/") == middle_name),
        None,
    )
    middle = json.loads(archive.read(middle_info)) if middle_info else {}
    middle_pages = middle.get("pages", []) if isinstance(middle, dict) else []
    middle_by_page = {
        p.get("page_idx", i): p for i, p in enumerate(middle_pages) if isinstance(p, dict)
    }

    def styles(value):
        if isinstance(value, list):
            return [styles(v) for v in value]
        if isinstance(value, dict):
            result = {k: styles(v) for k, v in value.items()}
            if "styles" in result:
                result["style"] = result["styles"]
            return result
        return value

    def text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(text(v) for v in value)
        if isinstance(value, dict):
            return text(value.get("content", ""))
        return ""

    items = []
    for page_index, page in enumerate(payload["pages"]):
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            raise error_type("MinerU structured_content 返回了无效页面。")
        page_idx = page.get("page_idx", page_index)
        middle_blocks = middle_by_page.get(page_idx, {}).get("blocks", [])
        for block_index, block in enumerate(page["blocks"]):
            if not isinstance(block, dict):
                raise error_type("MinerU structured_content 返回了无效结构块。")
            item = copy.deepcopy(block)
            content = item.pop("content", "")
            raw_type = item.get("type", "text")
            item.update(
                page_idx=page_idx,
                mineru_source_path=["pages", page_index, "blocks", block_index],
                mineru_structured_block=copy.deepcopy(block),
            )
            detail = middle_blocks[block_index] if block_index < len(middle_blocks) else {}
            detail_content = (
                detail.get("content")
                if isinstance(detail, dict) and detail.get("type") == raw_type else None
            )
            if detail_content is not None:
                item["mineru_nested_content"] = styles(detail_content)
            if raw_type == "table":
                item["table_body"] = text(content)
                if isinstance(detail_content, list):
                    body = next(
                        (span for span in detail_content
                         if isinstance(span, dict) and span.get("type") == "table_body"),
                        None,
                    )
                    if body is not None:
                        item["table_body"] = text(body)
                item["table_caption"] = [text(v) for v in block.get("captions", [])]
                item["table_footnote"] = [text(v) for v in block.get("footnotes", [])]
            elif raw_type in {"image", "chart", "figure"}:
                item["type"] = "image"
                item["image_caption"] = [text(v) for v in block.get("captions", [])]
                item["image_footnote"] = [text(v) for v in block.get("footnotes", [])]
                item["text"] = " ".join(item["image_caption"])
            else:
                item["text"] = text(detail_content) if detail_content is not None else text(content)
                if raw_type in {"paragraph_title", "title", "heading"}:
                    item["type"] = "title"
                    item["text_level"] = block.get("level", 1)
            image_source = block.get("image_source")
            if isinstance(image_source, str):
                item["img_path"] = image_source
            elif isinstance(image_source, dict) and isinstance(image_source.get("path"), str):
                item["img_path"] = image_source["path"]
            items.append(item)
    return items, raw, selected.filename
