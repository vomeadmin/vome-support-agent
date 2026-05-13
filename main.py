import os

from dotenv import load_dotenv

load_dotenv()

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response

from agent import process_ticket, process_ticket_update, sync_zoho_to_clickup
from ops.router import ops_router
from intake import run_intake_turn
from kb_search import run_kb_health_scan
from kb_sync import run_kb_sync
from clickup_assignee_handler import handle_assignee_updated
from clickup_needs_review_handler import handle_needs_review
from clickup_waiting_client_handler import handle_waiting_on_client
from database import init_db
from field_feedback import handle_field_feedback
from on_prod_handler import handle_on_prod
from slack_agent_mention_handler import handle_agent_mention
from slack_reply_handler import handle_reply
from slack_digest import send_daily_digest

REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "ZOHO_DESK_MCP_URL",
    "ZOHO_CRM_MCP_URL",
    "ZOHO_ORG_ID",
]

def _check_env():
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        for v in missing:
            print(f"WARNING: missing required env variable: {v}")
        print("The agent may fail on requests that need these variables.")
    else:
        print("Startup: all required env variables present")
    desk_url = os.environ.get("ZOHO_DESK_MCP_URL", "")
    crm_url = os.environ.get("ZOHO_CRM_MCP_URL", "")
    if not desk_url.startswith("http"):
        print("ERROR: ZOHO_DESK_MCP_URL not configured")
    if not crm_url.startswith("http"):
        print("ERROR: ZOHO_CRM_MCP_URL not configured")


def _extract_zoho_payload(raw_body: bytes) -> dict:
    """Extract the inner payload dict from Zoho's webhook format."""
    print(f"[RAW PAYLOAD] {raw_body.decode('utf-8', errors='replace')[:3000]}")

    data = json.loads(raw_body)

    # Zoho sends a list wrapper
    if isinstance(data, list):
        print(f"Payload is a list with {len(data)} item(s) -- using first")
        entry = data[0] if data else {}
    else:
        entry = data

    # The actual ticket data is nested under "payload"
    ticket = entry.get("payload", entry)
    event_type = entry.get("eventType", "unknown")
    print(f"[EVENT TYPE] {event_type}")
    print(f"[PARSED TICKET] {json.dumps(ticket, default=str)[:2000]}")

    return ticket


def _build_ticket_data(ticket: dict) -> dict:
    """Build normalized ticket_data from Zoho's nested payload structure."""
    contact = ticket.get("contact") or {}
    first = contact.get("firstName") or ""
    last = contact.get("lastName") or ""
    contact_name = f"{first} {last}".strip() or ""

    raw_desc = ticket.get("description") or ""
    clean_body = re.sub(r"<[^>]+>", "", raw_desc).strip()

    return {
        "ticket_id": str(ticket.get("id", "unknown")),
        "ticket_number": str(ticket.get("ticketNumber", "")),
        "subject": ticket.get("subject", ""),
        "body": clean_body,
        "contact_email": contact.get("email", ""),
        "contact_name": contact_name,
    }

_check_env()
init_db()

app = FastAPI(title="Vome Support Agent")

# ---------------------------------------------------------------------------
# Ticket Command Center (ops/) router
# ---------------------------------------------------------------------------
app.include_router(ops_router, prefix="/ops")

# ---------------------------------------------------------------------------
# APScheduler — daily digest at 17:00 ET (America/Montreal)
# ---------------------------------------------------------------------------

_scheduler = BackgroundScheduler(timezone="America/Montreal")
_scheduler.add_job(
    send_daily_digest,
    CronTrigger(hour=17, minute=0, timezone="America/Montreal"),
)
_scheduler.add_job(
    run_kb_health_scan,
    CronTrigger(day_of_week="mon", hour=9, minute=0, timezone="America/Montreal"),
)
_scheduler.add_job(
    run_kb_sync,
    CronTrigger(hour=2, minute=0, timezone="America/Montreal"),
)
_scheduler.start()


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------

