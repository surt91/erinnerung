# Implementierungsplan

Umsetzung von [idea.md](idea.md): ein persönlicher Erinnerungs-Bot, konfiguriert über YAML,
zustellend über Telegram, nervend bis zur Bestätigung, startbar per `docker compose up`.

## Entscheidungen

| Frage | Entscheidung |
|---|---|
| Sprache/Stack | Python 3.12, `python-telegram-bot`, Pydantic v2, SQLite, `croniter` |
| Nag-Verhalten | Festes Intervall pro Erinnerung + globale Ruhezeiten |
| Überlappung | Pro Erinnerung konfigurierbar (`on_overlap: replace \| stack`), Default `replace` |
| Downtime | Nachholen innerhalb eines Kulanzfensters (Default 24 h), Älteres verfällt |
| Schedule-Syntax | Lesbare Kurzform *und* Cron als Notausgang |
| Vorlaufzeit | Optionales `lead:`-Feld |
| Config-Reload | Automatischer Dateiwatch, bei Fehler bleibt alte Config aktiv |
| Einmal-Termine | Nach Erledigung automatisch nach `config/archive/` verschoben |
| Telegram-Fehler | Wackler werden zweimal wiederholt, Ablehnungen protokolliert; ein globaler Error-Handler meldet unerwartete Fehler in den Chat |

## Architektur

```
config/*.yaml  ──►  Loader/Validator  ──►  Reminder-Definitionen (in-memory)
     ▲                                          │
     │ Watchdog                                 ▼
     │                       ┌──────  Occurrence-Engine (Tick alle 30 s)
  Archiver  ◄────────────────┤        berechnet next_due, erzeugt Occurrences,
                             │        entscheidet über Nag/Ruhezeit/Verfall
                          SQLite                │
                       (occurrences)            ▼
                             ▲            Telegram-Sender
                             │                  │  Nachricht + Inline-Keyboard
                             └── Callback-Handler ◄──  [✅ Erledigt] [⏰ 1h] [🚫 Skip]
```

**Kernidee:** Die YAML-Dateien sind die *Definition*, die SQLite hält die *Instanzen*.
Jede Fälligkeit wird eine `occurrence`-Zeile (`open → acked | skipped | superseded | expired`).
Alles — Nagging, Snooze, Neustart-Robustheit — läuft über genau diesen einen Zustand,
statt über einen zweiten Scheduler mit eigenem Gedächtnis.

## Konfiguration

`config/haushalt.yaml`:

```yaml
reminders:
  - id: muell-bio                  # stabil, dient als DB-Key
    title: "Biotonne rausstellen"
    schedule:
      every: friday
      at: "18:00"
    nag:
      interval: 30m                # Default aus settings.yaml
    on_overlap: replace

  - id: rechnungen
    title: "Kontoauszüge durchsehen"
    schedule:
      cron: "0 9 1 * *"            # Notausgang für Exotisches
    on_overlap: stack              # jede Fälligkeit einzeln quittieren

  - id: geburtstag-mama
    title: "Geschenk für Mama besorgen"
    schedule:
      once: 2026-10-12 09:00
    lead: 3d                       # feuert schon am 09.10.
```

`config/settings.yaml` (global, optional):

```yaml
timezone: Europe/Berlin
quiet_hours: { from: "22:00", to: "08:00" }
nag: { interval: 60m }
catchup_window: 24h
snooze_options: [1h, 3h, tomorrow]
```

Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) kommen aus `.env`, nicht aus der YAML.

## Datenmodell (SQLite)

```sql
CREATE TABLE occurrences (
  id            INTEGER PRIMARY KEY,
  reminder_id   TEXT    NOT NULL,        -- aus der YAML
  due_at        TEXT    NOT NULL,        -- ISO 8601, UTC
  status        TEXT    NOT NULL,        -- open|acked|skipped|superseded|expired
  created_at    TEXT    NOT NULL,
  resolved_at   TEXT,
  last_nag_at   TEXT,
  nag_count     INTEGER NOT NULL DEFAULT 0,
  snoozed_until TEXT,
  message_id    INTEGER,                 -- für Edit statt Neu-Senden
  UNIQUE(reminder_id, due_at)            -- macht den Tick idempotent
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- u.a. last_tick_at
```

