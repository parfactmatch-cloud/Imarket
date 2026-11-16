"""
fyers_auth_automation.py
100% Automated Headless Auth using Playwright.
Handles Fyers' new Login UI: Official Slash Redirect URI -> Client ID Tab -> Fresh TOTP -> Auto-Submit PIN Capture.
"""

import os
import time
import json
import hashlib
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta
import pyotp
import requests
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
# Current Fyers docs (myapi.fyers.in/docsv3, Individual App Authentication):
# exchange the auth code at POST /api/v3/validate-authcode with
# {grant_type: "authorization_code", appIdHash, code}.
TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
# Fyers access tokens expire daily. A cached token is only trusted
# when it was saved today (IST); anything older is regenerated.
REQUEST_TIMEOUT_SECONDS = 30

# Phrases Fyers shows when a login step is rejected. Checked after every
# step so a bad Client ID / TOTP / PIN is reported as that step failing,
# instead of the flow blindly typing the next secret into the wrong
# screen and stalling at the login page (seen 16 Nov 2026: the run ended
# with "Stalled | Screen: ... Login to FYERS ... Mobile number ...",
# which said nothing about which step had failed).
_ERROR_PHRASES = (
    "invalid totp",
    "invalid otp",
    "incorrect totp",
    "incorrect otp",
    "incorrect pin",
    "invalid pin",
    "wrong pin",
    "wrong otp",
    "otp expired",
    "totp expired",
    "invalid client id",
    "invalid login",
    "login failed",
    "authentication failed",
    "too many attempts",
    "try again",
)


def _body_text(page) -> str:
    """Visible body text, whitespace-collapsed. Never includes input values."""
    try:
        return " ".join(page.inner_text("body").split())
    except Exception:
        return ""


def _safe_url(url: str) -> str:
    """Login URL with the client_id value redacted. The raw URL was
    previously copied into Telegram alerts, broadcasting the app ID."""
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        redacted = [(k, "***" if k == "client_id" else v) for k, v in params]
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(redacted)))
    except Exception:
        return url


def _screen_summary(page, limit: int = 500) -> str:
    try:
        url = _safe_url(page.url)
    except Exception:
        url = "?"
    return f"URL: {url} | Screen: {_body_text(page)[:limit]}"


def _find_error_text(body_text: str) -> str | None:
    lowered = body_text.lower()
    for phrase in _ERROR_PHRASES:
        if phrase in lowered:
            start = lowered.find(phrase)
            return body_text[max(0, start - 40):start + 120].strip()
    return None


def _raise_if_error(page, step: str) -> None:
    error = _find_error_text(_body_text(page))
    if error:
        raise RuntimeError(f"{step} failed: {error} | {_screen_summary(page, 300)}")


def _wait_for_text(page, pattern: str, step: str, timeout_ms: int = 15000) -> str:
    """Poll the page until ``pattern`` (case-insensitive) appears, or raise
    naming the step that did not advance and the screen it is stuck on."""
    import re

    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        text = _body_text(page)
        _raise_if_error(page, step)
        if re.search(pattern, text, re.IGNORECASE):
            return text
        if "auth_code=" in getattr(page, "url", ""):
            return text
        time.sleep(0.5)
    raise RuntimeError(
        f"{step} did not advance within {timeout_ms // 1000}s | {_screen_summary(page)}"
    )


# Selectors used on the Fyers login page. Kept as separate strings and
# tried in order (see _first_visible): a comma-combined selector is NOT
# used here because Playwright's CSS parser rejects "[role='tab']"
# inside one ("Unexpected token '='", seen in run #42 on 16 Nov 2026)
# and one bad part breaks the whole locator.
_TAB_SELECTORS = (
    "button:has-text('Client ID'):visible",
    "text='Client ID'",
)
_CLIENT_ID_INPUT_SELECTORS = (
    "input#fy_client_id:visible",
    "input[name='fy_client_id']:visible",
)


def _first_visible(page, selectors):
    """First locator with a visible match, or None. A selector that
    fails to parse is skipped instead of breaking the login."""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return None


