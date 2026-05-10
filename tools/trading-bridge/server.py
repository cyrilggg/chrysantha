"""
Chrysantha ↔ TradingAgents Bridge Service

FastAPI server that wraps TradingAgents' multi-agent analysis pipeline,
feeding it data from Chrysantha's database through a dynamically
registered data vendor.

Zero changes required to TradingAgents source code.
"""
import os
import sys
import logging
from pathlib import Path

# Add TradingAgents to sys.path so we can import it without installing
TRADINGAGENTS_PATH = os.environ.get(
    "TRADINGAGENTS_PATH",
    "/app/TradingAgents",
)
if TRADINGAGENTS_PATH not in sys.path:
    sys.path.insert(0, TRADINGAGENTS_PATH)

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from chrysantha_vendor import init_chrysantha_client, register_chrysantha_vendor

# ── Config ─────────────────────────────────────────────────────────

CHRYSANTHA_BASE_URL = os.environ.get(
    "GHOSTFOLIO_BASE_URL", "http://ghostfolio:3333/api/v1"
)
CHRYSANTHA_ACCESS_TOKEN = os.environ.get("GHOSTFOLIO_ACCESS_TOKEN", "")

# LLM config (defaults for TradingAgents)
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")
DEEP_THINK_MODEL = os.environ.get("DEEP_THINK_MODEL", "gpt-5.4")
QUICK_THINK_MODEL = os.environ.get("QUICK_THINK_MODEL", "gpt-5.4-mini")
BACKEND_URL = os.environ.get("BACKEND_URL") or None

# Analysis defaults
MAX_DEBATE_ROUNDS = int(os.environ.get("MAX_DEBATE_ROUNDS", "1"))
MAX_RISK_ROUNDS = int(os.environ.get("MAX_RISK_ROUNDS", "1"))
OUTPUT_LANGUAGE = os.environ.get("OUTPUT_LANGUAGE", "Chinese")

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


# ── TradingAgents wrapper ──────────────────────────────────────────

_graph_instance = None


def get_graph():
    """Lazy-init TradingAgentsGraph with chrysantha vendor registered."""
    global _graph_instance
    if _graph_instance is not None:
        return _graph_instance

    logger.info("Registering chrysantha data vendor...")
    config = {
        "llm_provider": LLM_PROVIDER,
        "deep_think_llm": DEEP_THINK_MODEL,
        "quick_think_llm": QUICK_THINK_MODEL,
        "backend_url": BACKEND_URL,
        "max_debate_rounds": MAX_DEBATE_ROUNDS,
        "max_risk_discuss_rounds": MAX_RISK_ROUNDS,
        "output_language": OUTPUT_LANGUAGE,
    }
    register_chrysantha_vendor(config)

    logger.info("Initializing TradingAgentsGraph...")
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    _graph_instance = TradingAgentsGraph(
        debug=False,
        config=config,
    )
    return _graph_instance


# ── App ─────────────────────────────────────────────────────────────

app = FastAPI(title="Chrysantha Trading Bridge", version="0.1.0")


@app.on_event("startup")
async def startup():
    if not CHRYSANTHA_ACCESS_TOKEN:
        logger.warning(
            "GHOSTFOLIO_ACCESS_TOKEN not set — chrysantha data vendor "
            "will fall back to yfinance for market data"
        )
    else:
        init_chrysantha_client(CHRYSANTHA_BASE_URL, CHRYSANTHA_ACCESS_TOKEN)
        logger.info("Chrysantha client initialized: %s", CHRYSANTHA_BASE_URL)

    # Pre-warm the graph
    get_graph()
    logger.info("TradingAgentsGraph ready")


@app.get("/health")
async def health():
    return {"status": "ok", "provider": LLM_PROVIDER, "model": DEEP_THINK_MODEL}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    """Run full multi-agent analysis for a single ticker on a given date."""
    try:
        graph = get_graph()
        final_state, decision = graph.propagate(req.ticker, req.date)

        return AnalyzeResponse(
            ticker=req.ticker,
            date=req.date,
            signal=decision,
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
