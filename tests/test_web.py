import time

import pytest
from fastapi.testclient import TestClient

from mail_radar import db, ops
from mail_radar import unsubscribe as un
from mail_radar.config import make_account
from mail_radar.demo import generate
from mail_radar.web import create_app


@pytest.fixture
def app(tmp_path):
    p = tmp_path / "x.db"
    generate(db.connect(p), months=6)
    return create_app(p, account=None, rules_path=tmp_path / "rules.yaml",
                      allowed_hosts={"testserver"})


def H(app):
    return {"X-MR-Token": app.state.token}


def wait(client):
    for _ in range(100):
        j = client.get("/api/job").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job non terminato")


def test_token_and_host_guard(app):
    c = TestClient(app)
    assert c.put("/api/rules", json={"text": "rules: []"}).status_code == 403
    assert c.get("/api/overview", headers={"Host": "evil.example"}).status_code == 403
    assert app.state.token in c.get("/").text
    r = c.put("/api/rules", json={"text": "rules: []"}, headers=H(app))
    assert r.status_code == 200 and c.get("/api/rules").json()["text"] == "rules: []"
    assert c.put("/api/rules", json={"text": "rules: [{match: {}}]"}, headers=H(app)).status_code == 400


def test_account_and_scan_job(app, monkeypatch):
    c = TestClient(app)
    assert c.post("/api/scan", json={}, headers=H(app)).status_code == 409
    r = c.post("/api/account", json={"user": "a@libero.it", "provider": "libero"}, headers=H(app)).json()
    assert r["host"] == "imapmail.libero.it" and r["smtp_host"] == "smtp.libero.it" and not r["has_password"]
    assert c.post("/api/account", json={"password": "pw"}, headers=H(app)).json()["has_password"]

    def fake_scan(acc, conn, folders=None, full=False, log=None, progress=None, **_):
        progress("Scan INBOX", 1, 2)
        log("INBOX: 3 nuove")
        return [{"folder": "INBOX", "new": 3, "removed": 1, "total": 10}]

    monkeypatch.setattr(ops, "scan", fake_scan)
    j = c.post("/api/scan", json={"folders": ["INBOX"]}, headers=H(app)).json()
    assert j["kind"] == "scan"
    j = wait(c)
    assert j["status"] == "done" and j["result"] == {"new": 3, "removed": 1}
    assert "INBOX: 3 nuove" in j["log"]


def test_rules_apply_job(app, monkeypatch):
    c = TestClient(app)
    c.post("/api/account", json={"user": "a@libero.it", "password": "pw"}, headers=H(app))
    got = {}
    monkeypatch.setattr(ops, "apply_matches", lambda acc, conn, ms, log: got.setdefault("n", len(ms)))
    c.post("/api/rules/apply", json={"text": "rules: [{name: z, match: {from_domain: zalando.it}}]"},
           headers=H(app))
    j = wait(c)
    assert j["status"] == "done" and got["n"] == j["result"]["moved"] > 0


def test_unsubscribe_plan_and_execute_reclassifies(app, monkeypatch):
    c = TestClient(app)
    c.post("/api/unsubscribe/plan", json={"senders": ["newsletter@zalando.it", "deals@aliexpress.com"]},
           headers=H(app))
    j = wait(c)
    by = {x["sender"]: x for x in j["result"]["candidates"]}
    assert by["newsletter@zalando.it"]["safe"] and not by["deals@aliexpress.com"]["safe"]

    c.post("/api/account", json={"user": "a@libero.it", "password": "pw"}, headers=H(app))
    calls = []
    monkeypatch.setattr(un, "do_one_click", lambda url: (calls.append(url) or True, "HTTP 200"))
    monkeypatch.setattr(un.time, "sleep", lambda s: None)
    # la GUI chiede anche un mittente non sicuro: il server deve ignorarlo
    c.post("/api/unsubscribe/execute",
           json={"senders": ["newsletter@zalando.it", "deals@aliexpress.com"], "trash": False},
           headers=H(app))
    j = wait(c)
    assert j["status"] == "done" and j["result"]["ok"] == 1 and len(calls) == 1
    hist = c.get("/api/unsubscribe/history").json()
    assert [h["sender"] for h in hist] == ["newsletter@zalando.it"]


def test_one_job_at_a_time(app, monkeypatch):
    import threading

    ev = threading.Event()
    c = TestClient(app)
    c.post("/api/account", json={"user": "a@libero.it", "password": "pw"}, headers=H(app))
    monkeypatch.setattr(ops, "scan", lambda *a, **k: ev.wait(2) and [])
    c.post("/api/scan", json={}, headers=H(app))
    assert c.post("/api/scan", json={}, headers=H(app)).status_code == 409
    ev.set()
    wait(c)


def test_make_account_unknown_provider():
    with pytest.raises(ValueError):
        make_account("a@sconosciuto.example")


def test_unsubscribe_force_via_api(app, monkeypatch):
    c = TestClient(app)
    c.post("/api/unsubscribe/plan", json={"all": True, "force": True}, headers=H(app))
    by = {x["sender"]: x for x in wait(c)["result"]["candidates"]}
    assert by["deals@aliexpress.com"]["safe"] and by["deals@aliexpress.com"]["warnings"]

    c.post("/api/account", json={"user": "a@libero.it", "password": "pw"}, headers=H(app))
    monkeypatch.setattr(un, "do_one_click", lambda url: (True, "HTTP 200"))
    monkeypatch.setattr(un.time, "sleep", lambda s: None)
    c.post("/api/unsubscribe/execute", json={"senders": ["deals@aliexpress.com"]}, headers=H(app))
    assert wait(c)["result"]["ok"] == 0  # senza force il server lo scarta
    c.post("/api/unsubscribe/execute", json={"senders": ["deals@aliexpress.com"], "force": True},
           headers=H(app))
    assert wait(c)["result"]["ok"] == 1
