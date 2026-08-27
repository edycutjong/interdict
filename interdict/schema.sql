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
    -- Recorded explicitly, never inferred. A first pass derived person-ness from
    -- "does this row have a date of birth?", which quietly reclassified 27 listed
    -- individuals who simply have no DOB on their SDN record as organisations. That
    -- flipped the type-consistency signal to a mismatch and cleared every one of them.
    -- Identity type is a fact about the counterparty, not a side effect of which
    -- optional fields happen to be populated.
    entity_type     text NOT NULL DEFAULT 'Individual'
                    CHECK (entity_type IN ('Individual','Entity','Vessel','Aircraft')),
    nationality     text,
    -- Provenance of this row in the seeded book.
    --   sentinel   drawn from the SDN list at seal time -- the release-leg proof
    --   variant    a sentinel's name in a different transliteration. The SAME person,
    --              so the correct verdict is HOLD. Catching these is the whole job.
    --   lookalike  a DIFFERENT person who shares a surname with a designated party and
    --              whose date of birth contradicts the record. Correct verdict CLEAR.
    --              These are the false positives that freeze an innocent grantee's
    --              money in the real world, and clearing them correctly is what the
    --              adjudication plane is for.
    --   ordinary   unrelated to anyone on the list; exercises the auto-no-hit path.
    origin          text NOT NULL
                    CHECK (origin IN ('sentinel','variant','lookalike','ordinary')),
    -- EVALUATION ONLY. Never read by the screening path -- the orchestrator does not
    -- know this column exists. It is the ground truth `scripts/adjudication_quality.py`
    -- grades against, which is only meaningful because the deciding code cannot see it.
    expected_verdict text CHECK (expected_verdict IN ('HOLD','CLEAR')),
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

-- NOTE: `seq` is a plain bigint, NOT a bigserial, and is assigned inside the trigger
-- below. This is deliberate and was found by the concurrency test.
--
-- bigserial draws from a sequence at INSERT time, which happens OUTSIDE the advisory
-- lock that serialises hash chaining. Two concurrent writers could therefore chain in
-- one order and receive sequence numbers in the other: T2 chains onto the tail and T1
-- chains onto T2, while T1 holds the lower seq. The chain is still linear, but reading
-- it ORDER BY seq shows a break -- and an audit trail that cannot be read in order is
-- not an audit trail. Assigning seq under the same lock makes sequence order and chain
-- order the same thing by construction.
CREATE TABLE IF NOT EXISTS ledger (
    seq         bigint PRIMARY KEY,
    event_type  text NOT NULL,
    payload     jsonb NOT NULL,
    prev_hash   bytea NOT NULL,
    entry_hash  bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- The advisory lock is what makes the chain linear. Without it two concurrent inserts
-- both read the same tail hash and the chain forks -- which is precisely the test in
-- tests/test_ledger.py::test_ledger_chain_does_not_fork_under_concurrent_writers.
CREATE OR REPLACE FUNCTION ledger_chain() RETURNS trigger AS $$
DECLARE
    last_hash bytea;
    last_seq  bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('interdict.ledger'));

    SELECT entry_hash, seq INTO last_hash, last_seq
    FROM ledger ORDER BY seq DESC LIMIT 1;

    IF last_hash IS NULL THEN
        last_hash := digest('interdict.genesis', 'sha256');   -- deterministic genesis
        last_seq  := 0;
    END IF;

    -- Assigned here, under the lock, so seq order == chain order.
    NEW.seq := last_seq + 1;

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
    -- ADJUDICATOR_UNAVAILABLE is deliberately its own reason rather than a flavour of
    -- PARSE_ERROR: it is the only one here that is fixed by waiting rather than by a
    -- human reading the evidence. See interdict/adjudicator.py.
    reason       text NOT NULL CHECK (reason IN
                 ('ORACLE_DISAGREE','LOOP_CAP','SCHEMA_INVALID','NO_CITATION',
                  'PARSE_ERROR','ADJUDICATOR_UNAVAILABLE')),
    match_id     bigint REFERENCES matches(id),
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz
);

-- Migration, idempotent. CREATE TABLE IF NOT EXISTS above does nothing to a database
-- that already has `quarantine`, so the widened CHECK has to be applied explicitly or
-- an existing deployment rejects ADJUDICATOR_UNAVAILABLE at insert time.
ALTER TABLE quarantine DROP CONSTRAINT IF EXISTS quarantine_reason_check;
ALTER TABLE quarantine ADD CONSTRAINT quarantine_reason_check CHECK (reason IN
    ('ORACLE_DISAGREE','LOOP_CAP','SCHEMA_INVALID','NO_CITATION',
     'PARSE_ERROR','ADJUDICATOR_UNAVAILABLE'));

-- Migration, idempotent. The second-model opinion (Gemma) recorded beside the external
-- oracle's. Nullable on purpose and with no CHECK on agreement: NULL means "not asked
-- or unreachable", never "agreed". An outage that recorded itself as agreement would be
-- the same bug the yente path is written to avoid.
--
-- Deliberately NOT in the decision path. Nothing reads this column to hold, clear or
-- quarantine; it is evidence for the human reading the console, exactly like
-- yente_verdict above it.
ALTER TABLE adjudications ADD COLUMN IF NOT EXISTS gemma_verdict text;
