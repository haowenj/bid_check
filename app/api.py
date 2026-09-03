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

from app.bid_document import (
    MinerUBidDocumentParser,
    parse_bid_document as clean_bid_document,
)
from app.attachment_review import (
    DeterministicAttachmentReviewLLM,
    OpenAICompatibleAttachmentReviewLLM,
    run_compliance_review_with_attachments,
)
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
from app.models import BidCheckTask, FileMetadata
from app.repository import BidCheckRepository
from app.template_matching import build_template_comparisons
from app.template_text_review import (
    DeterministicTemplateTextReviewLLM,
)
from app.workflow import BidCheckServices, BidCheckWorkflow

CheckModeInput = Literal["compliance", "evaluation", "full"]
logger = logging.getLogger(__name__)
STAGE_LABELS = {
    "requirements": "提取招标文件检查对象",
    "bid_parse": "解析投标文件",
    "review": "执行合规性检查",
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
    else:
        llm = DeterministicComplianceLLM()
        template_review_llm = DeterministicTemplateTextReviewLLM()
        attachment_review_llm = DeterministicAttachmentReviewLLM()

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
        ),
        extract_with_recorder=extract_requirements,
        review_with_recorder=partial(
            run_compliance_review_with_attachments,
            template_review_llm=template_review_llm,
            attachment_review_llm=attachment_review_llm,
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
                "bid_document": bid_document,
                "template_comparisons": template_comparisons,
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
        if check_mode != "compliance":
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
        return task.to_dict()

    return application
