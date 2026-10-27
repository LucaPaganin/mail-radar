"""CLI di mail-radar."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from . import analysis, db
from . import rules as rules_mod
from .config import load_config

app = typer.Typer(help="Radiografia di una casella IMAP dai soli header.", no_args_is_help=True)
rules_app = typer.Typer(help="Regole di pulizia (YAML).", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
console = Console()

_state: dict = {}


@app.callback()
def main(
    config: Annotated[Path | None, typer.Option("--config", "-c", help="File TOML")] = None,
    db_path: Annotated[Path | None, typer.Option("--db", help="Percorso cache SQLite")] = None,
):
    cfg = load_config(config)
    if db_path:
        cfg.db_path = db_path
    _state["cfg"] = cfg


def _conn():
    return db.connect(_state["cfg"].db_path)


def _account():
    acc = _state["cfg"].account
    if not acc:
        console.print("[red]Account non configurato.[/] Usa mail-radar.toml o MAIL_RADAR_USER (+ MAIL_RADAR_PROVIDER/HOST).")
        raise typer.Exit(2)
    return acc


def _mb(b) -> str:
    return f"{(b or 0) / 1_048_576:.1f} MB"


@app.command()
def folders():
    """Elenca le cartelle IMAP."""
    from .imap import list_folders, open_client

    with open_client(_account()) as c:
        for f in list_folders(c):
            console.print(f)


@app.command()
def scan(
    folder: Annotated[list[str] | None, typer.Option("--folder", "-f", help="Cartella (ripetibile)")] = None,
    all_folders: Annotated[bool, typer.Option("--all", help="Tutte le cartelle")] = False,
    full: Annotated[bool, typer.Option("--full", help="Riscarica tutto da zero")] = False,
):
    """Scarica (solo) gli header nella cache locale. Incrementale."""
    from .imap import list_folders, open_client, scan_folder

    acc = _account()
    conn = _conn()
    with open_client(acc) as c:
        targets = list_folders(c) if all_folders else (folder or acc.folders)
        for f in targets:
            with Progress(console=console, transient=True) as prog:
                task = prog.add_task(f"Scan {f}", total=None)

                def cb(done, total, task=task):
                    prog.update(task, completed=done, total=total)

                try:
                    res = scan_folder(c, conn, f, progress=cb, full=full)
                except Exception as e:  # cartelle non selezionabili ecc.
                    console.print(f"[yellow]{f}: saltata ({e})[/]")
                    continue
            console.print(f"[green]{f}[/]: {res['new']} nuove, {res['removed']} rimosse, {res['total']} totali")


@app.command()
def stats():
    """Panoramica della casella."""
    conn = _conn()
    o = analysis.overview(conn)
    if not o["n"]:
        console.print("Cache vuota: lancia prima [bold]mail-radar scan[/] (o [bold]mail-radar demo[/]).")
        raise typer.Exit()
    console.print(f"[bold]{o['n']}[/] mail, {_mb(o['bytes'])}, {o['senders']} mittenti, {o['domains']} domini")
    console.print(f"Non lette: {o['unread']} ({o['unread'] / o['n']:.0%})  ·  "
                  f"Bulk/newsletter: {o['bulk']} ({o['bulk'] / o['n']:.0%})")
    console.print(f"Periodo: {(o['first'] or '')[:10]} → {(o['last'] or '')[:10]}")
    t = Table("Etichetta", "Mittenti", "Mail", "Spazio")
    for k, v in analysis.junk_summary(conn).items():
        t.add_row(k, str(v["senders"]), str(v["messages"]), _mb(v["bytes"]))
    console.print(t)


@app.command()
def senders(
    by: Annotated[str, typer.Option(help="address | domain")] = "address",
    limit: int = 30,
    label: Annotated[str | None, typer.Option(help="junk | maybe | keep")] = None,
    min_count: int = 1,
):
    """Classifica dei mittenti per punteggio spazzatura."""
    rows = analysis.senders(_conn(), by=by, min_count=min_count, sample_subjects=1)
    if label:
        rows = [r for r in rows if r["label"] == label]
    t = Table("Mittente", "Mail", "Non lette", "Bulk", "Risp.", "Score", "Es. oggetto")
    color = {"junk": "red", "maybe": "yellow", "keep": "green"}
    for r in rows[:limit]:
        t.add_row(r["sender"], str(r["n"]), f"{r['unread_ratio']:.0%}", f"{r['bulk_ratio']:.0%}",
                  str(r["answered"]), f"[{color[r['label']]}]{r['score']:.2f}[/]",
                  ((r.get("subjects") or [""])[0] or "")[:50])
    console.print(t)


@app.command()
def export(
    output: Annotated[Path | None, typer.Option("-o", help="File di output (default stdout)")] = None,
    fmt: Annotated[str, typer.Option("--format", help="json | csv")] = "json",
    by: str = "address",
    min_count: int = 1,
):
    """Esporta il riepilogo per mittente (solo header): utile da dare a un LLM per scrivere regole."""
    rows = analysis.senders(_conn(), by=by, min_count=min_count, sample_subjects=3)
    out = open(output, "w", encoding="utf-8", newline="") if output else sys.stdout
    try:
        if fmt == "csv":
            for r in rows:
                r["subjects"] = " | ".join(r.get("subjects") or [])
            w = csv.DictWriter(out, fieldnames=list(rows[0].keys()) if rows else ["sender"])
            w.writeheader()
            w.writerows(rows)
        else:
            json.dump(rows, out, ensure_ascii=False, indent=1)
    finally:
        if output:
            out.close()
            console.print(f"Esportati {len(rows)} mittenti in {output}")


@rules_app.command("suggest")
def rules_suggest(
    output: Annotated[Path, typer.Option("-o")] = Path("rules.yaml"),
    min_score: float = 0.6,
    min_count: int = 3,
):
    """Genera regole bozza dai mittenti con score alto."""
    text = rules_mod.suggest(_conn(), min_score=min_score, min_count=min_count)
    output.write_text(text, encoding="utf-8")
    console.print(f"Scritto {output}. Rivedilo, poi: mail-radar rules apply {output}")


def _show_matches(matches: list[dict]):
    by_rule = defaultdict(list)
    for m in matches:
        by_rule[m["rule"]].append(m)
    t = Table("Regola", "Azione", "Mail", "Spazio", "Esempio")
    for r, ms in by_rule.items():
        t.add_row(r, ms[0]["action"], str(len(ms)), _mb(sum(m["size"] or 0 for m in ms)),
                  f"{ms[0]['from_addr']}: {(ms[0]['subject'] or '')[:40]}")
    console.print(t)
    console.print(f"Totale: [bold]{len(matches)}[/] mail, {_mb(sum(m['size'] or 0 for m in matches))}")


@rules_app.command("apply")
def rules_apply(
    rules_file: Path,
    execute: Annotated[bool, typer.Option("--execute", help="Esegui davvero (default: dry-run)")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Non chiedere conferma")] = False,
):
    """Valuta le regole sulla cache. Con --execute sposta le mail (mai cancellazione permanente)."""
    conn = _conn()
    matches = rules_mod.evaluate(conn, rules_mod.load_rules(rules_file))
    if not matches:
        console.print("Nessuna mail corrisponde alle regole.")
        return
    _show_matches(matches)
    if not execute:
        console.print("[cyan]Dry-run.[/] Aggiungi --execute per applicare.")
        return
    if not yes and not typer.confirm(f"Spostare {len(matches)} mail?"):
        raise typer.Abort()

    from .imap import find_trash, move_uids, open_client

    acc = _account()
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for m in matches:
        groups[(m["folder"], m["action"])].append(m)
    with open_client(acc) as c:
        trash = find_trash(c, acc.trash_folder)
        for (folder, action), ms in groups.items():
            dest = trash if action == "trash" else action.split(":", 1)[1]
            if dest == folder:
                continue
            uids = [m["uid"] for m in ms]
            for i in range(0, len(uids), 500):
                how = move_uids(c, folder, uids[i:i + 500], dest)
            db.log_actions(conn, [{"rule": m["rule"], "action": f"{action}->{dest}", "folder": folder,
                                   "uid": m["uid"], "from_addr": m["from_addr"],
                                   "subject": m["subject"]} for m in ms])
            db.delete_uids(conn, folder, uids)
            console.print(f"{folder} → {dest}: {len(uids)} mail ({how})")
            if how == "copy+flag_deleted":
                console.print("[yellow]Il server non supporta MOVE/UIDPLUS: originali marcati \\Deleted, non espunti.[/]")


@app.command()
def dashboard(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
):
    """Avvia la dashboard web locale."""
    import threading
    import webbrowser

    import uvicorn

    from .web import create_app

    url = f"http://{host}:{port}"
    console.print(f"Dashboard su [link={url}]{url}[/link] (Ctrl+C per uscire)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(_state["cfg"].db_path), host=host, port=port, log_level="warning")


@app.command()
def demo(months: int = 18):
    """Popola la cache con dati sintetici (per provare la dashboard)."""
    from .demo import generate

    n = generate(_conn(), months=months)
    console.print(f"Generate {n} mail sintetiche in {_state['cfg'].db_path}")


if __name__ == "__main__":
    app()
