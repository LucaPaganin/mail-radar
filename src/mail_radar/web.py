"""GUI locale: API JSON sulla cache, job in background per le operazioni IMAP, pagina statica.

Sicurezza (il server può modificare la casella):
- ascolta solo su 127.0.0.1;
- controlla l'header Host (protezione da DNS rebinding);
- le richieste che modificano qualcosa richiedono un token casuale generato all'avvio e
  inserito nella pagina: un sito esterno non può leggerlo, quindi non può forgiare richieste.
"""

from __future__ import annotations

import csv
import io
import json
import secrets
import threading
import time
import traceback
import uuid
from collections import Counter
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import analysis, db, ops
from . import rules as rules_mod
from .config import Account, make_account


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.status = "running"  # running | done | error
        self.started = time.time()
        self.finished: float | None = None
        self.lines: list[str] = []
        self.progress: dict = {}
        self.result = None
        self.error: str | None = None

    def log(self, msg: str) -> None:
        self.lines.append(str(msg))
        del self.lines[:-500]

    def set_progress(self, label: str, done: int, total: int) -> None:
        self.progress = {"label": label, "done": done, "total": total}

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "log": self.lines[-200:],
                "progress": self.progress, "result": self.result, "error": self.error,
                "elapsed": round((self.finished or time.time()) - self.started, 1)}


