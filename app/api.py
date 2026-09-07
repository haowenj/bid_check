from __future__ import annotations

import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.attachment_review import (
    DeterministicAttachmentReviewLLM,
    OpenAICompatibleAttachmentReviewLLM,
    run_compliance_review_with_attachments,
)
from app.bid_document import MinerUBidDocumentParser
from app.bid_document import parse_bid_document as clean_bid_document
from app.compliance_extraction import (
    DeterministicComplianceLLM,
    DocumentParser,
    JsonDocumentCache,
    JsonRequirementCache,
    MinerUDocumentParser,
    OpenAICompatibleLLM,
    extract_tender_compliance_objects,
)
from app.config import Settings, load_settings
from app.evaluation_rule_extraction import (
    DeterministicEvaluationRuleLLM,
    OpenAICompatibleEvaluationRuleLLM,
    extract_tender_evaluation_rules,
)
from app.evaluation_summary import load_evaluation_summary
from app.models import BidCheckTask, FileMetadata
from app.objective_scoring import (
    load_reusable_bid_evidence,
    load_reusable_tender_evidence,
    run_objective_scoring,
)
from app.repository import BidCheckRepository
from app.subjective_scoring import (
    DeterministicSubjectiveScoreLLM,
    OpenAICompatibleSubjectiveScoreLLM,
    run_subjective_scoring,
)
from app.template_matching import build_template_comparisons
from app.template_text_review import (
    DeterministicTemplateTextReviewLLM,
)
from app.veto_rule_execution import run_veto_rule_execution
from app.workflow import BidCheckServices, BidCheckWorkflow

CheckModeInput = Literal["compliance", "evaluation", "full"]
logger = logging.getLogger(__name__)
STAGE_LABELS = {
    "requirements": "提取招标文件检查对象",
    "bid_parse": "解析投标文件",
    "review": "执行合规性检查",
}
TASK_STATUS_LABELS = {
    "pending": "未开始",
    "running": "检查中",
    "complete": "已完成",
    "failed": "失败",
}
CHECK_MODE_LABELS = {
    "compliance": "标书合规性校验",
    "evaluation": "评标规则校验",
    "full": "全面校验",
}


