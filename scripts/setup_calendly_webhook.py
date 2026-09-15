"""
setup_calendly_webhook.py

Create, list, or delete the Calendly webhook subscription that feeds
/webhook/calendly. Calendly has no webhook UI, so this script is the only
way to wire it up.

Usage:
    py scripts/setup_calendly_webhook.py whoami
    py scripts/setup_calendly_webhook.py event-types
    py scripts/setup_calendly_webhook.py list
    py scripts/setup_calendly_webhook.py create https://your-app/webhook/calendly
    py scripts/setup_calendly_webhook.py delete <subscription-uri>

Needs CALENDLY_PAT in the environment (a Personal Access Token from
https://calendly.com/integrations/api_webhooks). If CALENDLY_WEBHOOK_SIGNING_KEY
is set, create passes it to Calendly so the value already in your environment
is the one used to sign deliveries. Generate one with:
    python -c "import secrets; print(secrets.token_hex(32))"

Organization scope needs an admin or owner on a paid plan. If the create call
comes back with a permission error, fall back to user scope, which only covers
the PAT owner's own bookings:
    py scripts/setup_calendly_webhook.py create <url> --scope user
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import calendly_api  # noqa: E402


def _me() -> tuple[str, str]:
    user = calendly_api.get_current_user()
    if not user:
        print("Could not read /users/me. Check CALENDLY_PAT.")
        sys.exit(1)
    return user.get("uri", ""), user.get("current_organization", "")


def cmd_whoami() -> None:
    user_uri, org_uri = _me()
    print(f"user:         {user_uri}")
    print(f"organization: {org_uri}")


def cmd_event_types() -> None:
    """List every event type, including collective ones.

    Querying by organization alone silently omits collective (team) event
    types, which is exactly the kind you want to route on. They only appear
    under each member, so union the per-member lists and dedupe by URI.
    """
    user_uri, org_uri = _me()

    found: dict[str, dict] = {}
    for et in calendly_api.list_event_types(org_uri):
        found[et.get("uri", "")] = et

    members = calendly_api.list_organization_members(org_uri)
    for member in members:
        member_uri = (member.get("user") or {}).get("uri", "")
        if not member_uri:
            continue
        resp = calendly_api._request(
            "GET", "/event_types", params={"user": member_uri, "count": 100}
        )
        if not resp or resp.status_code != 200:
            continue
        for et in resp.json().get("collection", []):
            found.setdefault(et.get("uri", ""), et)

    if not found:
        print("No event types returned.")
        return

    print(
        f"{len(found)} event type(s) across {len(members)} member(s). "
        "Use the uuid in CALENDLY_SDR_EVENT_TYPES: collective types have no "
        "slug, so uuid or exact name is the only way to match them.\n"
    )
    for et in sorted(found.values(), key=lambda t: (t.get("name") or "").lower()):
        owner = (et.get("profile") or {}).get("name", "")
        pooling = et.get("pooling_type") or "solo"
        print(f"  name:    {et.get('name', '')}")
        print(f"  uuid:    {(et.get('uri') or '').rsplit('/', 1)[-1]}")
        print(f"  slug:    {et.get('slug') or '(none, collective)'}")
        print(f"  owner:   {owner}   kind: {pooling}")
        print(f"  active:  {et.get('active')}   duration: {et.get('duration')} min")
        print()


def cmd_list() -> None:
    _, org_uri = _me()
    for scope in ("organization", "user"):
        user_uri = _me()[0] if scope == "user" else ""
        subs = calendly_api.list_webhook_subscriptions(
            org_uri, scope=scope, user_uri=user_uri
        )
        print(f"--- {scope} scope: {len(subs)} subscription(s) ---")
        for sub in subs:
            print(f"  uri:    {sub.get('uri', '')}")
            print(f"  url:    {sub.get('callback_url', '')}")
            print(f"  events: {', '.join(sub.get('events') or [])}")
            print(f"  state:  {sub.get('state', '')}")
            print()


def cmd_create(url: str, scope: str) -> None:
    user_uri, org_uri = _me()
    signing_key = os.environ.get("CALENDLY_WEBHOOK_SIGNING_KEY", "")
    if not signing_key:
        print(
            "WARNING: CALENDLY_WEBHOOK_SIGNING_KEY is not set. Calendly will "
            "generate one and it is shown ONCE, in the response below. Copy it "
            "into the environment or deliveries cannot be verified."
        )

    sub = calendly_api.create_webhook_subscription(
        url=url,
        organization_uri=org_uri,
        events=["invitee.created", "invitee.canceled"],
        scope=scope,
        user_uri=user_uri,
        signing_key=signing_key,
    )
    if not sub:
        print(
            "Create failed. If this is a permission error, either the plan does "
            "not include webhooks or the PAT owner is not an org admin. Retry "
            "with --scope user."
        )
        sys.exit(1)

    print("Subscription created.")
    print(f"  uri:    {sub.get('uri', '')}")
    print(f"  url:    {sub.get('callback_url', '')}")
    print(f"  events: {', '.join(sub.get('events') or [])}")
    if not signing_key and sub.get("signing_key"):
        print()
        print("SAVE THIS NOW, it is not shown again:")
        print(f"  CALENDLY_WEBHOOK_SIGNING_KEY={sub['signing_key']}")


def cmd_delete(uri: str) -> None:
    print("Deleted." if calendly_api.delete_webhook_subscription(uri) else "Delete failed.")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    command = args[0]
    if command == "whoami":
        cmd_whoami()
    elif command == "event-types":
        cmd_event_types()
    elif command == "list":
        cmd_list()
    elif command == "create":
        if len(args) < 2:
            print("create needs a callback URL")
            sys.exit(1)
        scope = "user" if "--scope" in args and "user" in args else "organization"
        cmd_create(args[1], scope)
    elif command == "delete":
        if len(args) < 2:
            print("delete needs a subscription URI")
            sys.exit(1)
        cmd_delete(args[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
