"""
Riskfolio-Lib integration for pre-trade risk validation and portfolio optimization.

Usage:
  - check_position_size(): Pre-trade gate — validates proposed trade
    against current portfolio concentration limits and risk metrics.
  - run_hrp_optimization(): Suggest optimal portfolio weights via HRP.
  - compute_risk_metrics(): VaR, CVaR, max drawdown for a returns series.

All functions accept pandas DataFrames so the caller can source returns
from chrysantha market data, yfinance, or any other provider.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("executor-bridge.risk")

# ── Thresholds ─────────────────────────────────────────────────

DEFAULT_MAX_SINGLE_POSITION = 0.20  # Max 20% in any single asset
DEFAULT_MAX_CORRELATED_GROUP = 0.40  # Max 40% in correlated assets
DEFAULT_MAX_VAR_95 = 0.02  # Max 2% daily VaR at 95% confidence
DEFAULT_MIN_DIVERSIFICATION = 0.30  # Min 30% of portfolio in uncorrelated assets


@dataclass
class RiskCheckResult:
    approved: bool
    current_weight: float = 0.0
    proposed_weight: float = 0.0
    max_single_position_pct: float = DEFAULT_MAX_SINGLE_POSITION
    var_95_daily: Optional[float] = None
    cvar_95_daily: Optional[float] = None
    current_herfindahl: Optional[float] = None  # Concentration index
    proposed_herfindahl: Optional[float] = None
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class OptimizationResult:
    weights: dict[str, float]
    risk_contribution: dict[str, float]
    expected_return: float
    expected_risk: float
    sharpe_ratio: float


# ── Pre-trade risk gate ────────────────────────────────────────


def check_position_size(
    holdings: dict[str, dict],
    proposed_ticker: str,
    proposed_quantity: float,
    proposed_price: float,
    returns: Optional[pd.DataFrame] = None,
    max_single_position: float = DEFAULT_MAX_SINGLE_POSITION,
    max_var_95: float = DEFAULT_MAX_VAR_95,
) -> RiskCheckResult:
    """Pre-trade risk gate.

    Args:
        holdings: Current portfolio holdings from chrysantha.
                  Format: {symbol: {quantity, price, allocation_pct, ...}}
        proposed_ticker: The ticker being traded.
        proposed_quantity: Number of shares (positive for buy, negative for sell).
        proposed_price: Execution price.
        returns: Optional DataFrame of historical returns (assets x dates) for VaR.
        max_single_position: Max allowed weight in a single asset.
        max_var_95: Max allowed daily VaR at 95% confidence.

    Returns:
        RiskCheckResult with approval decision and warnings.
    """
    warnings: list[str] = []

    # 1. Calculate total portfolio value
    total_value = _compute_portfolio_value(holdings, proposed_ticker,
                                           proposed_quantity, proposed_price)
    if total_value <= 0:
        return RiskCheckResult(
            approved=False,
            warnings=["Portfolio value is zero or negative — cannot assess risk"],
        )

    # 2. Calculate current and proposed weights
    current_weight = 0.0
    if proposed_ticker in holdings:
        h = holdings[proposed_ticker]
        current_value = h.get("quantity", 0) * h.get("market_price", h.get("price", 0))
        current_weight = current_value / total_value

    proposed_value = (holdings.get(proposed_ticker, {}).get("quantity", 0) + proposed_quantity) * proposed_price
    proposed_weight = proposed_value / total_value

    # 3. Single position limit check
    if proposed_weight > max_single_position:
        warnings.append(
            f"Proposed position {proposed_ticker} would be {proposed_weight:.1%} "
            f"of portfolio, exceeding {max_single_position:.0%} limit"
        )

    # 4. Concentration check (Herfindahl-Hirschman Index)
    current_weights = {}
    for sym, h in holdings.items():
        qty = h.get("quantity", 0)
        price = h.get("market_price", h.get("price", 0))
        current_weights[sym] = qty * price / total_value

    current_weights[proposed_ticker] = proposed_weight
    current_hhi = sum(w ** 2 for w in current_weights.values())

    proposed_weights = dict(current_weights)
    proposed_weights[proposed_ticker] = proposed_weight
    proposed_hhi = sum(w ** 2 for w in proposed_weights.values())

    if proposed_hhi > 0.25:  # HHI > 0.25 indicates high concentration
        warnings.append(
            f"Portfolio concentration (HHI) would be {proposed_hhi:.3f} "
            f"(>0.25 indicates high concentration)"
        )

    # 5. VaR / CVaR check (if returns data available)
    var_95 = None
    cvar_95 = None
    if returns is not None and proposed_ticker in returns.columns:
        try:
            # Compute daily returns VaR at 95% confidence
            ticker_returns = returns[proposed_ticker].dropna()
            if len(ticker_returns) > 60:
                var_95 = float(np.percentile(ticker_returns, 5))
                cvar_95 = float(ticker_returns[ticker_returns <= var_95].mean())
                position_var = var_95 * abs(proposed_value)
                position_var_pct = abs(var_95) * proposed_weight

                if position_var_pct > max_var_95:
                    warnings.append(
                        f"Daily VaR(95%) contribution of {position_var_pct:.2%} "
                        f"exceeds limit of {max_var_95:.2%}"
                    )
        except Exception as e:
            logger.warning("VaR computation failed: %s", e)

    # 6. Decision
    approved = len(warnings) == 0

    return RiskCheckResult(
        approved=approved,
        current_weight=current_weight,
        proposed_weight=proposed_weight,
        var_95_daily=var_95,
        cvar_95_daily=cvar_95,
        current_herfindahl=current_hhi,
        proposed_herfindahl=proposed_hhi,
        warnings=warnings,
        metrics={
            "portfolio_value": total_value,
            "current_weights": current_weights,
            "proposed_weights": proposed_weights,
            "hhi_current": current_hhi,
            "hhi_proposed": proposed_hhi,
            "n_assets": len(current_weights),
        },
    )


# ── Risk metrics ───────────────────────────────────────────────


def compute_risk_metrics(
    returns: pd.DataFrame,
    weights: Optional[dict[str, float]] = None,
    confidence: float = 0.95,
) -> dict:
    """Compute VaR, CVaR, max drawdown, and Sharpe ratio for a portfolio.

    Args:
        returns: DataFrame of asset returns (dates x assets).
        weights: Optional dict of {symbol: weight}. If None, equal weight.
        confidence: Confidence level for VaR/CVaR.

    Returns:
        Dict with var, cvar, max_drawdown, sharpe, volatility metrics.
    """
    if returns.empty:
        return {"error": "No returns data available"}

    common_assets = [c for c in returns.columns if c in (weights or {})]
    if weights and common_assets:
        w = pd.Series({a: weights[a] for a in common_assets})
        w = w / w.sum()
        port_returns = (returns[common_assets] * w).sum(axis=1)
    else:
        port_returns = returns.mean(axis=1)

    port_returns = port_returns.dropna()

    if len(port_returns) < 20:
        return {"error": "Insufficient returns history (need >=20 observations)"}

    try:
        var = float(np.percentile(port_returns, (1 - confidence) * 100))
        cvar = float(port_returns[port_returns <= var].mean())
        max_dd = float(_max_drawdown(port_returns))
        sharpe = _sharpe_ratio(port_returns)
        volatility = float(port_returns.std() * np.sqrt(252))
        annual_return = float(port_returns.mean() * 252)

        return {
            "var": round(var, 6),
            "cvar": round(cvar, 6),
            "var_confidence": confidence,
            "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": round(sharpe, 4),
            "annual_volatility": round(volatility, 4),
            "annual_return": round(annual_return, 4),
            "observations": len(port_returns),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Portfolio optimization ─────────────────────────────────────


def run_hrp_optimization(
    returns: pd.DataFrame,
    method: str = "HRP",
    risk_measure: str = "CVaR",
) -> OptimizationResult:
    """Run Hierarchical Risk Parity optimization.

    Args:
        returns: DataFrame of asset returns (dates x assets).
        method: "HRP" or "HERC" (Hierarchical Equal Risk Contribution).
        risk_measure: "CVaR", "MV" (min variance), "MAD", etc.

    Returns:
        OptimizationResult with target weights and risk metrics.
    """
    if returns.empty or len(returns.columns) < 2:
        raise ValueError("Need at least 2 assets with return history for optimization")

    returns_clean = returns.dropna(axis=1, how="all").fillna(0)

    try:
        import riskfolio as rp

        port = rp.HCPortfolio(returns=returns_clean)
        w = port.optimization(
            model="HRP",
            codependence="pearson",
            rm=risk_measure,
            rf=0,
            linkage="single",
            max_k=10,
            leaf_order=True,
        )

        weights = w["weights"].to_dict() if "weights" in w else {}

        # Compute portfolio-level metrics
        port_returns = (returns_clean * pd.Series(weights)).sum(axis=1)
        expected_return = float(port_returns.mean() * 252)
        expected_risk = float(port_returns.std() * np.sqrt(252))
        sharpe = expected_return / expected_risk if expected_risk > 0 else 0

        # Risk contribution per asset
        risk_contrib = {}
        if hasattr(port, "risk_contributions"):
            try:
                rc = port.risk_contributions
                if rc is not None:
                    risk_contrib = rc.to_dict()
            except Exception:
                pass

        return OptimizationResult(
            weights={k: round(float(v), 4) for k, v in weights.items()},
            risk_contribution={k: round(float(v), 4) for k, v in risk_contrib.items()},
            expected_return=round(expected_return, 4),
            expected_risk=round(expected_risk, 4),
            sharpe_ratio=round(sharpe, 4),
        )
    except ImportError:
        logger.warning("riskfolio-lib not installed — using fallback equal-weight")
        n = len(returns_clean.columns)
        equal_w = {col: 1.0 / n for col in returns_clean.columns}
        port_returns = returns_clean.mean(axis=1)
        er = float(port_returns.mean() * 252)
        vol = float(port_returns.std() * np.sqrt(252))
        return OptimizationResult(
            weights=equal_w,
            risk_contribution={},
            expected_return=round(er, 4),
            expected_risk=round(vol, 4),
            sharpe_ratio=round(er / vol, 4) if vol > 0 else 0,
        )


# ── Helpers ────────────────────────────────────────────────────


def _compute_portfolio_value(
    holdings: dict[str, dict],
    proposed_ticker: str,
    proposed_quantity: float,
    proposed_price: float,
) -> float:
    """Compute total portfolio value including proposed trade."""
    total = 0.0
    ticker_in_holdings = False
    for sym, h in holdings.items():
        qty = h.get("quantity", 0)
        price = h.get("market_price", h.get("price", 0))
        if sym == proposed_ticker:
            qty += proposed_quantity
            price = proposed_price
            ticker_in_holdings = True
        total += qty * price
    if not ticker_in_holdings:
        total += proposed_quantity * proposed_price
    return total


def _max_drawdown(returns: pd.Series) -> float:
    """Compute maximum drawdown from a returns series."""
    cumulative = (1 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    return float(drawdown.min())


def _sharpe_ratio(returns: pd.Series, rf: float = 0.0) -> float:
    """Annualized Sharpe ratio from daily returns."""
    if returns.std() == 0:
        return 0.0
    return float((returns.mean() - rf / 252) / returns.std() * np.sqrt(252))


def returns_from_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Compute daily returns from price DataFrame (assets x dates)."""
    return prices.pct_change().dropna()


def returns_from_holdings(holdings: dict[str, dict], prices: dict[str, list[float]]) -> pd.DataFrame:
    """Build portfolio returns from holdings and historical prices.

    Args:
        holdings: {symbol: {weight, ...}}
        prices: {symbol: [price_series_oldest_to_newest]}
    """
    dfs = []
    for sym, price_list in prices.items():
        if sym in holdings and len(price_list) > 1:
            s = pd.Series(price_list, name=sym)
            dfs.append(s.pct_change().dropna())

    if not dfs:
        return pd.DataFrame()

    return pd.concat(dfs, axis=1)
