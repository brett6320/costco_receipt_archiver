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

Or just launch the web app (creates the venv on first run):

```bash
./run_web.sh              # http://127.0.0.1:8000  (PORT=9000 ./run_web.sh to change)
```

### Docker

```bash
docker compose up --build       # http://localhost:8000
```

The image bundles headless Chromium (for PDF rendering). Your data (receipts,
CSVs, PDFs, Markdown, credentials) persists in `./data` via a mounted volume.
The **Collect** tab works the same in Docker — you paste a *Copy as cURL* from
your own browser; nothing is automated inside the container.

### Web app

Open the app and you land on **Search**. The UI is **mobile-friendly** and has a
**theme selector** (System / Light / **dark-gold** Dark). Two tabs:

- **Search** — free-text + date/price/item-number/**type**/warehouse filters;
  sortable columns; **Group by item #**; a colored left band per order (so items
  from the same receipt are visually bracketed); a **F/W/O** letter badge per row
  for transaction type (Fuel / Warehouse / Online); item numbers link to a Costco
  product search (excluded for fuel); and a **Refresh data** button to rebuild
  outputs from receipts on disk.
- **Collect** — capture credentials (paste a *Copy as cURL*) and run a collection
  back N months (default **36**) with a live progress bar and streaming log.

## 🛡️ Costco blocks automated logins (Kasada/Akamai) — use `import-curl`

Costco's sign-in is protected by **Kasada + Akamai** bot detection, which
fingerprints automated browsers (Playwright/Puppeteer) — even ones driving real
Chrome — and returns **HTTP 429** on the sign-in policy. So the scripted `login`
often won't get through.

**The reliable path is to skip automation entirely and copy one real request:**

1. Log into **costco.com in your normal browser** (as a human — no throttling).
2. Open **DevTools → Network**, then open your **receipts** page so it loads.
3. Find the request to `ecom-api.costco.com/.../orders/graphql`,
   **right-click → Copy → Copy as cURL**.
4. Run:
   ```bash
   python -m costco_archiver import-curl        # reads the cURL from your clipboard
   python -m costco_archiver fetch && python -m costco_archiver parse
   ```

`import-curl` captures the **exact** headers (fresh token, clientid, cookies) the
browser used, so the API accepts them — **and** the request's GraphQL query/body,
so `fetch` replays Costco's *own* query across date windows (no reliance on a
hard-coded query that could drift). Do steps 3–4 promptly — see below.

> The automated `login` command still exists and may work if you don't have
> passkeys / aren't being throttled, but `import-curl` is what to reach for when
> you see 429s.

## ⏱️ Important: tokens live ~15 minutes

Costco's sign-in tokens (Azure AD B2C) expire about **15 minutes** after issue.
So **don't** log in, walk away, and fetch later — you'll get `401 Unauthorized`.
Instead do login **and** fetch in one go (the `all` command does this), or run
`fetch` immediately after `login`. The tool auto-detects an expired cached token
and re-logs in, and captures a fresh token by loading your receipts page right
before returning. A full backfill only takes ~1 minute, well inside the window.

## ⭐ Easiest bulk path: browser-console export

