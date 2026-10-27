"""Operazioni condivise da CLI e GUI: scan, applicazione regole, disiscrizioni.

Ogni funzione riceve `log(str)` e, dove ha senso, `progress(label, done, total)` così che la CLI
possa stampare su terminale e la GUI aggiornare un job in background.
"""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from collections.abc import Callable

from . import db
from . import rules as rules_mod
from . import unsubscribe as unsub
from .headers import HDR_VERSION

Log = Callable[[str], None]
Progress = Callable[[str, int, int], None]


def _noop(*_a, **_k):
    return None


def scan(account, conn, folders: list[str] | None = None, all_folders: bool = False,
         full: bool = False, log: Log = _noop, progress: Progress = _noop) -> list[dict]:
    from .imap import list_folders, open_client, scan_folder

    out = []
    with open_client(account) as c:
        targets = list_folders(c) if all_folders else (folders or account.folders)
        for f in targets:
            try:
                res = scan_folder(c, conn, f, full=full,
                                  progress=lambda d, t, f=f: progress(f"Scan {f}", d, t))
            except Exception as e:  # noqa: BLE001 - cartelle non selezionabili ecc.
                log(f"{f}: saltata ({e})")
                continue
            log(f"{f}: {res['new']} nuove, {res['removed']} rimosse, {res['total']} totali")
            out.append(res)
    return out


def move_messages(client, conn, ms: list[dict], dest_for: Callable[[dict], str],
                  log: Log = _noop) -> int:
    """Sposta i messaggi (dict con folder, uid, from_addr, subject, rule, action)."""
    from .imap import move_uids

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for m in ms:
        groups[(m["folder"], dest_for(m))].append(m)
    moved = 0
    for (folder, dest), gms in groups.items():
        if dest == folder:
            continue
        uids = [m["uid"] for m in gms]
        how = ""
        for i in range(0, len(uids), 500):
            how = move_uids(client, folder, uids[i:i + 500], dest)
        db.log_actions(conn, [{"rule": m.get("rule"), "action": f"{m.get('action', 'trash')}->{dest}",
                               "folder": folder, "uid": m["uid"], "from_addr": m["from_addr"],
                               "subject": m["subject"]} for m in gms])
        db.delete_uids(conn, folder, uids)
        moved += len(uids)
        log(f"{folder} → {dest}: {len(uids)} mail ({how})")
        if how == "copy+flag_deleted":
            log("Il server non supporta MOVE/UIDPLUS: originali marcati \\Deleted, non espunti.")
    return moved


def apply_matches(account, conn, matches: list[dict], log: Log = _noop) -> int:
    from .imap import find_trash, open_client

    with open_client(account) as c:
        trash = find_trash(c, account.trash_folder)
        return move_messages(
            c, conn, matches,
            lambda m: trash if m["action"] == "trash" else m["action"].split(":", 1)[1], log)


def select_senders(conn, senders=None, domains=None, ruleset=None, rule_globs=None,
                   everyone: bool = False) -> set[str]:
    out: set[str] = {s.lower() for s in senders or []}
    if everyone:  # tutti i mittenti che hanno mai inviato un List-Unsubscribe
        out |= {r[0] for r in conn.execute(
            "SELECT DISTINCT from_addr FROM messages WHERE list_unsubscribe IS NOT NULL")}
    for d in domains or []:
        d = d.lower()
        out |= {r[0] for r in conn.execute(
            "SELECT DISTINCT from_addr FROM messages WHERE from_domain = ? OR from_domain LIKE ?",
            (d, f"%.{d}"))}
    if ruleset is not None:
        globs = [g.lower() for g in (rule_globs or ["*"])]
        for m in rules_mod.evaluate(conn, ruleset):
            if any(fnmatch.fnmatch(m["rule"].lower(), g) for g in globs):
                out.add(m["from_addr"])
    out.discard(None)
    return out


def unsub_plan(conn, account, selected: set[str], *, refresh: bool = True, min_messages: int = 3,
               min_months: int = 2, trust_dkim_signature: bool = False, force: bool = False,
               log: Log = _noop) -> list[unsub.Candidate]:
    cands = unsub.latest_per_sender(conn, selected)
    stale = [c for c in cands if c.hdr_version < HDR_VERSION and c.list_unsubscribe]
    if stale and refresh and account is not None:
        from .imap import open_client, refresh_headers

        log(f"Riscarico gli header estesi di {len(stale)} mail…")
        with open_client(account) as c:
            refresh_headers(c, conn, [vars(x) for x in stale])
        cands = unsub.latest_per_sender(conn, selected)
    done = db.unsubscribed(conn)
    for c in cands:
        unsub.classify(c, min_messages=min_messages, min_months=min_months,
                       trust_dkim_signature=trust_dkim_signature, done=done, force=force)
    return cands


def unsub_execute(conn, account, cands: list[unsub.Candidate], trash: bool = False,
                  log: Log = _noop, progress: Progress = _noop) -> dict:
    safe = [c for c in cands if c.safe]
    count = {"i": 0}

    def report(c, ok, detail):
        count["i"] += 1
        log(f"{'✓' if ok else '✗'} {c.sender}: {detail}")
        progress("Disiscrizioni", count["i"], len(safe))

    results = unsub.execute(conn, safe, account, progress=report)
    ok_senders = [c.sender for c, ok, _ in results if ok]
    moved = 0
    if trash and ok_senders:
        from .imap import find_trash, open_client

        ph = ",".join("?" * len(ok_senders))
        ms = [dict(r) | {"rule": "unsubscribe", "action": "trash"} for r in conn.execute(
            f"SELECT folder, uid, from_addr, subject FROM messages WHERE from_addr IN ({ph}) "
            "AND flagged = 0 AND answered = 0", ok_senders)]
        with open_client(account) as c:
            trash_folder = find_trash(c, account.trash_folder)
            moved = move_messages(c, conn, ms, lambda m: trash_folder, log)
    return {"ok": len(ok_senders), "failed": len(results) - len(ok_senders), "moved": moved}
