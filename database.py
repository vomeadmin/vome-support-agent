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

processed_events = Table(
    "processed_events",
    _metadata,
    Column("event_id", String, primary_key=True),
    Column(
        "processed_at", DateTime,
        default=datetime.now(timezone.utc),
    ),
)

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
    if not DATABASE_URL:
        print(
            "WARNING: DATABASE_URL not set — database features disabled. "
            "Set DATABASE_URL to enable thread persistence."
        )
        return
    try:
        engine = _get_engine()
        _metadata.create_all(engine)
        print("Database initialized — ticket_threads table ready")
    except Exception as e:
        print(f"WARNING: Database connection failed — {e}")
        print("Thread persistence will not work until database is available.")


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
    if not DATABASE_URL:
        print("[DB] save_thread: DATABASE_URL not set — skipping")
        return
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
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO ticket_threads
                        (thread_ts, ticket_id, ticket_number, subject,
                         channel, status, clickup_task_id,
                         classification, crm, pending_send,
                         close_after_send, created_at, updated_at)
                    VALUES
                        (:thread_ts, :ticket_id, :ticket_number,
                         :subject, :channel, :status,
                         :clickup_task_id,
                         CAST(:classification AS jsonb),
                         CAST(:crm AS jsonb),
                         :pending_send, :close_after_send,
                         :created_at, :updated_at)
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
        print(f"[DB] Thread saved: {thread_ts} → ticket {ticket_id}")
    except Exception as e:
        print(f"[DB ERROR] Failed to save thread {thread_ts}: {e}")
        raise


def get_thread(thread_ts: str) -> dict | None:
    """Fetch a single thread by its Slack thread_ts. Returns dict or None."""
    if not DATABASE_URL:
        return None
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT * FROM ticket_threads WHERE thread_ts = :ts"
                ),
                {"ts": thread_ts},
            )
            row = result.mappings().first()
            if not row:
                return None
            return _row_to_dict(row)
    except Exception as e:
        print(f"get_thread failed: {e}")
        return None


def update_thread(thread_ts: str, **kwargs):
    """Update specific fields on a thread entry."""
    if not kwargs:
        return
    if not DATABASE_URL:
        print("[DB] update_thread: DATABASE_URL not set — skipping")
        return
    engine = _get_engine()

    # Serialize dicts to JSON strings for JSONB columns
    for key in ("classification", "crm"):
        if key in kwargs and isinstance(kwargs[key], dict):
            kwargs[key] = json.dumps(kwargs[key])

    kwargs["updated_at"] = datetime.now(timezone.utc)

    # Use CAST for JSONB columns to avoid :: conflict with SQLAlchemy
    set_parts = []
    for k in kwargs:
        if k in ("classification", "crm"):
            set_parts.append(f"{k} = CAST(:{k} AS jsonb)")
        else:
            set_parts.append(f"{k} = :{k}")
    set_clauses = ", ".join(set_parts)
    kwargs["ts"] = thread_ts

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE ticket_threads SET {set_clauses} "
                    f"WHERE thread_ts = :ts"
                ),
                kwargs,
            )
        print(f"[DB] Thread updated: {thread_ts}")
    except Exception as e:
        print(f"[DB ERROR] Failed to update thread {thread_ts}: {e}")
        raise


def get_thread_by_ticket_id(ticket_id: str) -> tuple[str, dict] | None:
    """Find a thread by Zoho ticket ID. Returns (thread_ts, data) or None."""
    if not DATABASE_URL:
        return None
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
    if not DATABASE_URL:
        return {}
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
    if not DATABASE_URL:
        return {}
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT * FROM ticket_threads "
                "WHERE CAST(created_at AS date) = :d"
            ),
            {"d": date_str},
        )
        return {
            row["thread_ts"]: _row_to_dict(row)
            for row in result.mappings()
        }


def get_all_threads() -> dict:
    """Return entire thread map as {thread_ts: data}."""
    if not DATABASE_URL:
        return {}
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
# Event deduplication
# ---------------------------------------------------------------------------

def is_event_processed(event_id: str) -> bool:
    """Check if a Slack event was already processed. Returns True if duplicate."""
    if not DATABASE_URL or not event_id:
        return False
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT 1 FROM processed_events "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
            return result.first() is not None
    except Exception as e:
        print(f"[DB] is_event_processed check failed: {e}")
        return False


def mark_event_processed(event_id: str):
    """Record that a Slack event has been processed."""
    if not DATABASE_URL or not event_id:
        return
    try:
        engine = _get_engine()
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO processed_events (event_id, processed_at) "
                    "VALUES (:eid, :ts) "
                    "ON CONFLICT (event_id) DO NOTHING"
                ),
                {"eid": event_id, "ts": now},
            )
            # Clean up events older than 24 hours
            conn.execute(
                text(
                    "DELETE FROM processed_events "
                    "WHERE processed_at < NOW() - INTERVAL '24 hours'"
                )
            )
    except Exception as e:
        print(f"[DB] mark_event_processed failed: {e}")


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
