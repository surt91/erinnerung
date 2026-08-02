from __future__ import annotations

from datetime import time, timedelta

import pytest

from erinnerung.config import ConfigError, load_config


def write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_ordner_mit_mehreren_dateien(tmp_path):
    write(
        tmp_path,
        "haushalt.yaml",
        """
        reminders:
          - id: muell
            title: "Biotonne rausstellen"
            schedule: { every: friday, at: "18:00" }
            nag: { interval: 30m }
        """,
    )
    write(
        tmp_path,
        "termine.yml",
        """
        reminders:
          - id: reifen
            title: "Reifenwechsel"
            schedule: { once: 2026-10-01 09:00 }
            lead: 3d
        """,
    )

    config = load_config(tmp_path)

    assert set(config.reminders) == {"muell", "reifen"}
    assert config.reminders["muell"].nag.interval == timedelta(minutes=30)
    assert config.reminders["reifen"].lead_delta == timedelta(days=3)
    assert config.sources["reifen"].name == "termine.yml"
    # Defaults, wenn keine settings.yaml daliegt
    assert config.settings.nag.interval == timedelta(hours=1)
    assert config.settings.quiet_hours.start == time(22, 0)
    assert config.tzinfo.key == "Europe/Berlin"


def test_settings_werden_gelesen(tmp_path):
    write(
        tmp_path,
        "settings.yaml",
        """
        timezone: Europe/Lisbon
        quiet_hours: { from: "23:00", to: "06:30" }
        nag: { interval: 2h }
        catchup_window: 3d
        snooze_options: [15m, tomorrow]
        """,
    )
    config = load_config(tmp_path)

    assert config.tzinfo.key == "Europe/Lisbon"
    assert config.settings.quiet_hours.end == time(6, 30)
    assert config.settings.catchup_window == timedelta(days=3)
    assert config.settings.snooze_options == ["15m", "tomorrow"]


def test_ruhezeit_ueber_mitternacht(tmp_path):
    config = load_config(tmp_path)
    quiet = config.settings.quiet_hours
    assert quiet.contains(time(23, 30))
    assert quiet.contains(time(3, 0))
    assert not quiet.contains(time(21, 59))
    assert not quiet.contains(time(8, 0))


def test_doppelte_id_nennt_beide_dateien(tmp_path):
    für_beide = """
        reminders:
          - id: muell
            title: "Tonne"
            schedule: { every: friday }
        """
    write(tmp_path, "a.yaml", für_beide)
    write(tmp_path, "b.yaml", für_beide)

    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert "a.yaml" in str(info.value) and "b.yaml" in str(info.value)


@pytest.mark.parametrize(
    "content, hint",
    [
        ("reminders: [{id: x, title: t}]", "schedule"),
        ("reminders: [{id: x, schedule: {every: friday}}]", "title"),
        ("reminders: [{id: x, title: t, schedule: {every: nimmerlein}}]", "weder ein Wochentag"),
        ("reminders: [{id: x, title: t, schedule: {every: day}, nag: {interval: bald}}]", "Dauer"),
        ("reminders: [{id: x, title: t, schedule: {every: day}, tipfehler: 1}]", "tipfehler"),
        ("erinnerungen: []", "'reminders'"),
        ("reminders: {}", "Liste"),
        ("reminders: [\n  - kaputt", "YAML"),
    ],
)
def test_fehler_werden_erklaert(tmp_path, content, hint):
    write(tmp_path, "test.yaml", content)
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    message = str(info.value)
    assert hint in message
    assert "test.yaml" in message
    assert "Value error" not in message, "Pydantic-Jargon gehört nicht in den Chat"


def test_archivordner_wird_nicht_mitgeladen(tmp_path):
    write(
        tmp_path,
        "aktuell.yaml",
        "reminders: [{id: a, title: Aktuell, schedule: {every: day}}]",
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "2025.yaml").write_text(
        "reminders: [{id: alt, title: Alt, schedule: {once: 2025-01-01 09:00}}]",
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    assert set(config.reminders) == {"a"}


def test_zeitzone_aus_der_umgebung(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Lisbon")
    assert load_config(tmp_path).tzinfo.key == "Europe/Lisbon"

    write(tmp_path, "settings.yaml", "timezone: Europe/Berlin")
    assert load_config(tmp_path).tzinfo.key == "Europe/Berlin", "settings.yaml sticht TZ"


def test_unbrauchbare_schlummerdauer_faellt_beim_start_auf(tmp_path):
    write(tmp_path, "settings.yaml", "snooze_options: [1h, spaeter]")
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert "spaeter" in str(info.value)


def test_unbekannte_zeitzone(tmp_path):
    write(tmp_path, "settings.yaml", "timezone: Mittelerde/Auenland")
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert "Unbekannte Zeitzone" in str(info.value)