Der `UNIQUE`-Constraint ist der Trick für Neustart-Sicherheit: Der Tick darf beliebig oft
denselben Zeitraum verarbeiten, ohne doppelt zu erinnern.

## Tick-Logik (alle 30 s)

1. **Aufholen:** Für jede Erinnerung alle Fälligkeiten zwischen `last_tick_at` und *jetzt*
   berechnen (`croniter`, inkl. `lead`-Verschiebung). Älter als `catchup_window` → direkt
   als `expired` anlegen, nicht senden.
2. **Anlegen:** Neue Occurrences einfügen. Bei `on_overlap: replace` vorherige offene
   Occurrence derselben Erinnerung auf `superseded` setzen.
3. **Senden/Nerven:** Alle `open`-Occurrences, deren `snoozed_until` abgelaufen ist und
   deren letzter Nag länger als `nag.interval` her ist, zustellen — außerhalb der
   Ruhezeiten. Bereits gesendete Nachricht wird editiert (Zähler „seit 3 h offen"),
   damit der Chat nicht zumüllt.
4. **Archivieren:** Erledigte Einmal-Termine nach `config/archive/<jahr>.yaml` verschieben.

## Phasen

**Phase 1 — Gerüst**
`pyproject.toml` (uv), Paketlayout `erinnerung/`, Multi-Stage-`Dockerfile`,
`docker-compose.yml` (Bind-Mount `./config`, Volume für `state.db`, `TZ`, `.env`),
Beispielkonfiguration, `.env.example`.

**Phase 2 — Config-Layer**
Pydantic-Modelle für `Reminder`, `Schedule`, `Nag`, `Settings`. Parser für Kurzform *und*
Cron, Dauer-Parser (`30m`, `3d`, `tomorrow`). Loader über den Ordner mit Prüfung auf
doppelte IDs. Fehler schlagen beim Start fehl, nicht erst zur Laufzeit.

**Phase 3 — Occurrence-Engine**
DB-Schema + Migrationen, `next_occurrences(reminder, from, to)`, Tick-Loop mit
injizierbarer Uhr, Catch-up, Overlap-Handling, Ruhezeiten.
*Diese Phase bekommt die meisten Tests — hier steckt die eigentliche Komplexität.*

**Phase 4 — Telegram**
Bot per Long-Polling (kein offener Port, kein Reverse Proxy nötig), Chat-Allowlist,
Nachricht mit Inline-Keyboard, Callback-Handler → DB. Nach Phase 4 ist das Ding benutzbar.

**Phase 5 — Komfort**
Kommandos `/offen`, `/erledigt <id>`, `/snooze <id> <dauer>`, `/status`, `/reload`.
Dateiwatch (`watchfiles`) mit Fehlerreport per Telegram statt Absturz.
Archivierung via `ruamel.yaml` im Round-Trip-Modus, damit Kommentare und Formatierung
deiner Dateien erhalten bleiben.
`/neu <wann> <was>` schreibt eine neue Erinnerung nach `config/inbox.yaml` — damit geht
das Anlegen vom Handy, ohne dass die YAML ihren Rang als einzige Wahrheit verliert.

**Phase 6 — Abschluss**
`README.md` (BotFather-Setup, Config-Referenz, Betrieb), strukturiertes Logging,
Healthcheck in Compose, `restart: unless-stopped`.

## Offene Punkte / Annahmen

- Zeitzone `Europe/Berlin`, überschreibbar via `TZ` bzw. `settings.yaml`.
- Ein einziger Ziel-Chat (deiner). Mehrere Empfänger wären ein späteres `to:`-Feld.
- Zeiten in der DB immer UTC, Anzeige lokal — vermeidet DST-Überraschungen.
- Der Bot editiert YAML nur beim Archivieren und bei `/neu`; der Watcher ignoriert
  `config/archive/` und selbst ausgelöste Schreibvorgänge.

## Später (aus idea.md)

Web-Frontend (FastAPI + HTMX auf demselben Container), Gamification (Streaks aus der
`occurrences`-Tabelle — die Daten fallen ohnehin an), Import aus iCal/CalDAV als
weiterer Loader neben dem YAML-Loader.
