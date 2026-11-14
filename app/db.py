"""SQLite schema and connections."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import BASE_DIR

DATA_DIR = Path(os.environ.get("MEDIRAPPEL_DATA", BASE_DIR / "data"))
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "medirappel.db"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS user (
    id              INTEGER PRIMARY KEY,
    login           TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    initials        TEXT    NOT NULL DEFAULT '',  -- login page badge
    role            TEXT    NOT NULL CHECK (role IN ('practitioner', 'assistant', 'admin')),
    practitioner_id INTEGER,              -- Trophy NUM_PRATICIEN (practitioner)
    practitioner_ids TEXT,                -- JSON list of NUM_PRATICIEN (assistant)
    pin_hash        TEXT    NOT NULL,
    pin_salt        TEXT    NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    attempts        INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
);

CREATE TABLE IF NOT EXISTS session (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES user(id),
    created_at      TEXT    NOT NULL,
    seen_at         TEXT    NOT NULL,
    revoked         INTEGER NOT NULL DEFAULT 0
);

-- Disk cache of Trophy API answers (CLAUDE.md §4).
CREATE TABLE IF NOT EXISTS api_cache (
    key             TEXT    PRIMARY KEY,
    fetched_at      REAL    NOT NULL,
    body            TEXT    NOT NULL
);

-- One SMS per Trophy appointment. Local times, 'YYYY-MM-DDTHH:MM'.
-- status: planned | sending | submitted | sent | delivered | failed
--         | cancelled | voided | skipped | missed
CREATE TABLE IF NOT EXISTS reminder (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL UNIQUE,
    practitioner_id INTEGER NOT NULL,
    patient_id      INTEGER NOT NULL,
    patient_name    TEXT    NOT NULL,
    appointment_at  TEXT    NOT NULL,
    phone           TEXT,                 -- E.164, NULL when no French mobile
    body            TEXT    NOT NULL,
    send_at         TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    reason          TEXT,                 -- why skipped / voided / failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    gateway_id      TEXT,
    sent_at         TEXT,
    delivered_at    TEXT,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_day ON reminder(practitioner_id, appointment_at);
CREATE INDEX IF NOT EXISTS idx_reminder_status ON reminder(status, send_at);

-- Patients who no longer want SMS (CLAUDE.md §3). Trophy id only, no name.
CREATE TABLE IF NOT EXISTS optout (
    patient_id      INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    user_id         INTEGER,
    action          TEXT    NOT NULL,
    entity          TEXT,
    entity_id       TEXT,
    data            TEXT    NOT NULL,     -- JSON, never a patient name or phone
    prev_hash       TEXT    NOT NULL,
    hash            TEXT    NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Each request / task opens its own connection; async routes run on the loop thread.
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = connect()
    try:
        old = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'user'").fetchone()
        if old and "practitioner_ids" not in old[0]:
            # Pre-assistant schema: SQLite cannot alter a CHECK, rebuild the table (ids kept).
            create = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS user ("):SCHEMA.index(");") + 2]
            conn.executescript("PRAGMA foreign_keys = OFF; BEGIN;"
                               + create.replace("IF NOT EXISTS user (", "user_new (")
                               + "INSERT INTO user_new (id, login, name, role, practitioner_id, pin_hash, pin_salt,"
                               " active, attempts, locked_until) SELECT id, login, name, role, practitioner_id,"
                               " pin_hash, pin_salt, active, attempts, locked_until FROM user;"
                               " DROP TABLE user; ALTER TABLE user_new RENAME TO user; COMMIT;")
        conn.executescript(SCHEMA)
        if "initials" not in {r[1] for r in conn.execute("PRAGMA table_info(user)")}:
            conn.execute("ALTER TABLE user ADD COLUMN initials TEXT NOT NULL DEFAULT ''")
    finally:
        conn.close()
