# Calendly bookings into Zoho CRM and Slack

Every Calendly booking becomes a CRM record, a CRM meeting, a CRM note holding
the questionnaire answers, and one Slack post with a link to the record.

```
Calendly  ──POST──>  /webhook/calendly  ──>  match or create in Zoho CRM
                                        ──>  Meeting on that record
                                        ──>  Note with the form answers
                                        ──>  Slack post (general or SDR channel)
```

| Piece | File |
| --- | --- |
| Calendly REST client, signature check | `calendly_api.py` |
| Zoho CRM writes (lead, note, meeting) | `zoho_crm_api.py` |
| The pipeline itself | `calendly_booking_handler.py` |
| Route | `main.py`, `/webhook/calendly` |
| Dedup and reschedule state | `database.py`, `calendly_bookings` table |
| Subscription management | `scripts/setup_calendly_webhook.py` |
| Zoho token exchange and check | `scripts/zoho_crm_token.py` |
| Tests | `test_calendly_booking_handler.py` |

## How a person is matched

In order, stopping at the first hit:

1. **Email** against Contacts, then Leads. Contacts win because a converted
   contact is the better home for the meeting.
2. **Exact first plus last name**, again Contacts then Leads. Used only when
   there is exactly one match, and the Slack post says "matched by name" so a
   human can confirm. Two or more matches means we create a lead instead of
   guessing, and the post says so.
3. **Organization name** from the questionnaire. This does NOT reuse a record.
   A colleague at a known account is still a new person, so the lead is created
   and the account match is only mentioned in Slack and carried into `Company`.
4. **Create a Lead**, source `Calendly`.

The meeting and the note are written in every one of those cases, including
when the record already existed. That was the explicit requirement: book the
meeting whether or not the person is new.

## Reschedules and cancellations

Calendly has no `invitee.updated`. A reschedule arrives as two deliveries:
`invitee.canceled` with `rescheduled: true` on the old invitee, and
`invitee.created` carrying `old_invitee` on the new one.

The handler suppresses the cancel post and renders the pair as one "Booking
rescheduled" message. It also moves the existing CRM meeting rather than
creating a second one, which is why the `calendly_bookings` table exists: the
new invitee only knows the old invitee's URI, not the CRM ids it resolved to.

A real cancellation posts to Slack, prefixes the CRM meeting title with
`CANCELED:`, and adds a note with the cancellation reason.

## Routing

`SLACK_CHANNEL_CALENDLY_BOOKINGS` gets everything by default.

`CALENDLY_SDR_EVENT_TYPES` is a comma separated list of matchers. Each is
compared, case insensitively, against the event type slug, its name, its UUID,
and (with a `host:` prefix) the host's email. Anything that matches goes to
`SLACK_CHANNEL_CALENDLY_SDR` instead.

```
CALENDLY_SDR_EVENT_TYPES=demo-meeting-vome,discovery-call-vome
CALENDLY_SDR_EVENT_TYPES=host:sdr@vomevolunteer.com
```

Set `CALENDLY_SDR_ALSO_MAIN=true` to mirror SDR bookings into the general
channel as well instead of routing them away from it. That is the live
setting: the two SDR event types cover effectively every booking, so
routing them away would leave the general channel empty.

List the real slugs with:

```
py scripts/setup_calendly_webhook.py event-types
```

## Setup

### 1. Calendly Personal Access Token

From <https://calendly.com/integrations/api_webhooks>. Put it in
`CALENDLY_PAT`. Confirm it works:

```
py scripts/setup_calendly_webhook.py whoami
```

### 2. Zoho CRM refresh token

The MCP proxy only reads. Creating leads, notes, and meetings needs a direct
token, separate from the Desk one.

1. <https://api-console.zoho.com>, Self Client, Create (or reuse the existing
   CRM client if it is already a Self Client, so `ZOHO_CRM_CLIENT_ID` and
   `ZOHO_CRM_CLIENT_SECRET` stay valid).
2. Scope: `ZohoCRM.modules.ALL,ZohoCRM.settings.READ`. Duration 10 minutes.
   Generate the code.
3. Exchange it within those 10 minutes. Do not use curl on Windows, the
   PowerShell alias mangles the form body:

```
py scripts/zoho_crm_token.py exchange <grant-code>
```

4. Save the printed `ZOHO_CRM_REFRESH_TOKEN` in `.env` and in the deploy
   environment. It does not expire. Verify with:

```
py scripts/zoho_crm_token.py check
```

If the org is on a datacenter other than `.com`, override `ZOHO_ACCOUNTS_BASE`,
`ZOHO_CRM_API_BASE`, and `ZOHO_CRM_WEB_BASE`.

### 3. Slack

Invite the bot to both channels. It posts with the existing `SLACK_BOT_TOKEN`
and needs no new scopes beyond `chat:write`. Set `RON_SLACK_USER_ID` to get an
at-mention on brand new leads.

### 4. Create the subscription

Deploy first, the URL has to answer before Calendly will accept it.

```
py scripts/setup_calendly_webhook.py create https://<app-host>/webhook/calendly
py scripts/setup_calendly_webhook.py list
```

Organization scope needs an admin or owner on a paid plan. If the create call
returns a permission error, use `--scope user`, which covers only the PAT
owner's bookings, and repeat per host.

The script passes `CALENDLY_WEBHOOK_SIGNING_KEY` from the environment so the
value is known ahead of time. If that variable is empty, Calendly generates a
key and shows it exactly once, in the create output.

## Failure behavior

The endpoint always answers 2xx. A non-2xx makes Calendly retry, and replaying
a booking that already created a lead is worse than a lost log line. Every CRM
call is individually guarded, so Zoho being down still produces a Slack post,
carrying a warning that says what did not get written.

Signature verification is skipped when `CALENDLY_WEBHOOK_SIGNING_KEY` is empty,
matching the Slack webhook convention, so local runs work without it. Set it in
production.

Dedup runs on invitee URI plus event, in process and in the database, so
Calendly retries do not double-book.

## Tests

```
py -m pytest test_calendly_booking_handler.py -q
```

No network. The Zoho client, Slack client, the booking table, and the Calendly
event type lookup are all replaced with fakes.
