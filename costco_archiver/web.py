"""A small, dependency-free local web UI to search parsed receipt data.

Serves data/output/line_items.csv with free-text search and structured filters
(date range, price range, item number, warehouse), sortable columns, and a
"group by item" summary mode. Links each row to its PDF if one was rendered.

Run:  python -m costco_archiver web    (opens http://127.0.0.1:8000)
"""
from __future__ import annotations

import csv
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config

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
        elif path.startswith("/pdf/"):
            name = path[len("/pdf/"):]
            pdf = config.PDF_DIR / f"{name}.pdf"
            if pdf.exists():
                self._send(200, pdf.read_bytes(), "application/pdf")
            else:
                self._send(404, b'{"error":"pdf not found"}')
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
<title>Costco Receipt Search</title>
<style>
  :root { --bd:#d8dbe0; --fg:#1c2126; --muted:#6b7280; --accent:#005daa; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; color:var(--fg); background:#f6f7f9; }
  header { background:var(--accent); color:#fff; padding:12px 18px; }
  header h1 { margin:0; font-size:18px; }
  header .sub { font-size:12px; opacity:.9; }
  .wrap { padding:16px 18px; }
  .filters { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; background:#fff; border:1px solid var(--bd); border-radius:8px; padding:12px; }
  .filters label { font-size:11px; color:var(--muted); display:block; margin-bottom:3px; }
  .filters input, .filters select { width:100%; padding:6px 8px; border:1px solid var(--bd); border-radius:6px; font-size:13px; }
  .row2 { display:flex; align-items:center; gap:14px; margin:12px 2px; font-size:13px; color:var(--muted); flex-wrap:wrap; }
  .stat b { color:var(--fg); }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--bd); border-radius:8px; overflow:hidden; font-size:13px; }
  th,td { padding:7px 10px; text-align:left; border-bottom:1px solid #eef0f2; white-space:nowrap; }
  th { background:#f0f2f5; cursor:pointer; user-select:none; position:sticky; top:0; }
  th.sorted::after { content:" ▾"; }
  th.asc.sorted::after { content:" ▴"; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:hover td { background:#fbfcfe; }
  .desc { white-space:normal; min-width:220px; }
  a.pdf { color:var(--accent); text-decoration:none; }
  .toggle { display:flex; align-items:center; gap:6px; }
</style></head><body>
<header>
  <h1>Costco Receipt Search</h1>
  <div class="sub" id="meta">loading…</div>
</header>
<div class="wrap">
  <div class="filters">
    <div style="grid-column:1/-1"><label>Search (any term: description, item #, warehouse…)</label>
      <input id="q" placeholder="e.g. olive oil  ·  1610256  ·  kirkland" autofocus></div>
    <div><label>Date from</label><input id="date_from" type="date"></div>
    <div><label>Date to</label><input id="date_to" type="date"></div>
    <div><label>Item number</label><input id="item_number" placeholder="exact/partial"></div>
    <div><label>Warehouse</label><input id="warehouse" placeholder="contains…"></div>
    <div><label>Min price</label><input id="min_price" type="number" step="0.01"></div>
    <div><label>Max price</label><input id="max_price" type="number" step="0.01"></div>
  </div>
  <div class="row2">
    <span class="stat">Matches: <b id="count">0</b></span>
    <span class="stat">Total: <b id="total">$0.00</b></span>
    <label class="toggle"><input type="checkbox" id="group"> Group by item #</label>
    <span style="flex:1"></span>
    <button id="reset">Reset</button>
  </div>
  <div style="overflow:auto; max-height:70vh"><table id="tbl"><thead></thead><tbody></tbody></table></div>
</div>
<script>
const $ = id => document.getElementById(id);
const inputs = ["q","date_from","date_to","item_number","warehouse","min_price","max_price"];
let sort = "date", order = "desc";
const COLS = {
  line: [["date","Date",0],["item_number","Item #",0],["description","Description",0],
         ["unit_qty","Qty",1],["unit_price","Unit $",1],["amount","Amount",1],
         ["warehouse","Warehouse",0],["receipt_id","Receipt",0]],
  group: [["item_number","Item #",0],["description","Description",0],
          ["times_purchased","Times",1],["total_qty","Total Qty",1],
          ["total_spent","Total $",1],["last_price","Last $",1],
          ["first_purchase","First",0],["last_purchase","Last",0]],
};
function money(v){ return "$"+(Number(v)||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
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
  const thead = $("tbl").querySelector("thead");
  thead.innerHTML = "<tr>" + cols.map(([k,label,num])=>
    `<th data-k="${k}" class="${num?'num':''} ${k===sort?'sorted '+(order==='asc'?'asc':''):''}">${label}</th>`
  ).join("") + "</tr>";
  thead.querySelectorAll("th").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if(sort===k){ order = order==="asc"?"desc":"asc"; } else { sort=k; order="desc"; }
    run();
  });
  const tb = $("tbl").querySelector("tbody");
  tb.innerHTML = data.rows.map(r => "<tr>" + cols.map(([k,label,num])=>{
    let v = r[k];
    if(num) v = (k.includes("price")||k.includes("spent")||k==="amount") ? money(v) : (Number(v)||0).toLocaleString();
    else if(k==="receipt_id" && v) v = `<a class="pdf" href="/pdf/${encodeURIComponent(v)}" target="_blank">${v.slice(0,10)}…</a>`;
    else v = (v==null?"":String(v));
    return `<td class="${num?'num':''} ${k==='description'?'desc':''}">${v}</td>`;
  }).join("") + "</tr>").join("");
}
const debounce = (fn,ms)=>{ let t; return ()=>{ clearTimeout(t); t=setTimeout(fn,ms); }; };
inputs.forEach(k => $(k).addEventListener("input", debounce(run,250)));
$("group").addEventListener("change", ()=>{ sort = $("group").checked?"total_spent":"date"; run(); });
$("reset").onclick = ()=>{ inputs.forEach(k=>$(k).value=""); $("group").checked=false; sort="date"; order="desc"; run(); };
(async ()=>{
  const m = await (await fetch("/api/meta")).json();
  $("meta").textContent = `${m.total_line_items.toLocaleString()} line items · ${m.date_min||"?"} → ${m.date_max||"?"} · ${m.warehouses.length} warehouses`;
  run();
})();
</script></body></html>"""
