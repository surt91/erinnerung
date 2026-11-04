"""Die Chat-Zeile hinter /neu: Zeitangabe lesen, id bilden, YAML schreiben."""

from __future__ import annotations

from datetime import datetime

import pytest

from erinnerung.compose import ComposeError, make_id, parse, write
from erinnerung.config import INBOX_FILE, load_config

# Ein Montag, damit 'morgen' und die Jahreslogik nachvollziehbar bleiben.
NOW = datetime(2026, 8, 3, 12, 0)


def entry(line: str, **kwargs):
    return parse(line, now=NOW, **kwargs).entry


def schedule(line: str) -> dict:
    return dict(entry(line)["schedule"])


# -- Wiederkehrendes -------------------------------------------------------


def test_wochentag_mit_uhrzeit():
    draft = parse("freitag 18:00 Biotonne rausstellen", now=NOW)
    assert dict(draft.entry["schedule"]) == {"every": "fri", "at": "18:00"}
    assert draft.entry["title"] == "Biotonne rausstellen"
    assert draft.description == "jeden Freitag um 18:00"


def test_fuellwoerter_stoeren_nicht():
    assert schedule("jeden freitag um 18:00 Biotonne") == schedule("freitag 18:00 Biotonne")


def test_mehrere_wochentage():
    assert schedule("mo, mi und fr 7:30 Tabletten") == {
        "every": ["mon", "wed", "fri"],
        "at": "07:30",
    }


def test_taeglich():
    assert schedule("täglich 19:00 Pflanzen gießen") == {"every": "day", "at": "19:00"}


def test_monatlich_mit_und_ohne_tag():
    assert schedule("monatlich Kontoauszüge") == {"every": "month", "day": 1, "at": "09:00"}
    assert schedule("monatlich am 15. 9:00 Miete") == {
        "every": "month",
        "day": 15,
        "at": "09:00",
    }


def test_jaehrlich_braucht_ein_datum():
    assert schedule("jährlich 12.10. Geschenk") == {
        "every": "year",
        "date": "10-12",
        "at": "09:00",
    }
    with pytest.raises(ComposeError, match="Datum"):
        parse("jährlich Geschenk besorgen", now=NOW)


def test_cron_als_notausgang():
    draft = parse("cron 0 20 * * SUN Backup prüfen", now=NOW)
    assert dict(draft.entry["schedule"]) == {"cron": "0 20 * * SUN"}
    assert draft.entry["title"] == "Backup prüfen"


# -- Einmaliges ------------------------------------------------------------


def test_datum_ohne_jahr_meint_das_naechste_vorkommen():
    assert schedule("1.10. 9:00 Reifenwechsel")["once"] == datetime(2026, 10, 1, 9, 0)
    assert schedule("1.3. Steuer machen")["once"] == datetime(2027, 3, 1, 9, 0)


def test_datum_mit_jahr_und_monatsnamen():
    assert schedule("1.10.2027 9:00 X")["once"] == datetime(2027, 10, 1, 9, 0)
    assert schedule("12. oktober 9:00 X")["once"] == datetime(2026, 10, 12, 9, 0)
    assert schedule("2026-12-24 18:00 Bescherung")["once"] == datetime(2026, 12, 24, 18, 0)


def test_morgen_und_uebermorgen():
    assert schedule("morgen 8:00 Paket abholen")["once"] == datetime(2026, 8, 4, 8, 0)
    assert schedule("übermorgen Arzt anrufen")["once"] == datetime(2026, 8, 5, 9, 0)


def test_relative_dauer():
    assert schedule("in 2h Wäsche aufhängen")["once"] == datetime(2026, 8, 3, 14, 0)


def test_blosse_uhrzeit_ist_heute_sonst_morgen():
    assert schedule("20:00 Müll rausbringen")["once"] == datetime(2026, 8, 3, 20, 0)
    assert schedule("8 uhr Frühsport")["once"] == datetime(2026, 8, 4, 8, 0)


def test_vergangenes_wird_abgelehnt():
    with pytest.raises(ComposeError, match="vorbei"):
        parse("heute 8:00 Zu spät", now=NOW)


# -- Titel und Beschreibung ------------------------------------------------


def test_titel_behaelt_seine_ersten_woerter():
    """Ein Füllwort alleine darf den Titelanfang nicht verschieben."""
    assert entry("freitag 18:00 Am Wochenende einkaufen")["title"] == "Am Wochenende einkaufen"
    assert entry("morgen in der Stadt Brot kaufen")["title"] == "in der Stadt Brot kaufen"


def test_senkrechter_strich_trennt_explizit():
    draft = parse("freitag 18:00 | Montagsmeeting vorbereiten", now=NOW)
    assert draft.entry["title"] == "Montagsmeeting vorbereiten"
    assert dict(draft.entry["schedule"]) == {"every": "fri", "at": "18:00"}


