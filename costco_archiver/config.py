"""Shared configuration and paths."""
from __future__ import annotations

import os
from pathlib import Path

# Root of the repo (parent of this package).
ROOT = Path(__file__).resolve().parent.parent

# Persistent Playwright profile — keeps you logged in between runs.
# Lives outside version control (see .gitignore).
PROFILE_DIR = ROOT / ".costco_profile"

# Where downloaded artifacts land.
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"           # raw JSON responses, one file per receipt/order
CAPTURE_DIR = DATA_DIR / "captured"  # raw network captures (fallback harvest)
OUTPUT_DIR = DATA_DIR / "output"     # parsed CSV / summaries

# Exact request headers captured from the browser's own receipts call. Preferred
# over reconstructed headers because they include everything the API expects.
# Tokens live ~15 min, so this file is short-lived by nature.
API_HEADERS_FILE = DATA_DIR / "api_headers.json"

# Costco endpoints (undocumented; reverse-engineered from the site's own calls).
GRAPHQL_URL = "https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql"
SIGNIN_URL = "https://www.costco.com/LogonForm"
ACCOUNT_URL = "https://www.costco.com/OrderStatusCmd"
RECEIPTS_URL = "https://www.costco.com/myaccount/#/app/receipts"
ORDERS_URL = "https://www.costco.com/OrderStatusCmd"

# This client-identifier is a public constant baked into Costco's web app.
CLIENT_IDENTIFIER = os.environ.get(
    "COSTCO_CLIENT_IDENTIFIER", "481b1aec-aa3b-454b-b81b-48187e28f205"
)

# A modern desktop UA reduces the chance of being served a degraded/blocked page.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, CAPTURE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
