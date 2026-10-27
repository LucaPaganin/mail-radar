# mail-radar

Radiografia di una casella di posta IMAP **leggendo solo gli header** (mittente, oggetto, data, dimensione,
flag, `List-Unsubscribe`…). Nessun corpo dei messaggi viene scaricato.

- **scan** incrementale in una cache SQLite locale
- **insight**: classifica mittenti con punteggio "spazzatura", volumi nel tempo, orari, domini
- **GUI** web locale (`mail-radar dashboard`, solo `127.0.0.1`, nessuna dipendenza esterna): panoramica,
  scan, editor e applicazione delle regole, disiscrizioni
- **regole** YAML con dry-run di default; le mail vengono solo **spostate** nel cestino, mai cancellate
- **unsubscribe** in blocco, solo per mittenti autenticati (DMARC/DKIM) via one-click RFC 8058 o mailto
- **export** del riepilogo mittenti in JSON/CSV, da passare a un LLM per farsi scrivere le regole

Testato pensando a Libero Mail, funziona con qualsiasi server IMAP.

## Installazione

```bash
uv tool install git+https://github.com/LucaPaganin/mail-radar
# oppure, dal clone:  uv sync && uv run mail-radar --help
```

## Configurazione

`mail-radar.toml` nella cartella corrente oppure `~/.config/mail-radar/config.toml`
(esempi in [`examples/`](examples/); `mail-radar.toml`, `rules.yaml` e `*.db` sono in `.gitignore`):

```toml
[account]
user = "nome@libero.it"
provider = "libero"        # preset: libero, gmail, outlook, yahoo, icloud
# host = "imapmail.libero.it"  # in alternativa a provider
# port = 993
folders = ["INBOX"]        # cartelle per `scan` senza argomenti
# trash_folder = "Cestino" # default: rilevata automaticamente

# db_path = "~/.local/share/mail-radar/mail-radar.db"
```

La password **non** va nel file. In ordine: variabile `MAIL_RADAR_PASSWORD`, keyring di sistema
(`pip install mail-radar[keyring]`, servizio `mail-radar`), altrimenti la chiede a terminale.

Tutto si può dare anche via env: `MAIL_RADAR_USER`, `MAIL_RADAR_PROVIDER`, `MAIL_RADAR_HOST`,
`MAIL_RADAR_PORT`, `MAIL_RADAR_DB`.

> Libero: verifica che l'accesso IMAP da client esterni sia abilitato nelle impostazioni della casella.

## Uso

```bash
mail-radar folders                 # elenca le cartelle
mail-radar scan                    # scarica gli header (incrementale)
mail-radar scan --all              # tutte le cartelle
mail-radar stats                   # panoramica
mail-radar senders --label junk    # classifica mittenti
mail-radar dashboard               # GUI su http://127.0.0.1:8765 (--rules per scegliere il file di regole)
mail-radar export -o senders.json  # riepilogo per LLM

mail-radar rules suggest -o rules.yaml      # bozza di regole dai mittenti peggiori
mail-radar rules apply rules.yaml           # dry-run: mostra cosa verrebbe spostato
mail-radar rules apply rules.yaml --execute # sposta davvero (chiede conferma)
```

Senza una casella a portata di mano: `mail-radar --db demo.db demo && mail-radar --db demo.db dashboard`.

### GUI

`mail-radar dashboard` apre un'interfaccia con quattro schede:

- **Panoramica**: numeri, volume mensile, orari, domini, classifica mittenti con dettaglio delle mail.
  Dalla tabella selezioni mittenti e li trasformi in una regola o li mandi alle disiscrizioni; export CSV/JSON.
- **Scan**: account (se non configurato puoi inserirlo lì, resta solo in memoria), scelta cartelle, scan con
  avanzamento.
- **Regole**: editor YAML collegato a un file (`~/.config/mail-radar/rules.yaml` o `--rules`), anteprima per
  regola, salvataggio e applicazione con conferma.
- **Disiscrizioni**: analisi dei candidati (dalle regole o dalla selezione), con motivo per ogni scarto,
  esecuzione e storico. I mittenti non sicuri non sono selezionabili e il server li riclassifica comunque.

