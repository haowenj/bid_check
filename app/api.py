from __future__ import annotations

import logging
import shutil
import uuid
import zipfile
from contextlib import asynccontextmanager
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Literal

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

from app.compliance_extraction import (
    ComplianceExtractionError,
    DeterministicComplianceLLM,
    JsonDocumentCache,
    JsonRequirementCache,
    MinerUDocumentParser,
    OpenAICompatibleLLM,
    extract_tender_compliance_objects,
)
from app.config import Settings, load_settings
from app.mock_services import (
    empty_tender_extraction_result,
    parse_bid_document,
    run_compliance_review,
)
from app.models import FileMetadata
from app.repository import BidCheckRepository
from app.workflow import BidCheckServices, BidCheckWorkflow

CheckModeInput = Literal["compliance", "evaluation", "full"]
logger = logging.getLogger(__name__)
STAGE_LABELS = {
    "requirements": "提取招标文件检查对象",
    "bid_parse": "解析投标文件",
    "review": "执行合规性检查",
}


def build_default_workflow(
    settings: Settings,
    repository: BidCheckRepository,
) -> BidCheckWorkflow:
    parser = MinerUDocumentParser(
        settings.mineru_command,
        mineru_url=settings.mineru_url,
        mineru_api_key=settings.mineru_api_key,
        mineru_backend=settings.mineru_backend,
        mineru_server_url=settings.mineru_server_url,
        timeout_seconds=settings.mineru_timeout_seconds,
        poll_interval_seconds=settings.mineru_poll_interval_seconds,
        allow_docx_fallback=settings.allow_docx_fallback,
    )
    cache = JsonRequirementCache(settings.data_dir / "compliance_cache")
    parser_cache = JsonDocumentCache(settings.data_dir / "mineru_cache")
    if settings.llm_api_key:
        llm = OpenAICompatibleLLM(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    else:
        llm = DeterministicComplianceLLM()

    def extract_requirements(file_metadata: FileMetadata):
        try:
            return extract_tender_compliance_objects(
                file_metadata,
                parser=parser,
                llm=llm,
                cache=cache,
                parser_cache=parser_cache,
                max_batches=settings.compliance_max_batches,
            )
        except ComplianceExtractionError:
            # Only an explicitly enabled development/test fallback may keep
            # historical byte-stub fixtures runnable.  Normal business
            # settings always propagate MinerU errors to the failed task.
            if (
                settings.allow_docx_fallback
                and parser.parser_name == "docx_fallback"
                and not zipfile.is_zipfile(
                    file_metadata.storage_path
                )
            ):
                logger.warning(
                    "tender_objects.compatibility_fallback file=%s parser=docx_fallback reason=non_docx_fixture",
                    file_metadata.filename,
                )
                return deepcopy(empty_tender_extraction_result())
            raise

    services = BidCheckServices(
        extract=extract_requirements,
        parse=partial(
            parse_bid_document,
            delay_seconds=settings.mock_delay_seconds,
        ),
        review=run_compliance_review,
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
        return templates.TemplateResponse(
            request=request,
            name="bid_check_task.html",
            context={
                "task": task,
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
