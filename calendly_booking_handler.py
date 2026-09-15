"""
calendly_booking_handler.py

Turns a Calendly booking into three things:

  1. A Zoho CRM record. Match on email first, then on exact first plus last
     name (the duplicate guard), then on organization name. Create a Lead
     only when all three miss.
  2. A CRM Meeting on that record, always, even when the record already
     existed, plus a Note holding the Calendly questionnaire answers.
  3. A Slack post. Bookings on the SDR's event types go to her channel,
     everything else goes to the general bookings channel.

Calendly has no "invitee.updated" event. A reschedule arrives as
invitee.canceled (rescheduled=true) on the old invitee plus invitee.created
carrying old_invitee on the new one. We suppress the cancel post and render
the pair as one "rescheduled" message.
"""

import os
from datetime import datetime, timezone

from slack_sdk import WebClient

import calendly_api
import zoho_crm_api
from database import (
    get_calendly_booking,
    mark_calendly_booking_canceled,
    save_calendly_booking,
)

_slack = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))

# Every booking lands here unless it is routed to the SDR channel.
CHANNEL_BOOKINGS = os.environ.get("SLACK_CHANNEL_CALENDLY_BOOKINGS", "")
# The SDR's channel. Bookings on her event types go here instead.
CHANNEL_SDR = os.environ.get("SLACK_CHANNEL_CALENDLY_SDR", "")

# Comma separated matchers deciding what counts as an SDR booking. Each token
# is compared against the event type name, its slug, its UUID, and (with a
# "host:" prefix) the host's email. Example:
#   CALENDLY_SDR_EVENT_TYPES=demo-meeting-vome,discovery-call-vome
CALENDLY_SDR_EVENT_TYPES = [
    t.strip().lower()
    for t in os.environ.get("CALENDLY_SDR_EVENT_TYPES", "").split(",")
    if t.strip()
]
# Set true to mirror SDR bookings into the general channel as well.
CALENDLY_SDR_ALSO_MAIN = (
    os.environ.get("CALENDLY_SDR_ALSO_MAIN", "false").lower() == "true"
)

# Ron gets an at-mention when a booking produces a brand new lead.
RON_SLACK_USER_ID = os.environ.get("RON_SLACK_USER_ID", "")
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "America/Montreal")

# Questionnaire prompts that hold an organization name.
_ORG_QUESTION_HINTS = (
    "organization", "organisation", "company", "nonprofit",
    "non-profit", "employer", "org name", "which org",
)

# Fallback dedup when DATABASE_URL is not configured (local runs).
_seen_events: set[str] = set()


# ---------------------------------------------------------------------------
# Payload normalization
# ---------------------------------------------------------------------------

def _split_name(payload: dict) -> tuple[str, str]:
    """Calendly only guarantees `name`. first_name/last_name are often null."""
    first = (payload.get("first_name") or "").strip()
    last = (payload.get("last_name") or "").strip()
    if first or last:
        return first, last

    full = (payload.get("name") or "").strip()
    if not full:
        return "", ""
    parts = full.split()
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], " ".join(parts[1:])


def extract_org(questions: list[dict], email: str = "") -> str:
    """Pull an organization name out of the questionnaire answers."""
    for qa in questions or []:
        question = (qa.get("question") or "").lower()
        answer = (qa.get("answer") or "").strip()
        if not answer:
            continue
        if any(hint in question for hint in _ORG_QUESTION_HINTS):
            return answer
    return ""


