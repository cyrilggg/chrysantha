"""
vnpy WebTrader REST API client.

Manages JWT authentication, order placement, position queries,
and account queries against a vnpy_webtrader instance.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("executor-bridge.vnpy")

VNPY_BASE_URL = os.environ.get("VNPY_BASE_URL", "http://vnpy:8000")
VNPY_USERNAME = os.environ.get("VNPY_USERNAME", "admin")
VNPY_PASSWORD = os.environ.get("VNPY_PASSWORD", "")
TOKEN_REFRESH_MARGIN = 300  # Refresh token 5 minutes before expiry


class VnpyClient:
    """Async HTTP client for vnpy WebTrader REST API."""

    def __init__(self):
        self._token: str | None = None
        self._token_expiry: float = 0
        self._refresh_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=VNPY_BASE_URL,
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    async def _ensure_token(self):
        """Ensure we have a valid JWT token, refreshing if needed."""
        now = datetime.now(timezone.utc).timestamp()
        if self._token and (self._token_expiry - now) > TOKEN_REFRESH_MARGIN:
            return

        client = await self._get_client()
        resp = await client.post(
            "/token",
            data={"username": VNPY_USERNAME, "password": VNPY_PASSWORD},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data.get("access_token")
        if not self._token:
            raise RuntimeError("vnpy /token did not return access_token")

        # vnpy JWT typically has 'exp' claim; if not, default to 55 min
        try:
            import jwt
            payload = jwt.decode(self._token, options={"verify_signature": False})
            self._token_expiry = payload.get("exp", now + 3300)
        except Exception:
            self._token_expiry = now + 3300

        logger.info("vnpy token refreshed, expiry=%s", self._token_expiry)

    async def _auth_headers(self) -> dict:
        await self._ensure_token()
        return {"Authorization": f"Bearer {self._token}"}

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def _post(self, path: str, json_data: dict | None = None) -> dict:
        client = await self._get_client()
        headers = await self._auth_headers()
        resp = await client.post(path, json=json_data, headers=headers)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=5),
    )
    async def _get(self, path: str) -> dict:
        client = await self._get_client()
        headers = await self._auth_headers()
        resp = await client.get(path, headers=headers)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=5),
    )
    async def _delete(self, path: str) -> dict:
        client = await self._get_client()
        headers = await self._auth_headers()
        resp = await client.delete(path, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ── Health ─────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Check if vnpy is reachable and authenticated."""
        try:
            client = await self._get_client()
            resp = await client.get("/token", timeout=httpx.Timeout(5.0))
            return resp.status_code < 500
        except Exception:
            return False

    # ── Orders ─────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        direction: str,
        quantity: float,
        price: float,
        order_type: str = "LIMIT",
        exchange: str | None = None,
    ) -> dict:
        """Place an order on vnpy.

        Args:
            symbol: Trading symbol (e.g. "SH600519" for A-shares)
            direction: "long" or "short"
            quantity: Number of shares
            price: Limit price (ignored for MARKET orders)
            order_type: "LIMIT" or "MARKET"
            exchange: Exchange code (auto-detected from symbol prefix)
        """
        if exchange is None:
            exchange = _infer_exchange(symbol)

        payload = {
            "symbol": symbol,
            "exchange": exchange,
            "direction": direction,
            "offset": "OPEN",
            "type": order_type,
            "price": price,
            "volume": int(quantity),
        }
        logger.info("Placing order: %s", payload)
        result = await self._post("/order", payload)
        logger.info("Order placed: %s", result)
        return result

    async def cancel_order(self, vt_orderid: str) -> dict:
        """Cancel a pending order."""
        logger.info("Cancelling order: %s", vt_orderid)
        result = await self._delete(f"/order/{vt_orderid}")
        logger.info("Order cancelled: %s", result)
        return result

    async def query_orders(self) -> list[dict]:
        """Query all orders."""
        result = await self._get("/order")
        return result if isinstance(result, list) else result.get("data", [])

    async def query_order(self, vt_orderid: str) -> dict | None:
        """Query a single order by ID."""
        orders = await self.query_orders()
        for o in orders:
            if o.get("vt_orderid") == vt_orderid:
                return o
        return None

    # ── Positions ──────────────────────────────────────────────

    async def get_positions(self) -> list[dict]:
        """Get all positions."""
        result = await self._get("/position")
        return result if isinstance(result, list) else result.get("data", [])

    async def get_position(self, symbol: str) -> dict | None:
        """Get position for a specific symbol."""
        positions = await self.get_positions()
        for p in positions:
            if p.get("symbol") == symbol or p.get("vt_symbol", "").startswith(symbol):
                return p
        return None

    # ── Account ────────────────────────────────────────────────

    async def get_account(self) -> dict:
        """Get account summary (balance, available, frozen, etc.)."""
        result = await self._get("/account")
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and len(result) > 0:
            return result[0]
        return {}

    async def close(self):
        """Clean up HTTP client."""
        if self._refresh_task:
            self._refresh_task.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None


def _infer_exchange(symbol: str) -> str:
    """Infer exchange from symbol prefix."""
    upper = symbol.upper()
    if upper.startswith("SH"):
        return "SSE"
    if upper.startswith("SZ"):
        return "SZSE"
    return "SMART"


# Singleton
_vnpy_client: VnpyClient | None = None


def get_vnpy_client() -> VnpyClient:
    global _vnpy_client
    if _vnpy_client is None:
        _vnpy_client = VnpyClient()
    return _vnpy_client
