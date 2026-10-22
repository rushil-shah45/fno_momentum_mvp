"""Automatic Dhan access tokens (no manual copy-paste).

Dhan access tokens last 24 hours. If TOTP is enabled on your Dhan account, an
official endpoint can mint a fresh token from  client id + 6-digit PIN + a TOTP
code  (docs: https://dhanhq.co/docs/v2/authentication/#generate-token).
We compute the TOTP code ourselves (RFC 6238, standard library only) from the
secret key Dhan shows when you set up the authenticator.

Order of preference:
  1. DHAN_PIN + DHAN_TOTP_SECRET in .env -> automatic, cached in data/token.json
  2. DHAN_ACCESS_TOKEN in .env           -> manual fallback (expires in 24h)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import struct
import sys
import time
from datetime import datetime, timedelta

import requests

from settings import ROOT, Settings
from strategy import IST

log = logging.getLogger("auth")

AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
PROFILE_URL = "https://api.dhan.co/v2/profile"
CACHE = ROOT / "data" / "token.json"
# Sessions run 09:00-15:00, so a token with less than 8h left is replaced up front.
MIN_REMAINING = timedelta(hours=8)


def totp_now(secret: str, at: float | None = None, digits: int = 6, step: int = 30) -> str:
    """Standard time-based one-time password (RFC 6238, SHA-1)."""
    s = secret.replace(" ", "").replace("-", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return f"{code:0{digits}d}"


def auto_enabled(cfg: Settings) -> bool:
    return bool(cfg.pin and cfg.totp_secret)


def generate_token(cfg: Settings) -> tuple[str, datetime]:
    """Ask Dhan for a new 24h token. Never put the PIN or code in an error message."""
    http = requests.Session()
    http.trust_env = False
    problem = "unknown error"
    for attempt in range(2):
        resp = http.post(AUTH_URL, timeout=30, params={
            "dhanClientId": cfg.client_id, "pin": cfg.pin,
            "totp": totp_now(cfg.totp_secret)})
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.ok and data.get("accessToken"):
            try:
                expiry = datetime.fromisoformat(str(data["expiryTime"])).replace(tzinfo=IST)
            except (KeyError, ValueError):
                expiry = datetime.now(IST) + timedelta(hours=23)
            return data["accessToken"], expiry
        problem = f"HTTP {resp.status_code} {data.get('message') or data.get('errorMessage') or ''}".strip()
        if attempt == 0:
            time.sleep(31)    # a TOTP code can't be reused; try again in the next 30s window
    raise RuntimeError(f"Dhan token generation failed ({problem}). "
                       "Check DHAN_PIN, DHAN_TOTP_SECRET and that your PC clock is accurate.")


def _read_cache(client_id: str) -> tuple[str, datetime] | None:
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if d["client_id"] == client_id:
            return d["access_token"], datetime.fromisoformat(d["expiry"])
    except (OSError, ValueError, KeyError):
        pass
    return None


def _write_cache(client_id: str, token: str, expiry: datetime) -> None:
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({"client_id": client_id, "access_token": token,
                                 "expiry": expiry.isoformat()}), encoding="utf-8")
    try:
        CACHE.chmod(0o600)      # best effort; ignored on Windows
    except OSError:
        pass


def login(cfg: Settings, pin: str | None = None, totp: str | None = None, client_id: str | None = None) -> tuple[str, datetime]:
    """Authenticate with Dhan using PIN and TOTP code/secret, and cache the token."""
    cid = (client_id or (cfg.client_id if cfg else "") or "").strip()
    if not cid:
        if sys.stdin and sys.stdin.isatty():
            cid = input("Enter Dhan Client ID: ").strip()
        else:
            raise SystemExit("Dhan Client ID is required.")
    if not cid:
        raise SystemExit("Dhan Client ID is required.")

    user_pin = (pin or (cfg.pin if cfg else "") or "").strip()
    if not user_pin:
        if sys.stdin and sys.stdin.isatty():
            import getpass
            user_pin = getpass.getpass("Enter 6-digit Dhan PIN: ").strip()
        else:
            raise SystemExit("Dhan PIN is required.")
    if not user_pin:
        raise SystemExit("Dhan PIN is required.")

    code_or_secret = (totp or (cfg.totp_secret if cfg else "") or "").strip()
    if not code_or_secret:
        if sys.stdin and sys.stdin.isatty():
            code_or_secret = input("Enter 6-digit TOTP code (from authenticator app) or TOTP secret: ").strip()
        else:
            raise SystemExit("TOTP code or secret is required.")
    if not code_or_secret:
        raise SystemExit("TOTP code or secret is required.")

    cleaned = code_or_secret.replace(" ", "").replace("-", "")
    if cleaned.isdigit() and len(cleaned) == 6:
        totp_code = cleaned
    else:
        totp_code = totp_now(cleaned)

    http = requests.Session()
    http.trust_env = False
    resp = http.post(AUTH_URL, timeout=30, params={
        "dhanClientId": cid,
        "pin": user_pin,
        "totp": totp_code,
    })
    try:
        data = resp.json()
    except ValueError:
        data = {}

    if resp.ok and data.get("accessToken"):
        try:
            expiry = datetime.fromisoformat(str(data["expiryTime"])).replace(tzinfo=IST)
        except (KeyError, ValueError):
            expiry = datetime.now(IST) + timedelta(hours=23)
        _write_cache(cid, data["accessToken"], expiry)
        return data["accessToken"], expiry

    problem = f"HTTP {resp.status_code} {data.get('message') or data.get('errorMessage') or ''}".strip()
    raise RuntimeError(f"Dhan login failed ({problem}).")


def check_access(token: str, client_id: str = "") -> tuple[bool, int, str, dict]:
    """Test token validity against Dhan's profile endpoint.
    Returns (ok, status_code, message, profile_dict)."""
    if not token:
        return False, 401, "No token provided", {}
    http = requests.Session()
    http.trust_env = False
    headers = {"access-token": token}
    if client_id:
        headers["client-id"] = client_id
    try:
        resp = http.get(PROFILE_URL, headers=headers, timeout=15)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code == 200:
            return True, 200, "Authenticated", data
        msg = data.get("errorMessage") or data.get("message") or f"HTTP {resp.status_code}"
        return False, resp.status_code, str(msg), data
    except Exception as err:
        return False, 0, str(err), {}


def ensure_login(cfg: Settings, interactive: bool = True, force_refresh: bool = False) -> tuple[str, datetime, dict]:
    """Verify that current Dhan login/token works. If 401 Unauthorized or expired,
    logs in again and caches the new token.
    Returns (token, expiry, profile_dict)."""
    if not cfg:
        raise ValueError("Settings object is required.")

    cached = _read_cache(cfg.client_id)
    token = cached[0] if cached else cfg.access_token
    expiry = cached[1] if cached else (datetime.now(IST) + timedelta(hours=23))

    need_login = force_refresh or not token

    if not need_login:
        ok, status_code, msg, profile = check_access(token, cfg.client_id)
        if ok:
            now = datetime.now(IST)
            if cached and auto_enabled(cfg) and (cached[1] - now < MIN_REMAINING):
                log.info("Token valid but expiring within %s, refreshing...", MIN_REMAINING)
                need_login = True
            else:
                return token, expiry, profile
        elif status_code == 401:
            log.warning("Dhan access check returned 401 Unauthorized (%s). Logging in again...", msg)
            need_login = True
        else:
            # Temporary network error or non-401: use cached token if locally unexpired
            if cached and cached[1] > datetime.now(IST):
                return token, expiry, {}
            need_login = True

    if need_login:
        if auto_enabled(cfg):
            log.info("Authenticating with Dhan (PIN + TOTP)...")
            token, expiry = generate_token(cfg)
            _write_cache(cfg.client_id, token, expiry)
        elif interactive and sys.stdin and sys.stdin.isatty():
            log.info("Interactive login required...")
            token, expiry = login(cfg)
        else:
            raise RuntimeError("Dhan 401 Unauthorized: token expired or invalid, and automatic "
                               "credentials (DHAN_PIN/DHAN_TOTP_SECRET) are not configured.")

        # Re-check the newly generated token
        ok, status_code, msg, profile = check_access(token, cfg.client_id)
        if not ok and status_code == 401:
            raise RuntimeError(f"Dhan login succeeded but access check returned 401 Unauthorized ({msg}).")
        return token, expiry, profile


def get_access_token(cfg: Settings, force: bool = False) -> str:
    """Return a usable access token, creating a new one only when needed."""
    cached = _read_cache(cfg.client_id)
    now = datetime.now(IST)
    if cached and not force and cached[1] - now > MIN_REMAINING:
        return cached[0]

    if auto_enabled(cfg):
        try:
            token, expiry = generate_token(cfg)
        except Exception:
            if cached and cached[1] > now and not force:   # new token failed, old one still works
                return cached[0]
            raise
        _write_cache(cfg.client_id, token, expiry)
        return token

    if cached and cached[1] > now and not force:
        return cached[0]

    return cfg.access_token                      # manual mode
