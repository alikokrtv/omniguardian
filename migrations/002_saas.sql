ALTER TABLE tenants ADD COLUMN tier_plan text NOT NULL DEFAULT 'STARTER'
    CHECK (tier_plan IN ('STARTER', 'GROWTH', 'ENTERPRISE'));
ALTER TABLE tenants ADD COLUMN penalty_estimate_usd numeric(14,2) NOT NULL DEFAULT 25.00
    CHECK (penalty_estimate_usd >= 0);
ALTER TABLE api_keys ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE;

CREATE TABLE monthly_usage (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    cycle_start date NOT NULL CHECK (extract(day FROM cycle_start) = 1),
    allocation_count bigint NOT NULL DEFAULT 0 CHECK (allocation_count >= 0),
    PRIMARY KEY (tenant_id, cycle_start)
);
-- Preserve existing billable allocations, including subsequently cancelled orders.
INSERT INTO monthly_usage (tenant_id, cycle_start, allocation_count)
SELECT tenant_id, date_trunc('month', created_at AT TIME ZONE 'UTC')::date, count(*)
FROM allocation_orders GROUP BY tenant_id, 2;

CREATE TABLE audit_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    sku varchar(128) NOT NULL,
    warehouse_code varchar(128) NOT NULL,
    action text NOT NULL CHECK (action IN ('RESERVE','CONFIRM','CANCEL','FULFILL','MANUAL_ADJUST')),
    quantity_delta integer NOT NULL,
    previous_afs integer NOT NULL CHECK (previous_afs >= 0),
    new_afs integer NOT NULL CHECK (new_afs >= 0),
    reference_order_id varchar(128),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 256),
    timestamp timestamptz NOT NULL DEFAULT clock_timestamp(),
    details jsonb NOT NULL,
    CHECK (quantity_delta = new_afs - previous_afs)
);
CREATE INDEX audit_tenant_time ON audit_logs (tenant_id, timestamp DESC, id DESC);
CREATE INDEX audit_tenant_sku_time ON audit_logs (tenant_id, sku, timestamp DESC, id DESC);

CREATE FUNCTION reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only' USING ERRCODE = '42501';
END;
$$;
CREATE TRIGGER audit_logs_no_update_delete BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
CREATE TRIGGER audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_mutation();
REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM PUBLIC;

CREATE TABLE allocation_rejections (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    channel text NOT NULL,
    order_id varchar(128) NOT NULL,
    estimated_penalty_usd numeric(14,2) NOT NULL CHECK (estimated_penalty_usd >= 0),
    timestamp timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, channel, order_id)
);
CREATE INDEX rejection_tenant_time ON allocation_rejections (tenant_id, timestamp);
