"""Append-only audit journal, chained by SHA-256 (ported from Sterilog).

Each entry carries the hash of the previous one: editing or deleting a row breaks
the chain, and verify_chain() finds it. Never write a patient name or phone here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

GENESIS = "0" * 64
log = logging.getLogger("audit")


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _hash_row(ts, user_id, action, entity, entity_id, data, prev) -> str:
    return _digest(ts, "" if user_id is None else str(user_id), action, entity or "",
                   entity_id or "", data, prev)


def record(conn: sqlite3.Connection, action: str, *, user_id: int | None = None,
           entity: str | None = None, entity_id=None, data: dict | None = None) -> None:
    # Reading the last hash and inserting must be one step, or two writers fork the chain.
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute("SELECT hash FROM journal ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = json.dumps(data or {}, ensure_ascii=False, sort_keys=True)
        eid = None if entity_id is None else str(entity_id)
        conn.execute(
            "INSERT INTO journal (timestamp, user_id, action, entity, entity_id, data, prev_hash, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, user_id, action, entity, eid, payload, prev,
             _hash_row(ts, user_id, action, entity, eid, payload, prev)),
        )
        if own:
            conn.execute("COMMIT")
    except Exception:
        if own:
            conn.execute("ROLLBACK")
        raise
    uid = user_id or (entity_id if entity == "user" else None)  # failed login: the account is the entity
    who = conn.execute("SELECT login FROM user WHERE id = ?", (uid,)).fetchone() if uid else None
    log.info("%s user=%s %s=%s %s", action, who["login"] if who else "-", entity or "-",
             eid or "-", payload if data else "")


def verify_chain(conn: sqlite3.Connection) -> dict:
    prev, total = GENESIS, 0
    for r in conn.execute("SELECT * FROM journal ORDER BY id"):
        total += 1
        if r["prev_hash"] != prev:
            return {"valid": False, "entries": total, "broken_at": r["id"], "why": "chain broken"}
        if r["hash"] != _hash_row(r["timestamp"], r["user_id"], r["action"], r["entity"],
                                  r["entity_id"], r["data"], r["prev_hash"]):
            return {"valid": False, "entries": total, "broken_at": r["id"], "why": "content modified"}
        prev = r["hash"]
    return {"valid": True, "entries": total, "broken_at": None, "why": None}
