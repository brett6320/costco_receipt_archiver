"""Command-line interface.

Usage:
  python -m costco_archiver login      # open browser, sign in, cache creds
  python -m costco_archiver fetch      # download all warehouse/gas receipts
  python -m costco_archiver parse      # build deduplicated CSVs from raw data
  python -m costco_archiver all        # login -> fetch -> parse
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from . import config
from .auth import (
    Credentials,
    login_and_get_credentials,
    token_is_expired,
    token_expiry,
)

CRED_CACHE = config.DATA_DIR / "credentials.json"


def _load_or_login(
    force_login: bool = False, timeout: int = 300, channel: str | None = None
) -> Credentials:
    # Env override (paste headers manually if the automated path ever fails).
    env_token = os.environ.get("COSTCO_ID_TOKEN")
    env_cid = os.environ.get("COSTCO_CLIENT_ID")
    if env_token and env_cid:
        print(">>> Using COSTCO_ID_TOKEN / COSTCO_CLIENT_ID from environment.")
        if token_is_expired(env_token):
            print("!!! WARNING: COSTCO_ID_TOKEN is already EXPIRED "
                  "(these tokens last ~15 min). Re-grab a fresh one.")
        return Credentials(id_token=env_token, client_id=env_cid)

    if not force_login and CRED_CACHE.exists():
        data = json.loads(CRED_CACHE.read_text())
        creds = Credentials(**data)
        if token_is_expired(creds.id_token):
            print(">>> Cached token expired (they last ~15 min) — re-logging in.")
        else:
            return creds

    config.ensure_dirs()
    return login_and_get_credentials(
        timeout_seconds=timeout, cred_cache=CRED_CACHE, browser_channel=channel
    )


def cmd_login(args) -> None:
    creds = login_and_get_credentials(
        timeout_seconds=args.timeout,
        cred_cache=CRED_CACHE,
        browser_channel=getattr(args, "channel", None),
    )
    print(f"Cached credentials to {CRED_CACHE}")
    _ = asdict(creds)


_CONSOLE_SNIPPET = (
    "copy(JSON.stringify({idToken: localStorage.getItem('idToken'), "
    "clientID: localStorage.getItem('clientID')}))"
)


def _extract_creds_from_blob(text: str) -> Credentials:
    """Parse credentials from the JSON blob the console snippet copies.

    Falls back to loose key/value scraping so a partially-mangled paste still
    works. Handles the raw JSON `{"idToken": "...", "clientID": "..."}`.
    """
    text = text.strip()
    token = client_id = None
    try:
        blob = json.loads(text)
        token = blob.get("idToken") or blob.get("id_token")
        client_id = blob.get("clientID") or blob.get("clientId")
    except (ValueError, AttributeError):
        pass
    if not (token and client_id):
        import re
        m_t = re.search(r'idToken"?\s*[:=]\s*"?([A-Za-z0-9._\-]+)', text)
        m_c = re.search(r'clientI[dD]"?\s*[:=]\s*"?([A-Za-z0-9._\-]+)', text)
        token = token or (m_t.group(1) if m_t else None)
        client_id = client_id or (m_c.group(1) if m_c else None)
    if not (token and client_id):
        raise SystemExit(
            "Couldn't find both idToken and clientID in the input. Make sure you "
            "ran the snippet and the full JSON was captured."
        )
    return Credentials(id_token=token.replace("Bearer ", "").strip(),
                       client_id=client_id.strip())


def _read_clipboard() -> str | None:
    """Read the macOS clipboard (pbpaste). Returns None if unavailable/empty."""
    import subprocess
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        return out.stdout or None
    except Exception:
        return None


def cmd_paste_token(args) -> None:
    """Supply credentials grabbed from your own logged-in browser.

    Bypasses automated login entirely — useful when Costco bot-blocks (429) the
    scripted browser. The JWT is long, so we DON'T rely on pasting into a prompt
    (terminals truncate long pastes). Instead we read the clipboard the console
    snippet already populated, or a file you point us at.
    """
    config.ensure_dirs()

    # 1) --file wins if given.
    if getattr(args, "file", None):
        creds = _extract_creds_from_blob(Path(args.file).read_text())
    else:
        print("In your normal browser, logged into costco.com, open DevTools →")
        print("Console and run this (it copies the JSON to your clipboard):\n")
        print(f"    {_CONSOLE_SNIPPET}\n")
        clip = _read_clipboard()
        if clip and ("idToken" in clip or "clientID" in clip):
            print(">>> Read credentials from your clipboard.")
            creds = _extract_creds_from_blob(clip)
        else:
            print("Clipboard didn't contain the token. Alternatives:")
            print("  • re-run the snippet, then: python -m costco_archiver paste-token")
            print("  • or save the JSON to a file and pass --file <path>\n")
            raise SystemExit("No credentials found on clipboard.")

    CRED_CACHE.write_text(json.dumps(asdict(creds), indent=2))
    print(f"\nSaved to {CRED_CACHE}.")
    print("Token is valid ~1 hour — run this now:")
    print("    python -m costco_archiver fetch && python -m costco_archiver parse")


from .capture import parse_curl as _parse_curl, save_from_curl, CaptureError


def cmd_import_curl(args) -> None:
    """Import exact API headers from a DevTools 'Copy as cURL' (bypasses Kasada).

    Costco uses Kasada/Akamai bot detection that blocks automated browsers
    (429 on sign-in). Instead: log in with your NORMAL browser, open DevTools →
    Network, load your receipts page, right-click the request to
    ecom-api.costco.com/.../graphql → Copy → Copy as cURL. Then run this; it reads
    the cURL from your clipboard and captures every header verbatim.
    """
    text = None
    if getattr(args, "file", None):
        text = Path(args.file).read_text()
    else:
        print("In your NORMAL browser (logged into costco.com):")
        print("  1. DevTools → Network tab")
        print("  2. Open your receipts page so it loads")
        print("  3. Right-click the '.../orders/graphql' request → Copy → Copy as cURL")
        print("  4. Come back here (it reads your clipboard)\n")
        text = _read_clipboard()
    try:
        result = save_from_curl(text or "")
    except CaptureError as ex:
        raise SystemExit(str(ex))

    print(f"\n>>> Imported {result['headers']} headers from cURL"
          + (" + captured the receipts query." if result["has_query"] else "."))
    print(f">>> Token valid ~{result['token_minutes']} more min.")
    if result["expired"]:
        print("!!! That token is ALREADY expired — recopy a fresh cURL and retry.")
    else:
        print(">>> Run NOW:  python -m costco_archiver fetch && python -m costco_archiver parse")


def cmd_fetch(args) -> None:
    from .fetch import fetch_all_receipts

    creds = _load_or_login(timeout=args.timeout, channel=getattr(args, "channel", None))
    summary = fetch_all_receipts(
        creds,
        months_back=args.months_back,
        max_empty_windows=args.max_empty,
        document_type=args.doc_type,
    )
    print("\nFetch summary:")
    print(json.dumps(summary, indent=2))


def cmd_parse(args) -> None:
    from .parse import parse_all

    summary = parse_all()
    print("\nParse summary:")
    print(json.dumps(summary, indent=2))


def cmd_import(args) -> None:
    """Ingest saved receipt HTML/PDF files into raw receipt JSON."""
    from .ingest import ingest_paths, receipt_from_html, save_receipt

    if getattr(args, "clipboard", False):
        text = _read_clipboard()
        if not text or "<" not in text:
            raise SystemExit("Clipboard has no HTML. Copy a receipt's outerHTML first.")
        rec = receipt_from_html(text)
        if not rec.get("itemArray"):
            raise SystemExit("No line items found in the clipboard HTML.")
        out = save_receipt(rec)
        print(f"  clipboard → {out.name}  ({len(rec['itemArray'])} items, "
              f"total {rec.get('total')})")
        summary = {"ingested": 1}
    else:
        if not args.paths:
            raise SystemExit("Provide file/dir paths, or --clipboard.")
        summary = ingest_paths([Path(p) for p in args.paths])
    print(json.dumps(summary, indent=2))
    # Snapshot the imported data (unless suppressed) so every import is restorable.
    if summary.get("ingested") and not getattr(args, "no_backup", False):
        try:
            from .backup import create_backup
            b = create_backup(label="auto: after import")
            print(f"Backed up {b['receipt_count']} receipts → {b['name']} "
                  f"({b['size'] // 1024} KB)")
        except Exception as ex:
            print(f"(backup skipped: {ex})")


def cmd_pdf(args) -> None:
    from .pdf import render_all_pdfs

    summary = render_all_pdfs(force=getattr(args, "force", False))
    print(json.dumps(summary, indent=2))


def cmd_web(args) -> None:
    from .web import serve

    serve(host=args.host, port=args.port)


def cmd_markdown(args) -> None:
    from .markdown import generate_markdown

    summary = generate_markdown()
    print(json.dumps(summary, indent=2))


def cmd_refresh(args) -> None:
    """Refresh metadata (PDF, barcode, Markdown) for a single receipt."""
    from .markdown import generate_one
    from .pdf import render_one_pdf
    import re

    rid = args.receipt_id
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", rid)
    key = safe if (config.RAW_DIR / f"{safe}.json").exists() else None
    if key is None:  # fall back to matching by transactionBarcode
        for f in config.RAW_DIR.glob("*.json"):
            try:
                if str(json.loads(f.read_text()).get("transactionBarcode") or "") == rid:
                    key = f.stem
                    break
            except Exception:
                continue
    if key is None:
        raise SystemExit(f"Receipt {rid} not found in {config.RAW_DIR}")
    md = generate_one(key)
    pdf = render_one_pdf(key) if not args.no_pdf else False
    print(json.dumps({"receipt": rid, "key": key, "markdown": md, "pdf": pdf}, indent=2))


def _prompt_new_password(username: str) -> str:
    import getpass
    while True:
        pw = getpass.getpass(f"New password for {username!r}: ")
        if len(pw) < 8:
            print("  Password must be at least 8 characters.")
            continue
        if pw != getpass.getpass("Confirm password: "):
            print("  Passwords didn't match — try again.")
            continue
        return pw


def _print_totp_enrollment(username: str, secret: str) -> None:
    from .webauth import provisioning_uri
    print("\nScan this in your authenticator app (Google Authenticator, 1Password, "
          "Authy, …),\nor enter the secret manually:\n")
    print(f"  Account : {config.AUTH_ISSUER}:{username}")
    print(f"  Secret  : {secret}")
    print(f"  otpauth : {provisioning_uri(username, secret)}\n")
    print("Then sign in with your password + the 6-digit code it shows.")


def cmd_auth_adduser(args) -> None:
    from . import webauth
    pw = _prompt_new_password(args.username)
    try:
        secret = webauth.add_user(args.username, pw, role=getattr(args, "role", None)
                                  or webauth.DEFAULT_ROLE)
    except ValueError as ex:
        raise SystemExit(str(ex))
    role = webauth.get_role(args.username)
    print(f"\n✓ Created {role} {args.username!r}.")
    _print_totp_enrollment(args.username, secret)


def cmd_auth_setrole(args) -> None:
    from . import webauth
    try:
        webauth.set_role(args.username, args.role)
    except ValueError as ex:
        raise SystemExit(str(ex))
    print(f"✓ {args.username!r} is now {args.role}.")


def cmd_auth_passwd(args) -> None:
    from . import webauth
    pw = _prompt_new_password(args.username)
    try:
        webauth.set_password(args.username, pw)
    except ValueError as ex:
        raise SystemExit(str(ex))
    print(f"✓ Password updated for {args.username!r}.")


def cmd_auth_reset_mfa(args) -> None:
    from . import webauth
    try:
        secret = webauth.reset_totp(args.username)
    except ValueError as ex:
        raise SystemExit(str(ex))
    print(f"✓ New TOTP secret for {args.username!r} (old codes no longer work).")
    _print_totp_enrollment(args.username, secret)


def cmd_auth_deluser(args) -> None:
    from . import webauth
    try:
        webauth.delete_user(args.username)
    except ValueError as ex:
        raise SystemExit(str(ex))
    print(f"✓ Deleted user {args.username!r}.")


def cmd_auth_users(args) -> None:
    from . import webauth
    users = webauth.users_overview()
    if not users:
        print("No web accounts configured. Add one (first user becomes admin): "
              "python -m costco_archiver auth adduser <name>")
        return
    print("Web accounts:")
    for u in users:
        print(f"  • {u['username']:<20} {u['role']}")


def _fmt_backup_ts(ts) -> str:
    import datetime
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(
        int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def cmd_backup_create(args) -> None:
    from .backup import create_backup
    b = create_backup(label=getattr(args, "label", "") or "")
    print(f"✓ Backup created: {b['name']} "
          f"({b['receipt_count']} receipts, {b['size'] // 1024} KB)")


def cmd_backup_list(args) -> None:
    from .backup import list_backups
    backups = list_backups()
    if not backups:
        print("No backups yet. Create one: python -m costco_archiver backup create")
        return
    print("Backups (newest first):")
    for b in backups:
        cnt = b["receipt_count"] if b["receipt_count"] is not None else "?"
        label = f"  — {b['label']}" if b.get("label") else ""
        print(f"  • {b['name']:<30} {_fmt_backup_ts(b.get('created_at'))}  "
              f"{cnt} receipts  {b['size'] // 1024} KB{label}")


def cmd_backup_restore(args) -> None:
    from .backup import restore_backup
    try:
        r = restore_backup(args.name)
    except ValueError as ex:
        raise SystemExit(str(ex))
    extra = f", {r['invalid']} invalid" if r["invalid"] else ""
    print(f"✓ Restored {r['restored']}, skipped {r['skipped']} duplicate(s){extra} "
          f"(of {r['total']}).")
    if r["restored"]:
        print("  Rebuild outputs:  python -m costco_archiver parse "
              "&& python -m costco_archiver markdown && python -m costco_archiver pdf")


def cmd_backup_delete(args) -> None:
    from .backup import delete_backup
    try:
        delete_backup(args.name)
    except ValueError as ex:
        raise SystemExit(str(ex))
    print(f"✓ Deleted {args.name}.")


def cmd_backup_daily(args) -> None:
    """One scheduled-backup cycle (snapshot if changed, then prune). Meant for a
    cron job when you're not running the web server (which schedules its own)."""
    from .backup import daily_backup_tick
    res = daily_backup_tick(keep=getattr(args, "keep", None))
    if res.get("created"):
        c = res["created"]
        print(f"✓ Snapshot {c['name']} "
              f"({c['receipt_count']} receipts, {c['size'] // 1024} KB)")
    else:
        print(f"• Skipped ({res.get('reason')}).")
    if res.get("pruned"):
        print(f"  Pruned {len(res['pruned'])} old backup(s).")


