from __future__ import annotations

from app.config import load_settings


def test_load_settings_reads_project_env_before_process_environment(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=file-key\n"
        "LLM_BASE_URL=https://file.example/v1\n"
        "LLM_MODEL=qwen3.8-27b\n"
        "LLM_ENABLE_THINKING=false\n",
        encoding="utf-8",
    )
    for key in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_ENABLE_THINKING",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = load_settings(tmp_path)

    assert settings.llm_api_key == "file-key"
    assert settings.llm_base_url == "https://file.example/v1"
    assert settings.llm_model == "qwen3.8-27b"
    assert settings.llm_enable_thinking is False

    monkeypatch.setenv("LLM_MODEL", "process-model")
    assert load_settings(tmp_path).llm_model == "qwen3.8-27b"


def test_load_settings_reads_existing_pdf_trans_mineru_configuration(tmp_path):
    (tmp_path / ".env").write_text(
        "PDF_TRANS_MINERU_URL=http://mineru.example:7100\n"
        "PDF_TRANS_MINERU_BACKEND=hybrid-http-client\n"
        "PDF_TRANS_MINERU_SERVER_URL=http://model.example:8000\n"
        "MINERU_API_KEY=service-key\n",
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.mineru_url == "http://mineru.example:7100"
    assert settings.mineru_backend == "hybrid-http-client"
    assert settings.mineru_server_url == "http://model.example:8000"
    assert settings.mineru_api_key == "service-key"
    assert settings.allow_docx_fallback is False
