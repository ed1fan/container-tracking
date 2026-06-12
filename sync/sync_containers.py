"""sync_containers.py — Shipsgo v2 container tracking sync for Fantasia inbound POs.

Flow:
1. Read distinct open container IDs from fantasia-data.visibility (DATA_DB_URL, read-only).
2. For each container not yet in our DB, register it with Shipsgo POST /ocean/shipments.
   - 409 (already exists) is handled gracefully — we grab the existing ID at no extra cost.
3. For all registered containers, fetch latest details from GET /ocean/shipments/{id}.
4. Upsert containers table and append new events to container_events.
5. Write per-run log to runs/<timestamp>/.

Run: py -3.12 sync/sync_containers.py
Schedule: Task Scheduler daily (or use Shipsgo webhooks for real-time updates).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent.parent   # container-tracking/ root
load_dotenv(HERE / ".env", override=True)

DATA_DB_URL = os.environ["DATA_DB_URL"]          # fantasia-data read-only
APP_DB_URL  = os.environ["SUPABASE_DB_URL"]      # fantasia-tracking (this app)

SHIPSGO_BASE      = "https://api.shipsgo.com/v2"
SHIPSGO_API_KEY   = os.environ["SHIPSGO_API_KEY"]
SHIPSGO_FOLLOWERS = ["import@fantasia.com"]   # notified on every milestone update

RUN_TS   = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
RUNS_DIR = HERE / "runs" / RUN_TS
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Derive SCAC code from container number prefix
# Container format: AAAU1234567 — first 4 letters = owner code = SCAC
# e.g., MSCU1234567 -> MSCU, COSU1234567 → COSU, HLCU1234567 → HLCU
# ---------------------------------------------------------------------------

import re
CONTAINER_RE = re.compile(r'^[A-Z]{4}[0-9]{7}$')

def is_ocean_container(container_id: str | None) -> bool:
    """Validate ISO 6346 container format: 4 letters + 7 digits (e.g. MSCU1234567)."""
    if not container_id:
        return False
    return bool(CONTAINER_RE.match(container_id.strip().upper()))

def scac_from_container(container_id: str | None) -> str | None:
    if not container_id or len(container_id) < 4:
        return None
    prefix = container_id[:4].upper()
    if prefix.isalpha():
        return prefix
    return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(RUNS_DIR / "run.log"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step 1: fetch open containers from fantasia-data
# ---------------------------------------------------------------------------
OPEN_CONTAINERS_SQL = """
SELECT DISTINCT
    v.purchase_container_id          AS container_id,
    v.purchase_supplier_reference    AS bol_number,
    v.purchase_import_number         AS import_number,
    v.purchase_port_name             AS pod,
    v.purchase_po_number             AS po_number,
    v.purchase_ship_via              AS ship_via
FROM visibility v
WHERE v.purchase_container_id IS NOT NULL
  AND v.purchase_container_id <> ''
