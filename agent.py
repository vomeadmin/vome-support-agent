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
from clickup_tasks import create_clickup_task, close_clickup_task

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
        if resp.status_code != 200:
            body_preview = resp.text[:500]
            print(
                f"MCP HTTP {resp.status_code}"
                f" ({tool_name}): {body_preview}"
            )
            _last_mcp_error[0] = (
                f"HTTP {resp.status_code}: {body_preview}"
            )
            return None
        result = resp.json()
        if "error" in result:
            print(
                f"MCP error ({tool_name}): {result['error']}"
            )
            _last_mcp_error[0] = str(result["error"])
            return None
        # Check for isError in the result content
        res = result.get("result", result)
        if isinstance(res, dict) and res.get("isError"):
            content = res.get("content", [])
            err_text = ""
            for block in content:
                if block.get("type") == "text":
                    err_text = block.get("text", "")
                    break
            print(
                f"MCP tool error ({tool_name}): {err_text}"
            )
            _last_mcp_error[0] = err_text or str(res)
            return res  # Return so caller can inspect
        _last_mcp_error[0] = ""
        return res
    except Exception as e:
        print(f"MCP request failed ({tool_name}): {e}")
        _last_mcp_error[0] = str(e)
        return None


# Stores the last MCP error message for surfacing in Slack
_last_mcp_error: list[str] = [""]


def get_last_mcp_error() -> str:
    """Return the last MCP error message (for Slack display)."""
    return _last_mcp_error[0]


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


SOCIAL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "me.com", "msn.com", "mail.com", "ymail.com",
}


def _crm_search_contacts(query_params: dict) -> list | None:
    """Search CRM contacts. Returns list of contacts or None."""
    # Limit fields to prevent response truncation from MCP server
    params = {
        **query_params,
        "fields": "Full_Name,Email,Account_Name,FV_Offering,Offering",
    }
    result = _zoho_crm_call("ZohoCRM_searchRecords", {
        "path_variables": {"module": "Contacts"},
        "query_params": params,
    })
    if _is_auth_error(result):
        return None
    data = _unwrap_mcp_result(result)
    if not data or not isinstance(data, dict):
        return []
    return data.get("data", [])