def normalize_booking(body: dict) -> dict:
    """Flatten a Calendly webhook body into the fields this module needs."""
    payload = body.get("payload") or {}
    scheduled = payload.get("scheduled_event") or {}
    memberships = scheduled.get("event_memberships") or []
    host = memberships[0] if memberships else {}
    location = scheduled.get("location") or {}
    questions = payload.get("questions_and_answers") or []
    first, last = _split_name(payload)

    event_type_uri = scheduled.get("event_type") or ""
    # The slug is not in the payload and it is what the shared
    # calendly.com/d/<code>/<slug> links are named after, so resolve it.
    event_type = calendly_api.get_event_type(event_type_uri)

    email = (payload.get("email") or "").strip()

    return {
        "event": body.get("event", ""),
        "invitee_uri": payload.get("uri", ""),
        "email": email,
        "first_name": first,
        "last_name": last,
        "name": (payload.get("name") or f"{first} {last}").strip(),
        "org": extract_org(questions, email),
        "questions": [
            {
                "question": (qa.get("question") or "").strip(),
                "answer": (qa.get("answer") or "").strip(),
            }
            for qa in questions
        ],
        "timezone": payload.get("timezone") or "",
        "status": payload.get("status") or "",
        "rescheduled": bool(payload.get("rescheduled")),
        "old_invitee": payload.get("old_invitee") or "",
        "cancel_url": payload.get("cancel_url") or "",
        "reschedule_url": payload.get("reschedule_url") or "",
        "cancellation": payload.get("cancellation") or {},
        "event_uri": scheduled.get("uri") or payload.get("event") or "",
        "event_type_uri": event_type_uri,
        "event_type_name": scheduled.get("name") or event_type.get("name", ""),
        "event_type_slug": event_type.get("slug", ""),
        "start_time": scheduled.get("start_time") or "",
        "end_time": scheduled.get("end_time") or "",
        "location": _location_text(location),
        "host_name": host.get("user_name") or "",
        "host_email": host.get("user_email") or "",
        "host_uri": host.get("user") or "",
        # Collective event types have several hosts. Routing and display both
        # need all of them, or an SDR listed second is invisible.
        "host_names": ", ".join(
            m.get("user_name") or "" for m in memberships if m.get("user_name")
        ),
        "host_emails": [
            (m.get("user_email") or "").lower()
            for m in memberships if m.get("user_email")
        ],
    }


def _location_text(location: dict) -> str:
    """Render Calendly's polymorphic location object as one line."""
    if not location:
        return ""
    kind = location.get("type") or ""
    for key in ("join_url", "location", "text", "additional_info"):
        if location.get(key):
            return f"{kind}: {location[key]}" if kind else str(location[key])
    return kind


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _matches_token(booking: dict, token: str) -> bool:
    if token.startswith("host:"):
        wanted = token[5:].strip()
        hosts = booking.get("host_emails") or [
            (booking.get("host_email") or "").lower()
        ]
        return wanted in hosts
    candidates = {
        (booking.get("event_type_name") or "").lower(),
        (booking.get("event_type_slug") or "").lower(),
        (booking.get("event_type_uri") or "").lower(),
        (booking.get("event_type_uri") or "").rsplit("/", 1)[-1].lower(),
    }
    return token in {c for c in candidates if c}


def is_sdr_booking(booking: dict) -> bool:
    return any(
        _matches_token(booking, token) for token in CALENDLY_SDR_EVENT_TYPES
    )


def route_channels(booking: dict) -> list[str]:
    """Which Slack channels this booking should be posted to."""
    if is_sdr_booking(booking) and CHANNEL_SDR:
        channels = [CHANNEL_SDR]
        if CALENDLY_SDR_ALSO_MAIN and CHANNEL_BOOKINGS:
            channels.append(CHANNEL_BOOKINGS)
        return channels
    return [CHANNEL_BOOKINGS] if CHANNEL_BOOKINGS else []


# ---------------------------------------------------------------------------
# CRM matching
# ---------------------------------------------------------------------------

