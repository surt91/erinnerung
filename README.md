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

4. **Starten:**

   ```sh
   docker compose up -d
   ```

Nur die konfigurierte Chat-ID darf mit dem Bot reden — alles andere wird ignoriert.
Der Bot hängt per Long-Polling an Telegram: kein offener Port, kein Reverse Proxy.

## Erinnerungen konfigurieren

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

Mehr zu den Entwurfsentscheidungen steht in [PLAN.md](PLAN.md).
