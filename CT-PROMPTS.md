# Container Tracking Web App — Claude Code Prompts

Project prefix: **CT**
Supabase project: `fantasia-tracking` (already exists, tables: `containers`, `container_events`)
GitHub repo: `github.com/ed1fan/container-tracking` (create new, private)
Local folder: `Claude Playground/Fantasia/container-tracking/web/`

---

## CT-001: Scaffold Next.js container tracking web app

Context:
- Read `Claude Playground/Fantasia/SUPABASE_ACCESS_REFERENCE.md` — Fantasia federated Supabase architecture
- Read `Claude Playground/Fantasia/container-tracking/PROJECT_NOTES.md` — data schema and source fields
- Supabase project `fantasia-tracking` already exists with tables `containers` and `container_events`
- `fantasia-data` has a `visibility` table with internal PO data (read-only via `data_reader` role)
- Key fields from `visibility`:
  - `purchase_container_id` — join key to `containers.container_id`
  - `time_date` — internal ETA (from ERP)
  - `purchase_ship_orient_date` — internal ETD
  - `purchase_po_number` — PO number
  - `purchase_import_number` — ERP inbound number
  - `item_item_num_display` — item identifier

Deliverables — scaffold only, no logic yet:
- `web/` folder inside `Claude Playground/Fantasia/container-tracking/`
- Standard Next.js 14 App Router project (TypeScript, Tailwind CSS)
- `web/.env.local.example` with all required vars:
  ```
  # fantasia-tracking (this app's Supabase project)
  NEXT_PUBLIC_SUPABASE_URL=
  NEXT_PUBLIC_SUPABASE_ANON_KEY=
  SUPABASE_SERVICE_ROLE_KEY=

  # fantasia-data (read-only ERP mirror)
  DATA_DB_URL=postgresql://data_reader:...@aws-0-us-east-2.pooler.supabase.com:5432/postgres

  # Shipsgo
  SHIPSGO_API_KEY=
  SHIPSGO_API_URL=https://api.shipsgo.com/v2
  SHIPSGO_FOLLOWERS=import@fantasia.com
  ```
- `web/package.json` with deps: next, react, typescript, tailwindcss, @supabase/supabase-js, postgres (pg driver for DATA_DB_URL)
- `web/README.md` with setup steps

Acceptance criteria:
- `npm run dev` starts without errors
- Placeholder home page renders at localhost:3000

Commit message: `CT-001: Scaffold Next.js container tracking web app`

---

## CT-002: Build container tracking dashboard page

Context:
- Builds on CT-001
- `fantasia-tracking.containers` columns: `container_id, bol_number, import_number, carrier, vessel, voyage, pol, pod, etd, eta, actual_arrival, status, shipsgo_tracking_id, last_synced_at`
- `fantasia-data.visibility` columns of interest: `purchase_container_id, time_date (internal ETA), purchase_ship_orient_date (internal ETD), purchase_po_number, item_item_num_display`
- Status values from Shipsgo: `NEW, INPROGRESS, BOOKED, LOADED, SAILING, ARRIVED, DISCHARGED, UNTRACKED`

Deliverables:
- `web/app/page.tsx` — main dashboard with:
  - **Summary bar**: total containers, sailing, arriving within 7 days, delayed (Shipsgo ETA > internal ETA), last sync time
  - **Container table** with columns:
    - Container ID
    - Status (colored badge: SAILING=blue, ARRIVED=green, NEW/INPROGRESS=grey, DELAYED=red)
    - Carrier / Vessel
    - POD (port of discharge)
    - Internal ETA (`time_date` from visibility — most recent per container)
    - Shipsgo ETA (`containers.eta`)
    - Delta (days: Shipsgo ETA minus Internal ETA — red if positive/late, green if negative/early, grey if no Shipsgo ETA yet)
    - PO Numbers (comma-separated, from visibility join)
  - **Filter bar**: by status, by delta (delayed only toggle), search by container ID or PO number
  - **"Sync Now" button** — calls `POST /api/sync`, shows spinner, refreshes table on completion
  - **Last synced** timestamp from `MAX(last_synced_at)` in containers table
- `web/app/api/containers/route.ts` — GET endpoint that:
  - Queries `fantasia-tracking.containers` via Supabase JS client
  - Queries `fantasia-data.visibility` via `postgres` npm package using `DATA_DB_URL`
  - Joins on `container_id = purchase_container_id`
  - Returns merged rows sorted by delta descending (most delayed first)

Acceptance criteria:
- Dashboard loads and shows container list
- Delta column shows days difference correctly
- Delayed containers sort to top
- Filter by status works

Commit message: `CT-002: Build container tracking dashboard page`

---

## CT-003: Build TypeScript Shipsgo sync API route

Context:
- Builds on CT-002
- Replicates the Python sync logic (`container-tracking/sync/sync_containers.py`) in TypeScript
- Key Shipsgo API (base: `https://api.shipsgo.com/v2`, auth header: `X-Shipsgo-User-Token`):
  - `POST /ocean/shipments` — register container. Body: `{ reference, container_number, followers: ["import@fantasia.com"] }`. Returns `{ shipment: { id } }`. 409 = already exists (grab existing ID from response body).
  - `GET /ocean/shipments/{id}` — get tracking detail. Returns `{ shipment: { status, route: { port_of_loading: { date_of_loading, date_of_loading_initial }, port_of_discharge: { date_of_discharge, date_of_discharge_initial, location } }, carrier: { name } } }`
  - `POST /ocean/shipments/{id}/followers` — add follower. Body: `{ follower: email }`. 409 = already added (OK).
- Rate limit: 100 req/min — add 700ms delay between containers
- Container validation: must match `/^[A-Z]{4}[0-9]{7}$/` — skip invalid (parcel tracking numbers, "N/A", "TO FOLLOW")
- 402 response = out of credits — stop sync and return error

Deliverables:
- `web/app/api/sync/route.ts` — POST handler that:
  1. Reads distinct `purchase_container_id` from `fantasia-data.visibility` (via `DATA_DB_URL`) where not null and not empty
  2. Filters to valid ISO container format only
  3. Reads already-registered containers from `fantasia-tracking.containers` (via service role key)
  4. For each container:
     - If not registered: `POST /ocean/shipments` → save `shipsgo_tracking_id`
     - `GET /ocean/shipments/{id}` → parse ETD, ETA, status, vessel
     - `POST /ocean/shipments/{id}/followers` for `import@fantasia.com`
     - Upsert `containers` row in Supabase
     - Insert new `container_events` rows (ETD, ETA, ETA_UPDATE if ETA shifted from initial)
     - 700ms delay between iterations
  5. Returns `{ synced: N, registered: N, errors: N, duration_ms: N }`
- `web/app/api/sync/route.ts` should use `NextResponse.json()` and stream progress via `TransformStream` if possible (so UI can show progress)
- Update dashboard "Sync Now" button to show live progress count during sync

Acceptance criteria:
- POST /api/sync runs end-to-end and updates containers table
- Results match what the Python sync produces
- 402 from Shipsgo returns a clear error to the UI
- Button shows progress during sync, success/error on completion

Commit message: `CT-003: Build TypeScript Shipsgo sync API route`

---

## Setup checklist before running CT-001

1. Create GitHub repo `github.com/ed1fan/container-tracking` (private)
2. Clone to `Claude Playground/Fantasia/container-tracking/` (or init git in that folder)
3. Open `Claude Playground/Fantasia/container-tracking/` in VS Code
4. Paste CT-001 prompt into Claude Code
