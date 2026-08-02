"""Einstiegspunkt: ``python -m erinnerung``."""

from __future__ import annotations

import logging
import sys

from .app import MissingSetting, build_service
from .config import ConfigError


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Der Netzwerk-Unterbau von python-telegram-bot ist im Normalbetrieb still.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        service = build_service()
    except MissingSetting as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"Fehler in der Konfiguration:\n{exc}", file=sys.stderr)
        return 2

    service.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
