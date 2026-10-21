from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Settings
from app.mineru_connection import MinerUConnection


class MinerUSettingsRepository:
    def __init__(self, database_path: Path, defaults: Settings) -> None:
        self._database_path = Path(database_path).expanduser()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._defaults = {
            "url": defaults.mineru_url or "",
            "gpustack_model": defaults.mineru_gpustack_model,
            "api_key": defaults.mineru_api_key or "",
        }
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS application_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self._database_path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _stored(connection: sqlite3.Connection) -> dict | None:
        row = connection.execute(
            "SELECT value FROM application_settings WHERE key = ?", ("mineru",)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get(self) -> MinerUConnection:
        with self._connect() as connection:
            stored = self._stored(connection) or {}
        return self._effective(stored)

    def _effective(self, stored: dict) -> MinerUConnection:
        return MinerUConnection(**{
            key: self._defaults[key] if stored.get(key) is None else stored[key]
            for key in self._defaults
        })

    def public_settings(self) -> dict:
        with self._connect() as connection:
            stored = self._stored(connection)
        current = self._effective(stored or {})
        return {
            "url": current.url,
            "gpustack_model": current.gpustack_model,
            "has_api_key": bool(current.api_key),
            "is_overridden": stored is not None,
        }

    def draft(self, *, url: str, gpustack_model: str = "mineru4", api_key: str = "") -> MinerUConnection:
        if not url.strip():
            raise ValueError("请填写 MinerU 服务地址。")
        return MinerUConnection(
            url=url, gpustack_model=gpustack_model,
            api_key=api_key.strip() or self.get().api_key,
        )

    def save(self, *, url: str, gpustack_model: str, api_key: str = "") -> None:
        if not url.strip():
            raise ValueError("请填写 MinerU 服务地址。")
        with self._connect() as connection:
            # Read and retain an existing key in the same write transaction.
            connection.execute("BEGIN IMMEDIATE")
            stored = self._stored(connection) or {}
            key_override = api_key.strip() or stored.get("api_key")
            current = MinerUConnection(
                url=url, gpustack_model=gpustack_model,
                api_key=key_override or self._defaults["api_key"],
            )
            value = json.dumps({
                "url": current.url, "gpustack_model": current.gpustack_model,
                "api_key": key_override,
            })
            connection.execute("""
                INSERT INTO application_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """, ("mineru", value))

    def reset(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM application_settings WHERE key = ?", ("mineru",))
