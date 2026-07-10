"""Shared 'Copy as cURL' → credentials logic, used by both the CLI and web UI.

Parses a browser DevTools 'Copy as cURL' of Costco's receipts request and saves:
  - api_headers.json  : exact headers (token, clientid, cookies) for replay
  - api_request.json  : the request url + GraphQL body, so fetch replays Costco's
                        own query across date windows
  - credentials.json  : decoded token/clientid (for the ~15-min expiry guard)
"""
from __future__ import annotations

import datetime
import json
import shlex

from . import config
from .auth import Credentials, token_expiry, token_is_expired

# Headers that break an httpx replay (auto-managed or HTTP/2 pseudo-headers).
_DROP = {"content-length", "accept-encoding", "host", "connection",
         "transfer-encoding"}
_DATA_FLAGS = ("--data", "--data-raw", "--data-binary", "--data-ascii", "-d")


def parse_curl(text: str) -> tuple[str, dict, str]:
    """Parse a 'Copy as cURL' command into (url, headers, data)."""
    t = text.strip()
    t = t.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")
    t = t.replace(" $'", " '")
    try:
        tokens = shlex.split(t)
    except ValueError:
        tokens = shlex.split(t.replace("$'", "'"))

    url, headers, data = "", {}, ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            raw = tokens[i + 1]
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
        if tok in _DATA_FLAGS and i + 1 < len(tokens):
            data = tokens[i + 1]
            i += 2
            continue
        if tok.lower().startswith("http"):
            url = tok
        i += 1
    return url, headers, data


class CaptureError(ValueError):
    pass


def save_from_curl(text: str) -> dict:
    """Parse + persist credentials from a cURL. Returns a status summary.

    Raises CaptureError with a user-facing message on bad input.
    """
    if not text or "curl" not in text.lower():
        raise CaptureError("That doesn't look like a cURL command. Use "
                           "DevTools → Network → right-click the request → "
                           "Copy → Copy as cURL.")
    config.ensure_dirs()
    url, headers, data = parse_curl(text)
    headers = {k: v for k, v in headers.items()
               if not k.startswith(":") and k.lower() not in _DROP}
    hl = {k.lower(): v for k, v in headers.items()}
    auth = hl.get("costco-x-authorization") or hl.get("authorization")
    cid = hl.get("costco-x-wcs-clientid")
    if not auth or not cid:
        raise CaptureError(
            "This cURL is missing costco-x-authorization / costco-x-wcs-clientid. "
            "Copy the request to ecom-api.costco.com/.../graphql specifically "
            "(filter the Network tab by 'graphql').")

    config.API_HEADERS_FILE.write_text(json.dumps(headers, indent=2))
    token = auth.replace("Bearer ", "").strip()
    Credentials(id_token=token, client_id=cid)  # validate shape
    config.CRED_CACHE_FILE.write_text(
        json.dumps({"id_token": token, "client_id": cid,
                    "client_identifier": config.CLIENT_IDENTIFIER}, indent=2))

    has_query = False
    kind = "warehouse"
    if data and url:
        try:
            body = json.loads(data)
            has_query = isinstance(body, dict)
            # Route to the right template file by which query it is.
            query_text = str(body.get("query", "")) if has_query else ""
            if "getOnlineOrders" in query_text:
                kind = "online"
                target = config.API_REQUEST_ONLINE_FILE
            else:
                kind = "warehouse"
                target = config.API_REQUEST_FILE
            target.write_text(json.dumps({"url": url, "body": body}, indent=2))
        except ValueError:
            config.API_REQUEST_FILE.write_text(
                json.dumps({"url": url, "raw_body": data}, indent=2))

    exp = token_expiry(token)
    minutes = 0
    if exp:
        minutes = max(0, int((exp - datetime.datetime.now(datetime.timezone.utc))
                             .total_seconds()) // 60)
    return {
        "ok": True,
        "headers": len(headers),
        "has_query": has_query,
        "kind": kind,
        "token_minutes": minutes,
        "expired": token_is_expired(token),
        "url": url,
    }
