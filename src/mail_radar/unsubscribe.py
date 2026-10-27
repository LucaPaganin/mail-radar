"""Disiscrizione in blocco, solo per mittenti che passano TUTTI i controlli di sicurezza.

Perché così restrittivo: disiscriversi da spam vero conferma che l'indirizzo è attivo, e un
link di "unsubscribe" in una mail truffaldina può essere qualunque cosa. Si procede quindi solo se:

1. il mittente è stato selezionato esplicitamente (regole, indirizzi o domini indicati da te);
2. l'ultima mail ha un metodo standard: one-click RFC 8058 (POST https) oppure mailto;
3. il server di ricezione attesta l'autenticità: DMARC pass, oppure DKIM pass con dominio
   allineato al From (dall'header Authentication-Results più in alto, l'unico non falsificabile);
4. c'è uno storico: abbastanza mail e mesi di invii (i circuiti di spam ruotano domini);
5. non hai mai risposto o messo la stella a quel mittente (sarebbe un mittente che ti interessa);
6. l'URL è https verso un host pubblico.

I link senza one-click (pagine con form o conferme) non vengono mai aperti in automatico: li
elenca, da aprire a mano.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import parse_qs, unquote, urlsplit

from . import db

# suffissi a due livelli più comuni; il resto usa le ultime due etichette
_MULTI_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "co.jp", "ne.jp", "or.jp",
    "com.br", "com.ar", "com.mx", "co.nz", "co.za", "com.tr", "com.cn", "com.hk", "co.in",
    "co.kr", "com.sg", "gov.it", "edu.it",
}

USER_AGENT = "mail-radar (+https://github.com/LucaPaganin/mail-radar)"


def org_domain(domain: str | None) -> str:
    if not domain:
        return ""
    labels = domain.lower().strip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def parse_list_unsubscribe(value: str | None) -> list[str]:
    if not value:
        return []
    uris = re.findall(r"<\s*([^>]+?)\s*>", value)
    return [u for u in uris if u.lower().startswith(("http://", "https://", "mailto:"))]


def _ar_methods(auth_results: str) -> list[tuple[str, str, dict[str, str]]]:
    """'libero.it; dkim=pass header.d=x.it; dmarc=pass header.from=x.it' -> [(method, result, props)]"""
    out = []
    parts = auth_results.split(";")[1:]  # il primo pezzo è l'authserv-id
    for p in parts:
        m = re.match(r"\s*([a-z0-9_-]+)\s*=\s*([a-z]+)(.*)$", p.strip(), re.I | re.S)
        if not m:
            continue
        props = {k.lower(): v.strip('"').lower() for k, v in re.findall(r"([\w.]+)=(\S+)", m.group(3))}
        out.append((m.group(1).lower(), m.group(2).lower(), props))
    return out


def auth_verdict(auth_results: str | None, from_domain: str | None,
                 dkim_domains: str | None = None, trust_dkim_signature: bool = False) -> str | None:
    """Ritorna una stringa che descrive l'autenticazione allineata, o None."""
    org = org_domain(from_domain)
    if auth_results:
        methods = _ar_methods(auth_results)
        for meth, res, props in methods:
            if meth == "dmarc" and res == "pass":
                hf = props.get("header.from")
                if not hf or org_domain(hf) == org:
                    return "dmarc=pass"
        for meth, res, props in methods:
            if meth == "dkim" and res == "pass":
                d = props.get("header.d") or (props.get("header.i") or "").lstrip("@").split("@")[-1]
                if d and org_domain(d) == org:
                    return f"dkim=pass ({d})"
    if trust_dkim_signature and dkim_domains:
        for d in dkim_domains.split(","):
            if org_domain(d) == org:
                return f"firma DKIM {d} (non verificata)"
    return None


