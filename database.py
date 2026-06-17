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
    Integer,
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
    Column("pending_draft", Text, nullable=True),
    Column("close_after_send", String, default="false"),
    Column("created_at", DateTime, default=datetime.now(timezone.utc)),
    Column("updated_at", DateTime, default=datetime.now(timezone.utc)),
)


kb_deflection_log = Table(
    "kb_deflection_log",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("issue_fingerprint", String, nullable=False, index=True),
    Column("org_id", String, nullable=True),
    Column("user_email", String, nullable=True),
    Column("created_at", DateTime, default=datetime.now(timezone.utc)),
)


# Widget intake outcomes -- one row per conversation that reached a
# terminal state. This is the raw data behind the "how is Vic doing"
# overview: how many chats Vic resolved on its own vs. escalated to
# the team as a ticket.
#   outcome:         "resolved" (no ticket) | "escalated" (ticket filed)
#   resolution_type: kb_deflection | info_answered | out_of_scope_redirect
#                    | auth_self_serve | ticket_created
vic_resolution_log = Table(
    "vic_resolution_log",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("outcome", String, nullable=False, index=True),
    Column("resolution_type", String, nullable=True),
    Column("issue_fingerprint", String, nullable=True),
    Column("org_id", String, nullable=True),
    Column("user_email", String, nullable=True),
    Column("user_role", String, nullable=True),
    Column("ticket_id", String, nullable=True),
    Column("created_at", DateTime, default=datetime.now(timezone.utc), index=True),
)

# Ticket analysis tracking -- records which tickets have been
# processed by the knowledge book builder
analyzed_tickets = Table(
    "analyzed_tickets",
    _metadata,
    Column("ticket_id", String, primary_key=True),
    Column("ticket_number", String, default=""),
    Column("subject", String, default=""),
    Column("category", String, default=""),
    Column("module", String, default=""),
    Column("language", String, default="en"),
    Column("turn_count", Integer, default=0),
    Column("has_sam_response", String, default="false"),
    Column("analysis", JSONB, default={}),
    Column("analyzed_at", DateTime, default=datetime.now(timezone.utc)),
)

