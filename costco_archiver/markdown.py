"""Post-process receipts into a browsable Markdown archive.

Produces, under data/output/markdown/:
  index.md            - all purchases (receipts) in descending date order,
                        each linking to its own page, plus totals.
  receipts/<id>.md    - one page per receipt: header, line items (each with a
                        Costco search/detail link), and totals.

Detail links: warehouse item numbers don't map 1:1 to online product IDs, so we
link each line item to a Costco catalog search for that item number (and the
description) — the most reliable pointer without hitting Costco (which is bot-
protected). Enrichment stays link-based and offline by design.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus

from . import config
from .barcode_util import barcode_svg
from .parse import _load_receipts, _receipt_date, _num, order_type, _item_is_fuel

_TYPE_ICON = {"fuel": "⛽ Fuel", "online": "🌐 Online", "warehouse": "🏬 Warehouse"}

_SEARCH = "https://www.costco.com/CatalogSearch?dept=All&keyword={}"


def _money(v) -> str:
    return f"${_num(v):,.2f}"


def _safe(receipt: dict) -> str:
    key = receipt.get("transactionBarcode") or "-".join(
        str(receipt.get(k, "")) for k in ("transactionDate", "total"))
    return re.sub(r"[^A-Za-z0-9._-]", "_", key) or "receipt"


def _mask_member(m: str) -> str:
    m = str(m or "")
    return ("•" * max(0, len(m) - 4) + m[-4:]) if m else ""


def _item_link(item_number: str, description: str) -> str:
    """A markdown link that points at a Costco search for the item."""
    if item_number:
        return f"[{item_number}]({_SEARCH.format(quote_plus(item_number))})"
    if description:
        return f"[search]({_SEARCH.format(quote_plus(description))})"
    return ""


def _receipt_page(r: dict, barcode_href: str | None = None) -> str:
    date = _receipt_date(r)
    warehouse = r.get("warehouseName") or r.get("warehouseShortName") or ""
    barcode = r.get("transactionBarcode") or ""
    lines = [
        f"# Receipt — {warehouse}",
        "",
        f"[← Back to index](../index.md)",
        "",
    ]
    if barcode_href:
        lines += [f'<img src="{barcode_href}" alt="{barcode}" height="56">', ""]
    otype = order_type(r)
    lines += [
        f"- **Type:** {_TYPE_ICON.get(otype, otype)}",
        f"- **Date:** {r.get('transactionDateTime') or date}",
        f"- **Warehouse:** {warehouse}"
        + (f" (#{r.get('warehouseNumber')})" if r.get("warehouseNumber") else ""),
    ]
    if r.get("member"):
        lines.append(f"- **Member:** {_mask_member(r.get('member'))}")
    if barcode:
        lines.append(f"- **Transaction:** `{barcode}`")
    lines += [
        f"- **Items:** {r.get('totalItemCount') or len(r.get('itemArray') or [])}",
        "",
        "| Item # | Description | Qty | Amount | Tax | Detail |",
        "|---|---|--:|--:|:--:|---|",
    ]
    for it in r.get("itemArray") or []:
        num = str(it.get("itemNumber") or "").strip()
        desc = " ".join(
            x for x in (it.get("itemDescription01"), it.get("itemDescription02")) if x
        ).strip()
        qty = it.get("unit") or 1
        amount = _money(it.get("amount"))
        tax = it.get("taxFlag") or ""
        # Exclude product-lookup metadata for gas/fuel line items.
        detail = "" if (otype == "fuel" and _item_is_fuel(it)) else _item_link(num, desc)
        lines.append(f"| {num} | {desc} | {qty} | {amount} | {tax} | {detail} |")
    lines += [
        "",
        f"| | | | |",
        f"|---|---|---|--:|",
        f"| | | **Subtotal** | {_money(r.get('subTotal'))} |",
        f"| | | **Tax** | {_money(r.get('taxes'))} |",
        f"| | | **Total** | {_money(r.get('total'))} |",
    ]
    if _num(r.get("instantSavings")):
        lines.append(f"| | | **Instant savings** | {_money(r.get('instantSavings'))} |")
    # Link to the rendered PDF if it exists.
    if barcode and (config.PDF_DIR / f"{_safe(r)}.pdf").exists():
        lines += ["", f"[📄 PDF](../../pdfs/{_safe(r)}.pdf)"]
    lines.append("")
    return "\n".join(lines)


def generate_markdown(
    raw_dir: Path = config.RAW_DIR, output_dir: Path = config.OUTPUT_DIR
) -> dict:
    config.ensure_dirs()
    md_dir = output_dir / "markdown"
    pages_dir = md_dir / "receipts"
    bc_dir = md_dir / "barcodes"
    pages_dir.mkdir(parents=True, exist_ok=True)
    bc_dir.mkdir(parents=True, exist_ok=True)

    receipts = _load_receipts(raw_dir)
    # Descending by date (newest purchases first), then by total.
    receipts.sort(key=lambda r: (_receipt_date(r), _num(r.get("total"))), reverse=True)
    if not receipts:
        print("  No receipts found — run `fetch` or `import` first.")
        return {"receipts": 0, "dir": str(md_dir)}

    total_spent = round(sum(_num(r.get("total")) for r in receipts), 2)
    total_items = sum(int(r.get("totalItemCount") or len(r.get("itemArray") or []))
                      for r in receipts)
    dates = [_receipt_date(r) for r in receipts if _receipt_date(r)]

    # --- index.md ---
    idx = [
        "# Costco Purchases",
        "",
        f"**{len(receipts)}** receipts · **{total_items}** items · "
        f"**{_money(total_spent)}** total"
        + (f" · {dates[-1]} → {dates[0]}" if dates else ""),
        "",
        "All purchases, most recent first. Click a date to open the receipt.",
        "",
        "| Date | Warehouse | Items | Total | Receipt |",
        "|---|---|--:|--:|---|",
    ]
    for r in receipts:
        name = _safe(r)
        date = _receipt_date(r)
        warehouse = r.get("warehouseName") or r.get("warehouseShortName") or ""
        n_items = r.get("totalItemCount") or len(r.get("itemArray") or [])
        idx.append(
            f"| [{date}](receipts/{name}.md) | {warehouse} | {n_items} "
            f"| {_money(r.get('total'))} | `{r.get('transactionBarcode') or ''}` |")
    idx.append("")
    (md_dir / "index.md").write_text("\n".join(idx))

    # --- per-receipt pages (+ barcode SVG of the transaction number) ---
    for r in receipts:
        _write_receipt_page(r, pages_dir, bc_dir)

    print(f"  Wrote index.md + {len(receipts)} receipt pages → {md_dir}")
    return {"receipts": len(receipts), "index": str(md_dir / "index.md"),
            "pages_dir": str(pages_dir)}


def _write_receipt_page(r: dict, pages_dir: Path, bc_dir: Path) -> str:
    name = _safe(r)
    href = None
    bc = barcode_svg(r.get("transactionBarcode") or "")
    if bc:
        (bc_dir / f"{name}.svg").write_text(bc)
        href = f"../barcodes/{name}.svg"
    (pages_dir / f"{name}.md").write_text(_receipt_page(r, barcode_href=href))
    return name


def generate_one(receipt_key: str, raw_dir: Path = config.RAW_DIR,
                 output_dir: Path = config.OUTPUT_DIR) -> bool:
    """Regenerate a single receipt's Markdown page + barcode from its raw JSON."""
    import json
    src = raw_dir / f"{receipt_key}.json"
    if not src.exists():
        return False
    try:
        r = json.loads(src.read_text())
    except Exception:
        return False
    md_dir = output_dir / "markdown"
    pages_dir = md_dir / "receipts"
    bc_dir = md_dir / "barcodes"
    pages_dir.mkdir(parents=True, exist_ok=True)
    bc_dir.mkdir(parents=True, exist_ok=True)
    _write_receipt_page(r, pages_dir, bc_dir)
    return True
