import json
import os
import re
import sys

import anthropic
import httpx
from pathlib import Path

from clickup_tasks import create_clickup_task
from slack_ticket_brief import send_ticket_brief

# Fix Windows console encoding for emoji in system_prompt.md
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

client = anthropic.Anthropic()

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")

ZOHO_MCP_URL = os.environ.get("ZOHO_MCP_URL", "")
ZOHO_MCP_TOKEN = os.environ.get("ZOHO_MCP_TOKEN", "")
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


def _zoho_mcp_call(tool_name: str, arguments: dict) -> dict | None:
    """Send a tools/call request to the Zoho MCP server. Returns result or None."""
    mcp_url = f"{ZOHO_MCP_URL}?key={ZOHO_MCP_TOKEN}"
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
        resp = httpx.post(mcp_url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            print(f"Zoho MCP error ({tool_name}): {result['error']}")
            return None
        return result.get("result", result)
    except Exception as e:
        print(f"Zoho MCP request failed ({tool_name}): {e}")
        return None


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
    result = _zoho_mcp_call("ZohoDesk_getTicket", {
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
    result = _zoho_mcp_call("ZohoDesk_getTicketConversations", {
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

    return {
        "subject": ticket.get("subject", ""),
        "description": ticket.get("description", ""),
        "contact_name": contact_name,
        "contact_email": contact_email,
        "status": ticket.get("status", ""),
        "created_time": ticket.get("createdTime", ""),
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
        result = _zoho_mcp_call("ZohoDesk_searchContacts", {
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

        acct_result = _zoho_mcp_call("ZohoDesk_getAccount", {
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
        result = _zoho_mcp_call("ZohoCRM_Search_Records", {
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

        deal_result = _zoho_mcp_call("ZohoCRM_getRelatedRecords", {
            "path_variables": {
                "parentRecordModule": "Accounts",
                "parentRecord": account_id,
                "relatedList": "Deals",
            },
            "query_params": {
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

    result = _zoho_mcp_call("ZohoDesk_draftsReply", {
        "body": body,
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": str(ZOHO_ORG_ID)},
    })

    if result:
        print(f"Draft reply saved on Zoho ticket {ticket_id} -- success")
        return True
    print(f"Failed to save draft reply on Zoho ticket {ticket_id}")
    return False


def post_to_zoho(ticket_id: str, agent_response: str) -> bool:
    """Post the agent's analysis as an internal note on the Zoho Desk ticket."""
    note_content = f"{NOTE_HEADER}\n\n{agent_response}"

    result = _zoho_mcp_call("ZohoDesk_createTicketComment", {
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

    if result:
        print(f"Internal note posted to Zoho ticket {ticket_id} -- success")
        return True
    return False


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
        "SUGGESTED OWNER: [Sanjay / OnlyG / Sam / Either]\n\n"
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
        print(f"Agent processed ticket {ticket_id} -- response length: {len(result)} chars\n")

        post_to_zoho(ticket_id, result)

        zoho_url = (
            f"https://desk.zoho.com/support/vomevolunteer"
            f"/ShowHomePage.do#Cases/dv/{ticket_id}"
        )

        # Create ClickUp task
        clickup_result = create_clickup_task(
            ticket_data=ticket_data,
            agent_response=result,
            crm=crm,
            zoho_url=zoho_url,
        )
        clickup_task_url = (
            clickup_result["task_url"] if clickup_result else None
        )
        clickup_task_id = (
            clickup_result["task_id"] if clickup_result else None
        )

        attachment_count = 0
        if zoho_ticket:
            raw = _unwrap_mcp_result(zoho_ticket)
            if isinstance(raw, dict):
                try:
                    attachment_count = int(raw.get("attachmentCount") or 0)
                except (ValueError, TypeError):
                    attachment_count = 0

        # Extract structured fields from Claude's response for the Slack brief
        def _extract_field(field: str) -> str:
            m = re.search(
                rf"^{re.escape(field)}:\s*(.+)",
                result,
                re.IGNORECASE | re.MULTILINE,
            )
            return m.group(1).strip() if m else ""

        latest_reply = _get_latest_client_reply(
            conversations_result, contact_email
        )

        # Send Slack brief — primary interface for Sam
        send_ticket_brief(
            ticket_id=ticket_id,
            ticket_number=ticket_data.get("ticket_number", ticket_id),
            subject=subject,
            crm=crm,
            agent_response=result,
            clickup_task_url=clickup_task_url,
            clickup_task_id=clickup_task_id,
            zoho_ticket_url=zoho_url,
            attachment_count=attachment_count,
            contact_name=contact_name,
            contact_email=contact_email,
            issue_summary=_extract_field("ISSUE SUMMARY"),
            latest_reply=latest_reply,
            timing=_extract_field("TIMING"),
            priority=_extract_field("PRIORITY"),
            suggested_owner=_extract_field("SUGGESTED OWNER"),
        )

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
        _zoho_mcp_call("ZohoDesk_createTicketComment", {
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
