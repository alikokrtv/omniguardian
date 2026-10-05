-- Additive rollout. Fail fast instead of waiting behind production traffic for DDL locks.
SET LOCAL lock_timeout = '2s';
ALTER TABLE products ADD COLUMN currency varchar(3) NOT NULL DEFAULT 'USD';
ALTER TABLE inventory_levels ADD COLUMN managed_b2b integer NOT NULL DEFAULT 0;
ALTER TABLE inventory_levels ADD COLUMN revision bigint NOT NULL DEFAULT 0;
ALTER TABLE inventory_levels ADD CONSTRAINT inventory_managed_b2b_check
    CHECK (managed_b2b >= 0 AND committed_b2b >= managed_b2b) NOT VALID;
CREATE FUNCTION bump_inventory_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.revision := OLD.revision + 1;
    RETURN NEW;
END;
$$;
CREATE TRIGGER inventory_revision BEFORE UPDATE ON inventory_levels
    FOR EACH ROW EXECUTE FUNCTION bump_inventory_revision();
ALTER TABLE allocation_orders DROP CONSTRAINT allocation_orders_channel_check;
ALTER TABLE allocation_orders ADD CONSTRAINT allocation_orders_channel_check
    CHECK (channel IN ('shopify','amazon','mirakl','b2b','walmart','target','best_buy')) NOT VALID;
ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_action_check;
ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_action_check CHECK (action IN (
    'RESERVE','CONFIRM','CANCEL','FULFILL','MANUAL_ADJUST',
    'B2B_CONFIRM','B2B_CANCEL','B2B_FULFILL','RETURN_RECEIVE','QUARANTINE_RELEASE'
)) NOT VALID;

CREATE TABLE b2b_orders (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    customer varchar(256) NOT NULL,
    po_number varchar(128) NOT NULL,
    idempotency_key varchar(128) NOT NULL,
    request_hash varchar(64) NOT NULL,
    ship_window_start date NOT NULL,
    ship_window_end date NOT NULL CHECK (ship_window_end >= ship_window_start),
    status text NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','CONFIRMED','SHIPPED','CANCELLED')),
    line_items jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id), UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, customer, po_number)
);
CREATE TABLE pallet_tags (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    order_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    pallet_code varchar(128) NOT NULL,
    bin_code varchar(128) NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    FOREIGN KEY (tenant_id, order_id) REFERENCES b2b_orders(tenant_id, id),
    FOREIGN KEY (tenant_id, warehouse_id) REFERENCES warehouses(tenant_id, id)
);
CREATE UNIQUE INDEX pallet_active_owner ON pallet_tags (tenant_id, warehouse_id, pallet_code)
    WHERE is_active;
CREATE INDEX b2b_tenant_status ON b2b_orders (tenant_id, status, created_at DESC);

CREATE TABLE return_claims (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    marketplace text NOT NULL,
    rma varchar(128) NOT NULL,
    carrier text NOT NULL CHECK (carrier IN ('FEDEX','UPS','USPS')),
    tracking_number varchar(128) NOT NULL,
    sku_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    expected_value_usd numeric(14,2) NOT NULL CHECK (expected_value_usd >= 0),
    initiated_at timestamptz NOT NULL,
    claim_due_at timestamptz NOT NULL,
    business_timezone text NOT NULL,
    holidays jsonb NOT NULL,
    request_hash varchar(64) NOT NULL,
    received_at timestamptz,
    condition text CHECK (condition IN ('SELLABLE','DAMAGED_BOX','DEFECTIVE_SCRAP')),
    bin_code varchar(128),
    disposition text CHECK (disposition IN ('RESTOCKED','QUARANTINED','SCRAPPED')),
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id), UNIQUE (tenant_id, marketplace, rma),
    UNIQUE (tenant_id, carrier, tracking_number),
    FOREIGN KEY (tenant_id, sku_id) REFERENCES products(tenant_id, id),
    FOREIGN KEY (tenant_id, warehouse_id) REFERENCES warehouses(tenant_id, id),
    CHECK ((received_at IS NULL AND condition IS NULL AND disposition IS NULL)
        OR (received_at IS NOT NULL AND condition IS NOT NULL AND disposition IS NOT NULL))
);
CREATE INDEX returns_due ON return_claims (claim_due_at, tenant_id) WHERE received_at IS NULL;
CREATE TABLE claim_dossiers (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    return_id uuid NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'READY' CHECK (status IN ('READY','SUBMITTED','VOID_RECEIVED')),
    payload jsonb NOT NULL,
    carrier_reference varchar(128),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, return_id) REFERENCES return_claims(tenant_id, id)
);

CREATE TABLE marketplace_settlements (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    marketplace text NOT NULL,
    external_id varchar(128) NOT NULL,
    currency varchar(3) NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL CHECK (period_end >= period_start),
    actual_disbursement numeric(28,2) NOT NULL,
    expected_disbursement numeric(28,2) NOT NULL,
    payout_variance numeric(28,2) NOT NULL,
    net_contribution numeric(28,2) NOT NULL,
    request_hash varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id), UNIQUE (tenant_id, marketplace, external_id)
);
CREATE TABLE settlement_lines (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    settlement_id uuid NOT NULL,
    sku_id uuid NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_cogs numeric(14,2) NOT NULL CHECK (unit_cogs >= 0),
    gross_sales numeric(28,2) NOT NULL CHECK (gross_sales >= 0),
    commission_fees numeric(28,2) NOT NULL CHECK (commission_fees >= 0),
    fba_fees numeric(28,2) NOT NULL CHECK (fba_fees >= 0),
    ad_spend numeric(28,2) NOT NULL CHECK (ad_spend >= 0),
    return_chargebacks numeric(28,2) NOT NULL CHECK (return_chargebacks >= 0),
    shipping_cost numeric(28,2) NOT NULL CHECK (shipping_cost >= 0),
    other_withheld numeric(28,2) NOT NULL CHECK (other_withheld >= 0),
    ads_withheld boolean NOT NULL,
    expected_disbursement numeric(28,2) NOT NULL,
    net_contribution numeric(28,2) NOT NULL,
    FOREIGN KEY (tenant_id, settlement_id) REFERENCES marketplace_settlements(tenant_id, id),
    FOREIGN KEY (tenant_id, sku_id) REFERENCES products(tenant_id, id),
    UNIQUE (settlement_id, sku_id)
);
CREATE TABLE import_batches (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    source varchar(32) NOT NULL,
    idempotency_key varchar(128) NOT NULL,
    request_hash varchar(64) NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, idempotency_key)
);
CREATE TABLE bridge_sync_requests (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    provider varchar(32) NOT NULL,
    idempotency_key varchar(128) NOT NULL,
    status text NOT NULL DEFAULT 'REQUESTED' CHECK (status IN ('REQUESTED','COMPLETED')),
    import_batch_id uuid REFERENCES import_batches(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider, idempotency_key)
);
