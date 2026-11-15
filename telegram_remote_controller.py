"""
telegram_remote_controller.py
Sub-second Telegram Bot Interface for Remote Engine Monitoring and Kill-Switch.
"""

import logging

from engine_state_store import EngineStateStore
from execution_broker_interface import ExecutionRouter

logger = logging.getLogger(__name__)


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
                msg = f"🔔 *{title}*\n{details}"
                self.bot.send_message(self.chat_id, msg, parse_mode="Markdown")
            except Exception as e:
                logger.warning("[Telegram Error] %s", e)
        else:
            logger.info("[ALERT: %s] %s", title, details)

    def run_polling(self):
        if self.bot:
            self.bot.infinity_polling(timeout=10, long_polling_timeout=5)