def fetch_crm_account(
    contact_email: str,
    contact_name: str = "",
) -> dict:
    """Look up a contact in Zoho CRM by email, domain, or name."""
    not_found = {"found": False, "contact_type": "volunteer"}

    if not contact_email:
        print("CRM step 1: no email provided — skipping")
        return not_found

    try:
        # Step 1a — exact email match
        contacts = _crm_search_contacts({"email": contact_email})
        if contacts is None:
            print("CRM auth error — falling back to Desk")
            return _fetch_desk_fallback(contact_email)

        if contacts:
            print(f"CRM step 1a: exact email match ({contact_email})")
        else:
            print(f"CRM step 1a: no exact match ({contact_email})")

        # Step 1b — domain search (skip social email providers)
        if not contacts:
            domain = contact_email.split("@")[-1].lower()
            if domain not in SOCIAL_DOMAINS:
                print(f"CRM step 1b: trying domain {domain}")
                domain_contacts = _crm_search_contacts(
                    {"email": domain}
                )
                if domain_contacts:
                    contacts = domain_contacts
                    print(
                        f"CRM step 1b: domain match — "
                        f"found {len(contacts)} contact(s)"
                    )
                else:
                    print(f"CRM step 1b: no domain match")
            else:
                print(f"CRM step 1b: social domain, skipping")

        # Step 1c — name search as last resort
        if not contacts and contact_name:
            name_parts = contact_name.strip().split()
            if len(name_parts) >= 2:
                last = name_parts[-1]
                print(f"CRM step 1c: trying name '{last}'")
                name_contacts = _crm_search_contacts(
                    {"word": last}
                )
                if name_contacts:
                    contacts = name_contacts
                    print(
                        f"CRM step 1c: name match — "
                        f"found {len(contacts)} contact(s)"
                    )
                else:
                    print(f"CRM step 1c: no name match")

        if not contacts:
            print(f"CRM: all lookups exhausted — not found")
            return not_found

        contact = contacts[0]
        contact_name = contact.get("Full_Name", "unknown")
        contact_id = str(contact.get("id", ""))
        print(f"CRM step 1: found contact {contact_name} ({contact_id})")

        account_info = contact.get("Account_Name") or {}
        account_name = account_info.get("name")
        account_id = str(account_info.get("id", "")) if account_info.get("id") else None

        # Use the highest tier across all contacts at this account
        tier_rank = {
            "ultimate": 4, "enterprise": 3, "pro": 2, "recruit": 1,
        }
        best_tier = "Unknown"
        best_rank = -1
        for c in contacts:
            c_offering = c.get("FV_Offering") or c.get("Offering")
            if isinstance(c_offering, list):
                c_offering = c_offering[0] if c_offering else None
            c_tier = _normalize_tier(c_offering)
            rank = tier_rank.get(c_tier.lower(), 0)
            if rank > best_rank:
                best_rank = rank
                best_tier = c_tier
        tier = best_tier
        if len(contacts) > 1:
            print(f"CRM: {len(contacts)} contacts found — using highest tier: {tier}")

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
        # Match with optional markdown bold: **FIELD:** or FIELD:
        m = re.search(
            rf"^\*?\*?{re.escape(field)}\*?\*?:\s*\*?\*?(.+)",
            agent_response,
            re.IGNORECASE | re.MULTILINE,
        )
        if not m:
            return ""
        val = m.group(1).strip().lower()
        # Strip trailing markdown bold
        val = val.rstrip("*").strip()
        return val

    raw_category = _extract("CATEGORY")
    # Strip parenthetical notes: "feature request (details...)" -> "feature request"
    raw_category = re.sub(r"\s*\(.*\)$", "", raw_category).strip()
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
    raw_complexity = re.sub(r"\s*\(.*\)$", "", raw_complexity).strip()
    # Also handle "medium -- some note" or "medium - some note"
    raw_complexity = re.split(r"\s+[-—–]", raw_complexity)[0].strip()
    complexity_map = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "very high": "very-high",
        "very-high": "very-high",
    }
    complexity = complexity_map.get(raw_complexity, raw_complexity)

    raw_eng = _extract("ENGINEER TYPE")
    raw_eng = re.sub(r"\s*\(.*\)$", "", raw_eng).strip()
    raw_eng = re.split(r"\s+[-—–]", raw_eng)[0].strip()
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
        "Best,\n\nVome team\nsupport.vomevolunteer.com"
    ),
    (
        "Hi {name}, we've got this and are looking into it. "
        "You'll hear from us soon.\n"
        "Best,\n\nVome team\nsupport.vomevolunteer.com"
    ),
    (
        "Hi {name}, thanks for flagging this. Our team is on it "
        "and we'll get back to you with an update.\n"
        "Best,\n\nVome team\nsupport.vomevolunteer.com"
    ),
    (
        "Hi {name}, this has been received and is being reviewed "
        "by our team. We'll be in touch shortly.\n"
        "Best,\n\nVome team\nsupport.vomevolunteer.com"
    ),
]

