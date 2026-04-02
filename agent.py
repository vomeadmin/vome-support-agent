import json
import os
import re
import sys

import anthropic
import httpx
from pathlib import Path

import random

from slack_ticket_brief import send_ticket_brief
from slack import post_to_engineering
from clickup_tasks import create_clickup_task

# Fix Windows console encoding for emoji in system_prompt.md
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

client = anthropic.Anthropic()

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")

# Load response templates so Claude can reference them when drafting replies
_templates_path = Path(__file__).parent / "response_templates.md"
if _templates_path.exists():
    _templates_content = _templates_path.read_text(encoding="utf-8")
    SYSTEM_PROMPT += (
        "\n\n---\n\n"
        "## RESPONSE TEMPLATES\n\n"
        "The following are proven response templates for common support "
        "scenarios. When drafting a reply, check if a template fits the "
        "situation. If one does, use it as the base and personalize with "
        "the client's name and specific details from the ticket. You may "
        "adapt the wording to fit context, but preserve key links and "
        "instructions from the template. If Sam references a template by "
        "name (e.g. 'use the ForgotPassword template'), use that template "
        "as the foundation for the draft.\n\n"
        f"{_templates_content}"
    )

ZOHO_DESK_MCP_URL = os.environ.get("ZOHO_DESK_MCP_URL", "")
ZOHO_CRM_MCP_URL = os.environ.get("ZOHO_CRM_MCP_URL", "")
ZOHO_ORG_ID = os.environ.get("ZOHO_ORG_ID", "")

NOTE_HEADER = "\U0001f916 AGENT ANALYSIS \u2014 DO NOT SEND \u2014 FOR REVIEW ONLY"
UPDATE_HEADER = "\U0001f504 AGENT UPDATE \u2014 CLIENT REPLIED"
TEAM_EMAILS = {
    "admin@vomevolunteer.com",
    "sam@vomevolunteer.com",
    "s.fagen@vomevolunteer.com",
    "r.segev@vomevolunteer.com",
}
ZOHO_FROM_ADDRESS = os.environ.get("ZOHO_FROM_ADDRESS", "admin@vomevolunteer.com")


