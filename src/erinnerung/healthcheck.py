"""Healthcheck für Docker: schlägt der Taktgeber noch?

Der Tick-Loop schreibt bei jedem Durchlauf einen Zeitstempel in die Datenbank.
Ist der zu alt, steht etwas — dann soll Docker den Container neu starten.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from .app import DEFAULT_DB
from .engine import HEARTBEAT
from .store import Store

MAX_AGE = timedelta(minutes=5)


def main() -> int:
    path = os.environ.get("ERINNERUNG_STATE_DB", DEFAULT_DB)
    try:
        store = Store(path)
        heartbeat = store.get_time(HEARTBEAT)
        store.close()
    except Exception as exc:  # noqa: BLE001 - der Healthcheck darf nie selbst crashen
        print(f"Zustand nicht lesbar: {exc}", file=sys.stderr)
        return 1

    if heartbeat is None:
        print("noch kein Durchlauf", file=sys.stderr)
        return 1

    age = datetime.now(UTC) - heartbeat
    if age > MAX_AGE:
        print(f"letzter Durchlauf vor {age}", file=sys.stderr)
        return 1

    print(f"ok, letzter Durchlauf vor {int(age.total_seconds())}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
