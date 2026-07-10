"""A small, dependency-free local web UI: capture credentials, collect receipts,
and search the results — all in one page.

- Collect tab: paste a DevTools 'Copy as cURL' (the import-curl method) to capture
  credentials, then run a collection going back N months (default 36).
- Search tab: free-text + date/price/item/warehouse filters over the results,
  sortable columns, group-by-item mode, and per-row PDF links.

Run:  python -m costco_archiver web    (opens http://127.0.0.1:8000)
"""
from __future__ import annotations

import csv
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config

# --- collection job state (one at a time) -------------------------------------
_JOB = {"state": "idle", "message": "", "done": 0, "total": 0,
        "saved": 0, "error": None, "summary": None, "log": []}
_JOB_LOCK = threading.Lock()


def _log(msg: str):
    with _JOB_LOCK:
        _JOB["log"].append(msg)
        del _JOB["log"][:-200]  # keep last 200 lines


def _load_creds():
    from .auth import Credentials
    f = config.CRED_CACHE_FILE
    if not f.exists():
        return None
    try:
        return Credentials(**json.loads(f.read_text()))
    except Exception:
        return None


def _run_collection(months_back: int, do_pdf: bool):
    from .fetch import fetch_all_receipts
    from .parse import parse_all
    from .auth import token_is_expired

    creds = _load_creds()
    if creds is None:
        _set_job(state="error", error="No credentials — capture a cURL first.")
        return
    if token_is_expired(creds.id_token):
        _set_job(state="error",
                 error="Token expired (they last ~15 min). Re-capture a fresh cURL.")
        return

    def cb(done, total, saved, label):
        _set_job(state="running", done=done, total=total, saved=saved,
                 message=f"Fetching {label} — {saved} receipts so far")
        _log(f"{label}: {saved} receipts collected")

    try:
        with _JOB_LOCK:
            _JOB["log"] = []
        _set_job(state="running", message="Starting collection…",
                 done=0, total=months_back, saved=0, error=None, summary=None)
        _log(f"Collecting receipts back {months_back} months (newest first)…")
        summary = fetch_all_receipts(creds, months_back=months_back, progress_cb=cb)
        _log(f"Fetched {summary.get('receipts_saved_this_run', 0)} new; "
             f"{summary.get('total_receipts_on_disk', 0)} total on disk.")
        _set_job(state="parsing", message="Building CSVs, index & Markdown…")
        _log("Parsing → CSVs…")
        parse_all()
        from .markdown import generate_markdown
        _log("Generating Markdown archive…")
        generate_markdown()
        if do_pdf:
            _set_job(state="rendering", message="Rendering PDFs…")
            _log("Rendering per-receipt PDFs…")
            from .pdf import render_all_pdfs
            render_all_pdfs()
        _Handler.rows = _load_rows()  # refresh search data
        _log(f"Done. {len(_Handler.rows)} line items ready to search.")
        _set_job(state="done", message="Done.", summary=summary)
    except Exception as ex:
        _log(f"ERROR: {ex}")
        _set_job(state="error", error=str(ex))


def _run_reprocess(do_pdf: bool):
    """Rebuild all post-processing outputs from data/raw (no re-fetch).

    Backfills CSVs, Markdown (item links, barcodes), and optionally PDFs — use
    after changing raw data, or if outputs were never generated."""
    from .parse import parse_all
    from .markdown import generate_markdown
    try:
        with _JOB_LOCK:
            _JOB["log"] = []
        n_raw = len(list(config.RAW_DIR.glob("*.json")))
        _set_job(state="parsing", message="Rebuilding outputs…",
                 done=0, total=1, saved=n_raw, error=None, summary=None)
        _log(f"Refreshing metadata for {n_raw} receipts on disk…")
        _log("Parsing → CSVs…")
        parse_all()
        _log("Generating Markdown (item links + barcodes)…")
        generate_markdown()
        if do_pdf:
            _set_job(state="rendering", message="Rendering PDFs…")
            _log("Rendering per-receipt PDFs…")
            from .pdf import render_all_pdfs
            render_all_pdfs()
        _Handler.rows = _load_rows()
        _log(f"Done. {len(_Handler.rows)} line items refreshed.")
        _set_job(state="done", message="Metadata refreshed.",
                 summary={"receipts": n_raw, "line_items": len(_Handler.rows)})
    except Exception as ex:
        _log(f"ERROR: {ex}")
        _set_job(state="error", error=str(ex))