# Knowledge book sections -- versioned training content
# generated from ticket analysis
knowledge_sections = Table(
    "knowledge_sections",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("section_key", String, nullable=False, index=True),
    Column("title", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("version", Integer, default=1),
    Column("ticket_count", Integer, default=0),
    Column("last_ticket_date", String, default=""),
    Column("is_current", String, default="true"),
    Column("created_at", DateTime, default=datetime.now(timezone.utc)),
    Column("updated_at", DateTime, default=datetime.now(timezone.utc)),
)


# Language-aware FTS expression for the generated search_vector column.
# Uses 'french' stemming for FR articles and 'english' for everything else
# (the language column is set per-article by the nightly sync). Stemming
# lets "volunteers" match "volunteer", "cancellation" match "cancel", etc.
# The (regconfig, text) form of to_tsvector is IMMUTABLE, so it's valid
# inside a STORED generated column; the (text, text) form is not.
_KB_SEARCH_VECTOR_EXPR = (
    "setweight(to_tsvector("
    "  CASE WHEN language = 'fr' THEN 'french'::regconfig "
    "       ELSE 'english'::regconfig END, coalesce(title, '')), 'A') ||"
    "setweight(to_tsvector("
    "  CASE WHEN language = 'fr' THEN 'french'::regconfig "
    "       ELSE 'english'::regconfig END, coalesce(body, '')), 'B')"
)


def _kb_articles_create_sql() -> str:
    """Raw SQL for the kb_articles table.

    Uses a STORED generated tsvector column so FTS is automatic on UPSERT
    without a trigger. The vector is built with language-aware stemming
    (see _KB_SEARCH_VECTOR_EXPR) so queries match word variants, not just
    exact tokens.
    """
    return (
        "CREATE TABLE IF NOT EXISTS kb_articles ("
        "  zoho_article_id VARCHAR PRIMARY KEY,"
        "  title TEXT NOT NULL,"
        "  body TEXT NOT NULL DEFAULT '',"
        "  url TEXT NOT NULL DEFAULT '',"
        "  permalink TEXT NOT NULL DEFAULT '',"
        "  category TEXT NOT NULL DEFAULT '',"
        "  language VARCHAR NOT NULL DEFAULT 'en',"
        "  status VARCHAR NOT NULL DEFAULT '',"
        "  modified_time TIMESTAMP NULL,"
        "  created_time TIMESTAMP NULL,"
        "  synced_at TIMESTAMP NOT NULL DEFAULT NOW(),"
        "  search_vector tsvector GENERATED ALWAYS AS ("
        f"    {_KB_SEARCH_VECTOR_EXPR}"
        "  ) STORED"
        ")"
    )


def _migrate_kb_search_vector(conn) -> None:
    """Rebuild search_vector with language-aware stemming if it's still on
    the old 'simple' config.

    The prod table already exists, so CREATE TABLE IF NOT EXISTS won't pick
    up the new generation expression. This detects the legacy 'simple'
    definition and recreates the column (instant on a few hundred rows).
    Idempotent: once rebuilt, the expression no longer contains 'simple'
    and this is a no-op.
    """
    expr = conn.execute(text(
        "SELECT generation_expression FROM information_schema.columns "
        "WHERE table_name = 'kb_articles' AND column_name = 'search_vector'"
    )).scalar()
    # Only rebuild the legacy 'simple' vector. If the column is missing
    # (fresh DB) the CREATE TABLE above already used the new expression.
    if not expr or "simple" not in expr.lower():
        return
    print("[DB] Migrating kb_articles.search_vector to language-aware stemming")
    conn.execute(text("DROP INDEX IF EXISTS kb_articles_search_idx"))
    conn.execute(text(
        "ALTER TABLE kb_articles DROP COLUMN IF EXISTS search_vector"
    ))
    conn.execute(text(
        "ALTER TABLE kb_articles ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({_KB_SEARCH_VECTOR_EXPR}) STORED"
    ))
    conn.execute(text(_kb_articles_index_sql()))


def _kb_articles_index_sql() -> str:
    return (
        "CREATE INDEX IF NOT EXISTS kb_articles_search_idx "
        "ON kb_articles USING GIN (search_vector)"
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
        # Create kb_articles (raw SQL because of generated tsvector column)
        with engine.begin() as conn:
            conn.execute(text(_kb_articles_create_sql()))
            conn.execute(text(_kb_articles_index_sql()))
            _migrate_kb_search_vector(conn)
        # Migrate: add columns if missing
        with engine.begin() as conn:
            migrations = [
                "ADD COLUMN IF NOT EXISTS pending_draft TEXT",
                "ADD COLUMN IF NOT EXISTS zoho_assignee_id VARCHAR",
                "ADD COLUMN IF NOT EXISTS clickup_assignee_id INTEGER",
                "ADD COLUMN IF NOT EXISTS priority_score INTEGER",
                "ADD COLUMN IF NOT EXISTS missing_info TEXT",
                "ADD COLUMN IF NOT EXISTS engineer_note TEXT",
                "ADD COLUMN IF NOT EXISTS pending_info BOOLEAN DEFAULT FALSE",
                "ADD COLUMN IF NOT EXISTS parked BOOLEAN DEFAULT FALSE",
                "ADD COLUMN IF NOT EXISTS wake_date DATE",
                "ADD COLUMN IF NOT EXISTS last_action VARCHAR",
                "ADD COLUMN IF NOT EXISTS last_action_at TIMESTAMP",
            ]
            for m in migrations:
                try:
                    conn.execute(text(
                        f"ALTER TABLE ticket_threads {m}"
                    ))
                except Exception:
                    pass
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
# KB articles (Zoho Desk article bodies, FTS-searchable)
# ---------------------------------------------------------------------------

def upsert_kb_article(article: dict) -> str:
    """Insert or update a KB article. Returns 'added', 'updated', or 'unchanged'.

    Expected dict keys: id, title, content, url, permalink, category,
    language, status, modifiedTime, createdTime.
    """
    if not DATABASE_URL:
        return "skipped"
    article_id = str(article.get("id") or "")
    if not article_id:
        return "skipped"

    engine = _get_engine()
    now = datetime.now(timezone.utc)
    body = (article.get("content") or "").strip()
    title = (article.get("title") or "").strip()
    if not title:
        return "skipped"

    params = {
        "zoho_article_id": article_id,
        "title": title,
        "body": body,
        "url": article.get("url") or "",
        "permalink": article.get("permalink") or "",
        "category": article.get("category") or "",
        "language": article.get("language") or "en",
        "status": article.get("status") or "",
        "modified_time": _parse_zoho_time(article.get("modifiedTime")),
        "created_time": _parse_zoho_time(article.get("createdTime")),
        "synced_at": now,
    }

    with engine.begin() as conn:
        existing = conn.execute(
            text(
                "SELECT modified_time FROM kb_articles "
                "WHERE zoho_article_id = :zid"
            ),
            {"zid": article_id},
        ).first()

        if existing is not None:
            if (
                existing[0] is not None
                and params["modified_time"] is not None
                and existing[0] == params["modified_time"]
            ):
                # Body unchanged -- just update synced_at to mark freshness
                conn.execute(
                    text(
                        "UPDATE kb_articles SET synced_at = :synced_at "
                        "WHERE zoho_article_id = :zid"
                    ),
                    {"synced_at": now, "zid": article_id},
                )
                return "unchanged"

            conn.execute(
                text(
                    "UPDATE kb_articles SET "
                    "  title = :title, body = :body, url = :url, "
                    "  permalink = :permalink, category = :category, "
                    "  language = :language, status = :status, "
                    "  modified_time = :modified_time, "
                    "  created_time = :created_time, "
                    "  synced_at = :synced_at "
                    "WHERE zoho_article_id = :zoho_article_id"
                ),
                params,
            )
            return "updated"

        conn.execute(
            text(
                "INSERT INTO kb_articles "
                "(zoho_article_id, title, body, url, permalink, "
                " category, language, status, modified_time, "
                " created_time, synced_at) "
                "VALUES (:zoho_article_id, :title, :body, :url, "
                " :permalink, :category, :language, :status, "
                " :modified_time, :created_time, :synced_at)"
            ),
            params,
        )
        return "added"


def delete_missing_kb_articles(seen_ids: list[str]) -> int:
    """Delete kb_articles rows whose zoho_article_id is NOT in seen_ids.

    Called after a full sync to drop articles deleted in Zoho.
    Returns the number of rows deleted.
    """
    if not DATABASE_URL or not seen_ids:
        return 0
    engine = _get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM kb_articles "
                "WHERE zoho_article_id != ALL(:ids)"
            ),
            {"ids": list(seen_ids)},
        )
        return result.rowcount or 0


