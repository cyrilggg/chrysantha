"""
④ Quantitative screening layer.

Applies pandas vectorised filters to candidate stocks:
  turnover_rate > 1.5, close > MA20, volume_ratio > 1.2,
  5 < PE_TTM < 60, market_cap > min_market_cap.

Scores passing stocks with cross-sectional Z-scores and returns
the top N candidates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from models import Market, ScreenedStock, ScreeningResult
from vendors.vibe_trading import (
    fetch_cn_daily,
    fetch_hk_daily,
    fetch_us_daily,
    get_cn_stock_universe,
    get_hk_stock_universe,
    get_us_stock_universe,
    get_sector_mapping,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CHUNK_SIZE = 100
_LOOKBACK_CALENDAR_DAYS = 60  # pull ~60 calendar days to guarantee >=20 trading days
_MA_WINDOW = 20
_MAX_WORKERS = 8
_MIN_TURNOVER = 1.5
_MIN_VOLUME_RATIO = 1.2
_PE_MIN = 5.0
_PE_MAX = 60.0
_DEFAULT_MIN_MARKET_CAP = 5e9  # CNY


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _chunk_list(lst: list, n: int) -> list[list]:
    """Split *lst* into sub-lists of max length *n*."""
    return [lst[i : i + n] for i in range(0, len(lst), n)]


def _lookback_dates() -> tuple[str, str]:
    """Return (start_date, end_date) covering ~60 calendar days."""
    end = datetime.now()
    start = end - timedelta(days=_LOOKBACK_CALENDAR_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _safe_float(val, default: float = np.nan) -> float:
    """Convert a value to float, returning *default* on failure."""
    if val is None:
        return default
    try:
        f = float(val)
        return f if np.isfinite(f) else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Fundamental data fetchers (PE_TTM, market_cap, turnover, shares)
# ---------------------------------------------------------------------------

def _fetch_cn_fundamentals() -> pd.DataFrame:
    """Fetch A-share fundamentals via AKShare East Money spot.

    Returns a DataFrame keyed by symbol with columns:
        symbol, pe_ttm, market_cap, turnover_rate, shares_outstanding
    """
    try:
        import akshare as ak

        df = ak.stock_a_spot_em()
        if df is None or df.empty:
            logger.warning("_fetch_cn_fundamentals: empty response from stock_a_spot_em")
            return pd.DataFrame()

        records: list[dict] = []
        for _, row in df.iterrows():
            code = str(row.get("代码", row.get("code", "")))
            if not code:
                continue

            # Build symbol with exchange suffix
            if code.startswith(("60", "68")):
                symbol = f"{code}.SH"
            elif code.startswith(("00", "002", "003", "30")):
                symbol = f"{code}.SZ"
            elif code.startswith(("8", "4")):
                symbol = f"{code}.BJ"
            else:
                symbol = f"{code}.SZ"

            # Column names vary by AKShare version; try common aliases
            pe_raw = row.get("市盈率-动态", row.get("peTTM", row.get("pe", np.nan)))
            mc_raw = row.get("总市值", row.get("totalMarketCap", row.get("market_cap", np.nan)))
            to_raw = row.get("换手率", row.get("turnoverRate", row.get("turnover_rate", np.nan)))
            # Shares outstanding (总股本 / circulating shares)
            shares_raw = row.get("总股本", row.get("shares", row.get("shares_outstanding", np.nan)))

            pe = _safe_float(pe_raw)
            mc = _safe_float(mc_raw)
            turnover = _safe_float(to_raw)
            shares = _safe_float(shares_raw)

            records.append({
                "symbol": symbol,
                "pe_ttm": pe,
                "market_cap": mc,
                "turnover_rate": turnover,
                "shares_outstanding": shares,
            })

        result = pd.DataFrame(records)
        logger.info("_fetch_cn_fundamentals: %d stocks with fundamental data", len(result))
        return result

    except ImportError:
        logger.warning("_fetch_cn_fundamentals: akshare not installed")
        return pd.DataFrame()
    except Exception as exc:
        logger.warning("_fetch_cn_fundamentals failed: %s", exc)
        return pd.DataFrame()


def _fetch_hk_fundamentals() -> pd.DataFrame:
    """Fetch HK stock fundamentals via AKShare HK spot.

    Returns a DataFrame keyed by symbol with columns:
        symbol, pe_ttm, market_cap, turnover_rate, shares_outstanding
    """
    try:
        import akshare as ak

        df = ak.stock_hk_spot()
        if df is None or df.empty:
            logger.warning("_fetch_hk_fundamentals: empty response")
            return pd.DataFrame()

        records: list[dict] = []
        for _, row in df.iterrows():
            code = str(row.get("代码", row.get("code", "")))
            if not code:
                continue
            symbol = f"{code}.HK"

            # HK spot columns often use Chinese names; try multiple
            pe_raw = row.get("市盈率", row.get("peTTM", row.get("pe_ratio", np.nan)))
            mc_raw = row.get("总市值", row.get("marketCap", row.get("market_cap", np.nan)))
            to_raw = row.get("换手率", row.get("turnoverRate", row.get("turnover", np.nan)))
            shares_raw = row.get("总股本", row.get("shares", row.get("shares_outstanding", np.nan)))

            pe = _safe_float(pe_raw)
            mc = _safe_float(mc_raw)
            turnover = _safe_float(to_raw)
            shares = _safe_float(shares_raw)

            records.append({
                "symbol": symbol,
                "pe_ttm": pe,
                "market_cap": mc,
                "turnover_rate": turnover,
                "shares_outstanding": shares,
            })

        result = pd.DataFrame(records)
        logger.info("_fetch_hk_fundamentals: %d stocks with fundamental data", len(result))
        return result

    except ImportError:
        logger.warning("_fetch_hk_fundamentals: akshare not installed")
        return pd.DataFrame()
    except Exception as exc:
        logger.warning("_fetch_hk_fundamentals failed: %s", exc)
        return pd.DataFrame()


def _fetch_us_fundamentals(symbols: list[str]) -> pd.DataFrame:
    """Fetch US stock fundamentals via yfinance Ticker.info (parallel).

    Returns a DataFrame keyed by symbol with columns:
        symbol, pe_ttm, market_cap, shares_outstanding
    """
    if not symbols:
        return pd.DataFrame()

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("_fetch_us_fundamentals: yfinance not installed")
        return pd.DataFrame()

    def _fetch_one(sym: str) -> Optional[dict]:
        """Fetch info for a single ticker. Strip .US suffix for yfinance."""
        ticker_str = sym.replace(".US", "")
        try:
            t = yf.Ticker(ticker_str)
            info = t.info or {}
            pe = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
            mc = _safe_float(info.get("marketCap"))
            shares = _safe_float(info.get("sharesOutstanding"))
            return {
                "symbol": sym,
                "pe_ttm": pe,
                "market_cap": mc,
                "shares_outstanding": shares,
            }
        except Exception as exc:
            logger.debug("_fetch_us_fundamentals: failed for %s: %s", sym, exc)
            return None

    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in symbols}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                records.append(result)

    result = pd.DataFrame(records)
    logger.info("_fetch_us_fundamentals: %d stocks with fundamental data", len(result))
    return result


# ---------------------------------------------------------------------------
# Daily data fetching (chunked)
# ---------------------------------------------------------------------------

def _fetch_daily_chunked(
    market: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch daily OHLCV data in chunks of *_CHUNK_SIZE*."""
    if not symbols:
        return pd.DataFrame()

    fetch_func = {
        "cn": fetch_cn_daily,
        "hk": fetch_hk_daily,
        "us": fetch_us_daily,
    }.get(market)

    if fetch_func is None:
        logger.warning("_fetch_daily_chunked: unknown market %r", market)
        return pd.DataFrame()

    chunks = _chunk_list(symbols, _CHUNK_SIZE)
    frames: list[pd.DataFrame] = []

    for i, chunk in enumerate(chunks):
        try:
            df = fetch_func(chunk, start_date, end_date)
            if not df.empty:
                frames.append(df)
        except Exception as exc:
            logger.warning(
                "_fetch_daily_chunked: chunk %d/%d (market=%s) failed: %s",
                i + 1, len(chunks), market, exc,
            )

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    return combined


