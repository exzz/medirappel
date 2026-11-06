"""Routes. Run: uvicorn app.main:app (one process: the scheduler lives here)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import logging.handlers
import os
import sqlite3
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import date, timedelta

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, journal, planner, scheduler, smsgate, trophy
from .config import BASE_DIR
from .planner import iso, mask

log = logging.getLogger(__name__)
COOKIE = "medirappel_session"

STATUS = {  # label, css class
    "planned": ("Planifié", "wait"),
    "sending": ("Envoi en cours", "wait"),
    "submitted": ("Transmis au téléphone", "wait"),
    "sent": ("Envoyé", "good"),
    "delivered": ("Délivré", "good"),
    "failed": ("Échec envoi", "bad"),
    "cancelled": ("Annulé", "neutral"),
    "voided": ("Sans objet", "neutral"),
    "skipped": ("Non envoyé", "neutral"),
    "missed": ("Manqué", "bad"),
}
DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
LONG_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
LONG_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
               "octobre", "novembre", "décembre"]
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _setup_logs() -> None:
    db.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(db.LOGS_DIR / "medirappel.log", maxBytes=5_000_000,
                                             backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    for name in ("uvicorn.error", "uvicorn.access"):  # HTTP access and server errors in the same file
        logging.getLogger(name).addHandler(h)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logs()
    config.settings()
    config.trophy()
    db.init_db()
    conn = db.connect()
    try:
        auth.sync_users(conn, config.users())
        # Killed while handing an SMS to the phone: it may have left, never send it twice.
        conn.execute("UPDATE reminder SET status = 'failed', reason = 'envoi interrompu' WHERE status = 'sending'")
    finally:
        conn.close()
    sample = planner.render("Dr Prenom-Compose Nom-Compose", planner.now().replace(month=9, day=30, hour=10))
    if len(sample) > 160:
        log.warning("sms_template renders to %d chars (> 160): it will cost 2 SMS", len(sample))
    if not config.settings()["auto_send"]:
        log.warning("auto_send is false: planned SMS are NOT sent automatically")
    task = None if os.environ.get("MEDIRAPPEL_NO_SCHEDULER") else asyncio.create_task(scheduler.run())
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(css_v=int((BASE_DIR / "static" / "app.css").stat().st_mtime), status=STATUS, mask=mask, late=planner.LATE, excluded=planner.EXCLUDED)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
            " img-src 'self'; form-action 'self'; frame-ancestors 'none'")
    return resp


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def require_user(request: Request, conn: sqlite3.Connection):
    user = auth.session_user(conn, request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(307, headers={"Location": "/login"})
    return user


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 307:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return HTMLResponse(f"<p style='font:16px system-ui;padding:2rem'>{exc.detail}</p>",
                        status_code=exc.status_code)


# ------------------------------------------------------------------ login

def _accounts(conn):
    return conn.execute("SELECT id, name, role FROM user WHERE active = 1 ORDER BY name").fetchall()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, conn=Depends(get_conn)):
    return templates.TemplateResponse(request, "login.html", {"accounts": _accounts(conn), "error": None})


@app.post("/login")
def login(request: Request, user_id: int = Form(...), pin: str = Form(...), conn=Depends(get_conn)):
    if not auth.take_ip_try(_ip(request)):
        log.warning("too many login tries from %s", _ip(request))
        return templates.TemplateResponse(
            request, "login.html", status_code=429,
            context={"accounts": _accounts(conn), "error": "Trop d'essais depuis ce poste. Réessayez dans 15 minutes."})
    user, error = auth.authenticate(conn, user_id, pin)
    if user is None:
        return templates.TemplateResponse(request, "login.html", {"accounts": _accounts(conn), "error": error})
    auth.give_back_ip_try(_ip(request))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, auth.open_session(conn, user["id"]), httponly=True, samesite="strict",
                    max_age=12 * 3600)
    return resp


@app.post("/logout")
def logout(request: Request, conn=Depends(get_conn)):
    if token := request.cookies.get(COOKIE):
        auth.close_session(conn, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# -------------------------------------------------------------------- day

def _label(d: date) -> str:
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def _visible(user, managed) -> list:
    """Practitioners whose planning this user may open (CLAUDE.md §8)."""
    if user["role"] == "practitioner":
        return [p for p in managed if p["practitioner_id"] == user["practitioner_id"]]
    if user["role"] == "assistant":
        ids = json.loads(user["practitioner_ids"] or "[]")
        return [p for p in managed if p["practitioner_id"] in ids]
    return managed


def _practitioner(user, managed, practitioner: int | None) -> tuple[int | None, str | None]:
    """A practitioner only sees themself; an assistant or admin picks among visible ones.

    The name returned is the practitioner's account name: the one written in the SMS."""
    if user["role"] == "practitioner":
        return user["practitioner_id"], user["name"]
    managed = _visible(user, managed)
    choice = next((p for p in managed if p["practitioner_id"] == practitioner), managed[0] if managed else None)
    return (choice["practitioner_id"], choice["name"]) if choice else (None, None)


