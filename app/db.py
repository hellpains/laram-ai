"""Хранилище звонков в SQLite."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import settings

# Статусы обработки по порядку
STATUSES = ["queued", "waiting_record", "transcribing", "summarizing", "crm", "done", "error"]

# Колонки, добавленные после первой версии: дописываются в существующую базу при старте
MIGRATIONS = {
    "manager_id": "TEXT",
    "crm_entity_type": "TEXT",
    "crm_entity_id": "TEXT",
    "call_started_at": "TEXT",
    "wait_started_at": "TEXT",
    "stt_provider": "TEXT",
}
FINAL = {"done", "error"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    filename TEXT,
    audio_path TEXT,
    recording_url TEXT,
    external_id TEXT,
    phone TEXT,
    deal_id TEXT,
    duration_s REAL,
    transcript_json TEXT,
    summary_json TEXT,
    crm_status TEXT,
    crm_message TEXT,
    error TEXT,
    manager_id TEXT,
    crm_entity_type TEXT,
    crm_entity_id TEXT,
    call_started_at TEXT,
    wait_started_at TEXT,
    stt_provider TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_created ON calls(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_external ON calls(external_id) WHERE external_id IS NOT NULL;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(calls)")}
        for column, kind in MIGRATIONS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE calls ADD COLUMN {column} {kind}")


def create_call(source: str, **fields: Any) -> str:
    call_id = uuid.uuid4().hex[:12]
    now = _now()
    row = {"id": call_id, "created_at": now, "updated_at": now, "status": "queued", "source": source, **fields}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with connect() as conn:
        conn.execute(f"INSERT INTO calls ({cols}) VALUES ({marks})", list(row.values()))
    return call_id


def find_by_external_id(external_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM calls WHERE external_id = ?", (external_id,)).fetchone()
    return row["id"] if row else None


def update(call_id: str, **fields: Any) -> None:
    for key in ("transcript", "summary"):
        if key in fields:
            value = fields.pop(key)
            fields[f"{key}_json"] = json.dumps(value, ensure_ascii=False) if value is not None else None
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE calls SET {sets} WHERE id = ?", [*fields.values(), call_id])


def get(call_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    return _row(row) if row else None


def list_calls(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        item = _row(r)
        item.pop("transcript", None)
        out.append(item)
    return out


def unfinished() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM calls WHERE status NOT IN ('done', 'error') ORDER BY created_at"
        ).fetchall()
    return [r["id"] for r in rows]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("transcript", "summary"):
        raw = item.pop(f"{key}_json", None)
        item[key] = json.loads(raw) if raw else None
    return item
