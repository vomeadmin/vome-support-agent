"""
zoho_desk_api.py

Direct Zoho Desk REST API client for critical-path operations
(ticket creation) that shouldn't depend on the MCP proxy layer.
"""

import os
import threading
import time

import httpx

ZOHO_DESK_CLIENT_ID = os.environ.get("ZOHO_DESK_CLIENT_ID", "")
ZOHO_DESK_CLIENT_SECRET = os.environ.get("ZOHO_DESK_CLIENT_SECRET", "")
ZOHO_DESK_REFRESH_TOKEN = os.environ.get("ZOHO_DESK_REFRESH_TOKEN", "")
ZOHO_ORG_ID = os.environ.get("ZOHO_ORG_ID", "")

_token_lock = threading.Lock()
_access_token: str = ""
_token_expires_at: float = 0


def _refresh_access_token() -> str:
    """Exchange the refresh token for a fresh access token."""
    global _access_token, _token_expires_at

    with _token_lock:
        # Another thread may have refreshed while we waited
        if _access_token and time.time() < _token_expires_at - 60:
            return _access_token

        resp = httpx.post(
            "https://accounts.zoho.com/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": ZOHO_DESK_CLIENT_ID,
                "client_secret": ZOHO_DESK_CLIENT_SECRET,
                "refresh_token": ZOHO_DESK_REFRESH_TOKEN,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if "access_token" not in data:
            raise RuntimeError(
                f"Zoho token refresh failed: {data}"
            )

        _access_token = data["access_token"]
        _token_expires_at = time.time() + data.get(
            "expires_in", 3600
        )
        print("[ZOHO-API] Access token refreshed")
        return _access_token


def _get_token() -> str:
    """Return a valid access token, refreshing if needed."""
    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token
    return _refresh_access_token()


def _api_request(
    method: str,
    path: str,
    json_body: dict | None = None,
    params: dict | None = None,
    retry_on_401: bool = True,
) -> httpx.Response | None:
    """Make an authenticated Zoho Desk API request with auto-retry."""
    token = _get_token()
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "orgId": str(ZOHO_ORG_ID),
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    url = f"https://desk.zoho.com/api/v1{path}"

    try:
        resp = httpx.request(
            method, url,
            json=json_body, params=params,
            headers=headers, timeout=15,
        )

        if resp.status_code == 401 and retry_on_401:
            print("[ZOHO-API] 401 — refreshing token and retrying")
            global _token_expires_at
            _token_expires_at = 0
            return _api_request(
                method, path, json_body, params,
                retry_on_401=False,
            )

        return resp

    except Exception as e:
        print(f"[ZOHO-API] Request error {method} {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def search_contact(email: str) -> dict | None:
    """Find a Zoho Desk contact by email.

    Returns the contact dict or None.
    """
    resp = _api_request(
        "GET", "/contacts/search",
        params={"email": email, "limit": "1"},
    )
    if not resp or resp.status_code != 200:
        return None

    data = resp.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(items, list) and items:
        contact = items[0]
        print(
            f"[ZOHO-API] Found contact {contact.get('id')} "
            f"for {email}"
        )
        return contact
    return None


def create_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    account_id: str | None = None,
) -> dict | None:
    """Create a Zoho Desk contact."""
    payload: dict = {"email": email}
    if first_name:
        payload["firstName"] = first_name
    if last_name:
        payload["lastName"] = last_name
    if account_id:
        payload["accountId"] = account_id

    resp = _api_request("POST", "/contacts", json_body=payload)
    if not resp or resp.status_code not in (200, 201):
        print(
            f"[ZOHO-API] Contact creation failed for {email}: "
            f"{resp.status_code if resp else 'no response'}"
        )
        return None

    data = resp.json()
    print(
        f"[ZOHO-API] Contact created: {data.get('id')} "
        f"for {email}"
    )
    return data


def find_or_create_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    account_id: str | None = None,
) -> dict | None:
    """Look up a contact by email; create one if not found."""
    contact = search_contact(email)
    if contact:
        return contact
    return create_contact(
        email, first_name, last_name, account_id,
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def search_account(name: str) -> dict | None:
    """Find a Zoho Desk account by name.

    Returns the account dict or None.
    """
    resp = _api_request(
        "GET", "/accounts/search",
        params={"searchStr": name, "limit": "1"},
    )
    if not resp or resp.status_code != 200:
        return None

    data = resp.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(items, list) and items:
        account = items[0]
        print(
            f"[ZOHO-API] Found account {account.get('id')} "
            f"— {account.get('accountName')}"
        )
        return account
    return None


def create_account(name: str) -> dict | None:
    """Create a Zoho Desk account."""
    resp = _api_request(
        "POST", "/accounts",
        json_body={"accountName": name},
    )
    if not resp or resp.status_code not in (200, 201):
        print(
            f"[ZOHO-API] Account creation failed for {name}: "
            f"{resp.status_code if resp else 'no response'}"
        )
        return None

    data = resp.json()
    print(
        f"[ZOHO-API] Account created: {data.get('id')} "
        f"— {name}"
    )
    return data


def find_or_create_account(name: str) -> dict | None:
    """Look up an account by name; create one if not found."""
    if not name:
        return None
    account = search_account(name)
    if account:
        return account
    return create_account(name)


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def create_ticket(
    subject: str,
    description: str,
    email: str,
    contact_id: str | None = None,
    account_id: str | None = None,
    department_id: str = "569440000000006907",
    channel: str = "Chat",
    status: str = "Open",
) -> dict | None:
    """Create a Zoho Desk ticket via the REST API.

    When contact_id is provided, the ticket is linked to that contact
    (and their account) so it shows up in client search.

    Returns the full ticket dict on success, None on failure.
    """
    if not all([
        ZOHO_DESK_CLIENT_ID,
        ZOHO_DESK_CLIENT_SECRET,
        ZOHO_DESK_REFRESH_TOKEN,
    ]):
        print("[ZOHO-API] Desk OAuth credentials not configured")
        return None

    payload = {
        "subject": subject,
        "description": description,
        "channel": channel,
        "status": status,
        "departmentId": department_id,
    }

    if contact_id:
        payload["contactId"] = contact_id
    else:
        payload["email"] = email

    if account_id:
        payload["accountId"] = account_id

    resp = _api_request("POST", "/tickets", json_body=payload)

    if not resp or resp.status_code not in (200, 201):
        print(
            f"[ZOHO-API] Ticket creation failed: "
            f"HTTP {resp.status_code if resp else '?'} — "
            f"{resp.text[:500] if resp else 'no response'}"
        )
        return None

    data = resp.json()
    print(
        f"[ZOHO-API] Ticket created: "
        f"#{data.get('ticketNumber')} "
        f"(ID: {data.get('id')})"
    )
    return data


def search_tickets(
    email: str,
    limit: int = 25,
) -> list[dict]:
    """Search Zoho Desk tickets by contact email.

    Returns a list of ticket summary dicts.
    """
    # Primary: search by contact email using the contacts API
    # to find tickets linked to this contact
    contact = search_contact(email)
    if contact:
        contact_id = contact.get("id")
        resp = _api_request(
            "GET",
            f"/contacts/{contact_id}/tickets",
            params={
                "limit": str(limit),
                "sortBy": "createdTime",
            },
        )
        if resp and resp.status_code == 200:
            data = resp.json()
            items = (
                data.get("data", data)
                if isinstance(data, dict) else data
            )
            if isinstance(items, list) and items:
                return _normalize_ticket_list(items)

    # Fallback: full-text search (catches tickets created
    # with just an email string, not linked to a contact)
    resp = _api_request(
        "GET", "/tickets/search",
        params={
            "searchStr": email,
            "limit": str(limit),
            "sortBy": "createdTime",
        },
    )
    if resp and resp.status_code == 200:
        data = resp.json()
        items = (
            data.get("data", data)
            if isinstance(data, dict) else data
        )
        if isinstance(items, list):
            return _normalize_ticket_list(items)

    return []


def _normalize_ticket_list(items: list) -> list[dict]:
    """Normalize raw Zoho ticket dicts into a consistent shape."""
    tickets = []
    for t in items:
        tickets.append({
            "ticket_id": str(t.get("id", "")),
            "ticket_number": str(t.get("ticketNumber", "")),
            "subject": t.get("subject", ""),
            "status": t.get("status", ""),
            "created_time": t.get("createdTime", ""),
            "channel": t.get("channel", ""),
        })
    return tickets


def add_ticket_comment(
    ticket_id: str,
    content: str,
    is_public: bool = False,
) -> dict | None:
    """Add a comment (internal note) to a Zoho Desk ticket."""
    if not ZOHO_DESK_REFRESH_TOKEN:
        return None

    token = _get_token()

    try:
        resp = httpx.post(
            f"https://desk.zoho.com/api/v1/tickets"
            f"/{ticket_id}/comments",
            json={
                "content": content,
                "isPublic": is_public,
            },
            headers={
                "Authorization": f"Zoho-oauthtoken {token}",
                "orgId": str(ZOHO_ORG_ID),
                "Content-Type": "application/json",
            },
            timeout=10,
        )

        if resp.status_code not in (200, 201):
            print(
                f"[ZOHO-API] Comment failed on {ticket_id}: "
                f"HTTP {resp.status_code}"
            )
            return None

        return resp.json()

    except Exception as e:
        print(f"[ZOHO-API] Comment error on {ticket_id}: {e}")
        return None