@app.get("/", response_class=HTMLResponse)
async def day_page(request: Request, day: str | None = None, practitioner: int | None = None,
                   refresh: str | None = None, conn=Depends(get_conn)):
    user = require_user(request, conn)
    managed = _visible(user, planner.managed_practitioners(conn))
    pid, name = _practitioner(user, managed, practitioner)
    if pid is None:
        return templates.TemplateResponse(request, "day.html", {"user": user, "managed": [], "pid": None})

    today = planner.now().date()
    try:
        current = date.fromisoformat(day) if day else today
    except ValueError:
        raise HTTPException(400, "Date invalide.")
    tl = config.settings()["timeline"]

    warning = "Logiciel du cabinet injoignable : planning non rechargé." if refresh == "failed" else None
    try:
        if current >= today:
            # Future days: always re-read the agenda (user decision).
            await planner.sync(conn, pid, name, current, current, force=True, late_before=planner.now())
        is_open = await planner.calendar(pid)
    except Exception as exc:
        log.warning("Trophy unavailable: %s", exc)
        warning = "Logiciel du cabinet injoignable : planification non actualisée."
        is_open = lambda d: True  # noqa: E731

    timeline = [{"day": d, "iso": d.isoformat(), "label": _label(d), "open": is_open(d),
                 "today": d == today, "current": d == current}
                for d in (today + timedelta(days=i) for i in range(-tl["past_days"], tl["future_days"] + 1))]
    rows = conn.execute(
        "SELECT * FROM reminder WHERE practitioner_id = ? AND appointment_at >= ? AND appointment_at <= ?"
        " ORDER BY appointment_at",
        (pid, current.isoformat(), current.isoformat() + "T23:59"),
    ).fetchall()
    return templates.TemplateResponse(request, "day.html", {
        "user": user, "managed": managed, "pid": pid, "name": name, "rows": rows,
        "timeline": timeline, "current": current,
        "current_label": f"{LONG_DAYS[current.weekday()]} {current.day} {LONG_MONTHS[current.month - 1]}",
        "past": current < today, "now": iso(planner.now()), "warning": warning,
        "refreshed": refresh == "ok", "auto_send": config.settings()["auto_send"],
        "gate": config.settings()["smsgate"],
        "optout": {r[0] for r in conn.execute("SELECT patient_id FROM optout")},
    })


