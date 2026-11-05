"""Planning rules (CLAUDE.md §3): who gets an SMS, when, with which text."""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import date, datetime, timedelta

from . import config, trophy

log = logging.getLogger(__name__)

# Statuses the planner never touches again: the SMS is in flight or done.
LOCKED = ("sending", "submitted", "sent", "delivered", "failed", "missed")
# Shown as is in the page; also the marker of the "taken too late" rule.
LATE = "RDV pris après l'envoi auto"
EXCLUDED = "Refus SMS"

DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout",
          "septembre", "octobre", "novembre", "decembre"]


def now() -> datetime:
    """Local wall-clock time (TZ=Europe/Paris in production). Patched by tests."""
    return datetime.now().replace(second=0, microsecond=0)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M")


# ------------------------------------------------------------------ phone

def french_mobile(raw: str | None) -> str | None:
    """'06 12 34 56 78', '+33 6…', '0033 6…' -> '+33612345678'; anything else -> None."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    for prefix in ("+33", "0033"):
        if digits.startswith(prefix):
            digits = "0" + digits[len(prefix):]
    return "+33" + digits[1:] if re.fullmatch(r"0[67]\d{8}", digits) else None


def mask(phone: str | None) -> str:
    return phone[:-4] + "****" if phone else "-"


# ------------------------------------------------------------------- text

_PUNCT = str.maketrans({"’": "'", "‘": "'", "«": '"', "»": '"', "–": "-", "—": "-",
                        " ": " ", " ": " ", "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE"})
# Printable ASCII minus the GSM-7 extension characters, which cost two septets.
_GSM = set(chr(c) for c in range(32, 127)) - set("`[]{}\\^~|") | {"\n"}


def to_gsm(text: str) -> str:
    """Strip accents (user decision) and anything outside the GSM-7 basic set."""
    text = unicodedata.normalize("NFKD", text.translate(_PUNCT))
    return "".join(c for c in text if c in _GSM)


def render(practitioner_name: str, at: datetime) -> str:
    s = config.settings()
    p = s["practice"]
    return to_gsm(s["sms_template"].format(
        practice=p["name"], address=p["address"], phone=p["phone"],
        practitioner=practitioner_name,
        date=f"{DAYS[at.weekday()]} {at.day} {MONTHS[at.month - 1]}",
        time=f"{at.hour}h{at.minute:02d}",
    ))


# --------------------------------------------------------------- calendar

async def calendar(practitioner_id: int) -> Callable[[date], bool]:
    """Working-day test for a practitioner: weekly hours and holidays from Trophy."""
    p = await trophy.practitioner(practitioner_id)
    if p and p.get("horaires"):
        open_days = {d["jour_semaine"] for d in p["horaires"]
                     if d["matin"]["ouvert"] or d["apres_midi"]["ouvert"]}
    else:
        open_days = {1, 2, 3, 4, 5}  # unknown in Trophy: Monday to Friday
    year = now().year
    off = set()
    for y in (year - 1, year, year + 1):
        off |= await trophy.holidays(y)
    return lambda d: d.isoweekday() in open_days and d not in off


def send_time_for(day: date, is_open: Callable[[date], bool]) -> datetime:
    """N days before, moved back to the previous working day, at send_time."""
    s = config.settings()
    d = day - timedelta(days=s["reminder_days_before"])
    for _ in range(31):  # a closed month is a data error, not a reason to loop forever
        if is_open(d):
            break
        d -= timedelta(days=1)
    return datetime.combine(d, s["send_time"])


# ------------------------------------------------------------------- sync

async def sync(conn: sqlite3.Connection, practitioner_id: int, practitioner_name: str,
               start: date, end: date, *, force: bool, late_before: datetime) -> None:
    """Bring `reminder` rows of [start, end] in line with the Trophy agenda.

    late_before: a new SMS whose send time is earlier is "taken too late" and not sent.
    Pages pass now(); the scheduler passes today's send time so the morning batch
    still sends what it discovers.
    """
    s = config.settings()
    appts = await trophy.appointments(practitioner_id, start, end, force=force)
    is_open = await calendar(practitioner_id)
    stamp = iso(now())
    seen = []
    absent = 0
    optout = {r[0] for r in conn.execute("SELECT patient_id FROM optout")}
    for a in appts:
        pat = a.get("patient")
        if not pat or not a.get("debut"):
            continue  # pause, blocked slot, event
        at = datetime.fromisoformat(a["debut"])
        send = send_time_for(at.date(), is_open)
        phone = french_mobile(pat.get("mobile")) or french_mobile(pat.get("tel"))
        if pat["id"] in optout:
            status, reason = "skipped", EXCLUDED
        elif (a.get("type") or "").strip().lower() in s["excluded_appointment_types"]:
            status, reason = "skipped", "type exclu"
        # Patients.SMSACTIF is ignored: it reflects file age, not consent (CLAUDE.md §3).
        elif not phone:
            status, reason = "skipped", "pas de mobile"
        else:
            status, reason = "planned", None

        old = conn.execute("SELECT status, reason, send_at, attempts FROM reminder WHERE appointment_id = ?",
                           (a["id"],)).fetchone()
        seen.append(a["id"])
        if a.get("absent"):  # « RDV manqué » ticked: no SMS, not even listed; a sent SMS stays (user decision)
            absent += conn.execute("DELETE FROM reminder WHERE appointment_id = ? AND status IN"
                                   " ('planned', 'skipped', 'cancelled', 'voided')", (a["id"],)).rowcount
            continue
        if old and (old["status"] in LOCKED or old["attempts"]):  # a retry keeps its send time
            continue
        if status == "planned" and old and old["status"] == "cancelled":
            status = "cancelled"
        same_send = old and old["send_at"] == iso(send)
        if status == "planned" and (
            (same_send and old["reason"] == LATE)  # once too late, stays so
            or (send < late_before and not (same_send and old["status"] == "planned"))
        ):
            status, reason = "skipped", LATE

        conn.execute(
            """INSERT INTO reminder (appointment_id, practitioner_id, patient_id, patient_name,
                 appointment_at, phone, body, send_at, status, reason, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(appointment_id) DO UPDATE SET practitioner_id = excluded.practitioner_id,
                 patient_id = excluded.patient_id, patient_name = excluded.patient_name,
                 appointment_at = excluded.appointment_at, phone = excluded.phone,
                 body = excluded.body, send_at = excluded.send_at, status = excluded.status,
                 reason = excluded.reason, updated_at = excluded.updated_at""",
            (a["id"], practitioner_id, pat["id"],
             f"{pat.get('nom') or ''} {pat.get('prenom') or ''}".strip(),
             a["debut"], phone, render(practitioner_name, at), iso(send), status, reason, stamp),
        )

    # Gone from the agenda: deleted or moved out of the window.
    marks = ",".join("?" * len(seen)) or "-1"  # NOT IN (NULL) would match nothing
    gone = conn.execute(
        f"""UPDATE reminder SET status = 'voided', reason = 'rendez-vous supprime', updated_at = ?
            WHERE practitioner_id = ? AND appointment_at >= ? AND appointment_at <= ?
              AND status IN ('planned', 'skipped', 'cancelled') AND appointment_id NOT IN ({marks})""",
        [stamp, practitioner_id, start.isoformat(), end.isoformat() + "T23:59", *seen],
    ).rowcount
    log.info("agenda sync practitioner=%s %s..%s: %d appointments, %d voided (gone from agenda),"
             " %d dropped (marked missed)", practitioner_id, start, end, len(seen), gone, absent)


def managed_practitioners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Only practitioners with an active account are handled (CLAUDE.md §3)."""
    return conn.execute(
        "SELECT practitioner_id, name FROM user WHERE active = 1 AND role = 'practitioner'"
        " AND practitioner_id IS NOT NULL ORDER BY name"
    ).fetchall()
