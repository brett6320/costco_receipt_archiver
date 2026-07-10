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


def _parse_curl(text: str) -> tuple[str, dict, str]:
    """Parse a browser 'Copy as cURL' command into (url, headers, data).

    Handles bash ($'...') and cmd/PowerShell (^ line continuations) variants.
    """
    import shlex
    t = text.strip()
    # Normalize line continuations and Chrome's ANSI-C quoting.
    t = t.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")
    t = t.replace(" $'", " '")
    try:
        tokens = shlex.split(t)
    except ValueError:
        tokens = shlex.split(t.replace("$'", "'"))

    url, headers, data = "", {}, ""
    data_flags = ("--data", "--data-raw", "--data-binary", "--data-ascii", "-d")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            raw = tokens[i + 1]
            # Skip HTTP/2 pseudo-headers like ':authority: ...'.
            if ":" in raw and not raw.startswith(":"):
                k, v = raw.split(":", 1)
                if k.strip():
                    headers[k.strip()] = v.strip()
            i += 2
            continue
        if tok in ("-b", "--cookie") and i + 1 < len(tokens):
            headers["cookie"] = tokens[i + 1]
            i += 2
            continue
        if tok in data_flags and i + 1 < len(tokens):
            data = tokens[i + 1]
            i += 2
            continue
        if tok.lower().startswith("http"):
            url = tok
        i += 1
    return url, headers, data


def cmd_import_curl(args) -> None:
    """Import exact API headers from a DevTools 'Copy as cURL' (bypasses Kasada).

    Costco uses Kasada/Akamai bot detection that blocks automated browsers
    (429 on sign-in). Instead: log in with your NORMAL browser, open DevTools →
    Network, load your receipts page, right-click the request to
    ecom-api.costco.com/.../graphql → Copy → Copy as cURL. Then run this; it reads
    the cURL from your clipboard and captures every header verbatim.
    """
    config.ensure_dirs()
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
    if not text or "curl" not in text.lower():
        raise SystemExit("No cURL command found (clipboard empty? use --file <path>).")

    url, headers, data = _parse_curl(text)
    # Drop headers that break an httpx replay (auto-managed / HTTP-2 pseudo).
    _DROP = {"content-length", "accept-encoding", "host", "connection",
             "transfer-encoding"}
    headers = {
        k: v for k, v in headers.items()
        if not k.startswith(":") and k.lower() not in _DROP
    }
    # Normalize header keys to what the API client / capture expects.
    hl = {k.lower(): v for k, v in headers.items()}
    auth = hl.get("costco-x-authorization") or hl.get("authorization")
    cid = hl.get("costco-x-wcs-clientid")
    if not auth or not cid:
        raise SystemExit(
            "That cURL is missing costco-x-authorization / costco-x-wcs-clientid. "
            "Make sure you copied the request to ecom-api.costco.com/.../graphql "
            "(not a different request)."
        )

    config.API_HEADERS_FILE.write_text(json.dumps(headers, indent=2))
    token = auth.replace("Bearer ", "").strip()
    creds = Credentials(id_token=token, client_id=cid)
    CRED_CACHE.write_text(json.dumps(asdict(creds), indent=2))

    # Save the exact request so fetch can replay Costco's real query verbatim.
    req_saved = False
    if data and url:
        try:
            body = json.loads(data)
            config.API_REQUEST_FILE.write_text(
                json.dumps({"url": url, "body": body}, indent=2))
            req_saved = True
        except ValueError:
            config.API_REQUEST_FILE.write_text(
                json.dumps({"url": url, "raw_body": data}, indent=2))
            req_saved = True

    print(f"\n>>> Imported {len(headers)} headers from cURL"
          + (" + captured the request query." if req_saved else "."))
    exp = token_expiry(token)
    if exp:
        import datetime
        mins = int((exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds()) // 60
        print(f">>> Token valid ~{max(0, mins)} more min.")
    if token_is_expired(token):
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


def cmd_all(args) -> None:
    cmd_fetch(args)
    if not args.skip_online:
        cmd_online(args)
    cmd_parse(args)
    if not getattr(args, "skip_pdf", False):
        cmd_pdf(args)
    cmd_markdown(args)


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

    sp = sub.add_parser("online", help="harvest online-order data via browser")
    add_common(sp)
    sp.add_argument("--scroll-rounds", type=int, default=8)
    sp.set_defaults(func=cmd_online)

    sp = sub.add_parser("parse", help="build deduplicated CSVs")
    add_common(sp)
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("import",
                        help="ingest saved receipt HTML/PDF files (API-free path)")
    sp.add_argument("paths", nargs="*", help="receipt .html/.pdf files or directories")
    sp.add_argument("--clipboard", action="store_true",
                    help="ingest a receipt's HTML copied to the clipboard")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("pdf", help="render each receipt to a PDF archive (data/pdfs)")
    sp.add_argument("--force", action="store_true", help="re-render existing PDFs")
    sp.set_defaults(func=cmd_pdf)

    sp = sub.add_parser("web", help="launch the local receipt search UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("markdown",
                        help="generate a Markdown archive (index + per-receipt pages)")
    sp.set_defaults(func=cmd_markdown)

    sp = sub.add_parser("all", help="login -> fetch -> online -> parse -> pdf")
    add_common(sp)
    sp.add_argument("--months-back", type=int, default=36)
    sp.add_argument("--max-empty", type=int, default=6)
    sp.add_argument("--doc-type", default="all")
    sp.add_argument("--scroll-rounds", type=int, default=8)
    sp.add_argument("--skip-online", action="store_true")
    sp.add_argument("--skip-pdf", action="store_true")
    sp.set_defaults(func=cmd_all)

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
