from __future__ import annotations

from erinnerung.archive import YamlArchiver
from erinnerung.config import load_config

QUELLE = """\
# Meine Termine
reminders:
  # der wichtige hier
  - id: reifen
    title: "Reifenwechsel buchen"
    schedule:
      once: 2026-10-01 09:00

  - id: muell
    title: "Biotonne"   # bleibt drin
    schedule:
      every: friday
      at: "18:00"
"""


class FakeService:
    """Der Archivierer braucht vom Service nur die Konfiguration."""

    def __init__(self, config) -> None:
        self.config = config
        self.own_writes = 0

    def expect_own_write(self) -> None:
        self.own_writes += 1


def prepare(tmp_path):
    (tmp_path / "termine.yaml").write_text(QUELLE, encoding="utf-8")
    service = FakeService(load_config(tmp_path))
    return service, YamlArchiver(service)


async def test_archivieren_verschiebt_den_eintrag(tmp_path):
    _service, archiver = prepare(tmp_path)

    assert await archiver.archive("reifen") is True

    rest = (tmp_path / "termine.yaml").read_text(encoding="utf-8")
    assert "reifen" not in rest
    assert "muell" in rest

    archive = list((tmp_path / "archive").glob("*.yaml"))
    assert len(archive) == 1
    inhalt = archive[0].read_text(encoding="utf-8")
    assert "reifen" in inhalt
    assert "erledigt am" in inhalt


async def test_kommentare_und_formatierung_bleiben(tmp_path):
    _service, archiver = prepare(tmp_path)
    await archiver.archive("reifen")

    rest = (tmp_path / "termine.yaml").read_text(encoding="utf-8")
    assert "# Meine Termine" in rest
    assert "# bleibt drin" in rest
    assert 'at: "18:00"' in rest, "Anführungszeichen bleiben wie geschrieben"


async def test_archivierte_datei_wird_nicht_neu_geladen(tmp_path):
    _service, archiver = prepare(tmp_path)
    await archiver.archive("reifen")

    assert set(load_config(tmp_path).reminders) == {"muell"}


async def test_zweites_archivieren_haengt_an(tmp_path):
    service, archiver = prepare(tmp_path)
    (tmp_path / "mehr.yaml").write_text(
        "reminders:\n  - id: zweiter\n    title: Noch was\n    schedule: {once: 2026-11-01 09:00}\n",
        encoding="utf-8",
    )
    service.config = load_config(tmp_path)

    await archiver.archive("reifen")
    service.config = load_config(tmp_path)
    await archiver.archive("zweiter")

    archive = next((tmp_path / "archive").glob("*.yaml"))
    inhalt = archive.read_text(encoding="utf-8")
    assert "reifen" in inhalt and "zweiter" in inhalt


async def test_eigener_schreibvorgang_wird_angekuendigt(tmp_path):
    service, archiver = prepare(tmp_path)
    await archiver.archive("reifen")
    assert service.own_writes == 1, "der Dateiwatch soll deswegen nicht meckern"


async def test_unbekannte_id_tut_nichts(tmp_path):
    _service, archiver = prepare(tmp_path)
    assert await archiver.archive("gibtsnicht") is False
    assert not (tmp_path / "archive").exists()