_ACK_TEMPLATES_FR = [
    (
        "Bonjour {name}, merci de nous avoir contactes. Nous avons bien "
        "recu votre message et notre equipe l'examine. Nous reviendrons "
        "vers vous sous peu.\n"
        "Cordialement,\n\nVome team\nsupport.vomevolunteer.com"
    ),
    (
        "Bonjour {name}, nous avons bien pris note de votre demande "
        "et notre equipe s'en occupe. Vous aurez de nos nouvelles bientot.\n"
        "Cordialement,\n\nVome team\nsupport.vomevolunteer.com"
    ),
    (
        "Bonjour {name}, merci d'avoir signale ceci. Notre equipe "
        "examine la situation et nous vous tiendrons informe.\n"
        "Cordialement,\n\nVome team\nsupport.vomevolunteer.com"
    ),
    (
        "Bonjour {name}, votre demande a bien ete recue et est "
        "en cours d'examen par notre equipe. Nous vous recontacterons "
        "rapidement.\n"
        "Cordialement,\n\nVome team\nsupport.vomevolunteer.com"
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
    has_attachments: bool = False,
) -> bool:
    """Send an immediate auto-acknowledgment reply to the client.

    Picks a random template, optionally appends info-request for
    low/medium tier clients with sparse tickets.
    """
    # Extract a real first name, or fall back to "there"
    first_name = "there"
    candidate = (contact_name or "").split()[0] if contact_name else ""
    if candidate:
        c_lower = candidate.lower()
        # Must look like a person's first name:
        # - alphabetic only (no numbers, punctuation)
        # - not a common org/role/generic word
        # - between 2-15 chars (real names)
        # - full contact_name has 2-3 words (first + last)
        not_a_name = {
            "volunteer", "admin", "team", "support", "info",
            "contact", "office", "staff", "service", "help",
            "membership", "scheduling", "coordinator", "vome",
            "center", "centre", "program", "department",
            "general", "director", "manager", "operations",
            "hr", "the", "a", "my", "our", "new", "test",
            "billing", "accounts", "hello", "hi", "dear",
            "benevolat", "benevole", "equipe", "comite",
        }
        name_parts = contact_name.strip().split()
        is_valid = (
            c_lower.isalpha()
            and len(candidate) >= 2
            and len(candidate) <= 15
            and c_lower not in not_a_name
            and len(name_parts) in (2, 3)
        )
        # Also reject if ANY word in the full name is an org signal
        if is_valid:
            full_lower = contact_name.lower()
            org_anywhere = {
                "team", "center", "centre", "program",
                "department", "office", "staff", "volunteer",
                "admin", "service", "committee", "comite",
            }
            if any(w in full_lower for w in org_anywhere):
                is_valid = False
        if is_valid:
            first_name = candidate

    is_french = detected_lang == "French"
    templates = _ACK_TEMPLATES_FR if is_french else _ACK_TEMPLATES_EN
    reply = random.choice(templates).format(name=first_name)

    # Auto-ack is a clean acknowledgment only — never ask for more info.
    # If more details are needed, Sam handles that in a manual reply.

    # LIVE: Send reply to client via email
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


def _get_latest_client_message_full(conversations_result, contact_email: str) -> str:
    """Return the full text of the most recent client message (HTML stripped).

    Used to highlight the current ask in the Claude prompt so classification
    reflects the latest state of the conversation, not the original subject.
    """
    if not conversations_result:
        return ""

    data = _unwrap_mcp_result(conversations_result)
    if isinstance(data, dict):
        data = data.get("data", data.get("conversations", []))
    if not isinstance(data, list) or len(data) < 2:
        return ""

    contact_email_lower = contact_email.lower()

    for entry in data:
        if not isinstance(entry, dict):
            continue
        if not entry.get("isPublic", True):
            continue
        content = entry.get("content", "") or ""
        if NOTE_HEADER in content or UPDATE_HEADER in content:
            continue
        # Skip description thread (original ticket body)
        if entry.get("isDescriptionThread"):
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

        # content may be empty in list responses; fall back to summary
        text = content or entry.get("summary", "") or ""
        clean = re.sub(r"<[^>]+>", "", text).strip()
        if clean:
            return clean[:2000]

    return ""


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


# In-memory dedup: tracks ticket IDs currently being processed or recently
# completed.  Prevents duplicate processing when Zoho fires the webhook
# more than once (network retry, timeout, etc.).
_processing_tickets: dict[str, float] = {}
_DEDUP_TTL_SECONDS = 300  # ignore duplicate webhooks within 5 minutes

def _dedup_check(ticket_id: str) -> bool:
    """Return True if this ticket should be skipped (already processing)."""
    import time
    now = time.time()
    # Clean expired entries
    expired = [
        k for k, v in _processing_tickets.items()
        if now - v > _DEDUP_TTL_SECONDS
    ]
    for k in expired:
        del _processing_tickets[k]
    if ticket_id in _processing_tickets:
        return True
    _processing_tickets[ticket_id] = now
    return False


def _has_agent_reply(conversations_result) -> bool:
    """Check if the agent has already replied to this ticket.

    Returns True if any outbound message from our team exists in the
    conversation history.  This prevents sending duplicate auto-acks
    even if the in-memory dedup is bypassed (e.g. server restart).
    """
    if not conversations_result:
        return False
    data = _unwrap_mcp_result(conversations_result)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list):
        return False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("isDescriptionThread"):
            continue
        author = entry.get("author", {}) or {}
        if (author.get("type") or "").upper() == "AGENT":
            return True
        if entry.get("direction") == "out":
            return True
    return False