Pull your **entire** receipt history in one shot, using your own logged-in
browser (so Kasada never sees automation, and there's no token to copy):

1. Log into **costco.com** in your normal browser and open your receipts page.
2. DevTools → **Console**, paste the contents of
   [`browser/costco_fetch_receipts.js`](browser/costco_fetch_receipts.js), press Enter.
   It fetches every year of receipts and downloads `costco_receipts.json`.
3. Ingest and process the whole file:
   ```bash
   python -m costco_archiver import ~/Downloads/costco_receipts.json
   python -m costco_archiver parse && python -m costco_archiver pdf && python -m costco_archiver markdown
   python -m costco_archiver web
   ```

`import` extracts every receipt from that JSON into `data/raw/`, so the
post-processing steps then cover **all** of them (not just one). If the snippet
returns 0 receipts or GraphQL errors, copy the console output — the query/endpoint
may need a tweak for your account.

## 📄 API-free path: import saved receipt HTML/PDF

Because Kasada blocks scripted logins, the most dependable way to get data is to
open a receipt in your **normal** browser and save it, then import it — no API,
no token, no bot detection. Both formats parse into the same schema as `fetch`:

```bash
# Save a receipt as PDF (browser Print → Save as PDF) or copy its HTML, then:
python -m costco_archiver import path/to/receipt.pdf
python -m costco_archiver import path/to/receipts_folder/      # a whole folder
python -m costco_archiver import --clipboard                   # HTML on clipboard

python -m costco_archiver parse    # build CSVs
python -m costco_archiver pdf      # render a clean PDF archive of each receipt
python -m costco_archiver markdown # index + per-receipt Markdown pages
python -m costco_archiver web      # search UI at http://127.0.0.1:8000
```

To copy a receipt's HTML: open the receipt, DevTools → Elements, right-click the
receipt container (the `#dataToPrint` / receipt dialog) → Copy → **Copy element**,
then `import --clipboard`. Parsing reconciles line items against the printed
subtotal/tax/total, so you'll know if anything was missed.

## Web app: capture, collect, and search in one place

```bash
python -m costco_archiver web            # then open http://127.0.0.1:8000
```

Two tabs:

- **Collect** — on-screen instructions to grab a DevTools *Copy as cURL* of the
  receipts request, a box to paste it (**Capture**), then **Start collection**
  (default **36 months**, newest first) with a live progress bar and streaming
  log as it fetches, parses, renders PDFs, and builds Markdown.
- **Search** — free-text search across description / item # / warehouse, plus
  date-range, price-range, item-number and warehouse filters; sortable columns; a
  **Group by item #** mode (times purchased, total spent, last price, first/last
  date); and a link from each row to its rendered PDF.

The same capture logic is available on the CLI as `import-curl`; everything the
web app does maps to CLI commands, so both are maintained.

## Usage (automated path, if login works)

```bash
# Recommended: login + fetch + parse + pdf in one shot (no stale-token gap)
python -m costco_archiver all --skip-online

# Do everything incl. online-order harvest
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

A browsable **Markdown archive** is written to `data/output/markdown/`:
`index.md` lists every purchase (receipt) in descending date order and links to
`receipts/<id>.md`, a page per receipt with each line item, a Costco
search/detail link for the item, masked member number, totals, and a link to the
rendered PDF.

Raw archives are kept in `data/raw/` (per-receipt JSON) and `data/captured/`
(online-order network captures) so you can re-parse without re-downloading.
Rendered per-receipt PDFs (plus any real PDFs the site served) live in
`data/pdfs/`.

## Privacy

- `data/` and `.costco_profile/` are git-ignored. The profile holds your logged-in
  session and the cached `data/credentials.json` holds bearer tokens — **keep them
  private** and don't commit them.

## Troubleshooting

### Login stalls on "FIDO Consent" / passkey, or returns HTTP 429

Costco's bot protection (Akamai) and Azure AD B2C sign-in policy fingerprint
Playwright's *bundled* Chromium — the symptom is a stuck "FIDO Consent" page
after a security-key/passkey, or a `429 (Too Many Requests)` on the sign-in POST.

Fixes, in order of reliability:

1. **Use your real Chrome** (now the default). If it didn't pick it up, force it:
   ```bash
   python -m costco_archiver login --channel chrome   # or: msedge
   ```
2. **If you're already 429'd, wait ~15–30 min** — the throttle is per-IP/account
   and compounds with each retry. Then try again with real Chrome.
3. **Reset a poisoned session:** `rm -rf .costco_profile` and log in fresh.
4. **Bypass automated login entirely (most reliable).** Log into costco.com in
   your *normal* browser as a human, then hand the token to the tool:
   ```bash
   python -m costco_archiver paste-token
   ```
   It prints a one-line DevTools Console snippet that **copies** your `idToken`
   and `clientID` to the clipboard; the command then reads them straight off the
   clipboard (`pbpaste`) — no pasting into the terminal, so the long JWT can't be
   truncated. Then run `python -m costco_archiver fetch`. Prefer a file instead?
   Save the JSON and pass `--file <path>`. (Tokens expire in ~1 hour — re-run
   `paste-token` if a fetch starts returning 401.)

### Other

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
