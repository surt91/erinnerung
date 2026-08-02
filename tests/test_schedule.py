from __future__ import annotations

import pytest
from pydantic import ValidationError

from erinnerung.schedule import Schedule, next_occurrence, occurrences_between

from .conftest import BERLIN, local


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"every": "friday", "at": "18:00"}, "0 18 * * FRI"),
        ({"every": "Freitag", "at": "18:00"}, "0 18 * * FRI"),
        ({"every": ["mon", "wed", "fr"], "at": "07:30"}, "30 7 * * MON,WED,FRI"),
        ({"every": "day", "at": "19:00"}, "0 19 * * *"),
        ({"every": "täglich"}, "0 9 * * *"),  # ohne 'at' greift 09:00
        ({"every": "month", "day": 1, "at": "09:00"}, "0 9 1 * *"),
        ({"every": "month", "at": "09:00"}, "0 9 1 * *"),
        ({"every": "year", "date": "10-12", "at": "09:00"}, "0 9 12 10 *"),
        ({"every": "jahr", "date": "12.10.", "at": "09:00"}, "0 9 12 10 *"),
        ({"cron": "0 20 * * SUN"}, "0 20 * * SUN"),
    ],
)
def test_kurzform_wird_zu_cron(kwargs, expected):
    assert Schedule(**kwargs).cron_expr == expected


@pytest.mark.parametrize(
    "kwargs, hint",
    [
        ({}, "genau eines"),
        ({"every": "friday", "cron": "* * * * *"}, "genau eines"),
        ({"every": "freitag-ish", "at": "18:00"}, "weder ein Wochentag"),
        ({"every": "day", "at": "25:00"}, "außerhalb"),
        ({"every": "day", "at": "abends"}, "nicht verstanden"),
        ({"every": "year", "at": "09:00"}, "braucht zusätzlich 'date'"),
        ({"every": "friday", "day": 3}, "ergibt bei Wochentage"),
        ({"cron": "kaputt"}, "Ungültiger Zeitplan"),
        ({"once": "2026-10-01 09:00+02:00"}, "ohne Zeitzonen-Angabe"),
    ],
)
def test_unsinn_wird_abgelehnt(kwargs, hint):
    # Pydantic verpackt die ScheduleError-Meldung; der Loader packt sie
    # wieder aus, damit im Chat kein Stacktrace-Kauderwelsch landet.
    with pytest.raises(ValidationError) as info:
        Schedule(**kwargs)
    assert hint in str(info.value)


def test_faelligkeiten_im_zeitraum():
    schedule = Schedule(every="friday", at="18:00")
    moments = occurrences_between(
        schedule, BERLIN, local(2026, 8, 1), local(2026, 8, 31)
    )
    assert [m.astimezone(BERLIN).day for m in moments] == [7, 14, 21, 28]
    assert {m.astimezone(BERLIN).hour for m in moments} == {18}


def test_grenzen_sind_links_offen_rechts_geschlossen():
    schedule = Schedule(every="day", at="09:00")
    exakt = local(2026, 8, 5, 9, 0)
    assert occurrences_between(schedule, BERLIN, local(2026, 8, 5, 8, 0), exakt) == [exakt]
    assert occurrences_between(schedule, BERLIN, exakt, local(2026, 8, 5, 10, 0)) == []


def test_einmaliger_termin_nur_einmal():
    schedule = Schedule(once="2026-10-01 09:00")
    treffer = occurrences_between(schedule, BERLIN, local(2026, 9, 1), local(2026, 12, 1))
    assert len(treffer) == 1
    assert treffer[0] == local(2026, 10, 1, 9, 0)
    assert occurrences_between(schedule, BERLIN, local(2026, 10, 2), local(2027, 12, 1)) == []


def test_naechster_termin():
    schedule = Schedule(every="friday", at="18:00")
    moment = next_occurrence(schedule, BERLIN, local(2026, 8, 3, 12, 0))
    assert moment == local(2026, 8, 7, 18, 0)

    vergangen = Schedule(once="2020-01-01 09:00")
    assert next_occurrence(vergangen, BERLIN, local(2026, 8, 3)) is None


def test_zeitumstellung_haelt_die_wanduhrzeit():
    schedule = Schedule(every="day", at="09:00")
    # In der Nacht zum 25.10.2026 wird die Uhr zurückgestellt.
    moments = occurrences_between(schedule, BERLIN, local(2026, 10, 23), local(2026, 10, 27))
    lokal = [m.astimezone(BERLIN) for m in moments]
    assert [m.day for m in lokal] == [23, 24, 25, 26]
    assert {m.hour for m in lokal} == {9}
    # In UTC verschiebt sich der Zeitpunkt um eine Stunde — genau das ist gewollt.
    assert {m.hour for m in moments} == {7, 8}
