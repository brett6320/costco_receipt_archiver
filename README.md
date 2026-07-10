# Costco Receipt Archiver

Logs into Costco.com in a real browser you control, downloads **all available
warehouse & gas receipts**, and compiles every purchased item into
**deduplicated CSVs** — by date, price, and item number.

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
4. **Parse** — reads all raw data, dedupes, and writes CSVs to `data/output/`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Or just launch the web app (creates the venv on first run):

```bash
python -m costco_archiver auth adduser <name>   # first: create a login (see Web login)
./run_web.sh              # http://127.0.0.1:8000  (PORT=9000 ./run_web.sh to change)
```

The web UI requires a login — create an account first, or the sign-in page will
reject everyone. See [Web login](#web-login-authentication--mfa).

### Docker

```bash
docker compose up --build       # http://localhost:8000
PORT=9000 docker compose up --build   # or any port
```

Create a login before you sign in (the container serves the same auth-gated UI):

```bash
docker compose run --rm web python -m costco_archiver auth adduser <name>
```

The web port is configurable everywhere: `web --port 9000`, or the
`COSTCO_WEB_PORT` (or generic `PORT`) env var, honored by the CLI, `run_web.sh`,
and Docker/compose. `--port` overrides the env var. The bind host defaults to
`127.0.0.1` (`COSTCO_WEB_HOST`, or `web --host`); the Docker image binds
`0.0.0.0` so the container is reachable from the host.

The image bundles headless Chromium (for PDF rendering). Your data (receipts,
CSVs, PDFs, Markdown, credentials, and the `web_users.json` account store)
persists in `./data` via a mounted volume. The **Collect** tab works the same in
Docker — you paste a *Copy as cURL* from your own browser; nothing is automated
inside the container.

### Web app

Open the app and you land on **Search**. The UI is **mobile-friendly** and has a
**theme selector** (System / Light / **dark-gold** Dark). Two tabs:

- **Search** — free-text + date/price/item-number/**type**/**tax**/warehouse filters
  (the Tax filter selects Taxable / Non-taxable / Tax-exempt) plus a **Has discount**
  toggle (only items that carry a discount); sortable columns; **Group by item #**;
  a colored left band per order (so items from the same receipt are visually
  bracketed); a **F/W/D/T** letter badge per row for transaction type (Fuel /
  Warehouse / **Discount** / **Tax**); a **Tax** column showing Costco's per-line
  tax code (Y / N / numeric category codes) with a hover tooltip explaining it, and
  an **E** chip on tax-exempt items; item numbers link to a Costco product search
  (excluded for fuel/discount/tax);
  per-row **＋ / −** buttons to include / exclude that order, item number,
  description, or **store number** (they append editable tokens to the search box,
  e.g. `item:1610256`, `-store:358`); a **Refresh data** button to rebuild all
  outputs from receipts on disk; and a per-row **↻** to refresh a single receipt's
  PDF/barcode/Markdown (CLI: `refresh <receipt_id>`).
- **Summary stats & export** — **Matches**, **Total** (net spend), and **Discounts**
  (total savings) update with the filters; **⬇ Export** downloads the current view
  as a CSV (opens in Excel).
- **Price history** — click a price (**Unit $**, or **Last $** in grouped mode) to
  pop up a modal charting that item's price over time, with its average, low/high,
  purchase count, and how the clicked price compares to the average.
- **Orders collapse** — each transaction can collapse (click its start-of-order
  block icon) to a one-line summary showing the item count and order total; a
  **Collapse orders** toggle does all at once.
- **Discounts** — Costco's per-item discounts (which print as `/<item #>`) are
  labeled with the item they apply to (e.g. *Discount → LIVSFVARIETY VPACK*),
  typed **D**, and nested directly beneath that item. The discount amount shows in
  the **Unit $** column and the item's net price (total less discount) in **Amount**.
  Discounts reduce **Total** (never added positively) and are also summed in the
  **Discounts** stat; the collapsed item count excludes them.
- **Additional taxes** — per-item surcharges (which print as `T/<item #>`, e.g. a
  *liquor liter tax*) are the inverse of a discount: labeled with the taxed item,
  typed **T**, nested beneath it, with the surcharge in **Unit $** and the item
  total **plus** the tax in **Amount**. Like discounts they aren't counted as items.
- **Fuel** — gas lines show the **grade** (e.g. Regular), **gallons** (derived from
  total ÷ price, since the API omits the quantity), and **price per gallon**
  (3 decimals) in the Qty / Unit $ columns.
- **Collect** — capture credentials (paste a *Copy as cURL*) and run a collection
  back N months (default **36**) with a live progress bar and streaming log.

### Web login (authentication + MFA)

The web UI is **private**: every page and API route requires a signed-in session.
Accounts are **local**, and login is **password + a TOTP one-time code** (the
6-digit codes from Google Authenticator, 1Password, Authy, etc.). There's no open
access — if no account exists, the login page refuses everyone.

Manage accounts from the CLI (secrets are shown in your terminal, not the browser):

```bash
python -m costco_archiver auth adduser <name>   # prompts for a password, prints the TOTP secret
python -m costco_archiver auth users            # list accounts
python -m costco_archiver auth passwd <name>    # change password
python -m costco_archiver auth reset-mfa <name> # regenerate the TOTP secret
python -m costco_archiver auth deluser <name>   # remove an account
```

`adduser` prints an `otpauth://` URI and a base32 secret — scan/paste it into your
authenticator, then sign in with your password + the current code. Accounts live in
`data/web_users.json` (git-ignored, `0600`; passwords are salted **PBKDF2-HMAC-SHA256**,
never stored in plaintext).

Relevant environment variables:

- `COSTCO_SESSION_TTL` — session lifetime in seconds (default `43200` = 12h).
- `COSTCO_WEB_HTTPS=1` — mark the session cookie `Secure` when serving over TLS
  (directly or behind a reverse proxy).
- `COSTCO_AUTH_ISSUER` — label shown in authenticator apps (default
  "Costco Receipt Archiver").

> **Passkeys (WebAuthn) are planned but not yet implemented** — verifying them
> securely needs crypto that isn't in the stdlib, so MFA is TOTP for now. Each
> account already reserves a `passkeys` slot, so adding them later needs no data
> migration. Note WebAuthn will require HTTPS (or `localhost`) and a fixed domain.

> Sessions are held in server memory, so restarting `web` signs everyone out.

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
python -m costco_archiver auth adduser <name>   # one-time: create a login
python -m costco_archiver web                    # then open http://127.0.0.1:8000
```

Sign in with your password + authenticator code (see
[Web login](#web-login-authentication--mfa)), then use the two tabs:

- **Collect** — on-screen instructions to grab a DevTools *Copy as cURL* of the
  receipts request, a box to paste it (**Capture**), then **Start collection**
  (default **36 months**, newest first) with a live progress bar and streaming
  log as it fetches, parses, renders PDFs, and builds Markdown.
- **Search** — free-text (with `item:` / `store:` / `rcpt:` tokens and a leading
  `-` to exclude) plus date / price / item-number / type / tax / warehouse filters
  and a **Has discount** toggle; sortable columns; a **Group by item #** mode;
  per-row **＋ / −** include/exclude buttons; live **Matches / Total / Discounts**
  stats and **⬇ Export** to CSV; and per-receipt PDF links. See
  [Web app](#web-app) for the full feature list.

The same capture logic is available on the CLI as `import-curl`; everything the
web app does maps to CLI commands, so both are maintained.

## Usage (automated path, if login works)

```bash
# Recommended: login + fetch + parse + pdf in one shot (no stale-token gap)
python -m costco_archiver all

# …or step by step:
python -m costco_archiver login          # sign in once, cache the session
python -m costco_archiver fetch          # download all warehouse/gas receipts
python -m costco_archiver parse          # (re)build the CSVs from raw data
```

Useful flags:

- `--months-back N`   how far back to walk (default 36).
- `--max-empty N`     stop after N consecutive empty months (default 6).
- `--timeout SEC`     how long to wait for interactive login (default 300).

## Outputs (`data/output/`)

| File | Contents |
|------|----------|
| `line_items.csv` | Every purchased line item, one row each, **newest first**: date, item number, description, qty (gallons for fuel), unit price (price/gal for fuel), amount, department, tax flag, `tax_exempt` (Y for the receipt's far-left "E" items), `warehouse` (name) and `warehouse_number` (atomic, as distinct columns), receipt id, doc type, `order_type` (warehouse/fuel/**discount**/**tax**), `discount_ref` (for discount & additional-tax lines, the item number they apply to), source. |
| `items_deduped.csv` | **One row per item number**, aggregated across all purchases: times purchased, total qty, total spent, last price, first/last purchase date. |
| `receipts.csv` | One row per receipt: date, warehouse, totals, taxes, instant savings. |

A browsable **Markdown archive** is written to `data/output/markdown/`:
`index.md` lists every purchase (receipt) in descending date order and links to
`receipts/<id>.md`, a page per receipt with each line item, a Costco
search/detail link for the item, masked member number, totals, and a link to the
rendered PDF.

Raw archives are kept in `data/raw/` (per-receipt JSON) so you can re-parse
without re-downloading. Rendered per-receipt PDFs live in `data/pdfs/`. Re-running
`pdf` always re-renders from the current template and **overwrites a PDF only when
the result differs** (content or template change); unchanged files are left
untouched. Pass `pdf --force` to rewrite every PDF regardless.

## Privacy

- `data/` and `.costco_profile/` are git-ignored. The profile holds your logged-in
  session and the cached `data/credentials.json` holds bearer tokens — **keep them
  private** and don't commit them.
- The web UI requires login (password + TOTP MFA); accounts live in the git-ignored
  `data/web_users.json` (`0600`, salted PBKDF2 password hashes). See
  [Web login](#web-login-authentication--mfa).

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

## Tests

```bash
PYTHONPATH=. python tests/test_pipeline.py   # full fetch→parse pipeline (mocked API)
PYTHONPATH=. python tests/test_dedup.py      # receipt/line-item dedup rules
PYTHONPATH=. python tests/test_ingest.py     # HTML/PDF/JSON import path
```

`test_pipeline` runs the full fetch→parse pipeline against a mocked API (no
network/login) and asserts dedup, aggregation, and newest-first ordering;
`test_dedup` and `test_ingest` cover the dedup rules and the API-free import path.
