"""Regole YAML valutate sulla cache locale.

Formato:

    protect_flagged: true      # default: mai toccare mail con stella
    protect_answered: true     # default: mai toccare mail a cui hai risposto
    rules:
      - name: promo amazon
        match:
          from_domain: [amazon.it]          # match anche sottodomini
          from: ["*@news.*"]                # glob sull'indirizzo
          subject_contains: [offerta, sconto]
          subject_regex: "^\\[promo\\]"
          has_unsubscribe: true
          unread: true
          older_than_days: 30
          folder: [INBOX]
          min_sender_score: 0.6
        action: trash                       # trash | move:<cartella> | keep

Dentro un campo lista vale OR, tra campi diversi vale AND.
Ogni mail viene assegnata alla prima regola che la colpisce: una regola `keep` in cima
funziona da whitelist e sottrae le sue mail alle regole successive.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from . import analysis

KNOWN_KEYS = {
    "from", "from_domain", "subject_contains", "subject_regex", "has_unsubscribe",
    "unread", "older_than_days", "folder", "min_sender_score",
}


@dataclass
class Rule:
    name: str
    match: dict
    action: str = "trash"
    _subject_re: re.Pattern | None = field(default=None, repr=False)

    def __post_init__(self):
        unknown = set(self.match) - KNOWN_KEYS
        if unknown:
            raise ValueError(f"Regola '{self.name}': chiavi sconosciute {sorted(unknown)}")
        if not self.match:
            raise ValueError(f"Regola '{self.name}': 'match' vuoto (colpirebbe tutto)")
        if not (self.action in ("trash", "keep") or self.action.startswith("move:")):
            raise ValueError(f"Regola '{self.name}': azione non valida '{self.action}'")
        if self.match.get("subject_regex"):
            self._subject_re = re.compile(self.match["subject_regex"], re.IGNORECASE)
        for k in ("from", "from_domain", "subject_contains", "folder"):
            v = self.match.get(k)
            if isinstance(v, str):
                self.match[k] = [v]

    def matches(self, m: dict, scores: dict[str, float], now: datetime) -> bool:
        mt = self.match
        addr = (m.get("from_addr") or "").lower()
        dom = (m.get("from_domain") or "").lower()
        subj = m.get("subject") or ""
        if "from" in mt and not any(fnmatch.fnmatch(addr, p.lower()) for p in mt["from"]):
            return False
        if "from_domain" in mt and not any(
            dom == d.lower() or dom.endswith("." + d.lower()) for d in mt["from_domain"]
        ):
            return False
        if "subject_contains" in mt and not any(s.lower() in subj.lower() for s in mt["subject_contains"]):
            return False
        if self._subject_re and not self._subject_re.search(subj):
            return False
        if "has_unsubscribe" in mt and bool(m.get("list_unsubscribe")) != bool(mt["has_unsubscribe"]):
            return False
        if "unread" in mt and (not m.get("seen")) != bool(mt["unread"]):
            return False
        if "folder" in mt and m.get("folder") not in mt["folder"]:
            return False
        if "older_than_days" in mt:
            if not m.get("date"):
                return False
            dt = datetime.fromisoformat(m["date"])
            if dt > now - timedelta(days=int(mt["older_than_days"])):
                return False
        if "min_sender_score" in mt and scores.get(addr, 0.0) < float(mt["min_sender_score"]):
            return False
        return True


@dataclass
class RuleSet:
    rules: list[Rule]
    protect_flagged: bool = True
    protect_answered: bool = True


def load_rules(path: Path) -> RuleSet:
    return parse_rules(Path(path).read_text(encoding="utf-8"))


def parse_rules(text: str) -> RuleSet:
    data = yaml.safe_load(text) or {}
    rules = [
        Rule(name=r.get("name") or f"rule-{i}", match=dict(r.get("match") or {}),
             action=r.get("action", "trash"))
        for i, r in enumerate(data.get("rules") or [], 1)
    ]
    return RuleSet(rules, data.get("protect_flagged", True), data.get("protect_answered", True))


def evaluate(conn, ruleset: RuleSet, now: datetime | None = None) -> list[dict]:
    """Ritorna i match: un messaggio viene assegnato alla prima regola che lo colpisce."""
    now = now or datetime.now(UTC)
    scores = {s["sender"]: s["score"] for s in analysis.senders(conn, sample_subjects=0)}
    out = []
    for row in conn.execute("SELECT * FROM messages ORDER BY folder, uid"):
        m = dict(row)
        if ruleset.protect_flagged and m["flagged"]:
            continue
        if ruleset.protect_answered and m["answered"]:
            continue
        for r in ruleset.rules:
            if r.matches(m, scores, now):
                if r.action != "keep":
                    out.append({**m, "rule": r.name, "action": r.action})
                break
    return out


def suggest(conn, min_score: float = 0.6, min_count: int = 3, limit: int = 50) -> str:
    """Genera un file di regole a partire dai mittenti con punteggio alto. Da rivedere a mano."""
    rules = []
    for s in analysis.senders(conn, min_count=min_count, sample_subjects=2):
        if s["score"] < min_score:
            continue
        rules.append({
            "name": s["sender"],
            "note": f"{s['n']} mail, {round(s['unread_ratio'] * 100)}% non lette, score {s['score']}",
            "examples": [(x or "")[:70] for x in s.get("subjects", [])],
            "match": {"from": [s["sender"]]},
            "action": "trash",
        })
        if len(rules) >= limit:
            break
    header = (
        "# Regole suggerite da mail-radar in base ai soli header. RIVEDILE prima di applicarle.\n"
        "# Prova sempre con:  mail-radar rules apply rules.yaml  (dry-run di default)\n"
    )
    return header + yaml.safe_dump(
        {"protect_flagged": True, "protect_answered": True, "rules": rules},
        allow_unicode=True, sort_keys=False, width=120,
    )
