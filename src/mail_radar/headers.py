"""Parsing degli header grezzi in record per il DB."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

HEADER_FIELDS = (
    "FROM TO SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST LIST-ID PRECEDENCE "
    "CONTENT-TYPE AUTHENTICATION-RESULTS DKIM-SIGNATURE"
)
# Versione del set di header salvato: le righe con versione inferiore vanno riscaricate
# se servono i campi nuovi (vedi imap.refresh_headers).
HDR_VERSION = 2

_DKIM_D_RE = re.compile(r"(?:^|;)\s*d=([^;\s]+)", re.I)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _to_utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = ora locale
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_header_block(raw: bytes, internaldate: datetime | None = None) -> dict:
    msg = message_from_bytes(raw or b"")
    name, addr = parseaddr(_decode(msg.get("From")))
    addr = addr.lower().strip()
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    to_addrs = [a.lower() for _, a in getaddresses([_decode(msg.get("To"))]) if a]

    date = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(msg["Date"])
        except Exception:
            date = None
    date = date or internaldate

    ctype = (msg.get("Content-Type") or "").lower()
    precedence = (msg.get("Precedence") or "").strip().lower() or None
    # Il primo Authentication-Results è il più in alto, cioè quello aggiunto dal server di
    # ricezione: quelli più in basso possono essere stati inseriti dal mittente.
    auth = msg.get_all("Authentication-Results") or []
    dkim_domains = []
    for sig in msg.get_all("DKIM-Signature") or []:
        m = _DKIM_D_RE.search(" ".join(sig.split()))
        if m:
            dkim_domains.append(m.group(1).lower().strip())
    return {
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "from_addr": addr or None,
        "from_name": name or None,
        "from_domain": domain or None,
        "to_addr": ", ".join(to_addrs) or None,
        "subject": _decode(msg.get("Subject")) or None,
        "date": _to_utc_iso(date),
        "list_unsubscribe": _decode(msg.get("List-Unsubscribe")) or None,
        "list_id": _decode(msg.get("List-Id")) or None,
        "precedence": precedence,
        "multipart_mixed": int(ctype.startswith("multipart/mixed")),
        "list_unsubscribe_post": (msg.get("List-Unsubscribe-Post") or "").strip() or None,
        "auth_results": " ".join(auth[0].split())[:2000] if auth else None,
        "dkim_domains": ",".join(dict.fromkeys(dkim_domains)) or None,
        "hdr_version": HDR_VERSION,
    }
