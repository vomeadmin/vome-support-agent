"""
database.py

PostgreSQL-backed thread map, replacing thread_map.json.
Uses SQLAlchemy Core for simple table operations.
"""

import json
import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_engine = None
_metadata = MetaData()

ticket_threads = Table(
    "ticket_threads",
    _metadata,
    Column("thread_ts", String, primary_key=True),
    Column("ticket_id", String, nullable=False),
    Column("ticket_number", String, default=""),
    Column("subject", String, default=""),
    Column("channel", String, default=""),
    Column("status", String, default="open"),
    Column("clickup_task_id", String, nullable=True),
    Column("classification", JSONB, default={}),
    Column("crm", JSONB, default={}),
    Column("pending_send", Text, nullable=True),
    Column("close_after_send", String, default="false"),
    Column("created_at", DateTime, default=datetime.now(timezone.utc)),
    Column("updated_at", DateTime, default=datetime.now(timezone.utc)),
)


def _get_engine():
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL not set — cannot connect to PostgreSQL"
            )
        url = DATABASE_URL
        # Railway sometimes uses postgres:// which SQLAlchemy 2.x rejects
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def init_db():
    """Create the ticket_threads table if it doesn't exist."""
    engine = _get_engine()
    _metadata.create_all(engine)
    print("Database initialized — ticket_threads table ready")


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------

def save_thread(
    thread_ts: str,
    ticket_id: str,
    ticket_number: str = "",
    subject: str = "",
    channel: str = "",
    clickup_task_id: str | None = None,
    classification: dict | None = None,
    crm: dict | None = None,
):
    """Insert or update a thread mapping."""
    engine = _get_engine()
    now = datetime.now(timezone.utc)
    row = {
        "thread_ts": thread_ts,
        "ticket_id": ticket_id,
        "ticket_number": ticket_number,
        "subject": subject,
        "channel": channel,
        "status": "open",
        "clickup_task_id": clickup_task_id,
        "classification": json.dumps(classification or {}),
        "crm": json.dumps(crm or {}),
        "pending_send": None,
        "close_after_send": "false",
        "created_at": now,
        "updated_at": now,
    }
    with engine.begin() as conn:
        # Upsert: try insert, on conflict update
        conn.execute(
            text("""
                INSERT INTO ticket_threads
                    (thread_ts, ticket_id, ticket_number, subject,
                     channel, status, clickup_task_id,
                     classification, crm, pending_send,
                     close_after_send, created_at, updated_at)
                VALUES
                    (:thread_ts, :ticket_id, :ticket_number, :subject,
                     :channel, :status, :clickup_task_id,
                     :classification::jsonb, :crm::jsonb, :pending_send,
                     :close_after_send, :created_at, :updated_at)
                ON CONFLICT (thread_ts) DO UPDATE SET
                    ticket_id = EXCLUDED.ticket_id,
                    ticket_number = EXCLUDED.ticket_number,
                    subject = EXCLUDED.subject,
                    channel = EXCLUDED.channel,
                    clickup_task_id = EXCLUDED.clickup_task_id,
                    classification = EXCLUDED.classification,
                    crm = EXCLUDED.crm,
                    updated_at = EXCLUDED.updated_at
            """),
            row,
        )
    print(
        f"Thread saved: {thread_ts} → ticket {ticket_id}"
    )


def get_thread(thread_ts: str) -> dict | None:
    """Fetch a single thread by its Slack thread_ts. Returns dict or None."""
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM ticket_threads WHERE thread_ts = :ts"),
            {"ts": thread_ts},
        )
        row = result.mappings().first()
        if not row:
            return None
        return _row_to_dict(row)


def update_thread(thread_ts: str, **kwargs):
    """Update specific fields on a thread entry."""
    if not kwargs:
        return
    engine = _get_engine()

    # Serialize dicts to JSON strings for JSONB columns
    for key in ("classification", "crm"):
        if key in kwargs and isinstance(kwargs[key], dict):
            kwargs[key] = json.dumps(kwargs[key])

    kwargs["updated_at"] = datetime.now(timezone.utc)

    set_clauses = ", ".join(f"{k} = :{k}" for k in kwargs)
    kwargs["ts"] = thread_ts

    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE ticket_threads SET {set_clauses} "
                f"WHERE thread_ts = :ts"
            ),
            kwargs,
        )


def get_thread_by_ticket_id(ticket_id: str) -> tuple[str, dict] | None:
    """Find a thread by Zoho ticket ID. Returns (thread_ts, data) or None."""
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT * FROM ticket_threads "
                "WHERE ticket_id = :tid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"tid": str(ticket_id)},
        )
        row = result.mappings().first()
        if not row:
            return None
        d = _row_to_dict(row)
        return (d["thread_ts"], d)


def get_open_threads() -> dict:
    """Return all non-closed threads as {thread_ts: data}."""
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT * FROM ticket_threads "
                "WHERE status NOT IN ('handled', 'closed')"
            )
        )
        return {
            row["thread_ts"]: _row_to_dict(row)
            for row in result.mappings()
        }


def get_threads_by_date(date_str: str) -> dict:
    """Return all threads created on a specific date (YYYY-MM-DD)."""
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT * FROM ticket_threads "
                "WHERE created_at::date = :d"
            ),
            {"d": date_str},
        )
        return {
            row["thread_ts"]: _row_to_dict(row)
            for row in result.mappings()
        }


def get_all_threads() -> dict:
    """Return entire thread map as {thread_ts: data}."""
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM ticket_threads")
        )
        return {
            row["thread_ts"]: _row_to_dict(row)
            for row in result.mappings()
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    """Convert a DB row to the same dict shape thread_map.json used."""
    classification = row["classification"]
    if isinstance(classification, str):
        classification = json.loads(classification)

    crm = row["crm"]
    if isinstance(crm, str):
        crm = json.loads(crm)

    return {
        "thread_ts": row["thread_ts"],
        "ticket_id": row["ticket_id"],
        "ticket_number": row["ticket_number"] or "",
        "subject": row["subject"] or "",
        "channel": row["channel"] or "",
        "status": row["status"] or "open",
        "clickup_task_id": row["clickup_task_id"],
        "classification": classification or {},
        "crm": crm or {},
        "pending_send": row["pending_send"],
        "close_after_send": row["close_after_send"] == "true",
        "date": (
            row["created_at"].strftime("%Y-%m-%d")
            if row["created_at"] else ""
        ),
    }
