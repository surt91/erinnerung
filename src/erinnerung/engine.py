"""Der Taktgeber: was ist fällig geworden, woran muss erinnert werden.

Alles läuft über einen einzigen, wiederholbaren Durchlauf (:meth:`Engine.tick`).
Er ist bewusst idempotent — zweimal derselbe Zeitraum erzeugt keine doppelten
Erinnerungen — und braucht deshalb keinen Scheduler, der sich Dinge merkt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from .config import TOMORROW_ALIASES, LoadedConfig, Reminder
from .duration import parse_duration
from .schedule import occurrences_between
from .store import ACKED, EXPIRED, OPEN, REMOVED, SKIPPED, Occurrence, Store

log = logging.getLogger(__name__)

UTC = timezone.utc

LAST_TICK = "last_tick_at"
HEARTBEAT = "heartbeat_at"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Notifier(Protocol):
    """Zustellung nach draußen. Die Engine kennt Telegram nicht."""

    async def send(self, occurrence: Occurrence, reminder: Reminder | None) -> int | None:
        """Erinnerung zustellen, vorherige Nachricht dabei aufräumen. Gibt die message_id zurück."""

    async def withdraw(self, occurrence: Occurrence) -> None:
        """Eine nicht mehr aktuelle Nachricht aus dem Chat entfernen."""


class Archiver(Protocol):
    async def archive(self, reminder_id: str) -> bool:
        """Erledigten Einmal-Termin aus der YAML ins Archiv verschieben."""


@dataclass
class TickResult:
    created: list[Occurrence] = field(default_factory=list)
    expired: list[Occurrence] = field(default_factory=list)
    notified: list[Occurrence] = field(default_factory=list)
    superseded: list[Occurrence] = field(default_factory=list)
    removed: list[Occurrence] = field(default_factory=list)
    quiet: bool = False


def resolve_snooze(spec: str, now: datetime, config: LoadedConfig) -> datetime:
    """``"1h"`` oder ``"tomorrow"`` in einen Zeitpunkt übersetzen."""
    key = spec.strip().lower()
    if key in TOMORROW_ALIASES:
        tz = config.tzinfo
        quiet = config.settings.quiet_hours
        wake = quiet.end if quiet else time(8, 0)
        local = now.astimezone(tz)
        tomorrow = (local + timedelta(days=1)).date()
        return datetime.combine(tomorrow, wake).replace(tzinfo=tz).astimezone(UTC)
    return now + parse_duration(spec)


def snooze_label(spec: str) -> str:
    return "morgen früh" if spec.strip().lower() in TOMORROW_ALIASES else spec


class Engine:
    def __init__(
        self,
        store: Store,
        config: LoadedConfig,
        notifier: Notifier,
        *,
        archiver: Archiver | None = None,
        clock=utcnow,
    ) -> None:
        self.store = store
        self.config = config
        self.notifier = notifier
        self.archiver = archiver
        self.clock = clock

    # -- Takt --------------------------------------------------------------

    async def tick(self) -> TickResult:
        now = self.clock()
        result = TickResult()

        last_tick = self.store.get_time(LAST_TICK)
        if last_tick is None:
            # Erster Start: nicht rückwirkend erinnern, sondern hier beginnen.
            last_tick = now

        self._materialize(last_tick, now, result)
        await self._settle_superseded(result)
        await self._nag(now, result)

        self.store.set_time(LAST_TICK, now)
        self.store.set_time(HEARTBEAT, now)
        return result

    def _materialize(self, last_tick: datetime, now: datetime, result: TickResult) -> None:
        """Fälligkeiten des Zeitraums in die Datenbank schreiben."""
        settings = self.config.settings
        tz = self.config.tzinfo
        cutoff = now - settings.catchup_window

        for reminder in self.config.reminders.values():
            lead = reminder.lead_delta
            # Gesucht sind Termine, deren *Erinnerungszeitpunkt* ins Fenster fällt.
            # Bei Vorlauf liegt der Termin selbst entsprechend später.
            events = occurrences_between(
                reminder.schedule, tz, last_tick + lead, now + lead
            )
            for event_at in events:
                due_at = event_at - lead
                overdue = due_at < cutoff
                occurrence = self.store.add_occurrence(
                    reminder_id=reminder.id,
                    title=reminder.title,
                    event_at=event_at,
                    due_at=due_at,
                    status=EXPIRED if overdue else OPEN,
                    now=now,
                )
                if occurrence is None:
                    continue  # gab es schon — der Tick darf sich wiederholen

                if overdue:
                    result.expired.append(occurrence)
                    log.info("Verfallen (außerhalb Kulanzfenster): %s %s", reminder.id, due_at)
                    continue

                result.created.append(occurrence)
                if reminder.on_overlap == "replace":
                    result.superseded.extend(
                        self.store.supersede_others(reminder.id, occurrence.id, now)
                    )

    async def _settle_superseded(self, result: TickResult) -> None:
        for occurrence in result.superseded:
            await self.notifier.withdraw(occurrence)

    async def _nag(self, now: datetime, result: TickResult) -> None:
        settings = self.config.settings
        local_now = now.astimezone(self.config.tzinfo).time()
        quiet_now = bool(settings.quiet_hours and settings.quiet_hours.contains(local_now))
        result.quiet = quiet_now

        for occurrence in self.store.open_occurrences():
            reminder = self.config.reminders.get(occurrence.reminder_id)

            if reminder is None:
                # Aus der YAML gelöscht: dann will man auch nicht mehr erinnert werden.
                self.store.resolve(occurrence.id, REMOVED, now)
                await self.notifier.withdraw(occurrence)
                result.removed.append(occurrence)
                log.info("Erinnerung '%s' nicht mehr konfiguriert, offene Instanz geschlossen",
                         occurrence.reminder_id)
                continue

            if occurrence.due_at > now:
                continue
            if occurrence.snoozed_until and occurrence.snoozed_until > now:
                continue
            if quiet_now and reminder.respect_quiet_hours:
                continue

            interval = reminder.nag_interval(settings)
            if occurrence.last_nag_at and now - occurrence.last_nag_at < interval:
                continue

            message_id = await self.notifier.send(occurrence, reminder)
            self.store.record_nag(occurrence.id, now, message_id)
            result.notified.append(occurrence)

    # -- Reaktionen aus dem Chat -------------------------------------------

    async def ack(self, occurrence_id: int) -> Occurrence | None:
        return await self._resolve(occurrence_id, ACKED)

    async def skip(self, occurrence_id: int) -> Occurrence | None:
        return await self._resolve(occurrence_id, SKIPPED)

    async def _resolve(self, occurrence_id: int, status: str) -> Occurrence | None:
        occurrence = self.store.get(occurrence_id)
        if occurrence is None or occurrence.status != OPEN:
            return None

        self.store.resolve(occurrence_id, status, self.clock())
        reminder = self.config.reminders.get(occurrence.reminder_id)

        # Einmalige Termine sind mit der Bestätigung erledigt und wandern
        # aus der Konfiguration ins Archiv.
        if self.archiver and reminder and reminder.schedule.is_once:
            try:
                await self.archiver.archive(reminder.id)
            except Exception:  # Archivieren darf das Quittieren nie kaputtmachen
                log.exception("Archivieren von '%s' fehlgeschlagen", reminder.id)

        return self.store.get(occurrence_id)

    def snooze(self, occurrence_id: int, spec: str) -> tuple[Occurrence | None, datetime | None]:
        occurrence = self.store.get(occurrence_id)
        if occurrence is None or occurrence.status != OPEN:
            return None, None
        until = resolve_snooze(spec, self.clock(), self.config)
        self.store.snooze(occurrence_id, until)
        return self.store.get(occurrence_id), until

    # -- Auskunft -----------------------------------------------------------

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.config.tzinfo)

    @property
    def tzinfo(self) -> ZoneInfo:
        return self.config.tzinfo