def resolve_crm_record(booking: dict) -> dict:
    """Find, or create, the CRM record this booking belongs to.

    Order is email, then exact first plus last name, then organization name.
    A name match reuses the record but is flagged in Slack, because it is a
    guess: the booker used an address the CRM has never seen.

    An organization match does NOT reuse a record. A colleague at a known
    account is still a new person, so we create the lead and just carry the
    company name over.
    """
    result = {
        "module": "",
        "id": "",
        "name": booking.get("name", ""),
        "match": "none",
        "created": False,
        "warnings": [],
        "org_match": "",
    }

    email = booking.get("email", "")

    # 1. Email, contacts before leads: a converted contact is the better home.
    for module in ("Contacts", "Leads"):
        try:
            record = zoho_crm_api.search_by_email(module, email)
        except Exception as e:
            print(f"[CALENDLY] {module} email search failed: {e}")
            record = None
        if record:
            result.update({
                "module": module,
                "id": str(record.get("id", "")),
                "name": _record_name(record) or result["name"],
                "match": "email",
            })
            return result

    # 2. Exact name. Only trusted when it is unambiguous.
    first, last = booking.get("first_name", ""), booking.get("last_name", "")
    if last:
        for module in ("Contacts", "Leads"):
            try:
                matches = zoho_crm_api.search_by_name(module, first, last)
            except Exception as e:
                print(f"[CALENDLY] {module} name search failed: {e}")
                matches = []
            if len(matches) == 1:
                record = matches[0]
                result.update({
                    "module": module,
                    "id": str(record.get("id", "")),
                    "name": _record_name(record) or result["name"],
                    "match": "name",
                })
                result["warnings"].append(
                    f"Matched by name, not email. Booking used {email}, "
                    f"CRM has {record.get('Email') or 'no email'}. "
                    "Confirm this is the same person."
                )
                return result
            if len(matches) > 1:
                result["warnings"].append(
                    f"{len(matches)} {module} share this name. "
                    "Created a new lead rather than guessing."
                )

    # 3. Organization. Informs the new lead, does not replace it.
    org = booking.get("org", "")
    if org:
        try:
            account = zoho_crm_api.search_accounts_by_name(org)
            if account:
                result["org_match"] = (
                    f"Account: {account.get('Account_Name') or org}"
                )
            else:
                lead = zoho_crm_api.search_leads_by_company(org)
                if lead:
                    result["org_match"] = f"Existing leads at {org}"
        except Exception as e:
            print(f"[CALENDLY] org search failed: {e}")

    # 4. Create the lead.
    try:
        details = zoho_crm_api.create_lead(
            first_name=first,
            last_name=last,
            email=email,
            company=org,
            description=_lead_description(booking),
        )
    except Exception as e:
        print(f"[CALENDLY] lead creation failed: {e}")
        details = None

    if details and details.get("id"):
        result.update({
            "module": "Leads",
            "id": str(details["id"]),
            "match": "created",
            "created": True,
        })
    else:
        result["warnings"].append(
            "Could not create the CRM lead. Add it by hand."
        )
    return result


def _record_name(record: dict) -> str:
    full = " ".join(
        p for p in [record.get("First_Name"), record.get("Last_Name")] if p
    ).strip()
    return full or (record.get("Full_Name") or "")


