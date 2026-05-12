"""
Four-layer screening pipeline orchestrator.

Chains macro → sector → preference → screening, with support for
partial entry points and resilient error handling.
"""
from __future__ import annotations

import logging
from datetime import datetime

from models import (
    Market,
    MarketRegime,
    MacroRegime,
    SectorScore,
    SectorRanking,
    ScreenedStock,
    ScreeningResult,
    ScreenRequest,
    ErrorResponse,
)
from layers.macro import assess_macro
from layers.sector import rank_sectors
from layers.preference import apply_preferences, get_active_markets
from layers.screening import screen_candidates

logger = logging.getLogger("screener.pipeline")

# ── Layer ordering ────────────────────────────────────────────────────
_LAYER_ORDER = {"macro": 0, "sector": 1, "preference": 2, "screening": 3}


def _should_run(request: ScreenRequest, layer: str) -> bool:
    """Return True if `layer` should execute given the request's start_from."""
    req_level = _LAYER_ORDER.get(request.start_from, 0)
    layer_level = _LAYER_ORDER.get(layer, 0)
    return req_level <= layer_level


# ── Main pipeline ──────────────────────────────────────────────────────


async def run_pipeline(
    request: ScreenRequest,
    redis_client=None,
    force_refresh_macro: bool = False,
) -> ScreeningResult:
    """Execute the full four-layer screening pipeline.

    Flow:
    1. If start_from <= "macro": run macro layer (uses cache if available)
    2. If start_from <= "sector": run sector layer with regime context
    3. Get stock universe for requested markets
    4. If start_from <= "preference": run preference filter
    5. If start_from <= "screening": run quantitative screening
    6. Return ScreeningResult with all intermediate data
    """
    result = ScreeningResult()
    errors: list[str] = []

    regime: MacroRegime | None = None
    top_sectors: list[SectorScore] = []

    # ── Layer ①: Macro ────────────────────────────────────────────────
    if _should_run(request, "macro"):
        try:
            regime = await assess_macro(
                redis_client=redis_client,
                force_refresh=force_refresh_macro,
            )
            result.regime = regime
            logger.info("Macro: regime=%s confidence=%.2f", regime.regime.value, regime.confidence)
        except Exception as exc:
            logger.exception("Macro layer failed")
            errors.append(f"macro: {exc}")

    # ── Layer ②: Sector ──────────────────────────────────────────────
    if _should_run(request, "sector"):
        try:
            market_values = [m.value for m in request.markets]
            ranking: SectorRanking = await rank_sectors(markets=market_values)
            top_sectors = ranking.top_sectors
            result.top_sectors = top_sectors
            logger.info("Sector: %d top sectors ranked", len(top_sectors))
        except Exception as exc:
            logger.exception("Sector layer failed")
            errors.append(f"sector: {exc}")
    else:
        # If not running sector, still use requested sectors for filtering
        top_sectors = []

    # ── Stock universe ────────────────────────────────────────────────
    all_stocks: list[dict] = []
    try:
        all_stocks = _gather_stock_universe(request.markets)
        result.total_scanned = len(all_stocks)
        logger.info("Universe: %d stocks loaded across %d markets", len(all_stocks), len(request.markets))
    except Exception as exc:
        logger.exception("Stock universe gathering failed")
        errors.append(f"universe: {exc}")

    # ── Layer ③: Preference ──────────────────────────────────────────
    if _should_run(request, "preference"):
        try:
            passed, excluded = apply_preferences(all_stocks)
            all_stocks = passed
            result.excluded_count += len(excluded)
            logger.info("Preference: %d passed, %d excluded", len(passed), len(excluded))
        except Exception as exc:
            logger.exception("Preference layer failed")
            errors.append(f"preference: {exc}")
            # Continue with unfiltered stocks on preference failure

    # ── Layer ④: Screening ────────────────────────────────────────────
    if _should_run(request, "screening"):
        try:
            candidates: list[ScreenedStock] = await screen_candidates(
                stocks=all_stocks,
                top_n=request.top_n,
                sector_filter=request.sectors if request.sectors else None,
            )
            result.candidates = candidates
            logger.info("Screening: %d candidates selected", len(candidates))
        except Exception as exc:
            logger.exception("Screening layer failed")
            errors.append(f"screening: {exc}")

    # ── Finalize ──────────────────────────────────────────────────────
    result.generated_at = datetime.utcnow()

    if errors:
        logger.warning("Pipeline completed with %d error(s): %s", len(errors), errors)
        # Errors are logged; partial results are still returned in `result`

    return result


# ── Partial entry points ──────────────────────────────────────────────


async def run_from_sector(request: ScreenRequest) -> ScreeningResult:
    """Run from sector layer onwards (skip macro, use cached regime)."""
    request.start_from = "sector"
    return await run_pipeline(request)


async def run_from_screening(request: ScreenRequest) -> ScreeningResult:
    """Run only screening layer (skip macro, sector, preference)."""
    request.start_from = "screening"
    return await run_pipeline(request)


# ── Helpers ───────────────────────────────────────────────────────────


def _gather_stock_universe(markets: list[Market]) -> list[dict]:
    """Collect all stocks for the requested markets from vibe-trading.

    Each stock dict has: symbol, name. Returns an empty list on failure.
    """
    from vendors.vibe_trading import (
        get_cn_stock_universe,
        get_hk_stock_universe,
        get_us_stock_universe,
    )

    universe: list[dict] = []
    for market in markets:
        try:
            if market == Market.CN:
                universe.extend(get_cn_stock_universe())
            elif market == Market.HK:
                universe.extend(get_hk_stock_universe())
            elif market == Market.US:
                universe.extend(get_us_stock_universe())
        except Exception:
            logger.exception("Failed to load universe for market %s", market.value)
    return universe