def search_kb_articles_db(
    query: str,
    language: str | None = None,
    limit: int = 2,
    body_chars: int = 3000,
) -> list[dict]:
    """Postgres FTS over kb_articles. Returns ranked article dicts.

    Each dict: {id, title, body, url, permalink, category, language,
    modified_time, days_stale, score}. body is truncated to body_chars.
    """
    if not DATABASE_URL or not query or not query.strip():
        return []
    engine = _get_engine()

    # Match the tsquery config to the article language so stemming lines up
    # with how the stored vector was built (see _KB_SEARCH_VECTOR_EXPR).
    cfg = "french" if language == "fr" else "english"

    # We OR the query terms so an article matching *some* of the words
    # still surfaces -- but a pure OR lets a single common word (e.g.
    # "account", "question", "feature") pull up an off-topic article,
    # which is exactly how "Billing or account question" surfaced the
    # "Can minors volunteer?" article. To keep recall without that junk:
    #
    #   * split the plainto lexemes (plainto ANDs them with ' & ')
    #   * match on the OR of those lexemes (recall)
    #   * but require a row to match at least LEAST(2, term_count) of the
    #     lexemes -- so a one-common-word coincidence is dropped while a
    #     genuine multi-word question that hits most terms still matches
    #   * rank by how many terms matched first, then ts_rank_cd density
    #
    # plainto sanitises user input, so each split lexeme is a valid
    # single-term to_tsquery.
    sql = (
        "WITH q AS ("
        "  SELECT regexp_split_to_array("
        "           plainto_tsquery(:cfg::regconfig, :q)::text, ' & '"
        "         ) AS lexemes"
        ") "
        "SELECT a.zoho_article_id, a.title, a.body, a.url, a.permalink, "
        "       a.category, a.language, a.modified_time, "
        "       ts_rank_cd(a.search_vector, q_or.q) AS score, "
        "       ( SELECT count(*) FROM unnest(q.lexemes) lx "
        "          WHERE lx <> '' AND a.search_vector @@ lx::tsquery "
        "       ) AS match_count "
        "FROM kb_articles a, q, "
        "     to_tsquery(:cfg::regconfig, "
        "       array_to_string(q.lexemes, ' | ')) AS q_or(q) "
        "WHERE a.search_vector @@ q_or.q "
        "  AND ( SELECT count(*) FROM unnest(q.lexemes) lx "
        "         WHERE lx <> '' AND a.search_vector @@ lx::tsquery "
        "      ) >= LEAST(2, cardinality(q.lexemes)) "
    )
    params = {"q": query.strip(), "limit": limit, "cfg": cfg}
    if language in ("en", "fr"):
        sql += "  AND a.language = :lang "
        params["lang"] = language
    sql += "ORDER BY match_count DESC, score DESC LIMIT :limit"

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        body = (row["body"] or "")[:body_chars]
        if row["body"] and len(row["body"]) > body_chars:
            body = body.rstrip() + "..."
        days_stale = None
        mod = row["modified_time"]
        if mod is not None:
            mod_aware = mod if mod.tzinfo else mod.replace(tzinfo=timezone.utc)
            days_stale = (now - mod_aware).days
        out.append({
            "id": row["zoho_article_id"],
            "title": row["title"],
            "body": body,
            "url": row["url"] or "",
            "permalink": row["permalink"] or "",
            "category": row["category"] or "",
            "language": row["language"] or "en",
            "modified_time": (
                row["modified_time"].isoformat()
                if row["modified_time"] else ""
            ),
            "days_stale": days_stale,
            "score": float(row["score"]) if row["score"] is not None else 0.0,
        })
    return out


