"""Quick script to check ClickUp custom field option IDs."""
from dotenv import load_dotenv
load_dotenv()
import os, httpx, json

token = os.environ["CLICKUP_API_TOKEN"]
r = httpx.get(
    "https://api.clickup.com/api/v2/list/901113386257/field",
    headers={"Authorization": token},
    timeout=15,
)
fields = r.json().get("fields", [])
target_ids = {
    "be348a1d-6a63-4da8-83bb-9038b24264ff",
    "fd77f978-eca8-499e-bc3c-dc1bf4b8181e",
    "e0e439f5-397d-432d-addd-e90fbf50cd30",
}
for f in fields:
    if f["id"] in target_ids:
        print(f"FIELD: {f['name']} | id={f['id']} | type={f['type']}")
        for o in f.get("type_config", {}).get("options", []):
            print(f"  opt: {o.get('name','')} | id={o.get('id','')} | idx={o.get('orderindex','')}")