def _lead_description(booking: dict) -> str:
    lines = [
        f"Created from a Calendly booking: {booking.get('event_type_name', '')}",
        f"Booked for: {_format_time_range(booking)}",
    ]
    if booking.get("host_name"):
        lines.append(f"Host: {booking['host_name']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _zone(name: str):
    """Resolve a timezone name, degrading to UTC rather than raising."""
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        pass
    try:
        import pytz
        return pytz.timezone(name)
    except Exception:
        return timezone.utc


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_text(booking: dict) -> str:
    start, end = _parse(booking.get("start_time", "")), _parse(booking.get("end_time", ""))
    if not start or not end:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hr" if hours == 1 else f"{hours} hrs"
    return f"{minutes} mins"


def _format_time_range(booking: dict) -> str:
    """Local time first, invitee time second when they differ."""
    start = _parse(booking.get("start_time", ""))
    if not start:
        return "time unknown"

    local = start.astimezone(_zone(DISPLAY_TIMEZONE))
    text = local.strftime("%a %b %d, %Y at %I:%M %p").replace(" 0", " ")
    duration = _duration_text(booking)
    if duration:
        text = f"{text} ({duration})"

    invitee_tz = booking.get("timezone", "")
    if invitee_tz and invitee_tz != DISPLAY_TIMEZONE:
        their = start.astimezone(_zone(invitee_tz))
        text += (
            f", {their.strftime('%I:%M %p').lstrip('0')} for them "
            f"({invitee_tz})"
        )
    return text


def questionnaire_text(booking: dict) -> str:
    """The questionnaire rendered for the CRM note."""
    lines = []
    for qa in booking.get("questions", []):
        if not qa.get("question") and not qa.get("answer"):
            continue
        lines.append(f"Q: {qa['question']}")
        lines.append(f"A: {qa['answer'] or '(no answer)'}")
        lines.append("")
    return "\n".join(lines).strip()


def _note_body(booking: dict) -> str:
    parts = [
        f"Event: {booking.get('event_type_name', '')}",
        f"When: {_format_time_range(booking)}",
        f"Invitee: {booking.get('name', '')} <{booking.get('email', '')}>",
    ]
    if booking.get("org"):
        parts.append(f"Organization: {booking['org']}")
    if booking.get("host_name"):
        parts.append(f"Host: {booking['host_name']}")
    if booking.get("location"):
        parts.append(f"Location: {booking['location']}")

    qa = questionnaire_text(booking)
    if qa:
        parts.append("")
        parts.append("Questionnaire")
        parts.append(qa)
    if booking.get("reschedule_url"):
        parts.append("")
        parts.append(f"Reschedule: {booking['reschedule_url']}")
        parts.append(f"Cancel: {booking.get('cancel_url', '')}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

_KIND_HEADERS = {
    "created": "New booking",
    "rescheduled": "Booking rescheduled",
    "canceled": "Booking canceled",
}


def build_blocks(booking: dict, crm: dict, meeting_ok: bool, kind: str) -> tuple[str, list]:
    """Return (fallback_text, blocks) for the Slack post."""
    header = _KIND_HEADERS.get(kind, "Booking")
    event_name = booking.get("event_type_name") or "Calendly meeting"
    who = booking.get("name") or booking.get("email") or "Unknown invitee"

    fallback = f"{header}: {event_name} with {who}, {_format_time_range(booking)}"

    title = f"*{header}: {event_name}*"
    if kind == "created" and crm.get("created") and RON_SLACK_USER_ID:
        title = f"<@{RON_SLACK_USER_ID}> {title}"

    blocks: list = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": title},
    }]

    email = booking.get("email", "")
    who_line = f"<mailto:{email}|{who}>" if email else who
    if booking.get("org"):
        who_line += f" at {booking['org']}"

    fields = [
        {"type": "mrkdwn", "text": f"*Who*\n{who_line}"},
        {"type": "mrkdwn", "text": f"*When*\n{_format_time_range(booking)}"},
    ]
    hosts = booking.get("host_names") or booking.get("host_name")
    if hosts:
        label = "Hosts" if "," in hosts else "Host"
        fields.append(
            {"type": "mrkdwn", "text": f"*{label}*\n{hosts}"}
        )
    if booking.get("location"):
        fields.append({"type": "mrkdwn", "text": f"*Where*\n{booking['location']}"})
    blocks.append({"type": "section", "fields": fields})

    # The CRM line is the point of the whole post, so it stands alone.
    crm_line = _crm_line(crm, meeting_ok, kind)
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": crm_line}})

    qa_preview = _qa_preview(booking)
    if qa_preview:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": qa_preview},
        })

    if kind == "canceled":
        reason = (booking.get("cancellation") or {}).get("reason") or ""
        canceled_by = (booking.get("cancellation") or {}).get("canceled_by") or ""
        if reason or canceled_by:
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"Canceled by {canceled_by or 'unknown'}"
                            + (f": {reason}" if reason else ""),
                }],
            })
    else:
        links = []
        if booking.get("reschedule_url"):
            links.append(f"<{booking['reschedule_url']}|Reschedule>")
        if booking.get("cancel_url"):
            links.append(f"<{booking['cancel_url']}|Cancel>")
        if links:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(links)}],
            })

    for warning in crm.get("warnings", []):
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":warning: {warning}"}],
        })

    return fallback, blocks