# ---------------------------------------------------------------------------
# Per-stock metrics computation
# ---------------------------------------------------------------------------

def _compute_metrics(
    df_daily: pd.DataFrame,
    df_funda: pd.DataFrame,
) -> pd.DataFrame:
    """Compute per-stock screening metrics from daily and fundamental DataFrames.

    Parameters
    ----------
    df_daily : pd.DataFrame
        Columns: symbol, date, close, volume.  Multi-row per symbol.
    df_funda : pd.DataFrame
        Columns: symbol, pe_ttm, market_cap, turnover_rate, shares_outstanding.
        One row per symbol.

    Returns
    -------
    pd.DataFrame with one row per symbol and columns:
        symbol, latest_close, ma20, volume_ratio, turnover_rate,
        pe_ttm, market_cap
    """
    if df_daily.empty:
        return pd.DataFrame()

    # Ensure date column is datetime
    if "date" in df_daily.columns:
        df_daily["date"] = pd.to_datetime(df_daily["date"], errors="coerce")

    # Sort by symbol + date for rolling windows
    df_daily = df_daily.sort_values(["symbol", "date"])

    # --- Per-symbol rolling metrics ---
    df_daily["ma20"] = df_daily.groupby("symbol")["close"].transform(
        lambda s: s.rolling(_MA_WINDOW, min_periods=_MA_WINDOW).mean()
    )
    df_daily["avg_vol_20"] = df_daily.groupby("symbol")["volume"].transform(
        lambda s: s.rolling(_MA_WINDOW, min_periods=_MA_WINDOW).mean()
    )

    # Keep only the latest row per symbol (most recent trading day)
    latest_idx = df_daily.groupby("symbol")["date"].transform("max") == df_daily["date"]
    latest = df_daily[latest_idx].copy()

    # If multiple rows have the same latest date (unlikely), keep first
    latest = latest.drop_duplicates(subset=["symbol"], keep="first")

    # --- Compute volume_ratio: today's volume / avg 20-day volume ---
    latest["volume_ratio"] = np.where(
        latest["avg_vol_20"].notna() & (latest["avg_vol_20"] > 0),
        latest["volume"] / latest["avg_vol_20"],
        np.nan,
    )

    # --- Merge with fundamentals ---
    metrics = latest[["symbol", "close", "ma20", "volume_ratio", "volume"]].copy()
    metrics = metrics.rename(columns={"close": "latest_close"})

    if not df_funda.empty and "symbol" in df_funda.columns:
        metrics = metrics.merge(df_funda, on="symbol", how="left")
    else:
        for col in ("pe_ttm", "market_cap", "turnover_rate", "shares_outstanding"):
            if col not in metrics.columns:
                metrics[col] = np.nan

    # --- Compute turnover_rate from volume / shares if not available ---
    no_turnover = metrics["turnover_rate"].isna() | (metrics["turnover_rate"] <= 0)
    can_compute = (
        metrics["volume"].notna()
        & metrics["shares_outstanding"].notna()
        & (metrics["shares_outstanding"] > 0)
    )
    mask = no_turnover & can_compute
    metrics.loc[mask, "turnover_rate"] = (
        metrics.loc[mask, "volume"] / metrics.loc[mask, "shares_outstanding"]
    ) * 100  # convert to percentage

    return metrics


