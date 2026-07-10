"""Render each downloaded receipt (raw JSON) into a PDF archive.

Costco's warehouse/gas receipts have no official per-receipt PDF — the site
renders them from JSON. So we build a clean, printable HTML receipt from each
raw JSON and convert it to PDF with the headless Chromium already installed for
Playwright. This runs fully locally: no login, no bot-detection.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import config
from .barcode_util import barcode_svg
from .parse import (order_type, item_description, discount_ref, tax_ref,
                    resolve_discount, is_tax_exempt, fuel_gallons, warehouse_name,
                    _item_is_fuel, _num, tax_code_label)

# Chromium stamps each PDF with a wall-clock /CreationDate and /ModDate, so two
# renders of identical content differ only in those bytes. Blank them out before
# comparing, so we detect *real* changes (content or template) and skip only
# when the receipt is truly unchanged.
_PDF_DATE_RE = re.compile(rb"/(CreationDate|ModDate)\s*\(D:[^)]*\)")


def _normalized_pdf(data: bytes) -> bytes:
    return _PDF_DATE_RE.sub(rb"/\1 (D:00000000000000)", data)


def _write_if_changed(out: Path, data: bytes, force: bool = False) -> bool:
    """Write `data` to `out` unless an identical PDF already exists there.

    Returns True if the file was written, False if skipped as unchanged.
    "Identical" ignores only Chromium's volatile date stamps, so any content or
    template change is picked up and overwrites the old file.
    """
    if not force and out.exists():
        try:
            if _normalized_pdf(out.read_bytes()) == _normalized_pdf(data):
                return False
        except OSError:
            pass
    out.write_bytes(data)
    return True


def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v or "")


def _receipt_html(r: dict) -> str:
    date = (r.get("transactionDateTime") or r.get("transactionDate") or "")[:19]
    warehouse = warehouse_name(r)
    doc = r.get("documentType") or r.get("transactionType") or ""
    barcode = r.get("transactionBarcode") or ""
    items = r.get("itemArray") or []
    otype = order_type(r)
    rows = []
    tax_codes: dict[str, str] = {}  # code -> label, for the legend below the table
    has_exempt = False
    for it in items:
        desc = item_description(it)
        ref = discount_ref(desc)
        tref = tax_ref(desc) if ref is None else None
        is_child = ref is not None or tref is not None
        is_fuel = otype == "fuel" and _item_is_fuel(it)
        if ref is not None:  # discount: resolve to the item it applies to
            _, pdesc = resolve_discount(ref, items)
            desc = f"↳ Discount → {pdesc or ref}"
            qty = str(it.get("unit") or "")
        elif tref is not None:  # additional per-item tax: keep label, resolve 'T/…'
            pref, label = tref
            _, pdesc = resolve_discount(pref, items)
            desc = f"↳ {label} → {pdesc or pref}"
            qty = str(it.get("unit") or "")
        elif is_fuel:  # grade @ price/gal, quantity = derived gallons
            desc = f"{desc} @ ${_num(it.get('itemUnitPriceAmount')):.3f}/gal"
            qty = f"{fuel_gallons(it):.3f}"
        else:
            qty = str(it.get("unit") or "")
        if is_tax_exempt(it):
            desc += " ᴱ"  # tax-exempt marker (the far-left 'E' on the receipt)
            has_exempt = True
        # Per-line tax-category code (blank for discount/tax child lines), matching
        # the Markdown archive and the web UI's Tax column.
        tax = "" if is_child else str(it.get("taxFlag") or "")
        if tax:
            tax_codes[tax] = tax_code_label(tax)
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(it.get('itemNumber') or ''))}</td>"
            f"<td>{html.escape(desc)}</td>"
            f"<td class='r'>{html.escape(qty)}</td>"
            f"<td class='r'>{_fmt_money(it.get('amount'))}</td>"
            f"<td class='c'>{html.escape(tax)}</td>"
            "</tr>"
        )
    body_rows = "\n".join(rows) or "<tr><td colspan=5>No line items</td></tr>"
    # Legend explaining the per-line tax codes / exempt marker (the UI shows these
    # as hover tooltips; a static PDF can't, so spell them out).
    legend_items = [f"<b>{html.escape(c)}</b> = {html.escape(lbl)}"
                    for c, lbl in sorted(tax_codes.items()) if lbl]
    if has_exempt:
        legend_items.append("<b>ᴱ</b> = tax-exempt item")
    tax_legend = (f"<div class='legend'>Tax codes: {' · '.join(legend_items)}</div>"
                  if legend_items else "")
    bc = barcode_svg(barcode) if barcode else None
    txid_block = (
        f"<div class='txid'>Transaction ID: {html.escape(barcode)}</div>"
        if barcode else ""
    )
    barcode_block = (
        f"<div class='barcode'>{bc}</div>" if bc
        else (f"<div class='barcode'>{html.escape(barcode)}</div>" if barcode else "")
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      body {{ font-family: -apple-system, Arial, sans-serif; margin: 24px; color:#111; }}
      h1 {{ font-size: 18px; margin:0 0 2px; }}
      .meta {{ color:#555; font-size:12px; margin-bottom:12px; }}
      table {{ width:100%; border-collapse:collapse; font-size:12px; }}
      th,td {{ border-bottom:1px solid #ddd; padding:4px 6px; text-align:left; }}
      td.r, th.r {{ text-align:right; }}
      td.c, th.c {{ text-align:center; }}
      tfoot td {{ font-weight:bold; border-top:2px solid #333; }}
      .barcode {{ font-family:monospace; font-size:10px; color:#888; margin-top:6px; }}
      .barcode svg {{ max-width:280px; height:auto; }}
      .txid {{ font-size:15px; font-weight:700; letter-spacing:.4px; margin-top:16px; }}
      .legend {{ color:#555; font-size:11px; margin-top:8px; }}
    </style></head><body>
      <h1>Costco Receipt — {html.escape(warehouse)}</h1>
      <div class='meta'>{html.escape(date)} &nbsp;•&nbsp; {html.escape(doc)}</div>
      {txid_block}
      {barcode_block}
      <table>
        <thead><tr><th>Item #</th><th>Description</th><th class='r'>Qty</th><th class='r'>Amount</th><th class='c'>Tax</th></tr></thead>
        <tbody>{body_rows}</tbody>
        <tfoot>
          <tr><td colspan=4 class='r'>Subtotal</td><td class='r'>{_fmt_money(r.get('subTotal'))}</td></tr>
          <tr><td colspan=4 class='r'>Tax</td><td class='r'>{_fmt_money(r.get('taxes'))}</td></tr>
          <tr><td colspan=4 class='r'>Total</td><td class='r'>{_fmt_money(r.get('total'))}</td></tr>
          <tr><td colspan=4 class='r'>Instant savings</td><td class='r'>{_fmt_money(r.get('instantSavings'))}</td></tr>
        </tfoot>
      </table>
      {tax_legend}
    </body></html>"""


