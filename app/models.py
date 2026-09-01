from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

CheckMode = Literal["compliance", "evaluation", "full"]
TaskStatus = Literal["pending", "running", "complete", "failed"]
StageName = Literal["requirements", "bid_parse", "review"]


class TenderRequirementSource(TypedDict):
    section: str
    block_ids: list[str]
    source_text: str


class TenderRequirement(TypedDict):
    id: str
    name: str
    rule: str
    condition: str | None
    source: TenderRequirementSource


TenderSource = TenderRequirementSource


class TenderTemplateTable(TypedDict):
    block_id: str
    text: str
    metadata: dict[str, Any]


class TenderTemplate(TypedDict):
    id: str
    name: str
    section: str
    block_ids: list[str]
    body: str
    tables: list[TenderTemplateTable]
    fields: list[str]
    attachments: list[str]
    source: TenderSource


class ProjectRequirement(TypedDict):
    id: str
    requirement: str
    value: str | None
    source: TenderSource


class SupplementalMaterial(TypedDict):
    id: str
    name: str
    material: str
    source: TenderSource


class TenderExtractionResult(TypedDict):
    templates: list[TenderTemplate]
    project_requirements: list[ProjectRequirement]
    supplemental_materials: list[SupplementalMaterial]


@dataclass(frozen=True)
class FileMetadata:
    filename: str
    size: int
    storage_path: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "filename": self.filename,
            "size": self.size,
            "storage_path": self.storage_path,
        }


@dataclass(frozen=True)
class BidCheckTask:
    task_id: str
    tender_file: FileMetadata
    bid_file: FileMetadata
    check_mode: CheckMode
    status: TaskStatus
    requirements_status: TaskStatus
    bid_parse_status: TaskStatus
    review_status: TaskStatus
    failed_stage: StageName | None
    error_message: str | None
    result: dict[str, Any] | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "tender_file": self.tender_file.to_dict(),
            "bid_file": self.bid_file.to_dict(),
            "check_mode": self.check_mode,
            "status": self.status,
            "requirements_status": self.requirements_status,
            "bid_parse_status": self.bid_parse_status,
            "review_status": self.review_status,
            "failed_stage": self.failed_stage,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.result is not None:
            payload.update(self.result)
        return payload
