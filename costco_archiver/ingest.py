"""Turn a rendered Costco receipt (HTML or PDF text) into our receipt JSON.

This is the API-free data path: Costco's bot protection (Kasada) blocks scripted
logins, but you can still open a receipt in your normal browser. Save its HTML
(DevTools: right-click the receipt element → Copy → Copy element / outerHTML) or
its printed PDF, and this module parses it into the same schema `fetch` produces,
so it flows through `parse`, the web UI, and PDF rendering unchanged.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from . import config

# "9.99 N" / "371.70" / "38.77 Y" — amount with optional tax flag.
_AMT = re.compile(r"^\s*([\d,]+\.\d{2})\s*([NYA])?\s*$")
_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_TIME = re.compile(r"\b(\d{2}):(\d{2})\b")
_ITEMS_SOLD = re.compile(r"ITEMS?\s+SOLD\s*=\s*(\d+)", re.I)
_BARCODE = re.compile(r"^\d{18,}$")


class _ReceiptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._class_stack: list[str] = []
        self.by_class: dict[str, list[str]] = {}
        self._pending_class: str | None = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")
        if tag in ("tr",):
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        # Track first token of interesting div classes for header/address.
        toks = cls.split()
        for key in ("header", "address", "address1"):
            if key in toks:
                self._pending_class = key
        self._class_stack.append(cls)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            # Join text nodes with a space so adjacent <span>s (e.g. date + time)
            # don't fuse into "07/06/202616:52".
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        if self._class_stack:
            self._class_stack.pop()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        text = data.strip()
        if not text:
            return
        if self._pending_class:
            self.by_class.setdefault(self._pending_class, []).append(text)
            self._pending_class = None
        # Barcode number appears as a standalone digit run outside cells.
        if self._cell is None and _BARCODE.match(text):
            self.by_class.setdefault("_barcode", []).append(text)


def _money(s: str) -> float:
    try:
        return round(float(str(s).replace(",", "").replace("$", "").strip()), 2)
    except ValueError:
        return 0.0


def receipt_from_html(html_text: str) -> dict:
    p = _ReceiptParser()
    p.feed(html_text)
    rows = p.rows
    flat_text = " ".join(" ".join(r) for r in rows) + " " + " ".join(
        v for vs in p.by_class.values() for v in vs
    )

    items = []
    subtotal = taxes = total = 0.0
    for r in rows:
        cells = [c for c in r]
        # Item row: [flag, itemNumber, description, "amount [flag]"]
        if len(cells) == 4 and cells[1].isdigit() and _AMT.match(cells[3]):
            m = _AMT.match(cells[3])
            items.append({
                "itemNumber": cells[1],
                "itemDescription01": cells[2],
                "itemDescription02": "",
                "unit": 1,
                "itemUnitPriceAmount": _money(m.group(1)),
                "amount": _money(m.group(1)),
                "taxFlag": (m.group(2) or ""),
                "entryMethod": cells[0],
                "itemDepartmentNumber": "",
            })
            continue
        joined = " ".join(cells).upper()
        last_amt = next((c for c in reversed(cells) if _AMT.match(c)), None)
        if last_amt is None:
            continue
        val = _money(_AMT.match(last_amt).group(1))
        if "SUBTOTAL" in joined:
            subtotal = val
        elif re.search(r"\bTOTAL\b", joined) and "NUMBER" not in joined and "TAX" not in joined:
            total = val or total
        elif joined.strip().startswith("TAX") or "TOTAL TAX" in joined:
            taxes = val or taxes

    # Header / warehouse / barcode / member / date.
    warehouse = (p.by_class.get("header") or [""])[0]
    wh_num = ""
    m = re.search(r"#\s*(\d+)", warehouse)
    if m:
        wh_num = m.group(1)
    if not wh_num:
        m = re.search(r"WHSE:\s*(\d+)", flat_text, re.I)
        wh_num = m.group(1) if m else ""
    barcode = (p.by_class.get("_barcode") or [""])[0]
    member = ""
    mm = re.search(r"MEMBER\s*(\d+)", flat_text, re.I)
    if mm:
        member = mm.group(1)
    items_sold = ""
    mi = _ITEMS_SOLD.search(flat_text)
    if mi:
        items_sold = int(mi.group(1))

    dt_iso = ""
    md = _DATE.search(flat_text)
    date_only = ""
    if md:
        date_only = f"{md.group(3)}-{md.group(1)}-{md.group(2)}"
        mt = _TIME.search(flat_text)
        tm = f"{mt.group(1)}:{mt.group(2)}:00" if mt else "00:00:00"
        dt_iso = f"{date_only}T{tm}"

    return {
        "transactionBarcode": barcode,
        "transactionDateTime": dt_iso,
        "transactionDate": date_only,
        "warehouseName": warehouse,
        "warehouseShortName": warehouse,
        "warehouseNumber": wh_num,
        "documentType": "warehouse",
        "transactionType": "Sales",
        "member": member,
        "subTotal": subtotal,
        "taxes": taxes,
        "total": total,
        "totalItemCount": items_sold or len(items),
        "instantSavings": 0.0,
        "itemArray": items,
        "source": "html-import",
    }


def receipt_from_pdf(path: Path) -> dict:
    """Best-effort parse of a printed receipt PDF (needs pypdf)."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise SystemExit("PDF import needs pypdf: pip install pypdf") from e
    text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    return _receipt_from_text(text)