def _wait_for_enabled(locator, timeout_ms: int = 5000, poll_ms: int = 250) -> bool:
    """Poll until the locator's element is enabled. Fyers validates the
    Client ID as you type (possibly with an async check), so the button
    can still be disabled right after typing even for a good ID."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if not locator.is_disabled():
                return True
        except Exception:
            return False
        time.sleep(poll_ms / 1000.0)
    try:
        return not locator.is_disabled()
    except Exception:
        return False


class FyersAuthManager:
    def __init__(self, client_id: str, secret_key: str, redirect_uri: str, fyers_id: str, pin: str, totp_key: str):
        self.client_id = client_id.strip() if client_id else ""
        self.secret_key = secret_key.strip() if secret_key else ""
        env_redirect = os.environ.get("FYERS_REDIRECT_URI", "").strip()
        default_redirect = "https://trade.fyers.in/api-login/redirect-uri/index.html"
        self.redirect_uri = env_redirect if env_redirect else (redirect_uri.strip() if redirect_uri else default_redirect)
        self.fyers_id = fyers_id.strip().upper() if fyers_id else ""
        self.pin = pin.strip() if pin else ""
        self.totp_key = totp_key.strip().replace(" ", "") if totp_key else ""
        self.token_file = "fyers_access_token.json"

    def _get_app_id_hash(self) -> str:
        raw_str = f"{self.client_id}:{self.secret_key}"
        return hashlib.sha256(raw_str.encode('utf-8')).hexdigest()

    def _get_fresh_totp(self) -> str:
        remaining = 30 - (int(time.time()) % 30)
        if remaining < 5:
            time.sleep(remaining + 1)
        return pyotp.TOTP(self.totp_key).now()

    def generate_token(self) -> str:
        if not (self.client_id and self.secret_key and self.totp_key and self.pin and self.fyers_id):
            raise ValueError("Missing Secrets in GitHub Environment.")

        auth_code = self._get_auth_code_via_browser()

        app_id_hash = self._get_app_id_hash()
        token_payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code
        }
        res = requests.post(
            TOKEN_URL, json=token_payload, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if res.status_code != 200:
            raise RuntimeError(
                f"Token exchange failed: HTTP {res.status_code}: {res.text[:200]}"
            )
        try:
            res_json = res.json()
        except ValueError as exc:
            raise RuntimeError(f"Token exchange returned non-JSON: {exc}") from exc
        access_token = res_json.get("access_token", "")

        if not access_token:
            raise RuntimeError(f"Token exchange failed: {res_json}")

        with open(self.token_file, "w") as f:
            # saved_at is how get_valid_token knows the token's age.
            json.dump(
                {
                    "access_token": access_token,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
            )

        return access_token

    def _get_auth_code_via_browser(self) -> str:
        encoded_redirect = urllib.parse.quote(self.redirect_uri, safe="")
        login_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={self.client_id}&redirect_uri={encoded_redirect}"
            f"&response_type=code&state=sample_state"
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            initial_text = page.inner_text("body")
            if "invalid redirect" in initial_text.lower() or "invalid client" in initial_text.lower():
                browser.close()
                raise RuntimeError(f"App Config Error: {initial_text[:150].replace(chr(10), ' ')}")

            # Step 1: Switch to 'Client ID' tab, then prove the tab opened
            # by waiting for its specific input. The old code fell back
            # to the first visible text input, which on the initial
            # screen is the Mobile number field, so a failed tab switch
            # silently typed the Client ID into the wrong field.
            logger.info("[Auth] Step 1: opening Client ID tab")
            client_id_tab = _first_visible(page, _TAB_SELECTORS)
            if client_id_tab is not None:
                client_id_tab.first.click()
            client_id_input_loc = _first_visible(page, _CLIENT_ID_INPUT_SELECTORS)
            if client_id_input_loc is None:
                browser.close()
                raise RuntimeError(
                    f"Client ID tab did not open | {_screen_summary(page)}"
                )
            try:
                client_id_input_loc.first.wait_for(state="visible", timeout=10000)
            except Exception as exc:
                browser.close()
                raise RuntimeError(
                    f"Client ID tab did not open | {_screen_summary(page)}"
                ) from exc

            # Step 2: Type Client ID sequentially
            logger.info("[Auth] Step 2: entering Client ID")
            client_id_input = client_id_input_loc.first
            client_id_input.click()
            client_id_input.press_sequentially(self.fyers_id, delay=80)
            page.wait_for_timeout(500)
            # Confirm the field actually registered the typing (length
            # only, never the value): if the page's script did not pick
            # the keystrokes up, Continue stays disabled and Enter does
            # nothing, which looks exactly like the run #43 stall.
            try:
                typed_len = len(client_id_input.input_value())
            except Exception:
                typed_len = -1
            logger.info("[Auth] Client ID field length after typing: %s", typed_len)
            # What format does Fyers expect? Log the field's own hints
            # (maxlength / pattern / placeholder). These are page chrome,
            # not the entered value.
            try:
                field_attrs = {
                    name: client_id_input.get_attribute(name)
                    for name in ("maxlength", "pattern", "placeholder", "inputmode")
                }
            except Exception:
                field_attrs = {}
            logger.info("[Auth] Client ID field attrs: %s", field_attrs)

            submit_btn = page.locator("#clientIdSubmit:visible, button:has-text('Continue'):visible").first
            try:
                submit_desc = (
                    f"id={submit_btn.get_attribute('id')} "
                    f"text={submit_btn.inner_text().strip()[:30]!r} "
                    f"disabled={submit_btn.is_disabled()}"
                )
            except Exception:
                submit_desc = "unreadable"
            logger.info("[Auth] Client ID submit button: %s", submit_desc)
            # Blur the field (some pages validate on blur) and give Fyers
            # a few seconds to enable Continue: run #45 showed it still
            # disabled 500ms after typing, which may just be an async
            # check still in flight rather than a rejected ID.
            try:
                client_id_input.press("Tab")
            except Exception:
                pass
            enabled = _wait_for_enabled(submit_btn, timeout_ms=5000)
            logger.info("[Auth] Client ID submit button enabled after wait: %s", enabled)
            if not enabled:
                browser.close()
                raise RuntimeError(
                    "Client ID Continue button is disabled after typing "
                    f"(field length {typed_len}) and 5s wait: Fyers did not "
                    "accept the entered ID, or the field did not register "
                    f"the typing | {_screen_summary(page)}"
                )
            submit_btn.click()
            logger.info("[Auth] Client ID submitted, waiting for OTP screen")

            # Prove the Client ID was accepted: the next screen must
            # mention TOTP/OTP. If Fyers stays on the login screen, stop
            # here with that fact instead of typing the TOTP into the
            # mobile-number field and stalling at the end.
            try:
                _wait_for_text(page, r"TOTP|OTP|verification", "Client ID step", timeout_ms=15000)
            except RuntimeError as exc:
                browser.close()
                raise RuntimeError(
                    f"{exc} | No Fyers error message appeared, so the submit "
                    "was not rejected with a message: the page simply did not "
                    "move (field did not register the ID, or Fyers ignored "
                    "the submit from this runner)"
                ) from exc
            _raise_if_error(page, "Client ID step")
            logger.info("[Auth] Client ID accepted, OTP screen reached")
            # Step 3: Enter Fresh TOTP
            logger.info("[Auth] Step 3: entering TOTP")
            totp_switch = page.locator("text=/TOTP/i").first
            if totp_switch.count() > 0 and totp_switch.is_visible():
                try:
                    totp_switch.click()
                    page.wait_for_timeout(1000)
                except Exception as exc:
                    logger.warning("TOTP switch click failed (continuing): %s", exc)

            visible_inputs = page.locator("input:visible").all()
            if not visible_inputs:
                screen_text = page.inner_text("body")[:180].replace("\n", " ")
                browser.close()
                raise RuntimeError(f"No inputs on TOTP step. Screen says: {screen_text}")

            current_totp = self._get_fresh_totp()
            if len(visible_inputs) >= 6:
                for i in range(6):
                    visible_inputs[i].click()
                    visible_inputs[i].press_sequentially(current_totp[i], delay=50)
            else:
                visible_inputs[0].click()
                visible_inputs[0].press_sequentially(current_totp, delay=60)

            page.wait_for_timeout(1500)
            if "auth_code=" not in page.url:
                submit_otp = page.locator("#confirmOtpSubmit:visible, button:has-text('Continue'):visible, button:has-text('Verify'):visible, button[type='submit']:visible").first
                if submit_otp.count() > 0 and not submit_otp.is_disabled():
                    submit_otp.click()

            # Prove the TOTP was accepted: the next screen must mention
            # PIN. If Fyers rejects the TOTP it returns to the login /
            # OTP screen, and the error check above names the reason.
            _wait_for_text(page, r"PIN|auth_code=", "TOTP step", timeout_ms=15000)
            _raise_if_error(page, "TOTP step")
            logger.info("[Auth] TOTP accepted, PIN screen reached")

            # Step 4: Enter 4-Digit PIN
            logger.info("[Auth] Step 4: entering PIN")
            pin_inputs = page.locator("input:visible").all()
            if not pin_inputs:
                screen_text = page.inner_text("body")[:180].replace("\n", " ")
                browser.close()
                raise RuntimeError(f"No inputs on PIN step. Screen says: {screen_text}")

            if len(pin_inputs) >= 4:
                for i in range(4):
                    pin_inputs[i].click()
                    pin_inputs[i].press_sequentially(self.pin[i], delay=50)
            else:
                pin_inputs[0].click()
                pin_inputs[0].press_sequentially(self.pin, delay=60)

            # Allow 2.5s for Fyers auto-submit on 4th PIN digit
            page.wait_for_timeout(2500)
            if "auth_code=" not in page.url:
                submit_pin = page.locator("#verifyPinSubmit:visible, button:has-text('Continue'):visible, button:has-text('Verify'):visible, button:has-text('Submit'):visible, button[type='submit']:visible").first
                if submit_pin.count() > 0 and not submit_pin.is_disabled():
                    submit_pin.click()

            # Step 5: Wait for Redirect & Extract Auth Code immediately upon URL commit
            try:
                page.wait_for_url(lambda url: "auth_code=" in url, wait_until="commit", timeout=20000)
            except Exception as exc:
                logger.warning("Timed out waiting for auth_code redirect: %s", exc)
                curr_url = page.url
                if "auth_code=" not in curr_url:
                    _raise_if_error(page, "PIN step")
                    browser.close()
                    raise RuntimeError(
                        f"Stalled after PIN step, no auth_code redirect | {_screen_summary(page)}"
                    ) from exc

            final_url = page.url
            browser.close()
            logger.info("[Auth] Auth code received, exchanging for token")

            final_url = page.url
            browser.close()

            parsed = urllib.parse.urlparse(final_url)
            query_params = urllib.parse.parse_qs(parsed.query)
            auth_code = query_params.get("auth_code", [""])[0]

            if not auth_code:
                raise RuntimeError("Failed to extract auth_code from redirected URL.")

            return auth_code

    def get_valid_token(self) -> str:
        env_token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
        if env_token:
            return env_token

        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    data = json.load(f)
                    tok = data.get("access_token")
                    saved_at = data.get("saved_at")
                    if tok and self._token_saved_today(saved_at):
                        return tok
                    # A token from a previous day (or with no saved date)
                    # is expired: Fyers tokens last one day. Fall through
                    # to a fresh login instead of reusing it.
                    logger.info("Cached Fyers token is stale; generating a fresh token")
            except (OSError, ValueError) as exc:
                logger.warning("Could not read cached token file: %s", exc)
        return self.generate_token()

    @staticmethod
    def _token_saved_today(saved_at) -> bool:
        """True only when ``saved_at`` (ISO timestamp) is today in IST."""
        if not saved_at:
            return False
        try:
            saved = datetime.fromisoformat(str(saved_at))
        except ValueError:
            return False
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=timezone.utc)
        return saved.astimezone(IST).date() == datetime.now(IST).date()
