from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    compliance_max_batches: int = 8


def load_settings(base_dir: Path | None = None) -> Settings:
    project_dir = base_dir or Path(__file__).resolve().parent.parent
    data_dir = Path(os.getenv("APP_DATA_DIR", project_dir / "data")).expanduser()
    database_path = Path(
        os.getenv("APP_DATABASE_PATH", data_dir / "bid_check.db")
    ).expanduser()
    tasks_dir = Path(os.getenv("APP_TASKS_DIR", data_dir / "tasks")).expanduser()
    return Settings(
        project_dir=project_dir,
        data_dir=data_dir,
        database_path=database_path,
        tasks_dir=tasks_dir,
        mock_delay_seconds=float(os.getenv("MOCK_DELAY_SECONDS", "0.35")),
        mineru_command=os.getenv("MINERU_COMMAND") or None,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        compliance_max_batches=int(os.getenv("COMPLIANCE_MAX_BATCHES", "8")),
    )
