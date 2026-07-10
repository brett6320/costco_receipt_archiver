"""Parse raw receipts into deduplicated CSVs.

Outputs (in data/output/):
  line_items.csv      - every purchased line item, one row each, newest first.
  items_deduped.csv   - one row per item number, aggregated across all purchases.
  receipts.csv        - one row per receipt (header-level totals).
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterable

from . import config


def _num(v) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _receipt_date(r: dict) -> str:
    raw = r.get("transactionDateTime") or r.get("transactionDate") or ""
    return str(raw)[:10]  # YYYY-MM-DD


def _item_is_fuel(it: dict) -> bool:
    """A line item that represents fuel (gas pump), not a warehouse product."""
    if any(it.get(k) for k in ("fuelGradeCode", "fuelUomCode", "fuelGradeDescription",
                               "fuelUnitQuantity")):
        return True
    desc = " ".join(str(it.get(k) or "") for k in
                     ("itemDescription01", "itemDescription02")).upper()
    return any(w in desc for w in ("REGULAR GAS", "PREMIUM GAS", "UNLEAD",
                                   "DIESEL", "FUEL", "GASOLINE"))


def order_type(receipt: dict) -> str:
    """Classify a receipt as 'fuel' or 'warehouse'."""
    dt = str(receipt.get("documentType") or "").lower()
    tt = str(receipt.get("transactionType") or "").lower()
    wh = str(receipt.get("warehouseName") or receipt.get("warehouseShortName") or "").lower()
    if ("gas" in dt or "fuel" in dt or "gas" in tt or "gas" in wh or "fuel" in wh
            or any(_item_is_fuel(it) for it in receipt.get("itemArray") or [])):
        return "fuel"
    return "warehouse"


def item_description(it: dict) -> str:
    """Joined line-item description (both description fields)."""
    return " ".join(
        x for x in (it.get("itemDescription01"), it.get("itemDescription02")) if x
    ).strip()


def warehouse_name(receipt: dict) -> str:
    """Store name in a normalized alpha form: strip the trailing '#1262'
    store-number suffix (and any stray '#') and Title-Case it, so the variants
    "W TAMPA #1262", "W TAMPA" and "W Tampa" all collapse to "W Tampa". The number
    itself is kept separately as warehouse_number."""
    raw = receipt.get("warehouseName") or receipt.get("warehouseShortName") or ""
    name = re.sub(r"#\s*\d+", "", raw).replace("#", "")
    return re.sub(r"\s{2,}", " ", name).strip().title()


def fuel_gallons(it: dict) -> float:
    """Gallons pumped for a fuel line. Costco's API doesn't return the quantity
    (the raw `unit` is always 1), so derive it from total ÷ price-per-gallon."""
    price = _num(it.get("itemUnitPriceAmount"))
    amt = _num(it.get("amount"))
    return round(amt / price, 3) if price else _num(it.get("unit"))


def is_tax_exempt(it: dict) -> bool:
    """Costco marks tax-exempt line items with an 'E' identifier — the 'E' printed
    at the far left of the receipt line."""
    return str(it.get("itemIdentifier") or "").upper() == "E"


# Costco's per-line taxFlag is a tax-category code, not a plain Y/N. These labels
# mirror the tooltips shown in the web UI (web.py TAX_TIP) so the Markdown/PDF
# receipts carry the same explanation the UI does on hover.
TAX_CODE_LABELS = {
    "Y": "Taxable — standard rate",
    "N": "Not taxed",
    "3": "Special tax category (Costco code 3)",
    "4": "Special tax category (Costco code 4 — e.g. liquor)",
}


def tax_code_label(code) -> str:
    """Human-readable meaning of a per-line taxFlag code ('' if no code)."""
    code = str(code or "").strip()
    if not code:
        return ""
    return TAX_CODE_LABELS.get(code, f"Costco tax code {code}")


def discount_ref(desc: str):
    """A discount line encodes the discounted item after a leading '/', e.g.
    '/1967470' or '/ DECK BOX'. Return that reference token, or None if the line
    isn't a discount."""
    d = (desc or "").strip()
    return d[1:].strip() if d.startswith("/") else None


_TAX_RE = re.compile(r"\bT/\s*(\d+)")


