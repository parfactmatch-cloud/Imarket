"""
fyers_auth_automation.py
100% Automated Headless Auth using Playwright.
Handles Fyers' new Login UI: Client ID Tab -> Sequential Typing -> Universal TOTP/PIN Detection -> Instant URL Commit Capture.
"""

import os
import json
import hashlib
import urllib.parse
import pyotp
import requests
from playwright.sync_api import sync_playwright


class FyersAuthManager:
    def __init__(self, client_id: str, secret_key: str, redirect_uri: str, fyers_id: str, pin: str, totp_key: str):
        self.client_id = client_id.strip() if client_id else ""
        self.secret_key = secret_key.strip() if secret_key else ""
        self.redirect_uri = redirect_uri.strip() if redirect_uri else ""
        self.fyers_id = fyers_id.strip().upper() if fyers_id else ""
        self.pin = pin.strip() if pin else ""
        self.totp_key = totp_key.strip().replace(" ", "") if totp_key else ""
        self.token_file = "fyers_access_token.json"

    def _get_app_id_hash(self) -> str:
        raw_str = f"{self.client_id}:{self.secret_key}"
        return hashlib.sha256(raw_str.encode('utf-8')).hexdigest()

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
        res = requests.post("https://api-t1.fyers.in/api/v3/token", json=token_payload)
        res_json = res.json()
        access_token = res_json.get("access_token", "")

        if not access_token:
            raise RuntimeError(f"Token exchange failed: {res_json}")

        with open(self.token_file, "w") as f:
            json.dump({"access_token": access_token}, f)

        return access_token

    def _get_auth_code_via_browser(self) -> str:
        login_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={self.client_id}&redirect_uri={self.redirect_uri}"
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

            # Wait for Step 3 screen transition
            page.wait_for_timeout(3500)

            # Check if there is a "Verify with TOTP" or "Enter TOTP" option button
            totp_switch = page.locator("text=/TOTP/i").first
            if totp_switch.count() > 0 and totp_switch.is_visible():
                try:
                    totp_switch.click()
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

            # Step 3: Universal TOTP input detection (6 single boxes OR 1 combined box)
            visible_inputs = page.locator("input:visible").all()
            if not visible_inputs:
                screen_text = page.inner_text("body")[:180].replace("\n", " ")
                browser.close()
                raise RuntimeError(f"No inputs on TOTP step. Screen says: {screen_text}")

            current_totp = pyotp.TOTP(self.totp_key).now()
            if len(visible_inputs) >= 6:
                for i in range(6):
                    visible_inputs[i].click()
                    visible_inputs[i].press_sequentially(current_totp[i], delay=50)
            else:
                visible_inputs[0].click()
                visible_inputs[0].press_sequentially(current_totp, delay=60)

            page.wait_for_timeout(600)
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

            page.wait_for_timeout(600)
            submit_pin = page.locator("#verifyPinSubmit:visible, button:has-text('Continue'):visible, button:has-text('Verify'):visible, button:has-text('Submit'):visible, button[type='submit']:visible").first
            if submit_pin.count() > 0 and not submit_pin.is_disabled():
                submit_pin.click()

            # Step 5: Wait for Redirect & Extract Auth Code immediately upon URL commit
            try:
                page.wait_for_url(lambda url: "auth_code=" in url, wait_until="commit", timeout=25000)
            except Exception:
                curr_url = page.url
                if "auth_code=" not in curr_url:
                    screen_msg = page.inner_text("body")[:150].replace("\n", " ")
                    browser.close()
                    raise RuntimeError(f"Redirect stalled at {curr_url[:60]} | Msg: {screen_msg}")

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
                    if tok:
                        return tok
            except Exception:
                pass
        return self.generate_token()
