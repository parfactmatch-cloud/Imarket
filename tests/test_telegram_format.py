"""Tests for the Telegram alert card format (telegram_remote_controller).

No test here touches the live Telegram API; the bot is faked.
"""
import telegram_remote_controller as tgc


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text, parse_mode))


def _controller():
    controller = object.__new__(tgc.TelegramController)
    controller.bot = FakeBot()
    controller.chat_id = "123"
    return controller


def test_icon_per_type():
    assert tgc._icon_for("System Online (Live Feed)") == "🟢"
    assert tgc._icon_for("Engine Heartbeat") == "💓"
    assert tgc._icon_for("NIFTY BUY SIGNAL") == "📈"
    assert tgc._icon_for("NIFTY SELL SIGNAL") == "📉"
    assert tgc._icon_for("Auth Error") == "🔴"
    assert tgc._icon_for("Order Rejected") == "⛔"
    assert tgc._icon_for("Session Closed") == "🏁"
    assert tgc._icon_for("Something Else") == "🔔"


def test_format_card_aligns_rows_and_carries_time():
    text = tgc.format_card(
        "Engine Heartbeat",
        [("NIFTY Spot", "25,456.35 (+165.25 vs prev close)"),
         ("Running CVD", "5,923")],
        footer_extra="Paper Mode", when="12:56")
    assert text.startswith("💓 *Engine Heartbeat*")
    assert "```" in text
    # Labels are padded to the same width, so the values line up.
    assert "NIFTY Spot  " in text
    assert "Running CVD  " in text
    assert "_12:56 IST · Jev Engine · Paper Mode_" in text


def test_two_startups_are_distinguishable():
    rows = [("Prev close", "25,291.10"),
            ("PDH / PDL", "25,337.60 / 25,172.10")]
    first = tgc.format_card("System Online (Live Feed)", rows, when="12:02")
    second = tgc.format_card("System Online (Live Feed)", rows, when="12:26")
    assert first != second
    assert "12:02 IST" in first
    assert "12:26 IST" in second


def test_format_card_strips_backticks_from_values():
    text = tgc.format_card("Session Closed", [("Note", "closed `early`")], when="15:30")
    # A backtick inside the value would break the code fence.
    assert text.count("```") == 2
    assert "closed 'early'" in text


def test_send_alert_gains_icon_and_footer():
    controller = _controller()
    controller.send_alert("Auth Error", "Token generation failed: Login OTP step")
    assert len(controller.bot.sent) == 1
    _, text, mode = controller.bot.sent[0]
    assert mode == "Markdown"
    assert text.startswith("🔴 *Auth Error*")
    assert "Token generation failed: Login OTP step" in text
    assert "IST · Jev Engine_" in text


def test_send_card_sends_formatted_card():
    controller = _controller()
    controller.send_card("Session Closed", [("Final NIFTY Spot", "25,374.05")])
    assert len(controller.bot.sent) == 1
    _, text, mode = controller.bot.sent[0]
    assert mode == "Markdown"
    assert text.startswith("🏁 *Session Closed*")
    assert "Final NIFTY Spot  25,374.05" in text
    assert "```" in text


def test_send_card_without_bot_is_a_noop():
    controller = object.__new__(tgc.TelegramController)
    controller.bot = None
    controller.chat_id = "123"
    controller.send_card("Session Closed", [("Final NIFTY Spot", "25,374.05")])
    controller.send_alert("Auth Error", "boom")
