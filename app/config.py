from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    data_dir: Path
    database_path: Path
    tasks_dir: Path
    mock_delay_seconds: float = 0.35
    mineru_command: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_enable_thinking: bool = False
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 90.0
    compliance_max_batches: int = 8


def _project_env(project_dir: Path) -> dict[str, str]:
    env_path = project_dir / ".env"
    if not env_path.is_file():
        return {}
    return {
        key: value
        for key, value in dotenv_values(env_path).items()
        if value is not None
    }


def _env_value(
    project_env: dict[str, str],
    name: str,
    default: str | None = None,
) -> str | None:
    if name in project_env:
        return project_env[name]
    return os.getenv(name, default)


def _env_bool(
    project_env: dict[str, str],
    name: str,
    default: bool,
) -> bool:
    value = _env_value(project_env, name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(base_dir: Path | None = None) -> Settings:
    project_dir = base_dir or Path(__file__).resolve().parent.parent
    project_env = _project_env(project_dir)
    data_dir = Path(
        _env_value(project_env, "APP_DATA_DIR", str(project_dir / "data"))
        or project_dir / "data"
    ).expanduser()
    database_path = Path(
        _env_value(
            project_env,
            "APP_DATABASE_PATH",
            str(data_dir / "bid_check.db"),
        )
        or data_dir / "bid_check.db"
    ).expanduser()
    tasks_dir = Path(
        _env_value(project_env, "APP_TASKS_DIR", str(data_dir / "tasks"))
        or data_dir / "tasks"
    ).expanduser()
    return Settings(
        project_dir=project_dir,
        data_dir=data_dir,
        database_path=database_path,
        tasks_dir=tasks_dir,
        mock_delay_seconds=float(
            _env_value(project_env, "MOCK_DELAY_SECONDS", "0.35") or "0.35"
        ),
        mineru_command=_env_value(project_env, "MINERU_COMMAND") or None,
        llm_api_key=_env_value(project_env, "LLM_API_KEY") or None,
        llm_base_url=(
            _env_value(project_env, "LLM_BASE_URL", "https://api.openai.com/v1")
            or "https://api.openai.com/v1"
        ),
        llm_model=_env_value(project_env, "LLM_MODEL", "gpt-4o-mini")
        or "gpt-4o-mini",
        llm_enable_thinking=_env_bool(project_env, "LLM_ENABLE_THINKING", False),
        llm_max_tokens=int(
            _env_value(project_env, "LLM_MAX_TOKENS", "4096") or "4096"
        ),
        llm_timeout_seconds=float(
            _env_value(project_env, "LLM_TIMEOUT_SECONDS", "90") or "90"
        ),
        compliance_max_batches=int(
            _env_value(project_env, "COMPLIANCE_MAX_BATCHES", "8") or "8"
        ),
    )
