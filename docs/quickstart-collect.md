# Quickstart: collect your receipts via the Collect page

Costco's sign-in is protected by bot detection (Kasada/Akamai) that blocks
scripted logins, so the archiver doesn't log in for you. Instead you log in with
your **own** browser, copy the one authenticated request Costco's site makes, and
paste it into the app's **Collect** page. The app reuses that request's token to
download your receipts.

The token lives about **15 minutes**, so do this in one sitting: capture, then
collect right away.

> You need an **admin** account to see the Collect page. Create the first one
> with `python -m costco_archiver auth adduser <name>` (the first account is
> always an admin).

---

## 1. Open your receipts on costco.com

1. In your normal browser (Chrome, Edge, Firefox, or Safari), sign in at
   **costco.com**.
2. Go to **Orders & Purchases → Receipts** (your in‑warehouse and gas receipts).

## 2. Open DevTools → Network

1. Open DevTools:
   - **Chrome / Edge:** `F12` or `⌥⌘I` (Mac) / `Ctrl+Shift+I` (Win/Linux)
   - **Firefox:** `F12`
   - **Safari:** enable the Develop menu first (Settings → Advanced →
     "Show features for web developers"), then `⌥⌘I`.
2. Click the **Network** tab.
3. In the Network filter box, type **`graphql`**.

## 3. Capture the receipts request

1. **Reload the receipts page** so the request fires. A request to
   **`ecom-api.costco.com/…/orders/graphql`** appears in the Network list.
2. **Right‑click** that request → **Copy** → **Copy as cURL**.
   - Chrome/Edge on Mac/Linux: choose **Copy as cURL (bash)**.
   - Firefox: **Copy Value → Copy as cURL**.
   - Safari: **Copy as cURL**.

   This copies the full request — headers, token, and all — to your clipboard.

> Pick the request whose **Response** contains your receipts (a JSON body with
> `itemArray`/receipt data). If the filtered list has several `graphql` rows,
> click through them and use the one that returns receipts.

## 4. Paste it into the Collect page

1. In the archiver, open the **Collect** tab → **1 · Capture credentials**.
2. Paste the copied cURL into the big text box and click **Capture**.
3. You should see a green confirmation like
   *"✓ Captured. N headers, query captured, token ~14 min left."*
   - If it says the token is **already expired**, just recopy a fresh cURL
     (repeat step 3) — you were a little slow; it happens.

## 5. Collect

1. In **2 · Collect receipts**, set how far back to go (default **36 months**).
2. Leave **Render PDFs** checked to also build printable PDFs.
3. Click **Start collection** and watch the live log.

When it finishes, switch to the **Search** tab to browse everything. Your imported
data is snapshotted into a **backup** automatically (see **Collect → Backups**),
and a daily backup runs while the app is up.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Token already expired" on Capture | Recopy a fresh **Copy as cURL** and Capture again — do it within ~15 min. |
| No `graphql` request appears | Make sure the filter says `graphql`, then **reload** the receipts page while the Network tab is open. |
| Capture says "query not captured" | You copied a different `graphql` request. Pick the one whose response contains your receipts and copy that one. |
| Collect returns 0 receipts | The token may have expired mid‑run, or the account has no receipts in that window. Recapture and try a smaller months‑back. |
| No Collect tab visible | Your account is an **operator** (read‑only). Ask an admin to grant the admin role, or create an admin account from the CLI. |

Prefer the terminal? The same capture works headless with
`python -m costco_archiver import-curl` (reads the cURL from your clipboard or a
file) — see the main [README](../README.md).
