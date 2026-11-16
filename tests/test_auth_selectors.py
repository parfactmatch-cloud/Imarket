"""Every Playwright selector string in fyers_auth_automation.py must
parse in a real browser. Regression test for run #42 (16 Nov 2026),
where a comma-combined "[role='tab']" selector broke Step 1 before
any login attempt. Skipped when Playwright/Chromium is unavailable.
"""
import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402

import fyers_auth_automation  # noqa: E402


def _selector_strings():
    source = open(fyers_auth_automation.__file__).read()
    found = set(re.findall(r'page\.locator\("([^"]+)"\)', source))
    found |= set(re.findall(r"page\.locator\('([^']+)'\)", source))
    for selectors in (
        fyers_auth_automation._TAB_SELECTORS,
        fyers_auth_automation._CLIENT_ID_INPUT_SELECTORS,
    ):
        found.update(selectors)
    return sorted(found)


def test_all_login_selectors_parse():
    selectors = _selector_strings()
    assert selectors, "no selectors found in fyers_auth_automation.py"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.set_content(
                "<button>Client ID</button>"
                "<input id='fy_client_id' name='fy_client_id'>"
                "<button id='clientIdSubmit'>Continue</button>"
                "<button>Verify</button><button>Submit</button>"
                "<input type='submit'>"
            )
            for selector in selectors:
                page.locator(selector).count()  # raises if it does not parse
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium unavailable for selector check: {exc}")