def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        return True  # Skip verification if not configured (dev only)
    base = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Slack events webhook
# ---------------------------------------------------------------------------

SLACK_TICKETS_CHANNEL = os.environ.get("SLACK_CHANNEL_VOME_TICKETS", "")
SLACK_FIELD_FEEDBACK_CHANNEL = os.environ.get("SLACK_CHANNEL_VOME_FIELD_FEEDBACK", "")
SLACK_FINAL_REVIEW_CHANNEL = os.environ.get("SLACK_CHANNEL_SUPPORT_FINAL_REVIEW", "")
SLACK_QUEUE_SANJAY_CHANNEL = os.environ.get("SLACK_CHANNEL_SUPPORT_QUEUE_SANJAY", "")
SLACK_QUEUE_ONLYG_CHANNEL = os.environ.get("SLACK_CHANNEL_SUPPORT_QUEUE_ONLYG", "")

_slack_processed_events: dict[str, float] = {}


@app.post("/webhook/slack-events")
async def slack_events_webhook(request: Request):
    raw_body = await request.body()

    # Verify Slack request signature
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(raw_body, timestamp, signature):
        return Response(content="Invalid signature", status_code=403)

    payload = json.loads(raw_body)

    # Slack URL verification challenge (sent once when configuring Event Subscriptions)
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    # Dedup: Slack retries events if we don't respond in 3s.
    # Use event_id to prevent processing the same event twice.
    event_id = payload.get("event_id", "")
    if event_id and event_id in _slack_processed_events:
        return {"status": "ok"}
    if event_id:
        import time
        now = time.time()
        _slack_processed_events[event_id] = now
        # Clean old entries
        expired = [k for k, v in _slack_processed_events.items() if now - v > 120]
        for k in expired:
            del _slack_processed_events[k]

    event = payload.get("event", {})
    event_type = event.get("type", "")
    channel = event.get("channel", "")

    # Ignore bot messages and non-message events we don't handle
    if event.get("bot_id") or event.get("subtype"):
        return {"status": "ok"}

    # app_mention — @Agent was mentioned in a channel
    if event_type == "app_mention":
        handle_agent_mention(event)
        return {"status": "ok"}

    # file_shared — attach file data to event and route as a reply
    if event_type == "file_shared":
        file_id = event.get("file_id")
        if file_id and channel == SLACK_TICKETS_CHANNEL:
            try:
                file_info = __import__("slack_sdk", fromlist=["WebClient"])
                # Fetch file metadata from Slack
                from slack_sdk import WebClient as _WC
                _wc = _WC(token=os.environ.get("SLACK_BOT_TOKEN", ""))
                fdata = _wc.files_info(file=file_id).get("file", {})
                handle_reply({
                    "user": event.get("user_id"),
                    "text": "",
                    "thread_ts": fdata.get("shares", {}).get("public", {}).get(channel, [{}])[0].get("ts"),
                    "channel": channel,
                    "files": [fdata],
                })
            except Exception as e:
                print(f"file_shared handling failed: {e}")
        return {"status": "ok"}

    # message event — route by channel
    if event_type == "message":
        thread_ts = event.get("thread_ts")
        user = event.get("user")
        text = event.get("text", "")
        files = event.get("files", [])

        _reply_channels = (
            SLACK_TICKETS_CHANNEL,
            SLACK_FINAL_REVIEW_CHANNEL,
            SLACK_QUEUE_SANJAY_CHANNEL,
            SLACK_QUEUE_ONLYG_CHANNEL,
        )
        if channel in _reply_channels and thread_ts:
            # Only process replies in threads (not new top-level messages)
            if thread_ts != event.get("ts"):
                handle_reply({
                    "user": user,
                    "text": text,
                    "thread_ts": thread_ts,
                    "channel": channel,
                    "files": files,
                    "client_msg_id": event.get("client_msg_id"),
                    "event_ts": event.get("event_ts") or event.get("ts"),
                })

        elif channel == SLACK_FIELD_FEEDBACK_CHANNEL:
            handle_field_feedback({
                "user": user,
                "text": text,
                "ts": event.get("ts", ""),
                "thread_ts": thread_ts,
                "files": files,
            })

        elif event.get("channel_type") == "im":
            # DM to the bot — treat as @mention
            handle_agent_mention({
                "type": "app_mention",
                "user": user,
                "text": text,
                "ts": event.get("ts", ""),
                "thread_ts": thread_ts,
                "channel": channel,
            })

    return {"status": "ok"}


