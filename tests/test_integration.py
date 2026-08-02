"""Die mitgelieferte Beispielkonfiguration eine simulierte Woche lang laufen lassen."""

from __future__ import annotations

from pathlib import Path

import pytest

from erinnerung.config import Settings, load_config
from erinnerung.engine import Engine
from erinnerung.store import Store

from .conftest import BERLIN, FakeClock, local

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


async def durchlauf(store, notifier, config, start, tage: int):
    """In halbstündigen Schritten durch die Zeit gehen und alles bestätigen.

    Protokolliert je Zustellung, wann sie *fällig* war und wann sie tatsächlich
    im Chat landete — die beiden Zeiten fallen bei Nachtruhe auseinander.
    """
    clock = FakeClock(start)
    engine = Engine(store, config, notifier, clock=clock)
    protokoll: list[tuple[str, str, str]] = []

    def wann(moment):
        return engine.local(moment).strftime("%a %H:%M")

    await engine.tick()
    for _ in range(tage * 24 * 2):
        clock.advance(minutes=30)
        result = await engine.tick()
        for occurrence in result.notified:
            protokoll.append((occurrence.title, wann(occurrence.due_at), wann(clock.moment)))
            await engine.ack(occurrence.id)
    return protokoll


@pytest.fixture
def beispiel():
    config = load_config(CONFIG_DIR)
    # Die Beispieldatei enthält bewusst einen Termin in der Vergangenheit;
    # für den Durchlauf zählt nur, dass die Zeitzone fest steht.
    config.settings = Settings(**{**config.settings.model_dump(), "timezone": "Europe/Berlin"})
    return config


async def test_beispielkonfiguration_ist_gueltig(beispiel):
    assert beispiel.reminders, "die mitgelieferte Konfiguration sollte etwas enthalten"
    assert beispiel.tzinfo.key == "Europe/Berlin"


async def test_eine_woche_beispielkonfiguration(store, notifier, beispiel):
    # Montag, 3. August 2026, 00:00
    protokoll = await durchlauf(store, notifier, beispiel, local(2026, 8, 3, 0, 0), tage=7)
    titel = [eintrag[0] for eintrag in protokoll]

    assert titel.count("Pflanzen gießen") == 7, "täglich, jeden Tag einmal"
    assert titel.count("Tabletten nehmen") == 3, "Mo, Mi, Fr"
    assert titel.count("Biotonne rausstellen") == 1, "nur freitags"
    assert titel.count("Backup prüfen") == 1, "nur sonntags"
    assert "Kontoauszüge durchsehen" not in titel, "der 1. liegt nicht in dieser Woche"

    tabletten = [eintrag for eintrag in protokoll if eintrag[0] == "Tabletten nehmen"]
    assert {eintrag[2] for eintrag in tabletten} == {"Mon 07:30", "Wed 07:30", "Fri 07:30"}, (
        "07:30 liegt in der Nachtruhe — dieser Eintrag übersteuert sie bewusst"
    )


async def test_nachtruhe_verschiebt_ohne_uebersteuerung(store, notifier, beispiel):
    beispiel.reminders["tabletten"].respect_quiet_hours = True

    protokoll = await durchlauf(store, notifier, beispiel, local(2026, 8, 3, 0, 0), tage=2)
    tabletten = [eintrag for eintrag in protokoll if eintrag[0] == "Tabletten nehmen"]

    assert len(tabletten) == 1
    _, faellig, zugestellt = tabletten[0]
    assert faellig == "Mon 07:30", "fällig bleibt fällig ..."
    assert zugestellt == "Mon 08:00", "... zugestellt wird erst nach der Nachtruhe"


async def test_monatswechsel_und_jahrestermin(store, notifier, beispiel):
    protokoll = await durchlauf(store, notifier, beispiel, local(2026, 9, 30, 12, 0), tage=2)
    titel = [eintrag[0] for eintrag in protokoll]
    assert "Kontoauszüge durchsehen" in titel, "der 1. Oktober fällt in diesen Zeitraum"
    assert "Termin für Reifenwechsel buchen" in titel, "der einmalige Termin auch"


async def test_vorlauf_meldet_sich_drei_tage_vorher(store, notifier, beispiel):
    protokoll = await durchlauf(store, notifier, beispiel, local(2026, 10, 8, 12, 0), tage=2)
    treffer = [eintrag for eintrag in protokoll if eintrag[0].startswith("Geschenk")]
    assert treffer, "der Geburtstag am 12.10. wird am 9.10. angekündigt"
    assert treffer[0][1] == "Fri 09:00", "drei Tage vor dem Termin am Montag"
