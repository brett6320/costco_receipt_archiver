"""Parse raw receipts (+ captured online orders) into deduplicated CSVs.

Outputs (in data/output/):
  line_items.csv      - every purchased line item, one row each, newest first.
  items_deduped.csv   - one row per item number, aggregated across all purchases.
  receipts.csv        - one row per receipt (header-level totals).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

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
    """Classify a receipt as 'online', 'fuel', or 'warehouse'."""
    dt = str(receipt.get("documentType") or "").lower()
    tt = str(receipt.get("transactionType") or "").lower()
    wh = str(receipt.get("warehouseName") or receipt.get("warehouseShortName") or "").lower()
    src = str(receipt.get("source") or "").lower()
    if "online" in src or wh == "online" or "online" in dt:
        return "online"
    if ("gas" in dt or "fuel" in dt or "gas" in tt or "gas" in wh or "fuel" in wh
            or any(_item_is_fuel(it) for it in receipt.get("itemArray") or [])):
        return "fuel"
    return "warehouse"


def _iter_line_items(receipt: dict, source: str) -> Iterable[dict]:
    date = _receipt_date(receipt)
    warehouse = receipt.get("warehouseName") or receipt.get("warehouseShortName") or ""
    receipt_id = receipt.get("transactionBarcode") or ""
    doc_type = receipt.get("documentType") or receipt.get("transactionType") or ""
    otype = order_type(receipt)
    for it in receipt.get("itemArray") or []:
        desc = " ".join(
            x for x in (it.get("itemDescription01"), it.get("itemDescription02")) if x
        ).strip()
        # Per-line fuel flag so gas lines are excluded from product metadata.
        is_fuel = otype == "fuel" and _item_is_fuel(it)
        yield {
            "date": date,
            "item_number": (it.get("itemNumber") or "").strip(),
            "description": desc,
            "unit_qty": _num(it.get("unit")),
            "unit_price": _num(it.get("itemUnitPriceAmount")),
            "amount": _num(it.get("amount")),
            "department": it.get("itemDepartmentNumber") or "",
            "tax_flag": it.get("taxFlag") or "",
            "warehouse": warehouse,
            "receipt_id": receipt_id,
            "doc_type": doc_type,
            "order_type": "fuel" if is_fuel else otype,
            "source": source,
        }


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


def _harvest_line_items(capture_dir: Path) -> list[dict]:
    """Best-effort extraction of online-order line items from captured JSON."""
    items: list[dict] = []
    for f in sorted(capture_dir.glob("*.json")):
        try:
            blob = json.loads(f.read_text())
        except Exception:
            continue
        _walk_for_items(blob.get("body", blob), items)
    return items


def _walk_for_items(node: Any, out: list[dict], date_hint: str = "") -> None:
    """Recursively find dict nodes that look like an order line item."""
    if isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        looks_like_item = (
            {"itemnumber", "itemdescription"} & keys
            or {"sku", "quantity"} <= keys
            or {"itemid", "unitprice"} <= keys
        )
        this_date = (
            node.get("orderDate")
            or node.get("orderPlacedDate")
            or node.get("transactionDate")
            or date_hint
        )
        if looks_like_item:
            out.append(
                {
                    "date": str(this_date)[:10],
                    "item_number": str(
                        node.get("itemNumber") or node.get("sku") or node.get("itemId") or ""
                    ).strip(),
                    "description": str(
                        node.get("itemDescription")
                        or node.get("description")
                        or node.get("name")
                        or ""
                    ).strip(),
                    "unit_qty": _num(node.get("quantity") or node.get("unit")),
                    "unit_price": _num(node.get("unitPrice") or node.get("price")),
                    "amount": _num(
                        node.get("amount") or node.get("lineTotal") or node.get("total")
                    ),
                    "department": "",
                    "tax_flag": "",
                    "warehouse": "ONLINE",
                    "receipt_id": str(
                        node.get("orderNumber") or node.get("orderId") or ""
                    ),
                    "doc_type": "online",
                    "order_type": "online",
                    "source": "online",
                }
            )
        for v in node.values():
            _walk_for_items(v, out, str(this_date)[:10])
    elif isinstance(node, list):
        for v in node:
            _walk_for_items(v, out, date_hint)


FIELDS = [
    "date", "item_number", "description", "unit_qty", "unit_price",
    "amount", "department", "tax_flag", "warehouse", "receipt_id",
    "doc_type", "order_type", "source",
]


def _dedup_online_items(items: list[dict]) -> list[dict]:
    """Deduplicate ONLY online-order items.

    The online path recursively walks arbitrary JSON, which can visit the same
    line-item node via multiple paths and emit it more than once. We collapse
    exact repeats there. We do NOT touch warehouse items: each entry in a
    receipt's itemArray is a real scanned line, so two lines of the same SKU on
    one receipt are two genuine purchases and are both kept.
    """
    seen: set[tuple] = set()
    out = []
    for it in items:
        k = tuple(it[f] for f in FIELDS)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def parse_all(
    raw_dir: Path = config.RAW_DIR,
    capture_dir: Path = config.CAPTURE_DIR,
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
    # Online items: self-dedup only (JSON walk can revisit the same node).
    line_items.extend(_dedup_online_items(_harvest_line_items(capture_dir)))

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
                "warehouse": r.get("warehouseName") or r.get("warehouseShortName") or "",
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
