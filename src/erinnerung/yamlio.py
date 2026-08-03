"""Erinnerungs-Dateien schreiben, ohne sie zu verunstalten.

Gelesen und geschrieben wird im Round-Trip-Modus von ruamel.yaml: Kommentare,
Reihenfolge und Anführungszeichen deiner Dateien bleiben erhalten. Gelesen für
die *Konfiguration* wird trotzdem woanders (:mod:`erinnerung.config`) — hier
geht es nur ums Zurückschreiben.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


def parser() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def append_reminder(
    path: Path,
    entry,
    *,
    comment: str | None = None,
    header: str | None = None,
    yaml: YAML | None = None,
) -> None:
    """``entry`` an die ``reminders``-Liste in ``path`` anhängen.

    Existiert die Datei nicht, wird sie angelegt — dann bekommt sie ``header``
    als Kopfkommentar. ``comment`` steht über dem neuen Eintrag.
    """
    yaml = yaml or parser()

    data = yaml.load(path) if path.is_file() else None
    if not isinstance(data, CommentedMap):
        data = CommentedMap()
        if header:
            data.yaml_set_start_comment(header)
    if not isinstance(data.get("reminders"), CommentedSeq):
        data["reminders"] = CommentedSeq()

    entries = data["reminders"]
    entries.append(entry)
    if comment:
        try:
            entries.yaml_set_comment_before_after_key(len(entries) - 1, before=comment, indent=2)
        except AttributeError:
            pass  # ohne Kommentar ist auch in Ordnung

    path.parent.mkdir(parents=True, exist_ok=True)
    yaml.dump(data, path)
