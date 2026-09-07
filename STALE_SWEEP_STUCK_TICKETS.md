# Stale sweep: the tickets that come back every run

Handoff for the cloud project. Live read of Zoho Desk on **2026-09-06**, produced by
re-running `stale_waiting_client_sweeper._assess` in read-only mode against every
ticket currently in "Awaiting Client Response". Nothing was written to Zoho, ClickUp,
Postgres or Slack.

## 1. What the sweep actually reports right now

| Bucket | Count | Does it ever clear itself? |
|---|---|---|
| Parked in "Awaiting Client Response" | 96 | |
| `close` (eligible, would be closed) | **0** | yes, daily job is working |
| `skip_recent` (inside the 30d window) | 58 | yes, ages into `close` |
| `skip_client_replied` -> "Waiting on US" | **28** | **no** |
| `skip_no_timestamp` | **10** | **no** |
| `skip_clickup_busy` (drift) | not measurable locally | see caveats |

The daily job is healthy. It closes everything it is allowed to close, which is why
the `close` count is zero. **The 38 tickets in the two skip buckets are the entire
recurring payload**, and they recur because nothing in the system can ever move them.
`_assess` returns the same verdict every morning, forever.

The Aug 2026 scan recorded 28 `skip_client_replied`, median wait 127 days, worst 310.
Today it is still 28, median 144.5, worst 327. Same tickets, each 19 days older.
Nothing has been acted on, and the report has printed the same list every day since.

## 2. What is actually inside those 38

The "Waiting on US" headline is wrong for 22 of the 28. Only 6 are real.
**[A]** marks a ticket affected by Defect A in section 3.

### Group 1: Auto-responder mistaken for a client reply (11)

The inbound is an out-of-office bounce that landed seconds after our email. The
client never replied. These are not service failures.

| Ticket | Unanswered | Their last msg | Gap after our email | Subject |
|---|---|---|---|---|
| #5820 | 249d | 2025-12-31 | 15s | Youngest age to volunteer here |
| #5959 | 235d | 2026-01-14 | 26s | Creating an opportunity |
| #5881 | 229d | 2026-01-19 | 13s | Difficultés à naviguer dans l'application |
| #6120 | 221d | 2026-01-27 | 10s | vome onboarding |
| #8061 | 65d | 2026-07-03 | 9s | Technical thing |
| #8058 | 60d | 2026-07-07 | 8s | Billing |
| #8091 | 57d | 2026-07-10 | 6s | Application question not populating in profile |
| #8207 | 44d | 2026-07-24 | 10s | Change opportunity site link from Calgary/Edmonton to Edmonton |
| #7704 | 40d | 2026-07-27 | 8s | Volunteer trouble signing in |
| #8443 | 30d | 2026-08-07 | 16s | FW: You're Invited to VMHC 2026! |
| #8638 | 27d | 2026-08-10 | 8s | Kiosk Spinning |

### Group 2: System notification mistaken for a reply (2)

#5970 is a secure-message portal notice whose link expired 2026-01-29. #8288 is an
Outlook "reacted to your message" receipt.

| Ticket | Unanswered | Their last msg | Gap after our email | Subject |
|---|---|---|---|---|
| #5970 | 234d | 2026-01-15 | 724s | application |
| #8288 | 39d | 2026-07-28 | 2041s | Re: Problématiques de connexion avec VOME |

### Group 3: Real reply, but the conversation was already over (9)

The client said thanks, or said they would take it from here. No answer was owed.
The status simply never moved.

| Ticket | Unanswered | Their last msg | Gap after our email | Subject |
|---|---|---|---|---|
| #5186 | 327d | 2025-10-14 | 10h | shift specifics in form |
| #5538 | 290d | 2025-11-20 | 475s | Re: Issues with Database and Chat Feature **[A]** |
| #5602 | 279d | 2025-12-01 | 2d | Volunteer Database - Support - WRHN |
| #5913 | 235d | 2026-01-13 | 1d | URGENT - kiosque qui se déconnecte 1 à 2 fois par jour |
| #6949 | 145d | 2026-04-14 | 1899s | Re: Suggestions **[A]** |
| #6963 | 144d | 2026-04-14 | 4d | FW: [ EXTERNAL ] Re: REMINDER: Shine on Certificate |
| #8415 | 39d | 2026-07-28 | 3h | unable to upload immunization |
| #8533 | 32d | 2026-08-05 | 1h | New Application - discrepancy |
| #8534 | 32d | 2026-08-05 | 1d | Report export: volunteer names, emails, numbers, and shifts |

