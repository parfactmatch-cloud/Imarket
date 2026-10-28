"""
fyers_auth_automation.py
Production Headless Daily Token Generation using TOTP and SHA-256 AppIdHash.
Zero Manual Intervention with Detailed Logging and Fallback Safety.
"""

import json
import os
import hashlib
import pyotp
import requests


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
        """
        Fyers API v3 requires appIdHash: SHA-256(client_id:secret_key)
        """
        raw_str = f"{self.client_id}:{self.secret_key}"
        return hashlib.sha256(raw_str.encode('utf-8')).hexdigest()

    def generate_token(self) -> str:
        if not (self.client_id and self.secret_key and self.totp_key and self.pin and self.fyers_id):
            raise ValueError("Incomplete credentials: Ensure FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_ID, FYERS_PIN, FYERS_TOTP_KEY are set.")

        totp = pyotp.TOTP(self.totp_key)
        current_totp = totp.now()
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

        # Step 1: Request auth code
        res1 = session.post(
            "https://api-t1.fyers.in/api/v3/generate-authcode",
            json={"fy_id": self.fyers_id, "app_id": "2"}
        )
        if res1.status_code != 200 or not res1.text.strip():
            raise RuntimeError(f"Step 1 Failed: Status {res1.status_code}, Body: {res1.text[:200]}")

        try:
            res1_json = res1.json()
        except Exception:
            raise RuntimeError(f"Step 1 Non-JSON Response: {res1.text[:200]}")

        request_key = res1_json.get("request_key")
        if not request_key:
            raise RuntimeError(f"Step 1 Missing request_key: {res1_json}")

        # Step 2: Verify TOTP
        res2 = session.post(
            "https://api-t1.fyers.in/api/v3/verify-totp",
            json={"request_key": request_key, "otp": current_totp}
        )
        if res2.status_code != 200 or not res2.text.strip():
            raise RuntimeError(f"Step 2 Failed: Status {res2.status_code}, Body: {res2.text[:200]}")

        try:
            res2_json = res2.json()
        except Exception:
            raise RuntimeError(f"Step 2 Non-JSON Response: {res2.text[:200]}")

        request_key_2 = res2_json.get("request_key")
        if not request_key_2:
            raise RuntimeError(f"Step 2 Missing request_key_2: {res2_json}")

        # Step 3: Verify PIN
        res3 = session.post(
            "https://api-t1.fyers.in/api/v3/verify-pin",
            json={"request_key": request_key_2, "identity_type": "pin", "identifier": self.pin}
        )
        if res3.status_code != 200 or not res3.text.strip():
            raise RuntimeError(f"Step 3 Failed: Status {res3.status_code}, Body: {res3.text[:200]}")

        try:
            res3_json = res3.json()
        except Exception:
            raise RuntimeError(f"Step 3 Non-JSON Response: {res3.text[:200]}")

        auth_code = res3_json.get("data", {}).get("auth_code")
        if not auth_code:
            raise RuntimeError(f"Step 3 Missing auth_code: {res3_json}")

        # Step 4: Token Exchange with SHA-256 AppIdHash
        app_id_hash = self._get_app_id_hash()
        token_payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code
        }
        res4 = session.post("https://api-t1.fyers.in/api/v3/token", json=token_payload)
        if res4.status_code != 200 or not res4.text.strip():
            raise RuntimeError(f"Step 4 Failed: Status {res4.status_code}, Body: {res4.text[:200]}")

        try:
            res4_json = res4.json()
        except Exception:
            raise RuntimeError(f"Step 4 Non-JSON Response: {res4.text[:200]}")

        access_token = res4_json.get("access_token", "")
        if not access_token:
            raise RuntimeError(f"Step 4 Token generation failed: {res4_json}")

        with open(self.token_file, "w") as f:
            json.dump({"access_token": access_token}, f)

        return access_token

    def get_valid_token(self) -> str:
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
