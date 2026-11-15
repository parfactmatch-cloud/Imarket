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
TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"
# Fyers access tokens expire daily. A cached token is only trusted
# when it was saved today (IST); anything older is regenerated.
REQUEST_TIMEOUT_SECONDS = 30


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

            # Step 1: Switch to 'Client ID' tab
            client_id_tab = page.locator("text='Client ID'").first
            if client_id_tab.count() > 0:
                client_id_tab.click()
                page.wait_for_timeout(1000)

            # Step 2: Type Client ID sequentially
            client_id_input = page.locator("input#fy_client_id:visible, input[name='fy_client_id']:visible, input[type='text']:visible").first
            client_id_input.wait_for(state="visible", timeout=20000)
            client_id_input.click()
            client_id_input.press_sequentially(self.fyers_id, delay=80)
            page.wait_for_timeout(500)

            submit_btn = page.locator("#clientIdSubmit:visible, button:has-text('Continue'):visible").first
            if submit_btn.is_disabled():
                client_id_input.press("Enter")
            else:
                submit_btn.click()

            # Step 3: Enter Fresh TOTP
            page.wait_for_timeout(3500)
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

            # Step 4: Enter 4-Digit PIN
            page.wait_for_timeout(3000)
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
                    screen_msg = page.inner_text("body")[:180].replace("\n", " ")
                    browser.close()
                    raise RuntimeError(f"Stalled | Screen: {screen_msg}")

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