def kb_index_status() -> dict:
    """Return summary stats about the kb_articles table."""
    if not DATABASE_URL:
        return {"error": "DATABASE_URL not set"}
    engine = _get_engine()
    with engine.connect() as conn:
        total = conn.execute(
            text("SELECT COUNT(*) FROM kb_articles")
        ).scalar() or 0
        if total == 0:
            return {"total": 0}

        by_lang = dict(conn.execute(
            text(
                "SELECT language, COUNT(*) FROM kb_articles "
                "GROUP BY language"
            )
        ).all())

        by_category = conn.execute(
            text(
                "SELECT category, COUNT(*) AS n FROM kb_articles "
                "GROUP BY category ORDER BY n DESC LIMIT 10"
            )
        ).all()

        synced = conn.execute(
            text(
                "SELECT MIN(synced_at), MAX(synced_at) FROM kb_articles"
            )
        ).first()

        modified = conn.execute(
            text(
                "SELECT MIN(modified_time), MAX(modified_time) "
                "FROM kb_articles WHERE modified_time IS NOT NULL"
            )
        ).first()

    return {
        "total": int(total),
        "by_language": {k: int(v) for k, v in by_lang.items()},
        "top_categories": [(c, int(n)) for c, n in by_category],
        "synced_oldest": synced[0].isoformat() if synced and synced[0] else None,
        "synced_newest": synced[1].isoformat() if synced and synced[1] else None,
        "modified_oldest": (
            modified[0].isoformat() if modified and modified[0] else None
        ),
        "modified_newest": (
            modified[1].isoformat() if modified and modified[1] else None
        ),
    }


# ---------------------------------------------------------------------------
# Vic intake outcomes (resolved-by-Vic vs escalated-to-team)
# ---------------------------------------------------------------------------

