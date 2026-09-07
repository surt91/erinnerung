"""Telegram: Zustellung, Knöpfe, Kommandos.

Der Bot hängt per Long-Polling an Telegram — es muss also kein Port nach außen
offen sein und kein Reverse Proxy davor.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from . import compose
from . import format as fmt
from .config import INBOX_FILE, Reminder
from .duration import format_duration
from .engine import HEARTBEAT, Engine, snooze_label
from .schedule import next_occurrence
from .store import ACKED, OPEN, Occurrence, Store

log = logging.getLogger(__name__)

DONE = "done"
SKIP = "skip"
SNOOZE = "snooze"

_COMMAND = re.compile(r"^/\w+(?:@\S+)?\s*")

NEW_HELP = (
    "<b>/neu</b> — erst wann, dann was:\n"
    "<code>/neu freitag 18:00 Biotonne rausstellen</code>\n"
    "<code>/neu mo, mi, fr 7:30 Tabletten nehmen</code>\n"
    "<code>/neu täglich 19:00 Pflanzen gießen</code>\n"
    "<code>/neu monatlich am 1. Kontoauszüge durchsehen</code>\n"
    "<code>/neu 12. Oktober 9:00 Geschenk besorgen</code>\n"
    "<code>/neu in 2h Wäsche aufhängen</code>\n"
    "Eine zweite Zeile wird zur Beschreibung. Fängt der Titel selbst mit einer "
    "Zeitangabe an, trenn mit <code>|</code>."
)


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


T = TypeVar("T")

RETRY_DELAYS = (2.0, 5.0)  # zwei Nachfassversuche, dann war es kein Wackler mehr
MAX_WAIT = 60.0  # länger auf ein Rate-Limit zu warten bringt nichts


async def call(
    action: Callable[[], Awaitable[T]],
    what: str,
    *,
    ignore: tuple[type[BaseException], ...] = (),
) -> T | None:
    """Einen Telegram-Aufruf machen: Wackler wiederholen, alles andere protokollieren.

    Ergebnis ist ``None``, wenn es endgültig nicht geklappt hat. Kein API-Aufruf soll
    den Bot anhalten — die nächste Runde bzw. das nächste Kommando versucht es erneut.
    """
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            return await action()
        except ignore:
            return None
        except RetryAfter as exc:
            wait = min(_seconds(exc.retry_after) + 1.0, MAX_WAIT)
            reason = f"Telegram bremst uns ({exc})"
        except BadRequest as exc:
            # Unterklasse von NetworkError, heilt aber kein zweiter Versuch.
            log.warning("%s: Telegram lehnt ab — %s", what, exc)
            return None
        except (TimedOut, NetworkError) as exc:
            wait = delay or 0.0
            reason = f"Netz wackelt ({exc})"
        except Forbidden as exc:
            log.error("%s: Der Bot darf nicht in diesen Chat — %s", what, exc)
            return None
        except TelegramError:
            log.exception("%s: unerwarteter Telegram-Fehler", what)
            return None

        if delay is None:
            log.error("%s: nach %d Versuchen aufgegeben — %s", what, attempt, reason)
            return None
        log.warning("%s: %s — neuer Versuch in %.0fs", what, reason, wait)
        await asyncio.sleep(wait)
    return None


def _seconds(retry_after) -> float:
    # python-telegram-bot stellt retry_after gerade auf timedelta um.
    if isinstance(retry_after, timedelta):
        return retry_after.total_seconds()
    return float(retry_after)


async def reply(message, text: str, **kwargs) -> None:
    """Antwort auf ein Kommando — scheitert sie, bleibt es beim Logeintrag."""
    await call(lambda: message.reply_text(text, **kwargs), "Antwort im Chat")


async def answer(query, text: str | None = None) -> None:
    # Ein unbeantworteter Knopf dreht im Client ewig; zu alte Anfragen sind verloren.
    await call(lambda: query.answer(text), "Knopf-Quittung", ignore=(BadRequest,))


class TelegramNotifier:
    """Stellt Erinnerungen zu und räumt dabei den Chat auf."""

    def __init__(self, application: Application, chat_id: int, engine_holder) -> None:
        self.application = application
        self.chat_id = chat_id
        self._engine_holder = engine_holder

    @property
    def engine(self) -> Engine:
        return self._engine_holder.engine

    async def send(self, occurrence: Occurrence, reminder: Reminder | None) -> int | None:
        # Eine bearbeitete Nachricht löst keine Benachrichtigung aus — deshalb
        # wird die alte gelöscht und eine neue geschickt. Das Handy meldet sich,
        # der Chat bleibt trotzdem übersichtlich.
        await self.withdraw(occurrence)

        text = self.render(occurrence, reminder)
        markup = self.keyboard(occurrence)
        message = await call(
            lambda: self.application.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            ),
            f"Erinnerung '{occurrence.reminder_id}' zustellen",
        )
        if message is None:
            return None  # bleibt offen, der nächste Durchlauf nimmt einen neuen Anlauf
        return message.message_id

    async def withdraw(self, occurrence: Occurrence) -> None:
        if occurrence.message_id is None:
            return
        await call(
            lambda: self.application.bot.delete_message(self.chat_id, occurrence.message_id),
            f"Nachricht {occurrence.message_id} entfernen",
            ignore=(BadRequest,),  # zu alt oder schon weg — nicht schlimm
        )
        self.engine.store.forget_message(occurrence.id)

    async def notify(self, text: str) -> None:
        """Systemmeldung (Konfigurationsfehler, Neustart) ohne Knöpfe."""
        await call(
            lambda: self.application.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML
            ),
            "Systemmeldung zustellen",
        )

    # -- Darstellung -------------------------------------------------------

    def render(self, occurrence: Occurrence, reminder: Reminder | None) -> str:
        engine = self.engine
        now = engine.clock()
        due_local = engine.local(occurrence.due_at)

        lines = [f"🔔 <b>{_escape(occurrence.title)}</b>"]
        if reminder and reminder.text:
            lines.append(_escape(reminder.text))

        detail = []
        if occurrence.has_lead:
            event_local = engine.local(occurrence.event_at)
            detail.append(f"Termin: {fmt.stamp(event_local)} ({fmt.relative(occurrence.event_at - now)})")
        else:
            detail.append(f"fällig seit {fmt.clock(due_local)}")

        waiting = now - occurrence.due_at
        if waiting.total_seconds() >= 3600:
            detail.append(f"offen seit {format_duration(waiting)}")
        if occurrence.nag_count:
            detail.append(f"{occurrence.nag_count + 1}. Erinnerung")

        lines.append("<i>" + _escape(" · ".join(detail)) + "</i>")
        return "\n".join(lines)

    def keyboard(self, occurrence: Occurrence) -> InlineKeyboardMarkup:
        options = self.engine.config.settings.snooze_options
        rows = [
            [
                InlineKeyboardButton("✅ Erledigt", callback_data=f"{DONE}:{occurrence.id}"),
                InlineKeyboardButton("🚫 Diesmal nicht", callback_data=f"{SKIP}:{occurrence.id}"),
            ]
        ]
        snoozes = [
            InlineKeyboardButton(
                f"⏰ {snooze_label(option)}", callback_data=f"{SNOOZE}:{occurrence.id}:{option}"
            )
            for option in options[:3]
        ]
        if snoozes:
            rows.append(snoozes)
        return InlineKeyboardMarkup(rows)


class BotHandlers:
    """Kommandos und Knopfdrücke. Kennt die Engine, nicht die Datenbank."""

    def __init__(self, engine_holder, chat_id: int) -> None:
        self._engine_holder = engine_holder
        self.chat_id = chat_id

    @property
    def engine(self) -> Engine:
        return self._engine_holder.engine

    @property
    def store(self) -> Store:
        return self.engine.store

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler(["start", "hilfe", "help"], self.help))
        application.add_handler(CommandHandler(["neu", "new"], self.new))
        application.add_handler(CommandHandler(["offen", "open"], self.open_items))
        application.add_handler(CommandHandler(["erledigt", "done"], self.done))
        application.add_handler(CommandHandler(["snooze", "schlummern"], self.snooze))
        application.add_handler(CommandHandler(["plan", "naechste"], self.plan))
        application.add_handler(CommandHandler("status", self.status))
        application.add_handler(CommandHandler("reload", self.reload))
        application.add_handler(CallbackQueryHandler(self.on_button))

    # -- Zugangskontrolle --------------------------------------------------

    def _allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is not None and chat.id == self.chat_id:
            return True
        log.warning("Zugriff aus fremdem Chat %s ignoriert", chat.id if chat else "?")
        return False

    # -- Kommandos ---------------------------------------------------------

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        await reply(
            update.effective_message,
            "<b>Erinnerung</b>\n"
            "Ich melde mich, sobald etwas fällig ist, und hake nach, bis du bestätigst.\n\n"
            "/neu &lt;wann&gt; &lt;was&gt; — neue Erinnerung anlegen, z.B. "
            "<code>/neu freitag 18:00 Biotonne rausstellen</code>\n"
            "/offen — was gerade offen ist\n"
            "/erledigt &lt;id&gt; — ohne Knopfdruck bestätigen\n"
            "/snooze &lt;id&gt; &lt;dauer&gt; — z.B. <code>/snooze muell-bio 2h</code>\n"
            "/plan — nächste Fälligkeiten aller Erinnerungen\n"
            "/status — Zustand und Zeitzone\n"
            "/reload — Konfiguration neu einlesen",
            parse_mode=ParseMode.HTML,
        )

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Eine Erinnerung aus dem Chat anlegen — sie landet in der YAML, nicht in der DB."""
        if not self._allowed(update):
            return
        message = update.effective_message
        request = _COMMAND.sub("", message.text or "", count=1)
        if not request.strip():
            await reply(message, NEW_HELP, parse_mode=ParseMode.HTML)
            return

        engine = self.engine
        now = engine.local(engine.clock())
        try:
            draft = compose.parse(request, now=now, taken=engine.config.reminders)
        except compose.ComposeError as exc:
            await reply(message, f"⚠️ {_escape(str(exc))}\n\n{NEW_HELP}", parse_mode=ParseMode.HTML)
            return

        service = self._engine_holder
        service.expect_own_write()
        try:
            compose.write(draft, service.config_dir, now=now)
        except OSError as exc:
            await reply(message, f"⚠️ Schreiben nach {INBOX_FILE} ging schief: {exc}")
            return

        notice = service.reload()
        if draft.reminder.id not in self.engine.config.reminders:
            await reply(message, notice, parse_mode=ParseMode.HTML)
            return

        await reply(message, self._confirm(draft), parse_mode=ParseMode.HTML)

    def _confirm(self, draft: compose.Draft) -> str:
        reminder = draft.reminder
        lines = [f"➕ <b>{_escape(reminder.title)}</b>"]
        if reminder.text:
            lines.append(_escape(reminder.text))

        detail = [draft.description]
        now = self.engine.clock()
        moment = next_occurrence(reminder.schedule, self.engine.config.tzinfo, now)
        if moment is not None:
            moment -= reminder.lead_delta
            if reminder.schedule.is_once:
                # Der Termin steht schon in der Beschreibung — hier fehlt nur, wie weit weg er ist.
                detail[0] += f" ({fmt.relative(moment - now)})"
            else:
                detail.append(
                    f"zum ersten Mal {fmt.stamp(self.engine.local(moment))} "
                    f"({fmt.relative(moment - now)})"
                )
        lines.append("<i>" + _escape(" · ".join(detail)) + "</i>")
        lines.append(f"<code>{_escape(reminder.id)}</code> in {INBOX_FILE}")
        return "\n".join(lines)

    async def open_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        occurrences = self.store.open_occurrences()
        if not occurrences:
            await reply(update.effective_message, "Nichts offen. 🎉")
            return

        now = self.engine.clock()
        lines = ["<b>Offen:</b>"]
        for occurrence in occurrences:
            due = self.engine.local(occurrence.due_at)
            note = f"seit {fmt.stamp(due)}"
            if occurrence.snoozed_until and occurrence.snoozed_until > now:
                note += f", schlummert bis {fmt.clock(self.engine.local(occurrence.snoozed_until))}"
            lines.append(
                f"• <b>{_escape(occurrence.title)}</b> — {note}\n"
                f"  <code>/erledigt {_escape(occurrence.reminder_id)}</code>"
            )
        await reply(update.effective_message, "\n".join(lines), parse_mode=ParseMode.HTML)

    def _find_open(self, needle: str) -> list[Occurrence]:
        """Offene Instanzen zu einer Erinnerungs-id, auch bei Teileingabe."""
        needle = needle.strip().lower()
        occurrences = self.store.open_occurrences()
        exact = [o for o in occurrences if o.reminder_id.lower() == needle]
        if exact:
            return exact
        return [o for o in occurrences if o.reminder_id.lower().startswith(needle)]

    async def done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if not context.args:
            await reply(update.effective_message, "So: /erledigt <id> — welche id, zeigt /offen.")
            return

        matches = self._find_open(context.args[0])
        if not matches:
            await reply(
                update.effective_message, f"Zu '{context.args[0]}' ist gerade nichts offen."
            )
            return

        occurrence = await self.engine.ack(matches[0].id)
        if occurrence is None:
            await reply(update.effective_message, "Das war schon erledigt.")
            return
        await self._clear_message(matches[0], "✅ erledigt")
        rest = len(matches) - 1
        suffix = f" (noch {rest} weitere offen)" if rest else ""
        await reply(
            update.effective_message,
            f"✅ <b>{_escape(occurrence.title)}</b> erledigt{suffix}",
            parse_mode=ParseMode.HTML,
        )

    async def snooze(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if len(context.args) < 2:
            await reply(update.effective_message, "So: /snooze <id> <dauer>, z.B. 2h")
            return

        matches = self._find_open(context.args[0])
        if not matches:
            await reply(
                update.effective_message, f"Zu '{context.args[0]}' ist gerade nichts offen."
            )
            return

        try:
            occurrence, until = self.engine.snooze(matches[0].id, context.args[1])
        except ValueError as exc:
            await reply(update.effective_message, str(exc))
            return
        if occurrence is None:
            await reply(update.effective_message, "Das ist nicht mehr offen.")
            return
        await reply(
            update.effective_message,
            f"⏰ <b>{_escape(occurrence.title)}</b> meldet sich wieder um "
            f"{fmt.clock(self.engine.local(until))} ({fmt.day(self.engine.local(until))})",
            parse_mode=ParseMode.HTML,
        )

    async def plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        config = self.engine.config
        if not config.reminders:
            await reply(update.effective_message, "Es ist keine Erinnerung konfiguriert.")
            return

        now = self.engine.clock()
        upcoming: list[tuple[datetime | None, str]] = []
        for reminder in config.reminders.values():
            moment = next_occurrence(reminder.schedule, config.tzinfo, now)
            if moment is not None:
                moment -= reminder.lead_delta
            upcoming.append((moment, reminder.title))

        upcoming.sort(key=lambda item: (item[0] is None, item[0] or now))
        lines = ["<b>Als Nächstes:</b>"]
        for moment, title in upcoming:
            when = (
                f"{fmt.stamp(self.engine.local(moment))} ({fmt.relative(moment - now)})"
                if moment
                else "kein weiterer Termin"
            )
            lines.append(f"• <b>{_escape(title)}</b> — {when}")
        await reply(update.effective_message, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        engine = self.engine
        counts = self.store.counts_by_status()
        heartbeat = self.store.get_time(HEARTBEAT)
        lines = [
            "<b>Status</b>",
            f"Erinnerungen konfiguriert: {len(engine.config.reminders)}",
            f"Offen: {counts.get(OPEN, 0)} · erledigt: {counts.get(ACKED, 0)}",
            f"Zeitzone: {engine.config.tzinfo.key}",
            f"Letzter Durchlauf: "
            + (fmt.clock(engine.local(heartbeat)) if heartbeat else "noch keiner"),
        ]
        await reply(update.effective_message, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        message = self._engine_holder.reload()
        await reply(update.effective_message, message, parse_mode=ParseMode.HTML)

    # -- Knöpfe ------------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._allowed(update):
            await answer(query, "Nicht für dich.")
            return

        parts = (query.data or "").split(":")
        action, raw_id, extra = parts[0], parts[1] if len(parts) > 1 else "", parts[2:]
        try:
            occurrence_id = int(raw_id)
        except ValueError:
            await answer(query, "Kann ich nicht zuordnen.")
            return

        if action == DONE:
            occurrence = await self.engine.ack(occurrence_id)
            await self._answer(query, occurrence, "✅ erledigt", "War schon erledigt.")
        elif action == SKIP:
            occurrence = await self.engine.skip(occurrence_id)
            await self._answer(query, occurrence, "🚫 übersprungen", "War schon vom Tisch.")
        elif action == SNOOZE:
            spec = extra[0] if extra else "1h"
            try:
                occurrence, until = self.engine.snooze(occurrence_id, spec)
            except ValueError:
                await answer(query, "Diese Schlummerdauer verstehe ich nicht.")
                return
            if occurrence is None:
                await answer(query, "Nicht mehr offen.")
                return
            local_until = self.engine.local(until)
            await answer(query, f"Bis {fmt.clock(local_until)}.")
            await self._append_note(query, f"⏰ schlummert bis {fmt.clock(local_until)}")
        else:
            await answer(query)

    async def _answer(self, query, occurrence: Occurrence | None, note: str, fallback: str) -> None:
        if occurrence is None:
            await answer(query, fallback)
            return
        await answer(query, note)
        await self._append_note(query, f"{note} um {fmt.clock(self.engine.local(self.engine.clock()))}",
                               keep_buttons=False)

    async def _append_note(self, query, note: str, keep_buttons: bool = True) -> None:
        """Die Nachricht als quittiert markieren, statt sie zu löschen — als Beleg im Chat."""
        text = f"{query.message.text_html}\n<i>{_escape(note)}</i>"
        await call(
            lambda: query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=query.message.reply_markup if keep_buttons else None,
            ),
            "Nachricht quittieren",
            ignore=(BadRequest,),  # unverändert oder zu alt — der Knopf hat trotzdem gewirkt
        )

    async def _clear_message(self, occurrence: Occurrence, note: str) -> None:
        """Nach /erledigt die zugehörige Nachricht entschärfen."""
        if occurrence.message_id is None:
            return
        bot = self._engine_holder.notifier.application.bot
        await call(
            lambda: bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=occurrence.message_id, reply_markup=None
            ),
            f"Knöpfe an Nachricht {occurrence.message_id} entfernen",
            ignore=(BadRequest,),
        )