@app.post("/webhook/zoho-ticket")
async def zoho_ticket_webhook(request: Request):
    raw_body = await request.body()
    ticket = _extract_zoho_payload(raw_body)
    ticket_data = _build_ticket_data(ticket)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"[{timestamp}] Ticket #{ticket_data['ticket_number']} "
        f"(ID: {ticket_data['ticket_id']}) "
        f"from {ticket_data['contact_email'] or 'no email'} "
        f"-- {ticket_data['subject'] or 'no subject'}"
    )

    process_ticket(ticket_data)

    return {"status": "ok"}


@app.post("/webhook/zoho-update")
async def zoho_update_webhook(request: Request):
    raw_body = await request.body()
    ticket = _extract_zoho_payload(raw_body)
    ticket_id = str(ticket.get("id", "unknown"))
    ticket_number = str(ticket.get("ticketNumber", ""))

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] Ticket update #{ticket_number} (ID: {ticket_id})")

    process_ticket_update(ticket_id)

    # Sync Zoho assignee/status changes to ClickUp
    sync_zoho_to_clickup(ticket_id)

    return {"status": "ok"}


# ClickUp webhook subscription must include BOTH event types:
#   - taskStatusUpdated  (ON PROD, Waiting on Client, Needs Review)
#   - taskAssigneeUpdated (assignee sync to Zoho)
# Configure at: https://app.clickup.com > Space settings > Integrations > Webhooks

import time as _time

_clickup_status_dedup: dict[str, float] = {}
_CLICKUP_DEDUP_TTL = 300  # 5 minutes

def _clickup_dedup_check(key: str) -> bool:
    """Return True if this key was already processed recently."""
    now = _time.time()
    expired = [
        k for k, v in _clickup_status_dedup.items()
        if now - v > _CLICKUP_DEDUP_TTL
    ]
    for k in expired:
        del _clickup_status_dedup[k]
    if key in _clickup_status_dedup:
        return True
    _clickup_status_dedup[key] = now
    return False

@app.post("/webhook/clickup-status")
async def clickup_status_webhook(request: Request):
    raw_body = await request.body()
    payload = json.loads(raw_body)

    event = payload.get("event", "")
    task_id = payload.get("task_id", "")

    # Handle assignee changes
    if event == "taskAssigneeUpdated" and task_id:
        handle_assignee_updated(payload)
        return {"status": "ok"}

    if event != "taskStatusUpdated" or not task_id:
        return {"status": "ok"}

    for item in payload.get("history_items", []):
        if item.get("field") != "status":
            continue
        new_status = (
            (item.get("after") or {}).get("status") or ""
        ).lower().strip()
        user = item.get("user") or {}
        engineer_name = (
            user.get("username")
            or user.get("email")
            or "Engineer"
        )
        timestamp = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

        # Dedup: ClickUp can fire the same status webhook
        # multiple times (retries, network issues).
        dedup_key = f"{task_id}:{new_status}"
        if _clickup_dedup_check(dedup_key):
            print(
                f"[{timestamp}] Duplicate ClickUp status"
                f" webhook — {dedup_key} — skipping"
            )
            break

        if new_status in ("on prod", "on_prod", "on prod ✅"):
            print(
                f"[{timestamp}] ON PROD detected — "
                f"task {task_id} by {engineer_name}"
            )
            handle_on_prod(task_id, engineer_name)
            break

        if new_status in ("waiting on client", "waiting_on_client"):
            print(
                f"[{timestamp}] WAITING ON CLIENT detected — "
                f"task {task_id} by {engineer_name}"
            )
            handle_waiting_on_client(task_id, engineer_name)
            break

        if new_status in ("needs review", "needs_review"):
            print(
                f"[{timestamp}] NEEDS REVIEW detected — "
                f"task {task_id} by {engineer_name}"
            )
            handle_needs_review(task_id, engineer_name)
            break

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Support widget intake
# ---------------------------------------------------------------------------

