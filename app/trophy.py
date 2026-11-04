"""Client of the Trophy read-only API (../trophy), with a disk cache.

The API holds a single HFSQL connection: never two requests at once (one lock),
and as much as possible served from the `api_cache` table.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date

import httpx

from . import config, db

_lock = asyncio.Lock()
# Replaced by tests with an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None


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
        async with _lock:
            async with httpx.AsyncClient(base_url=cfg["url"], timeout=30, transport=transport,
                                         headers={"X-API-Key": cfg["api_key"]}) as client:
                resp = await client.get(path, params=params)
            resp.raise_for_status()
            data = resp.json()
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
