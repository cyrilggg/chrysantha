"""
③ Preference filter layer — pure rule engine driven by config/preference.yaml.

Rules (applied in order):
  1. Exclude blacklisted sectors
  2. Exclude stocks below minimum market cap (CNY)
  3. Enforce max sector concentration (truncate overweight sectors by market cap)
"""

from models import Market, ScreenedStock
from config.schema import PreferenceConfig


def apply_preferences(
    stocks: list[dict],
    config_path: str = "config/preference.yaml",
    total_capital: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """Apply preference rules and return (passed_stocks, excluded_stocks_with_reasons)."""

    config = PreferenceConfig.load(config_path)

    passed: list[dict] = []
    excluded: list[dict] = []

    # ── Rule 1: blacklist sectors ──────────────────────────────────────────
    blacklist: list[str] = config.blacklist_sectors
    for stock in stocks:
        sector = stock.get("sector", "")
        if sector in blacklist:
            excluded.append({**stock, "excluded_reason": f"blacklist_sector: {sector}"})
        else:
            passed.append(stock)

    # ── Rule 2: minimum market cap (CNY) ───────────────────────────────────
    min_cap: float = config.risk.min_market_cap_cny
    after_cap: list[dict] = []
    for stock in passed:
        market_cap = stock.get("market_cap", 0.0)
        if market_cap < min_cap:
            excluded.append({**stock, "excluded_reason": f"min_market_cap: {market_cap:.0f} < {min_cap:.0f}"})
        else:
            after_cap.append(stock)
    passed = after_cap

    # ── Rule 3: max sector concentration ───────────────────────────────────
    max_conc: float = config.risk.max_sector_concentration
    pool_size = len(passed)
    if pool_size == 0 or max_conc >= 1.0:
        return passed, excluded

    max_per_sector = max(1, int(max_conc * pool_size))

    # Group by sector
    by_sector: dict[str, list[dict]] = {}
    for stock in passed:
        sector = stock.get("sector", "其他")
        by_sector.setdefault(sector, []).append(stock)

    after_conc: list[dict] = []
    for sector, sector_stocks in by_sector.items():
        if len(sector_stocks) <= max_per_sector:
            after_conc.extend(sector_stocks)
        else:
            # Sort descending by market_cap, keep top N
            sorted_stocks = sorted(sector_stocks, key=lambda s: s.get("market_cap", 0.0), reverse=True)
            after_conc.extend(sorted_stocks[:max_per_sector])
            for s in sorted_stocks[max_per_sector:]:
                excluded.append({
                    **s,
                    "excluded_reason": (
                        f"sector_concentration: {sector} "
                        f"({len(sector_stocks)}/{pool_size} > {max_conc:.0%}, "
                        f"kept top {max_per_sector})"
                    ),
                })

    return after_conc, excluded


def get_active_markets(config_path: str = "config/preference.yaml") -> list[str]:
    """Return list of enabled market codes (e.g. ['cn', 'hk', 'us'])."""
    config = PreferenceConfig.load(config_path)
    markets_config = config.markets
    active: list[str] = []
    if markets_config.cn:
        active.append(Market.CN.value)
    if markets_config.hk:
        active.append(Market.HK.value)
    if markets_config.us:
        active.append(Market.US.value)
    return active
