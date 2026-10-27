"""Accesso IMAP: scan incrementale degli header e azioni (sposta nel cestino)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager, suppress

from imapclient import IMAPClient
from imapclient.imapclient import TRASH

from . import db
from .config import Account
from .headers import HEADER_FIELDS, parse_header_block

BATCH = 200
TRASH_FALLBACKS = ["Trash", "Cestino", "INBOX.Trash", "INBOX.Cestino", "Deleted Items", "Posta eliminata"]


@contextmanager
def open_client(account: Account, password: str | None = None):
    client = IMAPClient(account.host, port=account.port, ssl=account.ssl, timeout=60)
    try:
        client.login(account.user, password or account.password())
        yield client
    finally:
        with suppress(Exception):
            client.logout()


def list_folders(client) -> list[str]:
    return [name for _flags, _delim, name in client.list_folders()]


def _chunks(seq: list[int], n: int) -> Iterable[list[int]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _header_bytes(data: dict) -> bytes:
    for k, v in data.items():
        if isinstance(k, bytes) and k.startswith(b"BODY[HEADER"):
            return v or b""
    return b""


def scan_folder(
    client,
    conn,
    folder: str,
    progress: Callable[[int, int], None] | None = None,
    full: bool = False,
) -> dict:
    """Scarica gli header nuovi della cartella. Ritorna statistiche dello scan."""
    info = client.select_folder(folder, readonly=True)
    uidvalidity = int(info[b"UIDVALIDITY"])
    state = db.get_folder_state(conn, folder)
    if full or (state and state["uidvalidity"] != uidvalidity):
        db.purge_folder(conn, folder)
        state = None
    last_uid = state["last_uid"] if state else 0

    server_uids = set(client.search(["ALL"]))
    removed = db.remove_missing(conn, folder, uidvalidity, server_uids) if state else 0
    new_uids = sorted(u for u in server_uids if u > last_uid)

    fetch_items = ["FLAGS", "RFC822.SIZE", "INTERNALDATE", f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]"]
    done = 0
    for chunk in _chunks(new_uids, BATCH):
        resp = client.fetch(chunk, fetch_items)
        rows = []
        for uid, data in resp.items():
            flags = {f.decode() if isinstance(f, bytes) else f for f in data.get(b"FLAGS", ())}
            rec = parse_header_block(_header_bytes(data), data.get(b"INTERNALDATE"))
            rec.update(
                folder=folder,
                uidvalidity=uidvalidity,
                uid=int(uid),
                size=int(data.get(b"RFC822.SIZE", 0) or 0),
                seen=int("\\Seen" in flags),
                answered=int("\\Answered" in flags),
                flagged=int("\\Flagged" in flags),
            )
            rows.append(rec)
        db.upsert_messages(conn, rows)
        done += len(chunk)
        db.set_folder_state(conn, folder, uidvalidity, max(chunk))
        if progress:
            progress(done, len(new_uids))

    # aggiorna i flag dei messaggi già noti (letti/risposti cambiano nel tempo)
    known = sorted(server_uids - set(new_uids))
    for chunk in _chunks(known, 1000):
        resp = client.fetch(chunk, ["FLAGS"])
        conn.executemany(
            "UPDATE messages SET seen=?, answered=?, flagged=? WHERE folder=? AND uidvalidity=? AND uid=?",
            [
                (
                    int(b"\\Seen" in d[b"FLAGS"]),
                    int(b"\\Answered" in d[b"FLAGS"]),
                    int(b"\\Flagged" in d[b"FLAGS"]),
                    folder,
                    uidvalidity,
                    int(u),
                )
                for u, d in resp.items()
            ],
        )
    conn.commit()

    db.set_folder_state(conn, folder, uidvalidity, max(server_uids) if server_uids else last_uid)
    return {"folder": folder, "new": len(new_uids), "removed": removed, "total": len(server_uids)}


def find_trash(client, configured: str | None = None) -> str:
    if configured:
        return configured
    try:
        found = client.find_special_folder(TRASH)
        if found:
            return found
    except Exception:
        pass
    names = {n.lower(): n for n in list_folders(client)}
    for cand in TRASH_FALLBACKS:
        if cand.lower() in names:
            return names[cand.lower()]
    raise RuntimeError("Cartella cestino non trovata: impostala con trash_folder nel config")


def move_uids(client, folder: str, uids: list[int], dest: str) -> str:
    """Sposta i messaggi. Non fa mai cancellazioni permanenti fuori dagli UID indicati."""
    client.select_folder(folder, readonly=False)
    caps = {c.upper() for c in client.capabilities()}
    if b"MOVE" in caps:
        client.move(uids, dest)
        return "move"
    client.copy(uids, dest)
    client.add_flags(uids, [b"\\Deleted"])
    if b"UIDPLUS" in caps:
        client.uid_expunge(uids)
        return "copy+uid_expunge"
    # senza UIDPLUS un EXPUNGE toccherebbe anche altri messaggi marcati \Deleted: lo evitiamo
    return "copy+flag_deleted"
