"""Planning, sending, retry and web actions against a fake Trophy and simulated SMSGate."""

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from app import planner, scheduler, smsgate
from tests.conftest import appt


def rows(conn):
    return {r["appointment_id"]: r for r in conn.execute("SELECT * FROM reminder")}


def run(coro):
    return asyncio.run(coro)


def test_plan_send_and_deliver(env, clock):
    fake, conn = env
    fake.appointments = [
        appt(1, "2026-11-10T10:00"),                       # Tuesday -> SMS Monday 09:00
        appt(2, "2026-11-10T11:00", mobile="01 23 45 67 89"),
        appt(3, "2026-11-10T12:00", sms=False),
        appt(4, "2026-11-12T09:00"),                       # Thursday, Wed 11 holiday -> Tue 09:00
        {"id": 5, "debut": "2026-11-10T13:00", "type": "Pause", "patient": None},
    ]
    run(scheduler.plan_and_send(conn))  # 08:00: plan only
    r = rows(conn)
    assert 5 not in r
    assert (r[1]["status"], r[1]["send_at"]) == ("planned", "2026-11-09T09:00")
    assert (r[2]["status"], r[2]["reason"]) == ("skipped", "pas de mobile")
    assert (r[3]["status"], r[3]["reason"]) == ("skipped", "refus SMS")
    assert r[4]["send_at"] == "2026-11-10T09:00"
    assert "Dr Bellec" in r[1]["body"] and "mardi 10 novembre a 10h00" in r[1]["body"]

    clock["now"] = datetime(2026, 11, 9, 9, 0)
    run(scheduler.plan_and_send(conn))
    assert rows(conn)[1]["status"] == "submitted"
    assert rows(conn)[4]["status"] == "planned"
    run(scheduler.poll_states(conn))
    assert rows(conn)[1]["status"] == "delivered"


def test_cancelled_in_trophy_is_not_sent(env, clock):
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00")]
    run(scheduler.plan_and_send(conn))
    fake.appointments = []  # deleted in Trophy, nobody opened the page
    clock["now"] = datetime(2026, 11, 9, 9, 0)
    run(scheduler.plan_and_send(conn))
    assert (rows(conn)[1]["status"], rows(conn)[1]["reason"]) == ("voided", "rendez-vous supprime")


def test_taken_too_late_and_missed(env, clock):
    fake, conn = env
    clock["now"] = datetime(2026, 11, 9, 10, 0)
    fake.appointments = [appt(1, "2026-11-10T10:00")]  # SMS was due at 09:00 today
    client = TestClient(scheduler_app())
    with client:
        login(client)
        client.get("/?day=2026-11-10")
    assert (rows(conn)[1]["status"], rows(conn)[1]["reason"]) == ("skipped", "pris trop tard")
    # A later batch (scheduler re-read) must not resurrect it.
    conn.execute("UPDATE api_cache SET fetched_at = 0")
    run(scheduler.send_due(conn))
    assert rows(conn)[1]["status"] == "skipped"


def test_downtime_after_deadline_is_missed(env, clock):
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00")]
    run(scheduler.plan_and_send(conn))
    clock["now"] = datetime(2026, 11, 9, 21, 0)
    run(scheduler.send_due(conn))
    assert rows(conn)[1]["status"] == "missed"


def test_one_retry_then_failed(env, clock, monkeypatch):
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00")]

    async def broken(phone, text):
        raise OSError("phone unreachable")
    monkeypatch.setattr(smsgate, "send", broken)
    run(scheduler.plan_and_send(conn))
    clock["now"] = datetime(2026, 11, 9, 9, 0)
    run(scheduler.send_due(conn))
    assert (rows(conn)[1]["status"], rows(conn)[1]["send_at"]) == ("planned", "2026-11-09T10:00")
    clock["now"] = datetime(2026, 11, 9, 10, 0)
    run(scheduler.send_due(conn))
    assert rows(conn)[1]["status"] == "failed"


def scheduler_app():
    from app.main import app
    return app


def login(client, user_id=1):
    r = client.post("/login", data={"user_id": user_id, "pin": "123456"}, follow_redirects=False)
    assert r.status_code == 303


def test_web_cancel_restore_and_isolation(env, clock, caplog):
    caplog.set_level("INFO")
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00")]
    with TestClient(scheduler_app()) as client:
        login(client)
        page = client.get("/?day=2026-11-10")
        assert page.status_code == 200 and "DURAND Paul1" in page.text and "+3361234****" in page.text
        rid = rows(conn)[1]["id"]
        client.post(f"/reminder/{rid}/cancel")
        assert rows(conn)[1]["status"] == "cancelled"
        client.get("/?day=2026-11-10")  # re-sync keeps the cancellation
        assert rows(conn)[1]["status"] == "cancelled"
        client.post(f"/reminder/{rid}/restore")
        assert rows(conn)[1]["status"] == "planned"
        # Timeline greys the holiday and the weekend.
        assert 'class="closed' in page.text
    with TestClient(scheduler_app()) as other:
        login(other, user_id=2)
        assert other.post(f"/reminder/{rid}/cancel").status_code == 404
    actions = [r["action"] for r in conn.execute("SELECT action FROM journal")]
    assert "reminder.cancel" in actions and "reminder.restore" in actions
    for line in ("auth.login user=bellec", "reminder.cancel user=bellec reminder=", "reminder.restore user=bellec",
                 "agenda sync practitioner=1"):
        assert line in caplog.text, line


def test_cache_spares_trophy(env, clock):
    fake, conn = env
    run(scheduler.plan_and_send(conn))
    calls = fake.calls
    run(planner.calendar(1))
    assert fake.calls == calls  # practitioners and holidays served from disk
