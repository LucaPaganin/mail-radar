# mail-radar

Radiografia di una casella di posta IMAP **leggendo solo gli header** (mittente, oggetto, data, dimensione,
flag, `List-Unsubscribe`…). Nessun corpo dei messaggi viene scaricato.

- **scan** incrementale in una cache SQLite locale
- **insight**: classifica mittenti con punteggio "spazzatura", volumi nel tempo, orari, domini
- **dashboard** web locale (nessuna dipendenza esterna, gira su `127.0.0.1`)
- **regole** YAML con dry-run di default; le mail vengono solo **spostate** nel cestino, mai cancellate
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
mail-radar dashboard               # apre http://127.0.0.1:8765
mail-radar export -o senders.json  # riepilogo per LLM

mail-radar rules suggest -o rules.yaml      # bozza di regole dai mittenti peggiori
mail-radar rules apply rules.yaml           # dry-run: mostra cosa verrebbe spostato
mail-radar rules apply rules.yaml --execute # sposta davvero (chiede conferma)
```

Senza una casella a portata di mano: `mail-radar --db demo.db demo && mail-radar --db demo.db dashboard`.

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

## Sicurezza

- lo scan apre le cartelle in sola lettura e usa `BODY.PEEK` (non marca le mail come lette)
- nessuna cancellazione permanente: `MOVE`, oppure `COPY` + `UID EXPUNGE` solo sugli UID coinvolti; se il
  server non ha né `MOVE` né `UIDPLUS` gli originali restano marcati `\Deleted` senza expunge
- la dashboard ascolta solo su `127.0.0.1` e non può modificare la casella

## Sviluppo

```bash
uv sync
uv run pytest
uv run ruff check
```
