"""Render each downloaded receipt (raw JSON) into a PDF archive.

Costco's warehouse/gas receipts have no official per-receipt PDF — the site
renders them from JSON. So we build a clean, printable HTML receipt from each
raw JSON and convert it to PDF with the headless Chromium already installed for
Playwright. This runs fully locally: no login, no bot-detection.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import config
from .barcode_util import barcode_svg


def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v or "")


def _receipt_html(r: dict) -> str:
    date = (r.get("transactionDateTime") or r.get("transactionDate") or "")[:19]
    warehouse = r.get("warehouseName") or r.get("warehouseShortName") or ""
    doc = r.get("documentType") or r.get("transactionType") or ""
    barcode = r.get("transactionBarcode") or ""
    rows = []
    for it in r.get("itemArray") or []:
        desc = " ".join(
            x for x in (it.get("itemDescription01"), it.get("itemDescription02")) if x
        ).strip()
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(it.get('itemNumber') or ''))}</td>"
            f"<td>{html.escape(desc)}</td>"
            f"<td class='r'>{html.escape(str(it.get('unit') or ''))}</td>"
            f"<td class='r'>{_fmt_money(it.get('amount'))}</td>"
            "</tr>"
        )
    body_rows = "\n".join(rows) or "<tr><td colspan=4>No line items</td></tr>"
    bc = barcode_svg(barcode) if barcode else None
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
      tfoot td {{ font-weight:bold; border-top:2px solid #333; }}
      .barcode {{ font-family:monospace; font-size:10px; color:#888; margin-top:16px; }}
      .barcode svg {{ max-width:280px; height:auto; }}
    </style></head><body>
      <h1>Costco Receipt — {html.escape(warehouse)}</h1>
      <div class='meta'>{html.escape(date)} &nbsp;•&nbsp; {html.escape(doc)}</div>
      {barcode_block}
      <table>
        <thead><tr><th>Item #</th><th>Description</th><th class='r'>Qty</th><th class='r'>Amount</th></tr></thead>
        <tbody>{body_rows}</tbody>
        <tfoot>
          <tr><td colspan=3 class='r'>Subtotal</td><td class='r'>{_fmt_money(r.get('subTotal'))}</td></tr>
          <tr><td colspan=3 class='r'>Tax</td><td class='r'>{_fmt_money(r.get('taxes'))}</td></tr>
          <tr><td colspan=3 class='r'>Total</td><td class='r'>{_fmt_money(r.get('total'))}</td></tr>
          <tr><td colspan=3 class='r'>Instant savings</td><td class='r'>{_fmt_money(r.get('instantSavings'))}</td></tr>
        </tfoot>
      </table>
      <div class='barcode'>Transaction: {html.escape(barcode)}</div>
    </body></html>"""


def render_all_pdfs(
    raw_dir: Path = config.RAW_DIR, pdf_dir: Path = config.PDF_DIR, force: bool = False
) -> dict:
    """Render every raw receipt JSON to data/pdfs/<receipt>.pdf. Idempotent."""
    config.ensure_dirs()
    files = sorted(raw_dir.glob("*.json"))
    if not files:
        print("  No raw receipts found — run `fetch` first.")
        return {"rendered": 0, "pdf_dir": str(pdf_dir)}

    rendered, skipped = 0, 0
    with sync_playwright() as p:
        # --no-sandbox lets headless Chromium run as root inside Docker.
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        for f in files:
            out = pdf_dir / f"{f.stem}.pdf"
            if out.exists() and not force:
                skipped += 1
                continue
            try:
                r = json.loads(f.read_text())
            except Exception:
                continue
            page.set_content(_receipt_html(r), wait_until="load")
            page.pdf(path=str(out), format="Letter",
                     margin={"top": "0.4in", "bottom": "0.4in",
                             "left": "0.4in", "right": "0.4in"})
            rendered += 1
        browser.close()

    print(f"  Rendered {rendered} PDF(s), skipped {skipped} existing → {pdf_dir}")
    return {"rendered": rendered, "skipped": skipped,
            "total_pdfs": len(list(pdf_dir.glob('*.pdf'))), "pdf_dir": str(pdf_dir)}
