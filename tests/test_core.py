from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from mail_radar import analysis, db, rules
from mail_radar.demo import generate
from mail_radar.headers import parse_header_block
from mail_radar.imap import move_uids, scan_folder
from mail_radar.web import create_app

RAW = (
    b"From: =?UTF-8?B?WmFsYW5kbyDinIg=?= <Newsletter@Zalando.IT>\r\n"
    b"To: tu@libero.it\r\n"
    b"Subject: =?UTF-8?Q?Saldi_fino_al_-50=25?=\r\n"
    b"Date: Tue, 01 Oct 2024 10:00:00 +0200\r\n"
    b"List-Unsubscribe: <https://zalando.it/u>\r\n"
    b"Content-Type: multipart/alternative; boundary=x\r\n\r\n"
)


def test_parse_header_block():
    r = parse_header_block(RAW)
    assert r["from_addr"] == "newsletter@zalando.it"
    assert r["from_domain"] == "zalando.it"
    assert r["subject"] == "Saldi fino al -50%"
    assert r["date"] == "2024-10-01T08:00:00+00:00"
    assert r["list_unsubscribe"]
    assert r["multipart_mixed"] == 0


def test_parse_garbage_does_not_crash():
    r = parse_header_block(b"\xff\xfe garbage", None)
    assert r["from_addr"] is None


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    generate(c, months=6)
    return c


def test_scores_separate_newsletters_from_people(conn):
    s = {x["sender"]: x for x in analysis.senders(conn)}
    assert s["news@libero.it"]["label"] == "junk"
    assert s["mario.rossi@gmail.com"]["label"] == "keep"
    assert s["noreply@fineco.it"]["label"] == "keep"


def test_rules_and_protections(conn):
    conn.execute("UPDATE messages SET flagged=1 WHERE uid = (SELECT MIN(uid) FROM messages "
                 "WHERE from_addr='news@libero.it')")
    rs = rules.parse_rules("""
rules:
  - name: libero
    match: {from_domain: [libero.it], has_unsubscribe: true}
  - name: amazon-promo
    match: {from: ["offerte@*"], older_than_days: 0}
    action: "move:Promo"
""")
    m = rules.evaluate(conn, rs, now=datetime.now(UTC))
    n_libero = conn.execute("SELECT COUNT(*) FROM messages WHERE from_addr='news@libero.it'").fetchone()[0]
    assert sum(x["rule"] == "libero" for x in m) == n_libero - 1  # quella con stella è protetta
    assert all(x["from_addr"] == "offerte@amazon.it" for x in m if x["rule"] == "amazon-promo")
    assert not any(x["from_addr"] == "auto-confirm@amazon.it" for x in m)


def test_rule_validation():
    with pytest.raises(ValueError):
        rules.parse_rules("rules: [{name: x, match: {}}]")
    with pytest.raises(ValueError):
        rules.parse_rules("rules: [{name: x, match: {sender: a}}]")
    with pytest.raises(ValueError):
        rules.parse_rules("rules: [{name: x, match: {from: a}, action: delete}]")


def test_suggest_roundtrip(conn):
    text = rules.suggest(conn)
    rs = rules.parse_rules(text)
    assert rs.rules and all(r.action == "trash" for r in rs.rules)


class FakeIMAP:
    def __init__(self, uidvalidity=7):
        self.uidvalidity = uidvalidity
        self.msgs = {
            1: (RAW, [b"\\Seen"]),
            2: (RAW.replace(b"Newsletter@Zalando.IT", b"amico@gmail.com"), [b"\\Answered", b"\\Seen"]),
        }
        self.moved = []

    def select_folder(self, folder, readonly=False):
        return {b"UIDVALIDITY": self.uidvalidity}

    def search(self, crit):
        return list(self.msgs)

    def fetch(self, uids, items):
        out = {}
        for u in uids:
            raw, flags = self.msgs[u]
            out[u] = {b"FLAGS": tuple(flags), b"RFC822.SIZE": 1234, b"INTERNALDATE": None,
                      b"BODY[HEADER.FIELDS (FROM TO)]": raw}
        return out

    def capabilities(self):
        return (b"IMAP4REV1", b"MOVE")

    def move(self, uids, dest):
        self.moved.append((list(uids), dest))


def test_scan_incremental_and_resync():
    c = db.connect(":memory:")
    imap = FakeIMAP()
    assert scan_folder(imap, c, "INBOX")["new"] == 2
    assert scan_folder(imap, c, "INBOX")["new"] == 0
    imap.msgs[3] = (RAW, [])
    del imap.msgs[1]
    res = scan_folder(imap, c, "INBOX")
    assert (res["new"], res["removed"]) == (1, 1)
    rows = {r["uid"]: dict(r) for r in c.execute("SELECT * FROM messages")}
    assert set(rows) == {2, 3}
    assert rows[2]["answered"] == 1 and rows[3]["seen"] == 0
    imap.uidvalidity = 8  # cartella ricreata: si riparte da zero
    assert scan_folder(imap, c, "INBOX")["new"] == 2


def test_move_uses_move_capability():
    imap = FakeIMAP()
    assert move_uids(imap, "INBOX", [1], "Trash") == "move"
    assert imap.moved == [([1], "Trash")]


def test_web_api(tmp_path):
    p = tmp_path / "x.db"
    generate(db.connect(p), months=3)
    client = TestClient(create_app(p))
    assert "mail" in client.get("/").text
    assert client.get("/api/overview").json()["n"] > 0
    assert client.get("/api/senders?by=domain").status_code == 200
    r = client.post("/api/rules/preview", content="rules: [{name: a, match: {from_domain: libero.it}}]",
                    headers={"Content-Type": "text/plain"})
    assert r.json()["total"] > 0
    assert client.post("/api/rules/preview", content="rules: [{match: {}}]",
                       headers={"Content-Type": "text/plain"}).status_code == 400
