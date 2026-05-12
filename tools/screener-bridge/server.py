"""
Chrysantha Screener Bridge Service

FastAPI server providing four-layer stock screening pipeline:
① Macro regime → ② Sector rotation → ③ Preference filter → ④ Stock screening
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager

# Ensure the screener-bridge directory is on sys.path for direct execution
_screener_dir = os.path.dirname(os.path.abspath(__file__))
if _screener_dir not in sys.path:
    sys.path.insert(0, _screener_dir)

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
import uvicorn

from models import (
    MarketRegime,
    MacroRegime,
    SectorScore,
    SectorRanking,
    ScreenedStock,
    ScreeningResult,
    ScreenRequest,
    ErrorResponse,
)
from pipeline import run_pipeline, run_from_sector, run_from_screening
from layers.macro import assess_macro
from layers.sector import rank_sectors
from layers.preference import apply_preferences, get_active_markets
from config.schema import PreferenceConfig
from scheduler import init_scheduler, shutdown_scheduler, scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("screener-bridge")

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
CONFIG_PATH = os.environ.get("SCREENER_CONFIG_PATH", "config/preference.yaml")

# ── App lifecycle ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_scheduler()
    logger.info("Screener-bridge started")
    yield
    shutdown_scheduler()
    logger.info("Screener-bridge stopped")


app = FastAPI(
    title="Chrysantha Screener Bridge",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Routes ─────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Service health + cache status."""
    return {"status": "ok", "version": "0.1.0"}


@app.post("/screen", response_model=ScreeningResult)
async def screen(req: ScreenRequest):
    """Run full four-layer screening pipeline."""
    try:
        start = time.perf_counter()
        result = await run_pipeline(req)
        result.elapsed_ms = int((time.perf_counter() - start) * 1000)
        return result
    except Exception as e:
        logger.exception("Screening pipeline failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/macro", response_model=MacroRegime)
async def get_macro():
    """Latest macro regime assessment."""
    return await assess_macro(force_refresh=False)


@app.get("/sectors", response_model=SectorRanking)
async def get_sectors(market: str = None):
    """Latest sector ranking."""
    markets = [market] if market else None
    return await rank_sectors(markets=markets)


@app.post("/scheduler/run-macro")
async def trigger_macro():
    """Manually trigger macro assessment."""
    from scheduler import scheduled_macro

    await scheduled_macro()
    return {"status": "triggered"}


@app.post("/scheduler/run-sector")
async def trigger_sector():
    """Manually trigger sector ranking."""
    from scheduler import scheduled_sector

    await scheduled_sector()
    return {"status": "triggered"}


@app.get("/preferences")
async def get_preferences():
    """Read current preference config."""
    config = PreferenceConfig.load(CONFIG_PATH)
    return config.model_dump()


@app.put("/preferences")
async def update_preferences(data: dict):
    """Update preference config at runtime."""
    config = PreferenceConfig(**data)
    config.save(CONFIG_PATH)
    return {"status": "updated"}


# ── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
