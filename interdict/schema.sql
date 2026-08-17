-- Interdict — schema.
--
-- Correctness lives in Postgres constraints, not in application code. Three of the
-- audit findings that shaped this file are load-bearing enough to name inline:
--
--   F3  the ledger is hash-chained, append-only, and single-writer. Two concurrent
--       writers must produce ONE linear chain, never a fork.
--   F4  a full-book re-screen checkpoints per BATCH, never via a scalar cursor.
--       Workers claim batches with FOR UPDATE SKIP LOCKED and finish out of order,
--       so an optimistically-advanced cursor would resume PAST unfinished batches
--       and silently skip counterparties — a false-negative compliance bug.
--   F?  holds are idempotent under UNIQUE NULLS NOT DISTINCT: re-screening the same
--       counterparty against the same publication must not double-hold.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- The book
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS counterparties (
    id              bigserial PRIMARY KEY,
    external_ref    text UNIQUE NOT NULL,
    name            text NOT NULL,
    dob             text,                    -- free-form: the feed itself is imprecise
    nationality     text,
    -- Provenance of this row in the seeded book. 'sentinel' rows were drawn from the
    -- SDN list at seal time and are the release-leg proof; 'lookalike' rows are drawn
    -- from real public records so that a CLEAR verifies against a source we do not own.
    origin          text NOT NULL CHECK (origin IN ('sentinel', 'lookalike', 'ordinary')),
    source          text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS counterparties_name_idx ON counterparties (lower(name));

-- ---------------------------------------------------------------------------
-- Money
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS disbursements (
    id              bigserial PRIMARY KEY,
    counterparty_id bigint NOT NULL REFERENCES counterparties(id),
    amount_cents    bigint NOT NULL CHECK (amount_cents > 0),
    currency        text NOT NULL DEFAULT 'USD',
    memo            text,
    state           text NOT NULL DEFAULT 'QUEUED'
                    CHECK (state IN ('QUEUED','HELD','CLEARED','PAID','CANCELLED')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Illegal-transition trigger. The state machine is enforced by the database because
-- an agent that "forgets" to check is exactly the failure this project is about.
--
--   QUEUED  -> HELD | CLEARED | CANCELLED
--   HELD    -> CLEARED | CANCELLED          (CLEARED here = released on delisting)
--   CLEARED -> HELD | PAID | CANCELLED      (HELD here = a later delta re-screen hit)
--   PAID, CANCELLED are terminal.
CREATE OR REPLACE FUNCTION disbursement_transition() RETURNS trigger AS $$
BEGIN
    IF NEW.state = OLD.state THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;

    IF NOT (
        (OLD.state = 'QUEUED'  AND NEW.state IN ('HELD','CLEARED','CANCELLED')) OR
        (OLD.state = 'HELD'    AND NEW.state IN ('CLEARED','CANCELLED'))        OR
        (OLD.state = 'CLEARED' AND NEW.state IN ('HELD','PAID','CANCELLED'))
    ) THEN
        RAISE EXCEPTION 'illegal disbursement transition % -> % (disbursement %)',
            OLD.state, NEW.state, OLD.id
            USING ERRCODE = 'check_violation';
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS disbursement_transition_guard ON disbursements;
CREATE TRIGGER disbursement_transition_guard
    BEFORE UPDATE ON disbursements
    FOR EACH ROW EXECUTE FUNCTION disbursement_transition();

-- ---------------------------------------------------------------------------
-- OFAC publications and re-screen runs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS list_versions (
    id              bigserial PRIMARY KEY,
    published_at    date NOT NULL,
    source_hash     text NOT NULL UNIQUE,   -- sha256 of the raw payload as fetched
    record_count    integer,
    kind            text NOT NULL CHECK (kind IN ('SDN','DELTA')),
    fetched_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rescreen_runs (
    id              bigserial PRIMARY KEY,
    list_version_id bigint NOT NULL REFERENCES list_versions(id),
    trigger         text NOT NULL CHECK (trigger IN ('SCHEDULER','DELTA','MANUAL','CHALLENGE')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

-- F4: batch-level checkpointing. The safe resume point is MIN(batch_start) over rows
-- with completed_at IS NULL -- never a scalar cursor.
CREATE TABLE IF NOT EXISTS rescreen_batches (
    run_id          bigint NOT NULL REFERENCES rescreen_runs(id),
    batch_start     bigint NOT NULL,
    batch_end       bigint NOT NULL,
    claimed_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    PRIMARY KEY (run_id, batch_start)
);

CREATE INDEX IF NOT EXISTS rescreen_batches_incomplete_idx
    ON rescreen_batches (run_id, batch_start) WHERE completed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Plane 1 output: deterministic matches
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS matches (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES rescreen_runs(id),
    counterparty_id bigint NOT NULL REFERENCES counterparties(id),
    sdn_uid         text NOT NULL,
    det_score       numeric(5,4) NOT NULL CHECK (det_score BETWEEN 0 AND 1),
    components      jsonb NOT NULL,          -- per-signal breakdown, persisted for replay
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, counterparty_id, sdn_uid)
);

-- ---------------------------------------------------------------------------
-- Plane 2 output: Gemini adjudications
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS adjudications (
    id                 bigserial PRIMARY KEY,
    match_id           bigint NOT NULL REFERENCES matches(id),
    verdict            text NOT NULL CHECK (verdict IN ('HOLD','CLEAR','QUARANTINE')),
    rationale          text NOT NULL,
    model_id           text NOT NULL,
    prompt_hash        text NOT NULL,
    context            jsonb NOT NULL,       -- full input; `make replay ID=` re-runs it
    -- The oracle guard result is the deterministic plane checking the LLM plane at the
    -- inter-agent routing boundary. 'DISAGREE' is what routes into quarantine.
    oracle_guard_result text NOT NULL CHECK (oracle_guard_result IN ('AGREE','DISAGREE','SKIPPED')),
    yente_verdict      text,                 -- external oracle, recorded even when it agrees
    round_trips        smallint NOT NULL DEFAULT 1 CHECK (round_trips <= 2),
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Holds
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS holds (
    id               bigserial PRIMARY KEY,
    counterparty_id  bigint NOT NULL REFERENCES counterparties(id),
    disbursement_id  bigint REFERENCES disbursements(id),
    adjudication_id  bigint NOT NULL REFERENCES adjudications(id),
    placed_at        timestamptz NOT NULL DEFAULT now(),
    released_at      timestamptz,
    -- The statutory clock: OFAC blocking reports are due within 10 BUSINESS days.
    report_due_at    date NOT NULL,
    report_filed_at  timestamptz
);

-- Idempotency. NULLS NOT DISTINCT (Postgres 15+) is the point: a NULL released_at means
-- "currently active", and two active holds on the same (counterparty, disbursement) pair
-- must be impossible -- so re-screening the same book against the same publication is
-- safe to retry. Once released_at is set the row no longer blocks a future hold.
CREATE UNIQUE INDEX IF NOT EXISTS holds_active_uniq
    ON holds (counterparty_id, disbursement_id, released_at) NULLS NOT DISTINCT;

-- ---------------------------------------------------------------------------
-- F3: the ledger -- hash-chained, append-only, single writer
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ledger (
    seq         bigserial PRIMARY KEY,
    event_type  text NOT NULL,
    payload     jsonb NOT NULL,
    prev_hash   bytea NOT NULL,
    entry_hash  bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- The advisory lock is what makes the chain linear. Without it two concurrent inserts
-- both read the same tail hash and the chain forks -- which is precisely the test in
-- tests/test_ledger_concurrency.py.
CREATE OR REPLACE FUNCTION ledger_chain() RETURNS trigger AS $$
DECLARE
    last_hash bytea;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('interdict.ledger'));

    SELECT entry_hash INTO last_hash FROM ledger ORDER BY seq DESC LIMIT 1;
    IF last_hash IS NULL THEN
        last_hash := digest('interdict.genesis', 'sha256');   -- deterministic genesis
    END IF;

    NEW.prev_hash  := last_hash;
    -- jsonb normalises key order, so the digest is reproducible across processes.
    NEW.entry_hash := digest(
        last_hash
        || convert_to(NEW.event_type, 'UTF8')
        || convert_to(NEW.payload::text, 'UTF8'),
        'sha256');
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_chain_build ON ledger;
CREATE TRIGGER ledger_chain_build
    BEFORE INSERT ON ledger
    FOR EACH ROW EXECUTE FUNCTION ledger_chain();

CREATE OR REPLACE FUNCTION ledger_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'check_violation';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_no_mutate ON ledger;
CREATE TRIGGER ledger_no_mutate
    BEFORE UPDATE OR DELETE ON ledger
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();

DROP TRIGGER IF EXISTS ledger_no_truncate ON ledger;
CREATE TRIGGER ledger_no_truncate
    BEFORE TRUNCATE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_immutable();

-- ---------------------------------------------------------------------------
-- Transactional outbox -- the ONLY path from a decision to Pub/Sub, and the only
-- writer of ledger rows. No agent inserts into ledger directly (F3, option a).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS outbox (
    id           bigserial PRIMARY KEY,
    topic        text NOT NULL,
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);

CREATE INDEX IF NOT EXISTS outbox_unpublished_idx
    ON outbox (id) WHERE published_at IS NULL;

-- ---------------------------------------------------------------------------
-- Quarantine -- the terminal state for guard failures and the >=2 round-trip cap
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS quarantine (
    id           bigserial PRIMARY KEY,
    reason       text NOT NULL CHECK (reason IN
                 ('ORACLE_DISAGREE','LOOP_CAP','SCHEMA_INVALID','NO_CITATION','PARSE_ERROR')),
    match_id     bigint REFERENCES matches(id),
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz
);