class JobRunner:
    """Un job alla volta: le operazioni IMAP sulla stessa casella non vanno parallelizzate."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current: Job | None = None

    def start(self, kind: str, fn: Callable[[Job], object]) -> Job:
        with self.lock:
            if self.current and self.current.status == "running":
                raise HTTPException(409, f"C'è già un'operazione in corso ({self.current.kind})")
            job = Job(kind)
            self.current = job

        def run():
            try:
                job.result = fn(job)
                job.status = "done"
            except Exception as e:  # noqa: BLE001 - riportato alla GUI
                job.status = "error"
                job.error = str(e) or e.__class__.__name__
                job.log("ERRORE: " + job.error)
                job.log(traceback.format_exc(limit=3))
            finally:
                job.finished = time.time()

        threading.Thread(target=run, daemon=True).start()
        return job


def _cand_dict(c) -> dict:
    return {"sender": c.sender, "name": c.name, "n": c.n, "months": c.months_active,
            "method": c.method, "target": c.target, "auth": c.auth, "reasons": c.reasons,
            "warnings": c.warnings, "safe": c.safe}


def create_app(db_path: Path | str, account: Account | None = None,
               rules_path: Path | None = None, allowed_hosts: set[str] | None = None) -> FastAPI:
    conn = db.connect(db_path)
    token = secrets.token_urlsafe(24)
    jobs = JobRunner()
    state = {"account": account}
    app = FastAPI(title="mail-radar", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if allowed_hosts is not None and request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Host non consentito"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.url.path != "/api/rules/preview":
            if not secrets.compare_digest(request.headers.get("x-mr-token", ""), token):
                return JSONResponse({"detail": "Token mancante o errato"}, status_code=403)
        return await call_next(request)

    def need_account() -> Account:
        acc = state["account"]
        if acc is None:
            raise HTTPException(409, "Account non configurato")
        if acc.stored_password() is None:
            raise HTTPException(409, "Password IMAP non impostata")
        return acc

    def job_conn():
        return db.connect(db_path)

    # ── pagina ──────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (files("mail_radar") / "static" / "index.html").read_text(encoding="utf-8")
        return html.replace("__MR_TOKEN__", token)

    # ── lettura cache ───────────────────────────────────────────────────────
    @app.get("/api/overview")
    def overview():
        return {**analysis.overview(conn), "labels": analysis.junk_summary(conn)}

    @app.get("/api/monthly")
    def monthly():
        return analysis.monthly(conn)

    @app.get("/api/heatmap")
    def heatmap():
        return analysis.weekday_hour(conn)

    @app.get("/api/senders")
    def senders(by: str = "address", min_count: int = 1):
        if by not in ("address", "domain"):
            raise HTTPException(400, "by deve essere address o domain")
        return analysis.senders(conn, by=by, min_count=min_count, sample_subjects=2)

    @app.get("/api/messages")
    def messages(sender: str | None = None, domain: str | None = None, limit: int = 200):
        return analysis.messages(conn, sender=sender, domain=domain, limit=limit)

    @app.get("/api/export")
    def export(format: str = "csv", by: str = "address"):
        rows = analysis.senders(conn, by=by, sample_subjects=3)
        if format == "json":
            body, mime, ext = json.dumps(rows, ensure_ascii=False, indent=1), "application/json", "json"
        else:
            buf = io.StringIO()
            for r in rows:
                r["subjects"] = " | ".join(r.get("subjects") or [])
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["sender"])
            w.writeheader()
            w.writerows(rows)
            body, mime, ext = buf.getvalue(), "text/csv", "csv"
        return Response(body, media_type=mime, headers={
            "Content-Disposition": f'attachment; filename="mail-radar-senders.{ext}"'})

    # ── account ─────────────────────────────────────────────────────────────
    @app.get("/api/account")
    def get_account():
        acc = state["account"]
        if acc is None:
            return {"configured": False}
        return {"configured": True, "user": acc.user, "host": acc.host, "port": acc.port,
                "smtp_host": acc.smtp_host, "has_password": acc.stored_password() is not None,
                "folders": acc.folders}

    @app.post("/api/account")
    def set_account(data: dict = Body(...)):
        acc = state["account"]
        if data.get("user"):
            try:
                acc = make_account(data["user"].strip(), data.get("provider"), data.get("host") or None)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            state["account"] = acc
        if acc is None:
            raise HTTPException(400, "Indica l'indirizzo")
        if data.get("password"):
            acc.set_password(data["password"])
        return get_account()

    @app.get("/api/folders")
    def folders():
        from .imap import list_folders, open_client

        acc = need_account()
        try:
            with open_client(acc) as c:
                return list_folders(c)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"IMAP: {e}") from e

    # ── job ─────────────────────────────────────────────────────────────────
    @app.get("/api/job")
    def job():
        return jobs.current.as_dict() if jobs.current else None

    @app.post("/api/scan")
    def scan(data: dict = Body(...)):
        acc = need_account()
        folders = data.get("folders") or None
        full = bool(data.get("full"))

        def run(j: Job):
            res = ops.scan(acc, job_conn(), folders=folders, full=full, log=j.log,
                           progress=j.set_progress)
            return {"new": sum(r["new"] for r in res), "removed": sum(r["removed"] for r in res)}

        return jobs.start("scan", run).as_dict()

    # ── regole ──────────────────────────────────────────────────────────────
    def parse(text: str):
        try:
            return rules_mod.parse_rules(text)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Regole non valide: {e}") from e

    @app.get("/api/rules")
    def get_rules():
        text = rules_path.read_text(encoding="utf-8") if rules_path and rules_path.exists() else ""
        return {"path": str(rules_path) if rules_path else None, "text": text}

    @app.put("/api/rules")
    def save_rules(data: dict = Body(...)):
        if not rules_path:
            raise HTTPException(400, "Nessun file di regole configurato")
        text = data.get("text", "")
        parse(text)
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(text, encoding="utf-8")
        return {"path": str(rules_path), "saved": True}

    @app.post("/api/rules/preview")
    def rules_preview(yaml_text: str = Body(..., media_type="text/plain")):
        matches = rules_mod.evaluate(conn, parse(yaml_text))
        per_rule = Counter(m["rule"] for m in matches)
        return {
            "total": len(matches),
            "bytes": sum(m["size"] or 0 for m in matches),
            "per_rule": [{"rule": r, "n": n} for r, n in per_rule.most_common()],
            "sample": [{k: m[k] for k in ("rule", "from_addr", "subject", "date")}
                       for m in matches[:50]],
        }

    @app.post("/api/rules/apply")
    def rules_apply(data: dict = Body(...)):
        rs = parse(data.get("text", ""))
        acc = need_account()

        def run(j: Job):
            c = job_conn()
            matches = rules_mod.evaluate(c, rs)
            j.log(f"{len(matches)} mail da spostare")
            if not matches:
                return {"moved": 0}
            return {"moved": ops.apply_matches(acc, c, matches, log=j.log)}

        return jobs.start("rules", run).as_dict()

    # ── disiscrizioni ───────────────────────────────────────────────────────
    def selection(c, data: dict) -> set[str]:
        rs = parse(data["text"]) if data.get("rule_globs") and data.get("text") else None
        sel = ops.select_senders(c, data.get("senders"), data.get("domains"), rs,
                                 data.get("rule_globs"), everyone=bool(data.get("all")))
        if not sel:
            raise HTTPException(400, "Nessun mittente selezionato")
        return sel

    def plan_kwargs(data: dict) -> dict:
        return {"min_messages": int(data.get("min_messages", 3)),
                "min_months": int(data.get("min_months", 2)),
                "trust_dkim_signature": bool(data.get("trust_dkim_signature")),
                "force": bool(data.get("force"))}

    @app.post("/api/unsubscribe/plan")
    def unsub_plan(data: dict = Body(...)):
        sel = selection(conn, data)
        acc = state["account"]
        refresh = acc is not None and acc.stored_password() is not None

        def run(j: Job):
            cands = ops.unsub_plan(job_conn(), acc, sel, refresh=refresh, log=j.log,
                                   **plan_kwargs(data))
            if not refresh:
                j.log("Senza password non riscarico gli header: uso solo la cache.")
            return {"candidates": [_cand_dict(c) for c in cands]}

        return jobs.start("unsubscribe-plan", run).as_dict()

    @app.post("/api/unsubscribe/execute")
    def unsub_execute(data: dict = Body(...)):
        senders = {s.lower() for s in data.get("senders") or []}
        if not senders:
            raise HTTPException(400, "Nessun mittente indicato")
        acc = need_account()

        def run(j: Job):
            c = job_conn()
            # riclassifica lato server con le stesse opzioni (incluso force) scelte nella GUI
            cands = ops.unsub_plan(c, acc, senders, refresh=False, **plan_kwargs(data))
            skipped = [x.sender for x in cands if not x.safe]
            if skipped:
                j.log(f"Ignorati {len(skipped)} mittenti non sicuri: {', '.join(skipped[:5])}…")
            return ops.unsub_execute(c, acc, cands, trash=bool(data.get("trash")), log=j.log,
                                     progress=j.set_progress)

        return jobs.start("unsubscribe", run).as_dict()

    @app.get("/api/unsubscribe/history")
    def unsub_history():
        return [dict(r) for r in conn.execute("SELECT * FROM unsubscribes ORDER BY ts DESC LIMIT 500")]

    return app
