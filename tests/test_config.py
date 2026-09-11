from __future__ import annotations

from app.config import load_settings


def test_load_settings_reads_project_and_mineru_env_before_process_environment(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=file-key\n"
        "LLM_BASE_URL=https://file.example/v1\n"
        "LLM_MODEL=qwen3.8-27b\n"
        "LLM_PROVIDER=vllm\n"
        "LLM_ENABLE_THINKING=false\n"
        "MINERU_URL=https://mineru.file.example\n"
        "MINERU_BACKEND=hybrid-http-client\n"
        "MINERU_SERVER_URL=https://mineru-server.file.example\n",
        encoding="utf-8",
    )
    for key in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_PROVIDER",
        "LLM_ENABLE_THINKING",
        "MINERU_URL",
        "MINERU_BACKEND",
        "MINERU_SERVER_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = load_settings(tmp_path)

    assert settings.llm_api_key == "file-key"
    assert settings.llm_base_url == "https://file.example/v1"
    assert settings.llm_model == "qwen3.8-27b"
    assert settings.llm_provider == "vllm"
    assert settings.llm_enable_thinking is False
    assert settings.mineru_url == "https://mineru.file.example"
    assert settings.mineru_backend == "hybrid-http-client"
    assert settings.mineru_server_url == "https://mineru-server.file.example"

    monkeypatch.setenv("LLM_MODEL", "process-model")
    assert load_settings(tmp_path).llm_model == "qwen3.8-27b"
    monkeypatch.setenv("MINERU_URL", "https://mineru.process.example")
    assert load_settings(tmp_path).mineru_url == "https://mineru.file.example"


def test_load_settings_does_not_invent_mineru_endpoint_when_unconfigured(tmp_path):
    (tmp_path / ".env").write_text(
        "LLM_BASE_URL=https://llm.example/v1\n",
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.mineru_url is None
    assert settings.mineru_backend == "hybrid-engine"
    assert settings.mineru_server_url is None
    assert settings.mineru_api_key is None
    assert settings.llm_provider == "dashscope"


def test_load_settings_uses_default_and_env_override_for_llm_concurrency(tmp_path, monkeypatch):
    default_settings = load_settings(tmp_path)
    assert default_settings.llm_max_concurrency == 5

    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    assert load_settings(tmp_path).llm_max_concurrency == 2
