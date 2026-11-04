"""Erledigte Einmal-Termine aus der Konfiguration ins Archiv verschieben.

Geschrieben wird im Round-Trip-Modus von ruamel.yaml, damit Kommentare,
Reihenfolge und Formatierung deiner Dateien erhalten bleiben.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ruamel.yaml import YAML

from . import yamlio
from .config import ARCHIVE_DIR

log = logging.getLogger(__name__)


class YamlArchiver:
    """Nimmt einen Eintrag aus seiner Quelldatei und hängt ihn ans Jahresarchiv."""

    def __init__(self, service) -> None:
        self._service = service

    async def archive(self, reminder_id: str) -> bool:
        config = self._service.config
        source = config.sources.get(reminder_id)
        if source is None or not source.is_file():
            return False

        parser = yamlio.parser()
        data = parser.load(source)
        entries = (data or {}).get("reminders")
        if not entries:
            return False

        index = next((i for i, entry in enumerate(entries) if entry.get("id") == reminder_id), None)
        if index is None:
            return False

        entry = entries.pop(index)

        # Erst das Archiv schreiben, dann die Quelle kürzen: geht dazwischen
        # etwas schief, steht der Eintrag lieber doppelt als gar nicht mehr da.
        moment = datetime.now(self._service.config.tzinfo)
        self._write_archive(source.parent / ARCHIVE_DIR, entry, moment, parser)

        self._service.expect_own_write()
        parser.dump(data, source)
        log.info("'%s' ins Archiv verschoben", reminder_id)
        return True

    def _write_archive(self, archive_dir: Path, entry, moment: datetime, parser: YAML) -> None:
        yamlio.append_reminder(
            archive_dir / f"{moment.year}.yaml",
            entry,
            comment=f"erledigt am {moment.strftime('%d.%m.%Y um %H:%M')}",
            yaml=parser,
        )
