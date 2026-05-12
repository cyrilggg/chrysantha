"""
① Macro narrative / market regime layer.

Combines LLM reasoning with quantitative indicators to output a
MarketRegime assessment. Uses Redis caching with TTL until the next
trading day.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta
from typing import Optional

import anthropic
import yfinance as yf
from redis import Redis

from models import MacroRegime, MarketRegime

logger = logging.getLogger("screener.macro")

# ── Config from env ───────────────────────────────────────────────────
_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
_REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
_REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

# ── Redis key ─────────────────────────────────────────────────────────
_CACHE_KEY = "screener:macro:latest"


def _cache_key() -> str:
    return _CACHE_KEY


def _next_trading_day_ttl() -> int:
    """Seconds until the next trading day 9:00 AM (approximate).

    If before 9 AM today, TTL to today 9 AM; otherwise to tomorrow 9 AM.
    On weekends, uses Monday 9 AM.
    """
    now = datetime.now()
    today = now.date()
    weekday = today.weekday()  # Monday=0, Sunday=6

    nine_am = time(9, 0)
    nine_today = datetime.combine(today, nine_am)

    if now < nine_today and weekday < 5:
        # Before 9 AM on a trading day → target today 9 AM
        target = nine_today
    elif weekday == 4:  # Friday after 9 AM → Monday 9 AM
        target = datetime.combine(today + timedelta(days=3), nine_am)
    elif weekday == 5:  # Saturday → Monday 9 AM
        target = datetime.combine(today + timedelta(days=2), nine_am)
    elif weekday == 6:  # Sunday → Monday 9 AM
        target = datetime.combine(today + timedelta(days=1), nine_am)
    else:
        # Mon-Thu after 9 AM → tomorrow 9 AM
        target = datetime.combine(today + timedelta(days=1), nine_am)

    return int((target - now).total_seconds())


# ── Indicator fetch helpers ────────────────────────────────────────────


def _fetch_vix() -> dict:
    """Fetch VIX and compute spread vs 20-day MA. Returns empty dict on failure."""
    try:
        df = yf.download("^VIX", period="3mo", progress=False, auto_adjust=True)
        if df.empty or "Close" not in df.columns:
            logger.warning("VIX data empty")
            return {}
        closes = df["Close"].astype(float)
        vix = float(closes.iloc[-1])
        vix_ma20 = float(closes.rolling(20).mean().iloc[-1]) if len(closes) >= 20 else vix
        vix_spread = (vix - vix_ma20) / vix_ma20 if vix_ma20 else 0.0
        return {"vix": vix, "vix_ma20": vix_ma20, "vix_spread": vix_spread}
    except Exception:
        logger.exception("Failed to fetch VIX")
        return {}


def _fetch_csi300() -> dict:
    """Fetch CSI300 and compute position vs 250-day MA. Returns empty dict on failure."""
    try:
        df = yf.download("000300.SS", period="2y", progress=False, auto_adjust=True)
        if df.empty or "Close" not in df.columns:
            logger.warning("CSI300 data empty")
            return {}
        closes = df["Close"].astype(float)
        csi300 = float(closes.iloc[-1])
        csi300_ma250 = float(closes.rolling(250).mean().iloc[-1]) if len(closes) >= 250 else csi300
        csi300_pct = (csi300 - csi300_ma250) / csi300_ma250 if csi300_ma250 else 0.0
        return {"csi300": csi300, "csi300_ma250": csi300_ma250, "csi300_pct_vs_ma250": csi300_pct}
    except Exception:
        logger.exception("Failed to fetch CSI300")
        return {}


def _fetch_sentiment() -> Optional[dict]:
    """Fetch A-share sentiment score from AKShare. Returns None on failure."""
    try:
        import akshare as ak
        df = ak.stock_em_sentiment_score()
        if df is None or df.empty:
            logger.warning("Sentiment score empty")
            return None
        latest = df.iloc[-1]
        return {
            "score": float(latest.get("sentiment_score", 0)),
            "change": float(latest.get("sentiment_change", 0)),
            "date": str(latest.get("日期", "")),
        }
    except Exception:
        logger.exception("Failed to fetch A-share sentiment")
        return None


# ── LLM ────────────────────────────────────────────────────────────────


def _build_prompt(vix_data: dict, csi300_data: dict, sentiment: Optional[dict]) -> str:
    """Build the LLM system+user prompt from indicators."""
    vix = vix_data.get("vix", None)
    vix_ma20 = vix_data.get("vix_ma20", None)
    vix_spread = vix_data.get("vix_spread", None)

    csi300 = csi300_data.get("csi300", None)
    csi300_ma250 = csi300_data.get("csi300_ma250", None)
    csi300_pct = csi300_data.get("csi300_pct_vs_ma250", None)

    vix_line = f"VIX: {vix:.2f} (20-day MA: {vix_ma20:.2f}, spread: {vix_spread:+.1%})" if vix is not None else "VIX: unavailable"
    csi300_line = f"CSI300: {csi300:.2f} (250-day MA: {csi300_ma250:.2f}, position: {csi300_pct:+.1%})" if csi300 is not None else "CSI300: unavailable"

    if sentiment:
        sentiment_info = f"Score: {sentiment['score']}, Change: {sentiment['change']}, Date: {sentiment['date']}"
    else:
        sentiment_info = "unavailable"

    return (
        "You are a macro strategy analyst. Assess the current market environment based on the following data. "
        "Output ONLY valid JSON, no commentary.\n\n"
        "Macro indicators:\n"
        f"- {vix_line}\n"
        f"- {csi300_line}\n"
        f"- A-Share Sentiment: {sentiment_info}\n\n"
        'Classify the market as one of: risk_on, neutral, risk_off\n\n'
        "Output JSON:\n"
        '{"regime": "risk_on|neutral|risk_off", "confidence": 0.0-1.0, '
        '"reason": "Brief analysis in Chinese", "suggested_exposure": 0.0-1.0}'
    )


def _neutral_regime(reason: str = "LLM unavailable or indicators missing") -> MacroRegime:
    """Return a neutral low-confidence fallback regime."""
    return MacroRegime(
        regime=MarketRegime.NEUTRAL,
        confidence=0.3,
        reason=reason,
        indicators={},
        suggested_exposure=0.5,
        stale=True,
        generated_at=datetime.utcnow(),
    )


async def _call_claude(prompt: str) -> dict:
    """Call Anthropic Claude API, return parsed JSON dict. Runs in a thread."""
    client = anthropic.Anthropic(api_key=_ANTHROPIC_API_KEY)

    def _sync_call() -> str:
        msg = client.messages.create(
            model=_ANTHROPIC_MODEL,
            max_tokens=512,
            system="Output ONLY valid JSON. No markdown fences, no commentary.",
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    try:
        raw = await asyncio.to_thread(_sync_call)
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("\n```", 1)[0].strip()
        return json.loads(text)
    except Exception:
        logger.exception("Claude API call failed or response unparseable")
        raise


# ── Redis helpers ──────────────────────────────────────────────────────


def _redis_client(client: Optional[Redis] = None) -> Optional[Redis]:
    """Return the provided Redis client or create one from env."""
    if client is not None:
        return client
    try:
        kwargs = {"host": _REDIS_HOST, "decode_responses": True}
        if _REDIS_PASSWORD:
            kwargs["password"] = _REDIS_PASSWORD
        return Redis(**kwargs)
    except Exception:
        logger.warning("Cannot connect to Redis, caching disabled")
        return None


def _get_cached(redis_client: Redis) -> Optional[MacroRegime]:
    """Read cached MacroRegime from Redis."""
    try:
        raw = redis_client.get(_CACHE_KEY)
        if raw is None:
            return None
        data = json.loads(raw)
        data["generated_at"] = datetime.fromisoformat(data["generated_at"])
        data["stale"] = True  # mark as stale since it is from cache
        return MacroRegime(**data)
    except Exception:
        logger.exception("Failed to read cache")
        return None


def _set_cache(redis_client: Redis, regime: MacroRegime) -> None:
    """Write MacroRegime to Redis with TTL until next trading day."""
    try:
        payload = regime.model_dump(mode="json")
        ttl = _next_trading_day_ttl()
        redis_client.setex(_CACHE_KEY, ttl, json.dumps(payload))
        logger.info("Cached macro regime (TTL=%ds)", ttl)
    except Exception:
        logger.exception("Failed to write cache")


# ── Main entry ─────────────────────────────────────────────────────────


async def assess_macro(
    redis_client: Optional[Redis] = None,
    force_refresh: bool = False,
) -> MacroRegime:
    """Return current macro regime assessment. Uses cache unless force_refresh."""

    redis = _redis_client(redis_client)

    # ── Cache hit (non-forced) ───────────────────────────────────
    if redis is not None and not force_refresh:
        cached = _get_cached(redis)
        if cached is not None:
            logger.info("Returning stale cached macro regime")
            return cached

    # ── Fetch indicators in parallel ──────────────────────────────
    loop = asyncio.get_running_loop()
    vix_task = loop.run_in_executor(None, _fetch_vix)
    csi300_task = loop.run_in_executor(None, _fetch_csi300)
    sentiment_task = loop.run_in_executor(None, _fetch_sentiment)

    vix_data, csi300_data, sentiment = await asyncio.gather(vix_task, csi300_task, sentiment_task)

    # ── Call LLM ─────────────────────────────────────────────────
    prompt = _build_prompt(vix_data, csi300_data, sentiment)

    try:
        llm_response = await _call_claude(prompt)
    except Exception:
        logger.warning("LLM call failed, attempting stale cache fallback")
        if redis is not None:
            cached = _get_cached(redis)
            if cached is not None:
                return cached
        return _neutral_regime("LLM call failed")

    # ── Parse into MacroRegime ────────────────────────────────────
    regime_raw = llm_response.get("regime", "neutral")
    try:
        regime = MarketRegime(regime_raw)
    except ValueError:
        regime = MarketRegime.NEUTRAL

    confidence = max(0.0, min(1.0, float(llm_response.get("confidence", 0.5))))
    reason = str(llm_response.get("reason", "No reason provided"))
    suggested_exposure = max(0.0, min(1.0, float(llm_response.get("suggested_exposure", 0.5))))

    # If confidence < 0.6, halve suggested_exposure
    if confidence < 0.6:
        suggested_exposure *= 0.5

    indicators = {}
    if vix_data:
        indicators["vix_spread"] = vix_data.get("vix_spread")
    if csi300_data:
        indicators["csi300_pct_vs_ma250"] = csi300_data.get("csi300_pct_vs_ma250")
    if sentiment:
        indicators["sentiment_score"] = sentiment.get("score")

    macro = MacroRegime(
        regime=regime,
        confidence=confidence,
        reason=reason,
        indicators=indicators,
        suggested_exposure=suggested_exposure,
        stale=False,
        generated_at=datetime.utcnow(),
    )

    # ── Cache ─────────────────────────────────────────────────────
    if redis is not None:
        _set_cache(redis, macro)

    return macro