def _crm_line(crm: dict, meeting_ok: bool, kind: str) -> str:
    if not crm.get("id"):
        return ":x: *No CRM record.* Create it by hand."

    url = zoho_crm_api.record_url(crm["module"], crm["id"])
    label = crm.get("name") or "CRM record"
    module_label = "Lead" if crm["module"] == "Leads" else "Contact"
    line = f"<{url}|{label}> ({module_label})"

    if crm.get("created"):
        line += " · *new lead created*"
    elif crm.get("match") == "email":
        line += " · existing record"
    elif crm.get("match") == "name":
        line += " · matched by name"

    if crm.get("org_match"):
        line += f" · {crm['org_match']}"

    if meeting_ok:
        verb = {
            "created": "Meeting logged",
            "rescheduled": "Meeting updated",
            "canceled": "Meeting marked canceled",
        }.get(kind, "Meeting logged")
        line += f"\n{verb} in Zoho."
    else:
        line += "\n:warning: Meeting was not written to Zoho."
    return line


def _qa_preview(booking: dict, limit: int = 4) -> str:
    rows = [
        qa for qa in booking.get("questions", [])
        if qa.get("question") and qa.get("answer")
    ]
    if not rows:
        return ""
    lines = ["*From the booking form*"]
    for qa in rows[:limit]:
        answer = qa["answer"].replace("\n", " ")
        if len(answer) > 300:
            answer = answer[:297] + "..."
        lines.append(f"• *{qa['question']}*\n{answer}")
    if len(rows) > limit:
        lines.append(f"_+{len(rows) - limit} more in the CRM note._")
    return "\n".join(lines)


def _post(channels: list[str], text: str, blocks: list) -> str:
    ts = ""
    for channel in channels:
        if not channel:
            continue
        try:
            resp = _slack.chat_postMessage(
                channel=channel, text=text, blocks=blocks,
            )
            ts = ts or resp.get("ts", "")
            print(f"[CALENDLY] Posted booking to {channel}")
        except Exception as e:
            print(f"[CALENDLY] Slack post to {channel} failed: {e}")
    return ts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle_calendly_event(body: dict) -> dict:
    """Process one Calendly webhook body. Never raises."""
    event = body.get("event", "")
    if event not in ("invitee.created", "invitee.canceled"):
        print(f"[CALENDLY] Ignoring event type {event}")
        return {"status": "ignored", "event": event}

    booking = normalize_booking(body)
    invitee_uri = booking.get("invitee_uri", "")
    dedup_key = f"{event}:{invitee_uri}"

    if invitee_uri:
        if dedup_key in _seen_events:
            print(f"[CALENDLY] Duplicate delivery {dedup_key}, skipping")
            return {"status": "duplicate", "invitee": invitee_uri}
        _seen_events.add(dedup_key)
        existing = get_calendly_booking(invitee_uri)
        if existing and existing.get("last_event") == event:
            print(f"[CALENDLY] Already processed {dedup_key}, skipping")
            return {"status": "duplicate", "invitee": invitee_uri}

    if event == "invitee.canceled":
        return _handle_canceled(booking)
    return _handle_created(booking)