@app.post("/refresh")
async def refresh_planning(request: Request, day: str = Form(...), practitioner: int | None = Form(None),
                           conn=Depends(get_conn)):
    """Re-read the whole upcoming agenda of one practitioner from Trophy, bypassing the cache."""
    user = require_user(request, conn)
    pid, name = _practitioner(user, planner.managed_practitioners(conn), practitioner)
    if pid is None:
        raise HTTPException(404, "Aucun praticien.")
    today = planner.now().date()
    try:
        await planner.sync(conn, pid, name, today, today + timedelta(days=scheduler.horizon()),
                           force=True, late_before=planner.now())
        result = "ok"
    except Exception as exc:
        log.warning("planning refresh failed for practitioner %s: %s", pid, exc)
        result = "failed"
    journal.record(conn, "planning.refresh", user_id=user["id"], entity="practitioner", entity_id=pid,
                   data={"result": result})
    return RedirectResponse(f"/?day={day[:10]}&practitioner={pid}&refresh={result}", status_code=303)


# ---------------------------------------------------------------- actions

def _own(request: Request, conn, rid: int):
    user = require_user(request, conn)
    row = conn.execute("SELECT * FROM reminder WHERE id = ?", (rid,)).fetchone()
    if row is None or (user["role"] != "admin" and row["practitioner_id"] not in
                       {p["practitioner_id"] for p in _visible(user, planner.managed_practitioners(conn))}):
        raise HTTPException(404, "SMS introuvable.")
    return user, row


def _back(row, extra: str = "") -> RedirectResponse:
    return RedirectResponse(f"/?day={row['appointment_at'][:10]}&practitioner={row['practitioner_id']}{extra}",
                            status_code=303)


def _act(request: Request, conn, rid: int, action: str, sql: str, params: tuple) -> RedirectResponse:
    user, row = _own(request, conn, rid)
    if conn.execute(sql, (*params, rid)).rowcount:
        journal.record(conn, f"reminder.{action}", user_id=user["id"], entity="reminder", entity_id=rid)
    else:
        log.info("reminder.%s refused (wrong status) user=%s reminder=%s", action, user["login"], rid)
    return _back(row)


@app.post("/reminder/{rid}/cancel")
def cancel(request: Request, rid: int, conn=Depends(get_conn)):
    return _act(request, conn, rid, "cancel",
                "UPDATE reminder SET status = 'cancelled', updated_at = ? WHERE status = 'planned' AND id = ?",
                (iso(planner.now()),))


@app.post("/reminder/{rid}/restore")
def restore(request: Request, rid: int, conn=Depends(get_conn)):
    return _act(request, conn, rid, "restore",
                "UPDATE reminder SET status = 'planned', updated_at = ?"
                " WHERE status = 'cancelled' AND send_at > ? AND id = ?",
                (iso(planner.now()), iso(planner.now())))


@app.post("/reminder/{rid}/retry")
def retry(request: Request, rid: int, conn=Depends(get_conn)):
    # attempts = 1: one manual try, then `failed` again without an automatic retry.
    return _act(request, conn, rid, "retry",
                "UPDATE reminder SET status = 'planned', attempts = 1, send_at = ?, updated_at = ?"
                " WHERE status = 'failed' AND id = ?",
                (iso(planner.now()), iso(planner.now())))


@app.post("/reminder/{rid}/send")
async def send_now(request: Request, rid: int, conn=Depends(get_conn)):
    """Force, once, an SMS skipped because the appointment was booked after the automatic send."""
    user, row = _own(request, conn, rid)
    names = {p["practitioner_id"]: p["name"] for p in planner.managed_practitioners(conn)}
    day = date.fromisoformat(row["appointment_at"][:10])
    try:  # the appointment may have moved or gone since the page was shown
        await planner.sync(conn, row["practitioner_id"], names[row["practitioner_id"]], day, day,
                           force=True, late_before=planner.now())
    except Exception as exc:
        log.warning("send-now re-read failed for reminder %s: %s", rid, exc)
        return _back(row, "&refresh=failed")
    # attempts = 2: a failure goes straight to `failed`, no automatic retry.
    claimed = conn.execute("UPDATE reminder SET status = 'sending', attempts = 2, updated_at = ?"
                           " WHERE id = ? AND status = 'skipped' AND reason = ? AND appointment_at > ?"
                           " RETURNING *", (iso(planner.now()), rid, planner.LATE, iso(planner.now()))).fetchone()
    if not claimed:
        log.info("reminder.send_now refused (wrong status) user=%s reminder=%s", user["login"], rid)
        return _back(row)
    journal.record(conn, "reminder.send_now", user_id=user["id"], entity="reminder", entity_id=rid)
    await scheduler.hand_over(conn, claimed, 2)
    return _back(row)