def process_ticket(ticket_data: dict) -> str | None:
    """Process a Zoho Desk ticket through the support agent."""
    ticket_id = ticket_data.get("ticket_id", "unknown")

    # Dedup: skip if already processing or recently processed
    if _dedup_check(ticket_id):
        print(f"Duplicate webhook for ticket {ticket_id} -- skipping")
        return None

    # Fetch full ticket details and conversations from Zoho
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    conversations_result = fetch_ticket_conversations(ticket_id)

    # Safety: skip if we've already replied to this ticket
    if _has_agent_reply(conversations_result):
        print(f"Agent already replied to ticket {ticket_id} -- skipping to prevent duplicate")
        return None

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

    # Error reports from Vome frontend — Sam handles these directly
    combined_text = f"{subject} {body or ''}".lower()
    if "vome error report" in combined_text or "error report ===" in combined_text:
        print(f"Vome Error Report detected in ticket {ticket_id} -- leaving as New for Sam")
        return None

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
    crm = fetch_crm_account(contact_email, contact_name)
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

    # Extract latest client message for classification focus
    latest_client_msg = _get_latest_client_message_full(
        conversations_result, contact_email
    )

    latest_msg_block = ""
    if latest_client_msg:
        latest_msg_block = (
            "\n\nLATEST CLIENT MESSAGE (classify based on this):\n"
            "This is the most recent message from the client.\n"
            "Your classification, issue summary, and draft response\n"
            "must reflect what the client is asking for HERE, not\n"
            "what the original ticket subject says. The thread may\n"
            "have evolved -- bugs may have been fixed, new requests\n"
            "may have emerged.\n"
            f"\n{latest_client_msg}\n"
        )

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
        "ISSUE SUMMARY: [one line -- 2-3 sentences plain English:"
        " what the person is asking for NOW based on the latest"
        " message, in context of the thread history. Written as"
        " if briefing a colleague verbally.]\n"
        "SUGGESTED OWNER: [Sanjay / OnlyG / Sam / Either]\n"
        f"{attachment_note}\n"
        f"{enrichment_block}\n\n"
        f"SOURCE: Zoho Desk (webhook trigger)\n"
        f"Ticket ID: {ticket_id}\n"
        f"Client contact: {contact_name} ({contact_email})\n"
        f"Subject: {subject}\n"
        f"{lang_note}"
        f"{latest_msg_block}"
    )
    if extra_context:
        user_message += f"\n{extra_context}\n"
    user_message += (
        f"\nOriginal ticket body:\n{body}\n\n"
        f"Full conversation thread (oldest to newest):\n{thread_text}"
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
                rf"^\*?\*?{re.escape(field)}\*?\*?:\s*\*?\*?(.+)",
                result,
                re.IGNORECASE | re.MULTILINE,
            )
            if not m:
                return ""
            val = m.group(1).strip().rstrip("*").strip()
            return val

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

        # 3. Auto-ack: only for low/medium tier bug/investigation tickets
        #    Skip for: high-tier clients (Sam gives personal response),
        #    feature requests, how-to, billing
        _skip_ack = False
        if not routing["assignee_id"]:
            _skip_ack = True
            _skip_reason = "no engineer assigned, Sam will handle"
        elif new_class["client_tier"] in ("high", "very-high"):
            _skip_ack = True
            _skip_reason = (
                f"high-tier client ({new_class['client_tier']})"
                " — Sam should respond personally"
            )
        elif new_class["category"] in ("feature", "how-to", "billing"):
            _skip_ack = True
            _skip_reason = (
                f"category={new_class['category']}"
                " — not a bug/investigation"
            )

        if not _skip_ack:
            send_auto_acknowledgment(
                ticket_id=ticket_id,
                contact_name=contact_name,
                contact_email=contact_email,
                body=body,
                client_tier=new_class["client_tier"],
                detected_lang=detected_lang,
                has_attachments=attachment_info["has_attachments"],
            )
        else:
            print(f"Auto-ack skipped for ticket {ticket_id} -- {_skip_reason}")

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

        # Route Slack brief to the right channel:
        # OnlyG tickets -> #support-queue-onlyg
        # Sanjay tickets -> #support-queue-sanjay
        # Sam tickets -> #vome-tickets
        if routing["assignee_id"] == ZOHO_AGENT_ONLYG:
            slack_channel = os.environ.get("SLACK_CHANNEL_SUPPORT_QUEUE_ONLYG", "")
        elif routing["assignee_id"] == ZOHO_AGENT_SANJAY:
            slack_channel = os.environ.get("SLACK_CHANNEL_SUPPORT_QUEUE_SANJAY", "")
        else:
            slack_channel = os.environ.get("SLACK_CHANNEL_VOME_TICKETS", "")

        # 5. Create ClickUp task BEFORE Slack brief so we can include the link
        cu_result = create_clickup_task(
            ticket_data=ticket_data,
            agent_response=result,
            crm=crm,
            zoho_url=zoho_url,
            analysis=new_class,
        )
        clickup_task_url = None
        clickup_task_id = None
        if cu_result:
            clickup_task_url = cu_result["task_url"]
            clickup_task_id = cu_result["task_id"]
            print(
                f"ClickUp task created for ticket {ticket_id}: "
                f"{clickup_task_url}"
            )
            # Post ClickUp link as internal note on the Zoho ticket
            _zoho_desk_call("ZohoDesk_createTicketComment", {
                "body": {
                    "content": f"ClickUp task: {clickup_task_url}",
                    "contentType": "plainText",
                    "isPublic": False,
                    "attachmentIds": [],
                },
                "path_variables": {"ticketId": str(ticket_id)},
                "query_params": {"orgId": str(ZOHO_ORG_ID)},
            })
        else:
            print(f"No ClickUp task created for ticket {ticket_id} (category may not require one)")

        thread_ts = send_ticket_brief(
            ticket_id=ticket_id,
            ticket_number=ticket_data.get(
                "ticket_number", ticket_id
            ),
            subject=subject,
            crm=crm,
            agent_response=result,
            clickup_task_url=clickup_task_url,
            clickup_task_id=clickup_task_id,
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
            new_classification=new_class,
            channel=slack_channel,
        )

        # Save ClickUp task ID to database for sync
        if cu_result and thread_ts:
            try:
                from database import update_thread
                update_thread(thread_ts, clickup_task_id=clickup_task_id)
            except Exception as e:
                print(f"Failed to save ClickUp task ID to DB: {e}")

        return result
    except Exception as e:
        print(f"Agent error processing ticket {ticket_id}: {e}")
        return None


