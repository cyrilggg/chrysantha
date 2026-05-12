"""
② Sector rotation / fund flow layer.

Scores sectors across CN/HK/US markets and returns top 3-5 sectors
per market using excess returns, fund flow ranking, and volume expansion.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

from models import Market, MarketRegime, MacroRegime, SectorScore, SectorRanking
from vendors.vibe_trading import (
    fetch_hk_daily,
    get_hk_stock_universe,
    get_sector_mapping,
)

logger = logging.getLogger("screener.sector")

# ── Scoring weights ────────────────────────────────────────────────────────
_W_EXCESS = 0.4
_W_FLOW = 0.4
_W_VOL = 0.2

# ── Macro adjustment boost ─────────────────────────────────────────────────
_MACRO_BOOST = 0.15

# ── US sector ETF → name mapping ───────────────────────────────────────────
_US_SECTOR_ETFS: dict[str, str] = {
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

# ── Regime-affected sector names ───────────────────────────────────────────
_CN_CYCLICAL: set[str] = {"电子", "计算机", "汽车", "机械设备", "电气设备", "国防军工", "传媒"}
_CN_DEFENSIVE: set[str] = {"公用事业", "食品饮料", "医药生物", "农林牧渔", "银行"}
_US_CYCLICAL: set[str] = {"Technology", "Consumer Discretionary", "Industrials", "Energy", "Materials"}
_US_DEFENSIVE: set[str] = {"Utilities", "Consumer Staples", "Healthcare", "Real Estate"}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _rank_normalize(data: dict[str, float]) -> dict[str, float]:
    """Rank entries (1=highest value=best) and normalize ranks to [0, 1]."""
    if not data:
        return {}
    sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=True)
    n = len(sorted_items)
    norm: dict[str, float] = {}
    for rank, (name, _) in enumerate(sorted_items, start=1):
        norm[name] = (n - rank) / (n - 1) if n > 1 else 0.5
    return norm


def _build_flow_ranks(flow: dict[str, float]) -> dict[str, int]:
    """Build {sector: int_rank} where 1 = most inflow."""
    ranks: dict[str, int] = {}
    for rank, (name, _) in enumerate(
        sorted(flow.items(), key=lambda x: x[1], reverse=True), start=1
    ):
        ranks[name] = rank
    return ranks


def _assemble_scores(
    market: Market,
    excess_returns: dict[str, float],
    flow: dict[str, float],
    volume_ratios: dict[str, float],
) -> list[SectorScore]:
    """Combine excess, flow, and volume into SectorScore list."""
    excess_norm = _rank_normalize(excess_returns)
    flow_norm = _rank_normalize(flow)
    vol_norm = _rank_normalize(volume_ratios)
    flow_ranks = _build_flow_ranks(flow)

    scores: list[SectorScore] = []
    for name in excess_returns:
        score = (
            _W_EXCESS * excess_norm.get(name, 0.0)
            + _W_FLOW * flow_norm.get(name, 0.0)
            + _W_VOL * vol_norm.get(name, 0.0)
        )
        scores.append(
            SectorScore(
                sector=name,
                market=market,
                score=round(score, 4),
                excess_return_5d=round(excess_returns.get(name, 0.0), 4),
                fund_flow_rank=flow_ranks.get(name, len(flow_ranks) + 1),
                volume_ratio=round(volume_ratios.get(name, 1.0), 4),
            )
        )

    scores.sort(key=lambda s: s.score, reverse=True)
    return scores


# ═══════════════════════════════════════════════════════════════════════════════
# A-Share: 申万行业指数 + 北向资金
# ═══════════════════════════════════════════════════════════════════════════════

async def _score_cn_sectors() -> list[SectorScore]:
    """Score A-share sectors using 申万行业指数 and 北向资金 flow."""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare not available, skipping CN sectors")
        return []

    loop = asyncio.get_running_loop()

    # ── Get industry board list ──────────────────────────────────────────
    try:
        board_df: pd.DataFrame = await loop.run_in_executor(
            None, ak.stock_board_industry_hist_em
        )
        if board_df is None or board_df.empty:
            logger.warning("CN sector board list empty")
            return []
    except Exception:
        logger.exception("Failed to fetch CN industry board list")
        return []

    # Resolve name column
    name_col = next(
        (c for c in ["板块名称", "board_name", "name"] if c in board_df.columns),
        board_df.columns[0],
    )
    board_names: list[str] = board_df[name_col].astype(str).tolist()

    # ── Fetch CSI300 benchmark return ────────────────────────────────────
    csi300_5d_return = 0.0
    try:
        csi300_df = await loop.run_in_executor(
            None,
            lambda: yf.download("000300.SS", period="1mo", progress=False, auto_adjust=True),
        )
        if not csi300_df.empty and "Close" in csi300_df.columns:
            closes = csi300_df["Close"].astype(float)
            if len(closes) >= 5:
                csi300_5d_return = float(
                    (closes.iloc[-1] - closes.iloc[-5]) / closes.iloc[-5]
                )
    except Exception:
        logger.warning("Failed to fetch CSI300 benchmark", exc_info=True)

    # ── Fetch 北向资金 flow and map to sectors ───────────────────────────
    flow_by_sector: dict[str, float] = {}
    try:
        north_df = await loop.run_in_executor(
            None, ak.stock_hsgt_north_net_flow_in_em
        )
        if north_df is not None and not north_df.empty:
            flow_col = next(
                (c for c in ["净买入额", "net_flow", "net_amount"] if c in north_df.columns),
                None,
            )
            stock_col = next(
                (c for c in ["股票代码", "code", "symbol"] if c in north_df.columns),
                None,
            )
            if flow_col and stock_col:
                sector_map = await loop.run_in_executor(None, get_sector_mapping, "cn")
                for _, row in north_df.iterrows():
                    code = str(row.get(stock_col, ""))
                    flow_val = float(row.get(flow_col, 0) or 0)
                    if flow_val <= 0:
                        continue
                    # Build symbol
                    if code.startswith(("60", "68")):
                        sym = f"{code}.SH"
                    elif code.startswith(("00", "002", "003", "30")):
                        sym = f"{code}.SZ"
                    elif code.startswith(("8", "4")):
                        sym = f"{code}.BJ"
                    else:
                        sym = f"{code}.SZ"
                    sector = sector_map.get(sym, "")
                    if sector:
                        flow_by_sector[sector] = flow_by_sector.get(sector, 0.0) + flow_val
    except Exception:
        logger.warning("Failed to fetch 北向资金 flow for CN sectors", exc_info=True)

    # ── Score each sector board ──────────────────────────────────────────
    excess_returns: dict[str, float] = {}
    volume_ratios: dict[str, float] = {}

    for name in board_names[:80]:
        try:
            hist = await loop.run_in_executor(
                None, lambda n=name: ak.stock_board_industry_hist_em(symbol=n)
            )
            if hist is None or hist.empty or len(hist) < 5:
                continue

            close_col = next(
                (c for c in ["收盘价", "close"] if c in hist.columns), None
            )
            if close_col is None:
                continue
            closes = hist[close_col].astype(float)
            if len(closes) < 5:
                continue

            ret_5d = float((closes.iloc[-1] - closes.iloc[-5]) / closes.iloc[-5])
            excess_returns[name] = ret_5d - csi300_5d_return

            vol_col = next(
                (c for c in ["成交量", "volume"] if c in hist.columns), None
            )
            if vol_col:
                vols = hist[vol_col].astype(float)
                avg_vol_5d = float(vols.tail(5).mean()) if len(vols) >= 5 else 0.0
                avg_vol_20d = float(vols.tail(20).mean()) if len(vols) >= 20 else avg_vol_5d
                vol_ratio = avg_vol_5d / avg_vol_20d if avg_vol_20d > 0 else 1.0
            else:
                vol_ratio = 1.0
            volume_ratios[name] = vol_ratio
        except Exception:
            logger.debug("Failed to fetch data for CN sector %r", name, exc_info=True)
            continue

    if not excess_returns:
        logger.warning("No CN sector data available")
        return []

    return _assemble_scores(Market.CN, excess_returns, flow_by_sector, volume_ratios)


# ═══════════════════════════════════════════════════════════════════════════════
# HK: stock_hk_spot sectors + 南向资金
# ═══════════════════════════════════════════════════════════════════════════════

async def _score_hk_sectors() -> list[SectorScore]:
    """Score HK sectors via stock aggregation and 南向资金 flow."""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare not available, skipping HK sectors")
        return []

    loop = asyncio.get_running_loop()

    # ── Get universe + sector mapping ───────────────────────────────────
    try:
        universe = await loop.run_in_executor(None, get_hk_stock_universe)
        sector_map = await loop.run_in_executor(None, get_sector_mapping, "hk")
    except Exception:
        logger.exception("Failed to get HK stock universe / sector map")
        return []

    if not universe:
        logger.warning("HK stock universe empty")
        return []

    # ── 南向资金 flow ──────────────────────────────────────────────────
    flow_by_sector: dict[str, float] = {}
    try:
        south_df = await loop.run_in_executor(
            None, ak.stock_hsgt_south_net_flow_in_em
        )
        if south_df is not None and not south_df.empty:
            flow_col = next(
                (c for c in ["净买入额", "net_flow", "net_amount"] if c in south_df.columns),
                None,
            )
            stock_col = next(
                (c for c in ["股票代码", "code", "symbol"] if c in south_df.columns),
                None,
            )
            if flow_col and stock_col:
                for _, row in south_df.iterrows():
                    code = str(row.get(stock_col, ""))
                    flow_val = float(row.get(flow_col, 0) or 0)
                    if flow_val <= 0:
                        continue
                    sym = f"{code}.HK"
                    sector = sector_map.get(sym, "")
                    if sector:
                        flow_by_sector[sector] = flow_by_sector.get(sector, 0.0) + flow_val
    except Exception:
        logger.warning("Failed to fetch 南向资金 flow for HK sectors", exc_info=True)

    # ── Group stocks by sector ──────────────────────────────────────────
    by_sector: dict[str, list[str]] = {}
    for stock in universe:
        sym = stock["symbol"]
        sector = sector_map.get(sym, "其他")
        by_sector.setdefault(sector, []).append(sym)

    if not by_sector:
        logger.warning("No HK sector groupings available")
        return []

    today = datetime.now()
    start_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    # ── HSI benchmark ───────────────────────────────────────────────────
    hsi_return = 0.0
    try:
        hsi_df = await loop.run_in_executor(
            None,
            lambda: yf.download("^HSI", period="1mo", progress=False, auto_adjust=True),
        )
        if not hsi_df.empty and "Close" in hsi_df.columns:
            closes = hsi_df["Close"].astype(float)
            if len(closes) >= 5:
                hsi_return = float((closes.iloc[-1] - closes.iloc[-5]) / closes.iloc[-5])
    except Exception:
        logger.warning("Failed to fetch HSI benchmark", exc_info=True)

    # ── Compute sector returns ──────────────────────────────────────────
    excess_returns: dict[str, float] = {}
    volume_ratios: dict[str, float] = {}

    for sector, symbols in by_sector.items():
        try:
            sample = symbols[:50]  # cap per-sector fetch
            df = await loop.run_in_executor(
                None, fetch_hk_daily, sample, start_date, end_date
            )
            if df is None or df.empty or "close" not in df.columns:
                continue

            # Equal-weighted sector close
            daily_close = df.groupby("date")["close"].mean()
            if len(daily_close) < 5:
                continue
            ret_5d = float(
                (daily_close.iloc[-1] - daily_close.iloc[-5]) / daily_close.iloc[-5]
            )
            excess_returns[sector] = ret_5d - hsi_return

            # Volume expansion
            if "volume" in df.columns:
                daily_vol = df.groupby("date")["volume"].sum()
                avg_5 = float(daily_vol.tail(5).mean()) if len(daily_vol) >= 5 else 0.0
                avg_20 = float(daily_vol.tail(20).mean()) if len(daily_vol) >= 20 else avg_5
                vol_ratio = avg_5 / avg_20 if avg_20 > 0 else 1.0
            else:
                vol_ratio = 1.0
            volume_ratios[sector] = vol_ratio
        except Exception:
            logger.debug("Failed to compute HK sector %r", sector, exc_info=True)
            continue

    if not excess_returns:
        logger.warning("No HK sector data available")
        return []

    return _assemble_scores(Market.HK, excess_returns, flow_by_sector, volume_ratios)


# ═══════════════════════════════════════════════════════════════════════════════
# US: sector ETFs via yfinance
# ═══════════════════════════════════════════════════════════════════════════════

async def _score_us_sectors() -> list[SectorScore]:
    """Score US sectors via sector ETFs with SPY benchmark."""
    loop = asyncio.get_running_loop()

    etf_tickers = list(_US_SECTOR_ETFS.keys())
    all_tickers = etf_tickers + ["SPY"]

    try:
        df = await loop.run_in_executor(
            None,
            lambda: yf.download(all_tickers, period="1mo", progress=False, auto_adjust=True),
        )
    except Exception:
        logger.exception("Failed to fetch US sector ETF data")
        return []

    if df is None or df.empty or "Close" not in df.columns:
        logger.warning("US sector ETF data empty")
        return []

    closes = df["Close"].astype(float)
    volumes = df["Volume"] if "Volume" in df.columns else None

    # SPY benchmark return
    spy_return = 0.0
    if "SPY" in closes.columns:
        spy_closes = closes["SPY"].dropna()
        if len(spy_closes) >= 5:
            spy_return = float(
                (spy_closes.iloc[-1] - spy_closes.iloc[-5]) / spy_closes.iloc[-5]
            )

    excess_returns: dict[str, float] = {}
    volume_ratios: dict[str, float] = {}
    flow_proxy: dict[str, float] = {}  # price × volume proxy

    for etf, sector_name in _US_SECTOR_ETFS.items():
        if etf not in closes.columns:
            continue
        etf_closes = closes[etf].dropna()
        if len(etf_closes) < 5:
            continue

        ret_5d = float(
            (etf_closes.iloc[-1] - etf_closes.iloc[-5]) / etf_closes.iloc[-5]
        )
        excess_returns[sector_name] = ret_5d - spy_return

        # Volume expansion
        if volumes is not None and etf in volumes.columns:
            etf_vols = volumes[etf].dropna()
            avg_5 = float(etf_vols.tail(5).mean()) if len(etf_vols) >= 5 else 0.0
            avg_20 = float(etf_vols.tail(20).mean()) if len(etf_vols) >= 20 else avg_5
            vol_ratio = avg_5 / avg_20 if avg_20 > 0 else 1.0
        else:
            vol_ratio = 1.0
        volume_ratios[sector_name] = vol_ratio

        # Fund flow proxy: avg(price × volume) over last 5 days
        if volumes is not None and etf in volumes.columns:
            etf_vols = volumes[etf].dropna()
            common = etf_closes.index.intersection(etf_vols.index)
            if len(common) >= 5:
                tail = common[-5:]
                pv_mean = (etf_closes.loc[tail] * etf_vols.loc[tail]).mean()
                flow_proxy[sector_name] = float(pv_mean)
            else:
                flow_proxy[sector_name] = 0.0
        else:
            flow_proxy[sector_name] = 0.0

    if not excess_returns:
        logger.warning("No US sector data available")
        return []

    return _assemble_scores(Market.US, excess_returns, flow_proxy, volume_ratios)


# ═══════════════════════════════════════════════════════════════════════════════
# Macro context adjustment
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_macro_adjustment(
    scores: list[SectorScore], regime: MarketRegime
) -> list[SectorScore]:
    """Apply risk-on / risk-off boosts. Returns re-sorted list."""
    if regime == MarketRegime.NEUTRAL:
        return scores

    for s in scores:
        if s.market == Market.US:
            cyclical = s.sector in _US_CYCLICAL
            defensive = s.sector in _US_DEFENSIVE
        else:
            cyclical = s.sector in _CN_CYCLICAL
            defensive = s.sector in _CN_DEFENSIVE

        if regime == MarketRegime.RISK_ON and cyclical:
            s.score = round(s.score + _MACRO_BOOST, 4)
        elif regime == MarketRegime.RISK_OFF and defensive:
            s.score = round(s.score + _MACRO_BOOST, 4)

    scores.sort(key=lambda s: s.score, reverse=True)
    return scores


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

async def rank_sectors(
    markets: list[str] | None = None,
    regime_context: MacroRegime | None = None,
) -> SectorRanking:
    """Score and rank all sectors across specified markets. Returns top 5 per market.

    Args:
        markets: Market codes to process (``["cn", "hk", "us"]``). None = all.
        regime_context: Optional macro regime for risk-on/risk-off score adjustments.
    """
    if markets is None:
        markets = ["cn", "hk", "us"]

    tasks: dict[str, asyncio.Task] = {}

    if "cn" in markets:
        tasks["cn"] = asyncio.create_task(_score_cn_sectors(), name="cn_sectors")
    if "hk" in markets:
        tasks["hk"] = asyncio.create_task(_score_hk_sectors(), name="hk_sectors")
    if "us" in markets:
        tasks["us"] = asyncio.create_task(_score_us_sectors(), name="us_sectors")

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    all_scores: list[SectorScore] = []
    for market, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            logger.error("Sector scoring failed for %s: %s", market, result)
            continue
        if result:
            all_scores.extend(result)

    # Apply macro adjustment
    if regime_context is not None:
        all_scores = _apply_macro_adjustment(all_scores, regime_context.regime)

    # Top 5 per market, with overall fallback to reach at least 5
    top: list[SectorScore] = []
    for mkt in markets:
        mkt_scores = [s for s in all_scores if s.market.value == mkt]
        mkt_scores.sort(key=lambda s: s.score, reverse=True)
        top.extend(mkt_scores[:5])

    if len(top) < 5:
        remaining = [s for s in all_scores if s not in top]
        remaining.sort(key=lambda s: s.score, reverse=True)
        top.extend(remaining[: 5 - len(top)])

    return SectorRanking(
        top_sectors=top,
        all_scores=all_scores,
        generated_at=datetime.utcnow(),
        regime_context=regime_context,
    )
