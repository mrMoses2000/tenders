-- A transport run has a case, exact cargo lines, and an auditable journey state.
-- Legacy cost-only runs remain untracked; their allocations reveal item identity
-- without inventing shipped quantities or a journey status.
ALTER TABLE delivery_runs
    ALTER COLUMN carrier_id DROP NOT NULL,
    ALTER COLUMN total_cost DROP NOT NULL,
    ADD COLUMN status TEXT NOT NULL DEFAULT 'untracked' CHECK (
        status IN ('untracked','planned','in_transit','arrived','completed','cancelled')
    ),
    ADD COLUMN tracking_ref TEXT NOT NULL DEFAULT '',
    ADD COLUMN carrier_name TEXT NOT NULL DEFAULT '',
    ADD COLUMN expected_at TIMESTAMPTZ,
    ADD COLUMN note TEXT NOT NULL DEFAULT '',
    ADD COLUMN request_hash TEXT CHECK (request_hash ~ '^[0-9a-f]{64}$');

CREATE TABLE IF NOT EXISTS delivery_run_items (
    delivery_run_id UUID NOT NULL,
    case_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    purchase_item_id UUID,
    quantity NUMERIC(18,6) CHECK (quantity IS NULL OR quantity > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_run_id,request_item_id),
    FOREIGN KEY (delivery_run_id,case_id)
        REFERENCES delivery_runs(id,case_id) ON DELETE RESTRICT,
    FOREIGN KEY (request_item_id,case_id)
        REFERENCES request_items(id,case_id) ON DELETE RESTRICT
);
ALTER TABLE purchase_items
    ADD CONSTRAINT purchase_items_delivery_identity_unique
    UNIQUE (id,case_id,request_item_id);
ALTER TABLE delivery_run_items
    ADD CONSTRAINT delivery_run_items_purchase_fk
    FOREIGN KEY (purchase_item_id,case_id,request_item_id)
    REFERENCES purchase_items(id,case_id,request_item_id) ON DELETE RESTRICT;
CREATE INDEX IF NOT EXISTS delivery_run_items_case_item_idx
    ON delivery_run_items(case_id,request_item_id,delivery_run_id);

INSERT INTO delivery_run_items(delivery_run_id,case_id,request_item_id)
SELECT delivery_run_id,case_id,request_item_id FROM delivery_allocations
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS delivery_status_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL,
    delivery_run_id UUID NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL CHECK (
        to_status IN ('planned','in_transit','arrived','completed','cancelled')
    ),
    reason TEXT NOT NULL DEFAULT '',
    source_artifact_id UUID REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key<>''),
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (delivery_run_id,case_id)
        REFERENCES delivery_runs(id,case_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS delivery_status_events_run_idx
    ON delivery_status_events(delivery_run_id,occurred_at,id);

ALTER TABLE case_expenses
    ADD COLUMN delivery_run_id UUID,
    ADD CONSTRAINT case_expenses_delivery_run_fk
    FOREIGN KEY (delivery_run_id,case_id)
    REFERENCES delivery_runs(id,case_id) ON DELETE RESTRICT;
CREATE INDEX IF NOT EXISTS case_expenses_delivery_run_idx
    ON case_expenses(delivery_run_id,created_at) WHERE delivery_run_id IS NOT NULL;

CREATE OR REPLACE FUNCTION check_delivery_expense_cargo()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    run_id UUID;
BEGIN
    SELECT delivery_run_id INTO run_id FROM case_expenses WHERE id=NEW.expense_id;
    IF run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM delivery_run_items
        WHERE delivery_run_id=run_id AND request_item_id=NEW.request_item_id
    ) THEN
        RAISE EXCEPTION 'expense item is not in delivery cargo';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_expense_delivery_cargo_check
BEFORE INSERT OR UPDATE ON case_expense_allocations
FOR EACH ROW EXECUTE FUNCTION check_delivery_expense_cargo();
