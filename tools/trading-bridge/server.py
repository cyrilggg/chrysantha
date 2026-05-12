"""
Chrysantha ↔ TradingAgents-CN Bridge Service

FastAPI server that wraps TradingAgents-CN's multi-agent analysis pipeline.
TradingAgents-CN natively supports Chinese A-share markets via AKShare/Tushare
data sources, eliminating TLS errors from yfinance/Finnhub news fetching.
"""
import os
import sys
import logging

# Add TradingAgents-CN to sys.path so we can import it without installing
TRADINGAGENTS_PATH = os.environ.get(
    "TRADINGAGENTS_PATH",
    "/tmp/TradingAgents",
)
if TRADINGAGENTS_PATH not in sys.path:
    sys.path.insert(0, TRADINGAGENTS_PATH)

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# ── Config ─────────────────────────────────────────────────────────

# LLM config (defaults for TradingAgents-CN)
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")
DEEP_THINK_MODEL = os.environ.get("DEEP_THINK_MODEL", "gpt-5.4")
QUICK_THINK_MODEL = os.environ.get("QUICK_THINK_MODEL", "gpt-5.4-mini")
BACKEND_URL = os.environ.get("BACKEND_URL") or None

# Analysis defaults
MAX_DEBATE_ROUNDS = int(os.environ.get("MAX_DEBATE_ROUNDS", "1"))
MAX_RISK_ROUNDS = int(os.environ.get("MAX_RISK_ROUNDS", "1"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading-bridge")

# ── Models ──────────────────────────────────────────────────────────


class AnalyzeRequest(BaseModel):
    ticker: str
    date: str  # YYYY-MM-DD
    debate_rounds: int = 1
    risk_rounds: int = 1


class PortfolioAnalyzeRequest(BaseModel):
    ticker: str
    date: str
    positions: list[dict] = []  # Portfolio context from chrysantha
    debate_rounds: int = 1
    risk_rounds: int = 1


class AnalyzeResponse(BaseModel):
    ticker: str
    date: str
    signal: str  # Buy/Overweight/Hold/Underweight/Sell
    decision: dict
    reports: dict
    error: str | None = None


# ── TradingAgents-CN wrapper ──────────────────────────────────────

_graph_instance = None

# Map TradingAgents-CN Chinese action labels to English for downstream consumers
_ACTION_MAP = {"买入": "Buy", "卖出": "Sell", "持有": "Hold"}


def get_graph():
    """Lazy-init TradingAgentsGraph with TradingAgents-CN native data sources."""
    global _graph_instance
    if _graph_instance is not None:
        return _graph_instance

    logger.info("Initializing TradingAgents-CN graph...")
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = LLM_PROVIDER
    config["deep_think_llm"] = DEEP_THINK_MODEL
    config["quick_think_llm"] = QUICK_THINK_MODEL
    if BACKEND_URL:
        config["backend_url"] = BACKEND_URL
    config["max_debate_rounds"] = MAX_DEBATE_ROUNDS
    config["max_risk_discuss_rounds"] = MAX_RISK_ROUNDS

    from tradingagents.graph.trading_graph import TradingAgentsGraph

    _graph_instance = TradingAgentsGraph(
        debug=False,
        config=config,
    )
    return _graph_instance


# ── App ─────────────────────────────────────────────────────────────

app = FastAPI(title="Chrysantha Trading Bridge (CN)", version="0.2.0")


@app.on_event("startup")
async def startup():
    # Pre-warm the graph
    get_graph()
    logger.info("TradingAgents-CN graph ready")


@app.get("/health")
async def health():
    return {"status": "ok", "provider": LLM_PROVIDER, "model": DEEP_THINK_MODEL}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    """Run full multi-agent analysis for a single ticker on a given date."""
    try:
        graph = get_graph()
        final_state, decision = graph.propagate(req.ticker, req.date)

        # TradingAgents-CN returns a dict with Chinese action labels
        action = decision.get("action", "持有")
        signal = _ACTION_MAP.get(action, action)

        return AnalyzeResponse(
            ticker=req.ticker,
            date=req.date,
            signal=signal,
            decision={
                "trader_plan": final_state.get("trader_investment_plan", ""),
                "investment_plan": final_state.get("investment_plan", ""),
                "final_decision": final_state.get("final_trade_decision", ""),
            },
            reports={
                "market": final_state.get("market_report", ""),
                "sentiment": final_state.get("sentiment_report", ""),
                "news": final_state.get("news_report", ""),
                "fundamentals": final_state.get("fundamentals_report", ""),
            },
        )
    except Exception as e:
        logger.exception("Analysis failed for %s on %s", req.ticker, req.date)
        return AnalyzeResponse(
            ticker=req.ticker,
            date=req.date,
            signal="Error",
            decision={},
            reports={},
            error=str(e),
        )


@app.post("/reflect")
async def reflect(ticker: str, date: str, position_return: float):
    """Record outcome and reflect on past decisions for this ticker."""
    graph = get_graph()
    graph.reflect_and_remember(position_return)
    return {"status": "reflected", "ticker": ticker, "date": date}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
