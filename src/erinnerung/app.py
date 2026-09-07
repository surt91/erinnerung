"""Alles zusammenstecken und laufen lassen."""

from __future__ import annotations

import asyncio
import html
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram.error import Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter
from telegram.ext import Application, ApplicationBuilder, ContextTypes
from watchfiles import awatch

from .archive import YamlArchiver
from .bot import BotHandlers, TelegramNotifier
from .config import ConfigError, LoadedConfig, load_config
from .engine import Engine, TickResult
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = "config"
DEFAULT_DB = "data/state.db"


class MissingSetting(Exception):
    """Eine Umgebungsvariable fehlt oder ergibt keinen Sinn."""


class Service:
    """Hält Konfiguration, Zustand und Bot zusammen und taktet die Engine."""

    def __init__(self, *, config_dir: Path, db_path: Path, token: str, chat_id: int) -> None:
        self.config_dir = Path(config_dir)
        self.chat_id = chat_id
        self.config: LoadedConfig = load_config(self.config_dir)
        self.store = Store(db_path)

        self.application: Application = (
            ApplicationBuilder()
            .token(token)
            .post_init(self._on_start)
            .post_shutdown(self._on_stop)
            .build()
        )
        self.notifier = TelegramNotifier(self.application, chat_id, self)
        self.engine = Engine(
            self.store, self.config, self.notifier, archiver=YamlArchiver(self)
        )
        BotHandlers(self, chat_id).register(self.application)
        self.application.add_error_handler(self._on_error)

        self._tasks: list[asyncio.Task] = []
        self._last_config_error: str | None = None
        self._last_error_notice: str | None = None
        self._skip_reload_notice = False

    def run(self) -> None:
        log.info(
            "Erinnerung startet: %d Erinnerungen aus %s, Zeitzone %s",
            len(self.config.reminders),
            self.config_dir,
            self.config.tzinfo.key,
        )
        self.application.run_polling()

    # -- Konfiguration -----------------------------------------------------

    def expect_own_write(self) -> None:
        """Der nächste Reload kommt von uns selbst und braucht keine Meldung im Chat."""
        self._skip_reload_notice = True

    def reload(self) -> str:
        try:
            config = load_config(self.config_dir)
        except ConfigError as exc:
            self._last_config_error = str(exc)
            return (
                "⚠️ <b>Konfiguration nicht übernommen</b>\n"
                f"<pre>{html.escape(str(exc), quote=False)}</pre>\n"
                "Es gilt weiter die bisherige."
            )

        self.config = config
        self.engine.config = config
        self._last_config_error = None
        return f"♻️ Konfiguration neu geladen — {len(config.reminders)} Erinnerungen."

    # -- Fehler ------------------------------------------------------------

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Auffangnetz für alles, was in einem Handler oder beim Polling schiefgeht.

        Ohne diesen Handler verschluckt python-telegram-bot Ausnahmen mit einem
        dürren Logeintrag — man merkt es also erst, wenn der Bot nicht antwortet.
        """
        error = context.error
        log.error("Telegram-Update %s fehlgeschlagen", update, exc_info=error)

        if isinstance(error, (NetworkError, RetryAfter, Conflict, Forbidden, InvalidToken)):
            return  # Telegram-seitig: das Polling fängt sich selbst, der Chat hilft hier nicht

        notice = (
            "⚠️ <b>Da ist etwas schiefgegangen</b>\n"
            f"<pre>{html.escape(f'{type(error).__name__}: {error}', quote=False)}</pre>\n"
            "Der Bot läuft weiter, Details stehen im Log."
        )
        if notice == self._last_error_notice:
            return  # denselben Fehler nicht bei jedem Versuch erneut in den Chat schreiben
        self._last_error_notice = notice
        await self.notifier.notify(notice)

    # -- Lebenszyklus ------------------------------------------------------

    async def _on_start(self, application: Application) -> None:
        self._tasks = [
            asyncio.create_task(self._tick_loop(), name="tick"),
            asyncio.create_task(self._watch_loop(), name="watch"),
        ]

    async def _on_stop(self, application: Application) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - Herunterfahren
                pass
        self.store.close()

    # -- Takt --------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while True:
            interval = self.config.settings.tick_interval.total_seconds()
            try:
                result = await self.engine.tick()
            except Exception:
                log.exception("Durchlauf fehlgeschlagen — nächster Versuch in %.0fs", interval)
            else:
                await self._report(result)
            await asyncio.sleep(interval)

    async def _report(self, result: TickResult) -> None:
        """Was während einer Auszeit durchgerutscht ist, sollte man erfahren."""
        if not result.expired:
            return
        titles = "\n".join(
            f"• {html.escape(occurrence.title, quote=False)}"
            for occurrence in result.expired[:10]
        )
        more = "" if len(result.expired) <= 10 else f"\n… und {len(result.expired) - 10} weitere"
        await self.notifier.notify(
            "⏳ <b>Während meiner Auszeit verpasst</b> (außerhalb des Kulanzfensters, "
            f"deshalb ohne Nachhaken):\n{titles}{more}"
        )

    # -- Dateiwatch --------------------------------------------------------

    async def _watch_loop(self) -> None:
        polling = os.environ.get("ERINNERUNG_WATCH_POLLING", "").lower() in {"1", "true", "yes"}
        try:
            async for changes in awatch(
                self.config_dir, recursive=False, force_polling=polling or None
            ):
                if not any(Path(path).suffix in {".yaml", ".yml"} for _, path in changes):
                    continue

                previous_error = self._last_config_error
                message = self.reload()

                if self._skip_reload_notice and self._last_config_error is None:
                    self._skip_reload_notice = False  # war unsere eigene Archivierung
                    continue
                if self._last_config_error and self._last_config_error == previous_error:
                    continue  # denselben Fehler nicht bei jedem Tastendruck wiederholen
                await self.notifier.notify(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Dateiwatch beendet — Änderungen brauchen jetzt /reload")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSetting(
            f"{name} ist nicht gesetzt. Trage es in die .env ein "
            "(Vorlage: .env.example)."
        )
    return value


def build_service() -> Service:
    load_dotenv()
    token = _require_env("TELEGRAM_BOT_TOKEN")
    raw_chat_id = _require_env("TELEGRAM_CHAT_ID")
    try:
        chat_id = int(raw_chat_id)
    except ValueError as exc:
        raise MissingSetting(f"TELEGRAM_CHAT_ID muss eine Zahl sein, nicht '{raw_chat_id}'") from exc

    return Service(
        config_dir=Path(os.environ.get("ERINNERUNG_CONFIG_DIR", DEFAULT_CONFIG_DIR)),
        db_path=Path(os.environ.get("ERINNERUNG_STATE_DB", DEFAULT_DB)),
        token=token,
        chat_id=chat_id,
    )