def cmd_all(args) -> None:
    cmd_fetch(args)
    cmd_parse(args)
    if not getattr(args, "skip_pdf", False):
        cmd_pdf(args)
    cmd_markdown(args)


def webauth_roles() -> tuple[str, ...]:
    """Valid account roles, imported lazily so `build_parser` stays import-light."""
    from . import webauth
    return webauth.VALID_ROLES


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="costco_archiver", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--timeout", type=int, default=300,
                        help="seconds to wait for interactive login")
        sp.add_argument("--channel", default=None,
                        choices=["chrome", "msedge", "chromium"],
                        help="browser to drive (default: real Chrome, then Edge, "
                             "then bundled Chromium). Use real Chrome to avoid "
                             "passkey/consent stalls and 429 bot-blocks.")

    sp = sub.add_parser("login", help="interactive browser login")
    add_common(sp)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("paste-token",
                        help="manually supply token from your own browser (bypasses 429)")
    sp.add_argument("--file", default=None,
                    help="path to a file containing the JSON blob (instead of clipboard)")
    sp.set_defaults(func=cmd_paste_token)

    sp = sub.add_parser("import-curl",
                        help="import exact headers from DevTools 'Copy as cURL' "
                             "(best bypass for Kasada/429)")
    sp.add_argument("--file", default=None,
                    help="path to a file with the cURL command (instead of clipboard)")
    sp.set_defaults(func=cmd_import_curl)

    sp = sub.add_parser("fetch", help="download warehouse/gas receipts")
    add_common(sp)
    sp.add_argument("--months-back", type=int, default=36)
    sp.add_argument("--max-empty", type=int, default=6,
                    help="stop after N consecutive empty months")
    sp.add_argument("--doc-type", default="all")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("parse", help="build deduplicated CSVs")
    add_common(sp)
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("import",
                        help="ingest saved receipt HTML/PDF files (API-free path)")
    sp.add_argument("paths", nargs="*", help="receipt .html/.pdf files or directories")
    sp.add_argument("--clipboard", action="store_true",
                    help="ingest a receipt's HTML copied to the clipboard")
    sp.add_argument("--no-backup", action="store_true",
                    help="don't auto-create a compressed backup after importing")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("pdf", help="render each receipt to a PDF archive (data/pdfs)")
    sp.add_argument("--force", action="store_true",
                    help="rewrite every PDF even if unchanged (default: only "
                         "overwrite when the re-render differs)")
    sp.set_defaults(func=cmd_pdf)

    sp = sub.add_parser("web", help="launch the local receipt search UI")
    sp.add_argument("--host", default=config.WEB_HOST,
                    help="bind host (env: COSTCO_WEB_HOST; default 127.0.0.1)")
    sp.add_argument("--port", type=int, default=config.WEB_PORT,
                    help="bind port (env: COSTCO_WEB_PORT or PORT; default 8000)")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("auth", help="manage web-UI accounts (roles + password + TOTP MFA)")
    asub = sp.add_subparsers(dest="auth_command", required=True)
    a = asub.add_parser("adduser",
                        help="create an account (prompts for password; first user "
                             "is always admin)")
    a.add_argument("username")
    a.add_argument("--role", choices=list(webauth_roles()), default=None,
                   help="account role (default: operator; the first account is "
                        "forced to admin)")
    a.set_defaults(func=cmd_auth_adduser)
    a = asub.add_parser("setrole", help="change an account's role (admin/operator)")
    a.add_argument("username")
    a.add_argument("role", choices=list(webauth_roles()))
    a.set_defaults(func=cmd_auth_setrole)
    a = asub.add_parser("passwd", help="change an account's password")
    a.add_argument("username")
    a.set_defaults(func=cmd_auth_passwd)
    a = asub.add_parser("reset-mfa", help="regenerate an account's TOTP secret")
    a.add_argument("username")
    a.set_defaults(func=cmd_auth_reset_mfa)
    a = asub.add_parser("deluser", help="delete an account")
    a.add_argument("username")
    a.set_defaults(func=cmd_auth_deluser)
    a = asub.add_parser("users", help="list accounts and their roles")
    a.set_defaults(func=cmd_auth_users)

    sp = sub.add_parser("backup",
                        help="manage compressed backups of imported receipts (data/raw)")
    bsub = sp.add_subparsers(dest="backup_command", required=True)
    b = bsub.add_parser("create", help="snapshot data/raw into a .tar.gz backup")
    b.add_argument("--label", default="", help="optional label stored in the backup")
    b.set_defaults(func=cmd_backup_create)
    b = bsub.add_parser("list", help="list backups")
    b.set_defaults(func=cmd_backup_list)
    b = bsub.add_parser("restore",
                        help="restore receipts from a backup (skips duplicates)")
    b.add_argument("name", help="backup filename (see `backup list`)")
    b.set_defaults(func=cmd_backup_restore)
    b = bsub.add_parser("delete", help="delete a backup")
    b.add_argument("name")
    b.set_defaults(func=cmd_backup_delete)
    b = bsub.add_parser("daily",
                        help="run one scheduled backup cycle (snapshot if changed, "
                             "then prune) — for cron")
    b.add_argument("--keep", type=int, default=None,
                   help="retain N automatic backups (default: COSTCO_BACKUP_KEEP)")
    b.set_defaults(func=cmd_backup_daily)

    sp = sub.add_parser("markdown",
                        help="generate a Markdown archive (index + per-receipt pages)")
    sp.set_defaults(func=cmd_markdown)

    sp = sub.add_parser("refresh",
                        help="refresh metadata (PDF, barcode, Markdown) for one receipt")
    sp.add_argument("receipt_id", help="receipt transaction barcode / order number")
    sp.add_argument("--no-pdf", action="store_true", help="skip PDF re-render")
    sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("all", help="login -> fetch -> parse -> pdf")
    add_common(sp)
    sp.add_argument("--months-back", type=int, default=36)
    sp.add_argument("--max-empty", type=int, default=6)
    sp.add_argument("--doc-type", default="all")
    sp.add_argument("--skip-pdf", action="store_true")
    sp.set_defaults(func=cmd_all)

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
