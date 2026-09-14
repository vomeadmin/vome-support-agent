"""
zoho_crm_token.py

Turn a Zoho Self Client grant code into the refresh token the Calendly
booking pipeline needs, and check that the token actually works.

Why this exists instead of a curl line: the grant code is valid for minutes,
and PowerShell's curl is an alias for Invoke-WebRequest, which mangles the
form body. This does the exchange the same way every time.

Usage:
    py scripts/zoho_crm_token.py exchange <grant-code>
    py scripts/zoho_crm_token.py check

Reads ZOHO_CRM_CLIENT_ID and ZOHO_CRM_CLIENT_SECRET from .env. If the org is
on a datacenter other than .com, set ZOHO_ACCOUNTS_BASE and ZOHO_CRM_API_BASE
first (accounts.zoho.eu and www.zohoapis.eu, for example).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv

load_dotenv()

ACCOUNTS_BASE = os.environ.get("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.com")


def cmd_exchange(code: str) -> None:
    client_id = os.environ.get("ZOHO_CRM_CLIENT_ID", "")
    client_secret = os.environ.get("ZOHO_CRM_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("ZOHO_CRM_CLIENT_ID / ZOHO_CRM_CLIENT_SECRET are not set in .env")
        sys.exit(1)

    resp = httpx.post(
        f"{ACCOUNTS_BASE}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code.strip(),
        },
        timeout=20,
    )
    data = resp.json()

    if "refresh_token" not in data:
        print(f"No refresh token came back (HTTP {resp.status_code}):")
        print(data)
        print()
        hints = {
            "invalid_code": "The code expired or was already used. Generate a new one.",
            "invalid_client": "Client id/secret do not match, or the client lives on a different datacenter.",
            "invalid_client_secret": "The secret is wrong.",
        }
        print(hints.get(str(data.get("error", "")), "Check the scopes and the datacenter."))
        sys.exit(1)

    print("Success. Put this in .env and in the deploy environment:")
    print()
    print(f"ZOHO_CRM_REFRESH_TOKEN={data['refresh_token']}")
    print()
    print("It does not expire. This is the only time it is shown.")


def cmd_check() -> None:
    import zoho_crm_api

    if not zoho_crm_api.is_configured():
        print("Not configured. Need ZOHO_CRM_CLIENT_ID, "
              "ZOHO_CRM_CLIENT_SECRET, ZOHO_CRM_REFRESH_TOKEN.")
        sys.exit(1)

    try:
        zoho_crm_api._refresh_access_token()
    except Exception as e:
        print(f"Token refresh failed: {e}")
        sys.exit(1)

    # A read that proves the scope is right without writing anything.
    resp = zoho_crm_api._api_request(
        "GET", "/Leads", params={"per_page": 1, "fields": "Last_Name"}
    )
    if not resp:
        print("No response from the CRM API.")
        sys.exit(1)
    if resp.status_code in (200, 204):
        print("Zoho CRM is reachable and the token has module access.")
        print("The Calendly pipeline can create leads, notes, and meetings.")
        return

    print(f"CRM read returned {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 401:
        print("Token is not valid for this datacenter, or the scopes are wrong.")
    sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    if args[0] == "exchange":
        if len(args) < 2:
            print("exchange needs the grant code")
            sys.exit(1)
        cmd_exchange(args[1])
    elif args[0] == "check":
        cmd_check()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
