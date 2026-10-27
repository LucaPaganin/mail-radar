"""Dati sintetici per provare CLI e dashboard senza collegarsi a una casella."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from . import db

SENDERS = [
    # (addr, name, bulk, p_seen, p_answered, rate_per_month, subjects)
    ("newsletter@zalando.it", "Zalando", True, 0.05, 0, 14, ["Nuovi arrivi per te", "Fino al -50%", "Solo per oggi"]),
    ("offerte@amazon.it", "Amazon.it", True, 0.15, 0, 20, ["Offerte del giorno", "Consigliati per te"]),
    ("auto-confirm@amazon.it", "Amazon.it", False, 0.9, 0, 3, ["Il tuo ordine è stato spedito", "Conferma ordine"]),
    ("noreply@linkedin.com", "LinkedIn", True, 0.1, 0, 25, ["Hai 3 nuove notifiche", "Persone che potresti conoscere"]),
    ("news@libero.it", "Libero", True, 0.02, 0, 30, ["Le notizie di oggi", "Oroscopo"]),
    ("promo@trenitalia.it", "Trenitalia", True, 0.2, 0, 6, ["Viaggia con lo sconto", "Offerta weekend"]),
    ("biglietti@trenitalia.it", "Trenitalia", False, 1.0, 0, 2, ["Il tuo biglietto"]),
    ("noreply@fineco.it", "Fineco", False, 0.95, 0, 4, ["Estratto conto disponibile", "Avviso movimento"]),
    ("info@ikea.it", "IKEA", True, 0.1, 0, 8, ["Idee per la casa", "IKEA Family"]),
    ("mario.rossi@gmail.com", "Mario Rossi", False, 1.0, 0.6, 2, ["Re: cena sabato", "foto"]),
    ("amministratore@studiocond.it", "Studio Condominio", False, 1.0, 0.4, 1, ["Convocazione assemblea", "Rendiconto"]),
    ("deals@aliexpress.com", "AliExpress", True, 0.01, 0, 22, ["Super deals", "Il tuo coupon scade"]),
    ("no-reply@spotify.com", "Spotify", True, 0.3, 0, 4, ["Nuove uscite", "Il tuo Wrapped"]),
]


def _unsub_headers(addr: str, bulk: bool) -> dict:
    dom = addr.split("@")[1]
    auth = f"mx.libero.it; dkim=pass header.d={dom}; spf=pass; dmarc=pass header.from={dom}"
    out = {"hdr_version": 2, "auth_results": auth, "dkim_domains": dom}
    if not bulk:
        return out
    if "aliexpress" in dom:  # nessuna autenticazione allineata
        out["auth_results"] = "mx.libero.it; dkim=pass header.d=bulkmailer.example; dmarc=fail"
    if "ikea" in dom:  # solo link a pagina web
        return out
    if "trenitalia" in dom:  # solo mailto
        out["list_unsubscribe"] = f"<mailto:unsub@{dom}?subject=unsubscribe>"
        return out
    out["list_unsubscribe"] = f"<https://{dom}/unsub?t=demo>, <mailto:unsub@{dom}>"
    out["list_unsubscribe_post"] = "List-Unsubscribe=One-Click"
    return out


def generate(conn, months: int = 18, seed: int = 42) -> int:
    rnd = random.Random(seed)
    now = datetime.now(UTC)
    rows, uid = [], 0
    for addr, name, bulk, p_seen, p_ans, rate, subjects in SENDERS:
        n = max(1, int(rnd.gauss(rate * months, rate * months * 0.15)))
        for _ in range(n):
            uid += 1
            d = now - timedelta(days=rnd.random() * months * 30.4, hours=rnd.random() * 24)
            seen = rnd.random() < p_seen
            rows.append({
                "folder": "INBOX", "uidvalidity": 1, "uid": uid,
                "message_id": f"<demo-{uid}@mail-radar>",
                "from_addr": addr, "from_name": name, "from_domain": addr.split("@")[1],
                "to_addr": "tu@libero.it", "subject": rnd.choice(subjects),
                "date": d.isoformat(timespec="seconds"),
                "size": int(rnd.lognormvariate(10.5 if bulk else 9.5, 0.6)),
                "seen": int(seen), "answered": int(seen and rnd.random() < p_ans),
                "flagged": int(not bulk and rnd.random() < 0.05),
                "list_unsubscribe": f"<https://{addr.split('@')[1]}/unsub>" if bulk else None,
                "list_id": None, "precedence": "bulk" if bulk else None,
                "multipart_mixed": int(not bulk and rnd.random() < 0.3),
                # varietà per la demo di `unsubscribe`: one-click, mailto, solo link, auth assente
                **_unsub_headers(addr, bulk),
            })
    db.upsert_messages(conn, rows)
    db.set_folder_state(conn, "INBOX", 1, uid)
    return len(rows)
