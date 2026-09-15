"""
zoho_crm_api.py

Direct Zoho CRM REST client for the write operations the MCP proxy does not
expose (create lead, add note, book a meeting).

Reads are still fine through ZOHO_CRM_MCP_URL in agent.py. This module exists
because the Calendly booking flow has to CREATE records, and it must not
depend on a model deciding to call a tool.

Needs its own refresh token (ZOHO_CRM_REFRESH_TOKEN), separate from Desk's.
Scopes: ZohoCRM.modules.ALL, ZohoCRM.settings.READ
"""

import os
import re
import threading
import time
from datetime import datetime

import httpx

ZOHO_CRM_CLIENT_ID = os.environ.get("ZOHO_CRM_CLIENT_ID", "")
ZOHO_CRM_CLIENT_SECRET = os.environ.get("ZOHO_CRM_CLIENT_SECRET", "")
ZOHO_CRM_REFRESH_TOKEN = os.environ.get("ZOHO_CRM_REFRESH_TOKEN", "")

# Datacenter. Desk is on .com for this org, so CRM defaults to the same.
ZOHO_ACCOUNTS_BASE = os.environ.get(
    "ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com"
)
ZOHO_CRM_API_BASE = os.environ.get(
    "ZOHO_CRM_API_BASE", "https://www.zohoapis.com/crm/v2"
)
# Web UI base for the links posted into Slack.
ZOHO_CRM_WEB_BASE = os.environ.get(
    "ZOHO_CRM_WEB_BASE", "https://crm.zoho.com/crm"
)

_token_lock = threading.Lock()
_access_token: str = ""
_token_expires_at: float = 0


def is_configured() -> bool:
    return bool(
        ZOHO_CRM_CLIENT_ID
        and ZOHO_CRM_CLIENT_SECRET
        and ZOHO_CRM_REFRESH_TOKEN
    )


