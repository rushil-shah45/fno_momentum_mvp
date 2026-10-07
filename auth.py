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
import struct
import time
from datetime import datetime, timedelta

import requests

from settings import ROOT, Settings
from strategy import IST

AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
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


def get_access_token(cfg: Settings, force: bool = False) -> str:
    """Return a usable access token, creating a new one only when needed."""
    if not auto_enabled(cfg):
        return cfg.access_token                      # manual mode

    cached = _read_cache(cfg.client_id)
    now = datetime.now(IST)
    if cached and not force and cached[1] - now > MIN_REMAINING:
        return cached[0]
    try:
        token, expiry = generate_token(cfg)
    except Exception:
        if cached and cached[1] > now and not force:   # new token failed, old one still works
            return cached[0]
        raise
    _write_cache(cfg.client_id, token, expiry)
    return token
