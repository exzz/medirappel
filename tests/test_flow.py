"""Planning, sending, retry and web actions against a fake Trophy and simulated SMSGate."""

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

import httpx

from app import config, main, planner, scheduler, smsgate, trophy
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
    assert r[3]["status"] == "planned"  # SMSACTIF ignored
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
        assert (rows(conn)[1]["status"], rows(conn)[1]["reason"]) == ("skipped", planner.LATE)
        # A later batch (scheduler re-read) must not resurrect it.
        conn.execute("UPDATE api_cache SET fetched_at = 0")
        run(scheduler.send_due(conn))
        assert rows(conn)[1]["status"] == "skipped"
        # « Envoyer maintenant »: sent at once, and only once.
        rid = rows(conn)[1]["id"]
        assert "Envoyer maintenant" in client.get("/?day=2026-11-10").text
        client.post(f"/reminder/{rid}/send")
        assert rows(conn)[1]["status"] == "submitted"
        assert "Envoyer maintenant" not in client.get("/?day=2026-11-10").text
        client.post(f"/reminder/{rid}/send")
    assert [r["action"] for r in conn.execute("SELECT action FROM journal")].count("reminder.send_now") == 1


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
    # A page view during the wait must not turn the retry into « pris après l'envoi ».
    d = datetime(2026, 11, 10).date()
    run(planner.sync(conn, 1, "Dr Bellec", d, d, force=True, late_before=clock["now"]))
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


def test_refresh_button_rereads_whole_upcoming_agenda(env, clock):
    fake, conn = env
    with TestClient(scheduler_app()) as client:
        login(client)
        fake.appointments = [appt(1, "2026-11-10T10:00"), appt(2, "2026-11-20T10:00")]
        r = client.post("/refresh", data={"day": "2026-11-10", "practitioner": 2})  # foreign id ignored
        assert "Planning à venir rechargé" in r.text
        assert {1, 2} <= set(rows(conn)) and rows(conn)[2]["practitioner_id"] == 1
        fake.appointments = [appt(1, "2026-11-10T10:00")]
        client.post("/refresh", data={"day": "2026-11-10"})
        assert rows(conn)[2]["status"] == "voided"
    assert "planning.refresh" in [r["action"] for r in conn.execute("SELECT action FROM journal")]


def test_cache_spares_trophy(env, clock):
    fake, conn = env
    run(scheduler.plan_and_send(conn))
    calls = fake.calls
    run(planner.calendar(1))
    assert fake.calls == calls  # practitioners and holidays served from disk


def test_missed_in_trophy_is_neither_sent_nor_listed(env, clock):
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00"), appt(2, "2026-11-10T11:00"),
                         {**appt(3, "2026-11-10T12:00"), "absent": True}]
    run(scheduler.plan_and_send(conn))
    assert set(rows(conn)) == {1, 2}
    rows_ = rows(conn)
    conn.execute("UPDATE reminder SET status = 'delivered' WHERE id = ?", (rows_[2]["id"],))
    fake.appointments = [{**a, "absent": True} for a in fake.appointments]  # ticked after planning
    conn.execute("UPDATE api_cache SET fetched_at = 0")
    clock["now"] = datetime(2026, 11, 9, 9, 0)
    run(scheduler.send_due(conn))
    assert set(rows(conn)) == {2} and rows(conn)[2]["status"] == "delivered"  # sent SMS keeps its status


def test_exclude_and_include_patient(env, clock, caplog):
    caplog.set_level("INFO")
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00"), appt(2, "2026-11-12T10:00")]
    for a in fake.appointments:  # same patient, two days
        a["patient"]["id"] = 77
    with TestClient(scheduler_app()) as client:
        login(client)
        client.get("/?day=2026-11-10")
        run(scheduler.plan_and_send(conn))
        client.post(f"/reminder/{rows(conn)[1]['id']}/exclude")
        # Both SMS of the patient are stopped at once, the other day included.
        assert {r["status"] for r in rows(conn).values()} == {"skipped"}
        page = client.get("/?day=2026-11-10").text
        assert "Réactiver" in page and "Refus SMS</span>" in page and "Non envoyé" not in page
        assert rows(conn)[1]["reason"] == planner.EXCLUDED  # the re-read keeps it
        client.post(f"/reminder/{rows(conn)[1]['id']}/include")
        client.get("/?day=2026-11-10")
        assert rows(conn)[1]["status"] == "planned"
    actions = [r["action"] for r in conn.execute("SELECT action FROM journal")]
    assert "patient.exclude" in actions and "patient.include" in actions
    # Logs may leave the server: no Trophy patient id there (the journal table keeps it).
    assert "patient.exclude user=bellec patient=- " in caplog.text and "=77" not in caplog.text