def _load_bid_document_for_page(task: BidCheckTask) -> dict[str, Any] | None:
    """Load a compact, section-oriented view of the bid parse artifact."""

    artifact_path = (
        Path(task.bid_file.storage_path).parent
        / "bid_document_cleaning"
        / "structured_document.json"
    )
    try:
        document = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None

    raw_blocks = document.get("blocks", [])
    raw_tables = document.get("tables", [])
    raw_images = document.get("images", [])
    if not isinstance(raw_blocks, list):
        raw_blocks = []
    if not isinstance(raw_tables, list):
        raw_tables = []
    if not isinstance(raw_images, list):
        raw_images = []

    blocks_by_id = {
        block.get("block_id"): block
        for block in raw_blocks
        if isinstance(block, dict) and block.get("block_id")
    }
    tables_by_block_id = {
        table.get("block_id"): table
        for table in raw_tables
        if isinstance(table, dict) and table.get("block_id")
    }
    images_by_block_id = {
        image.get("block_id"): image
        for image in raw_images
        if isinstance(image, dict) and image.get("block_id")
    }

    sections: list[dict[str, Any]] = []
    raw_sections = document.get("sections", [])
    if not isinstance(raw_sections, list):
        raw_sections = []
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            continue
        direct_block_ids = raw_section.get("direct_block_ids")
        if not isinstance(direct_block_ids, list):
            direct_block_ids = raw_section.get("block_ids", [])
        if not isinstance(direct_block_ids, list):
            direct_block_ids = []

        blocks: list[dict[str, Any]] = []
        for block_id in direct_block_ids:
            block = blocks_by_id.get(block_id)
            if not isinstance(block, dict):
                continue
            display_block = {
                "block_id": block.get("block_id", ""),
                "type": block.get("type", "paragraph"),
                "text": block.get("text", ""),
                "order": block.get("order"),
                "is_section_heading": (
                    block.get("type") == "heading"
                    and block.get("order") == raw_section.get("start_order")
                ),
            }
            if block.get("type") == "table":
                table = tables_by_block_id.get(block.get("block_id"), {})
                display_block["rows"] = (
                    table.get("rows", []) if isinstance(table, dict) else []
                )
            elif block.get("type") == "image":
                image = images_by_block_id.get(block.get("block_id"), {})
                if isinstance(image, dict):
                    display_block["img_path"] = image.get("img_path")
                    display_block["caption"] = image.get("caption", "")
            blocks.append(display_block)

        sections.append(
            {
                "section_id": raw_section.get("section_id", ""),
                "parent_section_id": raw_section.get("parent_section_id"),
                "title": raw_section.get("title", "未命名章节"),
                "level": raw_section.get("level", 1),
                "path": raw_section.get("path", []),
                "block_count": len(blocks),
                "table_count": sum(block.get("type") == "table" for block in blocks),
                "image_count": sum(block.get("type") == "image" for block in blocks),
                "blocks": blocks,
                "child_sections": [],
            }
        )

    sections_by_id = {
        section["section_id"]: section
        for section in sections
        if section.get("section_id")
    }
    for section in sections:
        parent = sections_by_id.get(section.get("parent_section_id"))
        if parent is not None:
            parent["child_sections"].append(section)

    def populate_module_totals(section: dict[str, Any]) -> None:
        for child in section["child_sections"]:
            populate_module_totals(child)
        section["subsection_count"] = len(section["child_sections"])
        section["module_block_count"] = section["block_count"] + sum(
            child["module_block_count"] for child in section["child_sections"]
        )
        section["module_table_count"] = section["table_count"] + sum(
            child["module_table_count"] for child in section["child_sections"]
        )
        section["module_image_count"] = section["image_count"] + sum(
            child["module_image_count"] for child in section["child_sections"]
        )

    for section in sections:
        if not section.get("parent_section_id"):
            populate_module_totals(section)

    return {
        "source": document.get("source", {}),
        "stats": document.get("stats", {}),
        "sections": sections,
    }


def _attach_template_text_reviews(
    comparisons: list[dict[str, Any]],
    review_result: Any,
) -> list[dict[str, Any]]:
    reviews = (
        review_result.get("template_text_reviews", [])
        if isinstance(review_result, dict)
        else []
    )
    reviews_by_template_id = {
        str(review.get("template_id")): review
        for review in reviews
        if isinstance(review, dict) and review.get("template_id")
    }
    for comparison in comparisons:
        tender = comparison.get("tender")
        template_id = tender.get("id") if isinstance(tender, dict) else None
        comparison["text_review"] = (
            reviews_by_template_id.get(str(template_id))
            if template_id is not None
            else None
        )
    return comparisons


def _attach_attachment_reviews(
    comparisons: list[dict[str, Any]],
    review_result: Any,
) -> list[dict[str, Any]]:
    reviews = (
        review_result.get("attachment_reviews", [])
        if isinstance(review_result, dict)
        else []
    )
    reviews_by_template_id = {
        str(review.get("template_id")): review
        for review in reviews
        if isinstance(review, dict) and review.get("template_id")
    }
    for comparison in comparisons:
        tender = comparison.get("tender")
        template_id = tender.get("id") if isinstance(tender, dict) else None
        comparison["attachment_review"] = (
            reviews_by_template_id.get(str(template_id))
            if template_id is not None
            else None
        )
    return comparisons


def _count_problem_reviews(
    reviews: Any,
    *,
    include_semantic_skipped: bool = False,
    include_not_supported: bool = False,
) -> int:
    if not isinstance(reviews, list):
        return 0
    count = 0
    for review in reviews:
        if not isinstance(review, dict):
            continue
        execution_status = review.get("execution_status", "")
        if execution_status == "failed":
            count += 1
        elif include_semantic_skipped and execution_status == "semantic_skipped":
            count += 1
        elif include_not_supported and review.get("status") == "not_supported":
            count += 1
        elif review.get("status") in {"fail", "uncertain"}:
            count += 1
    return count


