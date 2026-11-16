"""Guard test: the Fyers auth module must not use a browser any more.

The Playwright login flow was removed on 16 Nov 2026 because the
Fyers login page gates Continue behind a Cloudflare "Verify you are
human" checkbox that headless automation cannot complete. This file
used to check every Playwright selector in the module against a real
browser; it now checks the opposite -- that no browser code crept
back in. (Kept under its old name because the GitHub integration
used here cannot delete files.)
"""
import fyers_auth_automation


def test_auth_module_has_no_browser_code():
    source = open(fyers_auth_automation.__file__).read()
    assert "from playwright" not in source
    assert "sync_playwright" not in source
    assert "page.locator" not in source
    assert "chromium.launch" not in source
