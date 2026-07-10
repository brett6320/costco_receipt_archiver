"""Dedup semantics: keep genuine repeat purchases, drop whole-receipt re-ingests.

'Duplicate' means the exact same receipt ingested more than once — NOT multiple
purchases of the same SKU (same-receipt repeat lines, or the SKU on other dates).
"""
import csv
import json
import shutil
import tempfile
from pathlib import Path

from costco_archiver import parse as parse_mod


def _item(num, desc, amt):
    return {"itemNumber": num, "itemDescription01": desc, "itemDescription02": "",
            "unit": 1, "itemUnitPriceAmount": amt, "amount": amt, "taxFlag": "N"}


def _receipt(barcode, date, items):
    return {"transactionBarcode": barcode, "transactionDateTime": f"{date}T10:00:00",
            "transactionDate": date, "warehouseName": "WH", "documentType": "warehouse",
            "transactionType": "Sales", "total": sum(i["amount"] for i in items),
            "totalItemCount": len(items), "itemArray": items}


def run(tmp: Path):
    raw = tmp / "raw"; out = tmp / "out"
    for d in (raw, out):
        d.mkdir(parents=True, exist_ok=True)

    # Receipt with the SAME sku scanned twice (two real lines) + another item.
    r1 = _receipt("BC1", "2026-07-06", [
        _item("931484", "KS WATER GAL", 5.49),
        _item("931484", "KS WATER GAL", 5.49),   # genuine 2nd purchase, same receipt
        _item("553499", "DRIED PLUMS", 9.99),
    ])
    # Same SKU bought again on a DIFFERENT receipt/date.
    r2 = _receipt("BC2", "2026-06-01", [_item("931484", "KS WATER GAL", 5.49)])

    (raw / "BC1.json").write_text(json.dumps(r1))
    (raw / "BC2.json").write_text(json.dumps(r2))
    # The SAME receipt ingested a second time under a different filename
    # (e.g. imported via PDF after being fetched via API) -> must NOT double count.
    (raw / "BC1_again.json").write_text(json.dumps(r1))

    summary = parse_mod.parse_all(raw_dir=raw, output_dir=out)

    with (out / "line_items.csv").open() as fh:
        lines = list(csv.DictReader(fh))
    # r1 has 3 lines, r2 has 1 -> 4 total. The duplicate BC1 file adds nothing.
    assert len(lines) == 4, (len(lines), summary)

    water = [l for l in lines if l["item_number"] == "931484"]
    # 2 (same receipt) + 1 (other receipt) = 3 genuine water purchases kept.
    assert len(water) == 3, len(water)

    with (out / "items_deduped.csv").open() as fh:
        agg = {r["item_number"]: r for r in csv.DictReader(fh)}
    assert agg["931484"]["times_purchased"] == "3", agg["931484"]
    assert float(agg["931484"]["total_spent"]) == round(5.49 * 3, 2), agg["931484"]
    assert agg["553499"]["times_purchased"] == "1"
    print("dedup semantics OK: repeats kept (3 water), re-ingest not double counted")
    print("\nALL DEDUP TESTS PASSED")


if __name__ == "__main__":
    d = Path(tempfile.mkdtemp())
    try:
        run(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
