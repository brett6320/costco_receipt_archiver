"""Interactive login + credential extraction via a real (headed) browser.

Costco's login involves 2FA/captcha and there is no public API, so we drive a
visible Chromium with a *persistent* profile. You log in once; the session is
reused on later runs. After login we read the `idToken` and `clientID` that the
web app stashes in localStorage — those authorize the GraphQL receipts API.

We also passively intercept the site's own GraphQL requests as a fallback, in
case the localStorage keys ever move.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from . import config


@dataclass
class Credentials:
    id_token: str
    client_id: str
    client_identifier: str = config.CLIENT_IDENTIFIER

    def headers(self) -> dict:
        """Headers Costco's web app sends on every receipts GraphQL call."""
        return {
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json, text/plain, */*",
            "Costco.Env": "ecom",
            "Costco.Service": "restOrders",
            "Costco-X-Wcs-Clientid": self.client_id,
            "Costco-X-Authorization": f"Bearer {self.id_token}",
            "Client-Identifier": self.client_identifier,
            "User-Agent": config.USER_AGENT,
            "Origin": "https://www.costco.com",
            "Referer": "https://www.costco.com/",
        }


# localStorage keys the web app is known to use, most-likely first.
_TOKEN_KEYS = ["idToken", "id_token", "accessToken", "access_token"]
_CLIENTID_KEYS = ["clientID", "clientId", "wcs-clientId", "WCS_CLIENT_ID"]


def _read_local_storage(page) -> dict:
    """Return the page's full localStorage as a dict."""
    return page.evaluate(
        "() => { const o = {}; for (let i = 0; i < localStorage.length; i++)"
        " { const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; }"
    )


def _extract_from_storage(store: dict) -> Optional[Credentials]:
    def pick(keys):
        for k in keys:
            v = store.get(k)
            if v:
                return v
        # some builds nest token inside a JSON blob
        for k, v in store.items():
            if not isinstance(v, str) or not v.startswith("{"):
                continue
            try:
                blob = json.loads(v)
            except ValueError:
                continue
            for want in keys:
                if isinstance(blob, dict) and blob.get(want):
                    return blob[want]
        return None

    token = pick(_TOKEN_KEYS)
    client_id = pick(_CLIENTID_KEYS)
    if token and client_id:
        return Credentials(id_token=token, client_id=client_id)
    return None


def login_and_get_credentials(
    timeout_seconds: int = 300, cred_cache: Optional[Path] = None
) -> Credentials:
    """Open a headed browser, let the user log in, and return API credentials.

    A network sniffer runs in parallel: if the site makes a GraphQL call we grab
    the real Authorization/clientId headers directly — the most reliable source.
    """
    config.ensure_dirs()
    captured: dict = {}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=False,
            user_agent=config.USER_AGENT,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if "graphql" in req.url and config.GRAPHQL_URL.split("/graphql")[0] in req.url:
                h = req.headers
                auth = h.get("costco-x-authorization") or h.get("authorization")
                cid = h.get("costco-x-wcs-clientid")
                if auth and cid and "captured" not in captured:
                    captured["captured"] = Credentials(
                        id_token=auth.replace("Bearer ", "").strip(),
                        client_id=cid,
                        client_identifier=h.get(
                            "client-identifier", config.CLIENT_IDENTIFIER
                        ),
                    )

        ctx.on("request", on_request)

        print("\n>>> A browser window has opened.")
        print(">>> Sign in to Costco.com (complete any 2FA), then return here.")
        page.goto(config.SIGNIN_URL, wait_until="domcontentloaded")

        creds = None
        import time

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if "captured" in captured:
                creds = captured["captured"]
                break
            try:
                store = _read_local_storage(page)
            except Exception:
                store = {}
            creds = _extract_from_storage(store)
            if creds:
                break
            time.sleep(2)

        # Nudge the app into making a receipts call so we can sniff live headers.
        if creds is None:
            try:
                page.goto(config.RECEIPTS_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
            except PWTimeout:
                pass
            creds = captured.get("captured") or _extract_from_storage(
                _read_local_storage(page)
            )

        ctx.close()

    if creds is None:
        raise RuntimeError(
            "Could not obtain credentials. Make sure you completed sign-in. "
            "If the site changed, inspect a graphql request's headers manually "
            "and set them via COSTCO_ID_TOKEN / COSTCO_CLIENT_ID env vars."
        )

    if cred_cache is not None:
        cred_cache.write_text(json.dumps(asdict(creds), indent=2))
    print(">>> Credentials acquired.\n")
    return creds