# ---------------------------------------------------------------------------
# Filtering & scoring
# ---------------------------------------------------------------------------

def _apply_filters(
    metrics: pd.DataFrame,
    min_market_cap: float,
) -> tuple[pd.DataFrame, dict]:
    """Apply quantitative filters, returning (passed, filter_stats).

    Each filter skips NaN values (does not exclude due to missing data).
    """
    if metrics.empty:
        return metrics, {}

    total = len(metrics)
    stats: dict[str, int] = {"total_input": total}

    # Build boolean mask; start with all True
    mask = pd.Series(True, index=metrics.index)

    # --- Filter 1: turnover_rate > _MIN_TURNOVER ---
    has_turnover = metrics["turnover_rate"].notna()
    turnover_ok = metrics["turnover_rate"] > _MIN_TURNOVER
    # Skip NaN: treat as pass (keep in)
    turnover_pass = ~has_turnover | turnover_ok
    stats["excluded_turnover"] = int((~turnover_pass).sum())
    mask = mask & turnover_pass

    # --- Filter 2: close > ma20 ---
    has_ma = metrics["latest_close"].notna() & metrics["ma20"].notna()
    ma_ok = metrics["latest_close"] > metrics["ma20"]
    ma_pass = ~has_ma | ma_ok
    stats["excluded_ma20"] = int((~ma_pass).sum())
    mask = mask & ma_pass

    # --- Filter 3: volume_ratio > _MIN_VOLUME_RATIO ---
    has_vr = metrics["volume_ratio"].notna()
    vr_ok = metrics["volume_ratio"] > _MIN_VOLUME_RATIO
    vr_pass = ~has_vr | vr_ok
    stats["excluded_volume_ratio"] = int((~vr_pass).sum())
    mask = mask & vr_pass

    # --- Filter 4: _PE_MIN < pe_ttm < _PE_MAX ---
    has_pe = metrics["pe_ttm"].notna()
    pe_in_range = (metrics["pe_ttm"] > _PE_MIN) & (metrics["pe_ttm"] < _PE_MAX)
    pe_pass = ~has_pe | pe_in_range
    stats["excluded_pe"] = int((~pe_pass).sum())
    mask = mask & pe_pass

    # --- Filter 5: market_cap > min_market_cap ---
    has_mc = metrics["market_cap"].notna()
    mc_ok = metrics["market_cap"] > min_market_cap
    mc_pass = ~has_mc | mc_ok
    stats["excluded_market_cap"] = int((~mc_pass).sum())
    mask = mask & mc_pass

    passed = metrics[mask].copy()
    stats["passed"] = len(passed)
    stats["total_excluded"] = total - len(passed)

    return passed, stats