def test_assistant_sees_only_her_practitioners(env, clock):
    fake, conn = env
    fake.appointments = [{**appt(1, "2026-11-10T10:00"), "praticien_id": 1},
                         {**appt(2, "2026-11-10T11:00"), "praticien_id": 2}]
    run(scheduler.plan_and_send(conn))
    with TestClient(scheduler_app()) as client:
        login(client, user_id=3)
        page = client.get("/?day=2026-11-10&practitioner=1").text  # not hers: falls back to Dr Autre
        assert "Dr Autre" in page and "Dr Bellec" not in page and 'id="practitioner"' in page
        assert client.post(f"/reminder/{rows(conn)[1]['id']}/cancel").status_code == 404
        client.post(f"/reminder/{rows(conn)[2]['id']}/cancel")
        assert rows(conn)[2]["status"] == "cancelled"
    assert "RDV avec Dr Autre" in rows(conn)[2]["body"]  # the SMS keeps the practitioner's name


def test_old_user_table_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    from app import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("""CREATE TABLE user (id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('practitioner', 'admin')), practitioner_id INTEGER,
        pin_hash TEXT NOT NULL, pin_salt TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT)""")
    old.execute("INSERT INTO user (id, login, name, role, pin_hash, pin_salt) VALUES (7, 'a', 'A', 'admin', 'h', 's')")
    old.commit(); old.close()
    db.init_db()
    conn = db.connect()
    assert conn.execute("SELECT id FROM user WHERE login = 'a'").fetchone()[0] == 7
    conn.execute("INSERT INTO user (login, name, role, pin_hash, pin_salt) VALUES ('b', 'B', 'assistant', 'h', 's')")


def test_free_message(env, clock, caplog):
    caplog.set_level("INFO")
    fake, conn = env
    with TestClient(scheduler_app()) as client:
        login(client)
        assert "Envoyer un message" in client.get("/?day=2026-11-10").text
        r = client.post("/message", data={"phone": "01 23 45 67 89", "text": "Bonjour"})
        assert "Numéro invalide" in r.text
        r = client.post("/message", data={"phone": "06 98 76 54 32", "text": "Rendez-vous décalé à 15h"})
        assert "SMS transmis au téléphone pour +3369876****" in r.text
    row = conn.execute("SELECT data FROM journal WHERE action = 'message.send'").fetchone()
    assert row and "98765432" not in row["data"] and "15h" not in row["data"]
    assert "06 98 76 54 32" not in caplog.text and "+33698765432" not in caplog.text


def test_patient_picker_full_then_first_page(env, clock, monkeypatch):
    fake, conn = env
    monkeypatch.setattr(trophy, "PAGE", 2)
    pat = lambda i, nom, prenom, born=None, mobile="": {  # noqa: E731
        "id": i, "nom": nom, "prenom": prenom, "naissance": born, "mobile": mobile, "tel": ""}
    fake.patients = [pat(3, "Martin", "Léa", "1980-03-12", "06 11 22 33 44"), pat(2, "MARTIN", "Lea", "1975-01-02"),
                     pat(1, "Dupont", "Jean", None, "0612345678")]
    run(trophy.refresh_patients())  # no cache: full load, 2 pages
    fake.patients.insert(0, pat(4, "Lemartin", "Zoé"))
    calls = fake.calls
    run(trophy.refresh_patients())  # cached: first page only
    assert fake.calls == calls + 1
    with TestClient(scheduler_app()) as client:
        login(client)
        hits = client.post("/message/patients", data={"q": "martin lea"}).json()
        assert [h["phone"] for h in hits] == ["0611223344", None]
        assert hits[0]["name"] == "MARTIN Léa" and hits[0]["detail"] == "né(e) le 12/03/1980"
        assert hits[1]["detail"].endswith("pas de mobile")
        assert client.post("/message/patients", data={"q": "martin"}).json()[-1]["name"] == "LEMARTIN Zoé"


def test_diagnostic(env, clock, monkeypatch):
    async def no_tailnet():
        return None, "test"
    monkeypatch.setattr(main, "_tailscale_out", no_tailnet)
    monkeypatch.setattr(smsgate, "transport", httpx.MockTransport(
        lambda r: httpx.Response(200, json={"status": "pass"}) if r.url.path == "/health" else httpx.Response(404)))
    with TestClient(scheduler_app()) as client:
        login(client)
        assert 'href="/diagnostic"' in client.get("/message").text
        html = client.get("/diagnostic").text
        assert "Mode simulation" in client.get("/?day=2026-11-10").text  # settings.example: simulation on
    assert html.count('class="ok"') == 2 and "requête de test réussie" in html and "état pass" in html


def test_auto_send_off_keeps_planned_but_sends_manual_retry(env, clock):
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00"), appt(2, "2026-11-10T11:00")]
    config.settings()["auto_send"] = False  # cache cleared by the fixture
    run(scheduler.plan_and_send(conn))
    conn.execute("UPDATE reminder SET status = 'planned', attempts = 1 WHERE appointment_id = 2")  # « Relancer »
    clock["now"] = datetime(2026, 11, 9, 9, 0)
    run(scheduler.send_due(conn))
    assert rows(conn)[1]["status"] == "planned" and rows(conn)[2]["status"] == "submitted"
    clock["now"] = datetime(2026, 11, 9, 20, 30)
    run(scheduler.send_due(conn))
    assert rows(conn)[1]["status"] == "missed"


