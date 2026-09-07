"""Der Service als Ganzes — vor allem sein Auffangnetz für Telegram-Fehler."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

from erinnerung.app import Service

CHAT_ID = 4242
TOKEN = "123456:abcdefghijklmnopqrstuvwxyz"


class Postfach:
    """Nimmt Systemmeldungen an, statt sie nach Telegram zu schicken."""

    def __init__(self) -> None:
        self.notices: list[str] = []

    async def notify(self, text: str) -> None:
        self.notices.append(text)


@pytest.fixture
def service(tmp_path):
    (tmp_path / "settings.yaml").write_text("timezone: Europe/Berlin\n", encoding="utf-8")
    service = Service(
        config_dir=tmp_path, db_path=tmp_path / "state.db", token=TOKEN, chat_id=CHAT_ID
    )
    service.notifier = Postfach()
    yield service
    service.store.close()


def context(error: Exception):
    return SimpleNamespace(error=error)


async def test_fehler_im_handler_landet_im_chat(service):
    await service._on_error("/offen", context(ValueError("kaputt")))

    assert len(service.notifier.notices) == 1
    assert "ValueError: kaputt" in service.notifier.notices[0]


async def test_derselbe_fehler_meldet_sich_nur_einmal(service):
    await service._on_error("/offen", context(ValueError("kaputt")))
    await service._on_error("/offen", context(ValueError("kaputt")))
    await service._on_error("/offen", context(ValueError("etwas anderes")))

    assert len(service.notifier.notices) == 2, "Wiederholungen bleiben im Log"


async def test_netzwerkfehler_bleibt_im_log(service):
    await service._on_error(None, context(NetworkError("Verbindung weg")))

    assert service.notifier.notices == [], "das Polling fängt sich selbst"


async def test_fehlerhandler_ist_registriert(service):
    assert service.application.error_handlers, "sonst schluckt PTB die Ausnahmen"
