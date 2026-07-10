# Costco Receipt Archiver

Logs into Costco.com in a real browser you control, downloads **all available
warehouse & gas receipts** (and best-effort online-order data), and compiles
every purchased item into **deduplicated CSVs** — by date, price, and item number.

It walks history **most-recent-first, backward in time**, and is **idempotent**:
each receipt is saved once (keyed by its transaction barcode), so re-running only
picks up what's new and never double-counts.

> ⚠️ This uses Costco's own **undocumented** internal API (the same calls the
> website makes). It only touches **your own** account data. Costco can change
> the endpoints at any time; see *Troubleshooting* if the schema shifts.

## How it works

1. **Login** — a visible Chromium opens with a *persistent profile*. You sign in
   once (handling any 2FA/captcha). The session is saved, so later runs skip login.
2. **Credentials** — after login the tool reads the `idToken` / `clientID` the web
   app stores in `localStorage` (and sniffs a live GraphQL request as a fallback).
   These authorize the receipts API. Nothing is sent anywhere but Costco.
3. **Fetch** — queries the receipts GraphQL endpoint in monthly windows going
   backward, saving each receipt's raw JSON to `data/raw/`.
4. **Online orders** — drives the logged-in browser through the "Orders &
   Purchases" pages and captures the JSON the site loads (`data/captured/`).
5. **Parse** — reads all raw data, dedupes, and writes CSVs to `data/output/`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# Do everything: login -> fetch receipts -> harvest online orders -> parse
python -m costco_archiver all

# …or step by step:
python -m costco_archiver login          # sign in once, cache the session
python -m costco_archiver fetch          # download all warehouse/gas receipts
python -m costco_archiver online         # harvest online-order data (browser)
python -m costco_archiver parse          # (re)build the CSVs from raw data
```

Useful flags:

- `--months-back N`   how far back to walk (default 36).
- `--max-empty N`     stop after N consecutive empty months (default 6).
- `--skip-online`     for `all`, skip the online-order harvest.
- `--timeout SEC`     how long to wait for interactive login (default 300).

## Outputs (`data/output/`)

| File | Contents |
|------|----------|
| `line_items.csv` | Every purchased line item, one row each, **newest first**: date, item number, description, qty, unit price, amount, warehouse, receipt id, source. |
| `items_deduped.csv` | **One row per item number**, aggregated across all purchases: times purchased, total qty, total spent, last price, first/last purchase date. |
| `receipts.csv` | One row per receipt: date, warehouse, totals, taxes, instant savings. |

Raw archives are kept in `data/raw/` (per-receipt JSON) and `data/captured/`
(online-order network captures) so you can re-parse without re-downloading.

## Privacy

- `data/` and `.costco_profile/` are git-ignored. The profile holds your logged-in
  session and the cached `data/credentials.json` holds bearer tokens — **keep them
  private** and don't commit them.

## Troubleshooting

- **Login didn't produce credentials.** Complete sign-in fully (reach your account
  page). If the site changed where it stores the token, open DevTools → Network,
  find a request to `ecom-api.costco.com/.../graphql`, copy its
  `Costco-X-Authorization` (drop `Bearer `) and `Costco-X-Wcs-Clientid`, then:
  ```bash
  export COSTCO_ID_TOKEN="<token>"
  export COSTCO_CLIENT_ID="<clientid>"
  python -m costco_archiver fetch
  ```
- **GraphQL errors about unknown fields/arguments.** Costco changed the schema.
  Override the query without editing code:
  ```bash
  export COSTCO_RECEIPTS_QUERY='query receiptsWithCounts($startDate:String!,$endDate:String!,$documentType:String!){ ... }'
  ```
  (Grab the current query from the site's own network request.)
- **Online orders came back empty.** The SPA endpoints move around; the captures in
  `data/captured/` are raw — inspect them and adjust `harvest.py`'s page list.

## Tests

```bash
PYTHONPATH=. python tests/test_pipeline.py
```

Runs the full fetch→parse pipeline against a mocked API (no network/login) and
asserts dedup, aggregation, and newest-first ordering.
