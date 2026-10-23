CREATE TABLE IF NOT EXISTS shipments (
    order_id              TEXT PRIMARY KEY,
    direction             TEXT,
    address               TEXT,
    weight                NUMERIC(10, 2),
    ship_date             DATE,
    plan_arrival          DATE,
    carrier               TEXT,
    vehicle               TEXT,
    driver                TEXT,
    gps_link              TEXT,
    comment               TEXT,
    status                TEXT NOT NULL DEFAULT 'планируется',

    -- GPS-поля
    ship_lat              DOUBLE PRECISION,
    ship_lng              DOUBLE PRECISION,
    current_lat           DOUBLE PRECISION,
    current_lng           DOUBLE PRECISION,
    first_seen_at         TIMESTAMPTZ,
    last_position_update  TIMESTAMPTZ,

    -- служебные
    last_parse_attempt    TIMESTAMPTZ,
    last_parse_error      TEXT,
    parse_fail_count      INTEGER NOT NULL DEFAULT 0,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments (status);
CREATE INDEX IF NOT EXISTS idx_shipments_order_id_pattern ON shipments (order_id text_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_shipments_updated_at ON shipments (updated_at DESC);