def sync_zoho_to_clickup(ticket_id: str) -> None:
    """Sync a Zoho ticket's status/assignee changes to ClickUp.

    Called when a Zoho ticket update webhook fires. Handles:
    - Ticket reassigned to Sam/Ron -> close ClickUp task (Sam took over)
    - Ticket closed on Zoho -> close ClickUp task
    """
    from database import get_thread_by_ticket_id, update_thread

    # Look up the ClickUp task ID from our database
    thread_info = get_thread_by_ticket_id(ticket_id)
    if not thread_info:
        return
    thread_ts, thread_data = thread_info
    clickup_task_id = thread_data.get("clickup_task_id")
    if not clickup_task_id:
        return

    # Fetch current ticket state from Zoho
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    if not zoho_ticket:
        return
    ticket = _unwrap_mcp_result(zoho_ticket) or {}
    status = (ticket.get("status") or "").lower().strip()
    assignee_id = ticket.get("assigneeId") or ""

    # Closed on Zoho -> close on ClickUp
    if status in ("closed", "resolved"):
        print(f"Zoho ticket {ticket_id} is {status} -- closing ClickUp task {clickup_task_id}")
        close_clickup_task(clickup_task_id)
        update_thread(thread_ts, status="closed")
        return

    # Reassigned to Sam or Ron (not an engineer) -> close ClickUp task
    engineer_ids = {ZOHO_AGENT_SANJAY, ZOHO_AGENT_ONLYG}
    if assignee_id and assignee_id not in engineer_ids:
        print(
            f"Zoho ticket {ticket_id} reassigned to non-engineer "
            f"({assignee_id}) -- closing ClickUp task {clickup_task_id}"
        )
        close_clickup_task(clickup_task_id)
        return

    # Unassigned -> also close ClickUp (Sam will handle)
    if not assignee_id:
        print(
            f"Zoho ticket {ticket_id} unassigned -- "
            f"closing ClickUp task {clickup_task_id}"
        )
        close_clickup_task(clickup_task_id)
        return


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


