"""Web-UI authentication: fixed local accounts with password + TOTP MFA.

Deliberately dependency-free (stdlib only):
  - Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes.
  - MFA is RFC 6238 TOTP (HMAC-SHA1, 30s step, 6 digits) — the standard that
    Google Authenticator / 1Password / Authy implement.
  - Sessions are opaque random tokens held in server memory and handed to the
    browser as an HttpOnly cookie.

Passkeys (WebAuthn) are intentionally deferred — verifying them correctly needs
real crypto (CBOR/COSE, ES256) that isn't reasonable to hand-roll. Each account
already carries an empty ``passkeys`` list so credentials can be added later
without a data migration.

Each account carries a ``role`` — ``admin`` or ``operator``:
  - ``operator``  can view/search receipts only.
  - ``admin``     can do everything an operator can, plus run data jobs
                  (collect/import/reprocess) and manage user accounts.
Accounts predating roles (no ``role`` field) are treated as ``admin`` so existing
single-user installs keep full access. The very first account created is always an
admin, so the store never ends up with nobody who can manage it.

Accounts are managed from the CLI (``python -m costco_archiver auth …``) and, for
admins, from the web UI's Users panel; both go through the helpers below.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

from . import config

_PBKDF2_ITERATIONS = 200_000
_TOTP_DIGITS = 6
_TOTP_PERIOD = 30
_LOCK = threading.Lock()

# Access roles. 'admin' is a superset of 'operator'.
VALID_ROLES = ("admin", "operator")
DEFAULT_ROLE = "operator"


# --- password hashing ---------------------------------------------------------
def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> dict:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return {"algo": "pbkdf2_sha256", "iterations": iterations,
            "salt": salt.hex(), "hash": dk.hex()}


def verify_password(password: str, rec: dict) -> bool:
    if not rec or rec.get("algo") != "pbkdf2_sha256":
        return False
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(rec["salt"]), int(rec["iterations"]))
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(dk.hex(), rec.get("hash", ""))


# --- TOTP (RFC 6238) ----------------------------------------------------------
def new_totp_secret() -> str:
    """A fresh base32 secret (no padding), the format authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret: str, counter: int) -> str:
    # Re-pad the base32 secret to a multiple of 8 chars before decoding.
    pad = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + pad, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** _TOTP_DIGITS)).zfill(_TOTP_DIGITS)