def _load_file_requirement_review_for_page(task: BidCheckTask) -> dict[str, Any]:
    """Read the standalone file-review artifact before using stored fallback data."""

    artifact_path = (
        Path(task.tender_file.storage_path).parent
        / "compliance_extraction"
        / "10_file_requirement_reviews.json"
    )
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        artifact = None
    if isinstance(artifact, dict):
        reviews = artifact.get("requirements", artifact.get("file_requirement_reviews", []))
        return {
            "source": "artifact",
            "reviews": reviews if isinstance(reviews, list) else [],
            "original_file": artifact.get("original_file", {}),
            "stats": artifact.get("stats", {}),
        }

    result = task.result if isinstance(task.result, dict) else {}
    review_result = result.get("review_result")
    if not isinstance(review_result, dict):
        review_result = {}
    reviews = review_result.get("file_requirement_reviews", [])
    return {
        "source": "review_result",
        "reviews": reviews if isinstance(reviews, list) else [],
        "original_file": review_result.get("file_requirement_original_file", {}),
        "stats": review_result.get("file_requirement_stats", {}),
    }


def _load_subjective_scores_for_page(task: BidCheckTask) -> dict[str, Any] | None:
    """Read the independent subjective-score artifact for API consumers."""

    artifact_path = (
        Path(task.tender_file.storage_path).parent
        / "compliance_extraction"
        / "subjective_scores.json"
    )
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return artifact if isinstance(artifact, dict) else None


