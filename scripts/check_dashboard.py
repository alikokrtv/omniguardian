"""Browser smoke test against a running dashboard; requires the browser-test extra."""

import argparse
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def run(base_url: str, api_key: str, channel: str | None, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(base_url.rstrip("/") + "/dashboard")
            expect(page.locator("#login")).to_be_visible()
            page.locator("#api-key").fill(api_key)
            page.get_by_role("button", name="Connect workspace").click()
            expect(page.locator("#workspace")).to_be_visible(timeout=15000)
            expect(page.locator("#connection-label")).to_have_text("Connected · live")
            expect(page.locator("#inventory-rows tr").first).to_be_visible()
            page.screenshot(path=str(output / "overview.png"), full_page=True)
            page.get_by_role("button", name="Audit trail", exact=False).first.click()
            expect(page.locator("#audit-view")).to_be_visible()
            expect(page.locator("#audit-rows tr").first).to_be_visible()
            page.locator("#audit-sku").fill("NONEXISTENT-UI-CHECK-SKU")
            page.get_by_role("button", name="Apply filters").click()
            expect(page.locator("#audit-rows")).to_contain_text("No audit records")
            page.locator("#audit-sku").fill("")
            page.get_by_role("button", name="Apply filters").click()
            page.get_by_role("button", name="Plan & usage", exact=False).click()
            expect(page.locator("#billing-view")).to_be_visible()
            expect(page.locator("#plan-name")).not_to_have_text("—")
            page.screenshot(path=str(output / "billing.png"), full_page=True)
            page.get_by_role("button", name="Overview", exact=False).click()
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#overview-view")).to_be_visible()
            page.screenshot(path=str(output / "mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
            page.get_by_role("button", name="Disconnect", exact=True).click()
            expect(page.locator("#workspace")).to_be_hidden()
            expect(page.locator("#api-key")).to_have_value("")
            page.locator("#api-key").fill("invalid-test-key")
            page.get_by_role("button", name="Connect workspace").click()
            expect(page.locator("#error")).to_contain_text("invalid or has been revoked")
            expect(page.locator("#workspace")).to_be_hidden()
            assert not errors, errors
            print(
                "PASS: authenticated overview, audit filters, billing, mobile layout, logout, "
                "invalid-key handling, and no JavaScript exceptions"
            )
            print(f"Screenshots: {output.resolve()}")
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--api-key", default=os.getenv("DEMO_API_KEY", "local-demo-key-change-before-deploying")
    )
    parser.add_argument("--browser-channel", default=None, help="e.g. msedge or chrome")
    parser.add_argument("--output-dir", type=Path, default=Path(".tools/ui-check"))
    args = parser.parse_args()
    run(args.base_url, args.api_key, args.browser_channel, args.output_dir)
