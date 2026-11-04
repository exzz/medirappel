"""Routes. Run: uvicorn app.main:app (one process: the scheduler lives here)."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, journal, planner, scheduler
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
    task = None if os.environ.get("MEDIRAPPEL_NO_SCHEDULER") else asyncio.create_task(scheduler.run())
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(status=STATUS, mask=mask)


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


@app.get("/", response_class=HTMLResponse)
async def day_page(request: Request, day: str | None = None, practitioner: int | None = None,
                   conn=Depends(get_conn)):
    user = require_user(request, conn)
    managed = planner.managed_practitioners(conn)
    if user["role"] == "practitioner":
        pid, name = user["practitioner_id"], user["name"]
    else:
        choice = next((p for p in managed if p["practitioner_id"] == practitioner), managed[0] if managed else None)
        if choice is None:
            return templates.TemplateResponse(request, "day.html", {"user": user, "managed": [], "pid": None})
        pid, name = choice["practitioner_id"], choice["name"]

    today = planner.now().date()
    try:
        current = date.fromisoformat(day) if day else today
    except ValueError:
        raise HTTPException(400, "Date invalide.")
    tl = config.settings()["timeline"]

    warning = None
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
    })


# ---------------------------------------------------------------- actions

def _act(request: Request, conn, rid: int, action: str, sql: str, params: tuple) -> RedirectResponse:
    user = require_user(request, conn)
    row = conn.execute("SELECT practitioner_id, appointment_at FROM reminder WHERE id = ?", (rid,)).fetchone()
    if row is None or (user["role"] == "practitioner" and row["practitioner_id"] != user["practitioner_id"]):
        raise HTTPException(404, "SMS introuvable.")
    if conn.execute(sql, (*params, rid)).rowcount:
        journal.record(conn, f"reminder.{action}", user_id=user["id"], entity="reminder", entity_id=rid)
    else:
        log.info("reminder.%s refused (wrong status) user=%s reminder=%s", action, user["login"], rid)
    return RedirectResponse(f"/?day={row['appointment_at'][:10]}&practitioner={row['practitioner_id']}",
                            status_code=303)


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