def tax_ref(desc: str):
    """An additional per-item tax line references its item as 'T/<item #>' inside
    the description (e.g. 'LIQUOR LITER TAX T/1472221') — the inverse of a discount
    (positive amount). Return (item_ref, label_without_ref), or None."""
    m = _TAX_RE.search(desc or "")
    if not m:
        return None
    return m.group(1), _TAX_RE.sub("", desc).strip()


def resolve_discount(ref: str, items: list[dict]) -> tuple[str, str]:
    """Resolve a discount reference to the (item_number, description) it applies to
    within the same receipt. Numeric refs match by item number; text refs match by
    a unique case-insensitive description substring. Returns ('', '') if unresolved."""
    prods = [(str(it.get("itemNumber") or "").strip(), item_description(it))
             for it in items if not item_description(it).startswith("/")]
    key = (ref or "").replace(" ", "")
    if key.isdigit():
        for num, d in prods:
            if num == key:
                return num, d
        return "", ""
    rl = (ref or "").strip().lower()
    if rl:
        hits = [(num, d) for num, d in prods if d and rl in d.lower()]
        if len(hits) == 1:
            return hits[0]
    return "", ""


def _iter_line_items(receipt: dict, source: str) -> Iterable[dict]:
    date = _receipt_date(receipt)
    warehouse = warehouse_name(receipt)
    receipt_id = receipt.get("transactionBarcode") or ""
    doc_type = receipt.get("documentType") or receipt.get("transactionType") or ""
    otype = order_type(receipt)
    items = receipt.get("itemArray") or []
    for it in items:
        desc = item_description(it)
        # Per-line fuel flag so gas lines report gallons (not the bogus unit=1)
        # and are excluded from product metadata.
        is_fuel = otype == "fuel" and _item_is_fuel(it)
        base = {
            "date": date,
            "item_number": (it.get("itemNumber") or "").strip(),
            "unit_qty": fuel_gallons(it) if is_fuel else _num(it.get("unit")),
            "unit_price": _num(it.get("itemUnitPriceAmount")),
            "amount": _num(it.get("amount")),
            "department": it.get("itemDepartmentNumber") or "",
            "tax_flag": it.get("taxFlag") or "",
            "warehouse": warehouse,
            "warehouse_number": str(receipt.get("warehouseNumber") or "").strip(),
            "receipt_id": receipt_id,
            "doc_type": doc_type,
            "source": source,
        }
        ref = discount_ref(desc)
        if ref is not None:
            # A discount: label it with the item it applies to and record that
            # item's number so the UI can nest it directly under that item.
            pnum, pdesc = resolve_discount(ref, items)
            yield {**base,
                   "description": f"Discount → {pdesc}" if pdesc else f"Discount → {ref}",
                   "tax_exempt": "",
                   "order_type": "discount",
                   "discount_ref": pnum}
            continue
        tref = tax_ref(desc)
        if tref is not None:
            # An additional per-item tax (the inverse of a discount): keep its
            # label, resolve the taxed item, and nest under it.
            pref, label = tref
            pnum, pdesc = resolve_discount(pref, items)
            yield {**base,
                   "description": f"{label} → {pdesc}" if pdesc else f"{label} → {pref}",
                   "tax_exempt": "",
                   "order_type": "tax",
                   "discount_ref": pnum}
            continue
        yield {**base,
               "description": desc,
               "tax_exempt": "Y" if is_tax_exempt(it) else "",
               "order_type": "fuel" if is_fuel else otype,
               "discount_ref": ""}


def _receipt_key(r: dict) -> str:
    """Identity of a receipt, so the SAME receipt ingested twice (e.g. via API
    and via PDF/HTML import) is counted once — without merging distinct items."""
    bc = (r.get("transactionBarcode") or "").strip()
    if bc:
        return bc
    # No barcode: fall back to a composite so genuinely different receipts stay
    # distinct while an identical re-ingest collapses.
    return "|".join(str(r.get(k, "")) for k in (
        "transactionDateTime", "warehouseNumber", "transactionType",
        "total", "totalItemCount"))


