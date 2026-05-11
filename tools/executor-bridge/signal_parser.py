"""
Parse TradingAgents trader_plan markdown into executable order fields.

The TradingAgents TraderProposal model renders to markdown like:

    FINAL TRANSACTION PROPOSAL: **BUY**

    **Action**: Buy
    **Entry Price**: 1850.00
    **Stop Loss**: 1820.00
    **Position Sizing**: 5% of portfolio

This parser extracts actionable fields via regex and maps them to
vnpy-compatible order parameters.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("executor-bridge.signal_parser")

# Patterns for each field in the markdown
PATTERNS = {
    "proposal_line": re.compile(
        r"FINAL\s*TRANSACTION\s*PROPOSAL\s*:\s*\*{0,2}(BUY|SELL|HOLD)\*{0,2}",
        re.IGNORECASE,
    ),
    "action": re.compile(
        r"\*{0,2}Action\*{0,2}\s*:\s*(Buy|Sell|Hold|Overweight|Underweight)",
        re.IGNORECASE,
    ),
    "entry_price": re.compile(
        r"\*{0,2}Entry\s*Price\*{0,2}\s*:\s*([\d,.]+)",
        re.IGNORECASE,
    ),
    "stop_loss": re.compile(
        r"\*{0,2}Stop\s*Loss\*{0,2}\s*:\s*([\d,.]+)",
        re.IGNORECASE,
    ),
    "position_sizing_pct": re.compile(
        r"\*{0,2}Position\s*Siz(?:e|ing)\*{0,2}\s*:\s*([\d,.]+)\s*%",
        re.IGNORECASE,
    ),
    "position_sizing_shares": re.compile(
        r"\*{0,2}Position\s*Siz(?:e|ing)\*{0,2}\s*:\s*([\d,.]+)\s*(?:shares|股)",
        re.IGNORECASE,
    ),
    "price_target": re.compile(
        r"\*{0,2}(?:Price\s*)?Target\*{0,2}\s*:\s*([\d,.]+)",
        re.IGNORECASE,
    ),
    "time_horizon": re.compile(
        r"\*{0,2}Time\s*Horizon\*{0,2}\s*:\s*(.+)",
        re.IGNORECASE,
    ),
    "rating": re.compile(
        r"\*{0,2}Rating\*{0,2}\s*:\s*(Buy|Sell|Hold|Overweight|Underweight)",
        re.IGNORECASE,
    ),
    "rating_final_decision": re.compile(
        r"rating.*?[:\-][\s*]*(\w+)",
        re.IGNORECASE,
    ),
}


@dataclass
class ParsedTraderPlan:
    """Extracted actionable fields from a TradingAgents decision."""
    action: str = "Hold"  # "Buy" | "Sell" | "Hold"
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    position_sizing_pct: Optional[float] = None
    position_sizing_shares: Optional[float] = None
    price_target: Optional[float] = None
    time_horizon: Optional[str] = None
    rating: Optional[str] = None
    raw_trader_plan: str = ""
    raw_final_decision: str = ""
    warnings: list[str] = field(default_factory=list)


def parse_decision(decision: dict, signal: str) -> ParsedTraderPlan:
    """Parse TradingAgents AnalyzeResponse decision dict into ParsedTraderPlan.

    Args:
        decision: The 'decision' field from AnalyzeResponse containing
                  trader_plan, investment_plan, final_decision markdown strings.
        signal: The processed signal string ("Buy"/"Sell"/"Hold"/etc.)

    Returns:
        ParsedTraderPlan with extracted actionable fields.
    """
    trader_plan = decision.get("trader_plan", "")
    final_decision = decision.get("final_decision", "")
    investment_plan = decision.get("investment_plan", "")

    # Combine all decision texts for parsing
    combined = f"{trader_plan}\n{final_decision}\n{investment_plan}"

    plan = ParsedTraderPlan(
        raw_trader_plan=trader_plan,
        raw_final_decision=final_decision,
    )

    # 1. Extract action from trader_plan proposal line or action field
    match = PATTERNS["proposal_line"].search(trader_plan)
    if match:
        plan.action = match.group(1).capitalize()
    else:
        match = PATTERNS["action"].search(combined)
        if match:
            plan.action = match.group(1).capitalize()

    # Fallback to signal from TradingAgents
    if plan.action == "Hold" and signal.lower() != "hold":
        plan.action = signal.capitalize()

    # 2. Extract entry price
    match = PATTERNS["entry_price"].search(trader_plan)
    if match:
        try:
            plan.entry_price = float(match.group(1).replace(",", ""))
        except ValueError:
            plan.warnings.append(f"Could not parse entry_price: {match.group(1)}")

    # 3. Extract stop loss
    match = PATTERNS["stop_loss"].search(trader_plan)
    if match:
        try:
            plan.stop_loss = float(match.group(1).replace(",", ""))
        except ValueError:
            plan.warnings.append(f"Could not parse stop_loss: {match.group(1)}")

    # 4. Extract position sizing (percentage preferred, shares as fallback)
    match = PATTERNS["position_sizing_pct"].search(combined)
    if match:
        try:
            plan.position_sizing_pct = float(match.group(1).replace(",", ""))
        except ValueError:
            plan.warnings.append(
                f"Could not parse position_sizing_pct: {match.group(1)}"
            )

    if plan.position_sizing_pct is None:
        match = PATTERNS["position_sizing_shares"].search(combined)
        if match:
            try:
                plan.position_sizing_shares = float(match.group(1).replace(",", ""))
            except ValueError:
                plan.warnings.append(
                    f"Could not parse position_sizing_shares: {match.group(1)}"
                )

    # 5. Extract price target from final_decision
    match = PATTERNS["price_target"].search(final_decision)
    if match:
        try:
            plan.price_target = float(match.group(1).replace(",", ""))
        except ValueError:
            pass

    # 6. Extract time horizon
    match = PATTERNS["time_horizon"].search(final_decision)
    if match:
        plan.time_horizon = match.group(1).strip()

    # 7. Extract rating
    match = PATTERNS["rating"].search(final_decision) or PATTERNS[
        "rating_final_decision"
    ].search(final_decision)
    if match:
        plan.rating = match.group(1).capitalize()

    # 8. Validate
    if plan.action == "Buy" or plan.action == "Sell":
        if plan.entry_price is None:
            plan.warnings.append(
                "No entry_price found in trader_plan; order will need price override"
            )
        if plan.position_sizing_pct is None and plan.position_sizing_shares is None:
            plan.warnings.append(
                "No position sizing found; order will need quantity override"
            )

    logger.info(
        "Parsed trader plan: action=%s entry=%.2f stop=%.2f sizing_pct=%.1f%% sizing_shares=%s warnings=%s",
        plan.action,
        plan.entry_price or 0,
        plan.stop_loss or 0,
        plan.position_sizing_pct or 0,
        plan.position_sizing_shares,
        plan.warnings,
    )

    return plan


def plan_to_order_params(
    plan: ParsedTraderPlan,
    user_quantity: Optional[float] = None,
    user_price: Optional[float] = None,
    user_order_type: str = "LIMIT",
    user_stop_loss: Optional[float] = None,
    portfolio_value: float = 0,
) -> dict:
    """Convert ParsedTraderPlan to vnpy order parameters.

    User overrides take precedence over parsed values.
    """
    # Determine direction
    direction_map = {
        "Buy": "long",
        "Sell": "short",
        "Overweight": "long",
        "Underweight": "short",
        "Hold": "long",  # Shouldn't reach here for Hold
    }
    direction = direction_map.get(plan.action, "long")

    # Price: user override > parsed entry_price > 0 (market)
    price = user_price or plan.entry_price or 0

    # Order type: MARKET if no price specified
    order_type = user_order_type
    if price == 0:
        order_type = "MARKET"

    # Quantity: user override > parsed shares > percentage of portfolio
    quantity = user_quantity
    if quantity is None:
        if plan.position_sizing_shares:
            quantity = plan.position_sizing_shares
        elif plan.position_sizing_pct and portfolio_value > 0:
            allocation = portfolio_value * (plan.position_sizing_pct / 100)
            if price > 0:
                quantity = allocation / price
            else:
                quantity = allocation  # Use allocation amount for MARKET orders

    # Stop loss: user override > parsed
    stop_loss = user_stop_loss or plan.stop_loss

    return {
        "direction": direction,
        "price": price,
        "quantity": round(quantity or 0, 2),
        "order_type": order_type,
        "stop_loss": stop_loss,
    }