def verify_totp(secret: str, code: str, *, window: int = 1, at: float | None = None) -> bool:
    """True if `code` matches the TOTP for `secret`, allowing ±`window` steps of
    clock skew. Comparison is constant-time."""
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit():
        return False
    counter = int((time.time() if at is None else at) // _TOTP_PERIOD)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_totp_at(secret, counter + drift), code):
            return True
    return False


def provisioning_uri(username: str, secret: str, issuer: str | None = None) -> str:
    """otpauth:// URI to paste/scan into an authenticator app."""
    issuer = issuer or config.AUTH_ISSUER
    label = quote(f"{issuer}:{username}")
    q = (f"secret={secret}&issuer={quote(issuer)}"
         f"&algorithm=SHA1&digits={_TOTP_DIGITS}&period={_TOTP_PERIOD}")
    return f"otpauth://totp/{label}?{q}"


# --- user store ---------------------------------------------------------------
def _load() -> dict:
    f = config.WEB_USERS_FILE
    if not f.exists():
        return {"users": {}}
    try:
        data = json.loads(f.read_text())
    except Exception:
        return {"users": {}}
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    return data


def _save(data: dict) -> None:
    config.ensure_dirs()
    f = config.WEB_USERS_FILE
    f.write_text(json.dumps(data, indent=2))
    try:  # best-effort: secrets file should not be world-readable
        os.chmod(f, 0o600)
    except OSError:
        pass


def users_exist() -> bool:
    return bool(_load().get("users"))


def list_users() -> list[str]:
    return sorted(_load().get("users", {}).keys())


def get_user(username: str) -> dict | None:
    return _load().get("users", {}).get(username)


def _role_of(rec: dict | None) -> str:
    """Role stored on a user record, defaulting legacy (role-less) users to admin."""
    role = str((rec or {}).get("role") or "").lower()
    return role if role in VALID_ROLES else "admin"


def get_role(username: str) -> str:
    """'admin' or 'operator' for a user, or '' if the user doesn't exist."""
    rec = get_user((username or "").strip())
    return _role_of(rec) if rec else ""


def is_admin(username: str) -> bool:
    return get_role(username) == "admin"


def _admins(data: dict) -> list[str]:
    return [name for name, rec in data.get("users", {}).items()
            if _role_of(rec) == "admin"]


def _is_last_admin(data: dict, username: str) -> bool:
    """True if `username` is the only admin left — used to block self-lockout."""
    return _admins(data) == [username]


def users_overview() -> list[dict]:
    """One dict per user (username, role, created_at, mfa) for management views."""
    data = _load()
    out = []
    for name in sorted(data.get("users", {})):
        rec = data["users"][name]
        out.append({"username": name, "role": _role_of(rec),
                    "created_at": rec.get("created_at"),
                    "mfa": bool(rec.get("totp_secret"))})
    return out


def add_user(username: str, password: str, role: str = DEFAULT_ROLE) -> str:
    """Create a user with a fresh TOTP secret. Returns the base32 secret.

    The first account ever created is forced to 'admin' so the store always has a
    manager."""
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")
    role = (role or DEFAULT_ROLE).lower()
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of: {', '.join(VALID_ROLES)}")
    with _LOCK:
        data = _load()
        if username in data["users"]:
            raise ValueError(f"user {username!r} already exists")
        if not data["users"]:
            role = "admin"  # bootstrap: never leave the store without an admin
        secret = new_totp_secret()
        data["users"][username] = {
            "password": hash_password(password),
            "totp_secret": secret,
            "role": role,
            "passkeys": [],  # reserved for future WebAuthn credentials
            "created_at": int(time.time()),
        }
        _save(data)
        return secret


def set_role(username: str, role: str) -> None:
    """Change a user's role. Refuses to demote the last remaining admin."""
    role = (role or "").lower()
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of: {', '.join(VALID_ROLES)}")
    with _LOCK:
        data = _load()
        if username not in data["users"]:
            raise ValueError(f"no such user: {username!r}")
        if role != "admin" and _is_last_admin(data, username):
            raise ValueError("cannot demote the last admin")
        data["users"][username]["role"] = role
        _save(data)


def set_password(username: str, password: str) -> None:
    with _LOCK:
        data = _load()
        if username not in data["users"]:
            raise ValueError(f"no such user: {username!r}")
        data["users"][username]["password"] = hash_password(password)
        _save(data)


def reset_totp(username: str) -> str:
    """Regenerate the user's TOTP secret. Returns the new base32 secret."""
    with _LOCK:
        data = _load()
        if username not in data["users"]:
            raise ValueError(f"no such user: {username!r}")
        secret = new_totp_secret()
        data["users"][username]["totp_secret"] = secret
        _save(data)
        return secret


def delete_user(username: str) -> None:
    with _LOCK:
        data = _load()
        if username not in data["users"]:
            raise ValueError(f"no such user: {username!r}")
        if _is_last_admin(data, username):
            raise ValueError("cannot delete the last admin")
        del data["users"][username]
        _save(data)


def authenticate(username: str, password: str, code: str) -> bool:
    """Verify password AND TOTP together. Constant-ish time; no early hints."""
    user = get_user((username or "").strip())
    pw_ok = verify_password(password or "", (user or {}).get("password", {}))
    otp_ok = verify_totp((user or {}).get("totp_secret", ""), code or "")
    return bool(user) and pw_ok and otp_ok


# --- sessions (in-memory) -----------------------------------------------------
@dataclass
class _Session:
    username: str
    expires: float


_SESSIONS: dict[str, _Session] = {}


def create_session(username: str, ttl: int | None = None) -> str:
    ttl = config.SESSION_TTL_SECONDS if ttl is None else ttl
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = _Session(username=username, expires=time.time() + ttl)
    return token


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    with _LOCK:
        s = _SESSIONS.get(token)
        if not s:
            return None
        if s.expires < time.time():
            del _SESSIONS[token]
            return None
        return s.username


def session_role(token: str | None) -> str:
    """Live role for a session's user ('' if none). Looked up fresh each call so a
    role change takes effect on the user's next request without re-login."""
    user = session_user(token)
    return get_role(user) if user else ""


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _LOCK:
        _SESSIONS.pop(token, None)