def render_all_pdfs(
    raw_dir: Path = config.RAW_DIR, pdf_dir: Path = config.PDF_DIR, force: bool = False
) -> dict:
    """Render every raw receipt JSON to data/pdfs/<receipt>.pdf.

    Always re-renders from the current template and overwrites when the result
    differs from the existing file (content or template change). A file is left
    untouched only when the freshly-rendered PDF is identical to it. Pass
    force=True to rewrite every file regardless.
    """
    config.ensure_dirs()
    files = sorted(raw_dir.glob("*.json"))
    if not files:
        print("  No raw receipts found — run `fetch` first.")
        return {"rendered": 0, "pdf_dir": str(pdf_dir)}

    written, unchanged = 0, 0
    with sync_playwright() as p:
        # --no-sandbox lets headless Chromium run as root inside Docker.
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        for f in files:
            out = pdf_dir / f"{f.stem}.pdf"
            try:
                r = json.loads(f.read_text())
            except Exception:
                continue
            page.set_content(_receipt_html(r), wait_until="load")
            data = page.pdf(format="Letter",
                            margin={"top": "0.4in", "bottom": "0.4in",
                                    "left": "0.4in", "right": "0.4in"})
            if _write_if_changed(out, data, force=force):
                written += 1
            else:
                unchanged += 1
        browser.close()

    print(f"  Wrote {written} PDF(s), {unchanged} unchanged → {pdf_dir}")
    return {"rendered": written, "unchanged": unchanged,
            "total_pdfs": len(list(pdf_dir.glob('*.pdf'))), "pdf_dir": str(pdf_dir)}


def render_one_pdf(receipt_key: str, raw_dir: Path = config.RAW_DIR,
                   pdf_dir: Path = config.PDF_DIR) -> bool:
    """Render a single receipt's PDF from its raw JSON. Returns True on success.

    Overwrites the existing PDF only if the re-render differs from it.
    """
    config.ensure_dirs()
    src = raw_dir / f"{receipt_key}.json"
    if not src.exists():
        return False
    try:
        r = json.loads(src.read_text())
    except Exception:
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.set_content(_receipt_html(r), wait_until="load")
        data = page.pdf(format="Letter",
                        margin={"top": "0.4in", "bottom": "0.4in",
                                "left": "0.4in", "right": "0.4in"})
        browser.close()
    _write_if_changed(pdf_dir / f"{receipt_key}.pdf", data)
    return True