def _public_https(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        return "." in host


@dataclass
class Candidate:
    sender: str
    domain: str
    name: str | None
    n: int
    months_active: int
    interactions: int
    folder: str
    uidvalidity: int
    uid: int
    list_unsubscribe: str | None
    list_unsubscribe_post: str | None
    auth_results: str | None
    dkim_domains: str | None
    hdr_version: int
    method: str = "none"          # one-click | mailto | manual | none
    target: str | None = None
    auth: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.reasons and self.method in ("one-click", "mailto")


def latest_per_sender(conn, senders: set[str]) -> list[Candidate]:
    """Ultima mail con List-Unsubscribe di ciascun mittente, più le statistiche aggregate."""
    out = []
    for s in sorted(senders):
        stats = conn.execute(
            """SELECT COUNT(*) n, COUNT(DISTINCT substr(date,1,7)) months,
                      SUM(answered) + SUM(flagged) inter, MAX(from_name) name, MAX(from_domain) dom
               FROM messages WHERE from_addr = ?""", (s,)).fetchone()
        last = conn.execute(
            """SELECT * FROM messages WHERE from_addr = ?
               ORDER BY list_unsubscribe IS NULL, date DESC LIMIT 1""", (s,)).fetchone()
        if not last:
            continue
        out.append(Candidate(
            sender=s, domain=stats["dom"] or "", name=stats["name"], n=stats["n"],
            months_active=stats["months"], interactions=stats["inter"] or 0,
            folder=last["folder"], uidvalidity=last["uidvalidity"], uid=last["uid"],
            list_unsubscribe=last["list_unsubscribe"],
            list_unsubscribe_post=last["list_unsubscribe_post"],
            auth_results=last["auth_results"], dkim_domains=last["dkim_domains"],
            hdr_version=last["hdr_version"],
        ))
    return out


def classify(c: Candidate, *, min_messages: int = 3, min_months: int = 2,
             trust_dkim_signature: bool = False, done: dict | None = None) -> Candidate:
    c.reasons = []
    uris = parse_list_unsubscribe(c.list_unsubscribe)
    https = [u for u in uris if u.lower().startswith("https://")]
    mailto = [u for u in uris if u.lower().startswith("mailto:")]
    one_click = "list-unsubscribe=one-click" in (c.list_unsubscribe_post or "").lower().replace(" ", "")
    if https and one_click:
        c.method, c.target = "one-click", https[0]
    elif mailto:
        c.method, c.target = "mailto", mailto[0]
    elif uris:
        c.method, c.target = "manual", uris[0]
    else:
        c.method, c.target = "none", None

    if done and c.sender in done:
        c.reasons.append(f"già disiscritto il {done[c.sender]['ts'][:10]}")
    if c.method == "none":
        c.reasons.append("nessun List-Unsubscribe")
    elif c.method == "manual":
        c.reasons.append("solo link a pagina web (da aprire a mano)")
    if c.method == "one-click" and not _public_https(c.target or ""):
        c.reasons.append("URL non https/pubblico")
    c.auth = auth_verdict(c.auth_results, c.domain, c.dkim_domains, trust_dkim_signature)
    if not c.auth:
        c.reasons.append("autenticità non verificata (né DMARC né DKIM allineato)")
    if c.n < min_messages or c.months_active < min_months:
        c.reasons.append(f"storico breve ({c.n} mail, {c.months_active} mesi)")
    if c.interactions:
        c.reasons.append("hai risposto o messo la stella a questo mittente")
    return c


def parse_mailto(uri: str) -> tuple[str, str, str]:
    u = urlsplit(uri)
    q = parse_qs(u.query)
    to = unquote(u.path)
    subject = q.get("subject", ["unsubscribe"])[0] or "unsubscribe"
    body = q.get("body", ["unsubscribe"])[0] or "unsubscribe"
    return to, subject, body


def do_one_click(url: str, timeout: float = 20) -> tuple[bool, str]:
    req = urllib.request.Request(
        url, data=b"List-Unsubscribe=One-Click", method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (solo https, verificato)
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"errore di rete: {e}"


class MailtoSender:
    """Connessione SMTP riusata per tutti i mailto della sessione."""

    def __init__(self, account):
        self.account = account
        self.smtp: smtplib.SMTP | None = None

    def _connect(self):
        a = self.account
        if not a.smtp_host:
            raise RuntimeError("smtp_host non configurato")
        ctx = ssl.create_default_context()
        if a.smtp_port == 465:
            self.smtp = smtplib.SMTP_SSL(a.smtp_host, a.smtp_port, context=ctx, timeout=30)
        else:
            self.smtp = smtplib.SMTP(a.smtp_host, a.smtp_port, timeout=30)
            self.smtp.starttls(context=ctx)
        self.smtp.login(a.user, a.password())

    def send(self, uri: str) -> tuple[bool, str]:
        to, subject, body = parse_mailto(uri)
        if "@" not in to:
            return False, "mailto senza destinatario"
        msg = EmailMessage()
        msg["From"] = self.account.user
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            if self.smtp is None:
                self._connect()
            self.smtp.send_message(msg)
            return True, f"mail inviata a {to}"
        except (smtplib.SMTPException, OSError, RuntimeError) as e:
            return False, f"SMTP: {e}"

    def close(self):
        if self.smtp is not None:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                self.smtp.quit()


def execute(conn, candidates: list[Candidate], account, progress=None,
            pause: float = 1.0) -> list[tuple[Candidate, bool, str]]:
    results = []
    mailer = MailtoSender(account) if any(c.method == "mailto" for c in candidates) else None
    try:
        for c in candidates:
            if not c.safe:
                continue
            if c.method == "one-click":
                ok, detail = do_one_click(c.target)
            else:
                ok, detail = mailer.send(c.target)
            db.log_unsubscribe(conn, c.sender, c.method, c.target, "ok" if ok else "error", detail)
            results.append((c, ok, detail))
            if progress:
                progress(c, ok, detail)
            time.sleep(pause)
    finally:
        if mailer:
            mailer.close()
    return results
