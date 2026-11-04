"""Die Telegram-Schicht gegen einen erfundenen Bot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from erinnerung import bot as bot_module
from erinnerung.bot import BotHandlers, TelegramNotifier
from erinnerung.config import INBOX_FILE, Reminder, load_config
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


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, parse_mode=None) -> None:
        self.replies.append(text)


def command(text: str):
    """Ein Update, wie es der CommandHandler durchreicht."""
    message = FakeMessage(text)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT_ID), effective_message=message
    ), message


@pytest.fixture
def neu(tmp_path):
    """Handler samt echtem Konfigordner — /neu schreibt schließlich Dateien."""
    (tmp_path / "settings.yaml").write_text("timezone: Europe/Berlin\n", encoding="utf-8")

    class FakeService:
        config_dir = tmp_path

        def __init__(self) -> None:
            self.config = load_config(tmp_path)
            self.own_writes = 0

        def expect_own_write(self) -> None:
            self.own_writes += 1

        def reload(self) -> str:
            self.config = load_config(self.config_dir)
            self.engine.config = self.config
            return "♻️ neu geladen"

    service = FakeService()
    store = Store(tmp_path / "state.db")
    service.engine = Engine(store, service.config, None, clock=FakeClock(local(2026, 8, 3, 12)))
    service.notifier = None

    yield SimpleNamespace(
        handlers=BotHandlers(service, CHAT_ID), service=service, config_dir=tmp_path
    )
    store.close()


async def test_neu_legt_die_erinnerung_an(neu):
    update, message = command("/neu freitag 18:00 Biotonne rausstellen\nDeckel zu.")
    await neu.handlers.new(update, None)

    reminder = neu.service.config.reminders["biotonne-rausstellen"]
    assert reminder.title == "Biotonne rausstellen"
    assert reminder.text == "Deckel zu."
    assert reminder.schedule.cron_expr == "0 18 * * FRI"
    assert (neu.config_dir / INBOX_FILE).is_file()

    antwort = message.replies[-1]
    assert "Biotonne rausstellen" in antwort
    assert "jeden Freitag um 18:00" in antwort
    assert "Fr, 07.08.2026 um 18:00" in antwort, "die erste Fälligkeit steht in der Antwort"


async def test_neu_nennt_bei_einmaligem_den_abstand(neu):
    update, message = command("/neu in 2h Wäsche aufhängen")
    await neu.handlers.new(update, None)

    assert "einmalig am Mo, 03.08.2026 um 14:00 (in 2 h)" in message.replies[-1]


async def test_neu_meldet_den_eigenen_schreibvorgang_an(neu):
    update, _ = command("/neu täglich 19:00 Pflanzen gießen")
    await neu.handlers.new(update, None)
    assert neu.service.own_writes == 1, "der Dateiwatch soll deswegen nicht meckern"


async def test_neu_ohne_verstaendliche_zeitangabe_erklaert_sich(neu):
    update, message = command("/neu Irgendwann mal aufräumen")
    await neu.handlers.new(update, None)

    assert "⚠️" in message.replies[-1]
    assert "/neu freitag 18:00" in message.replies[-1], "die Hilfe kommt gleich mit"
    assert not (neu.config_dir / INBOX_FILE).exists()
    assert not neu.service.config.reminders


async def test_neu_ohne_argumente_zeigt_die_hilfe(neu):
    update, message = command("/neu")
    await neu.handlers.new(update, None)

    assert message.replies[-1].startswith("<b>/neu</b>")
    assert not (neu.config_dir / INBOX_FILE).exists()


async def test_neu_aus_fremdem_chat_wird_ignoriert(neu):
    message = FakeMessage("/neu täglich 19:00 Pflanzen gießen")
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=99), effective_message=message)
    await neu.handlers.new(update, None)

    assert message.replies == []
    assert not (neu.config_dir / INBOX_FILE).exists()


async def test_handler_lassen_sich_registrieren():
    """Fängt Vertipper in der Verdrahtung, ohne echtes Telegram."""
    from telegram.ext import ApplicationBuilder

    application = ApplicationBuilder().token("123456:abcdefghijklmnopqrstuvwxyz").build()
    BotHandlers(Holder(), CHAT_ID).register(application)

    handlers = application.handlers[0]
    commands = {
        command for handler in handlers for command in getattr(handler, "commands", None) or []
    }
    assert {"neu", "offen", "erledigt", "snooze", "plan", "status", "reload"} <= commands


# -- Fehlerhandling ---------------------------------------------------------


@pytest.fixture(autouse=True)
def sofort_wiederholen(monkeypatch):
    """Wartezeiten zwischen den Versuchen interessieren im Test nicht."""
    monkeypatch.setattr(bot_module, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(bot_module.asyncio, "sleep", _no_sleep)


async def _no_sleep(_seconds) -> None:
    return None


class FlakyBot(FakeBot):
    """Wirft die vorgegebenen Fehler, bevor er sich zum Senden bequemt."""

    def __init__(self, *errors: Exception) -> None:
        super().__init__()
        self.errors = list(errors)
        self.attempts = 0

    async def send_message(self, *args, **kwargs):
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        return await super().send_message(*args, **kwargs)


async def test_zustellung_uebersteht_einen_netzwerkwackler(wired):
    flaky = FlakyBot(TimedOut(), RetryAfter(1))
    wired.notifier.application.bot = flaky

    await wired.engine.tick()
    wired.clock.advance(minutes=2)
    await wired.engine.tick()

    assert flaky.attempts == 3, "zwei Fehlversuche, dann kommt sie durch"
    assert "Biotonne rausstellen" in flaky.sent[-1].text
    offen = wired.engine.store.open_occurrences()
    assert offen[0].message_id == flaky.sent[-1].message_id


async def test_dauerhafter_fehler_laesst_die_erinnerung_offen(wired, caplog):
    flaky = FlakyBot(TimedOut(), TimedOut(), TimedOut())
    wired.notifier.application.bot = flaky

    await wired.engine.tick()
    wired.clock.advance(minutes=2)
    await wired.engine.tick()

    assert flaky.attempts == 3, "nach drei Versuchen ist Schluss"
    assert flaky.sent == []
    offen = wired.engine.store.open_occurrences()
    assert offen and offen[0].message_id is None, "ohne Nachricht wird beim nächsten Mal genervt"


async def test_abgelehnte_nachricht_wird_nicht_wiederholt(wired):
    flaky = FlakyBot(*[BadRequest("chat not found")] * 5)
    wired.notifier.application.bot = flaky

    await wired.engine.tick()
    wired.clock.advance(minutes=2)
    await wired.engine.tick()

    assert flaky.attempts == 1, "was Telegram ablehnt, lehnt es auch beim zweiten Mal ab"


async def test_kommandoantwort_scheitert_ohne_absturz(neu):
    class StureMessage(FakeMessage):
        async def reply_text(self, text, parse_mode=None):
            raise TimedOut()

    message = StureMessage("/neu täglich 19:00 Pflanzen gießen")
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=CHAT_ID), effective_message=message)

    await neu.handlers.new(update, None)

    assert "pflanzen-giessen" in neu.service.config.reminders, "angelegt ist sie trotzdem"