def _zscore(series: pd.Series) -> pd.Series:
    """Cross-sectional Z-score. Returns 0 where std == 0 or all NaN."""
    mean = series.mean()
    std = series.std(ddof=0)
    if pd.isna(std) or std == 0:
        return pd.Series(0.0, index=series.index)
    return (series - mean) / std


def _score_candidates(metrics: pd.DataFrame) -> pd.DataFrame:
    """Compute composite Z-score for each passing stock.

    Components (cross-sectional Z-scores):
        momentum_z  = (latest_close / ma20 - 1)
        volume_z    = (volume_ratio - 1)
        liquidity_z = turnover_rate

    Final score = mean of available component z-scores.
    """
    if metrics.empty:
        metrics["score"] = np.nan
        return metrics

    result = metrics.copy()

    # --- Component raw values ---
    has_ma = result["latest_close"].notna() & result["ma20"].notna() & (result["ma20"] > 0)
    result["momentum_raw"] = np.where(has_ma, result["latest_close"] / result["ma20"] - 1, np.nan)

    result["volume_raw"] = np.where(result["volume_ratio"].notna(), result["volume_ratio"] - 1, np.nan)
    result["liquidity_raw"] = result["turnover_rate"].copy()

    # --- Z-score each component cross-sectionally ---
    result["momentum_z"] = _zscore(result["momentum_raw"])
    result["volume_z"] = _zscore(result["volume_raw"])
    result["liquidity_z"] = _zscore(result["liquidity_raw"])

    # --- Composite: mean of available z-scores ---
    z_cols = ["momentum_z", "volume_z", "liquidity_z"]
    result["score"] = result[z_cols].mean(axis=1, skipna=True)
    # If all z-scores are NaN, set score to -inf so they sort last
    result["score"] = result["score"].fillna(-np.inf)

    return result


# ---------------------------------------------------------------------------
# Core market screening
# ---------------------------------------------------------------------------

