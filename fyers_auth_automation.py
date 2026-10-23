"""
fyers_auth_automation.py
Headless Daily Token Generation using TOTP (Zero Manual Intervention).
"""

import json
import os
import pyotp
import requests


class FyersAuthManager:
    def __init__(self, client_id: str, secret_key: str, redirect_uri: str, fyers_id: str, pin: str, totp_key: str):
        self.client_id = client_id
        self.secret_key = secret_key
        self.redirect_uri = redirect_uri
        self.fyers_id = fyers_id
        self.pin = pin
        self.totp_key = totp_key
        self.token_file = "fyers_access_token.json"

    def generate_token(self) -> str:
        totp = pyotp.TOTP(self.totp_key)
        current_totp = totp.now()
        session = requests.Session()

        # Step 1: Request auth code
        res1 = session.post(
            "https://api-t1.fyers.in/api/v3/generate-authcode",
            json={"fy_id": self.fyers_id, "app_id": "2"}
        )
        request_key = res1.json().get("request_key")

        # Step 2: Verify TOTP
        res2 = session.post(
            "https://api-t1.fyers.in/api/v3/verify-totp",
            json={"request_key": request_key, "otp": current_totp}
        )
        request_key_2 = res2.json().get("request_key")

        # Step 3: Verify PIN
        res3 = session.post(
            "https://api-t1.fyers.in/api/v3/verify-pin",
            json={"request_key": request_key_2, "identity_type": "pin", "identifier": self.pin}
        )
        auth_code = res3.json()["data"]["auth_code"]

        # Step 4: Token exchange
        res4 = session.post(
            "https://api-t1.fyers.in/api/v3/token",
            json={"grant_type": "authorization_code", "appIdHash": self.secret_key, "code": auth_code}
        )
        access_token = res4.json().get("access_token", "")

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
