"""
reactivate_clickup_webhook.py

Turn the ClickUp status webhook back on after ClickUp auto-suspends it.

WHY THIS EXISTS: ClickUp suspends a webhook after a run of consecutive failed
deliveries and never re-enables it on its own. While suspended, every status
change on the board is silently dropped, so the on prod / user education /
needs client info / escalated triggers all stop firing and nothing tells you.
Reactivation is only possible through the API (webhooks created with a personal
token do not appear anywhere in the ClickUp UI), so this script is the whole
recovery path.

Reactivating does NOT replay the events that were dropped. Any task already
sitting in a trigger status has to be moved out and back in to re-fire.

Run:  py scripts/reactivate_clickup_webhook.py
      py scripts/reactivate_clickup_webhook.py --check    (report only)
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
EVENTS = ["taskStatusUpdated"]

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


def call(url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="PUT" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"ClickUp API error {e.code}: {e.read().decode(errors='replace')}")
    except Exception as e:
        sys.exit(f"Could not reach the ClickUp API: {e}")


def describe(hook: dict) -> str:
    health = hook.get("health") or {}
    return (
        f"  id      {hook.get('id')}\n"
        f"  status  {health.get('status')}"
        f"  (fail_count {health.get('fail_count')})\n"
        f"  events  {', '.join(hook.get('events') or []) or 'none'}\n"
        f"  target  {hook.get('endpoint')}"
    )


def main() -> None:
    check_only = "--check" in sys.argv
    token, team = load_env()

    hooks = call(
        f"https://api.clickup.com/api/v2/team/{team}/webhook", token
    ).get("webhooks", [])
    if not hooks:
        sys.exit(
            "No webhooks registered on this team at all. The subscription is "
            "gone, not suspended, and has to be recreated."
        )

    print(f"Found {len(hooks)} webhook(s):\n")
    for hook in hooks:
        print(describe(hook) + "\n")

    suspended = [
        h for h in hooks
        if (h.get("health") or {}).get("status") != "active"
    ]
    if not suspended:
        print("All webhooks are already active. Nothing to do.")
        return

    if check_only:
        print(f"{len(suspended)} webhook(s) need reactivating. Re-run without --check.")
        return

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
        )

    print("\nVerifying ...\n")
    for hook in call(
        f"https://api.clickup.com/api/v2/team/{team}/webhook", token
    ).get("webhooks", []):
        print(describe(hook) + "\n")

    print(
        "Done. Reactivation does not replay dropped events: move any task "
        "already sitting in a trigger status out and back in to re-fire it."
    )


if __name__ == "__main__":
    main()
