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
import re
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config, webauth

# Session cookie name for the web UI login.
_COOKIE = "cra_session"

# POST endpoints only admins may call. Operators are read-only: they can search
# and view, but not run data jobs (capture/collect/reprocess/refresh) or manage
# accounts (anything under /api/users, gated separately by prefix).
_ADMIN_POSTS = {"/api/capture", "/api/collect", "/api/reprocess", "/api/refresh_one"}

# --- collection job state (one at a time) -------------------------------------
_JOB = {"state": "idle", "message": "", "done": 0, "total": 0,
        "saved": 0, "error": None, "summary": None, "log": []}
_JOB_LOCK = threading.Lock()


def _log(msg: str):
    with _JOB_LOCK:
        _JOB["log"].append(msg)
        del _JOB["log"][:-200]  # keep last 200 lines


def _raw_key_for(receipt_id: str):
    """Map a search-row receipt_id (transactionBarcode) to its raw file stem."""
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", receipt_id)
    if (config.RAW_DIR / f"{safe}.json").exists():
        return safe
    for f in config.RAW_DIR.glob("*.json"):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        if str(r.get("transactionBarcode") or "") == receipt_id:
            return f.stem
    return None


def _refresh_one(receipt_id: str, do_pdf: bool) -> dict:
    """Regenerate one receipt's Markdown page, barcode, and (optionally) PDF."""
    key = _raw_key_for(receipt_id)
    if not key:
        return {"ok": False, "error": f"receipt {receipt_id} not found on disk"}
    from .markdown import generate_one
    md_ok = generate_one(key)
    pdf_ok = False
    if do_pdf:
        from .pdf import render_one_pdf
        pdf_ok = render_one_pdf(key)
    return {"ok": True, "receipt_id": receipt_id, "key": key,
            "markdown": md_ok, "pdf": pdf_ok}


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
_TEXT_FIELDS = ["item_number", "description", "warehouse", "warehouse_number",
                "receipt_id", "doc_type", "source", "department"]


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


def _parse_query(text: str):
    """Parse the search box into (negate, field, value) filters.

    Plain words / "quoted phrases" match text (substring across fields). Field
    tokens item: / store: / rcpt: / desc: match that field exactly. A leading '-'
    on any token excludes. E.g.  kirkland -store:358 item:1610256
    """
    out = []
    for tok in re.findall(r'-?\w+:"[^"]*"|-?"[^"]*"|-?\w+:\S+|-?\S+', text or ""):
        negate = tok.startswith("-")
        if negate:
            tok = tok[1:]
        m = re.match(r"(\w+):(.*)$", tok, re.S)
        field, val = (m.group(1).lower(), m.group(2)) if m else ("text", tok)
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        val = val.strip().lower()
        if val:
            out.append((negate, field, val))
    return out


def _query_match(r: dict, field: str, val: str) -> bool:
    if field == "item":
        return val == str(r.get("item_number", "")).lower()
    if field in ("store", "wh"):
        return val == str(r.get("warehouse_number", "")).lower()
    if field == "rcpt":
        return val == str(r.get("receipt_id", "")).lower()
    if field == "desc":
        return val == str(r.get("description", "")).lower()
    hay = " ".join(str(r.get(f, "")) for f in _TEXT_FIELDS).lower()
    return val in hay