def _handle_created(booking: dict) -> dict:
    """A new booking, or the second half of a reschedule."""
    prior = (
        get_calendly_booking(booking["old_invitee"])
        if booking.get("old_invitee") else None
    )
    kind = "rescheduled" if prior else "created"

    if prior and prior.get("crm_record_id"):
        # Keep the person on the record the first booking resolved to.
        crm = {
            "module": prior.get("crm_module") or "Leads",
            "id": prior.get("crm_record_id") or "",
            "name": prior.get("invitee_name") or booking.get("name", ""),
            "match": "reschedule",
            "created": False,
            "warnings": [],
            "org_match": "",
        }
    else:
        crm = resolve_crm_record(booking)

    meeting_id = ""
    meeting_ok = False
    title = f"{booking.get('event_type_name') or 'Meeting'} with {booking.get('name') or booking.get('email')}"

    prior_meeting = (prior or {}).get("crm_meeting_id") or ""
    try:
        if prior_meeting:
            # Reschedule: move the existing meeting rather than stacking a
            # second one on the record.
            details = zoho_crm_api.update_meeting(prior_meeting, {
                "Event_Title": title,
                "Start_DateTime": booking.get("start_time", ""),
                "End_DateTime": booking.get("end_time", ""),
            })
            meeting_ok = bool(details)
            meeting_id = prior_meeting if details else ""
        elif crm.get("id"):
            details = zoho_crm_api.create_meeting(
                title=title,
                start_time=booking.get("start_time", ""),
                end_time=booking.get("end_time", ""),
                module=crm["module"],
                record_id=crm["id"],
                description=_note_body(booking),
                venue=booking.get("location", ""),
            )
            meeting_ok = bool(details)
            meeting_id = str((details or {}).get("id", ""))
    except Exception as e:
        print(f"[CALENDLY] meeting write failed: {e}")

    # The questionnaire note goes on every booking, new record or not.
    if crm.get("id") and (booking.get("questions") or kind == "created"):
        try:
            zoho_crm_api.add_note(
                crm["module"], crm["id"],
                title=f"Calendly {kind}: {booking.get('event_type_name', '')}"[:120],
                content=_note_body(booking),
            )
        except Exception as e:
            print(f"[CALENDLY] note write failed: {e}")

    channels = route_channels(booking)
    text, blocks = build_blocks(booking, crm, meeting_ok, kind)
    ts = _post(channels, text, blocks)

    save_calendly_booking({
        "invitee_uri": booking["invitee_uri"],
        "event_uri": booking.get("event_uri", ""),
        "event_type_uri": booking.get("event_type_uri", ""),
        "event_type_name": booking.get("event_type_name", ""),
        "invitee_email": booking.get("email", ""),
        "invitee_name": booking.get("name", ""),
        "start_time": booking.get("start_time", ""),
        "status": "active",
        "last_event": "invitee.created",
        "crm_module": crm.get("module", ""),
        "crm_record_id": crm.get("id", ""),
        "crm_meeting_id": meeting_id,
        "slack_channel": channels[0] if channels else "",
        "slack_ts": ts,
    })

    return {
        "status": kind,
        "invitee": booking["invitee_uri"],
        "crm": crm,
        "meeting_id": meeting_id,
        "channels": channels,
    }


def _handle_canceled(booking: dict) -> dict:
    """A cancellation. Silent when it is really the first half of a reschedule."""
    prior = get_calendly_booking(booking["invitee_uri"]) or {}

    if booking.get("rescheduled"):
        # The paired invitee.created will post the reschedule.
        mark_calendly_booking_canceled(
            booking["invitee_uri"], last_event="invitee.canceled"
        )
        print("[CALENDLY] Cancel is part of a reschedule, not posting")
        return {"status": "reschedule-cancel", "invitee": booking["invitee_uri"]}

    crm = {
        "module": prior.get("crm_module") or "",
        "id": prior.get("crm_record_id") or "",
        "name": prior.get("invitee_name") or booking.get("name", ""),
        "match": "prior",
        "created": False,
        "warnings": [],
        "org_match": "",
    }
    if not crm["id"]:
        crm["warnings"].append(
            "No stored CRM record for this booking, nothing was updated."
        )

    meeting_ok = False
    meeting_id = prior.get("crm_meeting_id") or ""
    if meeting_id:
        try:
            title = (
                f"CANCELED: {booking.get('event_type_name') or 'Meeting'} "
                f"with {booking.get('name') or booking.get('email')}"
            )
            meeting_ok = bool(
                zoho_crm_api.update_meeting(meeting_id, {"Event_Title": title})
            )
        except Exception as e:
            print(f"[CALENDLY] meeting cancel update failed: {e}")

    if crm.get("id"):
        try:
            reason = (booking.get("cancellation") or {}).get("reason") or ""
            zoho_crm_api.add_note(
                crm["module"], crm["id"],
                title=f"Calendly canceled: {booking.get('event_type_name', '')}"[:120],
                content=(
                    f"{booking.get('name', '')} canceled "
                    f"{booking.get('event_type_name', '')} on "
                    f"{_format_time_range(booking)}."
                    + (f"\nReason: {reason}" if reason else "")
                ),
            )
        except Exception as e:
            print(f"[CALENDLY] cancel note failed: {e}")

    channels = route_channels(booking)
    text, blocks = build_blocks(booking, crm, meeting_ok, "canceled")
    _post(channels, text, blocks)

    mark_calendly_booking_canceled(
        booking["invitee_uri"], last_event="invitee.canceled"
    )
    return {
        "status": "canceled",
        "invitee": booking["invitee_uri"],
        "channels": channels,
    }
