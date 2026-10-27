import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mail_radar import db
from mail_radar import unsubscribe as un
from mail_radar.demo import generate
from mail_radar.headers import parse_header_block


def test_org_domain():
    assert un.org_domain("news.mail.myautogrill.it") == "myautogrill.it"
    assert un.org_domain("e.shop.co.uk") == "shop.co.uk"
    assert un.org_domain(None) == ""


def test_parse_list_unsubscribe():
    v = "<mailto:u@x.it?subject=stop>, <https://x.it/u?a=1> , <javascript:alert(1)>"
    assert un.parse_list_unsubscribe(v) == ["mailto:u@x.it?subject=stop", "https://x.it/u?a=1"]


@pytest.mark.parametrize("ar, dom, ok", [
    ("mx.libero.it; dmarc=pass (p=none) header.from=news.brand.it", "news.brand.it", True),
    ("mx.libero.it; dkim=pass header.d=brand.it header.s=k1; dmarc=none", "news.brand.it", True),
    ("mx.libero.it; dkim=pass header.d=esp.com; dmarc=fail header.from=brand.it", "brand.it", False),
    ("mx.libero.it; dkim=fail header.d=brand.it", "brand.it", False),
    ("mx.libero.it; dmarc=pass header.from=altro.it", "brand.it", False),
    (None, "brand.it", False),
])
def test_auth_verdict(ar, dom, ok):
    assert bool(un.auth_verdict(ar, dom)) is ok


def test_auth_uses_topmost_header_and_dkim_signature():
    raw = (b"Authentication-Results: mx.libero.it; dmarc=fail header.from=brand.it\r\n"
           b"Authentication-Results: fake.example; dmarc=pass header.from=brand.it\r\n"
           b"DKIM-Signature: v=1; a=rsa-sha256; d=brand.it;\r\n s=k1; h=from; b=xx\r\n"
           b"List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
           b"From: news@brand.it\r\n\r\n")
    r = parse_header_block(raw)
    assert r["auth_results"].startswith("mx.libero.it")
    assert r["dkim_domains"] == "brand.it"
    assert un.auth_verdict(r["auth_results"], "brand.it") is None
    assert un.auth_verdict(None, "brand.it", r["dkim_domains"], trust_dkim_signature=True)


def test_classify_demo():
    conn = db.connect(":memory:")
    generate(conn, months=6)
    senders = {r[0] for r in conn.execute("SELECT DISTINCT from_addr FROM messages")}
    cs = {c.sender: un.classify(c) for c in un.latest_per_sender(conn, senders)}
    assert cs["newsletter@zalando.it"].safe and cs["newsletter@zalando.it"].method == "one-click"
    assert cs["promo@trenitalia.it"].safe and cs["promo@trenitalia.it"].method == "mailto"
    assert cs["info@ikea.it"].method == "manual" and not cs["info@ikea.it"].safe
    assert not cs["deals@aliexpress.com"].safe  # DMARC fail, DKIM non allineato
    assert not cs["mario.rossi@gmail.com"].safe  # nessun List-Unsubscribe + interazioni


def test_classify_rejects_short_history_and_interactions():
    c = un.Candidate(sender="a@brand.it", domain="brand.it", name=None, n=2, months_active=1,
                     interactions=1, folder="INBOX", uidvalidity=1, uid=1,
                     list_unsubscribe="<https://brand.it/u>",
                     list_unsubscribe_post="List-Unsubscribe=One-Click",
                     auth_results="mx; dmarc=pass header.from=brand.it", dkim_domains=None,
                     hdr_version=2)
    un.classify(c)
    assert not c.safe
    assert any("storico" in r for r in c.reasons) and any("risposto" in r for r in c.reasons)


def test_classify_rejects_non_public_url():
    c = un.Candidate(sender="a@brand.it", domain="brand.it", name=None, n=10, months_active=5,
                     interactions=0, folder="INBOX", uidvalidity=1, uid=1,
                     list_unsubscribe="<https://127.0.0.1/u>",
                     list_unsubscribe_post="List-Unsubscribe=One-Click",
                     auth_results="mx; dmarc=pass", dkim_domains=None, hdr_version=2)
    assert not un.classify(c).safe


def test_one_click_posts_rfc8058_body():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            seen["ctype"] = self.headers["Content-Type"]
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    ok, detail = un.do_one_click(f"http://127.0.0.1:{srv.server_port}/u")
    assert ok, detail
    assert seen == {"body": b"List-Unsubscribe=One-Click",
                    "ctype": "application/x-www-form-urlencoded"}


def test_mailto_and_execute(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append((msg["To"], msg["Subject"]))

        def quit(self):
            pass

    monkeypatch.setattr(un.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(un, "do_one_click", lambda url: (True, "HTTP 200"))

    class Acc:
        user, smtp_host, smtp_port = "me@libero.it", "smtp.libero.it", 465

        def password(self):
            return "x"

    conn = db.connect(":memory:")
    generate(conn, months=6)
    senders = {"newsletter@zalando.it", "promo@trenitalia.it", "deals@aliexpress.com"}
    cs = [un.classify(c) for c in un.latest_per_sender(conn, senders)]
    res = un.execute(conn, cs, Acc(), pause=0)
    assert {c.sender for c, ok, _ in res if ok} == {"newsletter@zalando.it", "promo@trenitalia.it"}
    assert sent == [("unsub@trenitalia.it", "unsubscribe")]
    # già fatti: alla seconda passata vengono scartati
    done = db.unsubscribed(conn)
    again = [un.classify(c, done=done) for c in un.latest_per_sender(conn, senders)]
    assert not any(c.safe for c in again)


def test_migration_adds_columns(tmp_path):
    import sqlite3

    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE messages (folder TEXT, uidvalidity INT, uid INT, message_id TEXT,"
                    " from_addr TEXT, from_name TEXT, from_domain TEXT, to_addr TEXT, subject TEXT,"
                    " date TEXT, size INT, seen INT, answered INT, flagged INT, list_unsubscribe TEXT,"
                    " list_id TEXT, precedence TEXT, multipart_mixed INT,"
                    " PRIMARY KEY (folder, uidvalidity, uid));"
                    "INSERT INTO messages (folder, uidvalidity, uid) VALUES ('INBOX', 1, 1);")
    c.commit()
    c.close()
    conn = db.connect(p)
    row = conn.execute("SELECT hdr_version, auth_results FROM messages").fetchone()
    assert tuple(row) == (1, None)


def test_force_overrides_soft_checks_only():
    conn = db.connect(":memory:")
    generate(conn, months=6)
    from mail_radar import ops

    everyone = ops.select_senders(conn, everyone=True)
    assert "deals@aliexpress.com" in everyone and "mario.rossi@gmail.com" not in everyone
    cs = {c.sender: c for c in ops.unsub_plan(conn, None, everyone, force=True)}
    ali = cs["deals@aliexpress.com"]
    assert ali.safe and any("autenticità" in w for w in ali.warnings)
    assert not cs["info@ikea.it"].safe  # solo link web: niente da automatizzare
    assert not cs["newsletter@zalando.it"].warnings


def test_force_still_blocks_private_urls():
    c = un.Candidate(sender="a@x.it", domain="x.it", name=None, n=1, months_active=1,
                     interactions=1, folder="INBOX", uidvalidity=1, uid=1,
                     list_unsubscribe="<https://192.168.1.1/u>",
                     list_unsubscribe_post="List-Unsubscribe=One-Click",
                     auth_results=None, dkim_domains=None, hdr_version=2)
    un.classify(c, force=True)
    assert not c.safe and c.reasons == ["URL non https/pubblico"]
