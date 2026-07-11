"""Backup/restore semantics: compressed snapshots, and de-duplicating restore.

A restore must re-add only receipts that aren't already on disk (keyed by
identity), so restoring a backup — even twice, or one that overlaps the current
data — never creates duplicates.
"""
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from costco_archiver import backup as bk


def _receipt(barcode, total, item="1"):
    return {"transactionBarcode": barcode, "total": total, "totalItemCount": 1,
            "itemArray": [{"itemNumber": item, "itemDescription01": "X",
                           "unit": 1, "amount": total, "taxFlag": "N"}]}


def _write(raw, barcode, total):
    (raw / f"{barcode}.json").write_text(json.dumps(_receipt(barcode, total)))


def run(tmp: Path):
    raw = tmp / "raw"; bdir = tmp / "backups"; raw.mkdir()
    _write(raw, "AAA", 10)
    _write(raw, "BBB", 20)
    _write(raw, "CCC", 30)

    # --- create: archive is real gzip'd tar with a manifest + raw/ payloads ---
    meta = bk.create_backup(raw_dir=raw, backup_dir=bdir, label="unit")
    assert meta["receipt_count"] == 3, meta
    arc = bdir / meta["name"]
    assert arc.read_bytes()[:2] == b"\x1f\x8b", "not gzip"
    with tarfile.open(arc, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert sum(n.startswith("raw/") for n in names) == 3, names
        man = json.loads(tar.extractfile("manifest.json").read())
        assert man["receipt_count"] == 3 and man["label"] == "unit"

    # --- list: newest-first, count from manifest ---
    listed = bk.list_backups(bdir)
    assert len(listed) == 1 and listed[0]["receipt_count"] == 3, listed

    # --- restore into a partial raw dir: only the missing receipt is added ---
    (raw / "BBB.json").unlink()                 # simulate loss of one receipt
    assert sorted(p.stem for p in raw.glob("*.json")) == ["AAA", "CCC"]
    r1 = bk.restore_backup(meta["name"], raw_dir=raw, backup_dir=bdir)
    assert (r1["restored"], r1["skipped"], r1["invalid"]) == (1, 2, 0), r1
    assert sorted(p.stem for p in raw.glob("*.json")) == ["AAA", "BBB", "CCC"]

    # --- restore AGAIN: everything present -> pure no-op, no duplicates ---
    r2 = bk.restore_backup(meta["name"], raw_dir=raw, backup_dir=bdir)
    assert (r2["restored"], r2["skipped"]) == (0, 3), r2
    assert len(list(raw.glob("*.json"))) == 3, "restore created duplicates!"

    # --- guards: traversal-y / malformed names are rejected ---
    for bad in ("../../etc/passwd", "receipts-oops.tar.gz", "x.tar.gz"):
        try:
            bk.restore_backup(bad, raw_dir=raw, backup_dir=bdir)
            assert False, f"accepted bad name: {bad}"
        except ValueError:
            pass

    # --- delete ---
    bk.delete_backup(meta["name"], backup_dir=bdir)
    assert bk.list_backups(bdir) == []
    print("backup OK: gzip archive + manifest, restore de-dupes (1 added / 2 skipped),")
    print("           re-restore is a no-op, bad names rejected, delete works")
    print("\nALL BACKUP TESTS PASSED")


def run_daily(tmp: Path):
    """Scheduled-backup behaviour: snapshot only on change, prune keeps N autos
    and never touches manual backups."""
    import time
    raw = tmp / "raw"; bdir = tmp / "backups"; raw.mkdir()

    def add(bc):
        (raw / f"{bc}.json").write_text(json.dumps(_receipt(bc, 1)))

    # No receipts -> skip.
    r = bk.daily_backup_tick(keep=5, raw_dir=raw, backup_dir=bdir)
    assert r["skipped"] and r["reason"] == "no receipts", r

    add("AAA")
    assert bk.daily_backup_tick(keep=5, raw_dir=raw, backup_dir=bdir)["created"], "first snapshot"
    # Unchanged -> skip (no duplicate daily archive).
    r = bk.daily_backup_tick(keep=5, raw_dir=raw, backup_dir=bdir)
    assert r["skipped"] and r["reason"] == "unchanged", r
    add("BBB")
    assert bk.daily_backup_tick(keep=5, raw_dir=raw, backup_dir=bdir)["created"], "snapshot after change"

    # Retention: a manual backup + several autos, keep=2 -> only autos pruned.
    bk.create_backup(raw_dir=raw, backup_dir=bdir, label="manual-keep-me")
    for i in range(3):
        add(f"C{i}")
        bk.create_backup(raw_dir=raw, backup_dir=bdir, label="auto: daily")
        time.sleep(1.05)  # keep timestamps (and names) distinct
    bk.prune_backups(keep=2, backup_dir=bdir)
    autos = [b for b in bk.list_backups(bdir) if b["label"].startswith("auto:")]
    manual = [b for b in bk.list_backups(bdir) if b["label"] == "manual-keep-me"]
    assert len(autos) == 2, autos
    assert len(manual) == 1, "prune deleted a manual backup!"
    print("daily OK: snapshot-on-change only, unchanged ticks skipped,")
    print("          retention keeps newest 2 autos and never prunes manual backups")
    print("\nALL DAILY-BACKUP TESTS PASSED")


def run_settings(tmp: Path):
    """Persisted schedule settings: defaults from config, overrides survive, and
    values are validated."""
    from costco_archiver import config
    config.DATA_DIR = tmp
    config.BACKUP_DAILY, config.BACKUP_INTERVAL_HOURS, config.BACKUP_KEEP = True, 24.0, 14

    assert bk.get_settings() == {"daily": True, "interval_hours": 24.0, "keep": 14}
    # Partial update persists and merges with unchanged fields.
    bk.update_settings(keep=5)
    assert bk.get_settings()["keep"] == 5 and bk.get_settings()["daily"] is True
    bk.update_settings(daily=False, interval_hours=12)
    got = bk.get_settings()
    assert (got["daily"], got["interval_hours"], got["keep"]) == (False, 12.0, 5), got
    # Bounds are enforced.
    for field, val in (("interval_hours", 0), ("interval_hours", 1000),
                       ("keep", -1), ("keep", 5000)):
        try:
            bk.update_settings(**{field: val})
            assert False, f"accepted out-of-range {field}={val}"
        except ValueError:
            pass
    print("settings OK: defaults from config, partial updates persist, bounds enforced")
    print("\nALL SETTINGS TESTS PASSED")


if __name__ == "__main__":
    for fn in (run, run_daily, run_settings):
        d = Path(tempfile.mkdtemp())
        try:
            fn(d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
