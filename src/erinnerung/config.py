"""Die YAML-Dateien: Modelle, Validierung, Laden eines ganzen Ordners.

Die Dateien sind die *Definition* der Erinnerungen. Was davon offen, erledigt
oder geschlummert ist, steht in der Datenbank (siehe :mod:`erinnerung.store`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

from .duration import parse_duration
from .schedule import Schedule, ScheduleError

Duration = Annotated[timedelta, BeforeValidator(parse_duration)]

SETTINGS_FILE = "settings.yaml"
ARCHIVE_DIR = "archive"
DEFAULT_TIMEZONE = "Europe/Berlin"

# Schlummern bis zum nächsten Morgen — keine Dauer, sondern ein Zeitpunkt.
TOMORROW_ALIASES = {"tomorrow", "morgen"}


class ConfigError(Exception):
    """Die Konfiguration ist kaputt — mit einer Meldung, die man dem Chat schicken kann."""


class QuietHours(BaseModel):
    """Zeitfenster, in dem nicht genervt wird. Darf über Mitternacht gehen."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start: time = Field(default=time(22, 0), alias="from")
    end: time = Field(default=time(8, 0), alias="to")

    def contains(self, moment: time) -> bool:
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end  # über Mitternacht


class NagConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval: Duration = timedelta(minutes=60)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = None
    quiet_hours: QuietHours | None = Field(default_factory=QuietHours)
    nag: NagConfig = Field(default_factory=NagConfig)
    catchup_window: Duration = timedelta(hours=24)
    snooze_options: list[str] = Field(default_factory=lambda: ["1h", "3h", "tomorrow"])
    tick_interval: Duration = timedelta(seconds=30)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"Unbekannte Zeitzone '{value}'") from exc
        return value

    @field_validator("snooze_options")
    @classmethod
    def _usable_snooze_options(cls, options: list[str]) -> list[str]:
        # Lieber beim Start meckern als später unter einem Knopf, der nicht tut.
        for option in options:
            if option.strip().lower() not in TOMORROW_ALIASES:
                parse_duration(option)
        return options

    @property
    def tzinfo(self) -> ZoneInfo:
        """Zeitzone aus der YAML, sonst aus ``TZ``, sonst Europe/Berlin."""
        name = self.timezone or os.environ.get("TZ") or DEFAULT_TIMEZONE
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(DEFAULT_TIMEZONE)


class Reminder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    text: str | None = None
    schedule: Schedule
    nag: NagConfig | None = None
    lead: Duration | None = None
    # Was passiert, wenn erneut fällig wird, was noch offen ist:
    # 'replace' lässt nur die neueste Instanz offen, 'stack' sammelt sie.
    on_overlap: Literal["replace", "stack"] = "replace"
    respect_quiet_hours: bool = True

    @field_validator("id")
    @classmethod
    def _usable_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(char.isspace() for char in cleaned):
            raise ValueError("id darf nicht leer sein und keine Leerzeichen enthalten")
        return cleaned

    def nag_interval(self, settings: Settings) -> timedelta:
        return self.nag.interval if self.nag else settings.nag.interval

    @property
    def lead_delta(self) -> timedelta:
        return self.lead or timedelta(0)


@dataclass
class LoadedConfig:
    """Eine eingelesene Konfiguration samt Herkunft jeder Erinnerung."""

    settings: Settings = field(default_factory=Settings)
    reminders: dict[str, Reminder] = field(default_factory=dict)
    sources: dict[str, Path] = field(default_factory=dict)

    @property
    def tzinfo(self) -> ZoneInfo:
        return self.settings.tzinfo


def _read_yaml(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: YAML lässt sich nicht lesen — {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path.name}: Datei nicht lesbar — {exc}") from exc


def _format_validation_error(exc: ValidationError, where: str) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        # Pydantic stellt eigenen Meldungen 'Value error, ' voran — im Chat
        # will das niemand lesen.
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {location + ': ' if location else ''}{message}")
    return f"{where}:\n" + "\n".join(lines)


def load_settings(config_dir: Path) -> Settings:
    path = config_dir / SETTINGS_FILE
    if not path.is_file():
        return Settings()

    raw = _read_yaml(path) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{SETTINGS_FILE}: erwartet wird eine Zuordnung von Einstellungen")
    try:
        return Settings(**raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, SETTINGS_FILE)) from exc
    except ValueError as exc:
        raise ConfigError(f"{SETTINGS_FILE}: {exc}") from exc


def reminder_files(config_dir: Path) -> list[Path]:
    """Alle Erinnerungs-Dateien im Ordner — ohne settings.yaml, ohne Archiv."""
    return sorted(
        path
        for path in config_dir.iterdir()
        if path.is_file() and path.suffix in {".yaml", ".yml"} and path.name != SETTINGS_FILE
    )


def load_config(config_dir: Path) -> LoadedConfig:
    """Ordner einlesen. Wirft :class:`ConfigError` mit einer erklärenden Meldung."""
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        raise ConfigError(f"Konfigurationsordner '{config_dir}' existiert nicht")

    config = LoadedConfig(settings=load_settings(config_dir))

    for path in reminder_files(config_dir):
        raw = _read_yaml(path)
        if raw is None:
            continue
        if not isinstance(raw, dict) or "reminders" not in raw:
            raise ConfigError(
                f"{path.name}: erwartet wird ein Schlüssel 'reminders' mit einer Liste darunter"
            )
        entries = raw["reminders"]
        if not isinstance(entries, list):
            raise ConfigError(f"{path.name}: 'reminders' muss eine Liste sein")

        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise ConfigError(f"{path.name}: Eintrag {index} ist keine Zuordnung")
            where = f"{path.name}, Eintrag {index}" + (
                f" ({entry['id']})" if isinstance(entry.get("id"), str) else ""
            )
            try:
                reminder = Reminder(**entry)
            except ValidationError as exc:
                raise ConfigError(_format_validation_error(exc, where)) from exc
            except (ScheduleError, ValueError) as exc:
                raise ConfigError(f"{where}: {exc}") from exc

            if reminder.id in config.reminders:
                first = config.sources[reminder.id].name
                raise ConfigError(
                    f"Die id '{reminder.id}' kommt doppelt vor: in {first} und in {path.name}"
                )
            config.reminders[reminder.id] = reminder
            config.sources[reminder.id] = path

    return config