def _set_job(**kw):
    with _JOB_LOCK:
        _JOB.update(kw)


def _job_snapshot():
    with _JOB_LOCK:
        return dict(_JOB)

# Columns to free-text search across.
_TEXT_FIELDS = ["item_number", "description", "warehouse", "receipt_id",
                "doc_type", "source", "department"]


def _load_rows() -> list[dict]:
    f = config.OUTPUT_DIR / "line_items.csv"
    if not f.exists():
        return []
    with f.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k in ("unit_qty", "unit_price", "amount"):
            try:
                r[k] = float(r.get(k) or 0)
            except ValueError:
                r[k] = 0.0
    return rows


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _search(rows: list[dict], q: dict) -> dict:
    text = (q.get("q", [""])[0] or "").strip().lower()
    terms = [t for t in text.split() if t]
    date_from = (q.get("date_from", [""])[0] or "").strip()
    date_to = (q.get("date_to", [""])[0] or "").strip()
    min_price = _num(q.get("min_price", [""])[0])
    max_price = _num(q.get("max_price", [""])[0])
    item_number = (q.get("item_number", [""])[0] or "").strip()
    warehouse = (q.get("warehouse", [""])[0] or "").strip().lower()
    otype_filter = (q.get("order_type", [""])[0] or "").strip().lower()
    sort = (q.get("sort", ["date"])[0] or "date")
    order = (q.get("order", ["desc"])[0] or "desc")
    group = (q.get("group", ["0"])[0] == "1")

    def keep(r):
        if terms:
            hay = " ".join(str(r.get(f, "")) for f in _TEXT_FIELDS).lower()
            if not all(t in hay for t in terms):
                return False
        if date_from and (r.get("date") or "") < date_from:
            return False
        if date_to and (r.get("date") or "") > date_to:
            return False
        if item_number and item_number not in str(r.get("item_number", "")):
            return False
        if warehouse and warehouse not in str(r.get("warehouse", "")).lower():
            return False
        if otype_filter and str(r.get("order_type", "")).lower() != otype_filter:
            return False
        amt = r.get("amount", 0.0)
        if min_price is not None and amt < min_price:
            return False
        if max_price is not None and amt > max_price:
            return False
        return True

    matched = [r for r in rows if keep(r)]

    if group:
        agg: dict[str, dict] = {}
        for r in matched:
            key = r.get("item_number") or f"NONUM::{r.get('description')}"
            a = agg.setdefault(key, {
                "item_number": r.get("item_number", ""),
                "description": r.get("description", ""),
                "order_type": r.get("order_type", "warehouse"),
                "times_purchased": 0, "total_qty": 0.0, "total_spent": 0.0,
                "last_price": r.get("unit_price") or r.get("amount"),
                "first_purchase": r.get("date", ""), "last_purchase": r.get("date", ""),
            })
            a["times_purchased"] += 1
            a["total_qty"] += r.get("unit_qty") or 1
            a["total_spent"] = round(a["total_spent"] + r.get("amount", 0.0), 2)
            d = r.get("date", "")
            if d and (not a["first_purchase"] or d < a["first_purchase"]):
                a["first_purchase"] = d
            if d and d >= a["last_purchase"]:
                a["last_purchase"] = d
                a["last_price"] = r.get("unit_price") or r.get("amount")
            if r.get("description") and not a["description"]:
                a["description"] = r["description"]
        result = list(agg.values())
        sort_key = sort if sort in result[0] else "last_purchase" if result else sort
    else:
        result = matched
        sort_key = sort if (result and sort in result[0]) else "date"

    reverse = order != "asc"
    try:
        result.sort(key=lambda x: (x.get(sort_key) is None, x.get(sort_key)),
                    reverse=reverse)
    except TypeError:
        result.sort(key=lambda x: str(x.get(sort_key, "")), reverse=reverse)

    total_spent = round(sum(r.get("amount", 0.0) for r in matched), 2)
    limit = int(q.get("limit", ["1000"])[0])
    return {
        "count": len(result),
        "line_item_matches": len(matched),
        "total_spent": total_spent,
        "grouped": group,
        "rows": result[:limit],
    }