def _format_score_for_page(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "未记录"
    return str(int(value)) if float(value).is_integer() else str(value)


def _load_evaluation_rules_for_page(task: BidCheckTask) -> dict[str, Any]:
    """Load the standalone evaluation artifact and add display-only relations."""

    result = task.result if isinstance(task.result, dict) else {}
    artifact_path = (
        Path(task.tender_file.storage_path).parent
        / "compliance_extraction"
        / "11_evaluation_rules.json"
    )
    try:
        rules = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rules = result.get("evaluation_rules")
    if not isinstance(rules, dict):
        rules = {}

    categories = [
        dict(item) for item in rules.get("score_categories", [])
        if isinstance(item, dict)
    ]
    score_items = [
        dict(item) for item in rules.get("score_items", [])
        if isinstance(item, dict)
    ]
    category_by_id = {
        item.get("id"): item for item in categories if item.get("id")
    }
    item_by_id = {
        item.get("id"): item for item in score_items if item.get("id")
    }
    items_by_category: dict[str, list[dict[str, Any]]] = {}
    for item in score_items:
        category_id = item.get("category_id")
        item["category_name"] = (
            category_by_id.get(category_id, {}).get("name")
            if category_id
            else None
        )
        parent_id = item.get("parent_item_id")
        item["parent_item_name"] = (
            item_by_id.get(parent_id, {}).get("name") if parent_id else None
        )
        item["full_score_label"] = _format_score_for_page(item.get("full_score"))
        if category_id:
            items_by_category.setdefault(category_id, []).append(item)
    for category in categories:
        category["parent_name"] = (
            category_by_id.get(category.get("parent_id"), {}).get("name")
            if category.get("parent_id")
            else None
        )
        category["full_score_label"] = _format_score_for_page(
            category.get("full_score")
        )
        category["items"] = items_by_category.get(category.get("id"), [])

    stats = dict(rules.get("stats", {})) if isinstance(rules.get("stats"), dict) else {}
    stats.setdefault("score_category_count", len(categories))
    stats.setdefault("score_item_count", len(score_items))
    stats.setdefault(
        "veto_rule_count",
        len(rules.get("veto_rules", []))
        if isinstance(rules.get("veto_rules"), list)
        else 0,
    )
    stats.setdefault(
        "uncertain_rule_count",
        len(rules.get("uncertain_rules", []))
        if isinstance(rules.get("uncertain_rules"), list)
        else 0,
    )
    return {
        **rules,
        "score_categories": categories,
        "score_items": score_items,
        "veto_rules": [
            dict(item) for item in rules.get("veto_rules", [])
            if isinstance(item, dict)
        ],
        "uncertain_rules": [
            dict(item) for item in rules.get("uncertain_rules", [])
            if isinstance(item, dict)
        ],
        "stats": stats,
    }


def _task_issue_counts(task: BidCheckTask) -> dict[str, int]:
    """Build list-page counts using the same result sources as the detail page."""

    result = task.result if isinstance(task.result, dict) else {}
    review_result = result.get("review_result")
    if not isinstance(review_result, dict):
        review_result = {}

    bid_document = _load_bid_document_for_page(task)
    template_comparisons = build_template_comparisons(
        result.get("templates", []),
        bid_document.get("sections", []) if bid_document else [],
    )
    template_issues = sum(
        comparison.get("status") in {"unmatched", "ambiguous"}
        for comparison in template_comparisons
        if isinstance(comparison, dict)
    )
    template_issues += _count_problem_reviews(
        review_result.get("template_text_reviews"),
        include_semantic_skipped=True,
    )
    attachment_issues = _count_problem_reviews(
        review_result.get("attachment_reviews"),
        include_semantic_skipped=True,
    )
    performance_issues = _count_problem_reviews(
        review_result.get("performance_reviews")
    )
    file_requirement_data = _load_file_requirement_review_for_page(task)
    file_requirement_issues = _count_problem_reviews(
        file_requirement_data["reviews"],
        include_not_supported=True,
    )
    return {
        "template": int(template_issues),
        "attachment": int(attachment_issues),
        "performance": int(performance_issues),
        "file": int(file_requirement_issues),
    }


def _build_task_list_rows(tasks: list[BidCheckTask]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task in tasks:
        issue_counts = _task_issue_counts(task) if task.result else {
            "template": 0,
            "attachment": 0,
            "performance": 0,
            "file": 0,
        }
        rows.append(
            {
                "task": task,
                "status_label": TASK_STATUS_LABELS.get(task.status, task.status),
                "check_mode_label": CHECK_MODE_LABELS.get(
                    task.check_mode, task.check_mode
                ),
                "created_at_display": task.created_at.replace("T", " ", 1)[:19],
                "issue_counts": issue_counts,
                "total_issue_count": sum(issue_counts.values()),
                "has_result": isinstance(task.result, dict),
            }
        )
    return rows


def build_default_workflow(
    settings: Settings,
    repository: BidCheckRepository,
    *,
    document_parser: DocumentParser | None = None,
    bid_document_parser: MinerUBidDocumentParser | None = None,
) -> BidCheckWorkflow:
    parser = document_parser or MinerUDocumentParser(
        settings.mineru_url,
        mineru_api_key=settings.mineru_api_key,
        mineru_backend=settings.mineru_backend,
        mineru_server_url=settings.mineru_server_url,
        timeout_seconds=settings.mineru_timeout_seconds,
        poll_interval_seconds=settings.mineru_poll_interval_seconds,
    )
    cache = JsonRequirementCache(settings.data_dir / "compliance_cache")
    evaluation_cache = JsonRequirementCache(
        settings.data_dir / "evaluation_rule_cache"
    )
    parser_cache = JsonDocumentCache(settings.data_dir / "mineru_cache")
    bid_parser = bid_document_parser or MinerUBidDocumentParser(
        settings.mineru_url,
        mineru_api_key=settings.mineru_api_key,
        mineru_backend=settings.mineru_backend,
        mineru_server_url=settings.mineru_server_url,
        timeout_seconds=settings.mineru_timeout_seconds,
        poll_interval_seconds=settings.mineru_poll_interval_seconds,
    )
    if settings.llm_api_key:
        llm = OpenAICompatibleLLM(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        template_review_llm = llm
        attachment_review_llm = OpenAICompatibleAttachmentReviewLLM(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        evaluation_llm = OpenAICompatibleEvaluationRuleLLM(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=max(settings.llm_timeout_seconds, 180.0),
        )
    else:
        llm = DeterministicComplianceLLM()
        template_review_llm = DeterministicTemplateTextReviewLLM()
        attachment_review_llm = DeterministicAttachmentReviewLLM()
        evaluation_llm = DeterministicEvaluationRuleLLM()

    if settings.llm_api_key:
        subjective_llm = OpenAICompatibleSubjectiveScoreLLM(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=max(settings.llm_timeout_seconds, 180.0),
        )
    else:
        subjective_llm = DeterministicSubjectiveScoreLLM()

    def extract_requirements(
        file_metadata: FileMetadata,
        recorder=None,
    ):
        return extract_tender_compliance_objects(
            file_metadata,
            parser=parser,
            llm=llm,
            cache=cache,
            parser_cache=parser_cache,
            recorder=recorder,
            max_batches=settings.compliance_max_batches,
        )

    def extract_evaluation(
        file_metadata: FileMetadata,
        recorder=None,
    ):
        return extract_tender_evaluation_rules(
            file_metadata,
            parser=parser,
            llm=evaluation_llm,
            cache=evaluation_cache,
            parser_cache=parser_cache,
            recorder=recorder,
            max_batches=settings.compliance_max_batches,
        )

    def score_objective(
        tender_file: FileMetadata,
        bid_file: FileMetadata,
        evaluation_result: dict[str, Any],
        recorder=None,
    ) -> dict[str, Any]:
        tender_evidence = load_reusable_tender_evidence(tender_file)
        evidence = load_reusable_bid_evidence(bid_file)
        parser_fallback_used = False
        if evidence["bid_document"] is None:
            parser_fallback_used = True
            bid_path = Path(bid_file.storage_path).expanduser().resolve()
            clean_bid_document(
                bid_file,
                parser=bid_parser,
                output_dir=bid_path.parent / "bid_document_cleaning",
            )
            evidence = load_reusable_bid_evidence(bid_file)
        return run_objective_scoring(
            evaluation_result,
            bid_file,
            bid_document=evidence["bid_document"],
            artifact_dir=(
                Path(evidence["bid_document_artifact"]).parent
                if evidence["bid_document_artifact"]
                else None
            ),
            tender_evidence=tender_evidence,
            recorder=recorder,
            bid_parse_fallback_used=parser_fallback_used,
        )

    def score_subjective(
        tender_file: FileMetadata,
        bid_file: FileMetadata,
        evaluation_result: dict[str, Any],
        recorder=None,
    ) -> dict[str, Any]:
        del tender_file
        # The subjective runner loads the existing structured bid artifact once
        # and never falls back to MinerU/OCR/full-document parsing.
        return run_subjective_scoring(
            evaluation_result,
            bid_file,
            subjective_llm=subjective_llm,
            recorder=recorder,
        )

    def execute_veto(
        tender_file: FileMetadata,
        bid_file: FileMetadata,
        evaluation_result: dict[str, Any],
        *,
        objective_scores: dict[str, Any] | None = None,
        recorder=None,
    ) -> dict[str, Any]:
        tender_evidence = load_reusable_tender_evidence(tender_file)
        evidence = load_reusable_bid_evidence(bid_file)
        if evidence["bid_document"] is None:
            bid_path = Path(bid_file.storage_path).expanduser().resolve()
            clean_bid_document(
                bid_file,
                parser=bid_parser,
                output_dir=bid_path.parent / "bid_document_cleaning",
            )
            evidence = load_reusable_bid_evidence(bid_file)
        return run_veto_rule_execution(
            evaluation_result,
            bid_file,
            bid_document=evidence["bid_document"],
            artifact_dir=(
                Path(evidence["bid_document_artifact"]).parent
                if evidence["bid_document_artifact"]
                else None
            ),
            objective_scores=objective_scores,
            tender_evidence=tender_evidence,
            recorder=recorder,
        )

    services = BidCheckServices(
        extract=extract_requirements,
        parse=partial(
            clean_bid_document,
            parser=bid_parser,
        ),
        review=partial(
            run_compliance_review_with_attachments,
            template_review_llm=template_review_llm,
            attachment_review_llm=attachment_review_llm,
            performance_text_llm=template_review_llm,
        ),
        extract_with_recorder=extract_requirements,
        extract_evaluation_with_recorder=extract_evaluation,
        score_objective_with_recorder=score_objective,
        score_subjective_with_recorder=score_subjective,
        execute_veto_with_recorder=execute_veto,
        review_with_recorder=partial(
            run_compliance_review_with_attachments,
            template_review_llm=template_review_llm,
            attachment_review_llm=attachment_review_llm,
            performance_text_llm=template_review_llm,
        ),
    )
    return BidCheckWorkflow(repository, services)


def create_app(
    *,
    settings: Settings | None = None,
    repository: BidCheckRepository | None = None,
    workflow: BidCheckWorkflow | None = None,
) -> FastAPI:
    active_settings = settings or load_settings()
    active_repository = repository or BidCheckRepository(active_settings.database_path)
    active_workflow = workflow or build_default_workflow(
        active_settings,
        active_repository,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        active_workflow.shutdown()

    application = FastAPI(title="标书检查", lifespan=lifespan)
    application.state.settings = active_settings
    application.state.repository = active_repository
    application.state.workflow = active_workflow
    app_dir = Path(__file__).resolve().parent
    application.mount(
        "/static",
        StaticFiles(directory=str(app_dir / "static")),
        name="static",
    )
    templates = Jinja2Templates(directory=str(app_dir / "templates"))

    @application.get("/")
    def root():
        return RedirectResponse("/bid-check")

    @application.get("/bid-check", response_class=HTMLResponse)
    def bid_check_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="bid_check.html",
            context={},
        )

    @application.get("/bid-check/tasks", response_class=HTMLResponse)
    def bid_check_tasks_page(request: Request):
        task_rows = _build_task_list_rows(active_repository.list_tasks())
        return templates.TemplateResponse(
            request=request,
            name="bid_check_tasks.html",
            context={
                "task_rows": task_rows,
                "task_count": len(task_rows),
            },
        )

    @application.get(
        "/bid-check/tasks/{task_id}",
        response_class=HTMLResponse,
    )
    def bid_check_task_page(request: Request, task_id: str):
        task = active_repository.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail="标书检查任务不存在。",
            )
        bid_document = _load_bid_document_for_page(task)
        result = task.result if isinstance(task.result, dict) else {}
        file_requirement_data = _load_file_requirement_review_for_page(task)
        template_comparisons = build_template_comparisons(
            result.get("templates", []),
            bid_document.get("sections", []) if bid_document else [],
        )
        template_comparisons = _attach_template_text_reviews(
            template_comparisons,
            result.get("review_result"),
        )
        template_comparisons = _attach_attachment_reviews(
            template_comparisons,
            result.get("review_result"),
        )
        return templates.TemplateResponse(
            request=request,
            name="bid_check_task.html",
            context={
                "task": task,
                "evaluation_rules": _load_evaluation_rules_for_page(task),
                "evaluation_summary": load_evaluation_summary(task),
                "bid_document": bid_document,
                "template_comparisons": template_comparisons,
                "file_requirement_reviews": file_requirement_data["reviews"],
                "file_requirement_original_file": file_requirement_data["original_file"],
                "file_requirement_stats": file_requirement_data["stats"],
                "failed_stage_label": STAGE_LABELS.get(task.failed_stage),
            },
        )

    @application.post("/api/bid-check/tasks", status_code=202)
    async def create_bid_check_task(
        background_tasks: BackgroundTasks,
        tender_file: UploadFile = File(...),
        bid_file: UploadFile = File(...),
        check_mode: CheckModeInput = Form(...),
    ):
        if check_mode not in {"compliance", "evaluation", "full"}:
            raise HTTPException(
                status_code=409,
                detail="该校验方式正在开发中。",
            )

        uploads = (tender_file, bid_file)
        if any(
            Path(upload.filename or "").suffix.lower() != ".docx" for upload in uploads
        ):
            raise HTTPException(
                status_code=400,
                detail="当前仅支持 .docx 文件。",
            )

        tender_content = await tender_file.read()
        bid_content = await bid_file.read()
        if not tender_content or not bid_content:
            raise HTTPException(status_code=400, detail="上传文件不能为空。")

        task_id = str(uuid.uuid4())
        task_dir = active_settings.tasks_dir / task_id
        tender_path = task_dir / "tender.docx"
        bid_path = task_dir / "bid.docx"
        try:
            task_dir.mkdir(parents=True, exist_ok=False)
            tender_path.write_bytes(tender_content)
            bid_path.write_bytes(bid_content)
            task = active_repository.create(
                task_id=task_id,
                tender_file=FileMetadata(
                    filename=Path(tender_file.filename or "tender.docx").name,
                    size=len(tender_content),
                    storage_path=str(tender_path),
                ),
                bid_file=FileMetadata(
                    filename=Path(bid_file.filename or "bid.docx").name,
                    size=len(bid_content),
                    storage_path=str(bid_path),
                ),
                check_mode=check_mode,
            )
        except Exception as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(
                status_code=500,
                detail="创建任务失败，请稍后重试。",
            ) from exc

        background_tasks.add_task(active_workflow.run, task_id)
        return task.to_dict()

    @application.get("/api/bid-check/tasks/{task_id}")
    def get_bid_check_task(task_id: str):
        task = active_repository.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail="标书检查任务不存在。",
            )
        payload = task.to_dict()
        file_requirement_data = _load_file_requirement_review_for_page(task)
        if (
            file_requirement_data["source"] == "artifact"
            or file_requirement_data["reviews"]
        ):
            review_result = payload.get("review_result")
            review_result = dict(review_result) if isinstance(review_result, dict) else {}
            review_result["file_requirement_reviews"] = file_requirement_data["reviews"]
            review_result["file_requirement_stats"] = file_requirement_data["stats"]
            review_result["file_requirement_original_file"] = file_requirement_data[
                "original_file"
            ]
            payload["review_result"] = review_result
        subjective_scores = _load_subjective_scores_for_page(task)
        if subjective_scores is not None:
            payload["subjective_scores"] = subjective_scores
        return payload

    @application.post(
        "/api/bid-check/tasks/{task_id}/subjective-score",
        status_code=202,
    )
    def run_subjective_score(
        task_id: str,
        background_tasks: BackgroundTasks,
    ):
        task = active_repository.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail="标书检查任务不存在。",
            )
        if task.check_mode != "evaluation":
            raise HTTPException(
                status_code=409,
                detail="主观评分仅支持评标任务。",
            )
        background_tasks.add_task(active_workflow.run_subjective, task_id)
        return {"task_id": task_id, "status": "accepted"}

    @application.delete("/api/bid-check/tasks/{task_id}")
    def delete_bid_check_task(task_id: str):
        task = active_repository.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail="标书检查任务不存在。",
            )
        if task.status in {"pending", "running"}:
            raise HTTPException(
                status_code=409,
                detail="任务正在执行，无法删除，请稍后重试。",
            )

        tasks_root = active_settings.tasks_dir.resolve()
        task_dir_path = active_settings.tasks_dir / task_id
        if task_dir_path.is_symlink():
            logger.error("task.delete.invalid_path task_id=%s", task_id)
            raise HTTPException(
                status_code=500,
                detail="任务产出目录不安全，未执行删除。",
            )
        task_dir = task_dir_path.resolve()
        if task_dir.parent != tasks_root:
            logger.error("task.delete.invalid_path task_id=%s", task_id)
            raise HTTPException(
                status_code=500,
                detail="任务产出目录不安全，未执行删除。",
            )
        try:
            if task_dir.exists():
                if not task_dir.is_dir():
                    raise OSError("任务产出路径不是目录")
                shutil.rmtree(task_dir)
            active_repository.delete_task(task_id)
        except OSError as exc:
            logger.exception(
                "task.delete.filesystem_error task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="任务产出数据删除失败，数据库记录未删除。",
            ) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="标书检查任务不存在。",
            ) from exc
        return {"task_id": task_id, "deleted": True}

    return application