@app.post("/chat/intake")
async def chat_intake(request: Request):
    body = await request.json()
    result = run_intake_turn(
        message=body.get("message", ""),
        session_context=body.get("session_context", {}),
        conversation_history=body.get("conversation_history", []),
        attachments=body.get("attachments", []),
    )
    return result


@app.get("/chat/tickets")
async def chat_tickets(request: Request):
    """Fetch Zoho Desk tickets for a given email address."""
    email = request.query_params.get("email", "")
    if not email:
        return {"tickets": []}

    from zoho_desk_api import search_tickets

    tickets = search_tickets(email)
    return {"tickets": tickets}


# ---------------------------------------------------------------------------
# Auth check (calls Django server-to-server)
# ---------------------------------------------------------------------------

DJANGO_API_URL = os.environ.get("DJANGO_API_URL", "")
SUPPORT_API_KEY = os.environ.get("SUPPORT_API_KEY", "")


@app.get("/chat/auth-check")
async def chat_auth_check(request: Request):
    """Check a user's auth status via Django."""
    email = request.query_params.get("email", "")
    if not email or not DJANGO_API_URL:
        return {"found": False, "reason": "Not configured"}

    try:
        resp = httpx.get(
            f"{DJANGO_API_URL}/api/support/auth-check/",
            params={"email": email},
            headers={"X-Support-Api-Key": SUPPORT_API_KEY},
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"found": False, "reason": str(e)}


@app.post("/chat/auth-bypass")
async def chat_auth_bypass(request: Request):
    """Bypass authentication for a user via Django."""
    body = await request.json()
    email = body.get("email", "")
    if not email or not DJANGO_API_URL:
        return {"bypassed": False, "reason": "Not configured"}

    try:
        resp = httpx.post(
            f"{DJANGO_API_URL}/api/support/auth-check/",
            json={"email": email, "action": "bypass"},
            headers={"X-Support-Api-Key": SUPPORT_API_KEY},
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"bypassed": False, "reason": str(e)}


@app.get("/debug/test-ticket-fetch")
async def debug_test_ticket_fetch(request: Request):
    """Test fetching tickets -- shows raw MCP response."""
    from agent import _zoho_desk_call, _unwrap_mcp_result, ZOHO_ORG_ID
    results = {}
    offset = int(request.query_params.get("offset", "0"))
    raw1 = _zoho_desk_call("ZohoDesk_getTickets", {
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "departmentId": "569440000000006907",
            "status": "Open,Closed,On Hold,Escalated",
            "from": str(offset),
            "limit": "100",
        },
    })
    results["offset_requested"] = offset
    results["getTickets_type"] = str(type(raw1))
    results["getTickets_isError"] = bool(
        isinstance(raw1, dict) and raw1.get("isError")
    ) if raw1 else "null"

    # Show raw content structure for debugging
    if isinstance(raw1, dict):
        content = raw1.get("content", [])
        results["getTickets_content_len"] = len(content)
        if content:
            first = content[0]
            results["getTickets_first_content_type"] = first.get("type", "?")
            text_val = first.get("text", "")
            results["getTickets_text_preview"] = text_val[:500] if text_val else "empty"

    unwrapped1 = _unwrap_mcp_result(raw1)
    if unwrapped1:
        if isinstance(unwrapped1, dict):
            results["getTickets_keys"] = list(unwrapped1.keys())[:10]
            results["getTickets_count"] = len(unwrapped1.get("data", []))
        elif isinstance(unwrapped1, list):
            results["getTickets_count"] = len(unwrapped1)
    else:
        results["getTickets_unwrapped"] = "None"
    return results


