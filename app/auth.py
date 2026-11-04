"""PIN authentication, sessions, accounts (ported from Sterilog).

A 6-digit PIN is weak by nature: slow hashing, per-account lockout, per-IP cap and
short sessions compensate.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from . import journal

ITERATIONS = 200_000
PIN_LENGTH = 6
MAX_ATTEMPTS = 5
LOCK_DURATION = timedelta(minutes=15)
IDLE_MAX = timedelta(minutes=30)
MAX_TRIES_PER_IP = 20
IP_WINDOW_S = 15 * 60

_ip_tries: dict[str, deque[float]] = defaultdict(deque)
_ip_lock = threading.Lock()


def take_ip_try(ip: str) -> bool:
    """Count one login try from this IP; False above the cap. In memory only."""
    now = time.monotonic()
    with _ip_lock:
        tries = _ip_tries[ip]
        while tries and now - tries[0] > IP_WINDOW_S:
            tries.popleft()
        if len(tries) >= MAX_TRIES_PER_IP:
            return False
        tries.append(now)
        return True


def give_back_ip_try(ip: str) -> None:
    with _ip_lock:
        if _ip_tries[ip]:
            _ip_tries[ip].pop()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def hash_pin(pin: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, ITERATIONS).hex(), salt.hex()


def sync_users(conn: sqlite3.Connection, accounts: list[dict]) -> None:
    """users.json is the source of truth; removed accounts go inactive, history stays."""
    logins = []
    for a in accounts:
        logins.append(a["login"])
        conn.execute(
            """INSERT INTO user (login, name, role, practitioner_id, pin_hash, pin_salt, active)
               VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(login) DO UPDATE SET name = excluded.name, role = excluded.role,
                 practitioner_id = excluded.practitioner_id, pin_hash = excluded.pin_hash,
                 pin_salt = excluded.pin_salt, active = 1""",
            (a["login"], a["name"], a["role"], a.get("trophy_practitioner_id"),
             a["pin_hash"], a["pin_salt"]),
        )
    marks = ",".join("?" * len(logins)) or "''"
    conn.execute(f"UPDATE user SET active = 0 WHERE login NOT IN ({marks})", logins)
    conn.execute("UPDATE session SET revoked = 1 WHERE user_id IN (SELECT id FROM user WHERE active = 0)")


def authenticate(conn: sqlite3.Connection, user_id: int, pin: str):
    """Return (user, error message)."""
    user = conn.execute("SELECT * FROM user WHERE id = ? AND active = 1", (user_id,)).fetchone()
    if user is None:
        return None, "Ce compte n'existe pas ou a été désactivé."
    now = _iso(_now())
    if user["locked_until"] and user["locked_until"] > now:
        return None, "Compte verrouillé. Réessayez dans 15 min."
    # Count the try before hashing, in one write: parallel requests cannot pass the cap.
    taken = conn.execute(
        """UPDATE user SET attempts = attempts + 1
           WHERE id = ? AND attempts < ? AND (locked_until IS NULL OR locked_until <= ?)
           RETURNING attempts""",
        (user_id, MAX_ATTEMPTS, now),
    ).fetchall()
    if not taken:
        conn.execute("UPDATE user SET attempts = 0, locked_until = ? WHERE id = ?",
                     (_iso(_now() + LOCK_DURATION), user_id))
        return None, "Compte verrouillé. Réessayez dans 15 min."
    attempts = taken[0]["attempts"]

    digest, _ = hash_pin(pin, bytes.fromhex(user["pin_salt"]))
    if not secrets.compare_digest(digest, user["pin_hash"]):
        if attempts >= MAX_ATTEMPTS:
            conn.execute("UPDATE user SET attempts = 0, locked_until = ? WHERE id = ?",
                         (_iso(_now() + LOCK_DURATION), user_id))
            journal.record(conn, "auth.locked", entity="user", entity_id=user_id)
            return None, "Cinq codes faux. Compte verrouillé 15 minutes."
        journal.record(conn, "auth.failed", entity="user", entity_id=user_id,
                       data={"attempts": attempts})
        return None, f"Code incorrect. {MAX_ATTEMPTS - attempts} essai(s) avant verrouillage."

    conn.execute("UPDATE user SET attempts = 0, locked_until = NULL WHERE id = ?", (user_id,))
    return user, None


def open_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _iso(_now())
    conn.execute("INSERT INTO session (token, user_id, created_at, seen_at) VALUES (?, ?, ?, ?)",
                 (token, user_id, now, now))
    journal.record(conn, "auth.login", user_id=user_id)
    return token


def close_session(conn: sqlite3.Connection, token: str) -> None:
    row = conn.execute("SELECT user_id FROM session WHERE token = ?", (token,)).fetchone()
    conn.execute("UPDATE session SET revoked = 1 WHERE token = ?", (token,))
    if row:
        journal.record(conn, "auth.logout", user_id=row["user_id"])


def session_user(conn: sqlite3.Connection, token: str | None):
    if not token:
        return None
    row = conn.execute(
        """SELECT s.seen_at, u.* FROM session s JOIN user u ON u.id = s.user_id
           WHERE s.token = ? AND s.revoked = 0 AND u.active = 1""",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if _now() - datetime.fromisoformat(row["seen_at"]) > IDLE_MAX:
        conn.execute("UPDATE session SET revoked = 1 WHERE token = ?", (token,))
        return None
    conn.execute("UPDATE session SET seen_at = ? WHERE token = ?", (_iso(_now()), token))
    return row
