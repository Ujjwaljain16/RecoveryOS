"""One-off script: capture real dashboard screenshots for the README.
Not part of the application -- run manually, not in CI. Deletable after use.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
OUT_DIR = "docs/images"

TARGETS = [
    ("control-tower", "/", None),
    ("payment-detail-replan", "/payments/{payment_id}", "REPLAN_PAYMENT_ID"),
    ("payment-detail-safety-escalation", "/payments/{payment_id}", "ESCALATION_PAYMENT_ID"),
    ("experiments", "/experiments", None),
    ("audit-explorer", "/audit", None),
]


def main() -> None:
    ids = {
        "REPLAN_PAYMENT_ID": sys.argv[1],
        "ESCALATION_PAYMENT_ID": sys.argv[2],
    }
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for name, path_template, id_key in TARGETS:
            path = path_template.format(payment_id=ids.get(id_key, "")) if id_key else path_template
            page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(800)
            page.screenshot(path=f"{OUT_DIR}/{name}.png", full_page=True)
            print(f"saved {OUT_DIR}/{name}.png ({BASE + path})")
        browser.close()


if __name__ == "__main__":
    main()
