"""SMSGate client, local-server mode on the Android phone, reached over Tailscale.

shortcut: endpoints and payload follow the SMSGate local-server README (POST /message,
GET /message/{id}); check them against the real phone before going live.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from . import config
from .planner import mask

log = logging.getLogger(__name__)
transport: httpx.AsyncBaseTransport | None = None  # tests

def _client() -> httpx.AsyncClient:
    g = config.settings()["smsgate"]
    return httpx.AsyncClient(base_url=g["url"].rstrip("/"), auth=(g["username"], g["password"]),
                             timeout=30, transport=transport)


async def send(phone: str, text: str) -> str:
    """Hand one SMS to the phone; return its gateway id. Raises on any failure."""
    g = config.settings()["smsgate"]
    phone = g.get("test_number") or phone
    if g.get("simulation"):
        log.info("simulation: SMS to %s (%d chars)", mask(phone), len(text))
        return "sim-" + uuid.uuid4().hex
    # Our own id: once the phone said yes, nothing below may raise (a raise means a second SMS).
    # shortcut: a timeout after the phone accepted still leads to a retry (2nd SMS); rare on Tailscale.
    gid = uuid.uuid4().hex
    async with _client() as c:
        r = await c.post("/message", json={"id": gid, "textMessage": {"text": text}, "phoneNumbers": [phone],
                                          "withDeliveryReport": True})
    r.raise_for_status()
    try:
        return r.json().get("id") or gid
    except (ValueError, AttributeError):
        return gid


async def state(gateway_id: str) -> str:
    """Pending | Processed | Sent | Delivered | Failed."""
    if gateway_id.startswith("sim-"):
        return "Delivered"
    async with _client() as c:
        r = await c.get(f"/message/{gateway_id}")
    r.raise_for_status()
    return r.json()["state"]