@app.post("/reminder/{rid}/exclude")
def exclude(request: Request, rid: int, conn=Depends(get_conn)):
    """The patient no longer wants SMS: local opt-out list, for every practitioner."""
    user, row = _own(request, conn, rid)
    if conn.execute("INSERT OR IGNORE INTO optout (patient_id, user_id, created_at) VALUES (?, ?, ?)",
                    (row["patient_id"], user["id"], iso(planner.now()))).rowcount:
        # Now, not at the next re-read: that re-read may fail and the SMS would leave.
        conn.execute("UPDATE reminder SET status = 'skipped', reason = ?, updated_at = ?"
                     " WHERE patient_id = ? AND status IN ('planned', 'cancelled')",
                     (planner.EXCLUDED, iso(planner.now()), row["patient_id"]))
        journal.record(conn, "patient.exclude", user_id=user["id"], entity="patient", entity_id=row["patient_id"])
    return _back(row)


@app.post("/reminder/{rid}/include")
def include(request: Request, rid: int, conn=Depends(get_conn)):
    """Undo an exclusion; the next agenda re-read (the page reload) plans the SMS again."""
    user, row = _own(request, conn, rid)
    if conn.execute("DELETE FROM optout WHERE patient_id = ?", (row["patient_id"],)).rowcount:
        journal.record(conn, "patient.include", user_id=user["id"], entity="patient", entity_id=row["patient_id"])
    return _back(row)


# ----------------------------------------------------------- free message

def _message_page(request, user, **ctx):
    g = config.settings()["smsgate"]
    return templates.TemplateResponse(request, "message.html", {
        "user": user, "simulation": g.get("simulation"), "test_number": g.get("test_number"), **ctx})


_tasks = set()  # keep background tasks referenced until they end


@app.get("/message", response_class=HTMLResponse)
async def message_page(request: Request, conn=Depends(get_conn)):
    user = require_user(request, conn)
    # The page never waits for Trophy: the search serves the cache, refreshed meanwhile.
    task = asyncio.get_running_loop().create_task(trophy.refresh_patients())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return _message_page(request, user)


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().casefold().split())


@app.get("/message/patients")
def search_patients(request: Request, q: str = "", conn=Depends(get_conn)):
    """Up to 10 cached patients whose name holds every typed word, surname matches first."""
    require_user(request, conn)
    words = _fold(q).split()
    if not words:
        return []
    hits = []
    for p in trophy.cached_patients(conn):
        key = _fold(f"{p['nom'] or ''} {p['prenom'] or ''}")
        if all(w in key for w in words):
            hits.append((not key.startswith(words[0]), key, p))
    out = []
    for _, _, p in sorted(hits, key=lambda h: h[:2])[:10]:
        born = p["naissance"]
        number = planner.french_mobile(p["mobile"]) or planner.french_mobile(p["tel"])
        out.append({
            "name": " ".join(x for x in ((p["nom"] or "").upper(), p["prenom"]) if x),
            # Birth date tells namesakes apart (as in Sterilog).
            "detail": " · ".join(([f"né(e) le {born[8:10]}/{born[5:7]}/{born[:4]}"] if born else [])
                                 + ([] if number else ["pas de mobile"])),
            "phone": "0" + number[3:] if number else None,
        })
    return out


