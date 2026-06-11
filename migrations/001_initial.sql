-- container-tracking: 001_initial.sql
-- Run in: fantasia-tracking Supabase SQL Editor

-- One row per container. Upserted on every Shipsgo sync.
CREATE TABLE IF NOT EXISTS containers (
    container_id        TEXT PRIMARY KEY,   -- purchase_container_id from fantasia-data.visibility
    bol_number          TEXT,               -- purchase_supplier_reference
    import_number       TEXT,               -- purchase_import_number (ERP inbound)
    carrier             TEXT,               -- purchase_ship_via
    vessel              TEXT,
    voyage              TEXT,
    pol                 TEXT,               -- port of loading
    pod                 TEXT,               -- port of discharge (purchase_port_name)
    etd                 DATE,
    eta                 DATE,
    actual_arrival      DATE,
    status              TEXT,               -- e.g. IN_TRANSIT, ARRIVED, AVAILABLE, DELAYED
    shipsgo_tracking_id TEXT,               -- Shipsgo's internal ID for this shipment
    last_synced_at      TIMESTAMPTZ DEFAULT now(),
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- Disable RLS — app-internal table, no end-user auth needed at this layer
ALTER TABLE containers DISABLE ROW LEVEL SECURITY;

-- One row per milestone event per container. Append-only; deduped on insert.
CREATE TABLE IF NOT EXISTS container_events (
    id              BIGSERIAL PRIMARY KEY,
    container_id    TEXT NOT NULL REFERENCES containers(container_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,  -- ETD | ETA_UPDATE | TRANSSHIPMENT | ARRIVED | AVAILABLE | DELAYED | MANUAL
    event_date      DATE,
    location        TEXT,
    vessel          TEXT,
    source          TEXT NOT NULL DEFAULT 'shipsgo', -- shipsgo | manual | factory_email
    notes           TEXT,
    recorded_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (container_id, event_type, event_date, source)
);

ALTER TABLE container_events DISABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS ce_container_idx ON container_events (container_id);
CREATE INDEX IF NOT EXISTS ce_event_type_idx ON container_events (event_type);
CREATE INDEX IF NOT EXISTS containers_status_idx ON containers (status);
CREATE INDEX IF NOT EXISTS containers_eta_idx ON containers (eta);