### Group 4: Genuine unanswered client reply. This is the real list (6)

Each of these sent us something we asked for, or reported the problem persisting,
and got silence. Verified message by message.

| Ticket | Unanswered | Their last msg | Gap after our email | Subject | What they said |
|---|---|---|---|---|---|
| #5517 | 290d | 2025-11-19 | 16h | Issue submitting application | "I exported the debug logs here it is!" (we asked for them) |
| #6516 | 186d | 2026-03-03 | 76s | VOME | answered our latency question, cc'd a colleague |
| #6602 | 173d | 2026-03-17 | 298s | Re: VOME - Base de donnée **[A]** | continued the diagnosis after our URL list |
| #6617 | 173d | 2026-03-17 | 201s | Compte vome désactivé | "Le problème persiste encore même sur un autre ordinateur" |
| #8459 | 39d | 2026-07-29 | 248s | Mobile Chat issues | "Android. 3.5.9" (answered our version question) |
| #8635 | 26d | 2026-08-11 | 16h | For Sam - UMMS Annual Flu Shots | "We had hoped to have it ready by September 1st" (date now passed) |

### Group 5: No resolvable outbound date (10)

The sweep cannot find an email we sent, so it refuses to close on a guessed date.
Four are Defect A (we did reply, the sweep cannot see it). The rest are forwards,
OOO bounces and portal notices that became tickets, several never answered at all.

| Ticket | Age | Created | Assignee | Subject |
|---|---|---|---|---|
| #5438 | 299d | 2025-11-10 | unassigned | Re: Questions from a Centre **[A]** |
| #5503 | 293d | 2025-11-16 | Support | Fwd: New submission from Contact |
| #5541 | 290d | 2025-11-20 | Support | Volunteer Application Error |
| #5537 | 290d | 2025-11-20 | Support | Form Submission Error (never answered, single inbound) |
| #5887 | 240d | 2026-01-08 | unassigned | Re: Vome troubleshooting **[A]** |
| #5974 | 234d | 2026-01-15 | unassigned | Secure Message - University of Maryland Medical System |
| #6132 | 221d | 2026-01-27 | Support | Vome Error Code: 009 |
| #6487 | 190d | 2026-02-27 | unassigned | Out of Office Re: Merging Volunteer Accounts |
| #8598 | 32d | 2026-08-05 | Support | Re: Form link not opening **[A]** |
| #8890 | 9d | 2026-08-27 | Vome Support | Question Vome **[A]** |

## 3. Two code defects that manufacture this residue

### Defect A: `_entry_direction` drops messages Zoho leaves unattributed

`stale_waiting_client_sweeper.py:_entry_direction` classifies a thread using
`author.type` and `author.email`. For any email that arrives through the mailbox
rather than through the Desk agent UI, Zoho returns `author.type: null` and
`author.email: null`, and populates only `fromEmailAddress`, which the function never
reads. Such a thread matches neither the "out" branch nor the `is_client` branch, so
it returns `""` and the message is invisible to the sweep in both directions.

Verified on ticket #8890, raw Zoho payload:

```json
{"type":"thread","direction":"in","createdTime":"2026-08-27T18:55:05.000Z",
 "fromEmailAddress":"\"Ron Segev\"<r.segev@vomevolunteer.co>",
 "author":{"name":"Ron Segev","type":null,"email":null}}
```

Ron answered that client. The sweep cannot see it, finds no outbound thread at all,
and files the ticket as `skip_no_timestamp` forever.

Blast radius across the 96 parked tickets: **23 dropped threads, 13 of them written
by a Vome mailbox, spread over 12 tickets**. Four of the ten `skip_no_timestamp`
tickets are caused by this alone.

Compounding it, `agent.TEAM_EMAILS` only lists `@vomevolunteer.com`:

```python
TEAM_EMAILS = {
    "admin@vomevolunteer.com", "sam@vomevolunteer.com",
    "s.fagen@vomevolunteer.com", "r.segev@vomevolunteer.com",
}
```

The real domain is `@vomevolunteer.co`. Nowhere in the repo is `.co` recognised, so
even once `fromEmailAddress` is read, a teammate's own mailbox still would not match
as team traffic.

### Defect B: an out-of-office autoresponder counts as a client reply

