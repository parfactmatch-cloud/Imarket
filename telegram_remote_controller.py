"""
telegram_remote_controller.py
Sub-second Telegram Bot Interface for Remote Engine Monitoring and Kill-Switch.
"""

import logging

from engine_state_store import EngineStateStore
from execution_broker_interface import ExecutionRouter

logger = logging.getLogger(__name__)


# One icon per alert type, so the feed is scannable at a glance instead of
# every message starting with the same bell.
ALERT_ICONS = {
    "System Online": "🟢",
    "Engine Heartbeat": "💓",
    "BUY SIGNAL": "📈",
    "SELL SIGNAL": "📉",
    "Auth Error": "🔴",
    "Levels Unavailable": "🔴",
    "Order Rejected": "⛔",
    "Position Update": "🔄",
    "Quote Unavailable": "⚠️",
    "End-of-Day Square-Off": "🛑",
    "Session Closed": "🏁",
    "System Shutdown": "🛑",
}


def _icon_for(title: str) -> str:
    for key, icon in ALERT_ICONS.items():
        if key in title:
            return icon
    return "🔔"


def _ist_now_str() -> str:
    """Current time as 'HH:MM' in IST, for the alert footer."""
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%H:%M")


def format_card(title: str, rows, *, footer_extra=None, when=None) -> str:
    """Render an alert as a card: icon + bold title, a monospace block of
    aligned label/value rows, and a footer with the IST send time.

    The footer time is what tells two otherwise identical messages apart
    (e.g. two 'System Online' startups on the same day). Every card shows
    the live value at the moment it was sent; nothing is copied from a
    stale snapshot.
    """
    icon = _icon_for(title)
    when = when or _ist_now_str()
    label_width = max((len(str(label)) for label, _ in rows), default=0)
    lines = []
    for label, value in rows:
        value = str(value).replace("`", "'")
        lines.append(f"{str(label):<{label_width}}  {value}")
    body = "\n".join(lines)
    footer = f"_{when} IST · Jev Engine"
    if footer_extra:
        footer += f" · {footer_extra}"
    footer += "_"
    return f"{icon} *{title}*\n```\n{body}\n```\n{footer}"


class TelegramController:
    def __init__(self, bot_token: str, chat_id: str, state_store: EngineStateStore, router: ExecutionRouter):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.store = state_store
        self.router = router
        self.bot = None

        if bot_token and bot_token != "YOUR_TELEGRAM_BOT_TOKEN":
            try:
                import telebot
                self.bot = telebot.TeleBot(bot_token)
                self._register_handlers()
            except ImportError:
                logger.warning("[TelegramController] pyTelegramBotAPI not installed. Telegram disabled.")

    def _register_handlers(self):
        @self.bot.message_handler(commands=['status'])
        def handle_status(message):
            if str(message.chat.id) != self.chat_id:
                return
            mode = "PAPER TRADING" if self.router.paper_mode else "LIVE REAL FUNDS"
            system_active = self.store.get_system_flag("SYSTEM_PAUSED", "0") == "0"
            state = "RUNNING" if system_active else "PAUSED"
            open_pos = self.store.load_open_positions()

            text = (
                f"📊 *Jev Engine Status*\n"
                f"• Execution Mode: `{mode}`\n"
                f"• State: `{state}`\n"
                f"• Open Positions: `{len(open_pos)}`\n"
                f"• Virtual Balance: `₹{self.router.virtual_cash:.2f}`\n"
            )
            for sym, pos in open_pos.items():
                text += f"\n👉 `{sym}` | Qty: {pos.qty} | SL: {pos.stop_loss}"

            self.bot.reply_to(message, text, parse_mode="Markdown")

        @self.bot.message_handler(commands=['pause'])
        def handle_pause(message):
            if str(message.chat.id) != self.chat_id:
                return
            self.store.set_system_flag("SYSTEM_PAUSED", "1")
            self.bot.reply_to(message, "⏸️ *Engine PAUSED:* New trade entries blocked.", parse_mode="Markdown")

        @self.bot.message_handler(commands=['resume'])
        def handle_resume(message):
            if str(message.chat.id) != self.chat_id:
                return
            self.store.set_system_flag("SYSTEM_PAUSED", "0")
            self.bot.reply_to(message, "▶️ *Engine RESUMED:* Signal execution active.", parse_mode="Markdown")

        @self.bot.message_handler(commands=['panic'])
        def handle_panic(message):
            if str(message.chat.id) != self.chat_id:
                return
            self.store.set_system_flag("SYSTEM_PAUSED", "1")
            self.router.emergency_panic_close_all({})
            self.bot.reply_to(message, "🚨 *PANIC TRIGGERED:* All positions closed and engine paused.", parse_mode="Markdown")

    def send_alert(self, title: str, details: str):
        if self.bot and self.chat_id:
            try:
                msg = (
                    f"{_icon_for(title)} *{title}*\n{details}\n"
                    f"_{_ist_now_str()} IST · Jev Engine_"
                )
                self.bot.send_message(self.chat_id, msg, parse_mode="Markdown")
            except Exception as e:
                logger.warning("[Telegram Error] %s", e)
        else:
            logger.info("[ALERT: %s] %s", title, details)

    def send_card(self, title: str, rows, footer_extra=None):
        """Send a formatted card (see format_card)."""
        if self.bot and self.chat_id:
            try:
                self.bot.send_message(
                    self.chat_id,
                    format_card(title, rows, footer_extra=footer_extra),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning("[Telegram Error] %s", e)
        else:
            logger.info(
                "[ALERT: %s]\n%s",
                title,
                format_card(title, rows, footer_extra=footer_extra),
            )

    def run_polling(self):
        if self.bot:
            self.bot.infinity_polling(timeout=10, long_polling_timeout=5)