ORDER BY v.purchase_container_id
"""


def fetch_open_containers() -> list[dict]:
    log.info("Fetching open containers from fantasia-data …")
    with psycopg.connect(DATA_DB_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(OPEN_CONTAINERS_SQL)
            rows = cur.fetchall()
    log.info("  %d distinct containers found", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Step 2: which containers are already in our app DB?
# ---------------------------------------------------------------------------

def fetch_registered(app_conn) -> dict[str, int]:
    """Returns {container_id: shipsgo_shipment_id} for open (not closed) containers."""
    with app_conn.cursor() as cur:
        cur.execute(
            "SELECT container_id, shipsgo_tracking_id FROM containers "
            "WHERE shipsgo_tracking_id IS NOT NULL AND closed_at IS NULL"
        )
        return {
            r["container_id"]: int(r["shipsgo_tracking_id"])
            for r in cur.fetchall()
            if r["shipsgo_tracking_id"]
        }


def mark_closed_containers(app_conn, open_ids: set) -> int:
    """Set closed_at on containers no longer in ERP visibility.
    Requires: containers.closed_at TIMESTAMPTZ column (migration needed if absent).
    Returns count of newly-closed containers."""
    if not open_ids:
        # Safety: empty pull would mark every container closed — skip instead.
        log.warning("mark_closed_containers: open_ids is empty — skipping to avoid mass closure")
        return 0
    with app_conn.cursor() as cur:
        cur.execute(
            "UPDATE containers SET closed_at = now() "
            "WHERE closed_at IS NULL AND NOT (container_id = ANY(%s))",
            (list(open_ids),),
        )
        count = cur.rowcount
    app_conn.commit()
    return count


# ---------------------------------------------------------------------------
# Step 3: Shipsgo API
# ---------------------------------------------------------------------------

def shipsgo_client() -> httpx.Client:
    return httpx.Client(
        base_url=SHIPSGO_BASE,
        headers={
            "X-Shipsgo-User-Token": SHIPSGO_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=30,
    )


def register_container(client: httpx.Client, meta: dict) -> int | None:
    """Register container with Shipsgo. Returns shipment_id (integer).
    409 means it already exists — we extract the ID from the response body.
    """
    container_id = meta["container_id"]
    payload = {
        "reference": meta.get("import_number") or meta.get("po_number") or container_id,
        "container_number": container_id,
        "followers": SHIPSGO_FOLLOWERS,
        # carrier omitted — Shipsgo auto-detects from container number
        # (container prefix is owner code, not always the operating carrier SCAC)
    }

    try:
        r = client.post("/ocean/shipments", json=payload)

        if r.status_code == 200:
            data = r.json()
            shipment_id = data["shipment"]["id"]
            log.info("  Registered %s -> shipment_id=%d", container_id, shipment_id)
            return shipment_id

        if r.status_code == 409:
            # Already exists — no credit charge, just grab the existing ID
            data = r.json()
            shipment_id = data.get("shipment", {}).get("id")
            if shipment_id:
                log.info("  Already exists %s -> shipment_id=%d", container_id, shipment_id)
                return int(shipment_id)
            log.warning("  409 for %s but no shipment ID in response", container_id)
            return None

        if r.status_code == 402:
            log.error("  Out of Shipsgo credits — stopping sync")
            raise RuntimeError("Shipsgo 402: insufficient credits")

        log.warning("  Unexpected %d for %s: %s", r.status_code, container_id, r.text[:200])
        return None

    except RuntimeError:
        raise
    except Exception as exc:
        log.warning("  Failed to register %s: %s", container_id, exc)
        return None


def fetch_shipment_detail(client: httpx.Client, shipment_id: int) -> dict | None:
    """GET /ocean/shipments/{shipment_id} — full tracking detail.
    Returns None on failure; raises StaleShipmentError on 404 (re-register needed).
    """
    try:
        r = client.get(f"/ocean/shipments/{shipment_id}")
        if r.status_code == 404:
            raise StaleShipmentError(shipment_id)
        r.raise_for_status()
        return r.json().get("shipment")
    except StaleShipmentError:
        raise
    except Exception as exc:
        log.warning("  Failed to fetch detail for shipment_id=%d: %s", shipment_id, exc)
        return None



class StaleShipmentError(Exception):
    """Shipsgo returned 404 — shipment ID is from a different account; re-register."""
    def __init__(self, shipment_id: int):
        self.shipment_id = shipment_id


# ---------------------------------------------------------------------------
# Step 4: parse Shipsgo response -> our schema
# ---------------------------------------------------------------------------

def _parse_date(val) -> dt.date | None:
    if not val:
        return None
    try:
        return dt.date.fromisoformat(str(val)[:10])
    except Exception:
        return None


def _build_route_legs(movements: list, pod_info: dict) -> tuple[list, int]:
    """Build route_legs list and current_leg index from Shipsgo movements."""
    legs: list[dict] = []
    last_act_idx = -1
    prev_vessel: str | None = None

    def _last_port() -> dict | None:
        for n in reversed(legs):
            if n["type"] == "port":
                return n
        return None

    for m in movements:
        mv        = m.get("vessel") or {}
        v_name    = mv.get("name") or None
        v_imo     = mv.get("imo")
        v_voy     = m.get("voyage") or mv.get("voyage")
        m_stat    = (m.get("status") or "").upper()
        loc       = m.get("location") or {}
        loc_code  = loc.get("code")
        loc_name  = loc.get("name")
        evt_code  = (m.get("event_type") or m.get("event") or "").upper() or None
        timestamp = m.get("timestamp")

        # Build event entry for this movement
        ev: dict = {}
        if evt_code:        ev["code"]   = evt_code
        if timestamp:       ev["date"]   = timestamp
        if m.get("status"): ev["status"] = m.get("status")

        # Port node: always emit (vessel may be null); deduplicate by code
        if loc_code or loc_name:
            lp = _last_port()
            if lp is not None and loc_code and lp.get("code") == loc_code:
                if ev:
                    lp["events"].append(ev)
            else:
                legs.append({
                    "type":   "port",
                    "name":   loc_name,
                    "code":   loc_code,
                    "events": [ev] if ev else [],
                })

        # Vessel node: only when v_name is non-null and has changed
        if v_name and v_name != prev_vessel:
            vessel_idx = len(legs)
            legs.append({"type": "vessel", "name": v_name, "voyage": v_voy, "imo": v_imo})
            if m_stat == "ACT":
                last_act_idx = vessel_idx
            prev_vessel = v_name

    # Final port: POD from route info (skip if already present by code)
    pod_loc  = pod_info.get("location") or {}
    pod_code = pod_loc.get("code")
    pod_name = pod_loc.get("name")
    pod_date = pod_info.get("date_of_discharge")
    if pod_name:
        lp = _last_port()
        if not (lp and pod_code and lp.get("code") == pod_code):
            ev = {"date": str(pod_date)[:10]} if pod_date else {}
            legs.append({
                "type":   "port",
                "name":   pod_name,
                "code":   pod_code,
                "events": [ev] if ev else [],
            })

    # Post-process: add ETD label to first DEPA on origin port,
    #               add ETA label to first ARRV on destination port
    port_nodes = [n for n in legs if n["type"] == "port"]
    if port_nodes:
        for ev in (port_nodes[0].get("events") or []):
            if (ev.get("code") or "") == "DEPA":
                ev["label"] = "ETD"
                break
        if len(port_nodes) > 1:
            for ev in (port_nodes[-1].get("events") or []):
                if (ev.get("code") or "") == "ARRV":
                    ev["label"] = "ETA"
                    break

    # current_leg: last ACT vessel node; fall back to last vessel node
    if last_act_idx >= 0:
        current_leg = last_act_idx
    else:
        vessel_idxs = [i for i, n in enumerate(legs) if n["type"] == "vessel"]
        current_leg = vessel_idxs[-1] if vessel_idxs else 0

    return legs, current_leg


def parse_container_row(shipment: dict, meta: dict) -> dict:
    """Map Shipsgo shipment detail to our containers table row."""
    route = shipment.get("route") or {}
    pol_info  = route.get("port_of_loading") or {}
    pod_info  = route.get("port_of_discharge") or {}
    carrier   = shipment.get("carrier") or {}

    mother_vessel  = None
    mother_voyage  = None
    current_vessel = None
    ts_port        = None
    prev_v_name    = None

    for m in ((shipment.get("containers") or [{}])[0].get("movements") or []):
        mv     = m.get("vessel") or {}
        v_name = mv.get("name")
        v_imo  = mv.get("imo")

        if v_name:
            current_vessel = v_name

        if v_name and v_imo:
            if ts_port is None and prev_v_name and prev_v_name != v_name:
                ts_port = (m.get("location") or {}).get("name")
            mother_vessel = v_name
            mother_voyage = m.get("voyage")

        if v_name:
            prev_v_name = v_name

    is_transshipment = bool(
        current_vessel and mother_vessel
        and current_vessel.strip().upper() != mother_vessel.strip().upper()
    )
    if not is_transshipment:
        ts_port = None

    movements  = (shipment.get("containers") or [{}])[0].get("movements") or []
    legs, current_leg = _build_route_legs(movements, pod_info)

    map_token = (shipment.get("tokens") or {}).get("map")

    return {
        "container_id":        meta["container_id"],
        "bol_number":          meta.get("bol_number"),
        "import_number":       meta.get("import_number"),
        "ship_via":            meta.get("ship_via"),
        "carrier":             carrier.get("name") or meta.get("carrier_name"),
        "vessel":              mother_vessel,
        "voyage":              mother_voyage,
        "current_vessel":      current_vessel,
        "map_token":           map_token,
        "pol":                 (pol_info.get("location") or {}).get("code"),
        "pod":                 (pod_info.get("location") or {}).get("code") or meta.get("pod"),
        "etd":                 _parse_date(pol_info.get("date_of_loading")),
        "eta":                 _parse_date(pod_info.get("date_of_discharge")),
        "actual_arrival":      None,   # set when status is ARRIVED/DISCHARGED
        "status":              shipment.get("status") or "UNKNOWN",
        "shipsgo_tracking_id": str(shipment["id"]),
        "last_synced_at":      dt.datetime.now(dt.timezone.utc).isoformat(),
        "is_transshipment":    is_transshipment,
        "ts_port":             ts_port,
        "route_legs":          Jsonb(legs) if legs else None,
        "current_leg":         current_leg,
    }


# Map Shipsgo status -> arrival date logic
ARRIVED_STATUSES = {"ARRIVED", "DISCHARGED"}


def parse_events(shipment: dict, meta: dict, existing_eta: dt.date | None) -> list[dict]:
    """Build container_events rows from Shipsgo shipment data."""
    container_id = meta["container_id"]
    events: list[dict] = []
    route    = shipment.get("route") or {}
    pol_info = route.get("port_of_loading") or {}
    pod_info = route.get("port_of_discharge") or {}

    etd_initial = _parse_date(pol_info.get("date_of_loading_initial"))
    etd_current = _parse_date(pol_info.get("date_of_loading"))
    eta_initial = _parse_date(pod_info.get("date_of_discharge_initial"))
    eta_current = _parse_date(pod_info.get("date_of_discharge"))
    status      = shipment.get("status") or ""

    def _evt(event_type, event_date, location=None, notes=None):
        return {
            "container_id": container_id,
            "event_type":   event_type,
            "event_date":   event_date,
            "location":     location,
            "vessel":       None,
            "source":       "shipsgo",
            "notes":        notes,
        }

    # ETD event
    if etd_current:
        events.append(_evt("ETD", etd_current,
                           location=(pol_info.get("location") or {}).get("code")))

    # ETA event (current)
    if eta_current:
        events.append(_evt("ETA", eta_current,
                           location=(pod_info.get("location") or {}).get("code")))

    # Delay detection — ETA shifted from original
    if eta_initial and eta_current and eta_current != eta_initial:
        delta = (eta_current - eta_initial).days
        events.append(_evt(
            "ETA_UPDATE", eta_current,
            notes=f"Original ETA: {eta_initial}. Shift: {'+' if delta > 0 else ''}{delta}d",
        ))

    # Arrival
    if status in ARRIVED_STATUSES and eta_current:
        events.append(_evt("ARRIVED", eta_current,
                           location=(pod_info.get("location") or {}).get("code")))

    return events


# ---------------------------------------------------------------------------
# Step 5: write to app DB
# ---------------------------------------------------------------------------

UPSERT_CONTAINER_SQL = """
INSERT INTO containers
    (container_id, bol_number, import_number, ship_via, carrier, vessel, voyage,
     current_vessel, map_token,
     pol, pod, etd, eta, actual_arrival, status, shipsgo_tracking_id, last_synced_at,
     is_transshipment, ts_port, route_legs, current_leg)
