"""Best-effort harvest of ONLINE (Costco.com) orders via network capture.

Online order history uses different, less-stable endpoints than warehouse
receipts. Rather than hard-code a fragile query, we drive the logged-in browser
to the "Orders & Purchases" pages and capture every JSON response the site
fetches, saving it raw. The parser then extracts line items best-effort.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import config

# Endpoints whose JSON responses are worth keeping.
_INTERESTING = re.compile(r"(order|purchase|receipt|graphql|history)", re.I)

# Online order history landing pages to visit (the SPA lazy-loads via XHR).
_ORDER_PAGES = [
    "https://www.costco.com/OrderStatusCmd",
    "https://www.costco.com/myaccount/#/app/ordersandpurchases",
    "https://www.costco.com/myaccount/#/app/onlineorders",
]


def harvest_online_orders(
    scroll_rounds: int = 8, capture_dir: Path = config.CAPTURE_DIR
) -> dict:
    config.ensure_dirs()
    count = {"saved": 0}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=False,
            user_agent=config.USER_AGENT,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            url = resp.url
            ct = (resp.headers or {}).get("content-type", "")
            # Save any real PDF the site serves (invoices, order receipts).
            if "pdf" in ct.lower() or url.lower().split("?")[0].endswith(".pdf"):
                try:
                    data = resp.body()
                    name = re.sub(r"[^A-Za-z0-9]", "_", url.split("?")[0])[-90:]
                    (config.PDF_DIR / f"online_{name}.pdf").write_bytes(data)
                    count["saved"] += 1
                except Exception:
                    pass
                return
            if "json" not in ct or not _INTERESTING.search(url):
                return
            try:
                body = resp.json()
            except Exception:
                return
            digest = re.sub(r"[^A-Za-z0-9]", "_", url)[-80:]
            fname = capture_dir / f"{count['saved']:04d}_{digest}.json"
            try:
                fname.write_text(json.dumps({"url": url, "body": body}, indent=2))
                count["saved"] += 1
            except Exception:
                pass

        ctx.on("response", on_response)

        for url in _ORDER_PAGES:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as ex:
                print(f"  ! could not open {url}: {ex}")
                continue
            # Let the SPA fire its XHRs; scroll to trigger lazy pagination.
            for _ in range(scroll_rounds):
                page.wait_for_timeout(1500)
                try:
                    page.mouse.wheel(0, 4000)
                except Exception:
                    pass
            page.wait_for_timeout(2000)

        ctx.close()

    print(f"  Captured {count['saved']} JSON responses to {capture_dir}")
    return {"captured": count["saved"], "capture_dir": str(capture_dir)}