def test_carddav_book_per_practitioner(env, clock):
    fake, conn = env
    auth_ok = ("telephone", "changer-moi")  # settings.example.json
    pat = lambda i, prat, mobile: {"id": i, "nom": "Durand", "prenom": f"Zoé{i}", "naissance": None,  # noqa: E731
                                   "mobile": mobile, "tel": "", "praticien_id": prat}
    with TestClient(scheduler_app()) as client:
        dav = lambda m, path, **kw: client.request(m, path, auth=kw.pop("auth", auth_ok), **kw)  # noqa: E731
        assert dav("PROPFIND", "/carddav/1/").status_code == 503  # empty cache: never an empty book
        fake.patients = [pat(1, 1, "06 11 22 33 44"), pat(2, 1, "01 23 45 67 89"), pat(3, 2, "0612345678")]
        run(trophy.refresh_patients())
        assert dav("PROPFIND", "/carddav/1/", auth=("telephone", "faux")).status_code == 401
        r = dav("PROPFIND", "/carddav/1/", headers={"Depth": "1"})
        assert r.status_code == 207 and "<card:addressbook/>" in r.text and "Dr Bellec" in r.text
        assert "/carddav/1/1.vcf" in r.text and "2.vcf" not in r.text and "3.vcf" not in r.text
        r = dav("REPORT", "/carddav/1/", content=(
            '<card:addressbook-multiget xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            '<d:prop><d:getetag/><card:address-data/></d:prop><d:href>/carddav/1/1.vcf</d:href></card:addressbook-multiget>'))
        assert "FN:DURAND Zoé1" in r.text and "TEL;TYPE=CELL:+33611223344" in r.text
        assert dav("GET", "/carddav/1/1.vcf").headers["etag"]
        assert dav("PUT", "/carddav/1/9.vcf", content="x").status_code == 403
        assert dav("PROPFIND", "/carddav/2/").status_code == 207  # Dr Autre has an account
        assert dav("PROPFIND", "/carddav/99/").status_code == 404


def test_retry_refused_once_the_appointment_is_past(env, clock):
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00")]
    run(scheduler.plan_and_send(conn))
    conn.execute("UPDATE reminder SET status = 'failed'")
    rid = rows(conn)[1]["id"]
    clock["now"] = datetime(2026, 11, 10, 11, 0)  # appointment over
    with TestClient(scheduler_app()) as client:
        login(client)
        assert "Relancer" not in client.get("/?day=2026-11-10").text
        client.post(f"/reminder/{rid}/retry")
    assert rows(conn)[1]["status"] == "failed"


def test_poll_states_skips_an_unknown_message(env, clock, monkeypatch):
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00"), appt(2, "2026-11-10T11:00")]
    run(scheduler.plan_and_send(conn))
    conn.execute("UPDATE reminder SET status = 'submitted', sent_at = '2026-11-09T09:00',"
                 " gateway_id = 'g' || appointment_id")
    monkeypatch.setattr(smsgate, "transport", httpx.MockTransport(
        lambda r: httpx.Response(404) if r.url.path == "/message/g1" else httpx.Response(200, json={"state": "Delivered"})))
    run(scheduler.poll_states(conn))
    assert (rows(conn)[1]["status"], rows(conn)[2]["status"]) == ("submitted", "delivered")


def test_health_line(env, clock, caplog, monkeypatch):
    async def tailnet_ok():
        return True, "ok"
    monkeypatch.setattr(main, "_tailscale_out", tailnet_ok)
    monkeypatch.setattr(smsgate, "transport", httpx.MockTransport(lambda r: httpx.Response(503)))
    config.settings()["smsgate"]["simulation"] = False  # cache cleared by the fixture
    run(main.health_line())
    rec = [r for r in caplog.records if r.getMessage().startswith("health ")][-1]
    assert rec.levelname == "WARNING"
    assert rec.getMessage() == "health tailscale=ok trophy=ok smsgate=fail | smsgate: réponse HTTP 503"


def test_no_phone_number_in_logs_and_healthcheck_dropped(env, clock, caplog):
    import logging
    caplog.set_level("INFO")
    fake, conn = env
    fake.appointments = [appt(1, "2026-11-10T10:00")]
    run(scheduler.plan_and_send(conn))
    clock["now"] = datetime(2026, 11, 9, 9, 0)
    run(scheduler.plan_and_send(conn))
    assert "handed to SMSGate" in caplog.text and "+336" not in caplog.text
    f = main._NoHealthcheck()
    rec = lambda ip: logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d',  # noqa: E731
                                       (ip, "GET", "/login", "1.1", 200), None)
    assert not f.filter(rec("127.0.0.1:5000")) and f.filter(rec("100.64.0.9:5000"))


def test_installable_web_app(env, clock):
    with TestClient(scheduler_app()) as client:
        assert '<link rel="manifest" href="/static/manifest.json">' in client.get("/login").text
        m = client.get("/static/manifest.json").json()
        assert m["display"] == "standalone" and m["start_url"] == "/"
        for icon in m["icons"]:  # Android needs 192 and 512, all reachable
            assert client.get(icon["src"]).status_code == 200
        assert {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"}
