"""Dauerangaben wie ``30m``, ``1h30m`` oder ``3d`` aus der YAML lesen."""

from __future__ import annotations

import re
from datetime import timedelta

_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}

_PART = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)


def parse_duration(value: object) -> timedelta:
    """``"1h30m"`` -> ``timedelta(hours=1, minutes=30)``.

    Erlaubte Einheiten: s, m, h, d, w. Mehrere Teile dürfen aneinandergehängt
    werden. Eine Zahl ohne Einheit wird abgelehnt — ``30`` ist zu zweideutig.
    """
    if isinstance(value, timedelta):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Dauer muss Text sein, z.B. '30m' (bekommen: {value!r})")

    text = value.strip().lower()
    if not text:
        raise ValueError("Dauer ist leer")

    parts = _PART.findall(text)
    # Nichts außer den erkannten Teilen darf übrig bleiben, sonst würde
    # '30 Minuten' klammheimlich als 30 Sekunden durchgehen.
    if not parts or _PART.sub("", text).strip():
        raise ValueError(
            f"Dauer '{value}' nicht verstanden. Erlaubt sind Angaben wie '45s', "
            "'30m', '2h', '1h30m', '3d', '1w'."
        )

    total = timedelta()
    for amount, unit in parts:
        total += timedelta(**{_UNITS[unit]: int(amount)})

    if total <= timedelta(0):
        raise ValueError(f"Dauer '{value}' muss größer als null sein")
    return total


def format_duration(delta: timedelta) -> str:
    """Menschenlesbare Näherung für Chat-Nachrichten: ``3 h 20 min``."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} s"

    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{minutes} min"
