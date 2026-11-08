"""Client of the Trophy read-only API (../trophy), with a disk cache.

The API holds a single HFSQL connection: never two requests at once (one lock),
and as much as possible served from the `api_cache` table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date

import httpx

from . import config, db

log = logging.getLogger(__name__)
_lock = asyncio.Lock()
# Replaced by tests with an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None


async def _fetch(path: str, params: dict):
    cfg = config.trophy()
    async with _lock:
        async with httpx.AsyncClient(base_url=cfg["url"], timeout=30, transport=transport,
                                     headers={"X-API-Key": cfg["api_key"]}) as client:
            resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


async def _get(endpoint: str, params: dict, ttl: int, force: bool = False):
    cfg = config.trophy()
    path = cfg["endpoints"][endpoint]
    key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    conn = db.connect()
    try:
        if not force:
            row = conn.execute("SELECT fetched_at, body FROM api_cache WHERE key = ?", (key,)).fetchone()
            if row and time.time() - row["fetched_at"] < ttl:
                return json.loads(row["body"])
        data = await _fetch(path, params)
        conn.execute("INSERT OR REPLACE INTO api_cache (key, fetched_at, body) VALUES (?, ?, ?)",
                     (key, time.time(), json.dumps(data)))
        return data
    finally:
        conn.close()


def _ttl(kind: str) -> int:
    return config.settings()["cache_ttl_s"][kind]


async def practitioner(practitioner_id: int) -> dict | None:
    for p in await _get("practitioners", {}, _ttl("practitioners")):
        if p["id"] == practitioner_id:
            return p
    return None


async def holidays(year: int) -> set[date]:
    # Whole years: a stable cache key, refreshed once a day.
    rows = await _get("holidays", {"from": f"{year}-01-01", "to": f"{year}-12-31"}, _ttl("calendar"))
    return {date.fromisoformat(r["date"]) for r in rows if r.get("date")}


async def appointments(practitioner_id: int, start: date, end: date, force: bool = False) -> list[dict]:
    return await _get(
        "appointments",
        {"practitioner": practitioner_id, "from": start.isoformat(), "to": end.isoformat(),
         "with_patient": "true"},
        _ttl("appointments"), force,
    )


def purge_cache(conn, older_than_s: int) -> None:
    conn.execute("DELETE FROM api_cache WHERE fetched_at < ?", (time.time() - older_than_s,))


# --------------------------------------------------------------- patients
# /patients pages by 500, newest first: one full load a day (catches edits and
# archivings), otherwise the first page alone, merged, brings the new patients.
PAGE = 500
FULL_EVERY_S = 86400
_refreshing = False


def cached_patients(conn) -> list[dict]:
    row = conn.execute("SELECT body FROM api_cache WHERE key = 'patients'").fetchone()
    return list(json.loads(row["body"])["patients"].values()) if row else []


async def refresh_patients() -> None:
    """Never raises: on error the old list stays (search serves the cache)."""
    global _refreshing
    if _refreshing:
        return
    _refreshing = True
    conn = db.connect()
    try:
        row = conn.execute("SELECT body FROM api_cache WHERE key = 'patients'").fetchone()
        old = json.loads(row["body"]) if row else None
        # v2 adds praticien_id (address books): an older cache gets a full load.
        full = not old or time.time() - old["full_at"] > FULL_EVERY_S or old.get("v") != 2
        found = {}
        for n in range(1000 if full else 1):
            page = await _fetch(config.trophy()["endpoints"]["patients"], {"limit": PAGE, "offset": n * PAGE})
            found.update({str(p["id"]): {k: p.get(k) for k in ("id", "nom", "prenom", "naissance", "mobile", "tel", "praticien_id")}
                          for p in page})
            if len(page) < PAGE:
                break
        data = ({"v": 2, "full_at": time.time(), "patients": found} if full
                else {**old, "patients": {**old["patients"], **found}})
        conn.execute("INSERT OR REPLACE INTO api_cache (key, fetched_at, body) VALUES ('patients', ?, ?)",
                     (time.time(), json.dumps(data)))
        log.info("patients %s: %d read, %d cached", "full load" if full else "update", len(found), len(data["patients"]))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("patients refresh failed (%s), cache kept", type(exc).__name__)
    finally:
        _refreshing = False
        conn.close()


_tasks = set()  # keep background tasks referenced until they end
_last_soon = 0.0


def refresh_patients_soon() -> None:
    """Start a refresh without waiting for it, once a minute at most (a DAVx⁵ sync is
    hundreds of requests): callers serve the cache meanwhile."""
    global _last_soon
    if time.monotonic() - _last_soon < 60:
        return
    _last_soon = time.monotonic()
    task = asyncio.get_running_loop().create_task(refresh_patients())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
