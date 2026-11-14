"""Configuration files: settings.json (Trophy API and SMSGate included), users.json (neither versioned).

Paths can be overridden by MEDIRAPPEL_SETTINGS / MEDIRAPPEL_USERS
(tests, container). Read once at startup; a restart applies changes.
"""

from __future__ import annotations

import json
import os
import re
from datetime import time
from functools import cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "reminder_days_before": 1,
    "send_time": "09:00",
    "send_deadline": "20:00",
    "auto_send": True,
    "excluded_appointment_types": [],
    "throttle": {"min_delay_s": 60, "max_delay_s": 90},
    "retry_delay_minutes": 60,
    "cache_ttl_s": {"practitioners": 86400, "calendar": 86400, "appointments": 900},
    "timeline": {"past_days": 30, "future_days": 14},
    "retention_days": 365,
}


class ConfigError(Exception):
    """Missing or invalid configuration: the application refuses to start."""


def _read(env: str, name: str) -> dict:
    path = Path(os.environ.get(env, BASE_DIR / name))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object.")
    return data


def _hhmm(value: str, key: str) -> time:
    if not re.fullmatch(r"\d{2}:\d{2}", str(value)):
        raise ConfigError(f"settings.json: {key} must be HH:MM.")
    return time.fromisoformat(value)


@cache
def settings() -> dict:
    s = {**DEFAULTS, **_read("MEDIRAPPEL_SETTINGS", "settings.json")}
    for key in ("practice", "sms_template", "smsgate"):
        if not s.get(key):
            raise ConfigError(f"settings.json: '{key}' is required.")
    for key in ("name", "address", "phone"):
        if not s["practice"].get(key):
            raise ConfigError(f"settings.json: practice.{key} is required.")
    gate = s["smsgate"]
    if not gate.get("simulation") and not all(gate.get(k) for k in ("url", "username", "password")):
        raise ConfigError("settings.json: smsgate.url, username and password are required outside simulation.")
    if not isinstance(s["reminder_days_before"], int) or s["reminder_days_before"] < 0:
        raise ConfigError("settings.json: reminder_days_before must be a positive integer.")
    c = s.get("contacts")
    if c is not None and not (isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k] for k in ("username", "password"))):
        raise ConfigError("settings.json: contacts needs a username and a password (or remove the block).")
    if not isinstance(s["auto_send"], bool):
        raise ConfigError("settings.json: auto_send must be true or false.")
    s["send_time"] = _hhmm(s["send_time"], "send_time")
    s["send_deadline"] = _hhmm(s["send_deadline"], "send_deadline")
    t = s["throttle"]
    if not 0 <= t["min_delay_s"] <= t["max_delay_s"]:
        raise ConfigError("settings.json: throttle needs 0 <= min_delay_s <= max_delay_s.")
    s["excluded_appointment_types"] = [x.strip().lower() for x in s["excluded_appointment_types"]]
    return s


@cache
def trophy() -> dict:
    t = dict(settings().get("trophy") or {})
    if not t.get("url") or not t.get("api_key"):
        raise ConfigError("settings.json: trophy.url and trophy.api_key are required.")
    t["endpoints"] = {
        "practitioners": "/practitioners",
        "appointments": "/appointments",
        "holidays": "/holidays",
        "patients": "/patients",
        **t.get("endpoints", {}),
    }
    return t


def users() -> list[dict]:
    data = _read("MEDIRAPPEL_USERS", "users.json").get("users")
    if not isinstance(data, list):
        raise ConfigError("users.json must contain a 'users' list.")
    seen = set()
    for n, u in enumerate(data, 1):
        # Never echo the entry: it holds the PIN hash.
        missing = [k for k in ("login", "name", "initials", "role", "pin_hash", "pin_salt") if not u.get(k)]
        if missing:
            raise ConfigError(f"users.json: entry {n} lacks {', '.join(missing)}.")
        if u["role"] not in ("practitioner", "assistant", "admin"):
            raise ConfigError(f"users.json: entry {n} has an unknown role.")
        if u["role"] == "practitioner" and not isinstance(u.get("trophy_practitioner_id"), int):
            raise ConfigError(f"users.json: entry {n} needs an integer trophy_practitioner_id.")
        ids = u.get("trophy_practitioner_ids")
        if u["role"] == "assistant" and not (isinstance(ids, list) and ids and all(isinstance(i, int) for i in ids)):
            raise ConfigError(f"users.json: entry {n} needs a list of integer trophy_practitioner_ids.")
        if u["login"] in seen:
            raise ConfigError(f"users.json: duplicate login '{u['login']}'.")
        seen.add(u["login"])
    return data
