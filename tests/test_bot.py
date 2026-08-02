"""Die Telegram-Schicht gegen einen erfundenen Bot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from erinnerung.bot import BotHandlers, TelegramNotifier
from erinnerung.config import Reminder
from erinnerung.engine import Engine
from erinnerung.schedule import Schedule
from erinnerung.store import Store

from .conftest import FakeClock, local, make_config

CHAT_ID = 4242


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []
        self.deleted: list[int] = []
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self._next_id += 1
        self.sent.append(
            SimpleNamespace(
                chat_id=chat_id, text=text, markup=reply_markup, message_id=self._next_id
            )
        )
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


class Holder:
    """Steht für den Service: Notifier und Handler greifen darüber auf die Engine zu."""

    engine: Engine | None = None
    notifier: TelegramNotifier | None = None


@pytest.fixture
def wired(tmp_path):
    holder = Holder()
    application = SimpleNamespace(bot=FakeBot())
    notifier = TelegramNotifier(application, CHAT_ID, holder)

    reminder = Reminder(
        id="muell",
        title="Biotonne rausstellen",
        text="Deckel zu.",
        schedule=Schedule(every="friday", at="18:00"),
        nag={"interval": "30m"},
    )
    store = Store(tmp_path / "state.db")
    clock = FakeClock(local(2026, 8, 7, 17, 59))
    engine = Engine(store, make_config(reminder), notifier, clock=clock)
    holder.engine = engine
    holder.notifier = notifier

    yield SimpleNamespace(
        engine=engine, clock=clock, bot=application.bot, notifier=notifier, holder=holder
    )
    store.close()


async def test_nachricht_enthaelt_titel_und_knoepfe(wired):
    await wired.engine.tick()
    wired.clock.advance(minutes=2)
    await wired.engine.tick()

    assert len(wired.bot.sent) == 1
    message = wired.bot.sent[0]
    assert message.chat_id == CHAT_ID
    assert "Biotonne rausstellen" in message.text
    assert "Deckel zu." in message.text
    assert "fällig seit 18:00" in message.text

    buttons = [button.text for row in message.markup.inline_keyboard for button in row]
    assert buttons[0].startswith("✅")
    assert any("morgen" in button for button in buttons)


async def test_erneutes_nerven_raeumt_die_alte_nachricht_weg(wired):
    await wired.engine.tick()
    wired.clock.advance(minutes=2)
    await wired.engine.tick()
    erste = wired.bot.sent[0].message_id

    wired.clock.advance(minutes=31)
    await wired.engine.tick()

    assert wired.bot.deleted == [erste], "die alte Nachricht verschwindet, die neue meldet sich"
    assert len(wired.bot.sent) == 2
    assert "2. Erinnerung" in wired.bot.sent[1].text


async def test_vorlauf_zeigt_den_eigentlichen_termin(wired, tmp_path):
    reminder = Reminder(
        id="geschenk",
        title="Geschenk besorgen",
        schedule=Schedule(every="year", date="10-12", at="09:00"),
        lead="3d",
    )
    wired.engine.config = make_config(reminder)
    wired.clock.set(local(2026, 10, 9, 8, 59))
    await wired.engine.tick()
    wired.clock.advance(minutes=2)
    await wired.engine.tick()

    text = wired.bot.sent[-1].text
    assert "Termin: Mo, 12.10.2026 um 09:00" in text
    assert "in 3 Tagen" in text


async def test_sonderzeichen_im_titel_brechen_nichts(wired):
    reminder = Reminder(
        id="html",
        title="Steuer <b>fällig</b> & Co",
        schedule=Schedule(every="day", at="18:00"),
    )
    wired.engine.config = make_config(reminder)
    await wired.engine.tick()
    wired.clock.advance(minutes=2)
    await wired.engine.tick()

    text = wired.bot.sent[-1].text
    assert "&lt;b&gt;fällig&lt;/b&gt; &amp; Co" in text


async def test_handler_lassen_sich_registrieren():
    """Fängt Vertipper in der Verdrahtung, ohne echtes Telegram."""
    from telegram.ext import ApplicationBuilder

    application = ApplicationBuilder().token("123456:abcdefghijklmnopqrstuvwxyz").build()
    BotHandlers(Holder(), CHAT_ID).register(application)

    handlers = application.handlers[0]
    commands = {
        command
        for handler in handlers
        for command in getattr(handler, "commands", None) or []
    }
    assert {"offen", "erledigt", "snooze", "plan", "status", "reload"} <= commands