`_last_email_times` takes the newest inbound thread, whatever it is. Eleven of the 28
"Waiting on US" tickets are Zoho recording an OOO bounce that arrived 6 to 26 seconds
after our outbound. The client never wrote anything. The ticket is then pinned in a
bucket the sweep refuses to close, and reported daily as a service failure that does
not exist. The 6 to 26 second gap is the tell, and it is already in the data.

## 4. What we are trying to achieve with auto-closure, and how to treat these

The sweep exists to keep "Awaiting Client Response" honest. A ticket sits there to
mean one thing: **we did our part, the ball is with the client**. Once nobody is
waiting on anything, the ticket is a record of a finished conversation, and leaving
it open costs us a queue nobody trusts and a support metric that measures nothing.

Auto-closure is not a judgement that the work was done well. It is a statement that
the thread is over. That is why the close is quiet (no client email), why the note is
internal, and why a client reply reopens the ticket to Processing. The cost of
closing something we should not have is a reply that puts it straight back in front
of us. The cost of leaving it open is a permanent 96-ticket pile that trains everyone
to ignore the daily report, which is exactly where we are.

### Soft-closing old tickets regardless of who answered last: yes

"Who spoke last" is a proxy for "is somebody still waiting". At 30 days it is a good
proxy. At 300 days it is worthless. Ticket #5186 is 327 days past a client saying
"Maybe I'll brainstorm a solution with my team", and nobody on either side has
thought about it since October 2025. There is no version of the future where
answering that is the right move.

Proposed: a hard age backstop, `STALE_SWEEP_MAX_AGE_DAYS` (suggest **120**), that
closes on total silence in **either** direction, ignoring the last-responder test and
ignoring the `skip_client_replied` / `skip_no_timestamp` buckets. Drift
(`skip_clickup_busy`) stays exempt, because that rule protects live dev work rather
than measuring conversation age.

Two conditions, or the backstop just hides the problem:

1. **Fix Defects A and B first.** Closing on age while the classifier is broken means
   auto-closing tickets Ron actually answered, and we keep manufacturing new residue
   at the same rate. Order matters.
2. **Closing the ticket does not discharge the obligation.** Group 4 is six people who
   sent us something we asked for and got silence, one of them for 290 days. Those get
   an answer from a human before anything closes them, or a short honest note if the
   moment has genuinely passed. Auto-closing them would be the system quietly deleting
   the evidence of its own failure, which is the one outcome that makes this feature
   worse than the pile it replaces.

### Stop repeating, start escalating

The deeper problem is not that 38 tickets are stuck. It is that the report has printed
the same 38 every morning for weeks and nothing happened, which is what a report does
when it repeats without escalating. Anything surviving N consecutive sweeps in the
same bucket should be assigned to a person once, then suppressed from the daily list,
rather than re-listed until it becomes wallpaper.

## 5. Suggested order of work

| # | Change | Clears |
|---|---|---|
| 1 | `_entry_direction`: fall back to `fromEmailAddress` when `author.email` is null; add `@vomevolunteer.co` to `TEAM_EMAILS` | 12 tickets, stops the ongoing drip |
| 2 | Autoresponder detection: inbound under ~60s after our outbound, or matching OOO patterns, is not a client reply | 11 tickets |
| 3 | Human answers Group 4 (6 tickets), starting with #5517, #6516, #6602, #6617 | 6 tickets |
| 4 | `STALE_SWEEP_MAX_AGE_DAYS=120` backstop, closes on age regardless of last responder | Groups 2, 3, aged part of 5 |
| 5 | Escalate-once-then-suppress on repeat offenders | stops the next pile forming |

## 6. Caveats on this data

- Run from a dev machine where `.env` has a placeholder `DATABASE_URL`, so the
  `db_last_action_at` fallback was stubbed out. `skip_no_timestamp` is therefore
  **over-inclusive**: on the host, some of those 10 may resolve a date from Postgres.
  Given they recur in every host run, the fallback evidently is not rescuing them, but
  confirm on the host before acting.
- For the same reason `clickup_task_id` was unavailable, so `skip_clickup_busy`
  (status drift) could not be evaluated at all. That bucket needs a host run.
- Everything driven by Zoho conversation timestamps (Groups 1 to 4, all counts and
  ages) is exact.
- The boundary between "acknowledgement" (Group 3) and "genuine" (Group 4) is a human
  reading of the last inbound message, not a rule. Group 4 was verified message by
  message; treat Group 3 as reviewable.