Le operazioni sulla casella girano in background, una alla volta, con barra di avanzamento e log.

## Punteggio spazzatura

Per ogni mittente, in `[0, 1]`:

```
score = 0.40·bulk + 0.35·non_lette + 0.15·noreply + 0.10·min(1, log10(n)/2)
```

dove `bulk` è la frazione di mail con `List-Unsubscribe`/`List-Id`/`Precedence: bulk`, `noreply` vale 1 se
l'indirizzo sembra un invio massivo. Se hai mai risposto lo score è moltiplicato per 0.2, se hai messo una
stella per 0.3. Etichette: `junk ≥ 0.6`, `maybe ≥ 0.35`, altrimenti `keep`.

## Regole

```yaml
protect_flagged: true    # default: mai toccare mail con stella
protect_answered: true   # default: mai toccare mail a cui hai risposto
rules:
  - name: promo amazon
    match:
      from_domain: [amazon.it]        # include i sottodomini
      from: ["offerte@*"]             # glob sull'indirizzo
      subject_contains: [offerta]
      subject_regex: "^sconto"
      has_unsubscribe: true
      unread: true
      older_than_days: 30
      folder: [INBOX]
      min_sender_score: 0.6
    action: trash                     # trash | move:<cartella> | keep
```

Tra chiavi diverse vale AND, dentro una lista vale OR. Ogni mail è assegnata alla prima regola che la
colpisce: regole `keep` in cima fanno da whitelist (fatture, mittenti personali) e proteggono quelle mail
dalle regole generiche più sotto. Le azioni eseguite finiscono nella tabella `actions_log` della cache.

## Disiscrizione in blocco

```bash
mail-radar unsubscribe --rules rules.yaml --rule 'newsletter*'      # dry-run
mail-radar unsubscribe --rules rules.yaml --rule 'newsletter*' --execute --trash
mail-radar unsubscribe -s news@brand.it -d altrobrand.com          # selezione puntuale
```

Disiscriversi da spam vero conferma che l'indirizzo è attivo, quindi il comando è volutamente
restrittivo. Un mittente viene disiscritto in automatico solo se **tutte** queste condizioni valgono:

1. l'hai selezionato tu (`--sender`, `--domain`, oppure le regole il cui nome corrisponde a `--rule`);
   non esiste un default "tutti";
2. l'ultima mail offre un metodo standard: **one-click RFC 8058** (`List-Unsubscribe-Post`, POST https)
   oppure **mailto** (inviato via SMTP del tuo provider, es. `smtp.libero.it:465`);
3. il server di ricezione ne attesta l'autenticità nell'header `Authentication-Results` più in alto
   (l'unico non falsificabile dal mittente): **DMARC pass**, oppure **DKIM pass** con dominio allineato al From;
4. c'è uno storico: almeno `--min-messages` mail (3) in `--min-months` mesi diversi (2);
5. non hai mai risposto o messo la stella a quel mittente;
6. l'URL è https verso un host pubblico.

I link che portano a una pagina web (senza one-click) non vengono mai aperti: `--manual-out links.tsv`
li esporta da aprire a mano. Ogni tentativo finisce nella tabella `unsubscribes` e non viene ripetuto.
`--trash` sposta nel cestino le mail esistenti dei mittenti disiscritti con successo.

Le cache create con la v0.1 non hanno gli header di autenticazione: il comando riscarica automaticamente
solo quelli dell'ultima mail di ciascun mittente selezionato (`--no-refresh` per evitarlo).

## Sicurezza

- lo scan apre le cartelle in sola lettura e usa `BODY.PEEK` (non marca le mail come lette)
- nessuna cancellazione permanente: `MOVE`, oppure `COPY` + `UID EXPUNGE` solo sugli UID coinvolti; se il
  server non ha né `MOVE` né `UIDPLUS` gli originali restano marcati `\Deleted` senza expunge
- la GUI ascolta solo su `127.0.0.1`, verifica l'header `Host` (DNS rebinding) e accetta richieste che
  modificano qualcosa solo con un token casuale generato all'avvio e inserito nella pagina

## Sviluppo

```bash
uv sync
uv run pytest
uv run ruff check
```
