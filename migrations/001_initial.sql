CREATE TABLE tenants (
    id uuid PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE api_keys (
    key_hash varchar(64) PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    role text NOT NULL CHECK (role IN ('reader', 'allocator', 'admin')),
    is_active boolean NOT NULL DEFAULT true
);
CREATE TABLE warehouses (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    code varchar(128) NOT NULL,
    name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    UNIQUE (tenant_id, code), UNIQUE (tenant_id, id)
);
CREATE TABLE products (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    merchant_sku varchar(128) NOT NULL,
    barcode varchar(128),
    title text NOT NULL,
    cost_price numeric(14,2) NOT NULL DEFAULT 0 CHECK (cost_price >= 0),
    list_price numeric(14,2) NOT NULL DEFAULT 0 CHECK (list_price >= 0),
    UNIQUE (tenant_id, merchant_sku), UNIQUE (tenant_id, id)
);
CREATE TABLE inventory_levels (
    tenant_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    sku_id uuid NOT NULL,
    stock_on_hand integer NOT NULL DEFAULT 0 CHECK (stock_on_hand >= 0),
    committed_b2b integer NOT NULL DEFAULT 0 CHECK (committed_b2b >= 0),
    in_flight_reserved integer NOT NULL DEFAULT 0 CHECK (in_flight_reserved >= 0),
    available_for_sale integer GENERATED ALWAYS AS
        (stock_on_hand - committed_b2b - in_flight_reserved) STORED,
    PRIMARY KEY (tenant_id, warehouse_id, sku_id),
    FOREIGN KEY (tenant_id, warehouse_id) REFERENCES warehouses(tenant_id, id),
    FOREIGN KEY (tenant_id, sku_id) REFERENCES products(tenant_id, id),
    CHECK (stock_on_hand >= committed_b2b + in_flight_reserved)
);
CREATE TABLE allocation_orders (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    order_id varchar(128) NOT NULL,
    channel text NOT NULL CHECK (channel IN ('shopify', 'amazon', 'mirakl', 'b2b')),
    idempotency_key varchar(128) NOT NULL,
    request_hash varchar(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('RESERVED', 'CONFIRMED', 'CANCELLED', 'FULFILLED')),
    line_items jsonb NOT NULL CHECK (jsonb_typeof(line_items) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, channel, order_id)
);
CREATE TABLE outbox_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    sku varchar(128) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);
CREATE INDEX outbox_pending ON outbox_events (tenant_id, available_at, id)
    WHERE processed_at IS NULL;