@app.get("/debug/mcp-tools")
async def debug_mcp_tools():
    """List available tools from the Zoho Desk MCP server."""
    from agent import ZOHO_DESK_MCP_URL
    if not ZOHO_DESK_MCP_URL:
        return {"error": "ZOHO_DESK_MCP_URL not set"}
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        resp = httpx.post(ZOHO_DESK_MCP_URL, json=payload, timeout=15)
        data = resp.json()
        tools = data.get("result", data)
        if isinstance(tools, dict) and "tools" in tools:
            names = [t.get("name", "") for t in tools["tools"]]
            ticket_tools = [n for n in names if "ticket" in n.lower() or "Ticket" in n]
            return {
                "total_tools": len(names),
                "ticket_related": ticket_tools,
                "all_tools": names,
            }
        return {"raw": data}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Knowledge Book builder
# ---------------------------------------------------------------------------

_analysis_running = False
_analysis_status = {"status": "idle", "started": None, "last_update": None}


@app.post("/knowledge-book/run")
async def run_knowledge_book(request: Request):
    """Trigger the ticket analysis pipeline.
    Runs in a background thread so it doesn't block the server.
    """
    global _analysis_running, _analysis_status
    if _analysis_running:
        return {"status": "already_running", "info": _analysis_status}

    import threading

    def _run():
        global _analysis_running, _analysis_status
        _analysis_running = True
        _analysis_status = {
            "status": "running",
            "started": datetime.now(timezone.utc).isoformat(),
            "last_update": None,
        }
        try:
            from ticket_analyzer import run_full_analysis
            run_full_analysis()
            _analysis_status["status"] = "completed"
        except Exception as e:
            _analysis_status["status"] = f"failed: {e}"
        finally:
            _analysis_status["last_update"] = (
                datetime.now(timezone.utc).isoformat()
            )
            _analysis_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"status": "started", "info": _analysis_status}


@app.get("/knowledge-book/status")
async def knowledge_book_status():
    """Check the status of the ticket analysis pipeline."""
    from ticket_analyzer import get_analysis_stats
    stats = get_analysis_stats()
    return {
        "pipeline": _analysis_status,
        "running": _analysis_running,
        "analyzed_tickets": stats,
        "total_analyzed": sum(stats.values()),
    }


_kb_sync_running = False
_kb_sync_status = {"status": "idle", "started": None, "last_update": None}


@app.post("/kb-sync/run")
async def kb_sync_run(request: Request):
    """Trigger a full Zoho Desk -> kb_articles sync.

    Runs in a background thread so the request returns immediately.
    Safe to call repeatedly -- the sync UPSERTs by zoho_article_id and
    only writes rows whose modifiedTime changed.
    """
    global _kb_sync_running, _kb_sync_status
    if _kb_sync_running:
        return {"status": "already_running", "info": _kb_sync_status}

    import threading

    def _run():
        global _kb_sync_running, _kb_sync_status
        _kb_sync_running = True
        _kb_sync_status = {
            "status": "running",
            "started": datetime.now(timezone.utc).isoformat(),
            "last_update": None,
            "result": None,
        }
        try:
            from kb_sync import (
                fetch_all_kb_articles,
                sync_articles_to_db,
            )
            articles = fetch_all_kb_articles()
            stats = sync_articles_to_db(articles) if articles else {}
            _kb_sync_status["status"] = "completed"
            _kb_sync_status["result"] = {
                "articles_fetched": len(articles),
                **stats,
            }
        except Exception as e:
            _kb_sync_status["status"] = f"failed: {e}"
        finally:
            _kb_sync_status["last_update"] = (
                datetime.now(timezone.utc).isoformat()
            )
            _kb_sync_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"status": "started", "info": _kb_sync_status}


