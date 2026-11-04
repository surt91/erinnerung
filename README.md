# Erinnerung

Ein persönlicher Erinnerungs-Bot. Termine und Todos stehen in YAML-Dateien, die
Erinnerung kommt per Telegram — und sie hakt so lange nach, bis du bestätigst.

```
🔔 Biotonne rausstellen
   fällig seit 18:00 · offen seit 2 h · 3. Erinnerung
   [✅ Erledigt]  [🚫 Diesmal nicht]
   [⏰ 1h] [⏰ 3h] [⏰ morgen früh]
```

## Schnellstart

1. **Bot anlegen:** In Telegram [@BotFather](https://t.me/BotFather) anschreiben,
   `/newbot`, Namen vergeben, Token kopieren.
2. **Chat-ID herausfinden:** Deinen neuen Bot anschreiben (irgendwas, z.B. `/start`), dann

   ```sh
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'
   ```

3. **Zugangsdaten eintragen:**

   ```sh
   cp .env.example .env   # TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID ausfüllen
   ```

4. **Starten:** Auf dem Server reichen `docker-compose.yml`, `.env` und ein Ordner
   `config/` mit deinen YAML-Dateien — kein Checkout nötig. Das Image baut die CI bei
   jedem Push auf `main` und legt es unter `ghcr.io/surt91/erinnerung` ab
   (amd64 und arm64).

   ```sh
   mkdir -p data && sudo chown 1000:1000 data config   # der Container läuft als UID 1000
   docker compose up -d
   ```

   In `data/` liegt nur der Zustand (`state.db`: was offen ist, Schlummerzeiten,
   letzter Durchlauf). Geht er verloren, läuft der Bot einfach ab jetzt weiter —
   offene Erinnerungen sind dann allerdings vergessen. Sichern solltest du vor allem
   `config/`.

   **Update:**

   ```sh
   docker compose pull && docker compose up -d
   ```

   Neben `latest` gibt es für jeden Commit `sha-<hash>` und für Tags `v1.2.3` die
   Version — zum Festnageln oder Zurückrollen einfach den Tag in `docker-compose.yml`
   eintragen. Lokal selbst bauen geht mit `docker build -t ghcr.io/surt91/erinnerung .`

Nur die konfigurierte Chat-ID darf mit dem Bot reden — alles andere wird ignoriert.
Der Bot hängt per Long-Polling an Telegram: kein offener Port, kein Reverse Proxy.

## Erinnerungen anlegen

### Aus dem Chat — `/neu`

Erst wann, dann was. Das ist der bequeme Weg vom Handy aus:

```
/neu freitag 18:00 Biotonne rausstellen
/neu mo, mi, fr 7:30 Tabletten nehmen
/neu täglich 19:00 Pflanzen gießen
/neu monatlich am 1. Kontoauszüge durchsehen
/neu 12. Oktober 9:00 Geschenk für Mama besorgen
/neu 1.10. 9:00 Reifenwechsel buchen
/neu in 2h Wäsche aufhängen
/neu 20:00 Müll rausbringen
```

Gelesen wird von links, solange die Wörter als Zeitangabe durchgehen — das erste,
das nicht mehr passt, beginnt den Titel. Verstanden werden Wochentage (deutsch wie
englisch), `täglich`/`monatlich`/`jährlich`, Datumsangaben (`1.10.`, `12. Oktober`,
`2026-12-24`), `heute`/`morgen`/`übermorgen`, `in 2h` und als Notausgang
`cron 0 20 * * SUN`. Ohne Uhrzeit gilt 09:00; eine Uhrzeit ohne Datum meint heute,
und wenn die schon vorbei ist, morgen. Eine zweite Zeile der Nachricht wird zur
Beschreibung. Fängt der Titel selbst mit einer Zeitangabe an, trennt ein `|`:

```
/neu freitag 18:00 | Montagsmeeting vorbereiten
```

Der Bot antwortet mit dem übersetzten Zeitplan und der ersten Fälligkeit — wenn da
etwas anderes steht als gemeint, war die Zeitangabe missverständlich. Angelegt wird
in `config/inbox.yaml`, einer ganz normalen Konfigurationsdatei: aufräumen,
umsortieren oder in eigene Dateien verschieben ist jederzeit erlaubt.

### In der YAML

Im Ordner `config/` dürfen beliebig viele `*.yaml` liegen, jede mit beliebig vielen
Erinnerungen. Änderungen greifen innerhalb von Sekunden — der Bot beobachtet den Ordner.
Ist eine Datei kaputt, bleibt die bisherige Konfiguration aktiv und du bekommst die
Fehlermeldung in den Chat.

```yaml
reminders:
  - id: muell-bio                # eindeutig, dient als Merkmal in der Datenbank
    title: "Biotonne rausstellen"
    text: "Deckel zu."           # optionale zweite Zeile
    schedule:
      every: friday
      at: "18:00"
    nag:
      interval: 30m              # Default: 60m (aus settings.yaml)
    on_overlap: replace          # replace | stack
    respect_quiet_hours: true
    lead: 3d                     # optional: so lange vorher erinnern
```

### Zeitpläne

| Was du willst | Wie du es schreibst |
|---|---|
| Jeden Freitag 18:00 | `every: friday` + `at: "18:00"` |
| Mehrere Wochentage | `every: [mon, wed, fri]` + `at: "07:30"` |
| Täglich | `every: day` + `at: "19:00"` |
| Monatlich am 1. | `every: month` + `day: 1` |
| Jährlich am 12.10. | `every: year` + `date: "10-12"` |
| Alles Exotische | `cron: "0 20 * * SUN"` |
| Einmalig | `once: 2026-10-01 09:00` |

Wochentage gehen deutsch wie englisch (`fr`, `fri`, `freitag`, `friday`). Fehlt `at`,
wird 09:00 angenommen. Die Kurzform wird intern in Cron übersetzt — `/plan` im Chat
zeigt dir, wann es das nächste Mal klingelt, falls du dir unsicher bist.

### Globale Einstellungen

`config/settings.yaml` ist optional; hier stehen die Vorgaben:

```yaml
timezone: Europe/Berlin
quiet_hours: { from: "22:00", to: "08:00" }
nag: { interval: 60m }
catchup_window: 24h
snooze_options: [1h, 3h, tomorrow]
```

Dauern schreibst du als `45s`, `30m`, `2h`, `1h30m`, `3d`, `1w`.

## Im Chat

| Kommando | Wofür |
|---|---|
| `/neu freitag 18:00 Biotonne rausstellen` | Neue Erinnerung anlegen (siehe oben) |
| `/offen` | Was gerade auf dich wartet |
| `/erledigt muell-bio` | Bestätigen ohne Knopfdruck (Teileingabe reicht) |
| `/snooze muell-bio 2h` | Später nochmal |
| `/plan` | Nächste Fälligkeit jeder Erinnerung |
| `/status` | Zeitzone, offene Posten, letzter Durchlauf |
| `/reload` | Konfiguration von Hand neu einlesen |

## Wie es sich verhält

**Nachhaken.** Eine fällige Erinnerung bleibt offen, bis du ✅ oder 🚫 drückst. Beim
Nachhaken wird die alte Nachricht gelöscht und eine neue geschickt — nur so klingelt
das Handy erneut, und der Chat läuft trotzdem nicht voll.

**Nachtruhe.** Was innerhalb der `quiet_hours` fällig wird, wartet still bis zum
Morgen. Einzelne Erinnerungen dürfen das per `respect_quiet_hours: false` überstimmen.

**Zweimal fällig, nie bestätigt.** Bei `on_overlap: replace` (Default) bleibt nur die
neueste Instanz offen, die alte Nachricht verschwindet. Bei `stack` sammeln sich die
Fälligkeiten und wollen einzeln quittiert werden.

**Neustart und Ausfall.** Der Zustand liegt in SQLite, nicht im Speicher. Nach einer
Auszeit wird nachgeholt, was innerhalb des `catchup_window` fällig gewesen wäre; alles
Ältere verfällt — du bekommst darüber eine Sammelmeldung, aber kein Nachhaken.

**Einmalige Termine** wandern nach dem Bestätigen automatisch nach
`config/archive/<jahr>.yaml`. Kommentare und Formatierung deiner Dateien bleiben dabei
erhalten. Der Archivordner wird nicht mehr als Konfiguration gelesen.

**Aus der YAML gelöscht** heißt „will ich nicht mehr": eine noch offene Instanz wird
beim nächsten Durchlauf still geschlossen.

**Zeitumstellung.** Gerechnet wird in lokaler Wanduhrzeit, gespeichert in UTC. `18:00`
bleibt also auch nach der Umstellung `18:00`. Ein Termin, der in der Nacht der
Umstellung in die übersprungene Stunde fällt, verschiebt sich um eine Stunde.

## Entwicklung

```sh
uv sync
uv run pytest
uv run python -m erinnerung     # braucht .env
```

Der Aufbau in Kurzform: `config.py` liest und validiert die YAML, `schedule.py`
rechnet Zeitpläne in Fälligkeiten um, `store.py` hält den Zustand, `engine.py` ist der
Taktgeber (alle 30 s, idempotent), `bot.py` macht Telegram, `app.py` steckt alles
zusammen. Die Engine kennt Telegram nicht — deshalb lässt sie sich gegen eine
gefälschte Uhr testen.

Zurückgeschrieben wird die YAML an zwei Stellen: `compose.py` übersetzt die
`/neu`-Zeile in einen Eintrag, `archive.py` räumt erledigte Einmal-Termine weg. Beide
gehen durch `yamlio.py` und damit durch den Round-Trip-Modus von ruamel.yaml, damit
deine Kommentare und Anführungszeichen überleben.

Mehr zu den Entwurfsentscheidungen steht in [PLAN.md](PLAN.md).