def _receipt_from_text(text: str) -> dict:
    """Parse plain receipt text (from a PDF). Handles wrapped descriptions."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    items, subtotal, taxes, total = [], 0.0, 0.0, 0.0
    # Merge wrapped item lines: an item starts with optional 'E', a number, and
    # ends (possibly lines later) with 'amount [flag]'.
    # Item number must be the whole token (followed by space or end) so amount
    # lines like "19.99 N" are NOT mistaken for a new item number.
    item_line = re.compile(r"^(E\s+)?(\d{3,7})(?:\s+(.*))?$")
    tail = re.compile(r"^(.*?)([\d,]+\.\d{2})\s*([NYA])?$")
    pending = None
    started = False  # only collect items between the Member line and SUBTOTAL

    def _new_item(num, desc, entry):
        return {"itemNumber": num, "itemDescription01": desc.strip(),
                "itemDescription02": "", "unit": 1, "amount": 0.0,
                "itemUnitPriceAmount": 0.0, "taxFlag": "",
                "entryMethod": "E" if entry else "", "itemDepartmentNumber": ""}

    def _complete(item, desc_extra, amt, flag):
        if desc_extra:
            item["itemDescription01"] = (item["itemDescription01"] + " " + desc_extra).strip()
        item["amount"] = item["itemUnitPriceAmount"] = _money(amt)
        item["taxFlag"] = flag or ""
        items.append(item)

    for ln in lines:
        u = ln.upper()
        if not started:
            if re.match(r"^MEMBER\b", u):
                started = True
            continue
        if u.startswith("SUBTOTAL"):
            m = tail.match(ln)
            subtotal = _money(m.group(2)) if m else subtotal
            if pending:
                items.append(pending)
            pending = None
            started = False
            continue
        # A pending item is still waiting for its amount (wrapped description).
        if pending is not None:
            t = tail.match(ln)
            if t and t.group(2):
                _complete(pending, t.group(1).strip(), t.group(2), t.group(3))
                pending = None
                continue
            if item_line.match(ln):
                items.append(pending)  # flush incomplete (rare)
                pending = None
            else:
                pending["itemDescription01"] += " " + ln.strip()
                continue
        m = item_line.match(ln)
        if m:
            desc = m.group(3) or ""
            t = tail.match(desc)
            if t and t.group(2):
                item = _new_item(m.group(2), t.group(1), m.group(1))
                _complete(item, "", t.group(2), t.group(3))
            else:
                pending = _new_item(m.group(2), desc, m.group(1))
    if pending:
        items.append(pending)

    mtax = re.search(r"\bTAX\s+([\d,]+\.\d{2})", text)
    taxes = _money(mtax.group(1)) if mtax else taxes
    mtot = re.search(r"\bTOTAL\s+([\d,]+\.\d{2})", text)
    total = _money(mtot.group(1)) if mtot else total

    md = _DATE.search(text); mt = _TIME.search(text)
    date_only = f"{md.group(3)}-{md.group(1)}-{md.group(2)}" if md else ""
    dt_iso = f"{date_only}T{mt.group(1)}:{mt.group(2)}:00" if (md and mt) else ""
    barcode = next((ln.strip() for ln in lines if _BARCODE.match(ln.strip())), "")
    wh = next((ln for ln in lines if re.search(r"#\s*\d+", ln)), "")
    mi = _ITEMS_SOLD.search(text)
    mmem = re.search(r"MEMBER\s+(\d{6,})", text, re.I)
    return {
        "transactionBarcode": barcode, "transactionDateTime": dt_iso,
        "transactionDate": date_only, "warehouseName": wh, "warehouseShortName": wh,
        "member": mmem.group(1) if mmem else "",
        "warehouseNumber": (re.search(r"#\s*(\d+)", wh) or [None, ""])[1] if wh else "",
        "documentType": "warehouse", "transactionType": "Sales",
        "subTotal": subtotal, "taxes": taxes, "total": total,
        "totalItemCount": int(mi.group(1)) if mi else len(items),
        "instantSavings": 0.0, "itemArray": items, "source": "pdf-import",
    }


def _safe_name(receipt: dict) -> str:
    key = receipt.get("transactionBarcode") or "-".join(
        str(receipt.get(k, "")) for k in ("transactionDate", "total"))
    return re.sub(r"[^A-Za-z0-9._-]", "_", key) or "receipt"


def save_receipt(receipt: dict, raw_dir: Path = config.RAW_DIR) -> Path:
    import json
    config.ensure_dirs()
    out = raw_dir / f"{_safe_name(receipt)}.json"
    out.write_text(json.dumps(receipt, indent=2))
    return out


def _ingest_json_file(f: Path, raw_dir: Path) -> int:
    """A .json export (API response, or array/obj of receipts). Extracts every
    receipt-like object and saves each — this is the bulk path used by the
    browser-console snippet that downloads your whole receipt history."""
    import json
    from .api import find_receipts
    blob = json.loads(f.read_text())
    receipts = find_receipts(blob)
    if not receipts and isinstance(blob, dict) and blob.get("itemArray"):
        receipts = [blob]
    saved = 0
    for rec in receipts:
        if not rec.get("itemArray"):
            continue
        rec.setdefault("source", "json-import")
        save_receipt(rec, raw_dir)
        saved += 1
    print(f"  {f.name} → {saved} receipt(s)")
    return saved


def ingest_paths(paths: list[Path], raw_dir: Path = config.RAW_DIR) -> dict:
    """Ingest .html/.pdf/.json files (or dirs) into raw receipt JSON."""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            for ext in ("*.html", "*.htm", "*.pdf", "*.json"):
                files += sorted(p.glob(ext))
        else:
            files.append(p)
    saved = 0
    for f in files:
        try:
            if f.suffix.lower() == ".json":
                saved += _ingest_json_file(f, raw_dir)
                continue
            if f.suffix.lower() in (".html", ".htm"):
                rec = receipt_from_html(f.read_text())
            elif f.suffix.lower() == ".pdf":
                rec = receipt_from_pdf(f)
            else:
                continue
        except Exception as ex:
            print(f"  ! failed to parse {f.name}: {ex}")
            continue
        if not rec.get("itemArray"):
            print(f"  ! no items found in {f.name} (skipped)")
            continue
        out = save_receipt(rec, raw_dir)
        saved += 1
        print(f"  {f.name} → {out.name}  ({len(rec['itemArray'])} items, total {rec.get('total')})")
    return {"ingested": saved, "raw_dir": str(raw_dir)}