def _refresh_access_token() -> str:
    """Exchange the refresh token for a fresh access token."""
    global _access_token, _token_expires_at

    with _token_lock:
        if _access_token and time.time() < _token_expires_at - 60:
            return _access_token

        resp = httpx.post(
            f"{ZOHO_ACCOUNTS_BASE}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": ZOHO_CRM_CLIENT_ID,
                "client_secret": ZOHO_CRM_CLIENT_SECRET,
                "refresh_token": ZOHO_CRM_REFRESH_TOKEN,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if "access_token" not in data:
            raise RuntimeError(f"Zoho CRM token refresh failed: {data}")

        _access_token = data["access_token"]
        _token_expires_at = time.time() + data.get("expires_in", 3600)
        print("[ZOHO-CRM] Access token refreshed")
        return _access_token


def _get_token() -> str:
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
    """Authenticated Zoho CRM request with one 401 retry."""
    if not is_configured():
        print("[ZOHO-CRM] Not configured, skipping request")
        return None

    try:
        token = _get_token()
    except Exception as e:
        print(f"[ZOHO-CRM] Token error: {e}")
        return None

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    url = f"{ZOHO_CRM_API_BASE}{path}"
    try:
        resp = httpx.request(
            method, url,
            json=json_body, params=params,
            headers=headers, timeout=20,
        )

        if resp.status_code == 401 and retry_on_401:
            print("[ZOHO-CRM] 401, refreshing token and retrying")
            global _token_expires_at
            _token_expires_at = 0
            return _api_request(
                method, path, json_body, params, retry_on_401=False
            )

        return resp
    except Exception as e:
        print(f"[ZOHO-CRM] Request error {method} {path}: {e}")
        return None


def _first_record(resp: httpx.Response | None) -> dict | None:
    """Pull the first record out of a search response. 204 means no match."""
    if not resp or resp.status_code == 204:
        return None
    if resp.status_code != 200:
        print(f"[ZOHO-CRM] Search returned {resp.status_code}: {resp.text[:300]}")
        return None
    try:
        records = resp.json().get("data") or []
    except Exception:
        return None
    return records[0] if records else None


def _all_records(resp: httpx.Response | None) -> list[dict]:
    if not resp or resp.status_code != 200:
        return []
    try:
        return resp.json().get("data") or []
    except Exception:
        return []


def _clean_criteria_value(value: str) -> str:
    """Strip characters that break Zoho's criteria grammar.

    Parentheses and commas are structural in criteria strings, so a company
    called "Habitat (Metro), Inc." would produce an unparseable query.
    """
    return re.sub(r"[(),]", " ", value or "").strip()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_by_email(module: str, email: str) -> dict:
    """Find a Contact or Lead by email, preferring an exact PRIMARY match.

    Zoho's ?email= search matches EVERY email field on a record, Secondary_Email
    included. A colleague who listed this person's address as their secondary
    can therefore outrank the person themselves, and with per_page=1 you never
    find out. That put a booked meeting on the wrong contact once already.

    Returns {"record", "quality", "matches"} where quality is:
      primary   an exact hit on the Email field, the one you want
      secondary the address lives on some other field of that record
      none      nothing matched
    """
    result: dict = {"record": None, "quality": "none", "matches": []}
    if not email:
        return result

    resp = _api_request(
        "GET", f"/{module}/search",
        params={"email": email, "per_page": 10},
    )
    if resp is not None and resp.status_code == 204:
        return result
    records = _all_records(resp)
    result["matches"] = records
    if not records:
        return result

    target = email.strip().lower()
    exact = [
        r for r in records
        if (r.get("Email") or "").strip().lower() == target
    ]

    if exact:
        # Several people can legitimately share a primary address (a shared
        # inbox). Newest wins, and the caller warns about the ambiguity.
        exact.sort(key=lambda r: r.get("Modified_Time") or "", reverse=True)
        result["record"] = exact[0]
        result["quality"] = "primary"
    else:
        result["record"] = records[0]
        result["quality"] = "secondary"

    record = result["record"]
    print(
        f"[ZOHO-CRM] {module} {result['quality']} email match "
        f"{record.get('id')} for {email} "
        f"({len(records)} row(s) matched)"
    )
    return result


def search_by_email(module: str, email: str) -> dict | None:
    """Best matching Contact or Lead for an email, or None."""
    return find_by_email(module, email)["record"]


def search_by_name(module: str, first_name: str, last_name: str) -> list[dict]:
    """Find records by exact first plus last name.

    This is the duplicate guard: someone books with a personal address after
    an earlier record was created under their work address. Returns every
    match so the caller can refuse to merge when the name is ambiguous.
    """
    first = _clean_criteria_value(first_name)
    last = _clean_criteria_value(last_name)
    if not last:
        return []

    if first:
        criteria = f"((Last_Name:equals:{last})and(First_Name:equals:{first}))"
    else:
        criteria = f"(Last_Name:equals:{last})"

    resp = _api_request(
        "GET", f"/{module}/search",
        params={"criteria": criteria, "per_page": 5},
    )
    if resp is not None and resp.status_code == 204:
        return []
    return _all_records(resp)


def search_accounts_by_name(name: str) -> dict | None:
    """Find an Account by organization name."""
    cleaned = _clean_criteria_value(name)
    if not cleaned:
        return None
    resp = _api_request(
        "GET", "/Accounts/search",
        params={"criteria": f"(Account_Name:equals:{cleaned})", "per_page": 1},
    )
    return _first_record(resp)


def search_leads_by_company(name: str) -> dict | None:
    """Find a Lead whose Company matches an organization name."""
    cleaned = _clean_criteria_value(name)
    if not cleaned:
        return None
    resp = _api_request(
        "GET", "/Leads/search",
        params={"criteria": f"(Company:equals:{cleaned})", "per_page": 1},
    )
    return _first_record(resp)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _write_result(resp: httpx.Response | None, label: str) -> dict | None:
    """Unwrap Zoho's {"data":[{"code":"SUCCESS","details":{"id":...}}]} envelope."""
    if not resp:
        print(f"[ZOHO-CRM] {label} failed: no response")
        return None
    if resp.status_code not in (200, 201, 202):
        print(f"[ZOHO-CRM] {label} failed {resp.status_code}: {resp.text[:500]}")
        return None
    try:
        entry = (resp.json().get("data") or [{}])[0]
    except Exception:
        print(f"[ZOHO-CRM] {label} returned unparseable body")
        return None
    if entry.get("code") != "SUCCESS":
        print(f"[ZOHO-CRM] {label} rejected: {entry}")
        return None
    return entry.get("details") or {}


def create_lead(
    first_name: str,
    last_name: str,
    email: str,
    company: str = "",
    description: str = "",
    lead_source: str = "Calendly",
    extra_fields: dict | None = None,
) -> dict | None:
    """Create a Lead. Company is mandatory in Zoho, so it always gets a value."""
    record: dict = {
        "Last_Name": last_name or (email.split("@")[0] if email else "Unknown"),
        "Company": company or (
            email.split("@")[-1] if email else "Unknown"
        ),
        "Lead_Source": lead_source,
    }
    if first_name:
        record["First_Name"] = first_name
    if email:
        record["Email"] = email
    if description:
        record["Description"] = description[:31000]
    if extra_fields:
        record.update(extra_fields)

    resp = _api_request("POST", "/Leads", json_body={"data": [record]})
    details = _write_result(resp, f"lead create for {email or last_name}")
    if details:
        print(f"[ZOHO-CRM] Lead created {details.get('id')} for {email}")
    return details


def add_note(
    module: str,
    record_id: str,
    title: str,
    content: str,
) -> dict | None:
    """Attach a note to a Lead or Contact via the related-list endpoint.

    Using /{module}/{id}/Notes avoids having to set Parent_Id and se_module
    by hand, which is the usual source of silent note failures.
    """
    if not record_id:
        return None
    body = {
        "data": [{
            "Note_Title": title[:120],
            "Note_Content": content[:32000],
        }]
    }
    resp = _api_request(
        "POST", f"/{module}/{record_id}/Notes", json_body=body
    )
    details = _write_result(resp, f"note on {module}/{record_id}")
    if details:
        print(f"[ZOHO-CRM] Note added to {module}/{record_id}")
    return details


def to_zoho_datetime(value: str) -> str:
    """Convert an ISO 8601 timestamp to Zoho's yyyy-MM-ddTHH:mm:ss+HH:mm form.

    Calendly sends UTC with a trailing Z and microseconds, which Zoho rejects.
    """
    if not value:
        return ""
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return value
    stamp = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    offset = parsed.strftime("%z") or "+0000"
    return f"{stamp}{offset[:3]}:{offset[3:]}"


# Meeting_Type is labelled "Reason" in the CRM UI. It is a picklist, so only
# these exact strings are accepted. Read from /settings/fields on 2026-09-15:
#   -None-, Discover Call, Demo, Account Review, Training Session,
#   Onboarding Call, Deal stage, Registration Review, Customer Support,
#   General, Referral Partnership
# ("Discover Call" really is spelled that way in the picklist.)
MEETING_TYPE_VALUES = (
    "Discover Call", "Demo", "Account Review", "Training Session",
    "Onboarding Call", "Deal stage", "Registration Review",
    "Customer Support", "General", "Referral Partnership",
)

# First match wins. Matched against the accent-stripped, lowercased event type
# NAME only, never the slug: "Vome: Training Session" has the slug
# vome-platform-demo, and calling that a Demo would be wrong.
# A title that says nothing useful, "30 Minute Meeting", maps to nothing and
# the field is left empty rather than guessed at.
_MEETING_TYPE_KEYWORDS = (
    ("account review", "Account Review"),
    ("revue de compte", "Account Review"),
    ("onboarding", "Onboarding Call"),
    ("training", "Training Session"),
    ("formation", "Training Session"),
    ("registration", "Registration Review"),
    ("referral", "Referral Partnership"),
    ("partnership", "Referral Partnership"),
    ("partenariat", "Referral Partnership"),
    ("support", "Customer Support"),
    ("soutien", "Customer Support"),
    ("discovery", "Discover Call"),
    ("discover", "Discover Call"),
    ("decouverte", "Discover Call"),
    ("introduction", "Discover Call"),
    ("deminar", "Demo"),
    ("demonstration", "Demo"),
    ("demo", "Demo"),
)


def _strip_accents(text: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def meeting_type_for(event_type_name: str) -> str:
    """Infer the Reason picklist value from a Calendly event type name.

    Returns "" when the title carries no signal. An empty Reason is correct;
    a wrong one is worse than none, because somebody will report on it.
    """
    if not event_type_name:
        return ""
    haystack = _strip_accents(event_type_name).lower()
    for keyword, value in _MEETING_TYPE_KEYWORDS:
        if keyword in haystack:
            return value
    return ""


def create_meeting(
    title: str,
    start_time: str,
    end_time: str,
    module: str,
    record_id: str,
    description: str = "",
    venue: str = "",
    meeting_type: str = "",
) -> dict | None:
    """Create an Event (Meeting) linked to a Lead or Contact.

    Zoho relates meetings differently per module: Contacts go in Who_Id,
    everything else goes in What_Id plus $se_module. If the related-record
    link is rejected we retry once unlinked, because a meeting that exists
    without a link is far better than a booking that silently vanished.
    """
    base: dict = {
        "Event_Title": title[:250],
        "Start_DateTime": to_zoho_datetime(start_time),
        "End_DateTime": to_zoho_datetime(end_time),
    }
    if description:
        base["Description"] = description[:31000]
    if venue:
        base["Venue"] = venue[:250]

    # Reason. Only ever a value from the picklist, and dropped on retry if the
    # org has since renamed its options, because a meeting with no Reason beats
    # no meeting at all.
    typed = dict(base)
    if meeting_type and meeting_type in MEETING_TYPE_VALUES:
        typed["Meeting_Type"] = meeting_type
    elif meeting_type:
        print(f"[ZOHO-CRM] Ignoring unknown Reason value {meeting_type!r}")

    linked = dict(typed)
    if record_id:
        if module == "Contacts":
            linked["Who_Id"] = record_id
        else:
            linked["What_Id"] = record_id
            linked["$se_module"] = module
        linked["Participants"] = [{
            "participant": record_id,
            "type": "contact" if module == "Contacts" else "lead",
        }]

    resp = _api_request("POST", "/Events", json_body={"data": [linked]})
    details = _write_result(resp, f"meeting create ({module}/{record_id})")
    if details:
        print(f"[ZOHO-CRM] Meeting created {details.get('id')} for {module}/{record_id}")
        return details

    # Reason is the likeliest thing an org changes under us, so shed it first
    # and keep the record link, which matters far more.
    if typed.get("Meeting_Type") and record_id:
        print("[ZOHO-CRM] Retrying meeting create without the Reason field")
        retry = {k: v for k, v in linked.items() if k != "Meeting_Type"}
        resp = _api_request("POST", "/Events", json_body={"data": [retry]})
        details = _write_result(resp, "meeting create (no Reason retry)")
        if details:
            return details

    if not record_id:
        return None

    print("[ZOHO-CRM] Retrying meeting create without record link")
    resp = _api_request("POST", "/Events", json_body={"data": [base]})
    return _write_result(resp, "meeting create (unlinked retry)")


def update_meeting(event_id: str, fields: dict) -> dict | None:
    """Patch an existing Event, used for reschedules and cancellations."""
    if not event_id:
        return None
    record = dict(fields)
    record["id"] = event_id
    for key in ("Start_DateTime", "End_DateTime"):
        if record.get(key):
            record[key] = to_zoho_datetime(record[key])
    resp = _api_request("PUT", "/Events", json_body={"data": [record]})
    return _write_result(resp, f"meeting update {event_id}")


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def record_url(module: str, record_id: str) -> str:
    """Deep link to a record in the Zoho CRM web UI."""
    if not record_id:
        return ""
    return f"{ZOHO_CRM_WEB_BASE}/tab/{module}/{record_id}"
