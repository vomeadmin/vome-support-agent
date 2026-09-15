"""
test_calendly_booking_handler.py

Tests for the Calendly booking pipeline.

The behaviours worth protecting:
  * a booking never creates a duplicate lead when the person is already in CRM
  * a name-only match is used but flagged, never silently trusted
  * an organization match does NOT reuse someone else's record
  * a reschedule moves the existing meeting instead of stacking a second one
  * the cancel half of a reschedule posts nothing to Slack
  * SDR event types route to the SDR channel, everything else to the general one
  * a meeting and a questionnaire note are written even for an existing record

Run:  py -m pytest test_calendly_booking_handler.py -q
"""

import hashlib
import hmac
import json
import time

import pytest

import calendly_api
import calendly_booking_handler as handler


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCRM:
    """Stands in for zoho_crm_api, recording every write."""

    def __init__(self, contacts=None, leads=None, name_matches=None,
                 account=None, company_lead=None, secondary=None,
                 duplicates=None):
        self.contacts = contacts or {}
        self.leads = leads or {}
        # email -> record that carries it on a NON primary field
        self.secondary = secondary or {}
        # email -> extra records sharing the same primary address
        self.duplicates = duplicates or {}
        self.name_matches = name_matches or {}
        self.account = account
        self.company_lead = company_lead
        self.created_leads = []
        self.notes = []
        self.meetings = []
        self.meeting_updates = []
        self._next_id = 1000

    def find_by_email(self, module, email):
        store = self.contacts if module == "Contacts" else self.leads
        record = store.get(email)
        if record:
            matches = [record] + self.duplicates.get(email, [])
            return {"record": record, "quality": "primary", "matches": matches}
        record = self.secondary.get((module, email))
        if record:
            return {"record": record, "quality": "secondary", "matches": [record]}
        return {"record": None, "quality": "none", "matches": []}

    def search_by_email(self, module, email):
        return self.find_by_email(module, email)["record"]

    def search_by_name(self, module, first, last):
        return self.name_matches.get((module, first, last), [])

    def search_accounts_by_name(self, name):
        return self.account

    def search_leads_by_company(self, name):
        return self.company_lead

    def create_lead(self, first_name, last_name, email, company="",
                    description="", lead_source="Calendly", extra_fields=None):
        self._next_id += 1
        record = {
            "id": str(self._next_id), "First_Name": first_name,
            "Last_Name": last_name, "Email": email, "Company": company,
        }
        self.created_leads.append(record)
        return {"id": record["id"]}

    def add_note(self, module, record_id, title, content):
        self.notes.append({
            "module": module, "record_id": record_id,
            "title": title, "content": content,
        })
        return {"id": "note-1"}

    def create_meeting(self, title, start_time, end_time, module, record_id,
                       description="", venue="", meeting_type=""):
        self._next_id += 1
        self.meetings.append({
            "id": str(self._next_id), "title": title, "start": start_time,
            "end": end_time, "module": module, "record_id": record_id,
            "meeting_type": meeting_type,
        })
        return {"id": str(self._next_id)}

    def meeting_type_for(self, name):
        import zoho_crm_api
        return zoho_crm_api.meeting_type_for(name)

    def update_meeting(self, event_id, fields):
        self.meeting_updates.append({"id": event_id, "fields": fields})
        return {"id": event_id}

    def record_url(self, module, record_id):
        return f"https://crm.zoho.com/crm/tab/{module}/{record_id}"


class FakeSlack:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, channel, text, blocks=None):
        self.posts.append({"channel": channel, "text": text, "blocks": blocks})
        return {"ts": f"ts-{len(self.posts)}"}


class FakeStore:
    """In-memory stand-in for the calendly_bookings table."""

    def __init__(self):
        self.rows = {}

    def get(self, invitee_uri):
        return self.rows.get(invitee_uri)

    def save(self, booking):
        self.rows[booking["invitee_uri"]] = dict(booking)

    def cancel(self, invitee_uri, last_event="invitee.canceled"):
        row = self.rows.get(invitee_uri)
        if row:
            row["status"] = "canceled"
            row["last_event"] = last_event