def _mcp_post(url: str, tool_name: str, arguments: dict) -> dict | None:
    """Send a tools/call JSON-RPC request to an MCP server."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    try:
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            print(f"MCP error ({tool_name}): {result['error']}")
            return None
        return result.get("result", result)
    except Exception as e:
        print(f"MCP request failed ({tool_name}): {e}")
        return None


def _zoho_desk_call(tool_name: str, arguments: dict) -> dict | None:
    """Call a ZohoDesk tool via the Desk MCP server."""
    if not ZOHO_DESK_MCP_URL:
        print(f"ZOHO_DESK_MCP_URL not set -- skipping {tool_name}")
        return None
    return _mcp_post(ZOHO_DESK_MCP_URL, tool_name, arguments)


def _zoho_crm_call(tool_name: str, arguments: dict) -> dict | None:
    """Call a ZohoCRM tool via the CRM MCP server."""
    if not ZOHO_CRM_MCP_URL:
        print(f"ZOHO_CRM_MCP_URL not set -- skipping {tool_name}")
        return None
    return _mcp_post(ZOHO_CRM_MCP_URL, tool_name, arguments)


def _unwrap_mcp_result(result: dict | list | None) -> dict | list | None:
    """Unwrap MCP content wrapper to get the raw JSON data."""
    if result is None:
        return None
    if isinstance(result, dict) and "content" in result:
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (json.JSONDecodeError, TypeError):
                    pass
    return result


def fetch_ticket_from_zoho(ticket_id: str) -> dict | None:
    """Fetch full ticket details from Zoho Desk including contact info."""
    result = _zoho_desk_call("ZohoDesk_getTicket", {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "include": "contacts,assignee",
        },
    })
    if result:
        print(f"Fetched ticket {ticket_id} from Zoho Desk")
    else:
        print(f"Failed to fetch ticket {ticket_id} from Zoho Desk")
    return result


def fetch_ticket_conversations(ticket_id: str) -> dict | None:
    """Fetch all conversation threads and comments for a ticket."""
    result = _zoho_desk_call("ZohoDesk_getTicketConversations", {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
            "from": 0,
            "limit": 100,
        },
    })
    if result:
        print(f"Fetched conversations for ticket {ticket_id}")
    else:
        print(f"Failed to fetch conversations for ticket {ticket_id}")
    return result


def _extract_ticket_fields(raw_result: dict) -> dict:
    """Extract relevant fields from a Zoho ticket API response."""
    ticket = _unwrap_mcp_result(raw_result) or {}
    if not isinstance(ticket, dict):
        ticket = {}

    # Contact info from the included contacts data
    contact = ticket.get("contact", {}) or {}
    first = contact.get("firstName", "") or ""
    last = contact.get("lastName", "") or ""
    contact_name = f"{first} {last}".strip()
    if not contact_name:
        contact_name = (
            contact.get("name", "")
            or ticket.get("email", "")
            or "unknown"
        )
    contact_email = (
        contact.get("email", "")
        or ticket.get("email", "")
        or ""
    )

    # CC list — Zoho returns as a comma-separated string or a list
    raw_cc = ticket.get("cc", "") or ""
    if isinstance(raw_cc, list):
        cc_email = ",".join(raw_cc)
    else:
        cc_email = raw_cc

    return {
        "subject": ticket.get("subject", ""),
        "description": ticket.get("description", ""),
        "contact_name": contact_name,
        "contact_email": contact_email,
        "cc_email": cc_email,
        "status": ticket.get("status", ""),
        "created_time": ticket.get("createdTime", ""),
    }


def _detect_attachments(
    zoho_ticket_raw,
    conversations_result,
) -> dict:
    """Check all attachment sources across ticket and thread.

    Returns:
        {
            "has_attachments": bool,
            "attachment_count": int,
            "attachment_locations": list[str],
        }
    """
    has_attachments = False
    total_count = 0
    locations: list[str] = []

    # --- Ticket-level fields ---
    ticket = _unwrap_mcp_result(zoho_ticket_raw) or {}
    if isinstance(ticket, dict):
        try:
            count = int(ticket.get("attachmentCount") or 0)
            if count > 0:
                has_attachments = True
                total_count += count
                locations.append("ticket body")
        except (ValueError, TypeError):
            pass

        desc_attach = ticket.get("descAttachments") or []
        if isinstance(desc_attach, list) and desc_attach:
            has_attachments = True
            if "ticket body" not in locations:
                locations.append("ticket body")

    # --- Thread message-level fields ---
    if conversations_result:
        data = _unwrap_mcp_result(conversations_result)
        if isinstance(data, dict):
            data = data.get("data", [])
        if isinstance(data, list):
            for i, entry in enumerate(data):
                has_attach = entry.get("hasAttach", False)
                msg_count = 0
                try:
                    msg_count = int(
                        entry.get("attachmentCount") or 0
                    )
                except (ValueError, TypeError):
                    pass
                if has_attach or msg_count > 0:
                    has_attachments = True
                    total_count += max(msg_count, 1)
                    locations.append(f"thread message {i + 1}")

    return {
        "has_attachments": has_attachments,
        "attachment_count": total_count,
        "attachment_locations": locations,
    }


def _format_conversations(raw_result: dict | list | None) -> str:
    """Format conversation threads into a readable chronological string."""
    if not raw_result:
        return "(No conversation threads found)"

    data = _unwrap_mcp_result(raw_result)

    # Normalize to a list of conversation entries
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list) or len(data) == 0:
        return "(No conversation threads found)"

    lines = []
    for entry in data:
        author = entry.get("author", {}) or {}
        author_name = author.get("name", "Unknown")
        timestamp = entry.get("createdTime", entry.get("sendDateTime", ""))
        content = entry.get("content", entry.get("summary", ""))
        is_public = entry.get("isPublic", True)
        visibility = "" if is_public else " [INTERNAL]"
        direction = entry.get("direction", "")

        header = f"--- {author_name}"
        if direction:
            header += f" [{direction}]"
        header += f"{visibility} -- {timestamp} ---"

        lines.append(header)
        lines.append(content if content else "(no content)")
        lines.append("")

    return "\n".join(lines)


def _normalize_tier(offering: str | None) -> str:
    """Map the CRM Offering field to a clean tier name."""
    if not offering:
        return "Unknown"
    normalized = offering.strip().lower()
    for tier in ("ultimate", "enterprise", "pro", "recruit"):
        if tier in normalized:
            return tier.capitalize()
    return "Unknown"


def _is_auth_error(result: dict | None) -> bool:
    """Check if an MCP result contains a 401/authorization error."""
    if result is None:
        return False
    raw = json.dumps(result, default=str).lower()
    return "401" in raw or "unauthorized" in raw or "invalid_token" in raw


def _fetch_desk_fallback(contact_email: str) -> dict:
    """Fall back to Zoho Desk contact/account search when CRM is unavailable."""
    not_found = {"found": False, "contact_type": "volunteer", "enrichment_source": "Desk only — CRM unavailable"}

    try:
        result = _zoho_desk_call("ZohoDesk_searchContacts", {
            "query_params": {
                "orgId": str(ZOHO_ORG_ID),
                "_all": contact_email,
                "limit": "1",
            },
        })
        data = _unwrap_mcp_result(result)

        if not data:
            print(f"Desk fallback: contact not found ({contact_email})")
            return not_found

        # Normalize to list
        contacts = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(contacts, list) or not contacts:
            print(f"Desk fallback: contact not found ({contact_email})")
            return not_found

        contact = contacts[0]
        contact_id = str(contact.get("id", ""))
        contact_name = contact.get("name") or contact.get("lastName") or "unknown"
        account_id = contact.get("accountId") or contact.get("account", {}).get("id") if isinstance(contact.get("account"), dict) else contact.get("accountId")
        print(f"Desk fallback: found contact {contact_name} ({contact_id})")

        enrichment = {
            "found": True,
            "contact_type": "admin",
            "account_name": None,
            "tier": "Unknown",
            "arr": None,
            "currency": None,
            "account_id": str(account_id) if account_id else None,
            "contact_id": contact_id,
            "enrichment_source": "Desk only — CRM unavailable",
        }

        if not account_id:
            print("Desk fallback: no account ID on contact")
            return enrichment

        acct_result = _zoho_desk_call("ZohoDesk_getAccount", {
            "path_variables": {"accountId": str(account_id)},
            "query_params": {"orgId": str(ZOHO_ORG_ID)},
        })
        acct_data = _unwrap_mcp_result(acct_result)

        if acct_data and isinstance(acct_data, dict):
            enrichment["account_name"] = acct_data.get("accountName") or acct_data.get("name")
            print(f"Desk fallback: account {enrichment['account_name']} ({account_id})")
        else:
            print("Desk fallback: could not fetch account details")

        return enrichment

    except Exception as e:
        print(f"Desk fallback failed: {e}")
        return not_found


def fetch_crm_account(contact_email: str) -> dict:
    """Look up a contact in Zoho CRM by email and pull account + deal data."""
    not_found = {"found": False, "contact_type": "volunteer"}

    if not contact_email:
        print("CRM step 1: no email provided — skipping lookup")
        return not_found

    try:
        # Step 1 — search Contacts by email
        result = _zoho_crm_call("ZohoCRM_searchRecords", {
            "path_variables": {"module": "Contacts"},
            "query_params": {"email": contact_email},
        })

        if _is_auth_error(result):
            print("CRM authorization error — re-authorization needed in Zoho MCP portal")
            return _fetch_desk_fallback(contact_email)

        data = _unwrap_mcp_result(result)

        if not data or not isinstance(data, dict):
            print(f"CRM step 1: not found ({contact_email})")
            return not_found

        contacts = data.get("data", [])
        if not contacts:
            print(f"CRM step 1: not found ({contact_email})")
            return not_found

        contact = contacts[0]
        contact_name = contact.get("Full_Name", "unknown")
        contact_id = str(contact.get("id", ""))
        print(f"CRM step 1: found contact {contact_name} ({contact_id})")

        account_info = contact.get("Account_Name") or {}
        account_name = account_info.get("name")
        account_id = str(account_info.get("id", "")) if account_info.get("id") else None

        offering = contact.get("FV_Offering") or contact.get("Offering")
        if isinstance(offering, list):
            offering = offering[0] if offering else None
        tier = _normalize_tier(offering)

        enrichment = {
            "found": True,
            "contact_type": "admin",
            "account_name": account_name,
            "tier": tier,
            "arr": None,
            "currency": None,
            "account_id": account_id,
            "contact_id": contact_id,
        }

        # Step 2 — get related Deals for the account
        if not account_id:
            print("CRM step 2: no account ID — skipping deal lookup")
            return enrichment

        deal_result = _zoho_crm_call("ZohoCRM_searchRecords", {
            "path_variables": {"module": "Deals"},
            "query_params": {
                "criteria": f"(Account_Name:equals:{account_id})",
                "fields": "Deal_Name,Stage,Amount,Currency",
            },
        })

        if _is_auth_error(deal_result):
            print("CRM authorization error on deals — returning contact-only enrichment")
            return enrichment

        deal_data = _unwrap_mcp_result(deal_result)

        if not deal_data or not isinstance(deal_data, dict):
            print("CRM step 2: no deals found")
            return enrichment

        deals = deal_data.get("data", [])
        if not deals:
            print("CRM step 2: no deals found")
            return enrichment

        # Prefer first Closed Won deal, fall back to any deal
        chosen = None
        for deal in deals:
            stage = (deal.get("Stage") or "").lower()
            if "closed won" in stage:
                chosen = deal
                break
        if not chosen:
            chosen = deals[0]

        amount = chosen.get("Amount")
        if amount is not None:
            enrichment["arr"] = str(amount)
            enrichment["currency"] = chosen.get("Currency") or None
            print(f"CRM step 2: deal found — {enrichment['arr']} {enrichment['currency']}")
        else:
            print("CRM step 2: deal found but no Amount")

        return enrichment

    except Exception as e:
        print(f"CRM lookup failed: {e}")
        print("Attempting Desk fallback...")
        return _fetch_desk_fallback(contact_email)


def _detect_language(text: str) -> str | None:
    """Detect non-English content by checking for common French patterns.

    Returns the language name if non-English detected, else None.
    """
    if not text:
        return None
    lower = text.lower()
    # Common French words/patterns unlikely in English
    french_markers = [
        " je ", " nous ", " vous ", " les ", " des ", " une ", " est ",
        " sont ", " dans ", " pour ", " avec ", " sur ", " pas ",
        " mais ", " aussi ", " cette ", " notre ", " votre ",
        "bonjour", "merci", "s'il vous", "bénévol",
    ]
    hits = sum(1 for m in french_markers if m in f" {lower} ")
    if hits >= 3:
        return "French"
    return None


def get_client_tier(arr) -> str:
    """Derive client tier from CRM ARR value.

    Returns one of: very-high, high, medium, low.
    """
    if arr is None:
        return "low"
    try:
        value = float(arr)
    except (ValueError, TypeError):
        return "low"
    if value >= 4000:
        return "very-high"
    if value >= 1500:
        return "high"
    if value >= 1000:
        return "medium"
    return "low"


# -- Zoho agent IDs for routing --
ZOHO_AGENT_SANJAY = "569440000023159001"
ZOHO_AGENT_ONLYG = "569440000023160001"
ZOHO_AGENT_SAM = "569440000000139001"


def _parse_new_classification(agent_response: str, arr) -> dict:
    """Parse the four new classification fields from Claude's structured output.

    Returns dict with keys: category, complexity, engineer_type, client_tier, flags.
    """

    def _extract(field: str) -> str:
        m = re.search(
            rf"^{re.escape(field)}:\s*(.+)",
            agent_response,
            re.IGNORECASE | re.MULTILINE,
        )
        return m.group(1).strip().lower() if m else ""

    raw_category = _extract("CATEGORY")
    category_map = {
        "technical bug": "bug",
        "bug": "bug",
        "investigation": "investigation",
        "feature request": "feature",
        "feature explanation/how-to": "how-to",
        "feature explanation": "how-to",
        "how-to": "how-to",
        "admin & billing": "billing",
        "admin and billing": "billing",
        "billing": "billing",
        "authentication": "auth",
        "auth": "auth",
    }
    category = category_map.get(raw_category, raw_category)

    raw_complexity = _extract("COMPLEXITY")
    complexity_map = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "very high": "very-high",
        "very-high": "very-high",
    }
    complexity = complexity_map.get(raw_complexity, raw_complexity)

    raw_eng = _extract("ENGINEER TYPE")
    eng_map = {
        "frontend": "frontend",
        "mobile": "mobile",
        "backend": "backend",
        "unclear": "unclear",
    }
    engineer_type = eng_map.get(raw_eng, raw_eng)

    client_tier = get_client_tier(arr)

    # Derive flags
    flags = []
    if category in ("bug", "investigation") and engineer_type in ("frontend", "mobile"):
        if complexity in ("high", "very-high") or client_tier == "very-high":
            flags.append("ping-sam")
    if client_tier == "very-high" and category in ("bug", "investigation"):
        if "ping-sam" not in flags:
            flags.append("ping-sam")
    if engineer_type == "unclear":
        flags.append("eng-unclear")

    return {
        "category": category,
        "complexity": complexity,
        "engineer_type": engineer_type,
        "client_tier": client_tier,
        "flags": flags,
    }


def _get_routing(classification: dict) -> dict:
    """Determine Zoho assignee and ClickUp list from classification.

    Returns dict with keys: assignee_id (str or None), clickup_list (str or None).
    """
    cat = classification["category"]
    eng = classification["engineer_type"]

    assignee_id = None
    clickup_list = None

    if cat in ("bug", "investigation") and eng in ("frontend", "mobile"):
        assignee_id = ZOHO_AGENT_SANJAY
        clickup_list = "priority_queue"
    elif cat == "auth":
        assignee_id = ZOHO_AGENT_ONLYG
        clickup_list = "priority_queue"
    elif cat in ("bug", "investigation") and eng == "backend":
        assignee_id = ZOHO_AGENT_ONLYG
        clickup_list = "priority_queue"
    elif cat in ("bug", "investigation") and eng == "unclear":
        assignee_id = ZOHO_AGENT_SANJAY
        clickup_list = "priority_queue"
    elif cat == "feature":
        assignee_id = None
        clickup_list = "raw_intake"
    elif cat in ("how-to", "billing"):
        assignee_id = None
        clickup_list = None

    return {"assignee_id": assignee_id, "clickup_list": clickup_list}


def update_zoho_ticket_assignment(
    ticket_id: str,
    assignee_id: str | None,
) -> bool:
    """Update a Zoho Desk ticket with assignee and set status to In Progress.

    If assignee_id is provided, sets the assignee and moves status to
    "In Progress".  If no assignee (how-to, billing, feature), leaves
    the ticket as-is for Sam to pick up.
    """
    if not assignee_id:
        print(f"Zoho ticket {ticket_id}: no engineer assignee -- leaving as New for Sam")
        return True

    body: dict = {
        "assigneeId": assignee_id,
        "status": "In Progress",
    }

    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    if not result:
        print(f"Failed to update assignee/status on ticket {ticket_id}")
        return False

    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"Failed to update assignee/status on ticket {ticket_id}: {data}")
        return False

    print(f"Zoho ticket {ticket_id} updated -- assignee: {assignee_id}, status: In Progress")
    return True


# -- Auto-acknowledgment reply templates --

_ACK_TEMPLATES_EN = [
    (
        "Hi {name}, thanks for reaching out. We've received your message "
        "and our team is reviewing it. We'll follow up shortly.\n"
        "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
    (
        "Hi {name}, we've got this and are looking into it. "
        "You'll hear from us soon.\n"
        "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
    (
        "Hi {name}, thanks for flagging this. Our team is on it "
        "and we'll get back to you with an update.\n"
        "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
    (
        "Hi {name}, this has been received and is being reviewed "
        "by our team. We'll be in touch shortly.\n"
        "Best,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
]

_ACK_TEMPLATES_FR = [
    (
        "Bonjour {name}, merci de nous avoir contactes. Nous avons bien "
        "recu votre message et notre equipe l'examine. Nous reviendrons "
        "vers vous sous peu.\n"
        "Cordialement,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
    (
        "Bonjour {name}, nous avons bien pris note de votre demande "
        "et notre equipe s'en occupe. Vous aurez de nos nouvelles bientot.\n"
        "Cordialement,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
    (
        "Bonjour {name}, merci d'avoir signale ceci. Notre equipe "
        "examine la situation et nous vous tiendrons informe.\n"
        "Cordialement,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
    (
        "Bonjour {name}, votre demande a bien ete recue et est "
        "en cours d'examen par notre equipe. Nous vous recontacterons "
        "rapidement.\n"
        "Cordialement,\n\nSam | Vome support\nsupport.vomevolunteer.com"
    ),
]

# Module names used for info-sufficiency check
_MODULE_KEYWORDS = [
    "volunteer", "homepage", "schedule", "opportunities", "sequences",
    "forms", "dashboard", "settings", "permissions", "sites", "groups",
    "categories", "hour", "kiosk", "email", "chat", "reports", "kpi",
    "integrations", "authentication", "login", "sso",
]

_INFO_REQUEST_EN = (
    "If possible, could you share the affected user's email, "
    "any screenshots or a short video, and the steps you took "
    "when this happened?"
)
_INFO_REQUEST_FR = (
    "Si possible, pourriez-vous nous partager l'adresse courriel "
    "de l'utilisateur concerne, des captures d'ecran ou une courte "
    "video, ainsi que les etapes que vous avez suivies?"
)


def _ticket_is_sparse(body: str) -> bool:
    """Check if ticket body is too vague to act on without more info.

    Sparse = fewer than 30 words AND contains no module keyword AND
    no email address AND no numbered/bulleted steps.
    """
    if not body:
        return True
    words = body.split()
    if len(words) >= 30:
        return False
    lower = body.lower()
    has_module = any(kw in lower for kw in _MODULE_KEYWORDS)
    has_email = bool(re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", body))
    has_steps = bool(re.search(r"(^|\n)\s*(\d+[\.\):]|[-*])\s+", body))
    if has_module or has_email or has_steps:
        return False
    return True


def send_auto_acknowledgment(
    ticket_id: str,
    contact_name: str,
    contact_email: str,
    body: str,
    client_tier: str,
    detected_lang: str | None,
) -> bool:
    """Send an immediate auto-acknowledgment reply to the client.

    Picks a random template, optionally appends info-request for
    low/medium tier clients with sparse tickets.
    """
    first_name = (contact_name or "").split()[0] if contact_name else ""
    if not first_name:
        first_name = "there"

    is_french = detected_lang == "French"
    templates = _ACK_TEMPLATES_FR if is_french else _ACK_TEMPLATES_EN
    reply = random.choice(templates).format(name=first_name)

    # For low/medium tier only: append info request if ticket is sparse
    if client_tier in ("low", "medium") and _ticket_is_sparse(body):
        info_req = _INFO_REQUEST_FR if is_french else _INFO_REQUEST_EN
        # Insert before the sign-off line
        sign_off_marker = "Cordialement," if is_french else "Best,"
        if sign_off_marker in reply:
            reply = reply.replace(
                sign_off_marker,
                f"\n{info_req}\n\n{sign_off_marker}",
            )

    # Send via ZohoDesk_sendReply (actually sends, not a draft)
    result = _zoho_desk_call("ZohoDesk_sendReply", {
        "body": {
            "channel": "EMAIL",
            "fromEmailAddress": ZOHO_FROM_ADDRESS,
            "to": contact_email,
            "content": reply,
            "contentType": "plainText",
        },
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    if not result:
        print(f"Auto-ack FAILED on ticket {ticket_id}")
        return False

    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"Auto-ack FAILED on ticket {ticket_id}: {data}")
        return False

    print(f"Auto-ack sent on ticket {ticket_id} (lang={'FR' if is_french else 'EN'}, tier={client_tier})")
    return True


def post_draft_reply(ticket_id: str, content: str, to_email: str = "") -> bool:
    """Post an email draft reply on the Zoho Desk ticket (saved, not sent)."""
    body = {
        "channel": "EMAIL",
        "fromEmailAddress": ZOHO_FROM_ADDRESS,
        "content": content,
        "contentType": "plainText",
    }
    if to_email:
        body["to"] = to_email

    result = _zoho_desk_call("ZohoDesk_draftsReply", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    if not result:
        print(f"Failed to save draft reply on Zoho ticket {ticket_id}")
        return False

    if isinstance(result, dict) and result.get("isError"):
        print(f"Failed to save draft reply on Zoho ticket {ticket_id}: {result}")
        return False

    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"Failed to save draft reply on Zoho ticket {ticket_id}: {data}")
        return False

    print(f"Draft reply saved on Zoho ticket {ticket_id} -- success")
    return True


def post_to_zoho(ticket_id: str, agent_response: str) -> bool:
    """Post the agent's analysis as an internal note on the Zoho Desk ticket."""
    note_content = f"{NOTE_HEADER}\n\n{agent_response}"

    result = _zoho_desk_call("ZohoDesk_createTicketComment", {
        "body": {
            "content": note_content,
            "contentType": "plainText",
            "isPublic": False,
            "attachmentIds": [],
        },
        "path_variables": {
            "ticketId": str(ticket_id),
        },
        "query_params": {
            "orgId": str(ZOHO_ORG_ID),
        },
    })

    if not result:
        return False

    if isinstance(result, dict) and result.get("isError"):
        print(f"Internal note FAILED on Zoho ticket {ticket_id}: {result}")
        return False

    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(f"Internal note FAILED on Zoho ticket {ticket_id}: {data}")
        return False

    print(f"Internal note posted to Zoho ticket {ticket_id} -- success")
    return True


