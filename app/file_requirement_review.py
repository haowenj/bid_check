from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from app.compliance_artifacts import ComplianceExtractionRecorder
from app.models import FileMetadata


FILE_REQUIREMENT_REVIEW_STATUS_LABELS = {
    "pass": "合规",
    "fail": "不合规",
    "not_supported": "无法自动检查",
}


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, FileMetadata):
        return value.to_dict()
    if isinstance(value, dict):
        return dict(value)
    return {}


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "未获取"
    if value < 1024:
        return f"{value} B"
    units = ("KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        amount /= 1024
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
    return f"{value} B"


def inspect_original_bid_file(metadata: Any) -> dict[str, Any]:
    """Read audit metadata from the original uploaded file, never intermediates."""

    raw = _metadata_dict(metadata)
    filename = str(raw.get("filename") or "")
    storage_path = str(raw.get("storage_path") or "")
    recorded_size = raw.get("size")
    try:
        recorded_size = int(recorded_size) if recorded_size is not None else None
    except (TypeError, ValueError):
        recorded_size = None
    path = Path(storage_path) if storage_path else None
    size_bytes: int | None = None
    metadata_error: str | None = None
    sha256: str | None = None
    if path is None or not path.is_file():
        metadata_error = "无法读取原始上传文件的真实文件信息。"
    else:
        try:
            size_bytes = path.stat().st_size
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
        except OSError as exc:
            metadata_error = f"无法读取原始上传文件的真实文件信息：{exc}"
    extension = Path(filename).suffix.lower()
    return {
        "filename": filename,
        "extension": extension,
        "mime_type": mimetypes.guess_type(filename)[0],
        "size_bytes": size_bytes,
        "size_display": _format_bytes(size_bytes),
        "recorded_upload_size": recorded_size,
        "size_source": "original_upload_path.stat" if size_bytes is not None else None,
        "storage_path": storage_path,
        "sha256": sha256,
        "metadata_error": metadata_error,
    }


def _base_review(
    requirement: dict[str, Any],
    file_info: dict[str, Any],
) -> dict[str, Any]:
    parameters = requirement.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    requirement_type = str(requirement.get("requirement_type") or "other")
    expected: dict[str, Any] = {
        "requirement_type": requirement_type,
        "parameters": parameters,
    }
    if requirement_type == "size":
        expected.update(
            {
                "max_bytes": parameters.get("limit_bytes"),
                "max_display": _format_bytes(parameters.get("limit_bytes")),
                "raw_value": parameters.get("raw_value"),
                "raw_unit": parameters.get("raw_unit"),
            }
        )
    elif requirement_type == "extension":
        expected["allowed_extensions"] = parameters.get("allowed_extensions", [])
    elif requirement_type == "filename":
        expected["contains"] = parameters.get(
            "concrete_contains", parameters.get("contains", [])
        )
    return {
        "requirement_id": requirement.get("id"),
        "name": requirement.get("name", ""),
        "requirement": requirement.get("requirement", ""),
        "requirement_type": requirement_type,
        "target": requirement.get("target", "single_bid_file"),
        "parameters": parameters,
        "auto_checkable": bool(requirement.get("auto_checkable")),
        "support_reason": requirement.get("support_reason", ""),
        "source": requirement.get("source", {}),
        "actual": dict(file_info),
        "expected": expected,
    }


def _not_supported(review: dict[str, Any], message: str) -> dict[str, Any]:
    review.update(
        {
            "status": "not_supported",
            "status_label": FILE_REQUIREMENT_REVIEW_STATUS_LABELS["not_supported"],
            "message": message,
        }
    )
    return review


def check_file_requirement(
    requirement: dict[str, Any],
    file_info: dict[str, Any],
) -> dict[str, Any]:
    """Check one extracted rule against one original uploaded file."""

    review = _base_review(requirement, file_info)
    if not requirement.get("auto_checkable"):
        return _not_supported(review, requirement.get("support_reason") or "当前系统不支持该要求的确定性检查。")
    if file_info.get("metadata_error"):
        return _not_supported(review, str(file_info["metadata_error"]))

    requirement_type = review["requirement_type"]
    parameters = review["parameters"]
    if requirement_type == "size":
        limit_bytes = parameters.get("limit_bytes")
        actual_bytes = file_info.get("size_bytes")
        if not isinstance(limit_bytes, int) or actual_bytes is None:
            return _not_supported(review, "大小要求缺少可比较的统一字节值。")
        if actual_bytes <= limit_bytes:
            status = "pass"
            message = f"原始文件大小为 {file_info['size_display']}（{actual_bytes} B），不超过 {_format_bytes(limit_bytes)}。"
        else:
            status = "fail"
            message = f"原始文件大小为 {file_info['size_display']}（{actual_bytes} B），超过 {_format_bytes(limit_bytes)}。"
    elif requirement_type == "extension":
        allowed = {
            str(value).strip().lower()
            for value in parameters.get("allowed_extensions", [])
            if str(value).strip()
        }
        actual_extension = str(file_info.get("extension") or "").lower()
        if not allowed:
            return _not_supported(review, "文件格式要求缺少可比较的扩展名。")
        if actual_extension in allowed:
            status = "pass"
            message = f"原始文件后缀为 {actual_extension or '无'}，符合允许格式。"
        else:
            status = "fail"
            message = f"原始文件后缀为 {actual_extension or '无'}，不在允许格式 {', '.join(sorted(allowed))} 内。"
    elif requirement_type == "filename":
        required_names = parameters.get("concrete_contains", [])
        if not isinstance(required_names, list):
            required_names = []
        required_names = [str(value) for value in required_names if str(value).strip()]
        if not required_names:
            return _not_supported(review, "文件名规则没有可直接核对的具体字面量。")
        filename = str(file_info.get("filename") or "")
        missing = [value for value in required_names if value.casefold() not in filename.casefold()]
        if not missing:
            status = "pass"
            message = "原始文件名包含要求的全部字面量。"
        else:
            status = "fail"
            message = f"原始文件名缺少：{'、'.join(missing)}。"
    else:
        return _not_supported(review, "当前没有针对该单文件属性的确定性比较器。")
    review.update(
        {
            "status": status,
            "status_label": FILE_REQUIREMENT_REVIEW_STATUS_LABELS[status],
            "message": message,
        }
    )
    return review


def run_file_requirement_review(
    extraction_result: dict[str, Any],
    parsed_bid: dict[str, Any],
    *,
    recorder: ComplianceExtractionRecorder | None = None,
) -> dict[str, Any]:
    """Run the single-upload-file checks and persist their standalone result."""

    raw_metadata = parsed_bid.get("original_file_metadata") if isinstance(parsed_bid, dict) else None
    file_info = inspect_original_bid_file(raw_metadata)
    raw_requirements = extraction_result.get("file_requirements", []) if isinstance(extraction_result, dict) else []
    requirements = [item for item in raw_requirements if isinstance(item, dict)] if isinstance(raw_requirements, list) else []
    reviews = [check_file_requirement(requirement, file_info) for requirement in requirements]
    stats = {
        "requirement_count": len(reviews),
        "pass_count": sum(review["status"] == "pass" for review in reviews),
        "fail_count": sum(review["status"] == "fail" for review in reviews),
        "not_supported_count": sum(review["status"] == "not_supported" for review in reviews),
    }
    stats["issue_count"] = stats["fail_count"] + stats["not_supported_count"]
    result = {
        "mode": "file_requirements",
        "source": "original_uploaded_file",
        "original_file": file_info,
        "file_requirement_reviews": reviews,
        "stats": stats,
    }
    if recorder is not None:
        recorder.write_json(
            "10_file_requirement_reviews.json",
            {**result, "requirements": reviews},
        )
        recorder.event(
            "file_requirement.review.end",
            status="complete",
            requirement_count=stats["requirement_count"],
            pass_count=stats["pass_count"],
            fail_count=stats["fail_count"],
            not_supported_count=stats["not_supported_count"],
        )
    return result
