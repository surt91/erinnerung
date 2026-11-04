"""Die Tick-Logik gegen eine gefälschte Uhr — hier steckt die eigentliche Komplexität."""

from __future__ import annotations

from datetime import timedelta

from erinnerung.config import QuietHours, Reminder
from erinnerung.schedule import Schedule
from erinnerung.store import ACKED, EXPIRED, OPEN, REMOVED, SUPERSEDED

from .conftest import local, make_config

FRIDAY_1759 = local(2026, 8, 7, 17, 59)  # Freitag


def weekly(**kwargs) -> Reminder:
    kwargs.setdefault("id", "muell")
    kwargs.setdefault("title", "Biotonne rausstellen")
    kwargs.setdefault("schedule", Schedule(every="friday", at="18:00"))
    return Reminder(**kwargs)


async def test_faellig_wird_zugestellt(build_engine, notifier):
    engine, clock = build_engine(make_config(weekly()), FRIDAY_1759)

    await engine.tick()
    assert notifier.sent == []  # erster Tick erinnert nicht rückwirkend

    clock.advance(minutes=2)  # 18:01
    result = await engine.tick()

    assert notifier.titles == ["Biotonne rausstellen"]
    assert len(result.created) == 1
    assert engine.local(result.created[0].due_at).hour == 18


async def test_tick_ist_idempotent(build_engine, notifier):
    engine, clock = build_engine(make_config(weekly()), FRIDAY_1759)
    await engine.tick()
    clock.advance(minutes=2)
    await engine.tick()
    notifier.reset()

    # Mehrere Ticks kurz hintereinander: keine zweite Instanz, kein zweites Nerven.
    for _ in range(3):
        clock.advance(seconds=30)
        result = await engine.tick()
        assert result.created == []
    assert notifier.sent == []


async def test_nervt_erst_nach_intervall(build_engine, notifier):
    reminder = weekly(nag={"interval": "30m"})
    engine, clock = build_engine(make_config(reminder), FRIDAY_1759)
    await engine.tick()
    clock.advance(minutes=2)
    await engine.tick()
    assert len(notifier.sent) == 1

    clock.advance(minutes=29)
    await engine.tick()
    assert len(notifier.sent) == 1, "vor Ablauf des Intervalls wird nicht erneut genervt"

    clock.advance(minutes=2)
    await engine.tick()
    assert len(notifier.sent) == 2


async def test_bestaetigen_beendet_das_nerven(build_engine, notifier, store):
    engine, clock = build_engine(make_config(weekly(nag={"interval": "10m"})), FRIDAY_1759)
    await engine.tick()
    clock.advance(minutes=2)
    result = await engine.tick()

    acked = await engine.ack(result.created[0].id)
    assert acked.status == ACKED

    clock.advance(hours=5)
    await engine.tick()
    assert len(notifier.sent) == 1
    assert store.open_occurrences() == []


async def test_schlummern_verschiebt(build_engine, notifier):
    config = make_config(weekly(nag={"interval": "10m"}), quiet_hours=None)
    engine, clock = build_engine(config, FRIDAY_1759)
    await engine.tick()
    clock.advance(minutes=2)
    result = await engine.tick()

    snoozed = engine.snooze(result.created[0].id, "3h")
    assert snoozed is not None
    _, until = snoozed
    assert until - clock.moment == timedelta(hours=3)

    clock.advance(hours=2)
    await engine.tick()
    assert len(notifier.sent) == 1, "während der Schlummerzeit ist Ruhe"

    clock.advance(hours=2)
    await engine.tick()
    assert len(notifier.sent) == 2


async def test_ruhezeiten_verschieben_auf_den_morgen(build_engine, notifier):
    reminder = weekly(schedule=Schedule(every="day", at="23:30"), nag={"interval": "15m"})
    config = make_config(reminder, quiet_hours=QuietHours(**{"from": "22:00", "to": "08:00"}))
    engine, clock = build_engine(config, local(2026, 8, 7, 23, 29))

    await engine.tick()
    clock.advance(minutes=2)  # 23:31, fällig — aber Nachtruhe
    result = await engine.tick()
    assert len(result.created) == 1
    assert notifier.sent == []

    clock.set(local(2026, 8, 8, 7, 59))
    await engine.tick()
    assert notifier.sent == []

    clock.set(local(2026, 8, 8, 8, 1))
    await engine.tick()
    assert len(notifier.sent) == 1, "nach der Nachtruhe meldet sich die offene Erinnerung"


async def test_ruhezeiten_koennen_uebersteuert_werden(build_engine, notifier):
    reminder = weekly(schedule=Schedule(every="day", at="23:30"), respect_quiet_hours=False)
    engine, clock = build_engine(make_config(reminder), local(2026, 8, 7, 23, 29))
    await engine.tick()
    clock.advance(minutes=2)
    await engine.tick()
    assert len(notifier.sent) == 1