def _screen_single_market(
    market: str,
    symbols: list[str],
    symbol_to_name: dict[str, str],
    symbol_to_sector: dict[str, str],
    start_date: str,
    end_date: str,
    min_market_cap: float,
) -> tuple[list[dict], dict]:
    """Run full screening pipeline for one market.

    Returns (scored_candidates, filter_stats).
    """
    if not symbols:
        return [], {"total_input": 0, "passed": 0, "total_excluded": 0}

    # 1. Fetch daily data
    logger.info("Fetching daily data for %d symbols (market=%s)", len(symbols), market)
    df_daily = _fetch_daily_chunked(market, symbols, start_date, end_date)
    logger.info("Daily data: %d rows for market=%s", len(df_daily), market)

    if df_daily.empty:
        logger.warning("No daily data returned for market=%s", market)
        return [], {"total_input": len(symbols), "passed": 0, "total_excluded": len(symbols)}

    # 2. Fetch fundamentals
    logger.info("Fetching fundamentals for market=%s", market)
    if market == "cn":
        df_funda = _fetch_cn_fundamentals()
    elif market == "hk":
        df_funda = _fetch_hk_fundamentals()
    else:  # us
        df_funda = _fetch_us_fundamentals(symbols)

    # 3. Compute per-stock metrics
    metrics = _compute_metrics(df_daily, df_funda)

    # 4. Apply filters
    passed, stats = _apply_filters(metrics, min_market_cap)
    if passed.empty:
        logger.info("Market %s: no stocks passed filters (%s)", market, stats)
        return [], stats

    # 5. Score
    scored = _score_candidates(passed)

    # 6. Build candidate dicts
    candidates: list[dict] = []
    for _, row in scored.iterrows():
        sym = str(row["symbol"])
        candidates.append({
            "symbol": sym,
            "name": symbol_to_name.get(sym, sym),
            "market": market,
            "sector": symbol_to_sector.get(sym, "未知"),
            "score": _safe_float(row.get("score"), default=-999.0),
            "turnover_rate": _safe_float(row.get("turnover_rate"), default=0.0),
            "pe_ttm": _safe_float(row.get("pe_ttm"), default=0.0),
            "volume_ratio": _safe_float(row.get("volume_ratio"), default=0.0),
            "market_cap": _safe_float(row.get("market_cap"), default=0.0),
            "filters_passed": ["turnover", "ma20", "volume_ratio", "pe_ttm", "market_cap"],
        })

    logger.info(
        "Market %s: %d input, %d passed, %d excluded. Stats: %s",
        market, stats.get("total_input", 0), stats.get("passed", 0),
        stats.get("total_excluded", 0), stats,
    )

    return candidates, stats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def screen_candidates(
    stocks: list[dict],
    markets: list[str] | None = None,
    top_n: int = 20,
    min_market_cap: float = _DEFAULT_MIN_MARKET_CAP,
) -> list[ScreenedStock]:
    """Run quantitative filters on a pre-filtered stock list and return scored
    candidates.

    Parameters
    ----------
    stocks : list[dict]
        Pre-filtered stocks. Each dict must have: ``symbol``, ``name``,
        ``market``, ``sector``.
    markets : list[str] | None
        Markets to process. Auto-detected from *stocks* if ``None``.
    top_n : int
        Number of top candidates to return.
    min_market_cap : float
        Minimum market cap in CNY (default 5e9).

    Returns
    -------
    list[ScreenedStock]
        Top N scored candidates.
    """
    if not stocks:
        logger.warning("screen_candidates: empty stock list")
        return []

    # Group stocks by market
    by_market: dict[str, list[dict]] = {}
    for s in stocks:
        m = s.get("market", "cn")
        by_market.setdefault(m, []).append(s)

    if markets is None:
        markets = list(by_market.keys())

    start_date, end_date = _lookback_dates()
    all_candidates: list[dict] = []
    total_excluded = 0
    total_scanned = 0

    loop = asyncio.get_running_loop()

    async def _process_market(market: str) -> tuple[list[dict], dict]:
        market_stocks = by_market.get(market, [])
        if not market_stocks:
            return [], {"total_input": 0, "passed": 0}

        symbols = [s["symbol"] for s in market_stocks]
        symbol_to_name = {s["symbol"]: s.get("name", s["symbol"]) for s in market_stocks}
        symbol_to_sector = {s["symbol"]: s.get("sector", "未知") for s in market_stocks}

        return await loop.run_in_executor(
            None,
            _screen_single_market,
            market,
            symbols,
            symbol_to_name,
            symbol_to_sector,
            start_date,
            end_date,
            min_market_cap,
        )

    tasks = [_process_market(m) for m in markets if m in by_market and by_market[m]]
    if not tasks:
        logger.warning("screen_candidates: no markets to process")
        return []

    results = await asyncio.gather(*tasks)
    for candidates, stats in results:
        all_candidates.extend(candidates)
        total_excluded += stats.get("total_excluded", 0)
        total_scanned += stats.get("total_input", 0)

    # Sort by score descending, take top N
    all_candidates.sort(key=lambda c: c["score"], reverse=True)
    top = all_candidates[:top_n]

    return [
        ScreenedStock(
            symbol=c["symbol"],
            name=c["name"],
            market=Market(c["market"]),
            sector=c["sector"],
            score=round(c["score"], 4),
            turnover_rate=round(c["turnover_rate"], 2),
            pe_ttm=round(c["pe_ttm"], 2),
            volume_ratio=round(c["volume_ratio"], 2),
            market_cap=round(c["market_cap"], 0),
            filters_passed=c["filters_passed"],
        )
        for c in top
    ]


