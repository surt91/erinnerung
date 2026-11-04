"""Zeitpläne: lesbare Kurzform und Cron, beides zu Fälligkeiten aufgelöst.

Die Kurzform (``every: friday`` / ``at: "18:00"``) wird beim Laden in einen
Cron-Ausdruck übersetzt. Dadurch gibt es zur Laufzeit nur *einen* Mechanismus,
der Zeitpunkte berechnet — die Kurzform ist reine Schreiberleichterung.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import CroniterBadCronError, croniter
from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

# Cron kennt nur englische Kürzel; hier darf man großzügiger schreiben.
# Öffentlich, weil /neu (siehe :mod:`erinnerung.compose`) dieselben Wörter
# verstehen soll wie die YAML.
WEEKDAYS = {
    "mon": "MON",
    "monday": "MON",
    "mo": "MON",
    "montag": "MON",
    "tue": "TUE",
    "tuesday": "TUE",
    "di": "TUE",
    "dienstag": "TUE",
    "wed": "WED",
    "wednesday": "WED",
    "mi": "WED",
    "mittwoch": "WED",
    "thu": "THU",
    "thursday": "THU",
    "do": "THU",
    "donnerstag": "THU",
    "fri": "FRI",
    "friday": "FRI",
    "fr": "FRI",
    "freitag": "FRI",
    "sat": "SAT",
    "saturday": "SAT",
    "sa": "SAT",
    "samstag": "SAT",
    "sun": "SUN",
    "sunday": "SUN",
    "so": "SUN",
    "sonntag": "SUN",
}
DAILY = {"day", "daily", "tag", "täglich", "taeglich"}
MONTHLY = {"month", "monthly", "monat", "monatlich"}
YEARLY = {"year", "yearly", "annually", "jahr", "jährlich", "jaehrlich"}

_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_MONTH_DAY = re.compile(r"^(\d{1,2})[-.](\d{1,2})\.?$")

DEFAULT_TIME = "09:00"


class ScheduleError(ValueError):
    """Der Zeitplan in der YAML ergibt keinen Sinn."""


def _parse_time(value: str | None) -> tuple[int, int]:
    if value is None:
        value = DEFAULT_TIME
    match = _TIME.match(str(value).strip())
    if not match:
        raise ScheduleError(f"Uhrzeit '{value}' nicht verstanden, erwartet wird z.B. '18:00'")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ScheduleError(f"Uhrzeit '{value}' liegt außerhalb von 00:00–23:59")
    return hour, minute


def _parse_month_day(value: str) -> tuple[int, int]:
    """``"10-12"`` oder ``"12.10."`` -> (Monat, Tag)."""
    text = str(value).strip()
    match = _MONTH_DAY.match(text)
    if not match:
        raise ScheduleError(
            f"Datum '{value}' nicht verstanden, erwartet wird 'MM-DD', z.B. '10-12'"
        )
    first, second = int(match.group(1)), int(match.group(2))
    # '10-12' ist Monat-Tag, '12.10.' ist die deutsche Schreibweise Tag.Monat.
    month, day = (second, first) if "." in text else (first, second)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ScheduleError(f"Datum '{value}' ist kein gültiger Kalendertag")
    return month, day


class Schedule(BaseModel):
    """Wann eine Erinnerung fällig wird.

    Genau eine der drei Formen muss gesetzt sein: ``every`` (Kurzform),
    ``cron`` (Notausgang) oder ``once`` (einmaliger Termin).
    """

    model_config = ConfigDict(extra="forbid")

    every: str | list[str] | None = None
    at: str | None = None
    day: int | None = None
    date: str | None = None
    cron: str | None = None
    once: datetime | None = None

    _compiled: str | None = PrivateAttr(default=None)

    @property
    def cron_expr(self) -> str | None:
        """Der Cron-Ausdruck, zu dem dieser Zeitplan ausgewertet wird."""
        return self._compiled

    @property
    def is_once(self) -> bool:
        return self.once is not None

    @model_validator(mode="after")
    def _compile(self) -> Schedule:
        forms = [
            f for f, v in (("every", self.every), ("cron", self.cron), ("once", self.once)) if v
        ]
        if len(forms) != 1:
            raise ScheduleError(
                "Ein Zeitplan braucht genau eines von 'every', 'cron' oder 'once'"
                + (f" (gefunden: {', '.join(forms)})" if forms else " (nichts davon gefunden)")
            )

        if self.once is not None:
            if self.once.tzinfo is not None:
                raise ScheduleError(
                    "'once' bitte ohne Zeitzonen-Angabe schreiben — es gilt die Zeitzone "
                    "aus settings.yaml"
                )
            return self

        self._compiled = self.cron.strip() if self.cron else self._sugar_to_cron()

        try:
            croniter(self._compiled)
        except (CroniterBadCronError, ValueError) as exc:
            source = (
                f"cron '{self.cron}'" if self.cron else f"'{self._compiled}' (aus der Kurzform)"
            )
            raise ScheduleError(f"Ungültiger Zeitplan {source}: {exc}") from exc
        return self

    def _sugar_to_cron(self) -> str:
        hour, minute = _parse_time(self.at)
        every = self.every

        if isinstance(every, str):
            key = every.strip().lower()

            if key in DAILY:
                self._reject("day", "date", context="every: day")
                return f"{minute} {hour} * * *"

            if key in MONTHLY:
                self._reject("date", context="every: month")
                day = self.day if self.day is not None else 1
                if not 1 <= day <= 31:
                    raise ScheduleError(f"'day: {day}' liegt außerhalb von 1–31")
                return f"{minute} {hour} {day} * *"

            if key in YEARLY:
                self._reject("day", context="every: year")
                if not self.date:
                    raise ScheduleError(
                        "'every: year' braucht zusätzlich 'date', z.B. date: \"10-12\""
                    )
                month, day = _parse_month_day(self.date)
                return f"{minute} {hour} {day} {month} *"

        names = [every] if isinstance(every, str) else list(every or [])
        self._reject("day", "date", context="Wochentage")
        days = []
        for name in names:
            code = WEEKDAYS.get(str(name).strip().lower())
            if code is None:
                raise ScheduleError(
                    f"'{name}' ist weder ein Wochentag noch 'day', 'month' oder 'year'"
                )
            if code not in days:
                days.append(code)
        return f"{minute} {hour} * * {','.join(days)}"

    def _reject(self, *fields: str, context: str) -> None:
        for field in fields:
            if getattr(self, field) is not None:
                raise ScheduleError(f"'{field}' ergibt bei {context} keinen Sinn")

    def describe(self) -> str:
        """Kurzbeschreibung für Chat-Nachrichten."""
        if self.once is not None:
            return self.once.strftime("einmalig am %d.%m.%Y um %H:%M")
        return f"cron: {self._compiled}"


def _to_utc(naive_local: datetime, tz: ZoneInfo) -> datetime:
    return naive_local.replace(tzinfo=tz).astimezone(UTC)


def occurrences_between(
    schedule: Schedule,
    tz: ZoneInfo,
    after: datetime,
    until: datetime,
    limit: int = 1000,
) -> list[datetime]:
    """Alle Fälligkeiten in ``(after, until]``, als UTC-Zeitpunkte.

    Beide Grenzen sind zeitzonenbehaftet. Gerechnet wird in lokaler Zeit
    (damit '18:00' auch nach der Zeitumstellung 18:00 bleibt), verglichen
    wird in UTC.
    """
    if until <= after:
        return []

    if schedule.once is not None:
        moment = _to_utc(schedule.once, tz)
        return [moment] if after < moment <= until else []

    local_after = after.astimezone(tz).replace(tzinfo=None)
    # Zwei Stunden Zuschlag, weil die Umrechnung lokal -> UTC rund um die
    # Zeitumstellung nicht streng monoton ist. Gefiltert wird ohnehin in UTC.
    local_stop = (until.astimezone(tz) + timedelta(hours=2)).replace(tzinfo=None)

    result: list[datetime] = []
    cron = croniter(_expr(schedule), local_after)
    for _ in range(limit):
        candidate = cron.get_next(datetime)
        if candidate > local_stop:
            break
        moment = _to_utc(candidate, tz)
        if after < moment <= until:
            result.append(moment)
    return result


def _expr(schedule: Schedule) -> str:
    # Alles außer 'once' wird beim Validieren zu einem Cron-Ausdruck kompiliert.
    expr = schedule.cron_expr
    if expr is None:
        raise ValueError("Einmal-Termine haben keinen Cron-Ausdruck")
    return expr


def next_occurrence(schedule: Schedule, tz: ZoneInfo, after: datetime) -> datetime | None:
    """Nächste Fälligkeit nach ``after`` (UTC), oder ``None`` bei erledigten Einmal-Terminen."""
    if schedule.once is not None:
        moment = _to_utc(schedule.once, tz)
        return moment if moment > after else None

    local_after = after.astimezone(tz).replace(tzinfo=None)
    cron = croniter(_expr(schedule), local_after)
    for _ in range(10):
        moment = _to_utc(cron.get_next(datetime), tz)
        if moment > after:
            return moment
    return None
