from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.mineru_connection import check_mineru_health, discover_mineru_endpoint

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _render(request: Request, *, error: str | None = None, status_code: int = 200):
    response = templates.TemplateResponse(
        request,
        "settings.html",
        {
            "mineru": request.app.state.mineru_settings.public_settings(),
            "gpustack_base_url": request.app.state.settings.llm_base_url,
            "gpustack_key_ready": bool(request.app.state.settings.llm_api_key),
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        source = urlsplit(origin)
        if source.scheme != request.url.scheme or source.netloc != request.url.netloc:
            raise HTTPException(status_code=403, detail="不允许跨站修改设置。")


@router.get("/settings")
def settings_page(request: Request):
    return _render(request)


@router.post("/settings/mineru")
def save_mineru_settings(
    request: Request,
    url: str = Form(...),
    gpustack_model: str = Form("mineru4"),
    api_key: str = Form(""),
):
    _check_origin(request)
    try:
        request.app.state.mineru_settings.save(
            url=url, gpustack_model=gpustack_model, api_key=api_key
        )
    except ValueError as exc:
        return _render(request, error=str(exc), status_code=400)
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/mineru/probe")
def probe_mineru(request: Request, gpustack_model: str = Form("")):
    _check_origin(request)
    try:
        settings = request.app.state.settings
        base_url = settings.llm_base_url.strip()
        if not base_url:
            raise ValueError("未配置 LLM_BASE_URL，请配置后再探测，或手动填写 MinerU 地址。")
        with httpx.Client() as client:
            url = discover_mineru_endpoint(
                base_url, gpustack_model, settings.llm_api_key or "", client
            )
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "url": url, "message": "已探测到实例并填入地址；点击保存后生效。"}


@router.post("/settings/mineru/test")
def test_mineru(
    request: Request, url: str = Form(""), api_key: str = Form(""),
    gpustack_model: str = Form("mineru4"),
):
    _check_origin(request)
    try:
        draft = request.app.state.mineru_settings.draft(
            url=url, api_key=api_key, gpustack_model=gpustack_model
        )
        with httpx.Client() as client:
            version = check_mineru_health(draft, client)
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "version": version, "message": f"连接正常，MinerU {version} 健康检查通过。"}


@router.post("/settings/mineru/reset")
def reset_mineru_settings(request: Request):
    _check_origin(request)
    request.app.state.mineru_settings.reset()
    return RedirectResponse("/settings?reset=1", status_code=303)