MAIN_CHANNEL = "C0C1MF2NMDJ"
SDR_CHANNEL = "C0C1D71K6S1"


@pytest.fixture
def env(monkeypatch):
    """Wire the handler to fakes and a known channel config."""
    crm, slack, store = FakeCRM(), FakeSlack(), FakeStore()

    monkeypatch.setattr(handler, "zoho_crm_api", crm)
    monkeypatch.setattr(handler, "_slack", slack)
    monkeypatch.setattr(handler, "get_calendly_booking", store.get)
    monkeypatch.setattr(handler, "save_calendly_booking", store.save)
    monkeypatch.setattr(handler, "mark_calendly_booking_canceled", store.cancel)
    monkeypatch.setattr(handler, "CHANNEL_BOOKINGS", MAIN_CHANNEL)
    monkeypatch.setattr(handler, "CHANNEL_SDR", SDR_CHANNEL)
    monkeypatch.setattr(
        handler, "CALENDLY_SDR_EVENT_TYPES",
        ["demo-meeting-vome", "discovery-call-vome"],
    )
    monkeypatch.setattr(handler, "CALENDLY_SDR_ALSO_MAIN", False)
    monkeypatch.setattr(handler, "RON_SLACK_USER_ID", "U_RON")

    # Event type lookup is the one network call in normalize_booking.
    monkeypatch.setattr(
        handler.calendly_api, "get_event_type",
        lambda uri: {
            "uri": uri, "name": "Demo Meeting [VOME]",
            "slug": "demo-meeting-vome", "duration": 60,
        },
    )

    handler._seen_events.clear()
    return {"crm": crm, "slack": slack, "store": store}