async def test_overlap_replace_laesst_nur_die_neueste_offen(build_engine, notifier, store):
    config = make_config(weekly(on_overlap="replace"), catchup_window="14d")
    engine, clock = build_engine(config, FRIDAY_1759)
    await engine.tick()

    clock.advance(days=7, minutes=2)  # zwei Freitage später, nie bestätigt
    result = await engine.tick()

    assert len(result.created) == 2
    assert len(result.superseded) == 1
    assert len(store.open_occurrences()) == 1
    assert notifier.withdrawn, "die überholte Nachricht wird aus dem Chat genommen"


async def test_overlap_stack_sammelt(build_engine, store):
    config = make_config(weekly(on_overlap="stack"), catchup_window="14d")
    engine, clock = build_engine(config, FRIDAY_1759)
    await engine.tick()

    clock.advance(days=7, minutes=2)
    result = await engine.tick()

    assert len(result.superseded) == 0
    assert len(store.open_occurrences()) == 2


async def test_downtime_kulanzfenster(build_engine, notifier, store):
    """Nach drei Tagen Ausfall: das Frische kommt nach, das Alte verfällt still."""
    reminder = weekly(schedule=Schedule(every="day", at="09:00"), on_overlap="stack")
    config = make_config(reminder, catchup_window="24h")
    engine, clock = build_engine(config, local(2026, 8, 3, 8, 59))
    await engine.tick()

    clock.set(local(2026, 8, 6, 10, 0))  # drei Tage später wieder da
    result = await engine.tick()

    assert len(result.expired) == 3, "3., 4. und 5. August liegen außerhalb der Kulanz"
    assert len(result.created) == 1, "der heutige 9-Uhr-Termin kommt nach"
    assert [o.status for o in store.open_occurrences()] == [OPEN]
    assert len(notifier.sent) == 1
    assert {o.status for o in [store.get(e.id) for e in result.expired]} == {EXPIRED}


async def test_vorlauf_zieht_die_erinnerung_nach_vorn(build_engine, notifier):
    reminder = Reminder(
        id="geschenk",
        title="Geschenk besorgen",
        schedule=Schedule(every="year", date="10-12", at="09:00"),
        lead="3d",
    )
    engine, clock = build_engine(make_config(reminder), local(2026, 10, 9, 8, 59))

    await engine.tick()
    clock.advance(minutes=2)  # 9. Oktober, 09:01 — drei Tage vor dem Termin
    result = await engine.tick()

    assert len(result.created) == 1
    occurrence = result.created[0]
    assert engine.local(occurrence.due_at).day == 9
    assert engine.local(occurrence.event_at).day == 12
    assert occurrence.has_lead


async def test_einmaliger_termin(build_engine, notifier, store):
    reminder = Reminder(
        id="reifen",
        title="Reifenwechsel buchen",
        schedule=Schedule(once="2026-10-01 09:00"),
    )
    engine, clock = build_engine(make_config(reminder), local(2026, 10, 1, 8, 59))
    await engine.tick()
    clock.advance(minutes=2)
    result = await engine.tick()
    assert len(result.created) == 1

    await engine.ack(result.created[0].id)
    clock.advance(days=400)
    await engine.tick()
    assert len(notifier.sent) == 1, "ein einmaliger Termin kommt nicht wieder"


async def test_aus_der_yaml_geloescht_schliesst_offene_instanz(build_engine, notifier, store):
    engine, clock = build_engine(make_config(weekly()), FRIDAY_1759)
    await engine.tick()
    clock.advance(minutes=2)
    result = await engine.tick()
    occurrence_id = result.created[0].id

    engine.config = make_config()  # Eintrag aus der Konfiguration entfernt
    clock.advance(hours=2)
    tick = await engine.tick()

    assert len(tick.removed) == 1
    assert store.get(occurrence_id).status == REMOVED
    assert len(notifier.sent) == 1


async def test_sommerzeit_haelt_die_wanduhrzeit(build_engine, notifier):
    """Über die Zeitumstellung hinweg bleibt 09:00 auch wirklich 09:00."""
    reminder = weekly(schedule=Schedule(every="day", at="09:00"), on_overlap="stack")
    engine, clock = build_engine(make_config(reminder), local(2026, 10, 24, 8, 59))
    await engine.tick()

    clock.set(local(2026, 10, 26, 12, 0))  # Umstellung war in der Nacht zum 25.
    result = await engine.tick()

    stunden = {
        engine.local(occurrence.due_at).hour for occurrence in result.created + result.expired
    }
    assert stunden == {9}


async def test_superseded_bleibt_ohne_nachricht(build_engine, notifier, store):
    engine, clock = build_engine(make_config(weekly(on_overlap="replace")), FRIDAY_1759)
    await engine.tick()
    clock.advance(minutes=2)
    first = (await engine.tick()).created[0]

    clock.advance(days=7)
    await engine.tick()

    assert store.get(first.id).status == SUPERSEDED
    assert await engine.ack(first.id) is None, "Überholtes lässt sich nicht mehr bestätigen"
