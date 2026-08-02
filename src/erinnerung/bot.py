"""Telegram: Zustellung, Knöpfe, Kommandos.

Der Bot hängt per Long-Polling an Telegram — es muss also kein Port nach außen
offen sein und kein Reverse Proxy davor.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from . import format as fmt
from .config import Reminder
from .duration import format_duration
from .engine import HEARTBEAT, Engine, snooze_label
from .schedule import next_occurrence
from .store import ACKED, OPEN, Occurrence, Store

log = logging.getLogger(__name__)

DONE = "done"
SKIP = "skip"
SNOOZE = "snooze"


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


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
        try:
            message = await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            log.exception("Erinnerung '%s' konnte nicht zugestellt werden", occurrence.reminder_id)
            return None
        return message.message_id

    async def withdraw(self, occurrence: Occurrence) -> None:
        if occurrence.message_id is None:
            return
        try:
            await self.application.bot.delete_message(self.chat_id, occurrence.message_id)
        except BadRequest:
            pass  # zu alt oder schon weg — nicht schlimm
        except TelegramError:
            log.warning("Nachricht %s ließ sich nicht entfernen", occurrence.message_id)
        finally:
            self.engine.store.forget_message(occurrence.id)

    async def notify(self, text: str) -> None:
        """Systemmeldung (Konfigurationsfehler, Neustart) ohne Knöpfe."""
        try:
            await self.application.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML
            )
        except TelegramError:
            log.exception("Systemmeldung konnte nicht zugestellt werden")

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
        await update.effective_message.reply_text(
            "<b>Erinnerung</b>\n"
            "Ich melde mich, sobald etwas fällig ist, und hake nach, bis du bestätigst.\n\n"
            "/offen — was gerade offen ist\n"
            "/erledigt &lt;id&gt; — ohne Knopfdruck bestätigen\n"
            "/snooze &lt;id&gt; &lt;dauer&gt; — z.B. <code>/snooze muell-bio 2h</code>\n"
            "/plan — nächste Fälligkeiten aller Erinnerungen\n"
            "/status — Zustand und Zeitzone\n"
            "/reload — Konfiguration neu einlesen",
            parse_mode=ParseMode.HTML,
        )

    async def open_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        occurrences = self.store.open_occurrences()
        if not occurrences:
            await update.effective_message.reply_text("Nichts offen. 🎉")
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
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

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
            await update.effective_message.reply_text(
                "So: /erledigt <id> — welche id, zeigt /offen."
            )
            return

        matches = self._find_open(context.args[0])
        if not matches:
            await update.effective_message.reply_text(
                f"Zu '{context.args[0]}' ist gerade nichts offen."
            )
            return

        occurrence = await self.engine.ack(matches[0].id)
        if occurrence is None:
            await update.effective_message.reply_text("Das war schon erledigt.")
            return
        await self._clear_message(matches[0], "✅ erledigt")
        rest = len(matches) - 1
        suffix = f" (noch {rest} weitere offen)" if rest else ""
        await update.effective_message.reply_text(
            f"✅ <b>{_escape(occurrence.title)}</b> erledigt{suffix}", parse_mode=ParseMode.HTML
        )

    async def snooze(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if len(context.args) < 2:
            await update.effective_message.reply_text("So: /snooze <id> <dauer>, z.B. 2h")
            return

        matches = self._find_open(context.args[0])
        if not matches:
            await update.effective_message.reply_text(
                f"Zu '{context.args[0]}' ist gerade nichts offen."
            )
            return

        try:
            occurrence, until = self.engine.snooze(matches[0].id, context.args[1])
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        if occurrence is None:
            await update.effective_message.reply_text("Das ist nicht mehr offen.")
            return
        await update.effective_message.reply_text(
            f"⏰ <b>{_escape(occurrence.title)}</b> meldet sich wieder um "
            f"{fmt.clock(self.engine.local(until))} ({fmt.day(self.engine.local(until))})",
            parse_mode=ParseMode.HTML,
        )

    async def plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        config = self.engine.config
        if not config.reminders:
            await update.effective_message.reply_text("Es ist keine Erinnerung konfiguriert.")
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
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

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
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        message = self._engine_holder.reload()
        await update.effective_message.reply_text(message, parse_mode=ParseMode.HTML)

    # -- Knöpfe ------------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._allowed(update):
            await query.answer("Nicht für dich.")
            return

        parts = (query.data or "").split(":")
        action, raw_id, extra = parts[0], parts[1] if len(parts) > 1 else "", parts[2:]
        try:
            occurrence_id = int(raw_id)
        except ValueError:
            await query.answer("Kann ich nicht zuordnen.")
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
                await query.answer("Diese Schlummerdauer verstehe ich nicht.")
                return
            if occurrence is None:
                await query.answer("Nicht mehr offen.")
                return
            local_until = self.engine.local(until)
            await query.answer(f"Bis {fmt.clock(local_until)}.")
            await self._append_note(query, f"⏰ schlummert bis {fmt.clock(local_until)}")
        else:
            await query.answer()

    async def _answer(self, query, occurrence: Occurrence | None, note: str, fallback: str) -> None:
        if occurrence is None:
            await query.answer(fallback)
            return
        await query.answer(note)
        await self._append_note(query, f"{note} um {fmt.clock(self.engine.local(self.engine.clock()))}",
                               keep_buttons=False)

    async def _append_note(self, query, note: str, keep_buttons: bool = True) -> None:
        """Die Nachricht als quittiert markieren, statt sie zu löschen — als Beleg im Chat."""
        text = f"{query.message.text_html}\n<i>{_escape(note)}</i>"
        try:
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=query.message.reply_markup if keep_buttons else None,
            )
        except BadRequest:
            pass

    async def _clear_message(self, occurrence: Occurrence, note: str) -> None:
        """Nach /erledigt die zugehörige Nachricht entschärfen."""
        if occurrence.message_id is None:
            return
        try:
            await self._engine_holder.notifier.application.bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=occurrence.message_id, reply_markup=None
            )
        except TelegramError:
            pass
