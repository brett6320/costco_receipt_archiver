"""Compressed backups of the imported raw receipts.

A backup is a gzip-compressed tar archive of everything in ``data/raw`` (the
canonical imported data — the CSVs, Markdown, and PDFs are all regenerated from
it), plus a small ``manifest.json`` describing the snapshot. Backups live in
``data/backups/`` as ``receipts-YYYYMMDD-HHMMSS.tar.gz``.

Restore is **idempotent / de-duplicating**: each receipt in the archive is keyed
by its identity (:func:`costco_archiver.parse._receipt_key` — the transaction
barcode, or a stable composite for barcode-less receipts) and only written if a
receipt with that identity isn't already on disk. Re-restoring the same backup,
or restoring one that overlaps what you already have, never creates duplicates.

Management (create / list / restore / delete) is admin-only in the web UI and is
also available from the CLI (``python -m costco_archiver backup …``).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .ingest import _safe_name
from .parse import _receipt_key

# Auto-created backups carry a label starting with this prefix; only these are
# eligible for retention pruning (manual / labelled backups are always kept).
_AUTO_PREFIX = "auto:"

_SETTINGS_LOCK = threading.Lock()


# --- schedule / retention settings -------------------------------------------
# The COSTCO_BACKUP_* env values are the DEFAULTS; admins can override them at
# runtime from the UI, persisted to data/backup_settings.json so they survive
# restarts. get_settings() merges the two (persisted wins).
def _settings_path() -> Path:
    return config.DATA_DIR / "backup_settings.json"


def get_settings() -> dict:
    """Effective schedule settings: {daily, interval_hours, keep}."""
    daily, interval, keep = (config.BACKUP_DAILY, config.BACKUP_INTERVAL_HOURS,
                             config.BACKUP_KEEP)
    try:
        raw = json.loads(_settings_path().read_text())
        if isinstance(raw.get("daily"), bool):
            daily = raw["daily"]
        iv = raw.get("interval_hours")
        if isinstance(iv, (int, float)) and iv > 0:
            interval = float(iv)
        k = raw.get("keep")
        if isinstance(k, int) and not isinstance(k, bool) and k >= 0:
            keep = k
    except Exception:
        pass  # missing/corrupt file → fall back to env defaults
    return {"daily": bool(daily), "interval_hours": float(interval), "keep": int(keep)}


def update_settings(daily=None, interval_hours=None, keep=None) -> dict:
    """Persist changed schedule settings (validated). Returns the new settings."""
    with _SETTINGS_LOCK:
        cur = get_settings()
        if daily is not None:
            cur["daily"] = bool(daily)
        if interval_hours is not None:
            iv = float(interval_hours)
            if not (0 < iv <= 720):  # up to 30 days
                raise ValueError("interval_hours must be between 0 and 720")
            cur["interval_hours"] = iv
        if keep is not None:
            k = int(keep)
            if not (0 <= k <= 1000):
                raise ValueError("keep must be between 0 and 1000")
            cur["keep"] = k
        config.ensure_dirs()
        _settings_path().write_text(json.dumps(cur, indent=2))
        return cur

# Backup file names we create and will accept back for restore/delete. Anchoring
# on this exact shape also hardens delete/restore against path-traversal names.
_NAME_RE = re.compile(r"^receipts-\d{8}-\d{6}\.tar\.gz$")
_ARCHIVE_GLOB = "receipts-*.tar.gz"


def _safe_backup_path(name: str, backup_dir: Path) -> Path:
    """Resolve `name` to a path guaranteed to sit directly inside `backup_dir`
    and match our backup-name shape. Rejects traversal / unexpected names."""
    base = Path(str(name)).name  # strip any directory components
    if not _NAME_RE.match(base):
        raise ValueError(f"invalid backup name: {name!r}")
    return backup_dir / base


def _receipt_key_safe(rec: dict) -> str:
    try:
        return _receipt_key(rec)
    except Exception:
        return ""


def _existing_keys(raw_dir: Path) -> set[str]:
    keys: set[str] = set()
    for f in raw_dir.glob("*.json"):
        try:
            keys.add(_receipt_key(json.loads(f.read_text())))
        except Exception:
            continue
    return keys


def _fingerprint(keys) -> str:
    """A stable digest of the set of receipt identities — two raw dirs with the
    same receipts share a fingerprint, so we can skip an unchanged daily backup."""
    joined = "\n".join(sorted(k for k in keys if k))
    return hashlib.sha256(joined.encode()).hexdigest()


def raw_fingerprint(raw_dir: Path = config.RAW_DIR) -> str:
    return _fingerprint(_existing_keys(raw_dir))


def latest_fingerprint(backup_dir: Path = config.BACKUP_DIR) -> str | None:
    """Fingerprint recorded in the most recent backup, or None if there are none."""
    for f in sorted(backup_dir.glob(_ARCHIVE_GLOB), reverse=True):
        try:
            with tarfile.open(f, "r:gz") as tar:
                m = tar.extractfile("manifest.json")
                if m is not None:
                    return json.loads(m.read().decode()).get("fingerprint")
        except Exception:
            continue
    return None


def create_backup(raw_dir: Path = config.RAW_DIR,
                  backup_dir: Path = config.BACKUP_DIR,
                  label: str = "") -> dict:
    """Snapshot every raw receipt into a new ``receipts-<ts>.tar.gz``.

    Returns metadata for the created archive. Creates the backup even when there
    are no receipts (an empty snapshot), so the caller always gets a file back.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(raw_dir.glob("*.json"))
    keys = [k for k in (_receipt_key_safe(json.loads(f.read_text()))
                        for f in files) if k]
    now = int(time.time())
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = backup_dir / f"receipts-{ts}.tar.gz"
    # Keep the canonical name shape (so it round-trips through restore/delete). If
    # two backups land in the same second, wait a beat and re-stamp.
    for _ in range(3):
        if not out.exists():
            break
        time.sleep(1.0)
        now = int(time.time())
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = backup_dir / f"receipts-{ts}.tar.gz"
    name = out.name
    manifest = {
        "schema": 1,
        "app": "costco-receipt-archiver",
        "created_at": now,
        "receipt_count": len(files),
        "label": str(label or ""),
        "fingerprint": _fingerprint(keys),
        "keys": keys,
    }
    with tarfile.open(out, "w:gz") as tar:
        blob = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        info.mtime = now
        tar.addfile(info, io.BytesIO(blob))
        for f in files:
            tar.add(f, arcname=f"raw/{f.name}")
    return {"name": name, "size": out.stat().st_size,
            "receipt_count": len(files), "created_at": now,
            "label": manifest["label"], "path": str(out)}


