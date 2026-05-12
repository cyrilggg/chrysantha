"""
Vibe-Trading data adapter for screener-bridge.

Wraps Vibe-Trading's AKShare and YFinance loaders to provide simple
functions for fetching market snapshots and stock universes.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path setup -- add Vibe-Trading's agent/ directory to sys.path
# ---------------------------------------------------------------------------
_VT_PATH = Path(os.environ.get("VIBE_TRADING_PATH", "/mnt/d/Project/Vibe-Trading"))
_AGENT_PATH = str(_VT_PATH / "agent")
if _AGENT_PATH not in sys.path:
    sys.path.insert(0, _AGENT_PATH)

# Lazy loader references
_akshare_loader = None
_yfinance_loader = None

# ---------------------------------------------------------------------------
# Simple in-memory cache for stock universes (1-hour TTL)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 3600  # seconds


def _cache_get(key: str) -> Optional[list[dict]]:
    ts, data = _cache.get(key, (0, []))
    if time.time() - ts < _CACHE_TTL:
        return data
    _cache.pop(key, None)
    return None


def _cache_set(key: str, data: list[dict]) -> None:
    _cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# Loader instantiation helpers
# ---------------------------------------------------------------------------

def _get_akshare_loader():
    """Lazily import and instantiate the AKShare DataLoader via registry."""
    global _akshare_loader
    if _akshare_loader is not None:
        return _akshare_loader
    try:
        from backtest.loaders.registry import resolve_loader
        _akshare_loader = resolve_loader("a_share")
        logger.info("AKShare loader initialized: %s", type(_akshare_loader).__name__)
        return _akshare_loader
    except Exception as exc:
        raise ImportError(
            f"Failed to initialize AKShare loader from Vibe-Trading. "
            f"Check VIBE_TRADING_PATH ({_VT_PATH}) and that akshare is installed. "
            f"Error: {exc}"
        ) from exc


def _get_yfinance_loader():
    """Lazily import and instantiate the YFinance DataLoader via registry."""
    global _yfinance_loader
    if _yfinance_loader is not None:
        return _yfinance_loader
    try:
        from backtest.loaders.registry import resolve_loader
        _yfinance_loader = resolve_loader("us_equity")
        logger.info("YFinance loader initialized: %s", type(_yfinance_loader).__name__)
        return _yfinance_loader
    except Exception as exc:
        raise ImportError(
            f"Failed to initialize YFinance loader from Vibe-Trading. "
            f"Check VIBE_TRADING_PATH ({_VT_PATH}) and that yfinance is installed. "
            f"Error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Data fetching functions
# ---------------------------------------------------------------------------

def fetch_cn_daily(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch A-share daily data via Vibe-Trading's AKShare loader.

    Args:
        symbols: A-share symbols like ``["000001.SZ", "600519.SH"]``.
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns:
        DataFrame with columns: symbol, date, open, high, low, close, volume.
        Returns empty DataFrame on failure.
    """
    try:
        loader = _get_akshare_loader()
        result = loader.fetch(symbols, start_date, end_date, interval="1D")
        if not result:
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for sym, df in result.items():
            df = df.reset_index()
            df["symbol"] = sym
            # Ensure consistent column naming
            col_map = {"trade_date": "date"}
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        # Define standard column order
        wanted = ["symbol", "date", "open", "high", "low", "close", "volume"]
        cols = [c for c in wanted if c in combined.columns]
        return combined[cols]
    except ImportError as exc:
        logger.warning("fetch_cn_daily: %s", exc)
        return pd.DataFrame()
    except Exception as exc:
        logger.warning("fetch_cn_daily failed for %d symbols: %s", len(symbols), exc)
        return pd.DataFrame()