async def scan_full_universe(
    markets: list[str],
    sectors: list[str] | None = None,
    top_n: int = 20,
    min_market_cap: float = _DEFAULT_MIN_MARKET_CAP,
) -> list[ScreenedStock]:
    """Fetch the full stock universe, apply all filters, and return top
    candidates.

    Parameters
    ----------
    markets : list[str]
        Markets to scan (e.g. ``["cn", "hk", "us"]``).
    sectors : list[str] | None
        Optional sector whitelist. Only stocks in these sectors are kept.
    top_n : int
        Number of top candidates to return.
    min_market_cap : float
        Minimum market cap in CNY (default 5e9).

    Returns
    -------
    list[ScreenedStock]
        Top N scored candidates from the full universe.
    """
    if not markets:
        logger.warning("scan_full_universe: empty markets list")
        return []

    loop = asyncio.get_running_loop()

    # --- Step 1: resolve sector mapping (if sectors filter) ---
    sector_maps: dict[str, dict[str, str]] = {}
    sector_set: set[str] | None = set(sectors) if sectors else None

    if sector_set:
        logger.info("scan_full_universe: filtering to sectors %s", sector_set)
        for m in markets:
            try:
                sector_maps[m] = await loop.run_in_executor(None, get_sector_mapping, m)
                logger.info("scan_full_universe: %d sector mappings for market=%s", len(sector_maps[m]), m)
            except Exception as exc:
                logger.warning("scan_full_universe: sector mapping failed for %s: %s", m, exc)
                sector_maps[m] = {}

    # --- Step 2: get stock universes in parallel ---
    universe_funcs = {
        "cn": get_cn_stock_universe,
        "hk": get_hk_stock_universe,
        "us": get_us_stock_universe,
    }

    async def _get_universe(market: str) -> tuple[str, list[dict]]:
        func = universe_funcs.get(market)
        if func is None:
            return market, []
        try:
            result = await loop.run_in_executor(None, func)
            return market, result
        except Exception as exc:
            logger.warning("scan_full_universe: failed for market=%s: %s", market, exc)
            return market, []

    universe_tasks = [_get_universe(m) for m in markets]
    universe_results = await asyncio.gather(*universe_tasks)

    # --- Step 3: build pre-filtered stock list ---
    all_stocks: list[dict] = []
    for market, universe in universe_results:
        for item in universe:
            sym = item.get("symbol", "")
            name = item.get("name", sym)

            # Sector assignment
            smap = sector_maps.get(market, {})
            sector = smap.get(sym, item.get("sector", "未知"))

            # Apply sector filter if requested
            if sector_set and sector not in sector_set:
                continue

            all_stocks.append({
                "symbol": sym,
                "name": name,
                "market": market,
                "sector": sector,
            })

    logger.info("scan_full_universe: %d total stocks after sector filter", len(all_stocks))

    if not all_stocks:
        return []

    # --- Step 4: delegate to screen_candidates ---
    return await screen_candidates(
        stocks=all_stocks,
        markets=markets,
        top_n=top_n,
        min_market_cap=min_market_cap,
    )