def _load_receipts(raw_dir: Path) -> list[dict]:
    """Load raw receipts, deduplicated by receipt identity (barcode)."""
    by_key: dict[str, dict] = {}
    for f in sorted(raw_dir.glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        by_key.setdefault(_receipt_key(r), r)  # first wins; dupes ignored
    return list(by_key.values())


FIELDS = [
    "date", "item_number", "description", "unit_qty", "unit_price",
    "amount", "department", "tax_flag", "tax_exempt", "warehouse",
    "warehouse_number", "receipt_id", "doc_type", "order_type",
    "discount_ref", "source",
]


def parse_all(
    raw_dir: Path = config.RAW_DIR,
    output_dir: Path = config.OUTPUT_DIR,
) -> dict:
    config.ensure_dirs()
    receipts = _load_receipts(raw_dir)

    # Warehouse items: every itemArray entry is a real scanned line — keep all.
    # Receipt-level dedup (above) already prevents the same receipt counting
    # twice, so identical SKU lines here are genuine repeat purchases.
    line_items: list[dict] = []
    for r in receipts:
        line_items.extend(_iter_line_items(r, source="warehouse"))

    # Sort newest first, then by receipt and item.
    line_items.sort(key=lambda x: (x["date"], x["receipt_id"], x["item_number"]), reverse=True)

    # --- line_items.csv ---
    li_path = output_dir / "line_items.csv"
    with li_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(line_items)

    # --- items_deduped.csv : aggregate by item number ---
    agg: dict[str, dict] = {}
    for it in line_items:
        num = it["item_number"] or f"NONUM::{it['description']}"
        a = agg.setdefault(
            num,
            {
                "item_number": it["item_number"],
                "description": it["description"],
                "times_purchased": 0,
                "total_qty": 0.0,
                "total_spent": 0.0,
                "first_purchase": it["date"],
                "last_purchase": it["date"],
                "last_price": it["unit_price"] or it["amount"],
            },
        )
        a["times_purchased"] += 1
        a["total_qty"] += it["unit_qty"] or 1
        a["total_spent"] = round(a["total_spent"] + it["amount"], 2)
        if it["date"] and it["date"] < a["first_purchase"]:
            a["first_purchase"] = it["date"]
        if it["date"] and it["date"] >= a["last_purchase"]:
            a["last_purchase"] = it["date"]
            a["last_price"] = it["unit_price"] or it["amount"]
        if it["description"] and not a["description"]:
            a["description"] = it["description"]

    agg_rows = sorted(agg.values(), key=lambda x: x["last_purchase"], reverse=True)
    agg_path = output_dir / "items_deduped.csv"
    with agg_path.open("w", newline="") as fh:
        cols = [
            "item_number", "description", "times_purchased", "total_qty",
            "total_spent", "last_price", "first_purchase", "last_purchase",
        ]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(agg_rows)

    # --- receipts.csv : header-level ---
    rec_rows = []
    for r in receipts:
        rec_rows.append(
            {
                "date": _receipt_date(r),
                "warehouse": warehouse_name(r),
                "doc_type": r.get("documentType") or r.get("transactionType") or "",
                "items": r.get("totalItemCount") or len(r.get("itemArray") or []),
                "subtotal": _num(r.get("subTotal")),
                "taxes": _num(r.get("taxes")),
                "total": _num(r.get("total")),
                "instant_savings": _num(r.get("instantSavings")),
                "receipt_id": r.get("transactionBarcode") or "",
            }
        )
    rec_rows.sort(key=lambda x: x["date"], reverse=True)
    rec_path = output_dir / "receipts.csv"
    with rec_path.open("w", newline="") as fh:
        cols = [
            "date", "warehouse", "doc_type", "items", "subtotal",
            "taxes", "total", "instant_savings", "receipt_id",
        ]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rec_rows)

    summary = {
        "receipts_parsed": len(receipts),
        "line_items": len(line_items),
        "unique_items": len(agg_rows),
        "total_spent": round(sum(x["total_spent"] for x in agg_rows), 2),
        "outputs": {
            "line_items": str(li_path),
            "items_deduped": str(agg_path),
            "receipts": str(rec_path),
        },
    }
    (output_dir / "parse_summary.json").write_text(json.dumps(summary, indent=2))
    return summary