@app.post("/message", response_class=HTMLResponse)
async def send_message(request: Request, phone: str = Form(...), text: str = Form(...), conn=Depends(get_conn)):
    """A free SMS, sent at once. Neither the number nor the text is stored (CLAUDE.md §7)."""
    user = require_user(request, conn)
    number, body = planner.french_mobile(phone), planner.to_gsm(text.strip())
    error = ("Numéro invalide : un mobile français (06 ou 07) est attendu." if not number
             else "Message vide." if not body
             else f"Message trop long : {len(body)} caractères, 160 au plus." if len(body) > 160 else None)
    if error:
        return _message_page(request, user, error=error, phone=phone, text=text)
    try:
        gid = await smsgate.send(number, body)
    except Exception as exc:
        log.warning("free SMS to %s failed: %s", mask(number), exc)
        return _message_page(request, user, error="Échec de l'envoi : téléphone SMSGate injoignable.",
                             phone=phone, text=text)
    journal.record(conn, "message.send", user_id=user["id"], entity="message", entity_id=None,
                   data={"chars": len(body), "gateway_id": gid})
    log.info("free SMS handed to SMSGate for %s by %s", mask(number), user["login"])
    return _message_page(request, user, sent=mask(number))


# ------------------------------------------------------------- diagnostic

async def _check(name, probe):
    """(name, ok, detail): ok is True, False, or None when it cannot be checked here."""
    try:
        ok, detail = await probe()
    except httpx.HTTPStatusError as exc:
        ok, detail = False, f"réponse HTTP {exc.response.status_code}"
    except Exception as exc:  # any failure is a red line, never a crash of the page
        ok, detail = False, f"injoignable ({type(exc).__name__})"
    return name, ok, detail


TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


async def _tailscale_out():
    """100.100.100.100 is tailscaled's own address: it answers whether Tailscale runs on
    the host (containers reach the tailnet through its routing) or in a sidecar."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("100.100.100.100", 53), 3)
    except (OSError, asyncio.TimeoutError):
        return False, "100.100.100.100 ne répond pas : Tailscale arrêté sur l'hôte, ou hôte hors du tailnet"
    writer.close()
    return True, "Tailscale répond (100.100.100.100)"


async def _trophy():
    t0 = time.monotonic()
    today = date.today().isoformat()
    # A real query (HFSQL + API key), outside the cache.
    await trophy._fetch(config.trophy()["endpoints"]["holidays"], {"from": today, "to": today})
    return True, f"requête de test réussie en {(time.monotonic() - t0) * 1000:.0f} ms"


async def _smsgate():
    g = config.settings()["smsgate"]
    note = " (mode simulation : les SMS ne passent pas encore par lui)" if g.get("simulation") else ""
    t0 = time.monotonic()
    async with smsgate._client() as c:
        r = await c.get("/health")  # shortcut: /health from the SMSGate docs, check on the real phone
    if r.status_code == 401:
        return False, "identifiants refusés" + note
    r.raise_for_status()
    status = r.json().get("status")
    return status == "pass", f"état {status}, {(time.monotonic() - t0) * 1000:.0f} ms" + note


@app.get("/diagnostic", response_class=HTMLResponse)
async def diagnostic(request: Request, conn=Depends(get_conn)):
    user = require_user(request, conn)
    ip = _ip(request)
    try:
        inbound = any(ipaddress.ip_address(ip) in net for net in TAILNET)
    except ValueError:
        inbound = False
    checks = [("Tailscale entrant", True if inbound else None,
               f"cette page arrive par Tailscale ({ip})" if inbound
               else f"cette page n'arrive pas par Tailscale ({ip or 'adresse inconnue'}) : non vérifiable d'ici")]
    checks += await asyncio.gather(_check("Tailscale sortant", _tailscale_out), _check("API Trophy", _trophy),
                                   _check("SMSGate (téléphone)", _smsgate))
    return templates.TemplateResponse(request, "diagnostic.html", {"user": user, "checks": checks})