def log_vic_outcome(
    outcome: str,
    resolution_type: str | None = None,
    issue_fingerprint: str | None = None,
    org_id: str | None = None,
    user_email: str | None = None,
    user_role: str | None = None,
    ticket_id: str | None = None,
):
    """Record a terminal widget-intake outcome for reporting.

    outcome is "resolved" (Vic closed it, no ticket) or "escalated"
    (a Zoho ticket was filed for the team). Best-effort: never raise,
    so a logging hiccup can't break the user-facing chat.
    """
    if not DATABASE_URL or not outcome:
        return
    try:
        engine = _get_engine()
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO vic_resolution_log "
                    "(outcome, resolution_type, issue_fingerprint, "
                    " org_id, user_email, user_role, ticket_id, created_at) "
                    "VALUES (:outcome, :rtype, :fp, :org, :email, "
                    " :role, :tid, :ts)"
                ),
                {
                    "outcome": outcome,
                    "rtype": resolution_type,
                    "fp": issue_fingerprint,
                    "org": org_id,
                    "email": user_email,
                    "role": user_role,
                    "tid": ticket_id,
                    "ts": now,
                },
            )
    except Exception as e:
        print(f"[DB] log_vic_outcome failed: {e}")


def count_vic_resolved_today() -> int:
    """Number of chats Vic resolved without a ticket since midnight (server tz)."""
    if not DATABASE_URL:
        return 0
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT COUNT(*) FROM vic_resolution_log "
                    "WHERE outcome = 'resolved' "
                    "AND created_at >= date_trunc('day', NOW())"
                )
            ).scalar() or 0
    except Exception as e:
        print(f"[DB] count_vic_resolved_today failed: {e}")
        return 0


def get_vic_metrics(days: int = 30) -> dict:
    """Aggregate widget-intake outcomes over the last `days` days.

    Returns the resolved-vs-escalated split, a deflection rate, today's
    counts, a breakdown by resolution type, and the top topics Vic
    resolved on its own.
    """
    if not DATABASE_URL:
        return {"error": "DATABASE_URL not set"}
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            outcome_rows = conn.execute(
                text(
                    "SELECT outcome, COUNT(*) AS n FROM vic_resolution_log "
                    "WHERE created_at > NOW() - make_interval(days => :days) "
                    "GROUP BY outcome"
                ),
                {"days": days},
            ).all()
            counts = {o: int(n) for o, n in outcome_rows}
            resolved = counts.get("resolved", 0)
            escalated = counts.get("escalated", 0)
            total = resolved + escalated

            type_rows = conn.execute(
                text(
                    "SELECT resolution_type, COUNT(*) AS n "
                    "FROM vic_resolution_log "
                    "WHERE created_at > NOW() - make_interval(days => :days) "
                    "GROUP BY resolution_type ORDER BY n DESC"
                ),
                {"days": days},
            ).all()

            today_rows = conn.execute(
                text(
                    "SELECT outcome, COUNT(*) AS n FROM vic_resolution_log "
                    "WHERE created_at >= date_trunc('day', NOW()) "
                    "GROUP BY outcome"
                )
            ).all()
            today = {o: int(n) for o, n in today_rows}

            topic_rows = conn.execute(
                text(
                    "SELECT issue_fingerprint, COUNT(*) AS n "
                    "FROM vic_resolution_log "
                    "WHERE outcome = 'resolved' "
                    "AND issue_fingerprint IS NOT NULL "
                    "AND created_at > NOW() - make_interval(days => :days) "
                    "GROUP BY issue_fingerprint ORDER BY n DESC LIMIT 10"
                ),
                {"days": days},
            ).all()

        return {
            "window_days": days,
            "resolved": resolved,
            "escalated": escalated,
            "total": total,
            "resolution_rate": round(resolved / total, 3) if total else 0.0,
            "today": {
                "resolved": today.get("resolved", 0),
                "escalated": today.get("escalated", 0),
            },
            "by_resolution_type": {
                (rt or "unknown"): int(n) for rt, n in type_rows
            },
            "top_resolved_topics": [
                {"fingerprint": fp, "count": int(n)} for fp, n in topic_rows
            ],
        }
    except Exception as e:
        print(f"[DB] get_vic_metrics failed: {e}")
        return {"error": str(e)}


def _parse_zoho_time(zoho_str: str | None) -> datetime | None:
    """Parse a Zoho ISO 8601 timestamp into a naive UTC datetime for storage."""
    if not zoho_str:
        return None
    try:
        dt = datetime.fromisoformat(zoho_str.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


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
        "pending_draft": row.get("pending_draft"),
        "close_after_send": row["close_after_send"] == "true",
        "date": (
            row["created_at"].strftime("%Y-%m-%d")
            if row["created_at"] else ""
        ),
    }
