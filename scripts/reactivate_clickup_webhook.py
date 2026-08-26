"""
reactivate_clickup_webhook.py

Get the ClickUp status webhook delivering again after ClickUp auto-suspends it.

WHY THIS EXISTS: ClickUp increments fail_count on every failed delivery and
suspends the webhook once that counter reaches 100, then never re-enables it on
its own. The counter is not documented as resetting on success, so a webhook
whose handler occasionally times out is on a slow countdown: it works for
months, dropping the odd event silently, and then falls off a cliff. Each
failure before the cliff is a client email that was never sent.

While suspended it sends NOTHING, not a failed call, not a timeout, zero
requests, so every status
change on the board is silently dropped and the on prod / user education /
needs client info / escalated triggers all stop firing with no trace in the
server logs. ClickUp keeps no delivery log and wipes fail_count when it
suspends, and webhooks created with a personal token do not appear anywhere in
the ClickUp UI, so the health field this script reads is the ONLY place the
outage is visible. Recovery is API-only, which is why this script is the whole
path.

Reactivating does NOT replay dropped events. Any task already sitting in a
trigger status has to be moved out and back in to re-fire it, and doing that
BEFORE reactivating is wasted: the flip is discarded.

Run:
  py scripts/reactivate_clickup_webhook.py             reactivate in place
  py scripts/reactivate_clickup_webhook.py --check     report only, no writes
  py scripts/reactivate_clickup_webhook.py --recreate  replace with a new one

--recreate registers a brand new webhook and deletes the suspended one it
replaced. Use it only if a plain reactivate will not stick. It is not safer or
more durable: a new webhook auto-suspends on exactly the same rule. The new
secret is harmless because /webhook/clickup-status does not verify signatures.
"""

import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = (
    "https://vome-support-agent-production.up.railway.app"
    "/webhook/clickup-status"
)
# main.py routes both taskStatusUpdated and taskAssigneeUpdated, but the live
# subscription has only ever carried the first, so ClickUp assignee changes
# have never synced to Zoho. Add the second deliberately, not as a side effect
# of an outage recovery.
EVENTS = ["taskStatusUpdated"]
SPACE_ID = 90114113004

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env() -> tuple[str, str]:
    """Read the ClickUp token and team ID out of .env."""
    token = os.environ.get("CLICKUP_API_TOKEN", "")
    team = os.environ.get("CLICKUP_TEAM_ID", "")
    env_path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if "=" not in line or line.startswith("#"):
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if key == "CLICKUP_API_TOKEN" and not token:
                    token = value
                elif key == "CLICKUP_TEAM_ID" and not team:
                    team = value
    if not token or not team:
        sys.exit(
            "Missing CLICKUP_API_TOKEN or CLICKUP_TEAM_ID. Run this from the "
            "support-agent directory so it can read .env."
        )
    return token, team


def call(url: str, token: str, body: dict | None = None, method: str = "GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode(errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        sys.exit(
            f"ClickUp API error {e.code} on {method} {url}: "
            f"{e.read().decode(errors='replace')}"
        )
    except Exception as e:
        sys.exit(f"Could not reach the ClickUp API: {e}")


def list_hooks(token: str, team: str) -> list[dict]:
    return call(
        f"https://api.clickup.com/api/v2/team/{team}/webhook", token
    ).get("webhooks", [])


def describe(hook: dict) -> str:
    health = hook.get("health") or {}
    return (
        f"  id      {hook.get('id')}\n"
        f"  status  {health.get('status')}"
        f"  (fail_count {health.get('fail_count')})\n"
        f"  events  {', '.join(hook.get('events') or []) or 'none'}\n"
        f"  target  {hook.get('endpoint')}"
    )


def report(hooks: list[dict]) -> None:
    print(f"Found {len(hooks)} webhook(s):\n")
    for hook in hooks:
        print(describe(hook) + "\n")


def recreate(token: str, team: str, hooks: list[dict]) -> None:
    """Register a fresh webhook, then remove the suspended one it replaces."""
    stale = [
        h for h in hooks
        if h.get("endpoint") == ENDPOINT
        and (h.get("health") or {}).get("status") != "active"
    ]
    print(f"Registering a new webhook on {ENDPOINT} ...")
    created = call(
        f"https://api.clickup.com/api/v2/team/{team}/webhook",
        token,
        {"endpoint": ENDPOINT, "events": EVENTS, "space_id": SPACE_ID},
        method="POST",
    )
    new_id = (created.get("webhook") or {}).get("id") or created.get("id")
    print(f"  created {new_id}")

    # Only ever delete a webhook that cannot deliver anyway. Deleting an
    # active one would take down a working subscription, and leaving a second
    # active one would double-send every event and email clients twice.
    for hook in stale:
        if hook.get("id") == new_id:
            continue
        print(f"Deleting the suspended webhook it replaces: {hook['id']} ...")
        call(
            f"https://api.clickup.com/api/v2/webhook/{hook['id']}",
            token,
            method="DELETE",
        )


def main() -> None:
    check_only = "--check" in sys.argv
    do_recreate = "--recreate" in sys.argv
    token, team = load_env()

    hooks = list_hooks(token, team)
    if not hooks and not do_recreate:
        sys.exit(
            "No webhooks registered on this team at all. The subscription is "
            "gone, not suspended. Re-run with --recreate to register one."
        )
    report(hooks)

    suspended = [
        h for h in hooks
        if (h.get("health") or {}).get("status") != "active"
    ]
    if not suspended and not do_recreate:
        print("All webhooks are already active. Nothing to do.")
        return

    if check_only:
        print(
            f"{len(suspended)} webhook(s) need reactivating. "
            "Re-run without --check."
        )
        return

    if do_recreate:
        recreate(token, team, hooks)
    else:
        for hook in suspended:
            print(f"Reactivating {hook.get('id')} ...")
            call(
                f"https://api.clickup.com/api/v2/webhook/{hook['id']}",
                token,
                {
                    "endpoint": hook.get("endpoint") or ENDPOINT,
                    "events": hook.get("events") or EVENTS,
                    "status": "active",
                },
                method="PUT",
            )

    print("\nVerifying ...\n")
    after = list_hooks(token, team)
    report(after)

    active = [
        h for h in after
        if (h.get("health") or {}).get("status") == "active"
    ]
    if not active:
        sys.exit(
            "Still not active. A plain reactivate did not stick. Re-run with "
            "--recreate to register a replacement webhook instead."
        )
    if len(active) > 1:
        print(
            "WARNING: more than one ACTIVE webhook now points at this "
            "endpoint. Every event will be delivered twice, which means "
            "duplicate client emails. Delete the extra one."
        )
    print(
        "Delivery is on. Reactivation does not replay dropped events: move "
        "each task already sitting in a trigger status out and back in to "
        "re-fire it, one at a time."
    )


if __name__ == "__main__":
    main()
