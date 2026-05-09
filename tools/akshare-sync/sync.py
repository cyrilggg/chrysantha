#!/usr/bin/env python3
"""
Chrysantha — AkShare → Ghostfolio A-Share Market Data Sync

Zero-config for symbols: auto-discovers A-share stocks from Ghostfolio
or reads CHRYSANTHA_SYMBOLS env var.

Environment variables (all optional if using config.yaml):
    GHOSTFOLIO_ACCESS_TOKEN   Ghostfolio access token (Settings → Access Token)
    GHOSTFOLIO_BASE_URL       Ghostfolio API base URL (default: http://ghostfolio:3333/api/v1)
    CHRYSANTHA_SYMBOLS        Comma-separated stock codes: SH600519,SZ002594
                              If not set, auto-discovers from Ghostfolio market data

Usage:
    python sync.py                  # Run once (auto-discover or use env var)
    python sync.py --daemon         # Run on schedule
    python sync.py --symbol SH600519  # Sync single symbol for testing
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional YAML config for advanced settings
try:
    import yaml

    CONFIG_PATH = Path(
        os.environ.get("CONFIG_PATH", Path(__file__).parent / "config.yaml")
    )
    HAS_YAML = CONFIG_PATH.exists()
except ImportError:
    HAS_YAML = False

# ── Constants ──────────────────────────────────────────────────

TRADING_START_HOUR = 9
TRADING_END_HOUR = 15

# A-share symbol pattern: SH + 6 digits (Shanghai) or SZ + 6 digits (Shenzhen)
A_SHARE_PATTERN = re.compile(r"^(SH|SZ)\d{6}$", re.IGNORECASE)


def load_config() -> dict:
    """Load configuration from env vars, falling back to YAML."""
    config = {
        "ghostfolio": {
            "base_url": os.environ.get(
                "GHOSTFOLIO_BASE_URL", "http://ghostfolio:3333/api/v1"
            ),
            "access_token": os.environ.get("GHOSTFOLIO_ACCESS_TOKEN", ""),
        },
        "symbols": [],
        "schedule": {"interval_minutes": 30},
        "logging": {"level": "INFO", "file": "/var/log/akshare-sync.log"},
    }

    # Overlay YAML config if available
    if HAS_YAML:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = os.path.expandvars(f.read())
            yaml_config = yaml.safe_load(raw) or {}
            # Deep merge (shallow for simplicity)
            for key in ["ghostfolio", "schedule", "logging"]:
                if key in yaml_config and yaml_config[key]:
                    if isinstance(config[key], dict):
                        config[key].update(yaml_config[key])
                    else:
                        config[key] = yaml_config[key]
        except Exception:
            pass

    # Parse CHRYSANTHA_SYMBOLS env var (comma-separated: SH600519,SZ002594)
    env_symbols = os.environ.get("CHRYSANTHA_SYMBOLS", "")
    if env_symbols:
        for s in env_symbols.split(","):
            s = s.strip()
            if A_SHARE_PATTERN.match(s):
                market = "sh" if s.upper().startswith("SH") else "sz"
                code = s[2:]  # Remove SH/SZ prefix
                config["symbols"].append(
                    {"symbol": s.upper(), "akshare_code": code, "market": market}
                )

    # Deduplicate symbols
    seen = set()
    unique = []
    for s in config["symbols"]:
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            unique.append(s)
    config["symbols"] = unique

    # Validate
    if not config["ghostfolio"]["access_token"]:
        raise ValueError(
            "GHOSTFOLIO_ACCESS_TOKEN is not set. "
            "Set it via environment variable or in config.yaml.\n"
            "Get your token: Ghostfolio → Settings → Access Token → Create"
        )

    return config


# ── Logging ────────────────────────────────────────────────────


def setup_logging(config: dict) -> logging.Logger:
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    logger = logging.getLogger("chrysantha-sync")
    logger.setLevel(level)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console)
    log_file = log_config.get("file")
    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
                )
            )
            logger.addHandler(fh)
        except Exception:
            pass
    return logger


# ── Ghostfolio API Client ──────────────────────────────────────


class GhostfolioClient:
    def __init__(self, base_url: str, access_token: str, logger: logging.Logger):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.logger = logger
        self.jwt_token: Optional[str] = None
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def authenticate(self) -> bool:
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/anonymous",
                json={"accessToken": self.access_token},
                timeout=10,
            )
            resp.raise_for_status()
            self.jwt_token = resp.json().get("authToken")
            self.logger.info("Ghostfolio authenticated")
            return True
        except requests.RequestException as e:
            self.logger.error(f"Auth failed: {e}")
            return False

    def _headers(self):
        return {"Authorization": f"Bearer {self.jwt_token}", "Content-Type": "application/json"}

    def discover_a_share_symbols(self) -> List[dict]:
        """
        Auto-discover A-share symbols from Ghostfolio.
        Queries the manual-a-shares endpoint (no admin required).
        """
        if not self.jwt_token and not self.authenticate():
            return []

        try:
            resp = self.session.get(
                f"{self.base_url}/symbol/manual-a-shares",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()

            data = resp.json()
            symbols = []
            seen = set()

            for item in data:
                symbol = item.get("symbol", "")
                if A_SHARE_PATTERN.match(symbol) and symbol not in seen:
                    seen.add(symbol)
                    market = "sh" if symbol.upper().startswith("SH") else "sz"
                    code = symbol[2:]
                    symbols.append(
                        {"symbol": symbol.upper(), "akshare_code": code, "market": market}
                    )

            self.logger.info(f"Auto-discovered {len(symbols)} A-share symbols")
            return symbols
        except requests.RequestException as e:
            self.logger.warning(f"Symbol discovery failed: {e}")
            return []

    def update_market_price(
        self, symbol: str, price: float, date_str: Optional[str] = None
    ) -> bool:
        if not self.jwt_token and not self.authenticate():
            return False
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        payload = {"marketData": [{"date": date_str, "marketPrice": price}]}

        def do_post():
            return self.session.post(
                f"{self.base_url}/market-data/MANUAL/{symbol}",
                json=payload,
                headers=self._headers(),
                timeout=15,
            )

        try:
            resp = do_post()
            if resp.status_code == 401:
                self.logger.warning("JWT expired, re-authenticating...")
                if self.authenticate():
                    resp = do_post()
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            self.logger.error(f"Push failed for {symbol}: {e}")
            return False


# ── AkShare Fetcher ────────────────────────────────────────────


class AkShareFetcher:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._ak = None
        self._cache = None
        self._cache_time = 0

    def _ensure_akshare(self):
        if self._ak is None:
            import akshare as ak
            self._ak = ak
            self.logger.info("AkShare loaded")

    def _get_spot_data(self):
        """Fetch all A-share spot data with caching (5s TTL)."""
        self._ensure_akshare()
        now = time.time()
        if self._cache is not None and (now - self._cache_time) < 5:
            return self._cache
        try:
            self._cache = self._ak.stock_zh_a_spot_em()
            self._cache_time = now
        except Exception as e:
            self.logger.error(f"AkShare fetch error: {e}")
        return self._cache

    def fetch_price(self, code: str, market: str) -> Optional[float]:
        df = self._get_spot_data()
        if df is None:
            return None
        try:
            row = df[df["代码"] == code]
            if row.empty:
                self.logger.warning(f"Symbol {market}{code} not found in AkShare")
                return None
            price = float(row["最新价"].iloc[0])
            name = row["名称"].iloc[0]
            self.logger.info(f"  {market.upper()}{code} ({name}): ¥{price}")
            return price
        except Exception as e:
            self.logger.error(f"Parse error for {market}{code}: {e}")
            return None


# ── Sync Engine ────────────────────────────────────────────────


class SyncEngine:
    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.client = GhostfolioClient(
            base_url=config["ghostfolio"]["base_url"],
            access_token=config["ghostfolio"]["access_token"],
            logger=logger,
        )
        self.fetcher = AkShareFetcher(logger=logger)

    def get_symbols(self) -> List[dict]:
        """Get symbols: configured ones + auto-discovered ones."""
        configured = list(self.config.get("symbols", []))

        # Auto-discover if no symbols configured
        if not configured:
            self.logger.info("No symbols in env, auto-discovering from Ghostfolio...")
            discovered = self.client.discover_a_share_symbols()
            if discovered:
                return discovered
            self.logger.warning(
                "No A-share symbols found. Set CHRYSANTHA_SYMBOLS=SH600519,SZ002594 in .env"
            )

        return configured

    def sync_all(self) -> dict:
        symbols = self.get_symbols()
        if not symbols:
            return {"success": 0, "failure": 0, "details": [], "error": "no_symbols"}

        if not self.client.authenticate():
            return {"success": 0, "failure": len(symbols), "details": [], "error": "auth_failed"}

        results = {"success": 0, "failure": 0, "details": []}

        for item in symbols:
            symbol = item["symbol"]
            code = item["akshare_code"]
            market = item.get("market", "sh")

            self.logger.info(f"Syncing {symbol}...")
            price = self.fetcher.fetch_price(code, market)

            if price is None:
                results["failure"] += 1
                results["details"].append({"symbol": symbol, "status": "fetch_failed"})
                continue

            if self.client.update_market_price(symbol, price):
                results["success"] += 1
                results["details"].append({"symbol": symbol, "status": "ok", "price": price})
            else:
                results["failure"] += 1
                results["details"].append({"symbol": symbol, "status": "push_failed", "price": price})

            time.sleep(0.3)

        return results


# ── Daemon ─────────────────────────────────────────────────────


def is_trading_hours() -> bool:
    now = datetime.now(timezone.utc)
    cst_hour = (now.hour + 8) % 24
    return now.weekday() < 5 and TRADING_START_HOUR <= cst_hour < TRADING_END_HOUR


def run_daemon(config: dict, logger: logging.Logger):
    try:
        import schedule as schedule_lib
    except ImportError:
        logger.error("pip install schedule")
        sys.exit(1)

    engine = SyncEngine(config, logger)

    def job():
        if is_trading_hours():
            logger.info("Trading hours — running sync...")
            r = engine.sync_all()
            logger.info(f"Done: {r['success']} ok, {r['failure']} fail")
        else:
            logger.debug("Outside trading hours, skip")

    interval = config.get("schedule", {}).get("interval_minutes", 30)
    schedule_lib.every(interval).minutes.do(job)
    logger.info(f"Daemon started (every {interval} min). First run...")
    job()

    while True:
        schedule_lib.run_pending()
        time.sleep(30)


# ── CLI ────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Chrysantha — A-Share Sync for Ghostfolio")
    parser.add_argument("--daemon", action="store_true", help="Run on schedule")
    parser.add_argument("--symbol", type=str, help="Sync single symbol (e.g. SH600519)")
    args = parser.parse_args()

    try:
        config = load_config()
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    logger = setup_logging(config)
    logger.info("Chrysantha Sync Engine v2.0")
    logger.info(f"Ghostfolio: {config['ghostfolio']['base_url']}")

    if args.symbol:
        engine = SyncEngine(config, logger)
        code = args.symbol[2:] if len(args.symbol) > 2 else args.symbol
        market = "sh" if args.symbol.upper().startswith("SH") else "sz"
        price = engine.fetcher.fetch_price(code, market)
        if price and engine.client.authenticate():
            ok = engine.client.update_market_price(args.symbol.upper(), price)
            logger.info(f"{'✓' if ok else '✗'} {args.symbol}: ¥{price}")
        sys.exit(0 if price else 1)

    if args.daemon:
        run_daemon(config, logger)
    else:
        engine = SyncEngine(config, logger)
        results = engine.sync_all()
        logger.info(f"Sync done: {results['success']} ok, {results['failure']} fail")
        for d in results["details"]:
            logger.info(f"  {d['symbol']}: {d['status']} @ ¥{d.get('price', 'N/A')}")
        if results.get("error"):
            logger.error(f"Hint: set CHRYSANTHA_SYMBOLS=SH600519,SZ002594 in .env")
        sys.exit(1 if results["failure"] > 0 else 0)


if __name__ == "__main__":
    main()
