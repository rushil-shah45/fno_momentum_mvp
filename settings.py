"""Configuration: reads .env (and environment variables) into one object."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                val = value.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                values[key.strip()] = val
    return values


@dataclass(frozen=True)
class Settings:
    active_broker: str = "indmoney"     # 'indmoney' or 'icici'
    trading_mode: str = "paper"         # 'paper' (default) or 'live'

    # INDMoney settings
    indmoney_client_id: str = ""        # x-api-key header (Client ID from INDstocks TOTP setup)
    indmoney_mpin: str = ""             # Account trading MPIN
    indmoney_totp_secret: str = ""      # TOTP secret key for automatic 24h token renewal
    indmoney_access_token: str = ""     # manual fallback: 24h access token from web dashboard

    # ICICI Direct settings
    icici_api_key: str = ""
    icici_api_secret: str = ""
    icici_user_id: str = ""
    icici_password: str = ""
    icici_totp_secret: str = ""
    icici_session_token: str = ""   # manual fallback

    # Risk controls
    max_risk_per_trade: float = 250.0
    max_daily_loss: float = 500.0
    force_exit: time = time(15, 0)
    top_n: int = 5
    enforce_risk_limit: bool = True
    dashboard_enabled: bool = True   # daemon also serves the dashboard
    dashboard_port: int = 8501

    # Strategy constants (see README for the reasoning behind each one)
    tolerance: float = 0.05         # "Open = Low" allows one NSE tick of slack
    close_zone: float = 0.20        # 5-min close must be in top/bottom 20% of range
    max_range_pct: float = 3.0      # skip stocks whose 5-min range is > 3%
    min_days_to_expiry: int = 3     # avoid contracts about to expire


def load_settings() -> Settings:
    env = {**_read_env_file(ROOT / ".env"), **os.environ}
    hh, mm = env.get("FORCE_EXIT", "15:00").split(":")
    active_broker = env.get("ACTIVE_BROKER", env.get("BROKER", "indmoney")).strip().lower()
    raw_mode = env.get("TRADING_MODE", env.get("MODE", "paper")).strip().lower()
    trading_mode = "live" if raw_mode in ("live", "real") else "paper"
    return Settings(
        active_broker=active_broker,
        trading_mode=trading_mode,
        indmoney_client_id=env.get("INDMONEY_CLIENT_ID", ""),
        indmoney_mpin=env.get("INDMONEY_MPIN", env.get("INDMONEY_PIN", "")),
        indmoney_totp_secret=env.get("INDMONEY_TOTP_SECRET", ""),
        indmoney_access_token=env.get("INDMONEY_ACCESS_TOKEN", ""),
        icici_api_key=env.get("ICICI_API_KEY", ""),
        icici_api_secret=env.get("ICICI_API_SECRET", ""),
        icici_user_id=env.get("ICICI_USER_ID", ""),
        icici_password=env.get("ICICI_PASSWORD", ""),
        icici_totp_secret=env.get("ICICI_TOTP_SECRET", ""),
        icici_session_token=env.get("ICICI_SESSION_TOKEN", ""),
        max_risk_per_trade=float(env.get("MAX_RISK_PER_TRADE", 250)),
        max_daily_loss=float(env.get("MAX_DAILY_LOSS", 500)),
        force_exit=time(int(hh), int(mm)),
        top_n=int(env.get("TOP_N", 5)),
        enforce_risk_limit=env.get("ENFORCE_RISK_LIMIT", "true").lower() == "true",
        dashboard_enabled=env.get("DASHBOARD", "true").lower() == "true",
        dashboard_port=int(env.get("DASHBOARD_PORT", 8501)),
    )
