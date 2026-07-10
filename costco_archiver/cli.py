"""Command-line interface.

Usage:
  python -m costco_archiver login      # open browser, sign in, cache creds
  python -m costco_archiver fetch      # download all warehouse/gas receipts
  python -m costco_archiver online     # harvest online-order data (browser)
  python -m costco_archiver parse      # build deduplicated CSVs from raw data
  python -m costco_archiver all        # login -> fetch -> online -> parse
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from . import config
from .auth import Credentials, login_and_get_credentials

CRED_CACHE = config.DATA_DIR / "credentials.json"


def _load_or_login(
    force_login: bool = False, timeout: int = 300, channel: str | None = None
) -> Credentials:
    # Env override (paste headers manually if the automated path ever fails).
    env_token = os.environ.get("COSTCO_ID_TOKEN")
    env_cid = os.environ.get("COSTCO_CLIENT_ID")
    if env_token and env_cid:
        print(">>> Using COSTCO_ID_TOKEN / COSTCO_CLIENT_ID from environment.")
        return Credentials(id_token=env_token, client_id=env_cid)

    if not force_login and CRED_CACHE.exists():
        data = json.loads(CRED_CACHE.read_text())
        return Credentials(**data)

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


def cmd_online(args) -> None:
    from .harvest import harvest_online_orders

    # Ensure we're logged in (persistent profile) before harvesting.
    _load_or_login(timeout=args.timeout, channel=getattr(args, "channel", None))
    summary = harvest_online_orders(scroll_rounds=args.scroll_rounds)
    print(json.dumps(summary, indent=2))


def cmd_parse(args) -> None:
    from .parse import parse_all

    summary = parse_all()
    print("\nParse summary:")
    print(json.dumps(summary, indent=2))


def cmd_all(args) -> None:
    cmd_fetch(args)
    if not args.skip_online:
        cmd_online(args)
    cmd_parse(args)


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

    sp = sub.add_parser("fetch", help="download warehouse/gas receipts")
    add_common(sp)
    sp.add_argument("--months-back", type=int, default=36)
    sp.add_argument("--max-empty", type=int, default=6,
                    help="stop after N consecutive empty months")
    sp.add_argument("--doc-type", default="all")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("online", help="harvest online-order data via browser")
    add_common(sp)
    sp.add_argument("--scroll-rounds", type=int, default=8)
    sp.set_defaults(func=cmd_online)

    sp = sub.add_parser("parse", help="build deduplicated CSVs")
    add_common(sp)
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("all", help="login -> fetch -> online -> parse")
    add_common(sp)
    sp.add_argument("--months-back", type=int, default=36)
    sp.add_argument("--max-empty", type=int, default=6)
    sp.add_argument("--doc-type", default="all")
    sp.add_argument("--scroll-rounds", type=int, default=8)
    sp.add_argument("--skip-online", action="store_true")
    sp.set_defaults(func=cmd_all)

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
