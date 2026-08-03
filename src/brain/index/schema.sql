-- The derived index (BLUEPRINT.md §7.7).
--
-- EVERY table here is rebuildable by scanning the canonical files. Dropping
-- brain.sqlite3 loses nothing. There are no exceptions and no carve-outs — that is
-- the entire point of making files canonical, and it is enforced by three tests in
-- tests/integration/test_index_derived.py rather than by convention.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_index (
    id            TEXT PRIMARY KEY,
    file_path     TEXT NOT NULL,
    workspace     TEXT NOT NULL DEFAULT 'default',
    type          TEXT NOT NULL,
    provenance    TEXT NOT NULL,
    volatility    TEXT NOT NULL CHECK (volatility IN
                    ('immutable','slow','volatile','ephemeral')),
    status        TEXT NOT NULL CHECK (status IN
                    ('proposed','confirmed','superseded','expired','tombstoned')),
    disposition   TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    review_by     TEXT,
    owner         TEXT,
    title         TEXT,
    content_hash  TEXT NOT NULL,
    newest_rev    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_valid  ON memory_index (workspace, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_status ON memory_index (status, disposition);
CREATE INDEX IF NOT EXISTS idx_review ON memory_index (status, review_by);

-- Evidence is what separates a memory from a note. Indexed so provenance can be
-- answered without reopening every file.
CREATE TABLE IF NOT EXISTS evidence_link (
    memory_id  TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    span_start INTEGER,
    span_end   INTEGER,
    PRIMARY KEY (memory_id, source_ref, span_start)
);

CREATE TABLE IF NOT EXISTS relations (
    src          TEXT NOT NULL,
    rel          TEXT NOT NULL,
    dst          TEXT NOT NULL,
    evidence_ref TEXT,
    PRIMARY KEY (src, rel, dst)
);

-- Scanned from memories/.revisions/. The FILES are canonical; this exists so that
-- history is queryable, not so that it is stored.
CREATE TABLE IF NOT EXISTS revision_index (
    memory_id        TEXT NOT NULL,
    revision_no      INTEGER NOT NULL,
    file_path        TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    predecessor_hash TEXT,
    capture          TEXT NOT NULL CHECK (capture IN ('mediated','reconciled','imported')),
    recorded_from    TEXT NOT NULL,
    recorded_to      TEXT NOT NULL,
    opid             TEXT,
    actor            TEXT,
    session          TEXT,
    reason           TEXT,
    PRIMARY KEY (memory_id, revision_no)
);

-- Scanned from tombstones.jsonl. The LEDGER is canonical (§11.5.1).
CREATE TABLE IF NOT EXISTS tombstone_index (
    subject_id    TEXT PRIMARY KEY,
    subject_kind  TEXT NOT NULL,
    tombstoned_at TEXT NOT NULL,
    chain_seq     INTEGER NOT NULL
);

-- Delivery and purge status are DERIVED PROJECTIONS over the append-only ledgers,
-- never mutations of them. An earlier design put a mutable quorum_state field on a
-- hash-chained tombstone, which would have rewritten a link in the chain whose only
-- purpose is tamper evidence.
CREATE TABLE IF NOT EXISTS delivery_state (
    subject_id  TEXT PRIMARY KEY,
    state       TEXT NOT NULL CHECK (state IN ('pending','confirmed')),
    replicas    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purge_state (
    subject_id  TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    memory_id UNINDEXED,
    workspace UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'unicode61'
);
