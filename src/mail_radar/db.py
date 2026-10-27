"""Cache locale SQLite degli header."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    folder          TEXT NOT NULL,
    uidvalidity     INTEGER NOT NULL,
    uid             INTEGER NOT NULL,
    message_id      TEXT,
    from_addr       TEXT,
    from_name       TEXT,
    from_domain     TEXT,
    to_addr         TEXT,
    subject         TEXT,
    date            TEXT,          -- ISO 8601 UTC
    size            INTEGER,
    seen            INTEGER NOT NULL DEFAULT 0,
    answered        INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    list_unsubscribe TEXT,
    list_id         TEXT,
    precedence      TEXT,
    multipart_mixed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folder, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS ix_messages_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS ix_messages_domain ON messages(from_domain);
CREATE INDEX IF NOT EXISTS ix_messages_date ON messages(date);

CREATE TABLE IF NOT EXISTS folders (
    name        TEXT PRIMARY KEY,
    uidvalidity INTEGER NOT NULL,
    last_uid    INTEGER NOT NULL DEFAULT 0,
    scanned_at  TEXT
);

CREATE TABLE IF NOT EXISTS actions_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    rule      TEXT,
    action    TEXT,
    folder    TEXT,
    uid       INTEGER,
    from_addr TEXT,
    subject   TEXT
);

CREATE TABLE IF NOT EXISTS unsubscribes (
    sender  TEXT PRIMARY KEY,
    ts      TEXT NOT NULL DEFAULT (datetime('now')),
    method  TEXT,
    target  TEXT,
    status  TEXT,           -- ok | error
    detail  TEXT
);
"""

# colonne aggiunte dopo la v0.1: migrazione automatica su cache esistenti
MIGRATIONS = {
    "list_unsubscribe_post": "TEXT",
    "auth_results": "TEXT",
    "dkim_domains": "TEXT",
    "hdr_version": "INTEGER NOT NULL DEFAULT 1",
}

MESSAGE_COLUMNS = [
    "folder", "uidvalidity", "uid", "message_id", "from_addr", "from_name", "from_domain",
    "to_addr", "subject", "date", "size", "seen", "answered", "flagged",
    "list_unsubscribe", "list_id", "precedence", "multipart_mixed", *MIGRATIONS,
]
_DEFAULTS = {c: None for c in MESSAGE_COLUMNS} | {
    "seen": 0, "answered": 0, "flagged": 0, "multipart_mixed": 0, "hdr_version": 1,
}


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")  # la GUI legge mentre un job scrive
    conn.executescript(SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    for col, ddl in MIGRATIONS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
    conn.commit()
    return conn


def upsert_messages(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    cols = ", ".join(MESSAGE_COLUMNS)
    ph = ", ".join(f":{c}" for c in MESSAGE_COLUMNS)
    conn.executemany(
        f"INSERT OR REPLACE INTO messages ({cols}) VALUES ({ph})",
        [_DEFAULTS | r for r in rows],
    )
    conn.commit()


def update_headers(conn: sqlite3.Connection, folder: str, uidvalidity: int, uid: int,
                   rec: dict) -> None:
    fields = [k for k in rec if k in MESSAGE_COLUMNS and k not in ("folder", "uidvalidity", "uid")]
    conn.execute(
        f"UPDATE messages SET {', '.join(f'{k}=?' for k in fields)} "
        "WHERE folder=? AND uidvalidity=? AND uid=?",
        [rec[k] for k in fields] + [folder, uidvalidity, uid],
    )


def unsubscribed(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["sender"]: r for r in conn.execute("SELECT * FROM unsubscribes WHERE status='ok'")}


def log_unsubscribe(conn, sender: str, method: str, target: str, status: str, detail: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO unsubscribes(sender, method, target, status, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (sender, method, target, status, detail[:500]),
    )
    conn.commit()


def get_folder_state(conn: sqlite3.Connection, folder: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM folders WHERE name = ?", (folder,)).fetchone()


def set_folder_state(conn, folder: str, uidvalidity: int, last_uid: int) -> None:
    conn.execute(
        """INSERT INTO folders(name, uidvalidity, last_uid, scanned_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(name) DO UPDATE SET uidvalidity=excluded.uidvalidity,
             last_uid=excluded.last_uid, scanned_at=excluded.scanned_at""",
        (folder, uidvalidity, last_uid),
    )
    conn.commit()


def purge_folder(conn, folder: str) -> None:
    conn.execute("DELETE FROM messages WHERE folder = ?", (folder,))
    conn.execute("DELETE FROM folders WHERE name = ?", (folder,))
    conn.commit()


def remove_missing(conn, folder: str, uidvalidity: int, server_uids: set[int]) -> int:
    local = {
        r[0]
        for r in conn.execute(
            "SELECT uid FROM messages WHERE folder = ? AND uidvalidity = ?", (folder, uidvalidity)
        )
    }
    gone = local - server_uids
    if gone:
        conn.executemany(
            "DELETE FROM messages WHERE folder = ? AND uidvalidity = ? AND uid = ?",
            [(folder, uidvalidity, u) for u in gone],
        )
        conn.commit()
    return len(gone)


def delete_uids(conn, folder: str, uids: list[int]) -> None:
    conn.executemany(
        "DELETE FROM messages WHERE folder = ? AND uid = ?", [(folder, u) for u in uids]
    )
    conn.commit()


def log_actions(conn, entries: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO actions_log(rule, action, folder, uid, from_addr, subject) "
        "VALUES (:rule, :action, :folder, :uid, :from_addr, :subject)",
        entries,
    )
    conn.commit()
