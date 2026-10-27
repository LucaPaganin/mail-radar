"""Configurazione: file TOML + variabili d'ambiente. La password non sta mai nel file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PRESETS: dict[str, tuple[str, int]] = {
    "libero": ("imapmail.libero.it", 993),
    "gmail": ("imap.gmail.com", 993),
    "outlook": ("outlook.office365.com", 993),
    "yahoo": ("imap.mail.yahoo.com", 993),
    "icloud": ("imap.mail.me.com", 993),
}
# SMTP usato solo per gli unsubscribe via mailto. Porta 465 = SSL, altrimenti STARTTLS.
SMTP_PRESETS: dict[str, tuple[str, int]] = {
    "imapmail.libero.it": ("smtp.libero.it", 465),
    "imap.gmail.com": ("smtp.gmail.com", 465),
    "outlook.office365.com": ("smtp.office365.com", 587),
    "imap.mail.yahoo.com": ("smtp.mail.yahoo.com", 465),
    "imap.mail.me.com": ("smtp.mail.me.com", 587),
}

DEFAULT_CONFIG_PATHS = [
    Path("mail-radar.toml"),
    Path.home() / ".config" / "mail-radar" / "config.toml",
]
DEFAULT_DB = Path.home() / ".local" / "share" / "mail-radar" / "mail-radar.db"


@dataclass
class Account:
    user: str
    host: str
    port: int = 993
    ssl: bool = True
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    trash_folder: str | None = None  # None = autodetect (\Trash special-use)
    smtp_host: str | None = None
    smtp_port: int = 465
    _pw: str | None = field(default=None, repr=False)

    def password(self) -> str:
        if self._pw is None:
            self._pw = self._read_password()
        return self._pw

    def _read_password(self) -> str:
        pw = os.environ.get("MAIL_RADAR_PASSWORD")
        if pw:
            return pw
        try:
            import keyring  # type: ignore

            pw = keyring.get_password("mail-radar", self.user)
        except Exception:
            pw = None
        if pw:
            return pw
        import getpass

        return getpass.getpass(f"Password IMAP per {self.user}: ")


@dataclass
class Config:
    account: Account | None
    db_path: Path


def load_config(path: Path | None = None) -> Config:
    data: dict = {}
    candidates = [path] if path else DEFAULT_CONFIG_PATHS
    for p in candidates:
        if p and p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            break

    acc = data.get("account", {})
    user = os.environ.get("MAIL_RADAR_USER", acc.get("user"))
    provider = os.environ.get("MAIL_RADAR_PROVIDER", acc.get("provider"))
    host = os.environ.get("MAIL_RADAR_HOST", acc.get("host"))
    port = int(os.environ.get("MAIL_RADAR_PORT", acc.get("port", 0)) or 0)
    if not host and provider:
        host, default_port = PRESETS[provider.lower()]
        port = port or default_port
    if not host and user and "@" in user:
        domain = user.split("@", 1)[1].lower()
        for name, (h, p) in PRESETS.items():
            if name in domain:
                host, port = h, port or p
                break

    account = None
    if user and host:
        account = Account(
            user=user,
            host=host,
            port=port or 993,
            ssl=acc.get("ssl", True),
            folders=acc.get("folders", ["INBOX"]),
            trash_folder=acc.get("trash_folder"),
        )
        smtp_default = SMTP_PRESETS.get(host, (None, 465))
        account.smtp_host = acc.get("smtp_host", smtp_default[0])
        account.smtp_port = int(acc.get("smtp_port", smtp_default[1]))

    db_path = Path(
        os.environ.get("MAIL_RADAR_DB", data.get("db_path", str(DEFAULT_DB)))
    ).expanduser()
    return Config(account=account, db_path=db_path)
