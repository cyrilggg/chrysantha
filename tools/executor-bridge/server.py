"""
Chrysantha ↔ vnpy Executor Bridge Service

FastAPI server that bridges TradingAgents analysis results to vnpy
trade execution, with risk validation in between.

Flow:
  1. Receive analysis + execution request
  2. Parse TradingAgents decision into actionable order params
  3. (Phase 2) Validate position sizing with Riskfolio-Lib
  4. Submit order to vnpy
  5. Poll for fill status in background
  6. Write execution result back to chrysantha as Activity
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
from fastapi import FastAPI, HTTPException
import uvicorn

from chrysantha_client import get_chrysantha_client
from models import (
    AutoExecuteRequest,
    ExecutionRequest,
    ExecutionResponse,
    ExecutionStatus,
    HealthResponse,
    OptimizationRequest,
    OptimizationResponse,
    RiskCheckRequest,
    RiskCheckResponse,
)
from risk_engine import check_position_size, run_hrp_optimization
from signal_parser import parse_decision, plan_to_order_params
from vnpy_client import get_vnpy_client

# ── Config ─────────────────────────────────────────────────────

TRADING_BRIDGE_URL = os.environ.get("TRADING_BRIDGE_URL", "http://trading-bridge:8000")

EXECUTION_POLL_INTERVAL = int(os.environ.get("EXECUTION_POLL_INTERVAL_SEC", "10"))
EXECUTION_POLL_TIMEOUT = int(os.environ.get("EXECUTION_POLL_TIMEOUT_MIN", "5")) * 60
MAX_POLL_ATTEMPTS = EXECUTION_POLL_TIMEOUT // EXECUTION_POLL_INTERVAL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("executor-bridge")

# ── In-memory execution store ───────────────────────────────────

_executions: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Background fill monitoring ──────────────────────────────────

async def _monitor_fill(execution_id: str):
    """Background task: poll vnpy for order fill status, write back to chrysantha."""
    vnpy = get_vnpy_client()
    chrysantha = get_chrysantha_client()

    exec_data = _executions.get(execution_id)
    if not exec_data:
        return

    vnpy_order_id = exec_data.get("vnpy_order_id")
    if not vnpy_order_id:
        _executions[execution_id]["status"] = "failed"
        _executions[execution_id]["error"] = "No vnpy_order_id to monitor"
        return

    for attempt in range(MAX_POLL_ATTEMPTS):
        await asyncio.sleep(EXECUTION_POLL_INTERVAL)

        try:
            order = await vnpy.query_order(vnpy_order_id)
        except Exception as e:
            logger.warning("Poll attempt %d failed: %s", attempt + 1, e)
            continue

        if order is None:
            logger.warning("Order %s not found in vnpy (attempt %d)", vnpy_order_id, attempt + 1)
            continue

        status = order.get("status", "")
        traded_volume = order.get("traded", 0)
        traded_price = order.get("price", 0)

        _executions[execution_id]["vnpy_status"] = status
        _executions[execution_id]["filled_quantity"] = traded_volume
        _executions[execution_id]["filled_price"] = traded_price

        # vnpy order status: SUBMITTING → NOTTRADED → PARTTRADED → ALLTRADED
        #                     → CANCELLED → REJECTED
        if status in ("ALLTRADED",):
            _executions[execution_id]["status"] = "filled"
            logger.info("Order %s filled: qty=%s price=%s", vnpy_order_id, traded_volume, traded_price)

            # Write back to chrysantha
            try:
                ed = _executions[execution_id]
                activity = await chrysantha.create_activity(
                    symbol=ed["symbol"],
                    data_source=ed.get("data_source", "MANUAL"),
                    order_type=ed["order_type_chrysantha"],
                    quantity=ed["quantity"],
                    unit_price=traded_price or ed["price"],
                    date=_now_iso(),
                    account_id=ed.get("account_id"),
                    comment=f"executor-bridge: {ed['ticker']} {ed['signal']} | vnpy:{vnpy_order_id}",
                )
                _executions[execution_id]["chrysantha_activity_id"] = activity.get("id")
                logger.info("Chrysantha activity created: %s", activity.get("id"))
            except Exception as e:
                logger.exception("Failed to write activity to chrysantha: %s", e)
                _executions[execution_id]["error"] = f"Write-back failed: {e}"
            return

        elif status in ("CANCELLED", "REJECTED"):
            _executions[execution_id]["status"] = status.lower()
            _executions[execution_id]["error"] = order.get("status_msg", f"Order {status}")
            logger.warning("Order %s %s", vnpy_order_id, status)
            return

        elif status in ("PARTTRADED",):
            _executions[execution_id]["status"] = "partial"
            logger.info("Order %s partially filled: %s", vnpy_order_id, traded_volume)

    # Timeout
    _executions[execution_id]["status"] = "timeout"
    _executions[execution_id]["error"] = (
        f"Order not filled within {EXECUTION_POLL_TIMEOUT}s"
    )
    logger.warning("Order %s monitoring timed out", vnpy_order_id)


# ── App ─────────────────────────────────────────────────────────

app = FastAPI(title="Chrysantha Executor Bridge", version="0.1.0")


@app.on_event("startup")
async def startup():
    vnpy = get_vnpy_client()
    chrysantha = get_chrysantha_client()

    # Warm up vnpy auth
    try:
        await vnpy._ensure_token()
        logger.info("vnpy client initialized: %s", os.environ.get("VNPY_BASE_URL"))
    except Exception as e:
        logger.warning("vnpy not available at startup: %s", e)

    # Check chrysantha
    try:
        ok = await chrysantha.health_check()
        logger.info("Chrysantha client initialized (health=%s)", ok)
    except Exception as e:
        logger.warning("Chrysantha not available at startup: %s", e)

    logger.info("Executor-bridge ready")


@app.on_event("shutdown")
async def shutdown():
    await get_vnpy_client().close()
    await get_chrysantha_client().close()


@app.get("/health", response_model=HealthResponse)
async def health():
    vnpy = get_vnpy_client()
    vnpy_ok = await vnpy.health_check()
    return HealthResponse(
        status="ok" if vnpy_ok else "degraded",
        vnpy="connected" if vnpy_ok else "unavailable",
    )


@app.post("/execute", response_model=ExecutionResponse)
async def execute(req: ExecutionRequest):
    """Execute a trade based on TradingAgents analysis result.

    Manual mode: chrysantha frontend sends the full AnalyzeResponse
    plus optional user overrides. executor-bridge parses, validates,
    and submits the order to vnpy.
    """
    # 1. Parse the decision into executable fields
    plan = parse_decision(req.decision, req.signal)

    if plan.action == "Hold":
        execution_id = str(uuid.uuid4())[:12]
        _executions[execution_id] = {
            "execution_id": execution_id,
            "status": "skipped",
            "ticker": req.ticker,
            "signal": req.signal,
            "message": "Signal is Hold — no trade executed",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return ExecutionResponse(
            execution_id=execution_id,
            status="skipped",
            ticker=req.ticker,
            signal=req.signal,
            message="Signal is Hold — no trade executed",
        )

    # 2. Convert to order params (user overrides take precedence)
    order_params = plan_to_order_params(
        plan,
        user_quantity=req.quantity,
        user_price=req.price,
        user_order_type=req.order_type,
        user_stop_loss=req.stop_loss,
    )

    if order_params["quantity"] <= 0:
        execution_id = str(uuid.uuid4())[:12]
        _executions[execution_id] = {
            "execution_id": execution_id,
            "status": "skipped",
            "ticker": req.ticker,
            "signal": req.signal,
            "message": (
                "Quantity is zero — position sizing could not be determined. "
                f"Warnings: {plan.warnings}"
            ),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return ExecutionResponse(
            execution_id=execution_id,
            status="skipped",
            ticker=req.ticker,
            signal=req.signal,
            message=_executions[execution_id]["message"],
        )

    # 3. Dry run: skip actual vnpy call
    if req.dry_run:
        execution_id = str(uuid.uuid4())[:12]
        _executions[execution_id] = {
            "execution_id": execution_id,
            "status": "dry_run",
            "ticker": req.ticker,
            "signal": req.signal,
            "order_params": order_params,
            "parsed_plan": {
                "action": plan.action,
                "entry_price": plan.entry_price,
                "stop_loss": plan.stop_loss,
                "position_sizing_pct": plan.position_sizing_pct,
                "position_sizing_shares": plan.position_sizing_shares,
                "warnings": plan.warnings,
            },
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return ExecutionResponse(
            execution_id=execution_id,
            status="dry_run",
            ticker=req.ticker,
            signal=req.signal,
            direction=order_params["direction"],
            quantity=order_params["quantity"],
            price=order_params["price"],
            order_type=order_params["order_type"],
            stop_loss=order_params["stop_loss"],
            message=f"Dry run — would {plan.action} {req.ticker} "
                    f"{order_params['direction']} {order_params['quantity']} "
                    f"@{order_params['price']:.2f} [{order_params['order_type']}]",
        )

    # 4. Submit order to vnpy
    try:
        vnpy = get_vnpy_client()
        result = await vnpy.place_order(
            symbol=req.ticker,
            direction=order_params["direction"],
            quantity=order_params["quantity"],
            price=order_params["price"],
            order_type=order_params["order_type"],
        )
        vnpy_order_id = result.get("vt_orderid") or result.get("orderid", "")
    except Exception as e:
        logger.exception("vnpy order submission failed")
        execution_id = str(uuid.uuid4())[:12]
        _executions[execution_id] = {
            "execution_id": execution_id,
            "status": "failed",
            "ticker": req.ticker,
            "signal": req.signal,
            "error": str(e),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return ExecutionResponse(
            execution_id=execution_id,
            status="failed",
            ticker=req.ticker,
            signal=req.signal,
            direction=order_params["direction"],
            quantity=order_params["quantity"],
            price=order_params["price"],
            order_type=order_params["order_type"],
            error=str(e),
        )

    # 5. Store execution and start background monitoring
    execution_id = str(uuid.uuid4())[:12]
    _executions[execution_id] = {
        "execution_id": execution_id,
        "status": "submitted",
        "ticker": req.ticker,
        "symbol": req.ticker,
        "data_source": req.data_source,
        "signal": req.signal,
        "vnpy_order_id": vnpy_order_id,
        "direction": order_params["direction"],
        "quantity": order_params["quantity"],
        "price": order_params["price"],
        "order_type": order_params["order_type"],
        "stop_loss": order_params["stop_loss"],
        "order_type_chrysantha": "BUY" if order_params["direction"] == "long" else "SELL",
        "account_id": req.account_id,
        "filled_quantity": 0,
        "filled_price": 0,
        "vnpy_status": "",
        "chrysantha_activity_id": None,
        "error": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    asyncio.create_task(_monitor_fill(execution_id))

    return ExecutionResponse(
        execution_id=execution_id,
        status="submitted",
        ticker=req.ticker,
        signal=req.signal,
        vnpy_order_id=vnpy_order_id,
        direction=order_params["direction"],
        quantity=order_params["quantity"],
        price=order_params["price"],
        order_type=order_params["order_type"],
        stop_loss=order_params["stop_loss"],
        message=f"Order submitted to vnpy: {vnpy_order_id}",
    )


@app.post("/auto-execute", response_model=ExecutionResponse)
async def auto_execute(req: AutoExecuteRequest):
    """Auto-execute: call trading-bridge for analysis, then execute if confident.

    This endpoint bridges trading-bridge → executor-bridge in one call.
    """
    # 1. Call trading-bridge for analysis
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.post(
                f"{TRADING_BRIDGE_URL}/analyze",
                json={
                    "ticker": req.ticker,
                    "date": req.date,
                    "debate_rounds": req.debate_rounds,
                    "risk_rounds": req.risk_rounds,
                },
            )
            resp.raise_for_status()
            analysis = resp.json()
    except Exception as e:
        execution_id = str(uuid.uuid4())[:12]
        _executions[execution_id] = {
            "execution_id": execution_id,
            "status": "failed",
            "ticker": req.ticker,
            "signal": "Error",
            "error": f"Trading-bridge analysis failed: {e}",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return ExecutionResponse(
            execution_id=execution_id,
            status="failed",
            ticker=req.ticker,
            signal="Error",
            error=f"Analysis failed: {e}",
        )

    signal = analysis.get("signal", "Hold")

    # 2. Skip if hold or error
    if signal in ("Hold", "Error") and not analysis.get("error"):
        execution_id = str(uuid.uuid4())[:12]
        _executions[execution_id] = {
            "execution_id": execution_id,
            "status": "skipped",
            "ticker": req.ticker,
            "signal": signal,
            "message": f"Auto-execute skipped: signal is {signal}",
            "analysis": analysis,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        return ExecutionResponse(
            execution_id=execution_id,
            status="skipped",
            ticker=req.ticker,
            signal=signal,
            message=f"Signal is {signal} — no trade executed",
        )

    # 3. Build ExecutionRequest from analysis
    exec_req = ExecutionRequest(
        ticker=req.ticker,
        data_source=req.data_source,
        date=req.date,
        signal=signal,
        decision=analysis.get("decision", {}),
        reports=analysis.get("reports", {}),
        account_id=req.account_id,
        dry_run=req.dry_run,
    )

    return await execute(exec_req)


@app.get("/execution/{execution_id}", response_model=ExecutionStatus)
async def get_execution_status(execution_id: str):
    """Get the current status of an execution."""
    exec_data = _executions.get(execution_id)
    if not exec_data:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")

    return ExecutionStatus(
        execution_id=execution_id,
        status=exec_data["status"],
        vnpy_order_id=exec_data.get("vnpy_order_id"),
        filled_quantity=exec_data.get("filled_quantity", 0),
        filled_price=exec_data.get("filled_price", 0),
        created_at=exec_data.get("created_at", ""),
        updated_at=exec_data.get("updated_at", _now_iso()),
    )


@app.get("/execution/{execution_id}/result", response_model=ExecutionResponse)
async def get_execution_result(execution_id: str):
    """Get the full result of an execution (including errors)."""
    exec_data = _executions.get(execution_id)
    if not exec_data:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")

    return ExecutionResponse(
        execution_id=execution_id,
        status=exec_data["status"],
        ticker=exec_data.get("ticker", ""),
        signal=exec_data.get("signal", ""),
        vnpy_order_id=exec_data.get("vnpy_order_id"),
        direction=exec_data.get("direction", ""),
        quantity=exec_data.get("quantity", 0),
        price=exec_data.get("price", 0),
        order_type=exec_data.get("order_type", ""),
        stop_loss=exec_data.get("stop_loss"),
        message=exec_data.get("message", ""),
        chrysantha_activity_id=exec_data.get("chrysantha_activity_id"),
        error=exec_data.get("error"),
    )


# ── Risk endpoints (Phase 2) ────────────────────────────────────


@app.post("/risk/check", response_model=RiskCheckResponse)
async def risk_check(req: RiskCheckRequest):
    """Pre-trade risk validation.

    Accepts proposed trade details and current holdings, returns
    go/no-go decision with concentration and VaR metrics.
    """
    result = check_position_size(
        holdings=req.holdings,
        proposed_ticker=req.ticker,
        proposed_quantity=req.quantity,
        proposed_price=req.price,
        max_single_position=req.max_single_position,
        max_var_95=req.max_var_95,
    )
    return RiskCheckResponse(
        approved=result.approved,
        current_weight=result.current_weight,
        proposed_weight=result.proposed_weight,
        max_single_position=result.max_single_position_pct,
        var_95_daily=result.var_95_daily,
        cvar_95_daily=result.cvar_95_daily,
        current_hhi=result.current_herfindahl,
        proposed_hhi=result.proposed_herfindahl,
        warnings=result.warnings,
        metrics=result.metrics,
    )


@app.post("/risk/optimize", response_model=OptimizationResponse)
async def risk_optimize(req: OptimizationRequest):
    """Run portfolio optimization (HRP) and return target weights."""
    if not req.returns:
        raise HTTPException(status_code=400, detail="Returns data is required")

    prices_df = pd.DataFrame(req.returns)
    if prices_df.shape[1] < 2:
        raise HTTPException(
            status_code=400,
            detail="Need at least 2 assets with return history",
        )

    try:
        opt = run_hrp_optimization(
            returns=prices_df,
            method=req.method,
            risk_measure=req.risk_measure,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return OptimizationResponse(
        weights=opt.weights,
        risk_contribution=opt.risk_contribution,
        expected_return=opt.expected_return,
        expected_risk=opt.expected_risk,
        sharpe_ratio=opt.sharpe_ratio,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
