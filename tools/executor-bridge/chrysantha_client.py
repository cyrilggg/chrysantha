"""
Chrysantha (Ghostfolio) REST API client for executor-bridge.

Handles writing execution results back to chrysantha as Activities,
and reading portfolio holdings for position context.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("executor-bridge.chrysantha")

CHRYSANTHA_BASE_URL = os.environ.get(
    "CHRYSANTHA_BASE_URL", "http://ghostfolio:3333/api/v1"
)
CHRYSANTHA_ACCESS_TOKEN = os.environ.get("CHRYSANTHA_ACCESS_TOKEN", "")


class ChrysanthaClient:
    """HTTP client for chrysantha API (write-back + position context)."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=CHRYSANTHA_BASE_URL,
                headers={
                    "Authorization": f"Bearer {CHRYSANTHA_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def _post(self, path: str, json_data: dict) -> dict:
        client = await self._get_client()
        resp = await client.post(path, json=json_data)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=5),
    )
    async def _get(self, path: str) -> dict:
        client = await self._get_client()
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.json()

    # ── Activity/Order write-back ───────────────────────────────

    async def create_activity(
        self,
        symbol: str,
        data_source: str,
        order_type: str,  # "BUY" | "SELL"
        quantity: float,
        unit_price: float,
        fee: float = 0,
        currency: str = "CNY",
        date: str | None = None,
        account_id: str | None = None,
        comment: str | None = None,
    ) -> dict:
        """Create an activity (order) in chrysantha to reflect execution.

        Maps to POST /api/v1/activities.
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "currency": currency,
            "dataSource": data_source,
            "date": date,
            "fee": fee,
            "quantity": quantity,
            "symbol": symbol,
            "type": order_type,
            "unitPrice": unit_price,
        }

        if account_id:
            payload["accountId"] = account_id

        if comment:
            payload["comment"] = comment

        logger.info("Creating chrysantha activity: %s %s %s @ %s",
                     order_type, symbol, quantity, unit_price)
        result = await self._post("/activities", payload)
        logger.info("Activity created: id=%s", result.get("id"))
        return result

    # ── Holdings / Portfolio context ────────────────────────────

    async def get_holdings(self) -> dict:
        """Get current portfolio holdings from chrysantha.

        Returns the detailed portfolio holdings for position sizing context.
        """
        result = await self._get("/portfolio/holdings")
        return result

    async def get_accounts(self) -> list[dict]:
        """List accounts from chrysantha."""
        result = await self._get("/account")
        if isinstance(result, dict) and "accounts" in result:
            return result["accounts"]
        return result if isinstance(result, list) else []

    # ── Health ─────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Check chrysantha connectivity."""
        try:
            client = await self._get_client()
            resp = await client.get("/health", timeout=httpx.Timeout(5.0))
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


# Singleton
_chrysantha_client: ChrysanthaClient | None = None


def get_chrysantha_client() -> ChrysanthaClient:
    global _chrysantha_client
    if _chrysantha_client is None:
        _chrysantha_client = ChrysanthaClient()
    return _chrysantha_client
