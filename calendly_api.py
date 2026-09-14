"""
calendly_api.py

Calendly v2 REST client plus webhook signature verification.

Auth is a Personal Access Token (CALENDLY_PAT). Everything here is read-only
except webhook subscription create/delete, which is how /webhook/calendly gets
fed in the first place (Calendly has no UI for webhooks, it is API only).

Docs: https://developer.calendly.com/api-docs
"""

import hashlib
import hmac
import os
import time

import httpx

CALENDLY_PAT = os.environ.get("CALENDLY_PAT", "")
CALENDLY_WEBHOOK_SIGNING_KEY = os.environ.get(
    "CALENDLY_WEBHOOK_SIGNING_KEY", ""
)

API_BASE = "https://api.calendly.com"

# event_type URI -> {"name", "slug", "scheduling_url", "duration"}
# Event type metadata is immutable enough to cache for the process lifetime.
# The slug is the only way to match the calendly.com/d/<x>/<slug> links people
# actually share, and it is NOT in the webhook payload, so we resolve it here.
_event_type_cache: dict[str, dict] = {}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {CALENDLY_PAT}",
        "Content-Type": "application/json",
    }


def _request(
    method: str,
    path: str,
    json_body: dict | None = None,
    params: dict | None = None,
) -> httpx.Response | None:
    """Make an authenticated Calendly API request. Returns None on transport error."""
    if not CALENDLY_PAT:
        print("[CALENDLY-API] CALENDLY_PAT not set, skipping request")
        return None

    url = path if path.startswith("http") else f"{API_BASE}{path}"
    try:
        return httpx.request(
            method, url,
            json=json_body, params=params,
            headers=_headers(), timeout=15,
        )
    except Exception as e:
        print(f"[CALENDLY-API] Request error {method} {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def get_current_user() -> dict | None:
    """Return the PAT owner's user resource, including their organization URI."""
    resp = _request("GET", "/users/me")
    if not resp or resp.status_code != 200:
        print(
            "[CALENDLY-API] /users/me failed: "
            f"{resp.status_code if resp else 'no response'}"
        )
        return None
    return resp.json().get("resource")


def list_organization_members(organization_uri: str) -> list[dict]:
    """Return every member of the organization (used by the setup script)."""
    members: list[dict] = []
    params: dict = {"organization": organization_uri, "count": 100}
    path = "/organization_memberships"
    while True:
        resp = _request("GET", path, params=params)
        if not resp or resp.status_code != 200:
            break
        body = resp.json()
        members.extend(body.get("collection", []))
        next_page = (body.get("pagination") or {}).get("next_page")
        if not next_page:
            break
        path, params = next_page, {}
    return members


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

def get_event_type(uri: str) -> dict:
    """Fetch one event type by URI, cached. Returns {} if it cannot be resolved.

    Callers must tolerate {}: routing falls back to the event type NAME that
    is already in the webhook payload, so a Calendly outage degrades routing
    rather than dropping the booking.
    """
    if not uri:
        return {}
    if uri in _event_type_cache:
        return _event_type_cache[uri]

    resp = _request("GET", uri)
    if not resp or resp.status_code != 200:
        print(
            f"[CALENDLY-API] event type fetch failed for {uri}: "
            f"{resp.status_code if resp else 'no response'}"
        )
        return {}

    resource = resp.json().get("resource") or {}
    info = {
        "uri": resource.get("uri", uri),
        "name": resource.get("name", ""),
        "slug": resource.get("slug", ""),
        "scheduling_url": resource.get("scheduling_url", ""),
        "duration": resource.get("duration", 0),
    }
    _event_type_cache[uri] = info
    return info


def list_event_types(organization_uri: str) -> list[dict]:
    """List every event type in the organization (used by the setup script)."""
    types: list[dict] = []
    params: dict = {"organization": organization_uri, "count": 100}
    path = "/event_types"
    while True:
        resp = _request("GET", path, params=params)
        if not resp or resp.status_code != 200:
            break
        body = resp.json()
        types.extend(body.get("collection", []))
        next_page = (body.get("pagination") or {}).get("next_page")
        if not next_page:
            break
        path, params = next_page, {}
    return types


# ---------------------------------------------------------------------------
# Webhook subscriptions
# ---------------------------------------------------------------------------

def list_webhook_subscriptions(
    organization_uri: str,
    scope: str = "organization",
    user_uri: str = "",
) -> list[dict]:
    params: dict = {
        "organization": organization_uri,
        "scope": scope,
        "count": 100,
    }
    if scope == "user" and user_uri:
        params["user"] = user_uri
    resp = _request("GET", "/webhook_subscriptions", params=params)
    if not resp or resp.status_code != 200:
        print(
            "[CALENDLY-API] list subscriptions failed: "
            f"{resp.status_code if resp else 'no response'} "
            f"{resp.text[:300] if resp else ''}"
        )
        return []
    return resp.json().get("collection", [])


def create_webhook_subscription(
    url: str,
    organization_uri: str,
    events: list[str] | None = None,
    scope: str = "organization",
    user_uri: str = "",
    signing_key: str = "",
) -> dict | None:
    """Create a webhook subscription.

    We pass our own signing_key so the value can live in the environment
    ahead of time instead of being read back out of the API response once.
    """
    body: dict = {
        "url": url,
        "events": events or ["invitee.created", "invitee.canceled"],
        "organization": organization_uri,
        "scope": scope,
    }
    if scope == "user":
        body["user"] = user_uri
    if signing_key:
        body["signing_key"] = signing_key

    resp = _request("POST", "/webhook_subscriptions", json_body=body)
    if not resp or resp.status_code not in (200, 201):
        print(
            "[CALENDLY-API] subscription create failed: "
            f"{resp.status_code if resp else 'no response'} "
            f"{resp.text[:500] if resp else ''}"
        )
        return None
    return resp.json().get("resource")


def delete_webhook_subscription(uri: str) -> bool:
    resp = _request("DELETE", uri)
    ok = bool(resp and resp.status_code in (200, 204))
    if not ok:
        print(
            "[CALENDLY-API] subscription delete failed: "
            f"{resp.status_code if resp else 'no response'}"
        )
    return ok


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

SIGNATURE_TOLERANCE_SECONDS = 300


def parse_signature_header(header: str) -> tuple[str, str]:
    """Split 't=1234567890,v1=abc...' into (timestamp, signature)."""
    timestamp, signature = "", ""
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signature = value
    return timestamp, signature


def verify_signature(
    raw_body: bytes,
    header: str,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Verify the Calendly-Webhook-Signature header.

    Signed payload is "{timestamp}.{raw body}", HMAC-SHA256 with the
    subscription's signing key. Returns True when no key is configured so
    local development works, same convention as _verify_slack_signature().
    """
    if not CALENDLY_WEBHOOK_SIGNING_KEY:
        return True

    timestamp, signature = parse_signature_header(header)
    if not timestamp or not signature:
        print("[CALENDLY-API] signature header missing t or v1")
        return False

    # Reject replays of an old, valid signature.
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        print("[CALENDLY-API] signature timestamp is not an integer")
        return False
    if tolerance_seconds and age > tolerance_seconds:
        print(f"[CALENDLY-API] signature timestamp too old ({int(age)}s)")
        return False

    base = timestamp.encode() + b"." + raw_body
    expected = hmac.new(
        CALENDLY_WEBHOOK_SIGNING_KEY.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