def test_zweite_zeile_wird_zur_beschreibung():
    draft = parse("täglich 19:00 Pflanzen gießen\nAuch die auf dem Balkon.", now=NOW)
    assert draft.entry["text"] == "Auch die auf dem Balkon."
    assert draft.reminder.text == "Auch die auf dem Balkon."


@pytest.mark.parametrize(
    "line, meldung",
    [
        ("Biotonne rausstellen", "Zeitangabe"),
        ("freitag 18:00", "Titel"),
        ("31.2. Unsinn", "gibt es nicht"),
        ("freitag täglich 8:00 X", "widerspricht"),
        ("25:00 Kaputt", "00:00"),
    ],
)
def test_unverstaendliches_wird_erklaert(line, meldung):
    with pytest.raises(ComposeError, match=meldung):
        parse(line, now=NOW)


# -- id --------------------------------------------------------------------


def test_id_aus_dem_titel():
    assert make_id("Biotonne rausstellen") == "biotonne-rausstellen"
    assert make_id("Öl für die Straßenbahn nachfüllen") == "oel-fuer-die-strassenbahn"
    assert make_id("!!!") == "erinnerung"


def test_id_weicht_belegten_aus():
    assert make_id("Biotonne rausstellen", {"biotonne-rausstellen"}) == "biotonne-rausstellen-2"
    taken = {"biotonne-rausstellen", "biotonne-rausstellen-2"}
    assert make_id("Biotonne rausstellen", taken) == "biotonne-rausstellen-3"


def test_gleicher_titel_zweimal_kollidiert_nicht(tmp_path):
    erst = parse("freitag 18:00 Biotonne", now=NOW)
    write(erst, tmp_path, now=NOW)
    zweit = parse("montag 8:00 Biotonne", now=NOW, taken=load_config(tmp_path).reminders)
    write(zweit, tmp_path, now=NOW)

    assert set(load_config(tmp_path).reminders) == {"biotonne", "biotonne-2"}


# -- Schreiben -------------------------------------------------------------


def test_geschriebenes_laesst_sich_wieder_laden(tmp_path):
    for line in [
        "freitag 18:00 Biotonne rausstellen\nDeckel zu.",
        "1.10. 9:00 Reifenwechsel buchen",
        "mo, mi, fr 7:30 Tabletten nehmen",
    ]:
        draft = parse(line, now=NOW, taken=load_config(tmp_path).reminders)
        assert write(draft, tmp_path, now=NOW) == tmp_path / INBOX_FILE

    config = load_config(tmp_path)
    assert set(config.reminders) == {
        "biotonne-rausstellen",
        "reifenwechsel-buchen",
        "tabletten-nehmen",
    }
    # Der YAML-Loader liest 18:00 als Text, nicht als Zahl — die Anführungszeichen
    # im geschriebenen at: sind also nicht bloß Kosmetik.
    assert config.reminders["biotonne-rausstellen"].schedule.cron_expr == "0 18 * * FRI"
    assert config.reminders["biotonne-rausstellen"].text == "Deckel zu."
    assert config.reminders["reifenwechsel-buchen"].schedule.once == datetime(2026, 10, 1, 9, 0)


def test_datei_bekommt_kopf_und_datumskommentar(tmp_path):
    write(parse("freitag 18:00 Biotonne", now=NOW), tmp_path, now=NOW)
    inhalt = (tmp_path / INBOX_FILE).read_text(encoding="utf-8")

    assert inhalt.startswith("# Per /neu")
    assert "# angelegt am 03.08.2026 um 12:00" in inhalt


def test_handgeschriebenes_bleibt_unangetastet(tmp_path):
    (tmp_path / INBOX_FILE).write_text(
        "# von Hand\nreminders:\n"
        '  - id: alt\n    title: "Alter Eintrag"   # bleibt\n'
        '    schedule:\n      every: day\n      at: "07:00"\n',
        encoding="utf-8",
    )

    write(parse("freitag 18:00 Neuer Eintrag", now=NOW), tmp_path, now=NOW)

    inhalt = (tmp_path / INBOX_FILE).read_text(encoding="utf-8")
    assert "# von Hand" in inhalt
    assert "# bleibt" in inhalt
    assert 'at: "07:00"' in inhalt
    assert set(load_config(tmp_path).reminders) == {"alt", "neuer-eintrag"}


def test_kein_schreiben_ohne_gueltigen_entwurf(tmp_path):
    with pytest.raises(ComposeError):
        parse("Ohne Zeitangabe", now=NOW)
    assert not (tmp_path / INBOX_FILE).exists()