def _num0(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _item_history(rows: list[dict], item: str, desc: str) -> dict:
    """Net price-over-time for one item (by item number, or description for NONUM
    items). Purchases are aggregated per receipt and the item's discounts on that
    receipt are subtracted, so the price reflects what was actually paid per unit."""
    # Discounts applied to each (receipt, item), summed (their amounts are negative).
    adj: dict[tuple, float] = {}
    for r in rows:
        if str(r.get("order_type", "")) == "discount" and r.get("discount_ref"):
            k = (r.get("receipt_id", ""), str(r.get("discount_ref")))
            adj[k] = adj.get(k, 0.0) + _num0(r.get("amount"))

    agg: dict[str, dict] = {}
    label = ""
    for r in rows:
        if item:
            if str(r.get("item_number", "")) != item:
                continue
        elif desc:
            if str(r.get("description", "")) != desc:
                continue
        else:
            continue
        if str(r.get("order_type", "")) in ("discount", "tax"):
            continue
        rid = r.get("receipt_id", "")
        e = agg.setdefault(rid, {"date": r.get("date", ""), "amt": 0.0, "qty": 0.0})
        e["amt"] += _num0(r.get("amount"))
        e["qty"] += _num0(r.get("unit_qty"))
        label = label or r.get("description", "")

    pts = []
    for rid, e in agg.items():
        net = e["amt"] + (adj.get((rid, item), 0.0) if item else 0.0)
        qty = e["qty"] or 1
        price = round(net / qty, 2)
        if price <= 0 or not e["date"]:
            continue
        pts.append({"date": e["date"], "price": price, "receipt_id": rid})
    pts.sort(key=lambda x: x["date"])
    prices = [p["price"] for p in pts]
    n = len(prices)
    return {
        "item_number": item, "description": label, "points": pts, "count": n,
        "avg": round(sum(prices) / n, 2) if n else 0,
        "min": min(prices) if prices else 0, "max": max(prices) if prices else 0,
    }


def _search(rows: list[dict], q: dict) -> dict:
    filters = _parse_query(q.get("q", [""])[0] or "")
    date_from = (q.get("date_from", [""])[0] or "").strip()
    date_to = (q.get("date_to", [""])[0] or "").strip()
    min_price = _num(q.get("min_price", [""])[0])
    max_price = _num(q.get("max_price", [""])[0])
    item_number = (q.get("item_number", [""])[0] or "").strip()
    warehouse = (q.get("warehouse", [""])[0] or "").strip().lower()
    otype_filter = (q.get("order_type", [""])[0] or "").strip().lower()
    tax = (q.get("tax", [""])[0] or "").strip().lower()
    sort = (q.get("sort", ["date"])[0] or "date")
    order = (q.get("order", ["desc"])[0] or "desc")
    group = (q.get("group", ["0"])[0] == "1")
    discounted_only = (q.get("discounted", ["0"])[0] == "1")
    # Items that carry an associated discount: (receipt_id, discounted item #).
    disc_items = set()
    if discounted_only:
        for r in rows:
            if str(r.get("order_type", "")) == "discount" and r.get("discount_ref"):
                disc_items.add((r.get("receipt_id", ""), str(r.get("discount_ref"))))

    def keep(r):
        for negate, field, val in filters:
            # positive token must be present; negative token must be absent.
            if _query_match(r, field, val) == negate:
                return False
        if date_from and (r.get("date") or "") < date_from:
            return False
        if date_to and (r.get("date") or "") > date_to:
            return False
        if item_number and item_number not in str(r.get("item_number", "")):
            return False
        # Warehouse filter matches either the name or the (atomic) number.
        if warehouse and warehouse not in str(r.get("warehouse", "")).lower() \
                and warehouse not in str(r.get("warehouse_number", "")).lower():
            return False
        if otype_filter and str(r.get("order_type", "")).lower() != otype_filter:
            return False
        if tax == "y" and str(r.get("tax_flag", "")).upper() != "Y":
            return False
        if tax == "n" and str(r.get("tax_flag", "")).upper() != "N":
            return False
        if tax == "exempt" and str(r.get("tax_exempt", "")).upper() != "Y":
            return False
        # "Has discount": keep discount lines and the items they apply to.
        if discounted_only and str(r.get("order_type", "")) != "discount" \
                and (r.get("receipt_id", ""), str(r.get("item_number", ""))) not in disc_items:
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

    # Total is the NET spend: discount lines lower it via their true (negative)
    # amount — never the positive per-line net shown in the table. Discounts are
    # also surfaced on their own as a separate stat.
    total_spent = round(sum(r.get("amount", 0.0) for r in matched), 2)
    total_discounts = round(sum(r.get("amount", 0.0) for r in matched
                                if str(r.get("order_type", "")) == "discount"), 2)
    limit = int(q.get("limit", ["1000"])[0])
    return {
        "count": len(result),
        "line_item_matches": len(matched),
        "total_spent": total_spent,
        "total_discounts": total_discounts,
        "grouped": group,
        "rows": result[:limit],
    }


class _Handler(BaseHTTPRequestHandler):
    rows: list[dict] = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # --- auth helpers ---------------------------------------------------------
    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return SimpleCookie(raw).get(_COOKIE).value  # type: ignore[union-attr]
        except Exception:
            return None

    def _current_user(self) -> str | None:
        return webauth.session_user(self._session_token())

    def _current_role(self) -> str:
        return webauth.session_role(self._session_token())

    def _require_admin(self) -> bool:
        """True if the caller is an admin; otherwise send 403 and return False."""
        if self._current_role() == "admin":
            return True
        self._send(403, b'{"error":"admin role required"}')
        return False

    def _cookie_header(self, token: str, *, expire: bool = False) -> tuple[str, str]:
        attrs = [f"{_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if config.COOKIE_SECURE:
            attrs.append("Secure")
        if expire:
            attrs.append("Max-Age=0")
        else:
            attrs.append(f"Max-Age={config.SESSION_TTL_SECONDS}")
        return ("Set-Cookie", "; ".join(attrs))

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _auth_status(self) -> bytes:
        user = self._current_user()
        return json.dumps({"authenticated": bool(user),
                           "user": user or "",
                           "role": webauth.get_role(user) if user else "",
                           "users_exist": webauth.users_exist()}).encode()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # Public (no session required): the login page and auth status probe.
        if path == "/login":
            self._send(200, _LOGIN_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/auth/status":
            self._send(200, self._auth_status())
            return
        # Everything else is gated behind a valid session.
        if not self._current_user():
            if path == "/":
                self._redirect("/login")
            else:
                self._send(401, b'{"error":"authentication required"}')
            return
        if path == "/":
            self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/search":
            q = parse_qs(parsed.query)
            out = json.dumps(_search(self.rows, q)).encode()
            self._send(200, out)
        elif path == "/api/item_history":
            q = parse_qs(parsed.query)
            item = (q.get("item", [""])[0] or "").strip()
            desc = (q.get("desc", [""])[0] or "").strip()
            out = json.dumps(_item_history(self.rows, item, desc)).encode()
            self._send(200, out)
        elif path == "/api/meta":
            warehouses = sorted({r.get("warehouse", "") for r in self.rows if r.get("warehouse")})
            # Count physical warehouses by their (atomic) number, not name — one
            # warehouse can appear under several name spellings.
            wh_nums = {r.get("warehouse_number", "") for r in self.rows if r.get("warehouse_number")}
            dates = sorted({r.get("date", "") for r in self.rows if r.get("date")})
            meta = {
                "total_line_items": len(self.rows),
                "warehouses": warehouses,
                "warehouse_count": len(wh_nums) if wh_nums else len(warehouses),
                "date_min": dates[0] if dates else "",
                "date_max": dates[-1] if dates else "",
            }
            self._send(200, json.dumps(meta).encode())
        elif path == "/api/users":
            # Admin-only: list accounts and their roles for the Users panel.
            if not self._require_admin():
                return
            self._send(200, json.dumps({"users": webauth.users_overview()}).encode())
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
        # Public auth endpoints (no existing session required).
        if path == "/api/login":
            body = self._read_json()
            user = str(body.get("username", "")).strip()
            if webauth.authenticate(user, str(body.get("password", "")),
                                    str(body.get("code", ""))):
                token = webauth.create_session(user)
                self._send(200, json.dumps(
                    {"ok": True, "user": user, "role": webauth.get_role(user)}).encode(),
                           extra_headers=[self._cookie_header(token)])
            else:
                self._send(401, json.dumps(
                    {"ok": False, "error": "Invalid username, password, or code."}
                ).encode())
            return
        if path == "/api/logout":
            webauth.destroy_session(self._session_token())
            self._send(200, json.dumps({"ok": True}).encode(),
                       extra_headers=[self._cookie_header("", expire=True)])
            return
        # All other POSTs require a valid session.
        if not self._current_user():
            self._send(401, b'{"error":"authentication required"}')
            return
        # Data jobs and account management are admin-only; operators are read-only.
        if path in _ADMIN_POSTS and not self._require_admin():
            return
        if path.startswith("/api/users"):
            self._handle_users_post(path)
        elif path == "/api/capture":
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
        elif path == "/api/refresh_one":
            body = self._read_json()
            rid = str(body.get("receipt_id", "")).strip()
            if not rid:
                self._send(400, json.dumps({"error": "receipt_id required"}).encode())
                return
            try:
                result = _refresh_one(rid, bool(body.get("render_pdf", True)))
                self._send(200 if result.get("ok") else 404, json.dumps(result).encode())
            except Exception as ex:
                self._send(500, json.dumps({"error": str(ex)}).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    # --- user management (admin only) -----------------------------------------
    def _enroll_payload(self, username: str, secret: str) -> dict:
        """TOTP enrollment details returned after creating a user / resetting MFA."""
        return {"secret": secret,
                "otpauth": webauth.provisioning_uri(username, secret),
                "issuer": config.AUTH_ISSUER}

    def _handle_users_post(self, path: str) -> None:
        """Create/update/delete accounts. Admin-only (checked here as well as by
        the /api/users prefix gate)."""
        if not self._require_admin():
            return
        body = self._read_json()
        username = str(body.get("username", "")).strip()
        try:
            if path == "/api/users":  # create
                password = str(body.get("password", ""))
                role = str(body.get("role", "") or webauth.DEFAULT_ROLE)
                if len(password) < 8:
                    self._send(400, json.dumps(
                        {"error": "password must be at least 8 characters"}).encode())
                    return
                secret = webauth.add_user(username, password, role=role)
                out = {"ok": True, "username": username,
                       "role": webauth.get_role(username),
                       **self._enroll_payload(username, secret)}
                self._send(200, json.dumps(out).encode())
            elif path == "/api/users/update":  # change role and/or password
                changed = []
                if body.get("role"):
                    webauth.set_role(username, str(body["role"]))
                    changed.append("role")
                if body.get("password"):
                    password = str(body["password"])
                    if len(password) < 8:
                        self._send(400, json.dumps(
                            {"error": "password must be at least 8 characters"}).encode())
                        return
                    webauth.set_password(username, password)
                    changed.append("password")
                if not changed:
                    self._send(400, json.dumps(
                        {"error": "nothing to update (role or password)"}).encode())
                    return
                self._send(200, json.dumps(
                    {"ok": True, "username": username, "changed": changed,
                     "role": webauth.get_role(username)}).encode())
            elif path == "/api/users/reset_mfa":
                secret = webauth.reset_totp(username)
                self._send(200, json.dumps(
                    {"ok": True, "username": username,
                     **self._enroll_payload(username, secret)}).encode())
            elif path == "/api/users/delete":
                webauth.delete_user(username)
                self._send(200, json.dumps({"ok": True, "username": username}).encode())
            else:
                self._send(404, b'{"error":"not found"}')
        except ValueError as ex:
            # Domain guards (duplicate user, unknown user, last-admin protection).
            self._send(400, json.dumps({"error": str(ex)}).encode())
        except Exception as ex:
            self._send(500, json.dumps({"error": str(ex)}).encode())


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    config.ensure_dirs()
    _Handler.rows = _load_rows()
    if not _Handler.rows:
        print("No parsed data found. Run `fetch` then `parse` first.")
    if not webauth.users_exist():
        print("⚠ No web accounts configured — the login page will reject everyone.\n"
              "  Create one first:  python -m costco_archiver auth adduser <name>")
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    print(f"Serving {len(_Handler.rows)} line items at {url}  "
          f"(login required · Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


_LOGIN_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Costco Receipt Archiver</title>
<script>(function(){ document.documentElement.setAttribute('data-theme',
  localStorage.getItem('theme') || 'system'); })();</script>
<style>
  :root { --bg:#f6f7f9; --fg:#1c2126; --muted:#6b7280; --card:#fff; --bd:#d8dbe0;
    --accent:#005daa; --on-accent:#fff; --input-bg:#fff; --err:#c5221f; color-scheme:light; }
  @media (prefers-color-scheme: dark){ :root[data-theme="system"]{
    --bg:#0f141a; --fg:#e6e9ee; --muted:#9aa4b2; --card:#161b22; --bd:#2b3440;
    --accent:#3b82f6; --input-bg:#0f141a; --err:#ff6b60; color-scheme:dark; } }
  :root[data-theme="dark"]{ --bg:#0f141a; --fg:#e6e9ee; --muted:#9aa4b2; --card:#161b22;
    --bd:#2b3440; --accent:#3b82f6; --input-bg:#0f141a; --err:#ff6b60; color-scheme:dark; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; min-height:100vh;
    display:flex; align-items:center; justify-content:center; background:var(--bg); color:var(--fg); }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:12px; padding:26px 24px;
    width:340px; max-width:92vw; box-shadow:0 10px 30px rgba(0,0,0,.12); }
  h1 { font-size:17px; margin:0 0 4px; }
  p.sub { font-size:12px; color:var(--muted); margin:0 0 18px; }
  label { display:block; font-size:12px; color:var(--muted); margin:12px 0 4px; }
  input { width:100%; padding:9px 10px; border:1px solid var(--bd); border-radius:8px;
    font-size:16px; background:var(--input-bg); color:var(--fg); }
  button { width:100%; margin-top:18px; background:var(--accent); color:var(--on-accent);
    border:0; padding:11px; border-radius:8px; font-size:14px; cursor:pointer; }
  button:disabled { opacity:.6; cursor:default; }
  .err { color:var(--err); font-size:13px; margin-top:12px; min-height:16px; }
  .note { font-size:12px; color:var(--muted); margin-top:14px; line-height:1.5; }
  code { background:rgba(128,128,128,.15); padding:1px 5px; border-radius:4px; }
</style></head><body>
  <form class="card" id="f" autocomplete="on">
    <h1>Costco Receipt Archiver</h1>
    <p class="sub">Sign in to view your receipts.</p>
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <label for="c">Authenticator code</label>
    <input id="c" name="code" inputmode="numeric" autocomplete="one-time-code"
           pattern="[0-9]*" placeholder="6-digit TOTP">
    <button id="btn" type="submit">Sign in</button>
    <div class="err" id="err"></div>
    <div class="note hidden" id="setup">No accounts exist yet. Create one from a terminal:
      <br><code>python -m costco_archiver auth adduser &lt;name&gt;</code></div>
  </form>
<script>
  const $ = id => document.getElementById(id);
  fetch("/api/auth/status").then(r=>r.json()).then(s=>{
    if(s && s.authenticated){ location.href = "/"; return; }
    if(s && !s.users_exist){ $("setup").classList.remove("hidden"); }
  }).catch(()=>{});
  $("f").addEventListener("submit", async (e)=>{
    e.preventDefault();
    $("err").textContent = ""; $("btn").disabled = true;
    try{
      const r = await fetch("/api/login", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({username:$("u").value, password:$("p").value, code:$("c").value})});
      const d = await r.json();
      if(r.ok && d.ok){ location.href = "/"; return; }
      $("err").textContent = d.error || "Sign in failed.";
    }catch(ex){ $("err").textContent = String(ex); }
    $("btn").disabled = false; $("c").value = ""; $("c").focus();
  });
</script>
<style>.hidden{display:none;}</style>
</body></html>"""


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
  .whoami { font-size:12px; opacity:.9; }
  .whoami:not(:empty)::before { content:"👤 "; }
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
  .enroll { background:var(--code); border:1px solid var(--bd); border-radius:6px; padding:10px 12px; margin-top:10px; font-size:13px; line-height:1.7; }
  .enroll code { word-break:break-all; }
  #usersTbl td, #usersTbl th { white-space:nowrap; }
  #usersTbl .btn.secondary { padding:5px 9px; font-size:12px; margin-right:4px; }
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
  .rfr { cursor:pointer; color:var(--muted); user-select:none; font-size:13px; }
  .rfr:hover { color:var(--accent); }
  /* Inline include(＋)/exclude(−) filter buttons */
  .flt { cursor:pointer; color:var(--muted); user-select:none; font-size:11px; font-weight:700; padding:0 1px; }
  .flt:hover { color:var(--accent); }
  /* Clickable price cell → price-history modal */
  td.pricelink { cursor:pointer; text-decoration:underline dotted; text-underline-offset:2px; }
  td.pricelink:hover { color:var(--accent); }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.5); display:flex; align-items:center; justify-content:center; z-index:1000; padding:12px; }
  .modalCard { background:var(--card); border:1px solid var(--bd); border-radius:12px; padding:16px 18px 18px; width:620px; max-width:96vw; box-shadow:0 20px 60px rgba(0,0,0,.35); }
  .modalHead { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; font-size:14px; }
  .modalX { cursor:pointer; color:var(--muted); font-size:15px; }
  .modalX:hover { color:var(--accent); }
  .pmStats { display:flex; gap:16px; flex-wrap:wrap; font-size:13px; color:var(--muted); margin-bottom:12px; }
  .pmStats b { color:var(--fg); }
  .pmsvg { background:var(--input-bg); border:1px solid var(--line); border-radius:8px; display:block; }
  .pmline { fill:none; stroke:var(--accent); stroke-width:2; }
  .pmavg { stroke:var(--muted); stroke-dasharray:4 3; stroke-width:1; }
  .pmgrid { stroke:var(--line); stroke-width:1; }
  .pmax { fill:var(--muted); font-size:11px; }
  .pmEmpty { color:var(--muted); font-size:13px; padding:20px; text-align:center; }
  /* Collapse trigger = the receipt's start-of-order block icon (the swatch). */
  td.ord.ordtrig { cursor:pointer; }
  td.ord.ordtrig > span.sw { transition:box-shadow .1s, transform .1s; }
  td.ord.ordtrig:hover > span.sw { box-shadow:0 0 0 2px var(--accent); transform:scale(1.15); }
  tr.osum td { background:var(--rowhover); font-variant-numeric:tabular-nums; }
  tr.osum .desc { color:var(--muted); }
  /* Tax-exempt "E" chip + discount hierarchy marker */
  .tebadge { display:inline-block; font-size:10px; font-weight:700; line-height:1.4;
    color:var(--muted); border:1px solid var(--bd); border-radius:3px; padding:0 3px;
    margin-right:5px; vertical-align:middle; }
  .disc-mark { color:var(--muted); margin-right:2px; }
  tr.discrow td { color:var(--muted); }
  /* Taxable Y/N column */
  td.taxcol, th.taxcol { text-align:center; }
  .tax-y { font-weight:700; }
  .tax-n { color:var(--muted); }
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
    <button id="tab-collect" class="hidden" onclick="showTab('collect')">Collect</button>
    <button id="tab-users" class="hidden" onclick="showTab('users')">Users</button>
    <select id="theme" class="themesel" title="Theme" onchange="setTheme(this.value)">
      <option value="system">🖥 System</option>
      <option value="light">☀ Light</option>
      <option value="dark">🌙 Dark</option>
    </select>
    <span id="whoami" class="whoami" title="Signed in"></span>
    <button class="themesel" title="Sign out" onclick="logout()">Sign out</button>
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
          <input id="q" placeholder="olive oil · item:1610256 · -store:358 (– excludes)"></div>
        <div><label>Date from</label><input id="date_from" type="date"></div>
        <div><label>Date to</label><input id="date_to" type="date"></div>
        <div><label>Item number</label><input id="item_number" placeholder="exact/partial"></div>
        <div><label>Type</label><select id="order_type">
          <option value="">All</option>
          <option value="warehouse">Warehouse</option>
          <option value="fuel">Fuel</option>
        </select></div>
        <div><label>Tax</label><select id="tax">
          <option value="">All</option>
          <option value="y">Taxable (Y)</option>
          <option value="n">Non-taxable (N)</option>
          <option value="exempt">Tax-exempt (E)</option>
        </select></div>
        <div><label>Warehouse</label><input id="warehouse" placeholder="name or #…"></div>
        <div><label>Min price</label><input id="min_price" type="number" step="0.01"></div>
        <div><label>Max price</label><input id="max_price" type="number" step="0.01"></div>
      </div>
      <div class="row2">
        <span class="stat">Matches: <b id="count">0</b></span>
        <span class="stat">Total: <b id="total">$0.00</b></span>
        <span class="stat">Discounts: <b id="discounts">$0.00</b></span>
        <label class="toggle"><input type="checkbox" id="group"> Group by item #</label>
        <label class="toggle" title="Only items that have an associated discount"><input type="checkbox" id="discounted"> Has discount</label>
        <label class="toggle"><input type="checkbox" id="collapseOrders"> Collapse orders</label>
        <span style="flex:1"></span>
        <button class="btn secondary" onclick="exportExcel()" title="Download the current view as a spreadsheet (CSV, opens in Excel)">⬇ Export</button>
        <button class="btn secondary" id="refreshBtn" onclick="reprocess()" title="Rebuild data & metadata from receipts on disk">↻ Refresh data</button>
        <button class="btn secondary" id="reset">Reset</button>
      </div>
    </div>
    <div class="tablewrap"><table id="tbl"><thead></thead><tbody></tbody></table></div>
  </div>

  <!-- ============ USERS (admin only) ============ -->
  <div id="view-users" class="hidden">
    <div class="card">
      <h2>Add user</h2>
      <p style="font-size:13px;color:var(--muted);margin:0 0 10px">
        <b>Operator</b> = view &amp; search only. <b>Admin</b> = full access, including
        collecting data and managing users. The account is created with a fresh TOTP
        secret shown once below — enroll it in an authenticator app.</p>
      <div class="filters" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
        <div><label>Username</label><input id="nu_user" autocomplete="off"></div>
        <div><label>Password (min 8)</label><input id="nu_pass" type="password" autocomplete="new-password"></div>
        <div><label>Role</label><select id="nu_role">
          <option value="operator">operator — view/search</option>
          <option value="admin">admin — full access</option>
        </select></div>
        <div style="display:flex;align-items:flex-end"><button class="btn" onclick="createUser()">Create user</button></div>
      </div>
      <div class="msg" id="nu_msg"></div>
      <div id="nu_enroll" class="enroll hidden"></div>
    </div>
    <div class="card">
      <h2>Accounts</h2>
      <div class="tablewrap"><table id="usersTbl"><thead>
        <tr><th>Username</th><th>Role</th><th>MFA</th><th>Created</th><th>Actions</th></tr>
      </thead><tbody></tbody></table></div>
      <div class="msg" id="um_msg"></div>
    </div>
  </div>
</div>

<!-- Price-history modal -->
<div id="priceModal" class="modal hidden" onclick="if(event.target===this) closePriceModal()">
  <div class="modalCard">
    <div class="modalHead"><b id="pmTitle"></b><span class="modalX" title="Close" onclick="closePriceModal()">✕</span></div>
    <div id="pmStats" class="pmStats"></div>
    <div id="pmChart"></div>
    <div id="pmEmpty" class="pmEmpty hidden">No price history for this item.</div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
// Any API 401 means the session lapsed — bounce to the login page.
async function api(url, opts){
  const r = await fetch(url, opts);
  if(r.status === 401){ location.href = "/login"; throw new Error("unauthorized"); }
  return r;
}
async function logout(){
  try{ await fetch("/api/logout", {method:"POST"}); }catch(e){}
  location.href = "/login";
}
// Current user's role, set from /api/auth/status. Operators are read-only.
let ROLE = "";
function isAdmin(){ return ROLE === "admin"; }
function showTab(name){
  // Operators can't reach admin-only tabs — fall back to Search.
  if((name==="collect" || name==="users") && !isAdmin()) name = "search";
  $("view-collect").classList.toggle("hidden", name!=="collect");
  $("view-search").classList.toggle("hidden", name!=="search");
  $("view-users").classList.toggle("hidden", name!=="users");
  $("tab-collect").classList.toggle("active", name==="collect");
  $("tab-search").classList.toggle("active", name==="search");
  $("tab-users").classList.toggle("active", name==="users");
  if(name==="search") run();
  if(name==="users") loadUsers();
}
// Show/hide admin-only controls based on ROLE.
function applyRole(){
  const admin = isAdmin();
  ["tab-collect","tab-users","refreshBtn"].forEach(id => {
    const el = $(id); if(el) el.classList.toggle("hidden", !admin);
  });
  // If an operator is somehow parked on an admin tab, bounce them to Search.
  if(!admin && (!$("view-collect").classList.contains("hidden")
             || !$("view-users").classList.contains("hidden"))) showTab("search");
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
      const kindTxt = d.has_query ? " (warehouse receipts request)" : "";
      msg.textContent = (d.expired ? "⚠ Token already expired — recopy a fresh cURL. " : "✓ Captured. ")
        + `${d.headers} headers` + (d.has_query?", query captured":"") + kindTxt + `, token ~${d.token_minutes} min left.`;
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
async function refreshOne(receiptId, el){
  const prev = el.textContent; el.textContent = "⏳"; el.style.pointerEvents="none";
  try{
    const r = await fetch("/api/refresh_one",{method:"POST",headers:{"Content-Type":"application/json"},
      body: JSON.stringify({receipt_id: receiptId, render_pdf: true})});
    const d = await r.json();
    el.textContent = (r.ok && d.ok) ? "✓" : "✗";
    el.title = (r.ok && d.ok) ? "Refreshed PDF, barcode & Markdown" : (d.error || "Failed");
  }catch(e){ el.textContent = "✗"; el.title = String(e); }
  setTimeout(()=>{ el.textContent = prev; el.style.pointerEvents=""; }, 2500);
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

// Inline filters append a token to the search box (visible + editable there),
// then let showTab('search') fire a single run(). Field tokens (item/store/rcpt)
// match exactly; an empty field is a text phrase; a leading '-' excludes.
function _q(v){ v = String(v).replace(/"/g, ""); return /\s/.test(v) ? '"'+v+'"' : v; }
function _tokAdd(tok){
  const cur = $("q").value.trim();
  $("q").value = cur ? cur + " " + tok : tok;
  showTab("search");
}
// field: "item" | "store" | "rcpt" | "" (text phrase). Value is URI-encoded at
// the call site so quotes/specials survive the inline handler.
function fInc(field, enc){ _tokAdd((field?field+":":"") + _q(decodeURIComponent(enc))); }
function fExc(field, enc){ _tokAdd("-" + (field?field+":":"") + _q(decodeURIComponent(enc))); }
// Render a ＋ (include) / − (exclude) button pair for a filterable cell value.
function fltBtns(field, val, label){
  const enc = encodeURIComponent(String(val)).replace(/'/g, "%27");
  return `<span class="flt" title="Only ${label}" onclick="fInc('${field}','${enc}')">＋</span>`
       + `<span class="flt" title="Exclude ${label}" onclick="fExc('${field}','${enc}')">－</span>`;
}

// ---------- Price-history modal ----------
function closePriceModal(){ $("priceModal").classList.add("hidden"); }
async function openPriceModal(item, enc, rid){
  const desc = decodeURIComponent(enc || "");
  const p = new URLSearchParams();
  if(item) p.set("item", item); else p.set("desc", desc);
  let d;
  try { d = await (await api("/api/item_history?"+p.toString())).json(); }
  catch(e){ return; }
  $("pmTitle").textContent = (d.description || desc || ("Item "+item)) + (item ? "  ·  #"+item : "");
  $("priceModal").classList.remove("hidden");
  if(!d.count){ $("pmStats").innerHTML=""; $("pmChart").innerHTML=""; $("pmEmpty").classList.remove("hidden"); return; }
  $("pmEmpty").classList.add("hidden");
  // The clicked purchase's net per-unit price (found by its receipt).
  const m = rid ? d.points.find(x=>String(x.receipt_id)===String(rid)) : null;
  const cl = m ? m.price : 0;
  const dpct = (cl && d.avg) ? Math.round((cl-d.avg)/d.avg*1000)/10 : null;
  $("pmStats").innerHTML =
      `<span>Average <b>${money(d.avg)}</b></span>`
    + `<span>Low <b>${money(d.min)}</b></span>`
    + `<span>High <b>${money(d.max)}</b></span>`
    + `<span>Purchases <b>${d.count}</b></span>`
    + (cl ? `<span>This <b>${money(cl)}</b>${dpct!=null?` (${dpct>0?"+":""}${dpct}% vs avg)`:""}</span>` : "");
  $("pmChart").innerHTML = priceChart(d, rid);
}
// Dependency-free SVG line chart of price over time, with a dashed average line.
// `rid` (a receipt id) highlights the clicked purchase's point.
function priceChart(d, rid){
  const pts = d.points, W=580, H=280, mL=56, mR=54, mT=16, mB=34;
  const iw=W-mL-mR, ih=H-mT-mB;
  const days = s => { const a=s.split("-").map(Number); return Date.UTC(a[0],a[1]-1,a[2])/86400000; };
  const xs = pts.map(p=>days(p.date));
  let x0=Math.min(...xs), x1=Math.max(...xs); if(x0===x1){ x0-=1; x1+=1; }
  const ys = pts.map(p=>p.price);
  let y0=Math.min(...ys, d.avg), y1=Math.max(...ys, d.avg);
  const pad=(y1-y0)*0.12 || (y1*0.1) || 1; y0=Math.max(0,y0-pad); y1+=pad;
  const X=v=> mL + (x1===x0?iw/2:(v-x0)/(x1-x0)*iw);
  const Y=v=> mT + ih - (y1===y0?ih/2:(v-y0)/(y1-y0)*ih);
  const line = pts.map((p,i)=>(i?"L":"M")+X(days(p.date)).toFixed(1)+" "+Y(p.price).toFixed(1)).join(" ");
  const dots = pts.map(p=>{ const hi = rid && String(p.receipt_id)===String(rid);
    return `<circle cx="${X(days(p.date)).toFixed(1)}" cy="${Y(p.price).toFixed(1)}" r="${hi?5:3}" fill="${hi?"#e67e22":"var(--accent)"}"${hi?' stroke="var(--card)" stroke-width="1.5"':""}><title>${p.date}: ${money(p.price)}</title></circle>`;
  }).join("");
  const grid = [y0,(y0+y1)/2,y1].map(v=>`<line x1="${mL}" y1="${Y(v).toFixed(1)}" x2="${W-mR}" y2="${Y(v).toFixed(1)}" class="pmgrid"/><text x="${mL-6}" y="${(Y(v)+3).toFixed(1)}" text-anchor="end" class="pmax">${money(v)}</text>`).join("");
  const ay = Y(d.avg).toFixed(1);
  const avg = `<line x1="${mL}" y1="${ay}" x2="${W-mR}" y2="${ay}" class="pmavg"/><text x="${W-mR+4}" y="${ay}" dominant-baseline="middle" class="pmax">avg</text>`;
  const xlab = [pts[0].date, pts[pts.length-1].date].map((dt,i)=>`<text x="${i?(W-mR).toFixed(1):mL}" y="${H-mB+16}" text-anchor="${i?"end":"start"}" class="pmax">${dt}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" class="pmsvg">${grid}${avg}<path d="${line}" class="pmline"/>${dots}${xlab}</svg>`;
}

// ---------- Per-order collapse (line view) ----------
// Orders whose line items are hidden (showing just a total + count summary).
// Kept across re-renders so a search/sort doesn't reopen collapsed orders.
const collapsed = new Set();
let lastOrderIds = [];
function _oidSel(id){ return (window.CSS && CSS.escape) ? CSS.escape(id) : id; }
function applyCollapse(id){
  const c = collapsed.has(id), esc = _oidSel(id);
  document.querySelectorAll(`tr.oline[data-oid="${esc}"]`).forEach(tr => tr.classList.toggle("hidden", c));
  const s = document.querySelector(`tr.osum[data-oid="${esc}"]`);
  if(s) s.classList.toggle("hidden", !c);
}
function toggleOrder(id, ev){
  if(ev) ev.stopPropagation();
  if(collapsed.has(id)) collapsed.delete(id); else collapsed.add(id);
  applyCollapse(id);
  // Un-check "Collapse orders" if the user manually re-opened one.
  const all = $("collapseOrders");
  if(all && all.checked && lastOrderIds.some(x => !collapsed.has(x))) all.checked = false;
}
function collapseAll(state){
  lastOrderIds.forEach(id => { if(state) collapsed.add(id); else collapsed.delete(id); applyCollapse(id); });
}

// ---------- Search ----------
const inputs = ["q","date_from","date_to","item_number","order_type","tax","warehouse","min_price","max_price"];
let sort = "date", order = "desc";
const COLS = {
  line: [["date","Date",0],["order_type","Type",0],["item_number","Item #",0],["description","Description",0],
         ["unit_qty","Qty",1],["unit_price","Unit $",1],["amount","Amount",1],["tax_flag","Tax",0],
         ["warehouse","Warehouse",0],["warehouse_number","Wh #",0],["receipt_id","Receipt",0]],
  group: [["order_type","Type",0],["item_number","Item #",0],["description","Description",0],
          ["times_purchased","Times",1],["total_qty","Total Qty",1],
          ["total_spent","Total $",1],["last_price","Last $",1],
          ["first_purchase","First",0],["last_purchase","Last",0]],
};
// Letter font-icon per transaction type: F(uel) / W(arehouse) / D(iscount) / T(ax).
const TYPE_BADGE = { fuel:["F","#c9821f"], warehouse:["W","#2e7d46"], discount:["D","#9b59b6"], tax:["T","#c0392b"] };
function typeBadge(t){ const [l,c]=TYPE_BADGE[t]||["W","#2e7d46"]; return `<span class="tbadge" style="--tc:${c}" title="${t||'warehouse'}">${l}</span>`; }
// Discount/tax lines are "child" lines nested under the item they reference.
function isChild(ot){ return ot==="discount" || ot==="tax"; }
// Costco's per-line taxFlag is a tax-category code, not a plain Y/N.
const TAX_TIP = {
  "Y": "Taxable — standard rate",
  "N": "Not taxed",
  "3": "Special tax category (Costco code 3)",
  "4": "Special tax category (Costco code 4 — e.g. liquor)",
};
function taxTip(code){ return TAX_TIP[code] || (code ? "Costco tax code "+code : ""); }
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
  if($("discounted").checked) p.set("discounted","1");
  p.set("sort", sort); p.set("order", order);
  return p.toString();
}
let lastData = null;
async function run(){
  const data = await (await api("/api/search?"+qs())).json();
  lastData = data;
  $("count").textContent = data.count.toLocaleString() + (data.grouped ? " items" : " lines");
  $("total").textContent = money(data.total_spent);
  const disc = data.total_discounts || 0;
  $("discounts").textContent = (disc ? "−" : "") + money(Math.abs(disc));
  const cols = data.grouped ? COLS.group : COLS.line;
  const grouped = data.grouped;
  const thead = $("tbl").querySelector("thead");
  const leadTh = grouped ? "" : `<th title="Order"></th>`;
  thead.innerHTML = "<tr>" + leadTh + cols.map(([k,label,num])=>
    `<th data-k="${k}" class="${num?'num':''} ${k==='tax_flag'?'taxcol':''} ${k===sort?'sorted '+(order==='asc'?'asc':''):''}">${label}</th>`
  ).join("") + "</tr>";
  thead.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if(sort===k){ order = order==="asc"?"desc":"asc"; } else { sort=k; order="desc"; }
    run();
  });
  const rows = data.rows;
  const tb = $("tbl").querySelector("tbody");

  // Render one <td> for a line/group row (`caretId` prepends a collapse caret
  // onto the Date cell of an order's first line).
  const receiptCell = v => `<a class="pdf" href="/pdf/${encodeURIComponent(v)}" target="_blank" rel="noopener">${v.slice(0,10)}…</a> `
    + fltBtns('rcpt', v, 'this order')
    + (isAdmin() ? ` <span class="rfr" title="Refresh this receipt's PDF, barcode & Markdown" onclick="refreshOne('${v}', this)">↻</span>` : "");
  const cell = (r,k,num) => {
    let v = r[k];
    if(num){
      // Discount/tax line: show the adjustment in Unit $, and the item's total
      // combined with it (net for a discount, plus-tax for a tax) in Amount.
      if(isChild(r.order_type) && k==="unit_price") v = money(r.amount);
      else if(isChild(r.order_type) && k==="amount") v = money((Number(r._parentAmt)||0) + (Number(r.amount)||0));
      // Fuel: price/gallon carries 3 decimals (e.g. $3.459); qty is gallons.
      else if(r.order_type==="fuel" && k==="unit_price") v = "$"+(Number(v)||0).toFixed(3);
      else if(r.order_type==="fuel" && k==="unit_qty") v = (Number(v)||0).toFixed(3);
      else v = (k.includes("price")||k.includes("spent")||k==="amount") ? money(v) : (Number(v)||0).toLocaleString();
    }
    else if(k==="order_type") v = typeBadge(r.order_type);
    // Exclude product metadata (lookup link) for gas/fuel purchases.
    else if(k==="item_number" && v){ const num2 = String(v);
      // Discount/tax lines carry a code, not a product #: show it plain (no lookup).
      v = isChild(r.order_type) ? num2
        : (((r.order_type==="fuel") ? num2 : itemLink(num2))
           + " " + fltBtns('item', num2, 'item '+num2)); }
    else if(k==="description" && v){ const d = String(v);
      const te = (r.tax_exempt==="Y") ? `<span class="tebadge" title="Tax-exempt (E)">E</span>` : "";
      const child = isChild(r.order_type);
      const dm = child ? `<span class="disc-mark" title="Applies to the item above">↳</span> ` : "";
      // No filter buttons on discount/tax lines (don't filter on their names).
      const filt = child ? "" : " " + fltBtns('', d, 'this description');
      v = te + dm + d + filt; }
    else if(k==="warehouse_number" && v){ const wn = String(v);
      v = wn + " " + fltBtns('store', wn, 'store '+wn); }
    else if(k==="receipt_id" && v) v = receiptCell(v);
    // Taxable indicator (the code printed at the far right of the receipt line);
    // hover shows what the code means.
    else if(k==="tax_flag") v = r.tax_flag ? `<span class="${r.tax_flag==='Y'?'tax-y':'tax-n'}" title="${taxTip(r.tax_flag)}">${r.tax_flag}</span>` : "";
    else v = (v==null?"":String(v));
    let cls = (num?"num ":"") + (k==="tax_flag"?"taxcol ":"") + (k==="description"?"desc ":"");
    let attr = "";
    // Price cells (Unit $ / grouped Last $) open a price-history chart for the item.
    if(!isChild(r.order_type) && r.item_number && (k==="unit_price"||k==="last_price") && (Number(r[k])||0)>0){
      cls += "pricelink ";
      const enc = encodeURIComponent(String(r.description||"")).replace(/'/g,"%27");
      attr = ` title="Price history" onclick="openPriceModal('${String(r.item_number)}','${enc}','${String(r.receipt_id||"")}')"`;
    }
    return `<td class="${cls.trim()}"${attr}>${v}</td>`;
  };

  if(grouped){
    tb.innerHTML = rows.map(r => `<tr>${cols.map(([k,l,num])=>cell(r,k,num)).join("")}</tr>`).join("");
    lastOrderIds = [];
    return;
  }

  // Line view: group rows into per-order blocks (first-appearance order) so each
  // transaction can collapse to a single summary row (item count + order total).
  const blocks = [], byId = {}; let oi = 0;
  for(const r of rows){
    const id = r.receipt_id || "";
    let b = byId[id];
    if(!b){ b = byId[id] = {id, idx: oi++, lines: [], total: 0}; blocks.push(b); }
    b.lines.push(r); b.total += Number(r.amount) || 0;
  }
  lastOrderIds = blocks.map(b => b.id);

  let html = "";
  for(const b of blocks){
    const id = b.id, col = orderColor(b.idx), isC = collapsed.has(id);
    // Nest each discount/tax line directly under the item it references
    // (discount_ref); children with no resolvable parent fall to the order's end.
    const discByRef = {};
    b.lines.forEach(r => { if(isChild(r.order_type)){ const k=r.discount_ref||""; (discByRef[k]=discByRef[k]||[]).push(r); } });
    const usedRefs = new Set(), ordered = [];
    b.lines.forEach(r => {
      if(isChild(r.order_type)) return;
      ordered.push(r);
      const ds = discByRef[r.item_number];
      if(ds && !usedRefs.has(r.item_number)){
        // Remember the item's total so a child line can show the combined price
        // (item total ± the adjustment) in its Amount column.
        ds.forEach(d => { d._parentAmt = r.amount; ordered.push(d); });
        usedRefs.add(r.item_number);
      }
    });
    b.lines.filter(r => isChild(r.order_type) && (!r.discount_ref || !usedRefs.has(r.discount_ref)))
           .forEach(d => ordered.push(d));
    b.lines = ordered;
    const n = b.lines.length, m = b.lines[0];
    const nItems = b.lines.filter(r => !isChild(r.order_type)).length;  // discount/tax aren't items
    // Collapsed summary row (hidden while expanded): item count + order total. Its
    // start-of-order block icon expands the order back open.
    const sum = cols.map(([k,l,num]) => {
      let v = "";
      if(k==="date") v = String(m.date||"");
      else if(k==="order_type") v = typeBadge(m.order_type);
      else if(k==="description") v = `<b>${nItems} item${nItems===1?'':'s'}</b>`;
      else if(k==="amount") v = `<b>${money(b.total)}</b>`;
      else if(k==="warehouse") v = String(m.warehouse||"");
      else if(k==="receipt_id" && id) v = receiptCell(id);
      return `<td class="${num?'num':''} ${k==='tax_flag'?'taxcol':''} ${k==='description'?'desc':''}">${v}</td>`;
    }).join("");
    const sumLead = `<td class="ord ordtrig" title="Expand order" onclick="toggleOrder('${id}',event)"><span class="band"></span><span class="sw"></span></td>`;
    html += `<tr class="ord osum ordf ordl ${isC?'':'hidden'}" data-oid="${id}" style="--oc:${col}" title="Order ${id}">${sumLead}${sum}</tr>`;
    // Expanded line rows (hidden while collapsed). The first row's block icon —
    // the swatch marking the start of the receipt — collapses the order.
    b.lines.forEach((r,j) => {
      const first = j===0, last = j===n-1, child = isChild(r.order_type);
      const cells = cols.map(([k,l,num]) => cell(r, k, num)).join("");
      const lead = first
        ? `<td class="ord ordtrig" title="Collapse order" onclick="toggleOrder('${id}',event)"><span class="band"></span><span class="sw"></span></td>`
        : `<td class="ord"><span class="band"></span></td>`;
      html += `<tr class="ord oline ${child?'discrow':''} ${first?'ordf':''} ${last?'ordl':''} ${isC?'hidden':''}" data-oid="${id}" style="--oc:${col}" title="Order ${id}">${lead}${cells}</tr>`;
    });
  }
  tb.innerHTML = html;
  // Keep the bulk "Collapse orders" toggle authoritative for newly-matched orders.
  if($("collapseOrders").checked) collapseAll(true);
}
const debounce = (fn,ms)=>{ let t; return ()=>{ clearTimeout(t); t=setTimeout(fn,ms); }; };
inputs.forEach(k => { $(k).addEventListener("input", debounce(run,250)); $(k).addEventListener("change", run); });
$("group").addEventListener("change", ()=>{ sort = $("group").checked?"total_spent":"date"; run(); });
$("collapseOrders").addEventListener("change", ()=> collapseAll($("collapseOrders").checked));
$("discounted").addEventListener("change", run);
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closePriceModal(); });
$("reset").onclick = ()=>{ inputs.forEach(k=>$(k).value=""); $("group").checked=false;
  $("discounted").checked=false; $("collapseOrders").checked=false; collapsed.clear();
  sort="date"; order="desc"; run(); };

// ---------- Export current view to a spreadsheet (CSV, opens in Excel) ----------
function csvCell(v){
  v = (v==null ? "" : String(v));
  return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g,'""') + '"' : v;
}
// Mirror the table's display values so the export matches the current view:
// discounts show the discount in Unit $ and the item's net in Amount; fuel keeps
// gallons / 3-decimal price. Everything else is the raw field value.
function exportVal(r,k){
  if(isChild(r.order_type)){
    if(k==="unit_price") return Number(r.amount)||0;
    if(k==="amount") return (Number(r._parentAmt)||0) + (Number(r.amount)||0);
  }
  return r[k];
}
function exportExcel(){
  if(!lastData || !lastData.rows || !lastData.rows.length){ return; }
  const cols = lastData.grouped ? COLS.group : COLS.line;
  const rows = [cols.map(c => csvCell(c[1])).join(",")];
  for(const r of lastData.rows){
    rows.push(cols.map(([k]) => csvCell(exportVal(r,k))).join(","));
  }
  const csv = "\ufeff" + rows.join("\r\n");    // BOM so Excel reads UTF-8
  const blob = new Blob([csv], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "costco-receipts.csv";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
}

async function loadMeta(){
  const m = await (await api("/api/meta")).json();
  const whn = (m.warehouse_count!=null ? m.warehouse_count : m.warehouses.length);
  $("meta").textContent = `${m.total_line_items.toLocaleString()} line items · ${m.date_min||"?"} → ${m.date_max||"?"} · ${whn} warehouses`;
}
async function loadWhoami(){
  try{
    const s = await (await fetch("/api/auth/status")).json();
    ROLE = s.role || "";
    const el = $("whoami"); if(el) el.textContent = (s.user || "") + (s.role ? ` · ${s.role}` : "");
    applyRole();
  }catch(e){}
}

// ---------- Users (admin only) ----------
function fmtDate(ts){ if(!ts) return ""; try{ return new Date(ts*1000).toISOString().slice(0,10); }catch(e){ return ""; } }
function enrollHtml(d){
  return `<b>MFA enrollment for ${d.username||""}</b> — scan or paste into an authenticator app (shown once):<br>`
    + `Account: <code>${d.issuer||""}:${d.username||""}</code><br>`
    + `Secret: <code>${d.secret||""}</code><br>`
    + `otpauth: <code>${d.otpauth||""}</code>`;
}
async function loadUsers(){
  const tb = $("usersTbl").querySelector("tbody");
  let d;
  try{ d = await (await api("/api/users")).json(); }catch(e){ return; }
  const rows = d.users || [];
  tb.innerHTML = rows.map(u => {
    const other = u.role==="admin" ? "operator" : "admin";
    const enc = encodeURIComponent(u.username).replace(/'/g,"%27");
    return `<tr>
      <td>${u.username}</td><td>${u.role}</td><td>${u.mfa?"✓":"—"}</td><td>${fmtDate(u.created_at)}</td>
      <td>
        <button class="btn secondary" onclick="setUserRole('${enc}','${other}')" title="Switch role to ${other}">→ ${other}</button>
        <button class="btn secondary" onclick="resetUserPassword('${enc}')" title="Set a new password">Password</button>
        <button class="btn secondary" onclick="resetUserMfa('${enc}')" title="Regenerate TOTP secret">Reset MFA</button>
        <button class="btn secondary" onclick="deleteUser('${enc}')" title="Delete account">Delete</button>
      </td></tr>`;
  }).join("") || `<tr><td colspan="5" style="color:var(--muted)">No users.</td></tr>`;
}
async function usersPost(url, body){
  const msg = $("um_msg"); msg.className = "msg"; msg.textContent = "Working…";
  try{
    const r = await api(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const d = await r.json();
    if(!r.ok || !d.ok){ msg.className = "msg err"; msg.textContent = d.error || "Failed"; return d; }
    msg.className = "msg ok"; msg.textContent = "Done."; loadUsers(); return d;
  }catch(e){ msg.className = "msg err"; msg.textContent = String(e); }
}
async function createUser(){
  const msg = $("nu_msg"); msg.className = "msg"; msg.textContent = "Creating…";
  const body = { username: $("nu_user").value.trim(), password: $("nu_pass").value, role: $("nu_role").value };
  if(!body.username){ msg.className="msg err"; msg.textContent="Username required."; return; }
  try{
    const r = await api("/api/users", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const d = await r.json();
    if(!r.ok || !d.ok){ msg.className = "msg err"; msg.textContent = d.error || "Failed"; return; }
    msg.className = "msg ok"; msg.textContent = `Created ${d.role} ${d.username}.`;
    $("nu_enroll").classList.remove("hidden"); $("nu_enroll").innerHTML = enrollHtml(d);
    $("nu_user").value = ""; $("nu_pass").value = "";
    loadUsers();
  }catch(e){ msg.className = "msg err"; msg.textContent = String(e); }
}
async function setUserRole(enc, role){ await usersPost("/api/users/update", { username: decodeURIComponent(enc), role }); }
async function resetUserPassword(enc){
  const pw = prompt("New password (min 8 characters):");
  if(pw == null) return;
  await usersPost("/api/users/update", { username: decodeURIComponent(enc), password: pw });
}
async function resetUserMfa(enc){
  const u = decodeURIComponent(enc);
  if(!confirm(`Reset MFA for ${u}? Their old codes stop working.`)) return;
  const d = await usersPost("/api/users/reset_mfa", { username: u });
  if(d && d.ok){ $("nu_enroll").classList.remove("hidden"); $("nu_enroll").innerHTML = enrollHtml(d); }
}
async function deleteUser(enc){
  const u = decodeURIComponent(enc);
  if(!confirm(`Delete ${u}? This cannot be undone.`)) return;
  await usersPost("/api/users/delete", { username: u });
}
// If a collection is already running when the page loads, resume showing status.
(async ()=>{
  const sel = document.getElementById('theme');
  if(sel) sel.value = localStorage.getItem('theme') || 'system';
  loadWhoami();
  loadMeta();
  run();  // Search is the landing page — populate it immediately.
  const s = await (await fetch("/api/collect/status")).json();
  if(["running","parsing","rendering"].includes(s.state)){ poll=setInterval(pollStatus,1000); pollStatus(); }
})();
</script></body></html>"""
