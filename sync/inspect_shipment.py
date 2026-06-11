"""inspect_shipment.py — one-off: print raw Shipsgo shipment JSON for the first tracked container."""
import json, os
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent.parent
load_dotenv(HERE / ".env", override=True)

APP_DB_URL      = os.environ["SUPABASE_DB_URL"]
SHIPSGO_API_KEY = os.environ["SHIPSGO_API_KEY"]

with psycopg.connect(APP_DB_URL, row_factory=dict_row) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT container_id, shipsgo_tracking_id FROM containers "
            "WHERE shipsgo_tracking_id IS NOT NULL LIMIT 1"
        )
        row = cur.fetchone()

if not row:
    print("No tracked containers found.")
    raise SystemExit(1)

print(f"Container: {row['container_id']}  shipsgo_tracking_id: {row['shipsgo_tracking_id']}\n")

with httpx.Client(base_url="https://api.shipsgo.com/v2",
                  headers={"X-Shipsgo-User-Token": SHIPSGO_API_KEY},
                  timeout=30) as client:
    r = client.get(f"/ocean/shipments/{row['shipsgo_tracking_id']}")

print(f"HTTP {r.status_code}\n")
print(json.dumps(r.json(), indent=2, default=str))
