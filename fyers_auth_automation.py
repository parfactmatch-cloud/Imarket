"""
fyers_auth_automation.py
100% Automated Auth using Fyers' own login APIs (no browser).

Why no browser: the Fyers login page gates its Continue button behind
a Cloudflare "Verify you are human" checkbox. That was verified on
16 Nov 2026 by typing the real Client ID into the live login page:
Continue stayed disabled with no error message, and the checkbox was
the only thing left undone. Headless automation cannot complete that
check, so the old Playwright flow could never get past Step 1/2.
This module instead calls the same login APIs the page itself uses
(send_login_otp -> verify_otp -> verify_pin -> token), which need no
page, no checkbox and no browser.

These vagator endpoints are NOT in Fyers' official API docs
(myapi.fyers.in/docsv3 documents only the browser login flow); they
come from community automation guides and may change without notice.
Every step names itself in its error, so a Fyers-side change shows up
as the exact step that broke instead of a generic stall.

Flow:
  1. POST api-t2.fyers.in/vagator/v2/send_login_otp
     {"fy_id": client_id, "app_id": "2"} -> request_key
  2. POST .../verify_otp {"request_key", "otp": fresh TOTP} -> request_key
     (retried once with a fresh TOTP: codes rotate every 30s)
  3. POST .../verify_pin
     {"request_key", "identity_type": "pin", "identifier": PIN}
     -> trade access_token
  4. POST api-t1.fyers.in/api/v3/token with the trade token as a
     Bearer header -> HTTP 308, body {"Url": "...?auth_code=..."}
  5. POST api-t1.fyers.in/api/v3/validate-authcode with the auth_code
     and appIdHash (SHA-256 of app_id:app_secret) -> API access_token.

Secrets (PIN, TOTP key) are never logged, not even partially.
"""

import hashlib
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import pyotp
import requests

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
# Current Fyers docs (myapi.fyers.in/docsv3, Individual App Authentication):
# exchange the auth code at POST /api/v3/validate-authcode with
# {grant_type: "authorization_code", appIdHash, code}.
TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
# Fyers access tokens expire daily. A cached token is only trusted
# when it was saved today (IST); anything older is regenerated.
REQUEST_TIMEOUT_SECONDS = 30

VAGATOR_BASE = "https://api-t2.fyers.in/vagator/v2"
URL_SEND_LOGIN_OTP = VAGATOR_BASE + "/send_login_otp"
URL_VERIFY_TOTP = VAGATOR_BASE + "/verify_otp"
URL_VERIFY_PIN = VAGATOR_BASE + "/verify_pin"
# Returns HTTP 308 with {"Url": "...?auth_code=..."}; it is NOT the
# validate-authcode endpoint (that is TOKEN_URL above).
URL_TOKEN = "https://api-t1.fyers.in/api/v3/token"
# "2" denotes the web login in the vagator login API.
VAGATOR_APP_ID = "2"


def _fyers_message(body) -> str:
    """Short error text from a Fyers JSON body, without any secret."""
    if not isinstance(body, dict):
        return ""
    for key in ("message", "msg", "error"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _post_json(url: str, payload: dict, step: str, headers=None, expect_status: int = 200) -> dict:
    """POST JSON and return the parsed body, or raise naming the step.

    The payload is never included in the error: it carries the TOTP
    and PIN. Only the HTTP status and Fyers' own message are reported.
    """
    try:
        response = requests.post(
            url, json=payload, headers=headers or {}, timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"{step} failed: network error: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != expect_status:
        detail = _fyers_message(body) or (response.text or "")[:200]
        raise RuntimeError(f"{step} failed: HTTP {response.status_code}: {detail}")
    if not isinstance(body, dict):
        raise RuntimeError(f"{step} failed: unexpected non-JSON response")
    return body


def _request_key(body: dict, step: str) -> str:
    """request_key from a vagator response (top-level or under data)."""
    key = body.get("request_key")
    if not key and isinstance(body.get("data"), dict):
        key = body["data"].get("request_key")
    if not key:
        raise RuntimeError(f"{step} failed: no request_key in Fyers response")
    return key


def _split_client_id(client_id: str) -> tuple:
    """Split an app client_id like "ABC123XYZ-100" into (app_id, app_type).

    The token step needs the two parts separately. An id without a
    dash keeps the whole id as app_id with the usual "100" type.
    """
    if "-" in client_id:
        app_id, app_type = client_id.rsplit("-", 1)
        return app_id, app_type
    return client_id, "100"


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

        auth_code = self._get_auth_code_via_api()

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

    def _get_auth_code_via_api(self) -> str:
        """Log in through Fyers' login APIs and return the auth_code."""
        # Step 1: request_key from send_login_otp.
        logger.info("[Auth] Step 1: requesting login OTP")
        body = _post_json(
            URL_SEND_LOGIN_OTP,
            {"fy_id": self.fyers_id, "app_id": VAGATOR_APP_ID},
            "Login OTP step",
        )
        request_key = _request_key(body, "Login OTP step")

        # Step 2: verify a fresh TOTP (retried once with a new code).
        logger.info("[Auth] Step 2: verifying TOTP")
        last_error = None
        for _attempt in range(2):
            totp = self._get_fresh_totp()
            try:
                body = _post_json(
                    URL_VERIFY_TOTP,
                    {"request_key": request_key, "otp": totp},
                    "TOTP step",
                )
                request_key = _request_key(body, "TOTP step")
                break
            except RuntimeError as exc:
                last_error = exc
        else:
            raise last_error
        logger.info("[Auth] TOTP accepted")

        # Step 3: verify the PIN, receiving the trade access token.
        logger.info("[Auth] Step 3: verifying PIN")
        body = _post_json(
            URL_VERIFY_PIN,
            {"request_key": request_key, "identity_type": "pin", "identifier": self.pin},
            "PIN step",
        )
        trade_token = ""
        if isinstance(body.get("data"), dict):
            trade_token = body["data"].get("access_token", "")
        if not trade_token:
            trade_token = body.get("access_token", "")
        if not trade_token:
            raise RuntimeError("PIN step failed: no access_token in Fyers response")
        logger.info("[Auth] PIN accepted")

        # Step 4: exchange the trade token for an auth_code.
        logger.info("[Auth] Step 4: requesting auth code")
        app_id, app_type = _split_client_id(self.client_id)
        try:
            response = requests.post(
                URL_TOKEN,
                json={
                    "fyers_id": self.fyers_id,
                    "app_id": app_id,
                    "redirect_uri": self.redirect_uri,
                    "appType": app_type,
                    "code_challenge": "",
                    "state": "sample_state",
                    "scope": "",
                    "nonce": "",
                    "response_type": "code",
                    "create_cookie": True,
                },
                headers={"Authorization": f"Bearer {trade_token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Auth code step failed: network error: {exc}") from exc
        try:
            token_body = response.json()
        except ValueError:
            token_body = {}
        if response.status_code not in (200, 308):
            detail = _fyers_message(token_body) or (response.text or "")[:200]
            raise RuntimeError(f"Auth code step failed: HTTP {response.status_code}: {detail}")
        redirect_url = token_body.get("Url", "") if isinstance(token_body, dict) else ""
        if not redirect_url:
            raise RuntimeError("Auth code step failed: no Url in Fyers response")
        query_params = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
        auth_code = query_params.get("auth_code", [""])[0]
        if not auth_code:
            raise RuntimeError("Auth code step failed: no auth_code in Fyers redirect URL")
        logger.info("[Auth] Auth code received, exchanging for token")
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
