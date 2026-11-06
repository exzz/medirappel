"""Background loops, in the single uvicorn process (CLAUDE.md §3).

- once a day (and at startup): plan the window [today, today + horizon] for every
  managed practitioner, purge old data;
- every minute: re-read the agenda of due SMS, then send them one by one with a
  random delay; one retry after `retry_delay_minutes`, then `failed`;
- every minute, separately: poll SMSGate for the delivery state (48 h at most).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, timedelta

from . import config, db, planner, smsgate, trophy
from .planner import iso, mask

log = logging.getLogger(__name__)
TICK_S = 60
_built_on: date | None = None


def horizon() -> int:
    s = config.settings()
    return max(s["reminder_days_before"] + 14, s["timeline"]["future_days"])


def _late_before(today: date) -> datetime:
    return datetime.combine(today, config.settings()["send_time"])


async def build(conn, today: date) -> None:
    for p in planner.managed_practitioners(conn):
        await planner.sync(conn, p["practitioner_id"], p["name"], today,
                           today + timedelta(days=horizon()), force=True,
                           late_before=_late_before(today))
    log.info("daily plan built for %s", today)
    keep = config.settings()["retention_days"]
    conn.execute("DELETE FROM reminder WHERE appointment_at < ?",
                 ((today - timedelta(days=keep)).isoformat(),))
    trophy.purge_cache(conn, 2 * 86400)
    conn.execute("DELETE FROM session WHERE seen_at < ?",
                 ((today - timedelta(days=7)).isoformat(),))


def fail(conn, row_id: int, attempts: int, error: str) -> None:
    """First failure: retry later. Second: `failed`, shown as « Échec envoi »."""
    if attempts >= 2:
        conn.execute("UPDATE reminder SET status = 'failed', reason = ?, updated_at = ? WHERE id = ?",
                     (error[:200], iso(planner.now()), row_id))
        log.error("SMS reminder %s failed twice: %s", row_id, error)
    else:
        retry = planner.now() + timedelta(minutes=config.settings()["retry_delay_minutes"])
        conn.execute("UPDATE reminder SET status = 'planned', send_at = ?, reason = ?, updated_at = ?"
                     " WHERE id = ?", (iso(retry), error[:200], iso(planner.now()), row_id))
        log.warning("SMS reminder %s failed, retry at %s: %s", row_id, iso(retry), error)


def _due(conn):
    return conn.execute("SELECT * FROM reminder WHERE status = 'planned' AND send_at <= ?"
                        " ORDER BY send_at, appointment_at", (iso(planner.now()),)).fetchall()


async def send_due(conn) -> None:
    s = config.settings()
    today = planner.now().date()
    # auto_send off: only manual retries (attempts > 0) leave; the rest waits as during a stop.
    paused = not s["auto_send"]
    due = [r for r in _due(conn) if r["attempts"] or not paused]
    # Re-read the agenda first: a cancelled appointment nobody looked at must not get its SMS.
    names = {p["practitioner_id"]: p["name"] for p in planner.managed_practitioners(conn)}
    for pid, day in {(r["practitioner_id"], r["appointment_at"][:10]) for r in due}:
        if pid not in names:
            n = conn.execute("UPDATE reminder SET status = 'voided', reason = 'praticien non gere'"
                             " WHERE practitioner_id = ? AND status = 'planned'", (pid,)).rowcount
            log.warning("%d SMS voided: practitioner %s has no account", n, pid)
            continue
        d = date.fromisoformat(day)
        try:
            await planner.sync(conn, pid, names[pid], d, d, force=True, late_before=_late_before(today))
        except Exception as exc:  # Trophy down: the midnight plan stands
            log.warning("agenda re-read failed for practitioner %s: %s", pid, exc)

    first = True
    for r in _due(conn):
        late = r["send_at"][:10] < today.isoformat() or planner.now().time() > s["send_deadline"]
        if r["attempts"] == 0 and late:
            conn.execute("UPDATE reminder SET status = 'missed', updated_at = ? WHERE id = ?",
                         (iso(planner.now()), r["id"]))
            log.warning("SMS reminder %s missed (was due %s)", r["id"], r["send_at"])
            continue
        if paused and not r["attempts"]:
            continue
        if not first:
            await asyncio.sleep(random.uniform(s["throttle"]["min_delay_s"], s["throttle"]["max_delay_s"]))
        first = False
        # Claim atomically: a « Annuler » clicked during the delay wins.
        claimed = conn.execute("UPDATE reminder SET status = 'sending', attempts = attempts + 1"
                               " WHERE id = ? AND status = 'planned' RETURNING attempts",
                               (r["id"],)).fetchone()
        if not claimed:
            log.info("SMS reminder %s no longer planned, not sent", r["id"])
            continue
        await hand_over(conn, r, claimed["attempts"])


async def hand_over(conn, r, attempts: int) -> None:
    """Send a row already claimed as `sending`."""
    try:
        gid = await smsgate.send(r["phone"], r["body"])
    except Exception as exc:
        fail(conn, r["id"], attempts, f"envoi: {exc}")
        return
    conn.execute("UPDATE reminder SET status = 'submitted', gateway_id = ?, sent_at = ?,"
                 " reason = NULL, updated_at = ? WHERE id = ?",
                 (gid, iso(planner.now()), iso(planner.now()), r["id"]))
    log.info("SMS reminder %s handed to SMSGate for %s", r["id"], mask(r["phone"]))


async def poll_states(conn) -> None:
    since = iso(planner.now() - timedelta(hours=48))
    rows = conn.execute("SELECT id, gateway_id, attempts FROM reminder"
                        " WHERE status IN ('submitted', 'sent') AND sent_at >= ?", (since,)).fetchall()
    for r in rows:
        try:
            st = await smsgate.state(r["gateway_id"])
        except Exception as exc:
            log.warning("SMSGate state of %s unavailable: %s", r["id"], exc)
            return  # phone unreachable: try again next tick
        if st == "Failed":
            fail(conn, r["id"], r["attempts"], "SMSGate: Failed")
        elif st in ("Sent", "Delivered"):
            n = conn.execute("UPDATE reminder SET status = ?, delivered_at = ?, updated_at = ?"
                             " WHERE id = ? AND status != ?",
                             (st.lower(), iso(planner.now()) if st == "Delivered" else None,
                              iso(planner.now()), r["id"], st.lower())).rowcount
            if n:
                log.info("SMS reminder %s %s", r["id"], st.lower())


async def _loop(step) -> None:
    while True:
        conn = db.connect()
        try:
            await step(conn)
        except Exception:
            log.exception("scheduler step %s failed", step.__name__)
        finally:
            conn.close()
        await asyncio.sleep(TICK_S)


async def plan_and_send(conn) -> None:
    global _built_on
    today = planner.now().date()
    if _built_on != today:
        await build(conn, today)
        _built_on = today
    await send_due(conn)


async def run() -> None:
    await asyncio.gather(_loop(plan_and_send), _loop(poll_states))
