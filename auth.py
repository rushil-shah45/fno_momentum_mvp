"""Automatic access tokens for Dhan and ICICI Direct (no manual copy-paste).

- Dhan: client id + 6-digit PIN + TOTP code via official endpoint.
- ICICI Direct: Breeze API session token via automated Playwright login (user_id + password + TOTP)
  or manual token fallback.
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
import urllib.parse
from datetime import datetime, timedelta

import requests

from settings import ROOT, Settings
from strategy import IST

log = logging.getLogger("auth")

# Dhan endpoints & cache
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
PROFILE_URL = "https://api.dhan.co/v2/profile"
CACHE = ROOT / "data" / "token.json"

# ICICI endpoints & cache
ICICI_CACHE = ROOT / "data" / "icici_token.json"
ICICI_BASE_URL = "https://api.icicidirect.com/breezeapi/api/v1"

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


# ==============================================================================
# Dhan Authentication
# ==============================================================================
def dhan_auto_enabled(cfg: Settings) -> bool:
    return bool(cfg.pin and cfg.totp_secret)


def generate_dhan_token(cfg: Settings) -> tuple[str, datetime]:
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


# Backwards compatibility alias for Dhan tests
generate_token = generate_dhan_token


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


def login_dhan(cfg: Settings, pin: str | None = None, totp: str | None = None, client_id: str | None = None) -> tuple[str, datetime]:
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


def check_dhan_access(token: str, client_id: str = "") -> tuple[bool, int, str, dict]:
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


def get_dhan_access_token(cfg: Settings, force: bool = False) -> str:
    """Return a usable access token, creating a new one only when needed."""
    cached = _read_cache(cfg.client_id)
    now = datetime.now(IST)
    if cached and not force and cached[1] - now > MIN_REMAINING:
        return cached[0]

    if dhan_auto_enabled(cfg):
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


def ensure_dhan_login(cfg: Settings, interactive: bool = True, force_refresh: bool = False) -> tuple[str, datetime, dict]:
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
            if cached and dhan_auto_enabled(cfg) and (cached[1] - now < MIN_REMAINING):
                log.info("Token valid but expiring within %s, refreshing...", MIN_REMAINING)
                need_login = True
            else:
                return token, expiry, profile
        elif status_code == 401:
            log.warning("Dhan access check returned 401 Unauthorized (%s). Logging in again...", msg)
            need_login = True
        else:
            if cached and cached[1] > datetime.now(IST):
                return token, expiry, {}
            need_login = True

    if need_login:
        if dhan_auto_enabled(cfg):
            log.info("Authenticating with Dhan (PIN + TOTP)...")
            token, expiry = generate_token(cfg)
            _write_cache(cfg.client_id, token, expiry)
        elif interactive and sys.stdin and sys.stdin.isatty():
            log.info("Interactive login required...")
            token, expiry = login(cfg)
        else:
            raise RuntimeError("Dhan 401 Unauthorized: token expired or invalid, and automatic "
                               "credentials (DHAN_PIN/DHAN_TOTP_SECRET) are not configured.")

        ok, status_code, msg, profile = check_access(token, cfg.client_id)
        if not ok and status_code == 401:
            raise RuntimeError(f"Dhan login succeeded but access check returned 401 Unauthorized ({msg}).")
        return token, expiry, profile


# ==============================================================================
# ICICI Direct Authentication
# ==============================================================================
def icici_auto_enabled(cfg: Settings) -> bool:
    """True if ICICI User ID, Password, TOTP Secret and API Key are configured."""
    return bool(cfg.icici_api_key and cfg.icici_user_id and cfg.icici_password and cfg.icici_totp_secret)


def _read_icici_cache(api_key: str) -> tuple[str, datetime] | None:
    try:
        d = json.loads(ICICI_CACHE.read_text(encoding="utf-8"))
        if d.get("api_key") == api_key or not api_key:
            return d["session_token"], datetime.fromisoformat(d["expiry"]).replace(tzinfo=IST)
    except (OSError, ValueError, KeyError):
        pass
    return None


def _write_icici_cache(api_key: str, token: str, expiry: datetime) -> None:
    ICICI_CACHE.parent.mkdir(exist_ok=True)
    ICICI_CACHE.write_text(json.dumps({
        "api_key": api_key,
        "session_token": token,
        "expiry": expiry.isoformat()
    }), encoding="utf-8")
    try:
        ICICI_CACHE.chmod(0o600)
    except OSError:
        pass


def generate_icici_token_playwright(cfg: Settings, headless: bool = True, timeout_sec: int = 45) -> tuple[str, datetime]:
    """Automate ICICI Direct login with Playwright using user_id, password, and TOTP."""
    if not cfg.icici_api_key:
        raise ValueError("ICICI_API_KEY is required for ICICI Direct login.")
    if not cfg.icici_user_id or not cfg.icici_password:
        raise ValueError("ICICI_USER_ID and ICICI_PASSWORD are required for auto login.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed. Run `pip install playwright && playwright install chromium`.")

    encoded_key = urllib.parse.quote_plus(cfg.icici_api_key)
    login_url = f"https://api.icicidirect.com/apiuser/login?api_key={encoded_key}"
    captured_token = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        def handle_url(target_url: str):
            nonlocal captured_token
            if "apisession=" in target_url or "API_Session=" in target_url:
                parsed = urllib.parse.urlparse(target_url)
                params = urllib.parse.parse_qs(parsed.query)
                token = params.get("apisession") or params.get("API_Session")
                if token and token[0]:
                    captured_token = token[0]

        page.on("request", lambda req: handle_url(req.url))
        page.on("framenavigated", lambda frame: handle_url(frame.url))

        log.info("Navigating to ICICI Direct login page...")
        page.goto(login_url, timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        content = page.content()
        if "Public Key does not exist" in content:
            browser.close()
            raise RuntimeError("ICICI Direct API Key is invalid ('Public Key does not exist'). Check ICICI_API_KEY in .env.")

        # Fill User ID
        if page.query_selector("#txtuid"):
            page.fill("#txtuid", cfg.icici_user_id)
        else:
            user_selectors = [
                "input[name*='user' i]", "input[id*='user' i]", "input[placeholder*='user' i]",
                "input[name*='login' i]", "input[id*='login' i]", "input[type='text']"
            ]
            for sel in user_selectors:
                elem = page.query_selector(sel)
                if elem and elem.is_visible():
                    elem.fill(cfg.icici_user_id)
                    break

        # Fill Password
        if page.query_selector("#txtPass"):
            page.fill("#txtPass", cfg.icici_password)
        else:
            pwd_elem = page.query_selector("input[type='password']")
            if pwd_elem and pwd_elem.is_visible():
                pwd_elem.fill(cfg.icici_password)

        # Check Terms & Conditions checkbox if present
        chk = page.query_selector("#chkssTnc")
        if chk and chk.is_visible() and not chk.is_checked():
            chk.check()

        # Click Proceed / Submit
        if page.query_selector("#btnSubmit"):
            page.click("#btnSubmit")
        else:
            btn = page.query_selector("button[type='submit'], input[type='submit'], button:has-text('Proceed'), button:has-text('Login')")
            if btn and btn.is_visible():
                btn.click()

        time.sleep(2)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        # Handle TOTP input
        totp_code = None
        if cfg.icici_totp_secret:
            cleaned = cfg.icici_totp_secret.replace(" ", "").replace("-", "")
            if cleaned.isdigit() and len(cleaned) == 6:
                totp_code = cleaned
            else:
                totp_code = totp_now(cleaned)

        start_time = time.time()
        otp_submitted = False
        while time.time() - start_time < timeout_sec:
            if captured_token:
                break
            handle_url(page.url)

            if totp_code and not otp_submitted:
                visible_texts = [inp for inp in page.query_selector_all('input[type="text"]') if inp.is_visible()]
                if len(visible_texts) == 6:
                    for i in range(6):
                        visible_texts[i].fill(totp_code[i])
                    sub = page.query_selector("#Button1, input[type='button'][value='Submit'], input[type='submit'], button[type='submit']")
                    if sub and sub.is_visible():
                        sub.click()
                    otp_submitted = True
                elif len(visible_texts) == 1:
                    visible_texts[0].fill(totp_code)
                    sub = page.query_selector("#Button1, input[type='button'][value='Submit'], input[type='submit'], button[type='submit']")
                    if sub and sub.is_visible():
                        sub.click()
                    otp_submitted = True
            time.sleep(0.5)

        browser.close()

    if not captured_token:
        raise RuntimeError(
            f"Failed to capture ICICI session token automatically via Playwright. "
            f"Log in manually at {login_url} and set ICICI_SESSION_TOKEN in .env or run `python run.py --login`."
        )

    # Tokens expire daily at midnight
    expiry = datetime.now(IST).replace(hour=23, minute=59, second=59, microsecond=0)
    return captured_token, expiry


def check_icici_access(token: str, api_key: str = "", api_secret: str = "") -> tuple[bool, int, str, dict]:
    """Verify ICICI Direct Breeze API session."""
    if not token:
        return False, 401, "No session token provided", {}
    if not api_key or not api_secret:
        # If API key/secret not provided, assume token is unverified
        return True, 200, "Token format accepted (keys not verified)", {}

    try:
        from breeze_connect import BreezeConnect
        breeze = BreezeConnect(api_key=api_key)
        breeze.generate_session(api_secret=api_secret, session_token=token)
        user_id = getattr(breeze, "user_id", "")
        return True, 200, "Authenticated", {"user_id": user_id}
    except Exception as err:
        err_str = str(err)
        status = 401 if any(w in err_str.lower() for w in ("session", "key", "token", "unauthorized", "expired", "incorrect")) else 0
        return False, status, err_str, {}


def login_icici(cfg: Settings, session_token: str | None = None, user_id: str | None = None,
                password: str | None = None, totp: str | None = None, api_key: str | None = None,
                api_secret: str | None = None) -> tuple[str, datetime]:
    """Authenticate with ICICI Direct via Playwright or manual session token."""
    key = (api_key or cfg.icici_api_key).strip()
    secret = (api_secret or cfg.icici_api_secret).strip()
    uid = (user_id or cfg.icici_user_id).strip()
    pwd = (password or cfg.icici_password).strip()
    totp_val = (totp or cfg.icici_totp_secret).strip()

    # If session token explicitly given
    tok = (session_token or "").strip()
    if tok:
        expiry = datetime.now(IST).replace(hour=23, minute=59, second=59, microsecond=0)
        _write_icici_cache(key, tok, expiry)
        return tok, expiry

    # If auto credentials available, try Playwright
    if key and uid and pwd and totp_val:
        log.info("Attempting automated ICICI Direct login via Playwright...")
        try:
            tok, expiry = generate_icici_token_playwright(cfg, headless=True)
            _write_icici_cache(key, tok, expiry)
            return tok, expiry
        except Exception as e:
            log.warning("Automated Playwright login failed: %s", e)

    # Manual fallback prompt
    if sys.stdin and sys.stdin.isatty():
        encoded = urllib.parse.quote_plus(key) if key else "YOUR_API_KEY"
        login_url = f"https://api.icicidirect.com/apiuser/login?api_key={encoded}"
        print(f"\n1. Open this link in your browser to log in:")
        print(f"   {login_url}")
        print("2. Complete login with your User ID, Password, and TOTP.")
        print("3. From the redirected URL, copy the 'apisession' query parameter value.\n")
        tok = input("Enter ICICI session token (apisession): ").strip()
        if not tok:
            raise SystemExit("ICICI session token is required.")
        expiry = datetime.now(IST).replace(hour=23, minute=59, second=59, microsecond=0)
        _write_icici_cache(key, tok, expiry)
        return tok, expiry

    raise RuntimeError(
        "ICICI Direct login failed: automatic credentials not fully configured and non-interactive shell. "
        "Set ICICI_API_KEY, ICICI_API_SECRET, ICICI_USER_ID, ICICI_PASSWORD, ICICI_TOTP_SECRET or ICICI_SESSION_TOKEN in .env."
    )


def get_icici_session_token(cfg: Settings, force: bool = False) -> str:
    """Return a usable ICICI session token, generating one via Playwright if needed."""
    cached = _read_icici_cache(cfg.icici_api_key)
    now = datetime.now(IST)
    if cached and not force and cached[1] > now:
        return cached[0]

    if icici_auto_enabled(cfg):
        try:
            token, expiry = generate_icici_token_playwright(cfg)
            _write_icici_cache(cfg.icici_api_key, token, expiry)
            return token
        except Exception:
            if cached and cached[1] > now and not force:
                return cached[0]
            raise

    if cached and cached[1] > now and not force:
        return cached[0]

    return cfg.icici_session_token


def ensure_icici_login(cfg: Settings, interactive: bool = True, force_refresh: bool = False) -> tuple[str, datetime, dict]:
    cached = _read_icici_cache(cfg.icici_api_key)
    token = cached[0] if cached else cfg.icici_session_token
    expiry = cached[1] if cached else datetime.now(IST).replace(hour=23, minute=59, second=59, microsecond=0)

    need_login = force_refresh or not token

    if not need_login:
        ok, status_code, msg, profile = check_icici_access(token, cfg.icici_api_key, cfg.icici_api_secret)
        if ok:
            return token, expiry, profile
        elif status_code == 401:
            log.warning("ICICI session check failed (%s). Logging in again...", msg)
            need_login = True
        else:
            if cached and cached[1] > datetime.now(IST):
                return token, expiry, {}
            need_login = True

    if need_login:
        if icici_auto_enabled(cfg):
            log.info("Authenticating with ICICI Direct via Playwright...")
            token, expiry = generate_icici_token_playwright(cfg)
            _write_icici_cache(cfg.icici_api_key, token, expiry)
        elif interactive and sys.stdin and sys.stdin.isatty():
            token, expiry = login_icici(cfg)
        else:
            raise RuntimeError("ICICI Direct session expired or missing, and automated credentials are not configured in .env.")

        ok, status_code, msg, profile = check_icici_access(token, cfg.icici_api_key, cfg.icici_api_secret)
        if not ok and status_code == 401:
            raise RuntimeError(f"ICICI Direct login succeeded but session check failed ({msg}).")
        return token, expiry, profile


# ==============================================================================
# Unified Broker Dispatchers
# ==============================================================================
def auto_enabled(cfg: Settings) -> bool:
    if cfg and cfg.active_broker in ("icici", "icicidirect"):
        return icici_auto_enabled(cfg)
    return dhan_auto_enabled(cfg)


def get_access_token(cfg: Settings, force: bool = False) -> str:
    if cfg and cfg.active_broker in ("icici", "icicidirect"):
        return get_icici_session_token(cfg, force=force)
    return get_dhan_access_token(cfg, force=force)


def login(cfg: Settings, pin: str | None = None, totp: str | None = None, client_id: str | None = None,
          session_token: str | None = None, user_id: str | None = None, password: str | None = None,
          api_key: str | None = None, api_secret: str | None = None) -> tuple[str, datetime]:
    if cfg and cfg.active_broker in ("icici", "icicidirect"):
        return login_icici(cfg, session_token=session_token, user_id=user_id, password=password,
                           totp=totp, api_key=api_key, api_secret=api_secret)
    return login_dhan(cfg, pin=pin, totp=totp, client_id=client_id)


def check_access(token: str, client_id: str = "", api_key: str = "", api_secret: str = "", active_broker: str = "dhan") -> tuple[bool, int, str, dict]:
    if active_broker in ("icici", "icicidirect") or (api_key and api_secret):
        return check_icici_access(token, api_key=api_key or client_id, api_secret=api_secret)
    return check_dhan_access(token, client_id=client_id)


def ensure_login(cfg: Settings, interactive: bool = True, force_refresh: bool = False) -> tuple[str, datetime, dict]:
    if cfg and cfg.active_broker in ("icici", "icicidirect"):
        return ensure_icici_login(cfg, interactive=interactive, force_refresh=force_refresh)
    return ensure_dhan_login(cfg, interactive=interactive, force_refresh=force_refresh)