def list_backups(backup_dir: Path = config.BACKUP_DIR) -> list[dict]:
    """All backups, newest first, with size and (from the manifest) receipt count."""
    if not backup_dir.exists():
        return []
    out = []
    for f in sorted(backup_dir.glob(_ARCHIVE_GLOB), reverse=True):
        st = f.stat()
        info = {"name": f.name, "size": st.st_size,
                "created_at": int(st.st_mtime), "receipt_count": None, "label": ""}
        try:
            with tarfile.open(f, "r:gz") as tar:
                m = tar.extractfile("manifest.json")
                if m is not None:
                    man = json.loads(m.read().decode())
                    info["receipt_count"] = man.get("receipt_count")
                    info["created_at"] = man.get("created_at", info["created_at"])
                    info["label"] = man.get("label", "")
        except Exception:
            pass  # unreadable/legacy archive: fall back to filesystem stats
        out.append(info)
    return out


def _unique_dest(raw_dir: Path, rec: dict) -> Path:
    """A raw-dir path for a restored receipt using the standard naming scheme,
    avoiding collision with an unrelated receipt that happens to share the name."""
    stem = _safe_name(rec)
    dest = raw_dir / f"{stem}.json"
    i = 1
    while dest.exists():
        dest = raw_dir / f"{stem}__{i}.json"
        i += 1
    return dest


def restore_backup(name: str, raw_dir: Path = config.RAW_DIR,
                   backup_dir: Path = config.BACKUP_DIR) -> dict:
    """Restore receipts from a backup WITHOUT creating duplicates.

    A receipt is written only if its identity isn't already present in raw_dir;
    otherwise it's skipped. Returns {restored, skipped, invalid, total}.
    """
    path = _safe_backup_path(name, backup_dir)
    if not path.exists():
        raise ValueError(f"no such backup: {name}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    existing = _existing_keys(raw_dir)
    restored = skipped = invalid = 0
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            # Only the receipt payloads; ignore the manifest and anything else.
            if not (member.isfile() and member.name.startswith("raw/")
                    and member.name.endswith(".json")):
                continue
            fobj = tar.extractfile(member)
            if fobj is None:
                continue
            try:
                rec = json.loads(fobj.read().decode())
            except Exception:
                invalid += 1
                continue
            key = _receipt_key_safe(rec)
            if key and key in existing:
                skipped += 1
                continue
            # Basename only — never trust the archived path (traversal guard).
            dest = _unique_dest(raw_dir, rec)
            dest.write_text(json.dumps(rec, indent=2))
            if key:
                existing.add(key)
            restored += 1
    return {"name": path.name, "restored": restored, "skipped": skipped,
            "invalid": invalid, "total": restored + skipped + invalid}


def delete_backup(name: str, backup_dir: Path = config.BACKUP_DIR) -> None:
    path = _safe_backup_path(name, backup_dir)
    if not path.exists():
        raise ValueError(f"no such backup: {name}")
    path.unlink()


def backup_bytes(name: str, backup_dir: Path = config.BACKUP_DIR) -> bytes:
    """Raw archive bytes, for the download endpoint."""
    return _safe_backup_path(name, backup_dir).read_bytes()


def prune_backups(keep: int, backup_dir: Path = config.BACKUP_DIR) -> list[str]:
    """Keep the newest `keep` AUTOMATIC backups; delete older ones. Manual /
    labelled backups are never pruned. Returns the names removed."""
    if keep is None or keep < 0:
        return []
    autos = [b for b in list_backups(backup_dir)
             if str(b.get("label", "")).startswith(_AUTO_PREFIX)]
    removed = []
    for b in autos[keep:]:  # list_backups is newest-first
        try:
            delete_backup(b["name"], backup_dir)
            removed.append(b["name"])
        except Exception:
            continue
    return removed


def daily_backup_tick(keep: int = None, raw_dir: Path = config.RAW_DIR,
                      backup_dir: Path = config.BACKUP_DIR) -> dict:
    """One scheduled-backup cycle: snapshot the raw data if it changed since the
    last backup, then prune old automatic backups. Returns what happened."""
    if keep is None:
        keep = config.BACKUP_KEEP
    raw_fp = raw_fingerprint(raw_dir)
    # Nothing imported yet, or unchanged since the last backup → skip (but still
    # prune, in case retention shrank).
    files_present = any(raw_dir.glob("*.json")) if raw_dir.exists() else False
    if not files_present or raw_fp == latest_fingerprint(backup_dir):
        return {"created": None, "skipped": True,
                "reason": "no receipts" if not files_present else "unchanged",
                "pruned": prune_backups(keep, backup_dir)}
    meta = create_backup(raw_dir=raw_dir, backup_dir=backup_dir, label="auto: daily")
    return {"created": meta, "skipped": False,
            "pruned": prune_backups(keep, backup_dir)}
