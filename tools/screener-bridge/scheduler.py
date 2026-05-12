"""
APScheduler setup for cron-triggered macro and sector runs.

Caches results to Redis so on-demand /screen requests can skip
redundant computation during trading hours.
"""
from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis import Redis

logger = logging.getLogger("screener.scheduler")
scheduler = AsyncIOScheduler()

# ── Redis config ──────────────────────────────────────────────────────
_REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
_REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")


def _make_redis() -> Redis:
    """Create a Redis client from environment config."""
    kwargs: dict = {"host": _REDIS_HOST, "decode_responses": True}
    if _REDIS_PASSWORD:
        kwargs["password"] = _REDIS_PASSWORD
    return Redis(**kwargs)


# ── Scheduled jobs ────────────────────────────────────────────────────


async def scheduled_macro():
    """Run macro assessment and cache to Redis. Called by APScheduler."""
    from layers.macro import assess_macro

    redis = _make_redis()
    try:
        regime = await assess_macro(redis_client=redis, force_refresh=True)
        logger.info(
            "Scheduled macro: regime=%s confidence=%.2f reason=%s",
            regime.regime.value,
            regime.confidence,
            regime.reason,
        )
    except Exception:
        logger.exception("Scheduled macro failed")


async def scheduled_sector():
    """Run sector ranking using latest macro and cache to Redis. Called by APScheduler."""
    from layers.sector import rank_sectors

    try:
        ranking = await rank_sectors()
        logger.info(
            "Scheduled sector: %d top sectors ranked (first=%s score=%.2f)",
            len(ranking.top_sectors),
            ranking.top_sectors[0].sector if ranking.top_sectors else "none",
            ranking.top_sectors[0].score if ranking.top_sectors else 0.0,
        )
    except Exception:
        logger.exception("Scheduled sector failed")


# ── Lifecycle ─────────────────────────────────────────────────────────


def init_scheduler():
    """Register jobs and start the scheduler."""
    scheduler.add_job(
        scheduled_macro,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=30,
        id="macro",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        scheduled_sector,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=35,
        id="sector",
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("Scheduler started: macro@8:30, sector@8:35 Mon-Fri")


def shutdown_scheduler():
    """Gracefully shutdown the scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
