"""
fyers_auth_automation.py
Hybrid Fyers Auth Manager:
1. Prioritizes direct FYERS_ACCESS_TOKEN if configured.
2. Attempts Headless Playwright Auth with anti-bot flags and prints page title/diagnostics if blocked by Cloudflare/Captcha.
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
        self.fyers_id = fyers_id.strip() if fyers_id else ""
        self.pin = pin.strip() if pin else ""
        self.totp_key = totp_key.strip().replace(" ", "") if totp_key else ""
        self.token_file = "fyers_access_token.json"

    def _get_app_id_hash(self) -> str:
        raw_str = f"{self.client_id}:{self.secret_key}"
        return hashlib.sha256(raw_str.encode('utf-8')).hexdigest()

    def generate_token(self) -> str:
        if not (self.client_id and self.secret_key and self.totp_key and self.pin and self.fyers_id):
            raise ValueError("Credentials missing in Environment Variables.")

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
            page.wait_for_timeout(4000)

            page_title = page.title()
            print(f"[Auth Debug] Loaded Page Title: '{page_title}' | URL: {page.url}")

            # Check if any input is present
            client_id_input = page.locator("input#fy_client_id:visible, input[name='fy_client_id']:visible, input[placeholder*='Client']:visible").first
            if client_id_input.count() == 0:
                body_snippet = page.inner_text("body")[:200].replace("\n", " ")
                browser.close()
                raise RuntimeError(f"Login input blocked/missing. Title: {page_title} | Content: {body_snippet}")

            client_id_input.fill(self.fyers_id)
            page.locator("#clientIdSubmit:visible, button:has-text('Continue'):visible").first.click()

            # Enter TOTP
            page.locator(".otp-container input:visible").first.wait_for(state="visible", timeout=20000)
            current_totp = pyotp.TOTP(self.totp_key).now()
            otp_inputs = page.locator(".otp-container input:visible").all()
            for i, char in enumerate(current_totp):
                otp_inputs[i].fill(char)
            page.locator("#confirmOtpSubmit:visible").first.click()

            # Enter PIN
            page.locator(".pin-container input:visible").first.wait_for(state="visible", timeout=20000)
            pin_inputs = page.locator(".pin-container input:visible").all()
            for i, char in enumerate(self.pin):
                pin_inputs[i].fill(char)
            page.locator("#verifyPinSubmit:visible").first.click()

            # Extract Auth Code
            page.wait_for_url(lambda url: "auth_code=" in url, timeout=25000)
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