@app.get("/kb-sync/debug")
async def kb_sync_debug():
    """Surface raw Zoho responses so we can see why fetch returns empty.

    Returns the categories list, plus -- if any categories exist -- a
    sample of the first category's article list. No body fetches, just
    the listing endpoints.
    """
    from agent import _zoho_desk_call, _unwrap_mcp_result, ZOHO_ORG_ID
    out: dict = {"zoho_org_id_set": bool(ZOHO_ORG_ID)}

    cat_result = _zoho_desk_call(
        "ZohoDesk_getAllKBRootCategories",
        {"query_params": {"orgId": str(ZOHO_ORG_ID)}},
    )
    out["categories_raw_type"] = type(cat_result).__name__
    out["categories_raw"] = cat_result

    raw_cats = _unwrap_mcp_result(cat_result)
    out["categories_unwrapped_type"] = type(raw_cats).__name__
    if isinstance(raw_cats, dict):
        out["categories_unwrapped_keys"] = list(raw_cats.keys())
        cat_list = raw_cats.get("data", [])
    elif isinstance(raw_cats, list):
        cat_list = raw_cats
    else:
        cat_list = []
    out["category_count"] = len(cat_list)
    out["category_sample"] = cat_list[:3]

    if cat_list:
        first = cat_list[0]
        cat_id = first.get("id")
        out["first_category_id"] = cat_id

        # Grab one real article ID so we can probe getArticle shapes.
        probe_art_id = None
        probe_result = _zoho_desk_call(
            "ZohoDesk_getArticles",
            {
                "query_params": {
                    "orgId": str(ZOHO_ORG_ID),
                    "categoryId": str(cat_id),
                    "from": 1,
                    "limit": 1,
                },
            },
        )
        probe_unwrapped = _unwrap_mcp_result(probe_result)
        if isinstance(probe_unwrapped, dict):
            probe_data = probe_unwrapped.get("data", []) or []
            if probe_data:
                probe_art_id = str(probe_data[0].get("id") or "")
        out["probe_article_id"] = probe_art_id

        # Probe getArticle (singular) -- multiple argument shapes
        if probe_art_id:
            article_attempts = [
                ("getArticle, path_var articleId", {
                    "path_variables": {"articleId": probe_art_id},
                    "query_params": {"orgId": str(ZOHO_ORG_ID)},
                }),
                ("getArticle, query articleId", {
                    "query_params": {
                        "orgId": str(ZOHO_ORG_ID),
                        "articleId": probe_art_id,
                    },
                }),
                ("getArticle, path_var id", {
                    "path_variables": {"id": probe_art_id},
                    "query_params": {"orgId": str(ZOHO_ORG_ID)},
                }),
            ]
            out["article_detail_attempts"] = []
            for label, args in article_attempts:
                raw = _zoho_desk_call("ZohoDesk_getArticle", args)
                entry: dict = {"label": label}
                if isinstance(raw, dict) and raw.get("isError"):
                    for blk in raw.get("content", []) or []:
                        if blk.get("type") == "text":
                            entry["error_text"] = (
                                blk.get("text", "") or ""
                            )[:400]
                            break
                    out["article_detail_attempts"].append(entry)
                    continue
                unwrapped = _unwrap_mcp_result(raw)
                if isinstance(unwrapped, dict):
                    if "isError" in unwrapped:
                        entry["isError"] = unwrapped.get("isError")
                        for blk in unwrapped.get("content", []) or []:
                            if blk.get("type") == "text":
                                entry["error_text"] = (
                                    blk.get("text", "") or ""
                                )[:400]
                                break
                    else:
                        entry["keys"] = list(unwrapped.keys())[:30]
                        entry["title"] = unwrapped.get("title", "")
                        entry["has_answer"] = bool(
                            unwrapped.get("answer")
                        )
                        entry["answer_len"] = len(
                            unwrapped.get("answer") or ""
                        )
                out["article_detail_attempts"].append(entry)

        # Try a few argument shapes -- whichever one returns articles
        # tells us how to talk to this MCP server.
        attempts = [
            ("getArticles, no locale", "ZohoDesk_getArticles", {
                "path_variables": {"categoryId": str(cat_id)},
                "query_params": {"orgId": str(ZOHO_ORG_ID), "limit": 5},
            }),
            ("getArticles, locale=en", "ZohoDesk_getArticles", {
                "path_variables": {"categoryId": str(cat_id)},
                "query_params": {
                    "orgId": str(ZOHO_ORG_ID),
                    "limit": 5,
                    "locale": "en",
                },
            }),
            ("searchArticleTranslations, en", (
                "ZohoDesk_searchArticleTranslations"
            ), {
                "path_variables": {"locale": "en"},
                "query_params": {
                    "orgId": str(ZOHO_ORG_ID),
                    "categoryId": str(cat_id),
                    "status": "Published",
                    "limit": 5,
                },
            }),
            ("getArticles, no path_var, query categoryId", (
                "ZohoDesk_getArticles"
            ), {
                "query_params": {
                    "orgId": str(ZOHO_ORG_ID),
                    "categoryId": str(cat_id),
                    "limit": 5,
                },
            }),
        ]

        out["attempts"] = []
        for label, tool, args in attempts:
            raw = _zoho_desk_call(tool, args)
            entry: dict = {"label": label, "tool": tool}

            # Capture error text if present (MCP wraps errors in content)
            if isinstance(raw, dict) and raw.get("isError"):
                err_text = ""
                for blk in raw.get("content", []) or []:
                    if blk.get("type") == "text":
                        err_text = blk.get("text", "")[:500]
                        break
                entry["isError"] = True
                entry["error_text"] = err_text
                out["attempts"].append(entry)
                continue

            unwrapped = _unwrap_mcp_result(raw)
            if isinstance(unwrapped, dict):
                if "isError" in unwrapped:
                    entry["isError"] = unwrapped.get("isError")
                    entry["raw_keys"] = list(unwrapped.keys())
                    err_text = ""
                    for blk in unwrapped.get("content", []) or []:
                        if blk.get("type") == "text":
                            err_text = blk.get("text", "")[:500]
                            break
                    entry["error_text"] = err_text
                else:
                    arts = unwrapped.get("data", [])
                    entry["article_count"] = len(arts)
                    entry["sample_titles"] = [
                        a.get("title") or a.get("name") or ""
                        for a in arts[:5]
                    ]
            elif isinstance(unwrapped, list):
                entry["article_count"] = len(unwrapped)
                entry["sample_titles"] = [
                    a.get("title") or a.get("name") or ""
                    for a in unwrapped[:5]
                ]
            else:
                entry["unwrapped_type"] = type(unwrapped).__name__

            out["attempts"].append(entry)

    return out


@app.get("/kb-sync/status")
async def kb_sync_status():
    """Inspect the kb_articles index and the last sync run."""
    from database import kb_index_status
    from kb_sync import LAST_FETCH_DEBUG
    try:
        index = kb_index_status()
    except Exception as e:
        index = {"error": str(e)}
    return {
        "pipeline": _kb_sync_status,
        "running": _kb_sync_running,
        "index": index,
        "last_fetch_debug": LAST_FETCH_DEBUG,
    }


@app.get("/health")
async def health():
    env_status = {v: bool(os.environ.get(v)) for v in REQUIRED_ENV}
    return {"status": "ok", "env": env_status}


# ---------------------------------------------------------------------------
# Serve Command Center frontend (React SPA)
# Must be LAST — catches all unmatched routes and serves index.html
# ---------------------------------------------------------------------------
import pathlib as _pathlib

_frontend_dist = _pathlib.Path(__file__).parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount(
        "/",
        StaticFiles(directory=str(_frontend_dist), html=True),
        name="frontend",
    )