VALUES
    (%(container_id)s, %(bol_number)s, %(import_number)s, %(ship_via)s, %(carrier)s,
     %(vessel)s, %(voyage)s, %(current_vessel)s, %(map_token)s, %(pol)s, %(pod)s,
     %(etd)s, %(eta)s, %(actual_arrival)s, %(status)s,
     %(shipsgo_tracking_id)s, %(last_synced_at)s,
     %(is_transshipment)s, %(ts_port)s, %(route_legs)s, %(current_leg)s)
ON CONFLICT (container_id) DO UPDATE SET
    bol_number          = EXCLUDED.bol_number,
    import_number       = EXCLUDED.import_number,
    ship_via            = EXCLUDED.ship_via,
    carrier             = EXCLUDED.carrier,
    vessel              = EXCLUDED.vessel,
    voyage              = EXCLUDED.voyage,
    current_vessel      = EXCLUDED.current_vessel,
    map_token           = EXCLUDED.map_token,
    pol                 = EXCLUDED.pol,
    pod                 = EXCLUDED.pod,
    etd                 = EXCLUDED.etd,
    eta                 = EXCLUDED.eta,
    actual_arrival      = EXCLUDED.actual_arrival,
    status              = EXCLUDED.status,
    shipsgo_tracking_id = EXCLUDED.shipsgo_tracking_id,
    last_synced_at      = EXCLUDED.last_synced_at,
    is_transshipment    = EXCLUDED.is_transshipment,
    ts_port             = EXCLUDED.ts_port,
    route_legs          = EXCLUDED.route_legs,
    current_leg         = EXCLUDED.current_leg