def make_payload(
    email="jane@habitatmetro.org",
    name="Jane Doe",
    event="invitee.created",
    invitee_uri="https://api.calendly.com/scheduled_events/E1/invitees/I1",
    old_invitee="",
    rescheduled=False,
    event_type_name="Demo Meeting [VOME]",
    questions=None,
    memberships=None,
):
    if questions is None:
        questions = [
            {
                "question": "What organization are you with?",
                "answer": "Habitat Metro",
                "position": 0,
            },
            {
                "question": "How many volunteers do you manage?",
                "answer": "About 400 a year",
                "position": 1,
            },
        ]
    return {
        "event": event,
        "created_at": "2026-09-13T12:00:00.000000Z",
        "payload": {
            "uri": invitee_uri,
            "email": email,
            "name": name,
            "first_name": None,
            "last_name": None,
            "status": "canceled" if event == "invitee.canceled" else "active",
            "timezone": "America/Los_Angeles",
            "rescheduled": rescheduled,
            "old_invitee": old_invitee or None,
            "cancel_url": "https://calendly.com/cancellations/abc",
            "reschedule_url": "https://calendly.com/reschedulings/abc",
            "questions_and_answers": questions,
            "cancellation": (
                {"canceled_by": "Jane Doe", "reason": "Conflict"}
                if event == "invitee.canceled" else None
            ),
            "scheduled_event": {
                "uri": "https://api.calendly.com/scheduled_events/E1",
                "name": event_type_name,
                "event_type": "https://api.calendly.com/event_types/ET1",
                "start_time": "2026-09-21T18:00:00.000000Z",
                "end_time": "2026-09-21T19:00:00.000000Z",
                "location": {"type": "zoom", "join_url": "https://zoom.us/j/1"},
                "event_memberships": memberships or [{
                    "user": "https://api.calendly.com/users/U1",
                    "user_email": "ron@vomevolunteer.com",
                    "user_name": "Ron",
                }],
            },
        },
    }


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _sign(body: bytes, key: str, ts: str) -> str:
    digest = hmac.new(
        key.encode(), ts.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={digest}"


def test_signature_accepts_a_valid_header(monkeypatch):
    monkeypatch.setattr(calendly_api, "CALENDLY_WEBHOOK_SIGNING_KEY", "secret")
    body = json.dumps({"event": "invitee.created"}).encode()
    header = _sign(body, "secret", str(int(time.time())))
    assert calendly_api.verify_signature(body, header) is True


def test_signature_rejects_a_tampered_body(monkeypatch):
    monkeypatch.setattr(calendly_api, "CALENDLY_WEBHOOK_SIGNING_KEY", "secret")
    header = _sign(b'{"event":"a"}', "secret", str(int(time.time())))
    assert calendly_api.verify_signature(b'{"event":"b"}', header) is False


def test_signature_rejects_a_replayed_timestamp(monkeypatch):
    monkeypatch.setattr(calendly_api, "CALENDLY_WEBHOOK_SIGNING_KEY", "secret")
    body = b'{"event":"invitee.created"}'
    old = str(int(time.time()) - 4000)
    assert calendly_api.verify_signature(body, _sign(body, "secret", old)) is False


def test_signature_skipped_when_no_key_configured(monkeypatch):
    monkeypatch.setattr(calendly_api, "CALENDLY_WEBHOOK_SIGNING_KEY", "")
    assert calendly_api.verify_signature(b"{}", "") is True


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_pulls_out_the_fields_we_post(env):
    booking = handler.normalize_booking(make_payload())
    assert booking["email"] == "jane@habitatmetro.org"
    assert booking["first_name"] == "Jane"
    assert booking["last_name"] == "Doe"
    assert booking["org"] == "Habitat Metro"
    assert booking["host_name"] == "Ron"
    assert booking["event_type_slug"] == "demo-meeting-vome"
    assert len(booking["questions"]) == 2
    assert "zoom" in booking["location"]


def test_multi_word_last_name_survives_the_split(env):
    booking = handler.normalize_booking(make_payload(name="Ana Maria de Souza"))
    assert booking["first_name"] == "Ana"
    assert booking["last_name"] == "Maria de Souza"


def test_duration_reads_from_the_scheduled_event(env):
    booking = handler.normalize_booking(make_payload())
    assert handler._duration_text(booking) == "1 hr"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_sdr_event_type_routes_to_the_sdr_channel(env):
    booking = handler.normalize_booking(make_payload())
    assert handler.route_channels(booking) == [SDR_CHANNEL]


def test_other_event_types_route_to_the_general_channel(env, monkeypatch):
    monkeypatch.setattr(
        handler.calendly_api, "get_event_type",
        lambda uri: {"name": "Support Call", "slug": "support-call"},
    )
    booking = handler.normalize_booking(
        make_payload(event_type_name="Support Call")
    )
    assert handler.route_channels(booking) == [MAIN_CHANNEL]


def test_sdr_can_also_mirror_into_the_general_channel(env, monkeypatch):
    monkeypatch.setattr(handler, "CALENDLY_SDR_ALSO_MAIN", True)
    booking = handler.normalize_booking(make_payload())
    assert handler.route_channels(booking) == [SDR_CHANNEL, MAIN_CHANNEL]


def test_host_email_can_route_too(env, monkeypatch):
    monkeypatch.setattr(
        handler, "CALENDLY_SDR_EVENT_TYPES", ["host:ron@vomevolunteer.com"]
    )
    booking = handler.normalize_booking(make_payload())
    assert handler.route_channels(booking) == [SDR_CHANNEL]


# ---------------------------------------------------------------------------
# CRM matching
# ---------------------------------------------------------------------------

def test_existing_contact_is_reused_and_no_lead_is_created(env):
    env["crm"].contacts["jane@habitatmetro.org"] = {
        "id": "777", "First_Name": "Jane", "Last_Name": "Doe",
    }
    result = handler.handle_calendly_event(make_payload())

    assert result["crm"]["match"] == "email"
    assert result["crm"]["id"] == "777"
    assert env["crm"].created_leads == []
    # The meeting is still booked, which is the whole point.
    assert len(env["crm"].meetings) == 1
    assert env["crm"].meetings[0]["module"] == "Contacts"


def test_name_match_is_used_but_flagged(env):
    env["crm"].name_matches[("Leads", "Jane", "Doe")] = [
        {"id": "888", "First_Name": "Jane", "Last_Name": "Doe",
         "Email": "jane@oldjob.org"},
    ]
    result = handler.handle_calendly_event(make_payload())

    assert result["crm"]["match"] == "name"
    assert result["crm"]["id"] == "888"
    assert env["crm"].created_leads == []
    assert any("Matched by name" in w for w in result["crm"]["warnings"])


def test_ambiguous_name_creates_a_lead_instead_of_guessing(env):
    env["crm"].name_matches[("Leads", "Jane", "Doe")] = [
        {"id": "1", "Last_Name": "Doe"}, {"id": "2", "Last_Name": "Doe"},
    ]
    result = handler.handle_calendly_event(make_payload())

    assert result["crm"]["created"] is True
    assert any("share this name" in w for w in result["crm"]["warnings"])


def test_org_match_notes_the_account_but_still_creates_the_person(env):
    env["crm"].account = {"id": "acc-1", "Account_Name": "Habitat Metro"}
    result = handler.handle_calendly_event(make_payload())

    assert result["crm"]["created"] is True
    assert "Habitat Metro" in result["crm"]["org_match"]
    assert env["crm"].created_leads[0]["Company"] == "Habitat Metro"


def test_new_booking_creates_lead_meeting_and_note(env):
    result = handler.handle_calendly_event(make_payload())

    assert len(env["crm"].created_leads) == 1
    assert len(env["crm"].meetings) == 1
    assert len(env["crm"].notes) == 1

    note = env["crm"].notes[0]
    assert "How many volunteers do you manage?" in note["content"]
    assert "About 400 a year" in note["content"]
    assert result["status"] == "created"


def test_note_is_written_even_when_the_contact_already_existed(env):
    env["crm"].contacts["jane@habitatmetro.org"] = {
        "id": "777", "First_Name": "Jane", "Last_Name": "Doe",
    }
    handler.handle_calendly_event(make_payload())

    assert len(env["crm"].notes) == 1
    assert env["crm"].notes[0]["record_id"] == "777"


# ---------------------------------------------------------------------------
# Slack output
# ---------------------------------------------------------------------------

def test_slack_post_links_the_crm_record(env):
    handler.handle_calendly_event(make_payload())

    post = env["slack"].posts[0]
    assert post["channel"] == SDR_CHANNEL
    rendered = json.dumps(post["blocks"])
    assert "https://crm.zoho.com/crm/tab/Leads/1001" in rendered
    assert "new lead created" in rendered
    assert "<@U_RON>" in rendered
    assert "Habitat Metro" in rendered


def test_existing_record_does_not_ping_ron(env):
    env["crm"].contacts["jane@habitatmetro.org"] = {
        "id": "777", "Last_Name": "Doe",
    }
    handler.handle_calendly_event(make_payload())
    assert "<@U_RON>" not in json.dumps(env["slack"].posts[0]["blocks"])


def test_a_failed_meeting_write_is_surfaced_not_swallowed(env, monkeypatch):
    monkeypatch.setattr(
        env["crm"], "create_meeting",
        lambda **kwargs: None,
    )
    handler.handle_calendly_event(make_payload())
    assert "not written to Zoho" in json.dumps(env["slack"].posts[0]["blocks"])


# ---------------------------------------------------------------------------
# Reschedules and cancellations
# ---------------------------------------------------------------------------

OLD_INVITEE = "https://api.calendly.com/scheduled_events/E0/invitees/I0"


def test_reschedule_moves_the_existing_meeting(env):
    env["store"].rows[OLD_INVITEE] = {
        "invitee_uri": OLD_INVITEE, "crm_module": "Leads",
        "crm_record_id": "555", "crm_meeting_id": "meet-9",
        "invitee_name": "Jane Doe",
    }
    result = handler.handle_calendly_event(make_payload(
        invitee_uri="https://api.calendly.com/scheduled_events/E2/invitees/I2",
        old_invitee=OLD_INVITEE,
    ))

    assert result["status"] == "rescheduled"
    assert env["crm"].created_leads == []
    assert env["crm"].meetings == []
    assert env["crm"].meeting_updates[0]["id"] == "meet-9"
    assert "rescheduled" in json.dumps(env["slack"].posts[0]["blocks"]).lower()


def test_cancel_half_of_a_reschedule_stays_quiet(env):
    env["store"].rows[
        "https://api.calendly.com/scheduled_events/E1/invitees/I1"
    ] = {"crm_record_id": "555", "crm_meeting_id": "meet-9"}

    result = handler.handle_calendly_event(make_payload(
        event="invitee.canceled", rescheduled=True,
    ))

    assert result["status"] == "reschedule-cancel"
    assert env["slack"].posts == []


def test_real_cancellation_posts_and_marks_the_meeting(env):
    env["store"].rows[
        "https://api.calendly.com/scheduled_events/E1/invitees/I1"
    ] = {
        "crm_module": "Leads", "crm_record_id": "555",
        "crm_meeting_id": "meet-9", "invitee_name": "Jane Doe",
    }

    result = handler.handle_calendly_event(
        make_payload(event="invitee.canceled")
    )

    assert result["status"] == "canceled"
    assert env["crm"].meeting_updates[0]["id"] == "meet-9"
    assert "CANCELED" in env["crm"].meeting_updates[0]["fields"]["Event_Title"]
    rendered = json.dumps(env["slack"].posts[0]["blocks"])
    assert "Booking canceled" in rendered
    assert "Conflict" in rendered


# ---------------------------------------------------------------------------
# Delivery safety
# ---------------------------------------------------------------------------

def test_a_retried_delivery_is_not_processed_twice(env):
    payload = make_payload()
    handler.handle_calendly_event(payload)
    result = handler.handle_calendly_event(payload)

    assert result["status"] == "duplicate"
    assert len(env["crm"].created_leads) == 1
    assert len(env["slack"].posts) == 1


def test_unhandled_event_types_are_ignored(env):
    result = handler.handle_calendly_event({"event": "routing_form_submission.created"})
    assert result["status"] == "ignored"
    assert env["slack"].posts == []


def test_crm_failure_still_posts_to_slack(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("Zoho is down")

    monkeypatch.setattr(env["crm"], "search_by_email", boom)
    monkeypatch.setattr(env["crm"], "search_by_name", boom)
    monkeypatch.setattr(env["crm"], "create_lead", boom)

    handler.handle_calendly_event(make_payload())

    rendered = json.dumps(env["slack"].posts[0]["blocks"])
    assert "No CRM record" in rendered


# ---------------------------------------------------------------------------
# Zoho datetime conversion
# ---------------------------------------------------------------------------

def test_calendly_timestamps_convert_to_zoho_format():
    import zoho_crm_api

    assert (
        zoho_crm_api.to_zoho_datetime("2026-09-21T18:00:00.000000Z")
        == "2026-09-21T18:00:00+00:00"
    )


# ---------------------------------------------------------------------------
# Collective (team) event types
#
# These are the real shape of the SDR's two calendars: two hosts, and NO slug,
# because Calendly does not give collective event types one. Matching has to
# fall back to the uuid or the exact name, and host matching has to look past
# the first membership or the second host is invisible.
# ---------------------------------------------------------------------------

COLLECTIVE_HOSTS = [
    {
        "user": "https://api.calendly.com/users/U1",
        "user_email": "r.segev@vomevolunteer.co",
        "user_name": "Ron Segev",
    },
    {
        "user": "https://api.calendly.com/users/U2",
        "user_email": "j.jackson@vomevolunteer.com",
        "user_name": "Jennifer Jackson",
    },
]


@pytest.fixture
def collective(env, monkeypatch):
    """A collective booking: two hosts, no slug from the API."""
    monkeypatch.setattr(handler.calendly_api, "get_event_type", lambda uri: {})
    return env


def test_host_match_finds_a_second_host(collective, monkeypatch):
    monkeypatch.setattr(
        handler, "CALENDLY_SDR_EVENT_TYPES",
        ["host:j.jackson@vomevolunteer.com"],
    )
    booking = handler.normalize_booking(make_payload(memberships=COLLECTIVE_HOSTS))
    assert booking["event_type_slug"] == ""
    assert handler.route_channels(booking) == [SDR_CHANNEL]


def test_uuid_matches_when_there_is_no_slug(collective, monkeypatch):
    monkeypatch.setattr(handler, "CALENDLY_SDR_EVENT_TYPES", ["et1"])
    booking = handler.normalize_booking(make_payload(memberships=COLLECTIVE_HOSTS))
    assert handler.route_channels(booking) == [SDR_CHANNEL]


def test_exact_name_matches_when_there_is_no_slug(collective, monkeypatch):
    monkeypatch.setattr(
        handler, "CALENDLY_SDR_EVENT_TYPES", ["demo meeting [vome]"]
    )
    booking = handler.normalize_booking(make_payload(memberships=COLLECTIVE_HOSTS))
    assert handler.route_channels(booking) == [SDR_CHANNEL]


def test_a_host_who_is_not_on_the_booking_does_not_match(collective, monkeypatch):
    monkeypatch.setattr(
        handler, "CALENDLY_SDR_EVENT_TYPES", ["host:someone.else@vome.com"]
    )
    booking = handler.normalize_booking(make_payload(memberships=COLLECTIVE_HOSTS))
    assert handler.route_channels(booking) == [MAIN_CHANNEL]


def test_slack_lists_every_host(collective):
    handler.handle_calendly_event(make_payload(memberships=COLLECTIVE_HOSTS))
    rendered = json.dumps(collective["slack"].posts[0]["blocks"])
    assert "*Hosts*" in rendered
    assert "Ron Segev, Jennifer Jackson" in rendered


# ---------------------------------------------------------------------------
# Secondary email addresses
#
# Incident 2026-09-15. Lara Hollaway booked a meeting. Angela Loos, a colleague
# at the same organization, carried lhollaway@lscarolinas.net in her
# Secondary_Email field. Zoho's ?email= search matches every email field and
# returned Angela first, per_page=1 took her, and Lara's meeting was filed on
# Angela's record.
# ---------------------------------------------------------------------------

ANGELA = {
    "id": "4693275000117781214", "First_Name": "Angela", "Last_Name": "Loos",
    "Email": "aloos@lscarolinas.net",
    "Secondary_Email": "lhollaway@lscarolinas.net",
}
LARA = {
    "id": "4693275000097043411", "First_Name": "Lara", "Last_Name": "Hollaway",
    "Email": "lhollaway@lscarolinas.net",
}


def test_a_colleagues_secondary_email_never_takes_the_meeting(env):
    """The incident. Angela must not receive Lara's booking."""
    env["crm"].secondary[("Contacts", "lhollaway@lscarolinas.net")] = ANGELA

    result = handler.handle_calendly_event(make_payload(
        email="lhollaway@lscarolinas.net", name="Lara Hollaway",
    ))

    assert result["crm"]["id"] != ANGELA["id"]
    for meeting in env["crm"].meetings:
        assert meeting["record_id"] != ANGELA["id"]
    assert any("secondary address" in w for w in result["crm"]["warnings"])


def test_the_real_person_wins_over_a_secondary_hit(env):
    """With Lara present on her primary address, she is the match."""
    env["crm"].contacts["lhollaway@lscarolinas.net"] = LARA
    env["crm"].secondary[("Contacts", "lhollaway@lscarolinas.net")] = ANGELA

    result = handler.handle_calendly_event(make_payload(
        email="lhollaway@lscarolinas.net", name="Lara Hollaway",
    ))

    assert result["crm"]["id"] == LARA["id"]
    assert result["crm"]["warnings"] == []


def test_a_persons_own_old_address_still_matches(env):
    """Same surname means it is an alias, not a colleague. Use it, but say so."""
    old_record = {
        "id": "555", "First_Name": "Lara", "Last_Name": "Hollaway",
        "Email": "lara@oldemployer.org",
        "Secondary_Email": "lhollaway@lscarolinas.net",
    }
    env["crm"].secondary[("Contacts", "lhollaway@lscarolinas.net")] = old_record

    result = handler.handle_calendly_event(make_payload(
        email="lhollaway@lscarolinas.net", name="Lara Hollaway",
    ))

    assert result["crm"]["id"] == "555"
    assert any("secondary email" in w.lower() for w in result["crm"]["warnings"])


def test_a_shared_primary_address_is_flagged(env):
    """Two people on one inbox. Newest wins, and the post admits the choice."""
    env["crm"].contacts["team@shared.org"] = {
        "id": "1", "Last_Name": "Newer", "Email": "team@shared.org",
    }
    env["crm"].duplicates["team@shared.org"] = [
        {"id": "2", "Last_Name": "Older", "Email": "team@shared.org"},
    ]

    result = handler.handle_calendly_event(make_payload(email="team@shared.org"))

    assert result["crm"]["id"] == "1"
    assert any("share team@shared.org" in w for w in result["crm"]["warnings"])


# ---------------------------------------------------------------------------
# The Reason field (Meeting_Type)
# ---------------------------------------------------------------------------

import zoho_crm_api as _crm


@pytest.mark.parametrize("event_type_name, expected", [
    ("Demo Meeting [VOME]", "Demo"),
    ("[Vome] Live Demo", "Demo"),
    ("Vome Deminar", "Demo"),
    ("D\u00e9monstration Vome", "Demo"),
    ("Discovery Call [VOME]", "Discover Call"),
    ("[Vome] Brief Discovery Call", "Discover Call"),
    ("[Vome] Appel de d\u00e9couverte", "Discover Call"),
    ("Appel d'introduction", "Discover Call"),
    ("[Vome] Account Review", "Account Review"),
    ("Vome: Revue de compte", "Account Review"),
    ("[Vome] Onboarding Meeting", "Onboarding Call"),
    ("[Vome] Training Session", "Training Session"),
    ("Vome: Session de formation", "Training Session"),
    ("Vome Support / Soutien Vome", "Customer Support"),
])
def test_reason_is_inferred_from_the_title(event_type_name, expected):
    assert _crm.meeting_type_for(event_type_name) == expected


@pytest.mark.parametrize("event_type_name", [
    "30 Minute Meeting",
    "15 Minute Meeting",
    "60 Minute Meeting",
    "Investment opportunity: Vome",
    "",
])
def test_an_uninformative_title_leaves_reason_empty(event_type_name):
    """A wrong Reason is worse than none. Somebody will report on this field."""
    assert _crm.meeting_type_for(event_type_name) == ""


def test_every_mapped_value_exists_in_the_picklist():
    """Guards a typo becoming a rejected write."""
    for _, value in _crm._MEETING_TYPE_KEYWORDS:
        assert value in _crm.MEETING_TYPE_VALUES


def test_the_reason_reaches_the_meeting(env):
    handler.handle_calendly_event(make_payload(
        event_type_name="Demo Meeting [VOME]",
    ))
    assert env["crm"].meetings[0]["meeting_type"] == "Demo"


def test_a_generic_title_sends_no_reason(env):
    handler.handle_calendly_event(make_payload(
        event_type_name="30 Minute Meeting",
    ))
    assert env["crm"].meetings[0]["meeting_type"] == ""