def _get_latest_client_reply(conversations_result, contact_email: str) -> str:
    """Return the most recent client message text (HTML stripped, max 200 chars).

    Returns empty string if there is only one conversation entry or no client
    reply is found — caller should only display this when non-empty.
    """
    if not conversations_result:
        return ""

    data = _unwrap_mcp_result(conversations_result)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list) or len(data) < 2:
        return ""

    contact_email_lower = contact_email.lower()

    for entry in data:
        if not entry.get("isPublic", True):
            continue
        content = entry.get("content", "") or ""
        if NOTE_HEADER in content or UPDATE_HEADER in content:
            continue

        author = entry.get("author", {}) or {}
        author_email = (author.get("email") or "").lower()
        author_type = (author.get("type") or "").upper()

        is_client = (
            author_type == "END_USER"
            or (author_email and author_email == contact_email_lower)
            or (author_email and author_email not in TEAM_EMAILS and author_type != "AGENT")
        )
        if not is_client:
            continue

        clean = re.sub(r"<[^>]+>", "", content).strip()
        if not clean:
            continue

        return clean[:200] + ("..." if len(clean) > 200 else "")

    return ""


def process_ticket(ticket_data: dict) -> str | None:
    """Process a Zoho Desk ticket through the support agent."""
    ticket_id = ticket_data.get("ticket_id", "unknown")

    # Fetch full ticket details and conversations from Zoho
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    conversations_result = fetch_ticket_conversations(ticket_id)

    if zoho_ticket:
        fields = _extract_ticket_fields(zoho_ticket)
        contact_name = fields["contact_name"]
        contact_email = fields["contact_email"]
        subject = fields["subject"]
        body = fields["description"]
        extra_context = (
            f"Status: {fields['status']}\n"
            f"Created: {fields['created_time']}"
        )
        print(f"Using full Zoho data for ticket {ticket_id}")
    else:
        # Fall back to webhook payload
        contact_name = ticket_data.get("contact_name", "")
        contact_email = ticket_data.get("contact_email", "")
        subject = ticket_data.get("subject", "")
        body = ticket_data.get("body", "")
        extra_context = ""
        print(f"Zoho fetch failed -- falling back to webhook payload for ticket {ticket_id}")

    thread_text = _format_conversations(conversations_result)

    # Comprehensive attachment detection across all sources
    attachment_info = _detect_attachments(
        zoho_ticket, conversations_result
    )
    if attachment_info["has_attachments"]:
        locs = ", ".join(attachment_info["attachment_locations"])
        print(
            f"Attachments detected — ticket {ticket_id}:"
            f" {attachment_info['attachment_count']} total"
            f" at {locs}"
        )

    # CRM enrichment
    crm = fetch_crm_account(contact_email)
    if crm["found"]:
        source_note = crm.get("enrichment_source", "")
        source_line = f"\nEnrichment source: {source_note}" if source_note else ""
        enrichment_block = (
            "ACCOUNT ENRICHMENT:\n"
            f"Account: {crm['account_name']}\n"
            f"Contact type: Admin\n"
            f"Tier: {crm['tier']}\n"
            f"ARR: {crm['arr']} {crm['currency']}\n"
            f"Account ID: {crm['account_id']}"
            f"{source_line}"
        )
    else:
        enrichment_block = (
            "ACCOUNT ENRICHMENT:\n"
            "Contact type: Volunteer\n"
            "Not found in CRM — treat as volunteer"
        )

    # Language detection
    lang_note = ""
    detected_lang = _detect_language(body) or _detect_language(thread_text)
    if detected_lang:
        lang_note = (
            f"\nNote: ticket content appears to be in {detected_lang} "
            f"-- draft response in {detected_lang}\n"
        )
        print(f"Language detected: {detected_lang} (ticket {ticket_id})")

    # Build attachment note for Claude prompt
    if attachment_info["has_attachments"]:
        locs_str = ", ".join(
            attachment_info["attachment_locations"]
        )
        attachment_note = (
            f"\nATTACHMENT FLAG: This ticket has"
            f" {attachment_info['attachment_count']}"
            f" attachment(s) at: {locs_str}.\n"
            "CRITICAL ATTACHMENT RULES:\n"
            "1. NEVER classify as Unclear based on vague text"
            " alone when attachments are present — classify"
            " as best you can from available text context.\n"
            "2. ALWAYS end your ISSUE SUMMARY with:"
            " [Attachment included — likely contains"
            " important context]\n"
            "3. Surface the attachment prominently in"
            " AGENT NOTES.\n"
        )
    else:
        attachment_note = ""

    user_message = (
        "\u26a0\ufe0f MANDATORY PROCESSING RULE \u2014 READ FIRST:\n"
        "This input arrived via Zoho Desk webhook.\n"
        "This is a CLIENT TICKET. Period.\n"
        "Do NOT classify as field feedback.\n"
        "Do NOT ask Ron to confirm anything.\n"
        "Do NOT post to Slack field feedback channel.\n"
        "The ticket submitter is the CLIENT, not Ron.\n"
        "Ron's replies in the thread are INTERNAL RESPONSES.\n"
        "Process as a client ticket with full enrichment.\n\n"
        "REQUIRED ADDITIONS TO YOUR AGENT ANALYSIS BLOCK:\n"
        "Include these two fields before CLASSIFICATION:\n"
        "ISSUE SUMMARY: [one line — 2-3 sentences plain English:"
        " what the person said, what they tried, any relevant"
        " context. Written as if briefing a colleague verbally.]\n"
        "SUGGESTED OWNER: [Sanjay / OnlyG / Sam / Either]\n"
        f"{attachment_note}\n"
        f"{enrichment_block}\n\n"
        f"SOURCE: Zoho Desk (webhook trigger)\n"
        f"Ticket ID: {ticket_id}\n"
        f"Client contact: {contact_name} ({contact_email})\n"
        f"Subject: {subject}\n"
        f"{lang_note}"
    )
    if extra_context:
        user_message += f"\n{extra_context}\n"
    user_message += (
        f"\nTicket body:\n{body}\n\n"
        f"Full conversation thread:\n{thread_text}"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        result = response.content[0].text
        print(f"\n{'='*60}")
        print(result)
        print(f"{'='*60}")
        print(
            f"Agent processed ticket {ticket_id}"
            f" -- response length: {len(result)} chars\n"
        )

        zoho_url = (
            f"https://desk.zoho.com/support/vomevolunteer"
            f"/ShowHomePage.do#Cases/dv/{ticket_id}"
        )

        # Extract structured fields from Claude's response
        def _extract_field(field: str) -> str:
            m = re.search(
                rf"^{re.escape(field)}:\s*(.+)",
                result,
                re.IGNORECASE | re.MULTILINE,
            )
            return m.group(1).strip() if m else ""

        # --- NEW: Parse classification, apply tags/assignee, auto-ack ---

        # 1. Parse four new classification fields
        new_class = _parse_new_classification(result, crm.get("arr"))
        print(
            f"Classification: cat={new_class['category']} "
            f"cx={new_class['complexity']} eng={new_class['engineer_type']} "
            f"tier={new_class['client_tier']} flags={new_class['flags']}"
        )

        # 2. Apply Zoho assignee + status (In Progress if routed to engineer)
        routing = _get_routing(new_class)
        update_zoho_ticket_assignment(ticket_id, routing["assignee_id"])

        # 3. Send auto-acknowledgment reply (immediate, no approval needed)
        send_auto_acknowledgment(
            ticket_id=ticket_id,
            contact_name=contact_name,
            contact_email=contact_email,
            body=body,
            client_tier=new_class["client_tier"],
            detected_lang=detected_lang,
        )

        # 4. Slack ping to Sam if flag:ping-sam is set
        if "ping-sam" in new_class["flags"]:
            ping_msg = (
                f"FYI -- High complexity or VIP ticket just assigned to "
                f"Sanjay: #{ticket_data.get('ticket_number', ticket_id)} "
                f"{subject} | tier:{new_class['client_tier']} "
                f"cx:{new_class['complexity']} | {zoho_url}"
            )
            try:
                post_to_engineering(ping_msg)
                print(f"ping-sam sent for ticket {ticket_id}")
            except Exception as e:
                print(f"ping-sam Slack post failed for ticket {ticket_id}: {e}")

        latest_reply = _get_latest_client_reply(
            conversations_result, contact_email
        )

        # Send Slack brief -- only output on new ticket arrival
        send_ticket_brief(
            ticket_id=ticket_id,
            ticket_number=ticket_data.get(
                "ticket_number", ticket_id
            ),
            subject=subject,
            crm=crm,
            agent_response=result,
            clickup_task_url=None,
            clickup_task_id=None,
            zoho_ticket_url=zoho_url,
            has_attachments=attachment_info["has_attachments"],
            attachment_count=attachment_info["attachment_count"],
            contact_name=contact_name,
            contact_email=contact_email,
            issue_summary=_extract_field("ISSUE SUMMARY"),
            latest_reply=latest_reply,
            timing=_extract_field("TIMING"),
            priority=_extract_field("PRIORITY"),
            suggested_owner=_extract_field("SUGGESTED OWNER"),
            # Pass new classification for enriched DB storage
            new_classification=new_class,
        )

        # 5. Create ClickUp task with full classification data
        cu_result = create_clickup_task(
            ticket_data=ticket_data,
            agent_response=result,
            crm=crm,
            zoho_url=zoho_url,
            analysis=new_class,
        )
        if cu_result:
            print(
                f"ClickUp task created for ticket {ticket_id}: "
                f"{cu_result['task_url']}"
            )
        else:
            print(f"No ClickUp task created for ticket {ticket_id} (category may not require one)")

        return result
    except Exception as e:
        print(f"Agent error processing ticket {ticket_id}: {e}")
        return None


def _is_client_reply(conversations_result: dict | None, ticket_id: str = "unknown") -> tuple[bool, dict]:
    """Check if the most recent conversation entry is a client reply.

    Returns (is_client_reply, latest_entry) so callers can inspect the author.
    """
    empty = (False, {})
    if not conversations_result:
        return empty

    data = _unwrap_mcp_result(conversations_result)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list) or not data:
        return empty

    latest = data[0]

    # Skip agent's own output
    content = latest.get("content", "") or ""
    if NOTE_HEADER in content or UPDATE_HEADER in content:
        print(f"Ticket update ignored -- agent's own note (ticket {ticket_id})")
        return (False, latest)

    author = latest.get("author", {}) or {}
    author_email = (author.get("email") or "").lower()
    author_type = (author.get("type") or "").upper()
    visibility = "public" if latest.get("isPublic", True) else "private"

    # A reply is from a client when:
    #   1. author.type is END_USER, OR
    #   2. visibility is public AND author is not a known team member
    is_end_user = author_type == "END_USER"
    is_public_non_team = visibility == "public" and author_email not in TEAM_EMAILS

    if is_end_user or is_public_non_team:
        print(f"Client reply detected from {author_email} on ticket {ticket_id} -- processing")
        return (True, latest)

    reason = f"author.type={author_type}, visibility={visibility}, email={author_email}"
    print(f"Ticket update ignored -- not a client reply ({reason}) (ticket {ticket_id})")
    return (False, latest)


