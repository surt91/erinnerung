"""Aus einer Chat-Zeile eine Erinnerung machen — das Kommando ``/neu``.

Die Zeitangabe steht vorn, der Titel dahinter::

    freitag 18:00 Biotonne rausstellen
    täglich 19:00 Pflanzen gießen
    1.10. 9:00 Reifenwechsel buchen
    in 2h Wäsche aufhängen

Gelesen wird von links, solange die Wörter noch als Zeitangabe durchgehen; das
erste, das nicht mehr passt, beginnt den Titel. Fängt der Titel selbst mit
einer Zeitangabe an, trennt ein ``|`` die beiden Teile.

Geschrieben wird nach ``config/inbox.yaml`` — eine gewöhnliche Datei, die man
später von Hand aufräumen, umbenennen oder aufteilen kann. Damit bleibt die
YAML die einzige Wahrheit; ``/neu`` ist nur eine bequeme Schreibhilfe.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Container
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as Quoted

from . import format as fmt
from . import yamlio
from .config import INBOX_FILE, Reminder
from .duration import parse_duration
from .schedule import DAILY, DEFAULT_TIME, MONTHLY, WEEKDAYS, YEARLY, ScheduleError

INBOX_HEADER = (
    "Per /neu aus dem Chat angelegt. Ganz normale Konfiguration — du darfst hier\n"
    "aufräumen, umsortieren oder Einträge in eigene Dateien verschieben."
)

# Füllwörter dürfen zwischen den Angaben stehen: 'jeden freitag um 18:00'.
_FILLER = {"jeden", "jede", "jedes", "jeder", "am", "um", "und", "at", "every", "and"}

_RELATIVE_DAYS = {
    "heute": 0, "today": 0,
    "morgen": 1, "tomorrow": 1,
    "übermorgen": 2, "uebermorgen": 2,
}

_MONTHS = {
    "januar": 1, "january": 1, "jan": 1,
    "februar": 2, "february": 2, "feb": 2,
    "märz": 3, "maerz": 3, "march": 3, "mär": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6,
    "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}

_GERMAN_WEEKDAYS = {
    "MON": "Montag", "TUE": "Dienstag", "WED": "Mittwoch", "THU": "Donnerstag",
    "FRI": "Freitag", "SAT": "Samstag", "SUN": "Sonntag",
}

_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_HOUR = re.compile(r"^(\d{1,2})uhr$")
_DATE_DE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?(\d{4})?$")
_DATE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DAY_ONLY = re.compile(r"^(\d{1,2})\.$")

_SLUG_MAX = 32
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


class ComposeError(ValueError):
    """Die Zeile aus dem Chat ergibt keine Erinnerung — mit Text für die Antwort."""


@dataclass
class _Spec:
    """Was die Zeitangabe bisher hergegeben hat."""

    weekdays: list[str] = field(default_factory=list)
    period: str | None = None  # 'day' | 'month' | 'year'
    day: int | None = None
    month: int | None = None
    year: int | None = None
    clock: tuple[int, int] | None = None
    relative: int | None = None  # heute/morgen/übermorgen
    offset: timedelta | None = None  # 'in 2h'
    cron: str | None = None


@dataclass
class Draft:
    """Eine fertige, bereits validierte Erinnerung samt YAML-Gestalt."""

    reminder: Reminder
    entry: dict
    description: str


# -- Zeitangabe lesen ------------------------------------------------------


def _clock_from(text: str, hour: int, minute: int) -> tuple[int, int]:
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ComposeError(f"Die Uhrzeit '{text}' liegt außerhalb von 00:00–23:59.")
    return hour, minute


def _take(spec: _Spec, tokens: list[str], index: int, word: str) -> int | None:
    """Ein Wort (oder zwei) als Zeitangabe schlucken. ``None`` = passt nicht mehr."""
    following = tokens[index + 1].lower().strip(",;") if index + 1 < len(tokens) else ""

    if word == "cron":
        fields = tokens[index + 1 : index + 6]
        if len(fields) < 5:
            raise ComposeError("Ein Cron-Ausdruck braucht fünf Felder, z.B. 'cron 0 20 * * SUN'.")
        spec.cron = " ".join(fields)
        return index + 6

    if word == "in":
        try:
            spec.offset = parse_duration(following)
        except ValueError:
            return None  # 'in der Stadt einkaufen' — das ist schon der Titel
        return index + 2

    code = WEEKDAYS.get(word)
    if code is not None:
        if code not in spec.weekdays:
            spec.weekdays.append(code)
        return index + 1

    for words, period in ((DAILY, "day"), (MONTHLY, "month"), (YEARLY, "year")):
        if word in words:
            spec.period = period
            return index + 1

    if word in _RELATIVE_DAYS:
        spec.relative = _RELATIVE_DAYS[word]
        return index + 1

    match = _CLOCK.match(word)
    if match:
        spec.clock = _clock_from(word, int(match.group(1)), int(match.group(2)))
        return index + 1

    match = _HOUR.match(word)  # '18uhr'
    if match:
        spec.clock = _clock_from(word, int(match.group(1)), 0)
        return index + 1

    if word.isdigit() and following == "uhr":  # '18 Uhr'
        spec.clock = _clock_from(word, int(word), 0)
        return index + 2

    match = _DATE_ISO.match(word)
    if match:
        spec.year, spec.month, spec.day = (int(part) for part in match.groups())
        return index + 1

    match = _DATE_DE.match(word)  # '1.10.' / '1.10.2026'
    if match:
        spec.day, spec.month = int(match.group(1)), int(match.group(2))
        if match.group(3):
            spec.year = int(match.group(3))
        return index + 1

    match = _DAY_ONLY.match(word)  # '12. Oktober' oder 'monatlich am 15.'
    if match:
        month = _MONTHS.get(following)
        if month is not None:
            spec.day, spec.month = int(match.group(1)), month
            after = tokens[index + 2].strip(",;") if index + 2 < len(tokens) else ""
            if len(after) == 4 and after.isdigit():
                spec.year = int(after)
                return index + 3
            return index + 2
        if spec.period == "month" and spec.day is None:
            spec.day = int(match.group(1))
            return index + 1
        return None

    if spec.period == "month" and spec.day is None and word.isdigit():
        spec.day = int(word)  # 'monatlich am 15'
        return index + 1

    return None


def _read(tokens: list[str]) -> tuple[_Spec, int]:
    """Von links lesen. Zurück kommt die Zeitangabe und wo der Titel anfängt."""
    spec = _Spec()
    title_starts_at = 0
    cursor = 0

    while cursor < len(tokens):
        word = tokens[cursor].lower().strip(",;")
        if not word or word in _FILLER:
            # Füllwörter alleine verschieben den Titelanfang nicht — sonst
            # verlöre '18:00 Am Wochenende einkaufen' sein 'Am'.
            cursor += 1
            continue
        step = _take(spec, tokens, cursor, word)
        if step is None:
            break
        cursor = title_starts_at = step

    return spec, title_starts_at


# -- Zeitangabe in einen Zeitplan übersetzen -------------------------------


def _nothing_but(spec: _Spec, *allowed: str) -> None:
    for name in ("weekdays", "period", "day", "month", "relative", "offset", "cron"):
        if name in allowed:
            continue
        if getattr(spec, name):
            raise ComposeError(
                "Diese Zeitangabe widerspricht sich — nimm entweder einen Wochentag, "
                "einen Rhythmus oder ein Datum."
            )


def _at(spec: _Spec) -> str:
    return f"{spec.clock[0]:02d}:{spec.clock[1]:02d}" if spec.clock else DEFAULT_TIME


def _weekday_names(codes: list[str]) -> str:
    names = [_GERMAN_WEEKDAYS[code] for code in codes]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " und " + names[-1]


def _once(moment: datetime, now: datetime) -> tuple[dict, str]:
    if moment <= now:
        raise ComposeError(f"{fmt.stamp(moment)} ist schon vorbei.")
    return {"once": moment}, f"einmalig am {fmt.stamp(moment)}"


def _to_schedule(spec: _Spec, now: datetime) -> tuple[dict, str]:
    """Zeitplan-Felder für die YAML plus eine Beschreibung für den Chat."""
    at = _at(spec)

    if spec.cron:
        _nothing_but(spec, "cron")
        return {"cron": Quoted(spec.cron)}, f"cron: {spec.cron}"

    if spec.offset is not None:
        _nothing_but(spec, "offset")
        if spec.clock:
            raise ComposeError("'in 2h' und eine Uhrzeit zusammen ergeben keinen Sinn.")
        return _once((now + spec.offset).replace(second=0, microsecond=0), now)

    if spec.weekdays:
        _nothing_but(spec, "weekdays")
        names = [code.lower() for code in spec.weekdays]
        every = names[0] if len(names) == 1 else names
        return (
            {"every": every, "at": Quoted(at)},
            f"jeden {_weekday_names(spec.weekdays)} um {at}",
        )

    if spec.period == "day":
        _nothing_but(spec, "period")
        return {"every": "day", "at": Quoted(at)}, f"täglich um {at}"

    if spec.period == "month":
        _nothing_but(spec, "period", "day")
        day = spec.day or 1
        if not 1 <= day <= 31:
            raise ComposeError(f"Den {day}. gibt es in keinem Monat.")
        return (
            {"every": "month", "day": day, "at": Quoted(at)},
            f"monatlich am {day}. um {at}",
        )

    if spec.period == "year":
        _nothing_but(spec, "period", "day", "month")
        if spec.day is None or spec.month is None:
            raise ComposeError("Für 'jährlich' fehlt das Datum, z.B. 'jährlich 12.10.'.")
        _valid_date(2000, spec.month, spec.day)  # Schaltjahr, damit 29.2. durchgeht
        return (
            {"every": "year", "date": Quoted(f"{spec.month:02d}-{spec.day:02d}"), "at": Quoted(at)},
            f"jährlich am {spec.day}.{spec.month}. um {at}",
        )

    # Bleibt der einmalige Termin.
    hour, minute = spec.clock or (9, 0)
    if spec.relative is not None:
        _nothing_but(spec, "relative")
        day = now.date() + timedelta(days=spec.relative)
        return _once(datetime.combine(day, time(hour, minute)), now)

    if spec.day is not None and spec.month is not None:
        moment = datetime.combine(
            _valid_date(spec.year or now.year, spec.month, spec.day), time(hour, minute)
        )
        if spec.year is None and moment <= now:
            # Ohne Jahresangabe ist das nächste Vorkommen gemeint.
            moment = moment.replace(year=moment.year + 1)
        return _once(moment, now)

    if spec.clock:
        # Nur eine Uhrzeit: heute, und wenn die schon durch ist, morgen.
        moment = datetime.combine(now.date(), time(hour, minute))
        if moment <= now:
            moment += timedelta(days=1)
        return _once(moment, now)

    raise ComposeError("Ich habe keine Zeitangabe gefunden.")


def _valid_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ComposeError(f"Den {day}.{month}. gibt es nicht.") from exc


# -- id, Parsen, Schreiben -------------------------------------------------


def make_id(title: str, taken: Container[str] = ()) -> str:
    """Aus dem Titel eine kurze, eindeutige id: 'Biotonne rausstellen' -> 'biotonne-rausstellen'."""
    slug = title.lower().translate(_UMLAUTS)
    slug = unicodedata.normalize("NFKD", slug).encode("ascii", "ignore").decode("ascii")

    picked: list[str] = []
    for word in (part for part in re.split(r"[^a-z0-9]+", slug) if part):
        if picked and len("-".join([*picked, word])) > _SLUG_MAX:
            break
        picked.append(word)

    base = "-".join(picked)[:_SLUG_MAX].rstrip("-") or "erinnerung"
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        candidate = f"{base}-{number}"
    return candidate


def parse(text: str, *, now: datetime, taken: Container[str] = ()) -> Draft:
    """Eine ``/neu``-Zeile in eine validierte Erinnerung übersetzen.

    ``now`` ist die lokale Zeit; alles Relative (``morgen``, ``in 2h``, ein
    Datum ohne Jahr) bezieht sich darauf.
    """
    now = now.replace(tzinfo=None, second=0, microsecond=0)
    head, _, rest = text.strip().partition("\n")
    note = rest.strip() or None

    if "|" in head:
        left, _, right = head.partition("|")
        tokens = left.split()
        while tokens and tokens[-1].lower().strip(",;") in _FILLER:
            tokens.pop()  # 'freitag 18:00 um | ...' soll nicht am 'um' scheitern
        spec, consumed = _read(tokens)
        if consumed < len(tokens):
            raise ComposeError(f"'{tokens[consumed]}' verstehe ich in der Zeitangabe nicht.")
        title = right.strip()
    else:
        tokens = head.split()
        spec, consumed = _read(tokens)
        title = " ".join(tokens[consumed:]).strip()

    fields, description = _to_schedule(spec, now)
    if not title:
        raise ComposeError("Es fehlt der Titel — nach der Zeitangabe kommt, woran ich erinnern soll.")

    entry: dict = {"id": make_id(title, taken), "title": title}
    if note:
        entry["text"] = note
    entry["schedule"] = fields

    try:
        reminder = Reminder(**entry)
    except ValidationError as exc:
        raise ComposeError(exc.errors()[0]["msg"].removeprefix("Value error, ")) from exc
    except (ScheduleError, ValueError) as exc:
        raise ComposeError(str(exc)) from exc

    return Draft(reminder=reminder, entry=entry, description=description)


def write(draft: Draft, config_dir: Path, *, now: datetime) -> Path:
    """Den Entwurf an ``config/inbox.yaml`` anhängen und den Pfad zurückgeben."""
    path = Path(config_dir) / INBOX_FILE
    yamlio.append_reminder(
        path,
        draft.entry,
        comment=f"angelegt am {now.strftime('%d.%m.%Y um %H:%M')}",
        header=INBOX_HEADER,
    )
    return path