def fetch_hk_daily(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch HK stock daily data. Falls back from futu to AKShare to yfinance.

    Args:
        symbols: HK symbols like ``["00700.HK", "09988.HK"]``.
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns:
        DataFrame with columns: symbol, date, open, high, low, close, volume.
    """
    try:
        from backtest.loaders.registry import resolve_loader, NoAvailableSourceError

        loader = None
        for chain_name in ["futu", "akshare", "yfinance"]:
            try:
                loader = resolve_loader("hk_equity")
                # resolve_loader returns the first available; test it
                test = loader.fetch([symbols[0]], start_date, end_date, interval="1D")
                if test:
                    break
            except (NoAvailableSourceError, Exception):
                continue

        if loader is None:
            logger.warning("fetch_hk_daily: no available loader for HK equity")
            return pd.DataFrame()

        result = loader.fetch(symbols, start_date, end_date, interval="1D")
        if not result:
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for sym, df in result.items():
            df = df.reset_index()
            df["symbol"] = sym
            df = df.rename(columns={"trade_date": "date"})
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        wanted = ["symbol", "date", "open", "high", "low", "close", "volume"]
        cols = [c for c in wanted if c in combined.columns]
        return combined[cols]
    except ImportError as exc:
        logger.warning("fetch_hk_daily: %s", exc)
        return pd.DataFrame()
    except Exception as exc:
        logger.warning("fetch_hk_daily failed for %d symbols: %s", len(symbols), exc)
        return pd.DataFrame()


def fetch_us_daily(symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch US stock daily data via yfinance loader.

    Args:
        symbols: US symbols like ``["AAPL.US", "MSFT.US"]``.
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns:
        DataFrame with columns: symbol, date, open, high, low, close, volume.
    """
    try:
        loader = _get_yfinance_loader()
        result = loader.fetch(symbols, start_date, end_date, interval="1D")
        if not result:
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for sym, df in result.items():
            df = df.reset_index()
            df["symbol"] = sym
            df = df.rename(columns={"trade_date": "date"})
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        wanted = ["symbol", "date", "open", "high", "low", "close", "volume"]
        cols = [c for c in wanted if c in combined.columns]
        return combined[cols]
    except ImportError as exc:
        logger.warning("fetch_us_daily: %s", exc)
        return pd.DataFrame()
    except Exception as exc:
        logger.warning("fetch_us_daily failed for %d symbols: %s", len(symbols), exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Stock universe functions
# ---------------------------------------------------------------------------

def get_cn_stock_universe() -> list[dict]:
    """Get all A-share stocks via AKShare stock_info_a_code_name().

    Returns:
        List of dicts: ``[{symbol, name}, ...]``.
        Excludes ST/*ST/退市 stocks.
    """
    cached = _cache_get("cn_universe")
    if cached is not None:
        return cached

    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            logger.warning("get_cn_stock_universe: akshare returned empty data")
            return []

        results: list[dict] = []
        for _, row in df.iterrows():
            code = str(row.get("code", ""))
            name = str(row.get("name", ""))
            # Exclude ST, *ST, and 退市 stocks
            if "ST" in name or "退市" in name:
                continue
            # Guess exchange suffix from code prefix
            if code.startswith(("60", "68")):
                symbol = f"{code}.SH"
            elif code.startswith(("00", "002", "003", "30")):
                symbol = f"{code}.SZ"
            elif code.startswith(("8", "4")):
                symbol = f"{code}.BJ"
            else:
                symbol = f"{code}.SZ"  # default
            results.append({"symbol": symbol, "name": name})

        _cache_set("cn_universe", results)
        logger.info("get_cn_stock_universe: %d stocks loaded", len(results))
        return results
    except ImportError as exc:
        logger.warning("get_cn_stock_universe: akshare not installed: %s", exc)
        return []
    except Exception as exc:
        logger.warning("get_cn_stock_universe failed: %s", exc)
        return []


def get_hk_stock_universe() -> list[dict]:
    """Get all HK stocks via AKShare stock_hk_spot().

    Returns:
        List of dicts: ``[{symbol, name}, ...]``.
    """
    cached = _cache_get("hk_universe")
    if cached is not None:
        return cached

    try:
        import akshare as ak
        df = ak.stock_hk_spot()
        if df is None or df.empty:
            logger.warning("get_hk_stock_universe: akshare returned empty data")
            return []

        results: list[dict] = []
        for _, row in df.iterrows():
            code = str(row.get("代码", row.get("code", "")))
            name = str(row.get("名称", row.get("name", "")))
            if not code or not name:
                continue
            # Strip any leading zeros for display but keep original for symbol
            symbol = f"{code}.HK"
            results.append({"symbol": symbol, "name": name})

        _cache_set("hk_universe", results)
        logger.info("get_hk_stock_universe: %d stocks loaded", len(results))
        return results
    except ImportError as exc:
        logger.warning("get_hk_stock_universe: akshare not installed: %s", exc)
        return []
    except Exception as exc:
        logger.warning("get_hk_stock_universe failed: %s", exc)
        return []


def get_us_stock_universe(sector_etfs: Optional[list[str]] = None) -> list[dict]:
    """Get US stocks by sector ETF constituents.

    Args:
        sector_etfs: List of sector ETF tickers. Defaults to
            ``["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLB", "XLRE", "XLU"]``.

    Returns:
        List of dicts: ``[{symbol, name}, ...]``.
    """
    if sector_etfs is None:
        sector_etfs = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLB", "XLRE", "XLU"]

    cache_key = f"us_universe:{','.join(sorted(sector_etfs))}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        import akshare as ak
        results: list[dict] = []
        seen: set[str] = set()

        for etf in sector_etfs:
            try:
                df = ak.index_stock_cons(symbol=etf)
                if df is None or df.empty:
                    continue
                # Column name varies by AKShare version
                code_col = None
                name_col = None
                for col in df.columns:
                    col_lower = str(col).lower()
                    if "code" in col_lower or "代码" in col:
                        code_col = col
                    if "name" in col_lower or "名称" in col:
                        name_col = col

                if code_col is None:
                    # fallback: use first column
                    code_col = df.columns[0]
                    name_col = df.columns[1] if len(df.columns) > 1 else code_col

                for _, row in df.iterrows():
                    code = str(row.get(code_col, "")).strip()
                    name = str(row.get(name_col, "")).strip()
                    if not code or code in seen:
                        continue
                    seen.add(code)
                    symbol = f"{code}.US"
                    results.append({"symbol": symbol, "name": name, "sector_etf": etf})
            except Exception as exc:
                logger.debug("get_us_stock_universe: failed for ETF %s: %s", etf, exc)
                continue

        _cache_set(cache_key, results)
        logger.info("get_us_stock_universe: %d stocks loaded from %d ETFs", len(results), len(sector_etfs))
        return results
    except ImportError as exc:
        logger.warning("get_us_stock_universe: akshare not installed: %s", exc)
        return []
    except Exception as exc:
        logger.warning("get_us_stock_universe failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Sector mapping
# ---------------------------------------------------------------------------

def get_sector_mapping(market: str) -> dict[str, str]:
    """Get ``{symbol: sector_name}`` mapping for a market.

    Args:
        market: Market identifier -- ``"cn"``, ``"hk"``, or ``"us"``.

    Returns:
        Mapping of symbol to sector name string. For CN market, uses
        申万行业 classification via AKShare.
    """
    cached = _cache_get(f"sector_map:{market}")
    if cached is not None:
        return {item["symbol"]: item["name"] for item in cached}

    try:
        if market == "cn":
            mapping = _get_cn_sector_mapping()
        elif market == "hk":
            mapping = _get_hk_sector_mapping()
        elif market == "us":
            mapping = _get_us_sector_mapping()
        else:
            logger.warning("get_sector_mapping: unknown market %r", market)
            return {}

        # Cache the mapping items as list[dict] format for reuse
        _cache_set(f"sector_map:{market}", [{"symbol": k, "name": v} for k, v in mapping.items()])
        return mapping
    except ImportError as exc:
        logger.warning("get_sector_mapping(%s): %s", market, exc)
        return {}
    except Exception as exc:
        logger.warning("get_sector_mapping(%s) failed: %s", market, exc)
        return {}


def _get_cn_sector_mapping() -> dict[str, str]:
    """Build CN sector mapping using 申万行业 classification."""
    import akshare as ak

    mapping: dict[str, str] = {}
    try:
        # Use AKShare's 申万行业分类
        df = ak.stock_board_industry_hist_em()
        if df is None or df.empty:
            return mapping

        # For each industry board, get its constituent stocks
        for _, row in df.head(100).iterrows():  # limit to avoid excessive API calls
            board_name = str(row.get("板块名称", row.get("board_name", "")))
            if not board_name:
                continue
            try:
                cons_df = ak.stock_board_industry_cons_em(symbol=board_name)
                if cons_df is None or cons_df.empty:
                    continue
                for _, stock in cons_df.iterrows():
                    code = str(stock.get("代码", stock.get("code", "")))
                    if code:
                        # Guess suffix from code prefix
                        if code.startswith(("60", "68")):
                            symbol = f"{code}.SH"
                        elif code.startswith(("00", "002", "003", "30")):
                            symbol = f"{code}.SZ"
                        elif code.startswith(("8", "4")):
                            symbol = f"{code}.BJ"
                        else:
                            symbol = f"{code}.SZ"
                        mapping[symbol] = board_name
            except Exception:
                continue
    except Exception:
        pass

    # Fallback: try stock_board_industry_name_em
    if not mapping:
        try:
            df = ak.stock_board_industry_name_em()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row.get("代码", row.get("code", "")))
                    sector = str(row.get("板块", row.get("sector", row.get("板块名称", ""))))
                    if code and sector:
                        if code.startswith(("60", "68")):
                            symbol = f"{code}.SH"
                        elif code.startswith(("00", "002", "003", "30")):
                            symbol = f"{code}.SZ"
                        elif code.startswith(("8", "4")):
                            symbol = f"{code}.BJ"
                        else:
                            symbol = f"{code}.SZ"
                        mapping[symbol] = sector
        except Exception:
            pass

    return mapping


def _get_hk_sector_mapping() -> dict[str, str]:
    """Build HK sector mapping from stock_hk_spot data."""
    import akshare as ak

    mapping: dict[str, str] = {}
    try:
        df = ak.stock_hk_spot()
        if df is None or df.empty:
            return mapping
        for _, row in df.iterrows():
            code = str(row.get("代码", row.get("code", "")))
            sector = str(row.get("行业", row.get("industry", row.get("sector", ""))))
            if code and sector and sector not in ("nan", "", "None"):
                mapping[f"{code}.HK"] = sector
    except Exception:
        pass
    return mapping


def _get_us_sector_mapping() -> dict[str, str]:
    """Build US sector mapping from ETF constituents."""
    default_etfs = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLB", "XLRE", "XLU"]
    # Map ETF ticker to sector name
    etf_sector_map = {
        "XLK": "Technology",
        "XLF": "Financials",
        "XLE": "Energy",
        "XLV": "Healthcare",
        "XLI": "Industrials",
        "XLP": "Consumer Staples",
        "XLY": "Consumer Discretionary",
        "XLB": "Materials",
        "XLRE": "Real Estate",
        "XLU": "Utilities",
    }
    mapping: dict[str, str] = {}
    try:
        universe = get_us_stock_universe(default_etfs)
        for item in universe:
            etf = item.get("sector_etf", "")
            if etf in etf_sector_map:
                mapping[item["symbol"]] = etf_sector_map[etf]
    except Exception:
        pass
    return mapping
