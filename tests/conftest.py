"""Shared fixtures: temp config + data dir, fake Trophy API, frozen clock."""

import json
from datetime import datetime

import httpx
import pytest

from app import auth, config, db, planner, scheduler, trophy

PRACTITIONER = {
    "id": 1, "nom": "BELLEC Antoine", "inactif": False,
    "horaires": [{"jour_semaine": d,
                  "matin": {"ouvert": d <= 5, "debut": "08:30", "fin": "12:30"},
                  "apres_midi": {"ouvert": d <= 4, "debut": "14:00", "fin": "19:00"}}
                 for d in range(1, 8)],
}


class FakeTrophy:
    def __init__(self):
        self.appointments = []
        self.patients = []  # newest first, like the API
        self.holidays = [{"date": "2026-11-11", "libelle": "Armistice"}]
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "k"
        self.calls += 1
        if request.url.path == "/practitioners":
            return httpx.Response(200, json=[PRACTITIONER])
        if request.url.path == "/holidays":
            return httpx.Response(200, json=self.holidays)
        if request.url.path == "/patients":
            q = request.url.params
            start = int(q["offset"])
            return httpx.Response(200, json=self.patients[start:start + int(q["limit"])])
        if request.url.path == "/appointments":
            q = request.url.params
            day = lambda a: a["debut"][:10]  # noqa: E731
            return httpx.Response(200, json=[a for a in self.appointments
                                             if q["from"] <= day(a) <= q["to"]
                                             and str(a.get("praticien_id", q["practitioner"])) == q["practitioner"]])
        return httpx.Response(404)


def appt(aid, when, mobile="06 12 34 56 78", sms=True, type_="Soins"):
    return {"id": aid, "debut": when, "type": type_,
            "patient": {"id": 100 + aid, "nom": "DURAND", "prenom": f"Paul{aid}",
                        "mobile": mobile, "tel": "", "sms_actif": sms}}


@pytest.fixture
def clock(monkeypatch):
    t = {"now": datetime(2026, 11, 9, 8, 0)}  # Monday
    monkeypatch.setattr(planner, "now", lambda: t["now"])
    return t


@pytest.fixture
def env(tmp_path, monkeypatch, clock):
    settings = json.load(open(config.BASE_DIR / "settings.example.json"))
    settings["throttle"] = {"min_delay_s": 0, "max_delay_s": 0}
    pin_hash, salt = auth.hash_pin("123456")
    users = {"users": [
        {"login": "bellec", "name": "Dr Bellec", "role": "practitioner", "trophy_practitioner_id": 1,
         "pin_hash": pin_hash, "pin_salt": salt},
        {"login": "other", "name": "Dr Autre", "role": "practitioner", "trophy_practitioner_id": 2,
         "pin_hash": pin_hash, "pin_salt": salt},
        {"login": "assist", "name": "Assistante", "role": "assistant", "trophy_practitioner_ids": [2],
         "pin_hash": pin_hash, "pin_salt": salt},
    ]}
    for name, data in (("settings.json", settings), ("users.json", users),
                       ("trophy.json", {"url": "http://trophy", "api_key": "k"})):
        (tmp_path / name).write_text(json.dumps(data))
    monkeypatch.setenv("MEDIRAPPEL_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MEDIRAPPEL_USERS", str(tmp_path / "users.json"))
    monkeypatch.setenv("MEDIRAPPEL_TROPHY", str(tmp_path / "trophy.json"))
    monkeypatch.setenv("MEDIRAPPEL_NO_SCHEDULER", "1")
    monkeypatch.setattr(scheduler, "_built_on", None)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    config.settings.cache_clear()
    config.trophy.cache_clear()
    fake = FakeTrophy()
    monkeypatch.setattr(trophy, "transport", httpx.MockTransport(fake.handler))
    db.init_db()
    conn = db.connect()
    auth.sync_users(conn, config.users())
    yield fake, conn
    conn.close()
    config.settings.cache_clear()
    config.trophy.cache_clear()