def _classify_client_reply(reply_content: str) -> dict:
    """Classify a client reply as acknowledgment or substantive.

    Returns {"type": "ack"|"substantive", "summary": str}.
    - ack: simple thank-you, got it, ok, thumbs-up ��� no new info
    - substantive: answers a question, provides details, screenshots,
      steps, emails, new context the engineer needs
    """
    prompt = (
        "Classify this client reply to a support ticket.\n\n"
        f"Reply:\n\"{reply_content}\"\n\n"
        "Return valid JSON only:\n"
        "{\n"
        '  "type": "ack" or "substantive",\n'
        '  "summary": "one-line summary of what the client said"\n'
        "}\n\n"
        "Rules:\n"
        "- ack: thank you, thanks, got it, ok, sounds good,"
        " will do, perfect, great, acknowledged, any simple"
        " confirmation with no new information\n"
        "- substantive: provides new details, answers a"
        " question, shares steps to reproduce, mentions"
        " affected users/emails, describes behavior,"
        " provides screenshots, adds context the team needs"
    )
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        import json as _json
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return _json.loads(raw)
    except Exception as e:
        print(f"Reply classification failed: {e}")
        # Default to substantive to be safe
        return {"type": "substantive", "summary": ""}


def _update_clickup_task_status(task_id: str, status: str) -> bool:
    """Update a ClickUp task's status (e.g. QUEUED, IN PROGRESS)."""
    import httpx as _httpx
    cu_token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not cu_token or not task_id:
        return False
    try:
        r = _httpx.put(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            json={"status": status},
            headers={
                "Authorization": cu_token,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"ClickUp task {task_id} status set to {status}")
        return True
    except Exception as e:
        print(f"ClickUp status update failed ({task_id}): {e}")
        return False


def _append_clickup_task_context(
    task_id: str, new_context: str
) -> bool:
    """Append new context to a ClickUp task's description."""
    import httpx as _httpx
    from datetime import datetime, timezone
    cu_token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not cu_token or not task_id:
        return False
    try:
        # Fetch current description
        r = _httpx.get(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            headers={"Authorization": cu_token},
            timeout=15,
        )
        r.raise_for_status()
        current_desc = r.json().get("description", "")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        updated = (
            f"{current_desc}\n\n"
            f"[Client reply — {date_str}]: {new_context}"
        )
        r2 = _httpx.put(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            json={"description": updated},
            headers={
                "Authorization": cu_token,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r2.raise_for_status()
        print(f"ClickUp task {task_id} description updated")
        return True
    except Exception as e:
        print(f"ClickUp description update failed ({task_id}): {e}")
        return False


def _set_zoho_ticket_status(ticket_id: str, status: str) -> bool:
    """Update the Zoho ticket status."""
    result = _zoho_desk_call("ZohoDesk_updateTicket", {
        "body": {"status": status},
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })
    if not result:
        print(
            f"Failed to set Zoho ticket {ticket_id}"
            f" to {status}"
        )
        return False
    data = _unwrap_mcp_result(result)
    if isinstance(data, dict) and data.get("errorCode"):
        print(
            f"Zoho status update failed for ticket"
            f" {ticket_id}: {data}"
        )
        return False
    print(f"Zoho ticket {ticket_id} status set to {status}")
    return True


def _get_zoho_assignee_status(ticket_id: str) -> str:
    """Determine the correct Zoho status based on current assignee.

    Engineers -> Pending Developer Fix
    Sam/Ron/unassigned -> Processing
    """
    zoho_ticket = fetch_ticket_from_zoho(ticket_id)
    if not zoho_ticket:
        return "Processing"
    ticket = _unwrap_mcp_result(zoho_ticket) or {}
    assignee_id = ticket.get("assigneeId") or ""
    engineer_ids = {ZOHO_AGENT_SANJAY, ZOHO_AGENT_ONLYG}
    if assignee_id in engineer_ids:
        return "Pending Developer Fix"
    return "Processing"


_processing_updates: dict[str, float] = {}

def process_ticket_update(ticket_id: str) -> str | None:
    """Reprocess a ticket after a client reply. Returns agent output or None."""
    # Dedup: Zoho may fire the update webhook multiple times
    import time
    now = time.time()
    expired = [k for k, v in _processing_updates.items() if now - v > _DEDUP_TTL_SECONDS]
    for k in expired:
        del _processing_updates[k]
    if ticket_id in _processing_updates:
        print(f"Duplicate update webhook for ticket {ticket_id} -- skipping")
        return None
    _processing_updates[ticket_id] = now

    try:
        conversations_result = fetch_ticket_conversations(ticket_id)

        is_reply, latest = _is_client_reply(conversations_result, ticket_id)
        if not is_reply:
            return None

        # --- Classify the reply and sync ClickUp/Zoho ---
        from database import get_thread_by_ticket_id, update_thread

        reply_content = (latest.get("content") or "").strip()
        reply_content_clean = re.sub(
            r"<[^>]+>", "", reply_content
        ).strip()

        classification = _classify_client_reply(
            reply_content_clean or "(no text content)"
        )
        reply_type = classification.get("type", "substantive")
        reply_summary = classification.get("summary", "")
        print(
            f"Client reply classified as '{reply_type}'"
            f" on ticket {ticket_id}: {reply_summary}"
        )

        # Look up linked ClickUp task
        thread_info = get_thread_by_ticket_id(ticket_id)
        clickup_task_id = None
        thread_ts = None
        thread_data = {}
        was_waiting = False
        if thread_info:
            thread_ts, thread_data = thread_info
            clickup_task_id = thread_data.get("clickup_task_id")
            was_waiting = (
                thread_data.get("status") == "waiting-client"
            )

        if reply_type == "ack":
            # Simple acknowledgment — restore status, skip reprocessing
            correct_status = _get_zoho_assignee_status(ticket_id)
            _set_zoho_ticket_status(ticket_id, correct_status)
            print(
                f"Ack reply on ticket {ticket_id}"
                f" — Zoho status restored to {correct_status}"
            )
            if was_waiting and thread_ts:
                update_thread(thread_ts, status="open")
            return None

        # Substantive reply — update ClickUp + Zoho
        if clickup_task_id:
            if was_waiting:
                # Re-queue the task for the engineer
                _update_clickup_task_status(
                    clickup_task_id, "QUEUED"
                )
                print(
                    f"ClickUp task {clickup_task_id}"
                    " re-queued (was waiting on client)"
                )
            # Append the client's reply context
            _append_clickup_task_context(
                clickup_task_id,
                reply_summary or reply_content_clean[:500],
            )

        # Set Zoho back to the correct status
        correct_status = _get_zoho_assignee_status(ticket_id)
        _set_zoho_ticket_status(ticket_id, correct_status)

        if was_waiting and thread_ts:
            update_thread(thread_ts, status="open")

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

        crm = fetch_crm_account(contact_email, contact_name)
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

        # Notify in Slack if this was a waiting-client reply
        if was_waiting and thread_ts:
            try:
                from slack_sdk import WebClient
                from slack_sdk.errors import SlackApiError
                _slack_client = WebClient(
                    token=os.environ.get("SLACK_BOT_TOKEN", "")
                )
                zoho_url = (
                    "https://desk.zoho.com/support/"
                    "vomevolunteer/ShowHomePage.do"
                    f"#Cases/dv/{ticket_id}"
                )
                cu_url = (
                    f"https://app.clickup.com/t/"
                    f"{clickup_task_id}"
                ) if clickup_task_id else ""
                link_parts = [f"<{zoho_url}|Zoho>"]
                if cu_url:
                    link_parts.append(f"<{cu_url}|ClickUp>")
                links = " | ".join(link_parts)

                channel = thread_data.get("channel", "")
                if channel:
                    _slack_client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f":incoming_envelope: *Client replied*"
                            f" (was waiting on client)\n"
                            f"{reply_summary}\n\n"
                            f"{links}\n"
                            f"ClickUp task re-queued."
                            " Reply in plain English"
                            " to take action."
                        ),
                    )
                    print(
                        f"Slack notified for client reply"
                        f" on ticket {ticket_id}"
                    )
            except Exception as slack_err:
                print(
                    f"Slack notification failed for"
                    f" ticket {ticket_id}: {slack_err}"
                )

        return result
    except Exception as e:
        print(f"Agent error processing ticket update {ticket_id}: {e}")
        return None
