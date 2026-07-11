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
RAW_DIR = DATA_DIR / "raw"           # raw JSON responses, one file per receipt
OUTPUT_DIR = DATA_DIR / "output"     # parsed CSV / summaries
PDF_DIR = DATA_DIR / "pdfs"          # per-receipt PDF archive (+ captured PDFs)
BACKUP_DIR = DATA_DIR / "backups"    # compressed (.tar.gz) snapshots of raw receipts

# Exact request headers captured from the browser's own receipts call. Preferred
# over reconstructed headers because they include everything the API expects.
# Tokens live ~15 min, so this file is short-lived by nature.
API_HEADERS_FILE = DATA_DIR / "api_headers.json"

# Exact request (url + POST body) captured from the browser's receipts call, so
# fetch can replay Costco's own GraphQL query/variables verbatim across date
# windows instead of relying on a hard-coded query that may drift.
API_REQUEST_FILE = DATA_DIR / "api_request.json"

# Decoded token/clientid cache (short-lived; token ~15 min).
CRED_CACHE_FILE = DATA_DIR / "credentials.json"

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

# Web server host/port. Configurable via env (COSTCO_WEB_HOST / COSTCO_WEB_PORT,
# or the generic PORT). The `web --port/--host` flags override these.
WEB_HOST = os.environ.get("COSTCO_WEB_HOST", "127.0.0.1")
try:
    WEB_PORT = int(os.environ.get("COSTCO_WEB_PORT") or os.environ.get("PORT") or 8000)
except ValueError:
    WEB_PORT = 8000

# --- Web authentication (password + TOTP MFA) --------------------------------
# Account store for the web UI (usernames, password hashes, TOTP secrets, and a
# reserved slot for future passkeys). Local-only; keep it out of version control.
WEB_USERS_FILE = DATA_DIR / "web_users.json"

# Label shown in authenticator apps when enrolling TOTP.
AUTH_ISSUER = os.environ.get("COSTCO_AUTH_ISSUER", "Costco Receipt Archiver")

# Session lifetime (seconds) and cookie hardening. Set COSTCO_WEB_HTTPS=1 when
# serving over TLS (directly or behind a proxy) so the session cookie is marked
# Secure. Sessions live in server memory, so a restart signs everyone out.
try:
    SESSION_TTL_SECONDS = int(os.environ.get("COSTCO_SESSION_TTL") or 43200)  # 12h
except ValueError:
    SESSION_TTL_SECONDS = 43200
COOKIE_SECURE = (os.environ.get("COSTCO_WEB_HTTPS", "").lower()
                 in ("1", "true", "yes", "on"))

# --- Automatic backups --------------------------------------------------------
# The web server runs a background scheduler that snapshots data/raw on an
# interval (default daily) and keeps the newest N automatic backups. Manual /
# labelled backups are never pruned. A tick is a no-op when the raw data hasn't
# changed since the last backup, so identical archives aren't piled up.
BACKUP_DAILY = (os.environ.get("COSTCO_BACKUP_DAILY", "1").lower()
                in ("1", "true", "yes", "on"))
try:
    BACKUP_INTERVAL_HOURS = float(os.environ.get("COSTCO_BACKUP_INTERVAL_HOURS") or 24)
except ValueError:
    BACKUP_INTERVAL_HOURS = 24.0
try:
    BACKUP_KEEP = int(os.environ.get("COSTCO_BACKUP_KEEP") or 14)  # retention count
except ValueError:
    BACKUP_KEEP = 14

# A modern desktop UA reduces the chance of being served a degraded/blocked page.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, OUTPUT_DIR, PDF_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)
