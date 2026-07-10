"""Walk backward through time, downloading every receipt, saving raw JSON.

Strategy: query in monthly windows starting from the most recent and moving
backward. Each receipt is saved once, keyed by its transaction barcode, so
re-running is idempotent and overlapping windows never create duplicates.
We stop early after several consecutive empty months (history exhausted).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Optional

from dateutil.relativedelta import relativedelta

from . import config
from .api import CostcoAPI, CostcoAPIError, find_receipts, override_date_vars
from .auth import Credentials


def _load_request_template() -> dict | None:
    """The exact receipts request captured by `import-curl`, if any."""
    f = config.API_REQUEST_FILE
    if not f.exists():
        return None
    try:
        tpl = json.loads(f.read_text())
    except Exception:
        return None
    return tpl if isinstance(tpl.get("body"), dict) else None


def _safe_key(receipt: dict) -> str:
    """A stable, filesystem-safe unique id for a receipt."""
    key = (
        receipt.get("transactionBarcode")
        or "-".join(
            str(receipt.get(k, ""))
            for k in ("transactionDate", "warehouseNumber", "transactionType", "total")
        )
    )
    return re.sub(r"[^A-Za-z0-9._-]", "_", key) or "receipt"


def fetch_all_receipts(
    creds: Credentials,
    months_back: int = 36,
    max_empty_windows: int = 6,
    document_type: str = "all",
    raw_dir: Path = config.RAW_DIR,
    progress_cb=None,
) -> dict:
    """Download all receipts, newest first. Returns a run summary.

    progress_cb(done, total, saved, label) is called after each window so a UI
    can show live progress.
    """
    config.ensure_dirs()
    today = dt.date.today()
    window_end = today
    saved, seen, empty_streak, windows = 0, set(), 0, 0

    # Preload already-downloaded barcodes so reruns skip existing files.
    for f in raw_dir.glob("*.json"):
        seen.add(f.stem)

    template = _load_request_template()
    if template:
        print("  Using captured request template (Costco's own query).")

    with CostcoAPI(creds) as api:
        for _ in range(months_back):
            window_start = window_end - relativedelta(months=1) + dt.timedelta(days=1)
            s, e = window_start.isoformat(), window_end.isoformat()
            windows += 1
            try:
                if template:
                    body = dict(template["body"])
                    body["variables"] = override_date_vars(
                        body.get("variables", {}), s, e)
                    resp = api.post(body, url=template.get("url"))
                    receipts = find_receipts(resp)
                else:
                    receipts = api.receipts(s, e, document_type=document_type)
            except CostcoAPIError as ex:
                print(f"  ! GraphQL error for {s}..{e}: {ex.errors}")
                receipts = []
            except Exception as ex:  # network / auth hiccup — log and continue
                print(f"  ! request failed for {s}..{e}: {ex}")
                receipts = []

            new_here = 0
            for r in receipts:
                key = _safe_key(r)
                if key in seen:
                    continue
                seen.add(key)
                (raw_dir / f"{key}.json").write_text(json.dumps(r, indent=2))
                saved += 1
                new_here += 1

            date_label = f"{window_start:%Y-%m}"
            print(f"  {date_label}: {len(receipts)} receipts ({new_here} new)")
            if progress_cb:
                try:
                    progress_cb(windows, months_back, saved, date_label)
                except Exception:
                    pass

            empty_streak = empty_streak + 1 if not receipts else 0
            if empty_streak >= max_empty_windows:
                print(
                    f"  Stopping: {empty_streak} consecutive empty months "
                    "(history looks exhausted)."
                )
                break

            window_end = window_start - dt.timedelta(days=1)

    summary = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "windows_queried": windows,
        "receipts_saved_this_run": saved,
        "total_receipts_on_disk": len(list(raw_dir.glob("*.json"))),
        "raw_dir": str(raw_dir),
    }
    (config.DATA_DIR / "fetch_summary.json").write_text(json.dumps(summary, indent=2))
    return summary
