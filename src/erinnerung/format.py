"""Texte für den Chat. Bewusst ohne Locale-Abhängigkeit — im Container gibt es keine."""

from __future__ import annotations

from datetime import datetime, timedelta

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def clock(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def day(moment: datetime) -> str:
    return f"{WEEKDAYS[moment.weekday()]}, {moment.strftime('%d.%m.%Y')}"


def stamp(moment: datetime) -> str:
    return f"{day(moment)} um {clock(moment)}"


def relative(delta: timedelta) -> str:
    """``timedelta`` als Abstand in Worten: 'in 3 Tagen', 'vor 20 min'."""
    seconds = int(delta.total_seconds())
    past = seconds < 0
    seconds = abs(seconds)

    minutes = seconds // 60
    hours = minutes // 60

    if minutes < 1:
        return "gerade eben"
    if minutes < 60:
        value = f"{minutes} min"
    elif hours < 24:
        rest = minutes % 60
        value = f"{hours} h" + (f" {rest} min" if rest else "")
    else:
        # Auf ganze Tage runden statt abschneiden: drei Tage Vorlauf sollen
        # auch dann 'in 3 Tagen' heißen, wenn eine Minute daran fehlt.
        days = round(seconds / 86400)
        value = "1 Tag" if days == 1 else f"{days} Tagen"

    return f"vor {value}" if past else f"in {value}"
