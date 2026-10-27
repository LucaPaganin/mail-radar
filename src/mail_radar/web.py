"""Dashboard locale: API JSON sulla cache + una pagina statica senza dipendenze esterne."""

from __future__ import annotations

from collections import Counter
from importlib.resources import files
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from . import analysis, db
from . import rules as rules_mod


def create_app(db_path: Path | str) -> FastAPI:
    conn = db.connect(db_path)
    app = FastAPI(title="mail-radar", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (files("mail_radar") / "static" / "index.html").read_text(encoding="utf-8")

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

    @app.post("/api/rules/preview")
    def rules_preview(yaml_text: str = Body(..., media_type="text/plain")):
        try:
            rs = rules_mod.parse_rules(yaml_text)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
        matches = rules_mod.evaluate(conn, rs)
        per_rule = Counter(m["rule"] for m in matches)
        size = sum(m["size"] or 0 for m in matches)
        return {
            "total": len(matches),
            "bytes": size,
            "per_rule": [{"rule": r, "n": n} for r, n in per_rule.most_common()],
            "sample": [
                {k: m[k] for k in ("rule", "from_addr", "subject", "date")} for m in matches[:50]
            ],
        }

    return app
