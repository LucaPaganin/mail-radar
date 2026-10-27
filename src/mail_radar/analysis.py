"""Statistiche e punteggio "spazzatura" per mittente, calcolati solo dagli header."""

from __future__ import annotations

import math
import re
import sqlite3

NOREPLY_RE = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|news|info|marketing|promo|mailer|notifiche|notification)", re.IGNORECASE)

BULK_EXPR = (
    "(list_unsubscribe IS NOT NULL OR list_id IS NOT NULL "
    "OR precedence IN ('bulk','list','junk'))"
)


def junk_score(count: int, bulk_ratio: float, unread_ratio: float, answered: int,
               flagged: int, addr: str) -> float:
    """Punteggio in [0,1]. Combinazione lineare di segnali, smorzata dai segnali di interazione.

    - bulk_ratio: frazione di mail con List-Unsubscribe / List-Id / Precedence: bulk
    - unread_ratio: frazione mai aperte
    - noreply: indirizzo tipico da invio massivo
    - volume: log10(count)/2, saturato a 1 (100 mail -> 1)
    Se hai mai risposto o messo la stella, il punteggio viene abbattuto.
    """
    noreply = 1.0 if NOREPLY_RE.search(addr or "") else 0.0
    volume = min(1.0, math.log10(max(count, 1)) / 2)
    s = 0.40 * bulk_ratio + 0.35 * unread_ratio + 0.15 * noreply + 0.10 * volume
    if answered:
        s *= 0.2
    if flagged:
        s *= 0.3
    return round(s, 3)


def label(score: float) -> str:
    return "junk" if score >= 0.6 else "maybe" if score >= 0.35 else "keep"


def overview(conn: sqlite3.Connection) -> dict:
    r = conn.execute(
        f"""SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes,
                   COALESCE(SUM(1-seen),0) unread, COALESCE(SUM({BULK_EXPR}),0) bulk,
                   COUNT(DISTINCT from_addr) senders, COUNT(DISTINCT from_domain) domains,
                   MIN(date) first, MAX(date) last
            FROM messages"""
    ).fetchone()
    folders = [dict(x) for x in conn.execute(
        "SELECT folder, COUNT(*) n, SUM(size) bytes FROM messages GROUP BY folder ORDER BY n DESC"
    )]
    return {**dict(r), "folders": folders}


def monthly(conn: sqlite3.Connection) -> list[dict]:
    return [dict(x) for x in conn.execute(
        f"""SELECT substr(date,1,7) month, COUNT(*) n,
                   SUM({BULK_EXPR}) bulk, SUM(1-seen) unread
            FROM messages WHERE date IS NOT NULL
            GROUP BY month ORDER BY month"""
    )]


def weekday_hour(conn: sqlite3.Connection) -> list[dict]:
    # strftime su ISO UTC: %w 0=domenica
    return [dict(x) for x in conn.execute(
        """SELECT CAST(strftime('%w', date) AS INT) dow, CAST(strftime('%H', date) AS INT) hour,
                  COUNT(*) n FROM messages WHERE date IS NOT NULL GROUP BY dow, hour"""
    )]


def senders(conn: sqlite3.Connection, by: str = "address", limit: int | None = None,
            min_count: int = 1, sample_subjects: int = 3) -> list[dict]:
    key = "from_addr" if by == "address" else "from_domain"
    rows = conn.execute(
        f"""SELECT {key} AS sender, MAX(from_name) AS name, MAX(from_domain) AS domain,
                   COUNT(*) n, SUM(size) bytes, AVG(1-seen) unread_ratio,
                   AVG({BULK_EXPR}) bulk_ratio, SUM(answered) answered, SUM(flagged) flagged,
                   MIN(date) first, MAX(date) last,
                   COUNT(DISTINCT substr(date,1,7)) months_active
            FROM messages WHERE {key} IS NOT NULL
            GROUP BY {key} HAVING COUNT(*) >= ?""",
        (min_count,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["score"] = junk_score(d["n"], d["bulk_ratio"] or 0, d["unread_ratio"] or 0,
                                d["answered"] or 0, d["flagged"] or 0, d["sender"])
        d["label"] = label(d["score"])
        out.append(d)
    out.sort(key=lambda d: (d["score"] * math.log1p(d["n"])), reverse=True)
    if limit:
        out = out[:limit]
    if sample_subjects:
        for d in out:
            d["subjects"] = [s[0] for s in conn.execute(
                f"""SELECT subject FROM messages WHERE {key} = ? AND subject IS NOT NULL
                    GROUP BY subject ORDER BY MAX(date) DESC LIMIT ?""",
                (d["sender"], sample_subjects),
            )]
    return out


def junk_summary(conn: sqlite3.Connection) -> dict:
    """Quante mail/byte sono attribuibili a mittenti per etichetta."""
    agg = {"junk": [0, 0, 0], "maybe": [0, 0, 0], "keep": [0, 0, 0]}
    for s in senders(conn, sample_subjects=0):
        a = agg[s["label"]]
        a[0] += 1
        a[1] += s["n"]
        a[2] += s["bytes"] or 0
    return {k: {"senders": v[0], "messages": v[1], "bytes": v[2]} for k, v in agg.items()}


def messages(conn: sqlite3.Connection, sender: str | None = None, domain: str | None = None,
             limit: int = 200) -> list[dict]:
    q = "SELECT folder, uid, from_addr, from_name, subject, date, size, seen, answered, flagged, " \
        "list_unsubscribe IS NOT NULL AS has_unsub FROM messages"
    args: list = []
    if sender:
        q += " WHERE from_addr = ?"
        args.append(sender)
    elif domain:
        q += " WHERE from_domain = ?"
        args.append(domain)
    q += " ORDER BY date DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]
