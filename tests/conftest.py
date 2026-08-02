from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from erinnerung.config import LoadedConfig, Reminder, Settings
from erinnerung.engine import Engine
from erinnerung.store import Occurrence, Store

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


def local(year, month, day, hour=0, minute=0, tz=BERLIN) -> datetime:
    """Lokale Wanduhrzeit als UTC-Zeitpunkt — so denkt man beim Testen."""
    return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(UTC)


class FakeClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> datetime:
        self.moment += timedelta(**kwargs)
        return self.moment

    def set(self, moment: datetime) -> datetime:
        self.moment = moment
        return self.moment


class FakeNotifier:
    """Merkt sich, was zugestellt und was wieder eingesammelt wurde."""

    def __init__(self) -> None:
        self.sent: list[Occurrence] = []
        self.withdrawn: list[Occurrence] = []
        self._next_message_id = 100

    async def send(self, occurrence: Occurrence, reminder: Reminder | None) -> int:
        self.sent.append(occurrence)
        self._next_message_id += 1
        return self._next_message_id

    async def withdraw(self, occurrence: Occurrence) -> None:
        self.withdrawn.append(occurrence)

    @property
    def titles(self) -> list[str]:
        return [occurrence.title for occurrence in self.sent]

    def reset(self) -> None:
        self.sent.clear()
        self.withdrawn.clear()


def make_config(*reminders: Reminder, **settings_kwargs) -> LoadedConfig:
    settings_kwargs.setdefault("timezone", "Europe/Berlin")
    return LoadedConfig(
        settings=Settings(**settings_kwargs),
        reminders={reminder.id: reminder for reminder in reminders},
    )


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "state.db")
    yield store
    store.close()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def build_engine(store, notifier):
    """Fabrik für eine Engine mit kontrollierter Uhr."""

    def factory(config: LoadedConfig, now: datetime) -> tuple[Engine, FakeClock]:
        clock = FakeClock(now)
        return Engine(store, config, notifier, clock=clock), clock

    return factory
