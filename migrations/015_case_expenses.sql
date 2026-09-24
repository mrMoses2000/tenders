-- Shared planned and actual expenses beyond the legacy delivery_run model.
CREATE TABLE IF NOT EXISTS case_expenses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement_cases(id) ON DELETE RESTRICT,
    category TEXT NOT NULL CHECK (category IN (
        'delivery','handling','packaging','commission','travel','storage','other'
    )),
    cost_kind TEXT NOT NULL CHECK (cost_kind IN ('planned','actual')),
    amount NUMERIC(16,2) NOT NULL CHECK (amount >= 0),
    currency CHAR(3) NOT NULL DEFAULT 'KZT' CHECK (currency ~ '^[A-Z]{3}$'),
    allocation_method TEXT NOT NULL CHECK (
        allocation_method IN ('equal','quantity','manual')
    ),
    provenance TEXT NOT NULL CHECK (provenance IN ('user_reported','documented')),
    source_artifact_id UUID REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    note TEXT NOT NULL DEFAULT '',
    incurred_at TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (idempotency_key <> ''),
    recorded_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id,case_id),
    CHECK (provenance <> 'documented' OR source_artifact_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS case_expenses_case_idx
    ON case_expenses(case_id,cost_kind,category,created_at);

CREATE TABLE IF NOT EXISTS case_expense_allocations (
    expense_id UUID NOT NULL,
    case_id UUID NOT NULL,
    request_item_id UUID NOT NULL,
    allocated_amount NUMERIC(16,2) NOT NULL CHECK (allocated_amount >= 0),
    allocation_weight NUMERIC(18,6) NOT NULL CHECK (allocation_weight > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (expense_id,request_item_id),
    FOREIGN KEY (expense_id,case_id)
        REFERENCES case_expenses(id,case_id) ON DELETE RESTRICT,
    FOREIGN KEY (request_item_id,case_id)
        REFERENCES request_items(id,case_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS case_expense_allocations_item_idx
    ON case_expense_allocations(request_item_id,expense_id);

CREATE OR REPLACE FUNCTION check_case_expense_allocation_total()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_id UUID;
    target_amount NUMERIC(16,2);
    allocated NUMERIC(16,2);
    allocation_count INTEGER;
BEGIN
    IF TG_TABLE_NAME = 'case_expenses' THEN
        target_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target_id := OLD.expense_id;
    ELSE
        IF TG_OP = 'UPDATE' AND NEW.expense_id<>OLD.expense_id THEN
            RAISE EXCEPTION 'expense allocation cannot change expense_id';
        END IF;
        target_id := NEW.expense_id;
    END IF;
    SELECT amount INTO target_amount FROM case_expenses WHERE id=target_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT count(*),COALESCE(sum(allocated_amount),0)
    INTO allocation_count,allocated
    FROM case_expense_allocations WHERE expense_id=target_id;
    IF allocation_count=0 OR allocated<>target_amount THEN
        RAISE EXCEPTION 'expense % allocation total % differs from amount %',
            target_id,allocated,target_amount;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER case_expense_total_check
AFTER INSERT OR UPDATE OF amount ON case_expenses
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION check_case_expense_allocation_total();

CREATE CONSTRAINT TRIGGER case_expense_allocation_total_check
AFTER INSERT OR UPDATE OR DELETE ON case_expense_allocations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION check_case_expense_allocation_total();