"""

INSERT_EVENT_SQL = """
INSERT INTO container_events
    (container_id, event_type, event_date, location, vessel, source, notes)
VALUES
    (%(container_id)s, %(event_type)s, %(event_date)s,
     %(location)s, %(vessel)s, %(source)s, %(notes)s)
ON CONFLICT (container_id, event_type, event_date, source) DO NOTHING
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Fantasia container-tracking sync — %s ===", RUN_TS)

    open_containers = fetch_open_containers()
    meta_by_id = {r["container_id"]: r for r in open_containers}

    summary = {
        "run_ts":     RUN_TS,
        "total":      len(meta_by_id),
        "registered": 0,
        "synced":     0,
        "errors":     0,
    }

    # Filter to valid ISO ocean container numbers only
    skipped = [c for c in meta_by_id if not is_ocean_container(c)]
    if skipped:
        log.info("Skipping %d non-ocean container IDs: %s", len(skipped), skipped[:10])
    meta_by_id = {k: v for k, v in meta_by_id.items() if is_ocean_container(k)}
    summary["total"] = len(meta_by_id)
    summary["skipped_invalid"] = len(skipped)

    with psycopg.connect(APP_DB_URL, row_factory=dict_row) as app_conn:
        # Mark containers no longer in ERP as closed (sets closed_at, preserves status)
        n_closed = mark_closed_containers(app_conn, set(meta_by_id.keys()))
        summary["closed"] = n_closed
        if n_closed:
            log.info("Marked %d container(s) closed (no longer in ERP)", n_closed)

        registered = fetch_registered(app_conn)
        log.info("Already registered: %d of %d ocean containers", len(registered), len(meta_by_id))

        # Rate limit: 100 req/min. We do up to 2 req/container (register + detail).
        # 0.7s delay keeps us well under the limit even in the worst case.
        RATE_DELAY = 0.7

        with shipsgo_client() as client:
            for container_id, meta in meta_by_id.items():
                shipment_id = registered.get(container_id)

                # Register if new
                if shipment_id is None:
                    shipment_id = register_container(client, meta)
                    time.sleep(RATE_DELAY)
                    if shipment_id:
                        summary["registered"] += 1
                    else:
                        summary["errors"] += 1
                        continue

                # Fetch full detail
                try:
                    shipment = fetch_shipment_detail(client, shipment_id)
                except StaleShipmentError:
                    # ID is from a different/old account — clear it and re-register
                    log.warning("  Stale shipment_id=%d for %s — re-registering", shipment_id, container_id)
                    with app_conn.cursor() as cur:
                        cur.execute(
                            "UPDATE containers SET shipsgo_tracking_id = NULL WHERE container_id = %s",
                            (container_id,)
                        )
                    app_conn.commit()
                    shipment_id = register_container(client, meta)
                    time.sleep(RATE_DELAY)
                    if not shipment_id:
                        summary["errors"] += 1
                        continue
                    summary["registered"] += 1
                    shipment = fetch_shipment_detail(client, shipment_id)

                time.sleep(RATE_DELAY)
                if shipment is None:
                    summary["errors"] += 1
                    continue


                # Get existing ETA for delay comparison
                existing_eta = None
                with app_conn.cursor() as cur:
                    cur.execute("SELECT eta FROM containers WHERE container_id = %s", (container_id,))
                    row = cur.fetchone()
                    if row:
                        existing_eta = row["eta"]

                try:
                    container_row = parse_container_row(shipment, meta)
                    events = parse_events(shipment, meta, existing_eta)

                    # Mark actual_arrival if arrived
                    if container_row["status"] in ARRIVED_STATUSES:
                        container_row["actual_arrival"] = container_row["eta"]

                    with app_conn.cursor() as cur:
                        cur.execute(UPSERT_CONTAINER_SQL, container_row)
                        if events:
                            cur.executemany(INSERT_EVENT_SQL, events)

                    app_conn.commit()
                    summary["synced"] += 1
                    log.info(
                        "  OK %s  status=%-12s  eta=%s  events=%d",
                        container_id,
                        container_row["status"],
                        container_row["eta"],
                        len(events),
                    )

                except Exception as exc:
                    app_conn.rollback()
                    log.error("  ERR %s: %s", container_id, exc)
                    summary["errors"] += 1

    (RUNS_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    log.info(
        "Done -- %d synced, %d newly registered, %d errors",
        summary["synced"], summary["registered"], summary["errors"],
    )


if __name__ == "__main__":
    main()
