"""End-to-end test of fetch->parse using a mocked API (no network/login)."""
import json
import shutil
from pathlib import Path

from costco_archiver import fetch as fetch_mod
from costco_archiver import parse as parse_mod
from costco_archiver.auth import Credentials


def _receipt(barcode, date, items):
    return {
        "transactionBarcode": barcode,
        "transactionDateTime": f"{date}T10:00:00",
        "transactionDate": date,
        "warehouseName": "TEST WAREHOUSE",
        "documentType": "warehouse",
        "transactionType": "Sales",
        "subTotal": sum(i["amount"] for i in items),
        "taxes": 0.0,
        "total": sum(i["amount"] for i in items),
        "totalItemCount": len(items),
        "instantSavings": 0.0,
        "itemArray": items,
    }


def _item(num, desc, unit, price, amount):
    return {
        "itemNumber": num, "itemDescription01": desc, "itemDescription02": "",
        "unit": unit, "itemUnitPriceAmount": price, "amount": amount,
        "itemDepartmentNumber": "14", "taxFlag": "N",
    }


class FakeAPI:
    """Returns receipts whose date falls in the requested window. The July
    window is returned for BOTH July and (overlapping) queries to prove
    barcode-level dedup prevents double counting."""
    def __init__(self, *a, **k):
        self.by_month = {
            "2026-07": [
                _receipt("BC-JUL-1", "2026-07-05",
                         [_item("1610256", "KS OLIVE OIL", 1, 15.99, 15.99),
                          _item("40606", "BANANAS", 2, 1.99, 3.98)]),
            ],
            "2026-06": [
                _receipt("BC-JUN-1", "2026-06-10",
                         [_item("1610256", "KS OLIVE OIL", 1, 15.99, 15.99)]),
            ],
        }

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def receipts(self, s, e, document_type="all"):
        out = []
        for month, recs in self.by_month.items():
            for r in recs:
                if s <= r["transactionDate"] <= e:
                    out.append(r)
        return out


def run(tmp: Path):
    raw = tmp / "raw"; cap = tmp / "cap"; out = tmp / "out"
    for d in (raw, cap, out):
        d.mkdir(parents=True, exist_ok=True)

    # Isolate from any captured request template on the machine, so we test the
    # default query path deterministically.
    from costco_archiver import config
    config.API_REQUEST_FILE = tmp / "no_request_template.json"

    # Patch the API and fetch twice to simulate overlapping/rerun dedup.
    fetch_mod.CostcoAPI = FakeAPI
    creds = Credentials(id_token="x", client_id="y")
    fetch_mod.fetch_all_receipts(creds, months_back=3, max_empty_windows=1, raw_dir=raw)
    fetch_mod.fetch_all_receipts(creds, months_back=3, max_empty_windows=1, raw_dir=raw)  # rerun

    summary = parse_mod.parse_all(raw_dir=raw, capture_dir=cap, output_dir=out)
    print(json.dumps(summary, indent=2))

    # Assertions
    assert summary["receipts_parsed"] == 2, summary
    assert summary["line_items"] == 3, summary          # 2 + 1, no dupes from rerun
    olive = None
    import csv
    with (out / "items_deduped.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    by_num = {r["item_number"]: r for r in rows}
    assert by_num["1610256"]["times_purchased"] == "2", by_num["1610256"]
    assert float(by_num["1610256"]["total_spent"]) == 31.98, by_num["1610256"]
    assert by_num["1610256"]["last_purchase"] == "2026-07-05"
    assert by_num["1610256"]["first_purchase"] == "2026-06-10"
    # line_items newest first
    with (out / "line_items.csv").open() as fh:
        li = list(csv.DictReader(fh))
    assert li[0]["date"] == "2026-07-05", li[0]
    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    import tempfile
    d = Path(tempfile.mkdtemp())
    try:
        run(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