def process_ticket_update(ticket_id: str) -> str | None:
    """Reprocess a ticket after a client reply. Returns agent output or None."""
    try:
        conversations_result = fetch_ticket_conversations(ticket_id)

        is_reply, latest = _is_client_reply(conversations_result, ticket_id)
        if not is_reply:
            return None

        zoho_ticket = fetch_ticket_from_zoho(ticket_id)
        if zoho_ticket:
            fields = _extract_ticket_fields(zoho_ticket)
            contact_name = fields["contact_name"]
            contact_email = fields["contact_email"]
            subject = fields["subject"]
            body = fields["description"]
            extra_context = (
                f"Status: {fields['status']}\n"
                f"Created: {fields['created_time']}"
            )
        else:
            print(f"Zoho fetch failed for update on ticket {ticket_id}")
            return None

        thread_text = _format_conversations(conversations_result)

        crm = fetch_crm_account(contact_email)
        if crm["found"]:
            enrichment_block = (
                "ACCOUNT ENRICHMENT:\n"
                f"Account: {crm['account_name']}\n"
                f"Contact type: Admin\n"
                f"Tier: {crm['tier']}\n"
                f"ARR: {crm['arr']} {crm['currency']}\n"
                f"Account ID: {crm['account_id']}"
            )
        else:
            enrichment_block = (
                "ACCOUNT ENRICHMENT:\n"
                "Contact type: Volunteer\n"
                "Not found in CRM -- treat as volunteer"
            )

        # Language detection
        lang_note = ""
        detected_lang = _detect_language(body) or _detect_language(thread_text)
        if detected_lang:
            lang_note = (
                f"\nNote: ticket content appears to be in {detected_lang} "
                f"-- draft response in {detected_lang}\n"
            )
            print(f"Language detected: {detected_lang} (ticket {ticket_id})")

        user_message = (
            "\u26a0\ufe0f MANDATORY PROCESSING RULE -- READ FIRST:\n"
            "This is a REPROCESSING of an existing ticket.\n"
            "The CLIENT HAS REPLIED with new information.\n"
            "Review the full thread including the new reply.\n"
            "Update your classification and draft if needed.\n"
            "Do NOT treat this as a new ticket.\n"
            "IMPORTANT: Structure your response with two clearly separated sections:\n"
            "1. DRAFT RESPONSE (the client-facing reply, ready to review and send)\n"
            "2. AGENT ANALYSIS (enrichment, classification, notes for the reviewer)\n\n"
            f"{enrichment_block}\n\n"
            f"SOURCE: Zoho Desk (ticket update -- client replied)\n"
            f"Ticket ID: {ticket_id}\n"
            f"Client contact: {contact_name} ({contact_email})\n"
            f"Subject: {subject}\n"
            f"{lang_note}"
            f"\n{extra_context}\n"
            f"\nOriginal ticket body:\n{body}\n\n"
            f"Full conversation thread (newest first):\n{thread_text}"
        )

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        result = response.content[0].text
        print(f"\n{'='*60}")
        print(result)
        print(f"{'='*60}")
        print(f"Agent reprocessed ticket {ticket_id} -- response length: {len(result)} chars\n")

        # Post draft reply via Zoho Desk draftsReply API
        post_draft_reply(ticket_id, result, to_email=contact_email)

        # Also post as internal note for audit trail
        note_content = f"{UPDATE_HEADER}\n\n{result}"
        _zoho_desk_call("ZohoDesk_createTicketComment", {
            "body": {
                "content": note_content,
                "contentType": "plainText",
                "isPublic": False,
                "attachmentIds": [],
            },
            "path_variables": {"ticketId": str(ticket_id)},
            "query_params": {"orgId": str(ZOHO_ORG_ID)},
        })
        print(f"Update note posted to Zoho ticket {ticket_id}")

        return result
    except Exception as e:
        print(f"Agent error processing ticket update {ticket_id}: {e}")
        return None
