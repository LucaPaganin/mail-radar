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


def _select_senders(conn, senders, domains, rules_file, rule_globs) -> set[str]:
    import fnmatch

    out: set[str] = {s.lower() for s in senders or []}
    for d in domains or []:
        d = d.lower()
        out |= {r[0] for r in conn.execute(
            "SELECT DISTINCT from_addr FROM messages WHERE from_domain = ? OR from_domain LIKE ?",
            (d, f"%.{d}"))}
    if rules_file:
        globs = rule_globs or ["*"]
        for m in rules_mod.evaluate(conn, rules_mod.load_rules(rules_file)):
            if any(fnmatch.fnmatch(m["rule"].lower(), g.lower()) for g in globs):
                out.add(m["from_addr"])
    out.discard(None)
    return out


@app.command()
def unsubscribe(
    sender: Annotated[list[str] | None, typer.Option("--sender", "-s", help="Indirizzo (ripetibile)")] = None,
    domain: Annotated[list[str] | None, typer.Option("--domain", "-d", help="Dominio (ripetibile)")] = None,
    rules_file: Annotated[Path | None, typer.Option("--rules", help="File di regole da cui prendere i mittenti")] = None,
    rule: Annotated[list[str] | None, typer.Option("--rule", "-r", help="Glob sul nome regola, es. 'newsletter*'")] = None,
    min_messages: int = 3,
    min_months: int = 2,
    trust_dkim_signature: Annotated[bool, typer.Option(help="Accetta la firma DKIM allineata anche senza Authentication-Results (più debole)")] = False,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Riscarica gli header estesi dell'ultima mail dei candidati")] = True,
    execute: Annotated[bool, typer.Option("--execute", help="Esegui davvero (default: dry-run)")] = False,
    trash: Annotated[bool, typer.Option("--trash", help="Dopo una disiscrizione riuscita sposta nel cestino le mail di quel mittente")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    manual_out: Annotated[Path | None, typer.Option(help="Scrivi qui i link da aprire a mano")] = None,
):
    """Disiscrizione in blocco, SOLO per mittenti selezionati che passano i controlli di sicurezza.

    Esempio:  mail-radar unsubscribe --rules rules.yaml --rule 'newsletter*'
    """
    from . import unsubscribe as unsub
    from .headers import HDR_VERSION

    conn = _conn()
    if not (sender or domain or rules_file):
        console.print("[red]Seleziona i mittenti:[/] --sender, --domain oppure --rules/--rule. "
                      "Per sicurezza non c'è un default 'tutti'.")
        raise typer.Exit(2)
    selected = _select_senders(conn, sender, domain, rules_file, rule)
    if not selected:
        console.print("Nessun mittente selezionato.")
        return
    cands = unsub.latest_per_sender(conn, selected)

    stale = [c for c in cands if c.hdr_version < HDR_VERSION and c.list_unsubscribe]
    if stale and refresh:
        from .imap import open_client, refresh_headers

        with open_client(_account()) as c:
            n = refresh_headers(c, conn, [vars(x) for x in stale])
        console.print(f"Aggiornati gli header estesi di {n} mail.")
        cands = unsub.latest_per_sender(conn, selected)

    done = db.unsubscribed(conn)
    for c in cands:
        unsub.classify(c, min_messages=min_messages, min_months=min_months,
                       trust_dkim_signature=trust_dkim_signature, done=done)

    safe = [c for c in cands if c.safe]
    manual = [c for c in cands if c.method == "manual" and not c.interactions]
    t = Table("Mittente", "Mail", "Metodo", "Autenticazione", "Esito")
    for c in sorted(cands, key=lambda c: (not c.safe, -c.n)):
        t.add_row(c.sender, str(c.n), c.method, c.auth or "—",
                  "[green]ok[/]" if c.safe else f"[yellow]{'; '.join(c.reasons)}[/]")
    console.print(t)
    reasons: dict[str, int] = defaultdict(int)
    for c in cands:
        for r in c.reasons:
            reasons[r.split(" (")[0]] += 1
    console.print(f"[bold]{len(safe)}[/] su {len(cands)} mittenti disiscrivibili in automatico "
                  f"({sum(c.method == 'one-click' for c in safe)} one-click, "
                  f"{sum(c.method == 'mailto' for c in safe)} mailto).")
    for r, k in sorted(reasons.items(), key=lambda x: -x[1]):
        console.print(f"  scartati: {k:4d}  {r}")
    if manual:
        if manual_out:
            manual_out.write_text("\n".join(f"{c.sender}\t{c.target}" for c in manual) + "\n", encoding="utf-8")
            console.print(f"{len(manual)} link da aprire a mano scritti in {manual_out}")
        else:
            console.print(f"{len(manual)} mittenti hanno solo un link web: usa --manual-out per esportarli.")

    if not safe:
        return
    if not execute:
        console.print("[cyan]Dry-run.[/] Aggiungi --execute per disiscriverti dai mittenti 'ok'.")
        return
    if not yes and not typer.confirm(f"Disiscriversi da {len(safe)} mittenti?"):
        raise typer.Abort()

    def report(c, ok, detail):
        console.print(f"{'[green]✓[/]' if ok else '[red]✗[/]'} {c.sender}: {detail}")

    results = unsub.execute(conn, safe, _account(), progress=report)
    ok_senders = [c.sender for c, ok, _ in results if ok]
    console.print(f"Riuscite {len(ok_senders)} su {len(results)}. Log nella tabella 'unsubscribes'.")

    if trash and ok_senders:
        from .imap import find_trash, open_client

        ph = ",".join("?" * len(ok_senders))
        ms = [dict(r) for r in conn.execute(
            f"SELECT folder, uid, from_addr, subject FROM messages WHERE from_addr IN ({ph}) "
            "AND flagged = 0 AND answered = 0", ok_senders)]
        with open_client(_account()) as c:
            _move_to(c, conn, ms, find_trash(c, _account().trash_folder), "unsubscribe")


def _move_to(client, conn, ms: list[dict], dest: str, rule: str) -> None:
    from .imap import move_uids

    by_folder: dict[str, list[dict]] = defaultdict(list)
    for m in ms:
        by_folder[m["folder"]].append(m)
    for folder, fms in by_folder.items():
        if folder == dest:
            continue
        uids = [m["uid"] for m in fms]
        for i in range(0, len(uids), 500):
            how = move_uids(client, folder, uids[i:i + 500], dest)
        db.log_actions(conn, [{"rule": rule, "action": f"trash->{dest}", "folder": folder,
                               "uid": m["uid"], "from_addr": m["from_addr"],
                               "subject": m["subject"]} for m in fms])
        db.delete_uids(conn, folder, uids)
        console.print(f"{folder} → {dest}: {len(uids)} mail ({how})")


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