class _Handler(BaseHTTPRequestHandler):
    rows: list[dict] = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/search":
            q = parse_qs(parsed.query)
            out = json.dumps(_search(self.rows, q)).encode()
            self._send(200, out)
        elif path == "/api/meta":
            warehouses = sorted({r.get("warehouse", "") for r in self.rows if r.get("warehouse")})
            dates = sorted({r.get("date", "") for r in self.rows if r.get("date")})
            meta = {
                "total_line_items": len(self.rows),
                "warehouses": warehouses,
                "date_min": dates[0] if dates else "",
                "date_max": dates[-1] if dates else "",
            }
            self._send(200, json.dumps(meta).encode())
        elif path == "/api/reload":
            _Handler.rows = _load_rows()
            self._send(200, json.dumps({"reloaded": len(self.rows)}).encode())
        elif path == "/api/collect/status":
            self._send(200, json.dumps(_job_snapshot()).encode())
        elif path.startswith("/pdf/"):
            name = path[len("/pdf/"):]
            pdf = config.PDF_DIR / f"{name}.pdf"
            if pdf.exists():
                self._send(200, pdf.read_bytes(), "application/pdf")
            else:
                self._send(404, b'{"error":"pdf not found"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/capture":
            from .capture import save_from_curl, CaptureError
            body = self._read_json()
            try:
                result = save_from_curl(body.get("curl", ""))
                self._send(200, json.dumps(result).encode())
            except CaptureError as ex:
                self._send(400, json.dumps({"error": str(ex)}).encode())
            except Exception as ex:
                self._send(500, json.dumps({"error": str(ex)}).encode())
        elif path == "/api/collect":
            snap = _job_snapshot()
            if snap["state"] in ("running", "parsing", "rendering"):
                self._send(409, json.dumps({"error": "A collection is already running."}).encode())
                return
            body = self._read_json()
            months = int(body.get("months_back") or 36)
            do_pdf = bool(body.get("render_pdf", True))
            t = threading.Thread(target=_run_collection, args=(months, do_pdf), daemon=True)
            t.start()
            self._send(200, json.dumps({"started": True, "months_back": months}).encode())
        elif path == "/api/reprocess":
            snap = _job_snapshot()
            if snap["state"] in ("running", "parsing", "rendering"):
                self._send(409, json.dumps({"error": "A job is already running."}).encode())
                return
            body = self._read_json()
            do_pdf = bool(body.get("render_pdf", True))
            t = threading.Thread(target=_run_reprocess, args=(do_pdf,), daemon=True)
            t.start()
            self._send(200, json.dumps({"started": True}).encode())
        else:
            self._send(404, b'{"error":"not found"}')


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    config.ensure_dirs()
    _Handler.rows = _load_rows()
    if not _Handler.rows:
        print("No parsed data found. Run `fetch` then `parse` first.")
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    print(f"Serving {len(_Handler.rows)} line items at {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Costco Receipt Archiver</title>
<script>
  // Apply theme before paint to avoid a flash. Default: follow system.
  (function(){ document.documentElement.setAttribute('data-theme',
    localStorage.getItem('theme') || 'system'); })();
</script>
<style>
  /* ---- Theme tokens (light defaults) ---- */
  :root {
    --bg:#f6f7f9; --fg:#1c2126; --muted:#6b7280; --card:#fff; --bd:#d8dbe0;
    --line:#eef0f2; --accent:#005daa; --on-accent:#fff; --thead:#f0f2f5;
    --rowhover:#fbfcfe; --input-bg:#fff; --chip:#eef1f5; --code:#eef1f5;
    --log-bg:#0e1420; --log-fg:#cfe3ff; --ok:#127a2b; --err:#c5221f;
    color-scheme: light;
  }
  /* ---- Explicit dark ---- */
  :root[data-theme="dark"] {
    --bg:#0f141a; --fg:#e6e9ee; --muted:#9aa4b2; --card:#161b22; --bd:#2b3440;
    --line:#232b34; --accent:#3b82f6; --on-accent:#ffffff; --thead:#1b222b;
    --rowhover:#1a212a; --input-bg:#0f141a; --chip:#222a33; --code:#222a33;
    --log-bg:#080c12; --log-fg:#cfe3ff; --ok:#4ecb71; --err:#ff6b60;
    color-scheme: dark;
  }
  /* ---- System (default): mirror dark when the OS is dark ---- */
  @media (prefers-color-scheme: dark) {
    :root[data-theme="system"] {
      --bg:#0f141a; --fg:#e6e9ee; --muted:#9aa4b2; --card:#161b22; --bd:#2b3440;
      --line:#232b34; --accent:#3b82f6; --on-accent:#ffffff; --thead:#1b222b;
      --rowhover:#1a212a; --input-bg:#0f141a; --chip:#222a33; --code:#222a33;
      --log-bg:#080c12; --log-fg:#cfe3ff; --ok:#4ecb71; --err:#ff6b60;
      color-scheme: dark;
    }
  }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; color:var(--fg); background:var(--bg); }
  header { background:var(--accent); color:var(--on-accent); padding:12px 18px; display:flex; align-items:center; gap:18px; flex-wrap:wrap; }
  header h1 { margin:0; font-size:18px; }
  header .sub { font-size:12px; opacity:.9; }
  .tabs { display:flex; gap:6px; margin-left:auto; align-items:center; }
  .tabs button { background:rgba(255,255,255,.2); color:var(--on-accent); border:0; padding:7px 14px; border-radius:6px; cursor:pointer; font-size:13px; }
  .tabs button.active { background:var(--card); color:var(--accent); font-weight:600; }
  .themesel { background:rgba(255,255,255,.2); color:var(--on-accent); border:0; border-radius:6px; padding:6px 8px; font-size:12px; cursor:pointer; }
  .themesel option { color:#111; }
  .wrap { padding:16px 18px; }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:8px; padding:16px; margin-bottom:14px; }
  .card h2 { margin:0 0 8px; font-size:15px; }
  ol.steps { margin:0 0 10px; padding-left:20px; font-size:13px; line-height:1.6; }
  ol.steps code, code { background:var(--code); padding:1px 5px; border-radius:4px; }
  textarea { width:100%; height:120px; border:1px solid var(--bd); border-radius:6px; padding:8px; font-family:ui-monospace,Menlo,monospace; font-size:12px; background:var(--input-bg); color:var(--fg); }
  .btn { background:var(--accent); color:var(--on-accent); border:0; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:13px; }
  .btn:disabled { opacity:.5; cursor:default; }
  .btn.secondary { background:var(--chip); color:var(--fg); }
  .inline { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  .msg { font-size:13px; margin-top:8px; }
  .msg.ok { color:var(--ok); } .msg.err { color:var(--err); }
  .bar { height:10px; background:var(--chip); border-radius:6px; overflow:hidden; margin:10px 0; }
  .bar > div { height:100%; background:var(--accent); width:0%; transition:width .3s; }
  .log { background:var(--log-bg); color:var(--log-fg); font-family:ui-monospace,Menlo,monospace; font-size:12px; border-radius:6px; padding:10px; height:200px; overflow:auto; white-space:pre-wrap; }
  .filters { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
  .filters label { font-size:11px; color:var(--muted); display:block; margin-bottom:3px; }
  .filters input, .filters select { width:100%; padding:6px 8px; border:1px solid var(--bd); border-radius:6px; font-size:13px; background:var(--input-bg); color:var(--fg); }
  .row2 { display:flex; align-items:center; gap:14px; margin:12px 2px; font-size:13px; color:var(--muted); flex-wrap:wrap; }
  .stat b { color:var(--fg); }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--bd); border-radius:8px; overflow:hidden; font-size:13px; }
  th,td { padding:7px 10px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { background:var(--thead); cursor:pointer; user-select:none; position:sticky; top:0; }
  th.sorted::after { content:" ▾"; }
  th.asc.sorted::after { content:" ▴"; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:hover td { background:var(--rowhover); }
  .desc { white-space:normal; min-width:220px; }
  .tbadge { display:inline-block; width:20px; height:20px; line-height:20px; text-align:center;
    border-radius:5px; font-weight:700; font-size:11px; color:#fff; background:var(--tc,#666); }
  a.pdf { color:var(--accent); text-decoration:none; }
  .toggle { display:flex; align-items:center; gap:6px; }
  .hidden { display:none; }
  /* Same-order visual delineator */
  td.ord { width:16px; padding:0; position:relative; }
  td.ord > span.band { position:absolute; left:5px; top:0; bottom:0; width:5px; background:var(--oc,#ccc); }
  td.ord > span.sw { position:absolute; left:2px; width:11px; height:11px; border-radius:3px; background:var(--oc,#ccc); box-shadow:0 0 0 1px rgba(128,128,128,.45); }
  tr.ordf td.ord > span.band { top:4px; border-top-left-radius:4px; border-top-right-radius:4px; }
  tr.ordl td.ord > span.band { bottom:4px; border-bottom-left-radius:4px; border-bottom-right-radius:4px; }
  tr.ordf td.ord > span.sw { top:5px; }
  .tablewrap { overflow:auto; -webkit-overflow-scrolling:touch; max-height:70vh; border-radius:8px; }
  /* ---- Mobile ---- */
  @media (max-width:680px) {
    header { padding:10px 12px; gap:8px; }
    header h1 { font-size:16px; width:100%; }
    header .sub { order:3; width:100%; }
    .tabs { margin-left:0; width:100%; }
    .tabs button { flex:1; padding:9px 8px; }
    .wrap { padding:10px; }
    .card { padding:12px; }
    .filters { grid-template-columns:1fr 1fr; }
    .filters div[style*="1/-1"] { grid-column:1/-1; }
    .btn, .btn.secondary { padding:10px 14px; }        /* larger touch targets */
    .filters input, .filters select, textarea { font-size:16px; }  /* no iOS zoom */
    th,td { padding:8px 8px; }
    .tablewrap { max-height:64vh; }
    .row2 { gap:10px; }
  }
  @media (max-width:420px) { .filters { grid-template-columns:1fr; } }
</style></head><body>
<header>
  <h1>Costco Receipt Archiver</h1>
  <div class="sub" id="meta">loading…</div>
  <div class="tabs">
    <button id="tab-search" class="active" onclick="showTab('search')">Search</button>
    <button id="tab-collect" onclick="showTab('collect')">Collect</button>
    <select id="theme" class="themesel" title="Theme" onchange="setTheme(this.value)">
      <option value="system">🖥 System</option>
      <option value="light">☀ Light</option>
      <option value="dark">🌙 Dark</option>
    </select>
  </div>
</header>

<div class="wrap">
  <!-- ============ COLLECT ============ -->
  <div id="view-collect" class="hidden">
    <div class="card">
      <h2>1 · Capture credentials (import-curl)</h2>
      <ol class="steps">
        <li>In your <b>normal browser</b>, log in at <code>costco.com</code> and open your <b>Orders &amp; Purchases → Receipts</b> page.</li>
        <li>Open <b>DevTools</b> (F12 / ⌥⌘I) → <b>Network</b> tab. In the filter box type <code>graphql</code>.</li>
        <li>Reload the receipts page so a request to <code>ecom-api.costco.com/…/orders/graphql</code> appears.</li>
        <li><b>Right-click</b> that request → <b>Copy</b> → <b>Copy as cURL</b> (bash on Mac/Linux).</li>
        <li>Paste it below and click <b>Capture</b>. (The token lasts ~15 min — capture, then collect right away.)</li>
      </ol>
      <textarea id="curl" placeholder="curl 'https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql' -H '...' --data-raw '...'"></textarea>
      <div class="inline" style="margin-top:8px">
        <button class="btn" id="captureBtn" onclick="capture()">Capture</button>
        <span class="msg" id="captureMsg"></span>
      </div>
    </div>

    <div class="card">
      <h2>2 · Collect receipts</h2>
      <div class="inline">
        <label>Go back
          <input id="months" type="number" min="1" max="120" value="36" style="width:70px;padding:5px;border:1px solid var(--bd);border-radius:6px"> months
        </label>
        <label class="toggle"><input type="checkbox" id="renderPdf" checked> Render PDFs</label>
        <button class="btn" id="collectBtn" onclick="collect()">Start collection</button>
        <span class="msg" id="collectMsg"></span>
      </div>
      <div class="bar"><div id="bar"></div></div>
      <div class="log" id="log">Idle. Capture credentials above, then start a collection.</div>
    </div>

    <div class="card">
      <h2>3 · Refresh metadata</h2>
      <p style="font-size:13px;color:var(--muted);margin:0 0 10px">
        Rebuild the post-processing outputs (CSVs, item links, barcodes, Markdown,
        PDFs) from the receipts already on disk — no re-fetch. Use this to backfill
        if outputs weren't generated, or after changing data.</p>
      <div class="inline">
        <label class="toggle"><input type="checkbox" id="reRenderPdf" checked> Render PDFs</label>
        <button class="btn secondary" id="reprocessBtn" onclick="reprocess()">Refresh metadata</button>
        <span class="msg" id="reprocessMsg"></span>
      </div>
    </div>
  </div>

  <!-- ============ SEARCH ============ -->
  <div id="view-search">
    <div class="card">
      <div class="filters">
        <div style="grid-column:1/-1"><label>Search (any term: description, item #, warehouse…)</label>
          <input id="q" placeholder="e.g. olive oil  ·  1610256  ·  kirkland"></div>
        <div><label>Date from</label><input id="date_from" type="date"></div>
        <div><label>Date to</label><input id="date_to" type="date"></div>
        <div><label>Item number</label><input id="item_number" placeholder="exact/partial"></div>
        <div><label>Type</label><select id="order_type">
          <option value="">All</option>
          <option value="warehouse">Warehouse</option>
          <option value="fuel">Fuel</option>
          <option value="online">Online</option>
        </select></div>
        <div><label>Warehouse</label><input id="warehouse" placeholder="contains…"></div>
        <div><label>Min price</label><input id="min_price" type="number" step="0.01"></div>
        <div><label>Max price</label><input id="max_price" type="number" step="0.01"></div>
      </div>
      <div class="row2">
        <span class="stat">Matches: <b id="count">0</b></span>
        <span class="stat">Total: <b id="total">$0.00</b></span>
        <label class="toggle"><input type="checkbox" id="group"> Group by item #</label>
        <span style="flex:1"></span>
        <button class="btn secondary" id="refreshBtn" onclick="reprocess()" title="Rebuild data & metadata from receipts on disk">↻ Refresh data</button>
        <button class="btn secondary" id="reset">Reset</button>
      </div>
    </div>
    <div class="tablewrap"><table id="tbl"><thead></thead><tbody></tbody></table></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
function showTab(name){
  $("view-collect").classList.toggle("hidden", name!=="collect");
  $("view-search").classList.toggle("hidden", name!=="search");
  $("tab-collect").classList.toggle("active", name==="collect");
  $("tab-search").classList.toggle("active", name==="search");
  if(name==="search") run();
}

// ---------- Collect ----------
async function capture(){
  const msg = $("captureMsg"); msg.className="msg"; msg.textContent="Capturing…";
  $("captureBtn").disabled = true;
  try{
    const r = await fetch("/api/capture",{method:"POST",headers:{"Content-Type":"application/json"},
      body: JSON.stringify({curl: $("curl").value})});
    const d = await r.json();
    if(!r.ok){ msg.className="msg err"; msg.textContent = d.error || "Capture failed"; }
    else {
      msg.className = d.expired ? "msg err" : "msg ok";
      msg.textContent = (d.expired ? "⚠ Token already expired — recopy a fresh cURL. " : "✓ Captured. ")
        + `${d.headers} headers` + (d.has_query?", query captured":"") + `, token ~${d.token_minutes} min left.`;
    }
  }catch(e){ msg.className="msg err"; msg.textContent = String(e); }
  $("captureBtn").disabled = false;
}

let poll = null;
async function collect(){
  const msg = $("collectMsg"); msg.className="msg"; msg.textContent="Starting…";
  $("collectBtn").disabled = true;
  const body = { months_back: Number($("months").value)||36, render_pdf: $("renderPdf").checked };
  const r = await fetch("/api/collect",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json();
  if(!r.ok){ msg.className="msg err"; msg.textContent = d.error || "Could not start"; $("collectBtn").disabled=false; return; }
  msg.textContent = "";
  if(poll) clearInterval(poll);
  poll = setInterval(pollStatus, 1000);
  pollStatus();
}
async function pollStatus(){
  const s = await (await fetch("/api/collect/status")).json();
  const pct = s.total ? Math.min(100, Math.round(100*s.done/s.total)) : (s.state==="done"?100:0);
  $("bar").style.width = pct + "%";
  $("log").textContent = (s.log||[]).join("\n");
  $("log").scrollTop = $("log").scrollHeight;
  const done = s.state==="done", err = s.state==="error";
  if(done || err){
    clearInterval(poll); poll=null;
    $("collectBtn").disabled=false;
    const rb=$("reprocessBtn"); if(rb) rb.disabled=false;
    ["collectMsg","reprocessMsg"].forEach(id=>{
      const m=$(id); if(!m) return;
      m.className = err ? "msg err" : "msg ok";
      m.textContent = err ? ("Error: "+(s.error||"")) : "Done.";
    });
    if(done){ loadMeta(); if(!$("view-search").classList.contains("hidden")) run(); }
  }
}
async function reprocess(){
  const rb=$("reprocessBtn"); if(rb) rb.disabled=true;
  const m=$("reprocessMsg"); if(m){ m.className="msg"; m.textContent="Refreshing…"; }
  const pdf = ($("reRenderPdf")||{checked:true}).checked;
  const r = await fetch("/api/reprocess",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({render_pdf:pdf})});
  const d = await r.json();
  if(!r.ok){ if(m){ m.className="msg err"; m.textContent=d.error||"busy"; } if(rb) rb.disabled=false; return; }
  if(poll) clearInterval(poll);
  poll=setInterval(pollStatus,1000); pollStatus();
}

// ---------- Search ----------
const inputs = ["q","date_from","date_to","item_number","order_type","warehouse","min_price","max_price"];
let sort = "date", order = "desc";
const COLS = {
  line: [["date","Date",0],["order_type","Type",0],["item_number","Item #",0],["description","Description",0],
         ["unit_qty","Qty",1],["unit_price","Unit $",1],["amount","Amount",1],
         ["warehouse","Warehouse",0],["receipt_id","Receipt",0]],
  group: [["order_type","Type",0],["item_number","Item #",0],["description","Description",0],
          ["times_purchased","Times",1],["total_qty","Total Qty",1],
          ["total_spent","Total $",1],["last_price","Last $",1],
          ["first_purchase","First",0],["last_purchase","Last",0]],
};
// Letter font-icon per transaction type: F(uel) / W(arehouse) / O(nline).
const TYPE_BADGE = { fuel:["F","#c9821f"], online:["O","#2d7dd2"], warehouse:["W","#2e7d46"] };
function typeBadge(t){ const [l,c]=TYPE_BADGE[t]||["W","#2e7d46"]; return `<span class="tbadge" style="--tc:${c}" title="${t||'warehouse'}">${l}</span>`; }
function money(v){ return "$"+(Number(v)||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
// Well-separated color per order: golden-angle hue steps (137.5°) by display
// order guarantee consecutive orders are far apart on the wheel; alternating
// lightness + a small saturation cycle push similar hues (e.g. magenta vs
// fuchsia) further apart so neighbours never look alike.
function orderColor(idx){
  const hue = (idx * 137.508) % 360;
  const light = idx % 2 ? 40 : 55;     // alternate dark/light bands
  const sat = 62 + (idx % 3) * 9;      // 62 / 71 / 80
  return `hsl(${hue.toFixed(1)} ${sat}% ${light}%)`;
}
function setTheme(t){
  localStorage.setItem('theme', t);
  document.documentElement.setAttribute('data-theme', t);
  const sel = document.getElementById('theme'); if(sel) sel.value = t;
}
// Product lookup: warehouse item #s don't map to a fixed URL, so link to a
// Costco catalog search for the item number (opens in a new tab).
function itemLink(num){
  const u = "https://www.costco.com/CatalogSearch?dept=All&keyword=" + encodeURIComponent(num);
  return `<a class="pdf" href="${u}" target="_blank" rel="noopener" title="Look up item ${num} on Costco.com">${num}</a>`;
}
function qs(){
  const p = new URLSearchParams();
  inputs.forEach(k => { if($(k).value) p.set(k, $(k).value); });
  if($("group").checked) p.set("group","1");
  p.set("sort", sort); p.set("order", order);
  return p.toString();
}
async function run(){
  const data = await (await fetch("/api/search?"+qs())).json();
  $("count").textContent = data.count.toLocaleString() + (data.grouped ? " items" : " lines");
  $("total").textContent = money(data.total_spent);
  const cols = data.grouped ? COLS.group : COLS.line;
  const grouped = data.grouped;
  const thead = $("tbl").querySelector("thead");
  const leadTh = grouped ? "" : `<th title="Order"></th>`;
  thead.innerHTML = "<tr>" + leadTh + cols.map(([k,label,num])=>
    `<th data-k="${k}" class="${num?'num':''} ${k===sort?'sorted '+(order==='asc'?'asc':''):''}">${label}</th>`
  ).join("") + "</tr>";
  thead.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if(sort===k){ order = order==="asc"?"desc":"asc"; } else { sort=k; order="desc"; }
    run();
  });
  const rows = data.rows;
  // Assign each distinct order an index in display order for well-spread colors.
  const orderIdx = {}; let _oi = 0;
  if(!grouped) for(const r of rows){ const id=r.receipt_id||""; if(!(id in orderIdx)) orderIdx[id]=_oi++; }
  const tb = $("tbl").querySelector("tbody");
  tb.innerHTML = rows.map((r,i) => {
    const cells = cols.map(([k,label,num])=>{
      let v = r[k];
      if(num) v = (k.includes("price")||k.includes("spent")||k==="amount") ? money(v) : (Number(v)||0).toLocaleString();
      else if(k==="order_type") v = typeBadge(r.order_type);
      // Exclude product metadata (lookup link) for gas/fuel purchases.
      else if(k==="item_number" && v) v = (r.order_type==="fuel") ? String(v) : itemLink(v);
      else if(k==="receipt_id" && v) v = `<a class="pdf" href="/pdf/${encodeURIComponent(v)}" target="_blank" rel="noopener">${v.slice(0,10)}…</a>`;
      else v = (v==null?"":String(v));
      return `<td class="${num?'num':''} ${k==='description'?'desc':''}">${v}</td>`;
    }).join("");
    if(grouped) return `<tr>${cells}</tr>`;
    // Order delineator: color band per receipt; bracket at group start/end.
    const id = r.receipt_id || "";
    const col = orderColor(orderIdx[id] || 0);
    const first = (rows[i-1]||{}).receipt_id !== id;
    const last  = (rows[i+1]||{}).receipt_id !== id;
    const lead = `<td class="ord"><span class="band"></span>${first?'<span class="sw"></span>':''}</td>`;
    return `<tr class="ord ${first?'ordf':''} ${last?'ordl':''}" style="--oc:${col}" title="Order ${id}">${lead}${cells}</tr>`;
  }).join("");
}
const debounce = (fn,ms)=>{ let t; return ()=>{ clearTimeout(t); t=setTimeout(fn,ms); }; };
inputs.forEach(k => { $(k).addEventListener("input", debounce(run,250)); $(k).addEventListener("change", run); });
$("group").addEventListener("change", ()=>{ sort = $("group").checked?"total_spent":"date"; run(); });
$("reset").onclick = ()=>{ inputs.forEach(k=>$(k).value=""); $("group").checked=false; sort="date"; order="desc"; run(); };

async function loadMeta(){
  const m = await (await fetch("/api/meta")).json();
  $("meta").textContent = `${m.total_line_items.toLocaleString()} line items · ${m.date_min||"?"} → ${m.date_max||"?"} · ${m.warehouses.length} warehouses`;
}
// If a collection is already running when the page loads, resume showing status.
(async ()=>{
  const sel = document.getElementById('theme');
  if(sel) sel.value = localStorage.getItem('theme') || 'system';
  loadMeta();
  run();  // Search is the landing page — populate it immediately.
  const s = await (await fetch("/api/collect/status")).json();
  if(["running","parsing","rendering"].includes(s.state)){ poll=setInterval(pollStatus,1000); pollStatus(); }
})();
</script></body></html>"""
