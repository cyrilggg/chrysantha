"""
Pydantic models for executor-bridge.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ExecutionRequest(BaseModel):
    """Manual execution request from chrysantha frontend."""
    ticker: str
    data_source: str = "YAHOO"
    date: str  # YYYY-MM-DD
    signal: str  # "Buy"|"Sell"|"Overweight"|"Underweight"|"Hold"
    decision: dict = Field(default_factory=dict)
    reports: dict = Field(default_factory=dict)
    # User overrides (take precedence over parsed values)
    quantity: Optional[float] = None
    price: Optional[float] = None
    order_type: str = "LIMIT"  # "LIMIT" | "MARKET"
    stop_loss: Optional[float] = None
    # Target account
    account_id: Optional[str] = None
    # Dry-run mode
    dry_run: bool = False


class AutoExecuteRequest(BaseModel):
    """Auto-execution: trigger analysis then execute based on confidence."""
    ticker: str
    data_source: str = "YAHOO"
    date: str
    confidence_threshold: float = 0.7
    max_position_pct: float = 0.1
    debate_rounds: int = 1
    risk_rounds: int = 1
    account_id: Optional[str] = None
    dry_run: bool = False


class ExecutionResponse(BaseModel):
    execution_id: str
    status: str  # "pending"|"submitted"|"filled"|"cancelled"|"failed"|"skipped"
    ticker: str
    signal: str
    vnpy_order_id: Optional[str] = None
    order_type: str = "LIMIT"
    direction: str = "long"
    quantity: float = 0
    price: float = 0
    stop_loss: Optional[float] = None
    message: str = ""
    chrysantha_activity_id: Optional[str] = None
    error: Optional[str] = None


class ExecutionStatus(BaseModel):
    execution_id: str
    status: str
    vnpy_order_id: Optional[str] = None
    filled_quantity: float = 0
    filled_price: float = 0
    created_at: str = ""
    updated_at: str = ""


class HealthResponse(BaseModel):
    status: str
    vnpy: str  # "connected"|"unavailable"
    version: str = "0.1.0"


# ── Risk models (Phase 2) ──────────────────────────────────────


class RiskCheckRequest(BaseModel):
    ticker: str
    quantity: float
    price: float
    holdings: dict[str, dict] = Field(default_factory=dict)
    max_single_position: float = 0.20
    max_var_95: float = 0.02


class RiskCheckResponse(BaseModel):
    approved: bool
    current_weight: float = 0.0
    proposed_weight: float = 0.0
    max_single_position: float = 0.20
    var_95_daily: Optional[float] = None
    cvar_95_daily: Optional[float] = None
    current_hhi: Optional[float] = None
    proposed_hhi: Optional[float] = None
    warnings: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class OptimizationRequest(BaseModel):
    returns: dict[str, list[float]] = Field(default_factory=dict)
    method: str = "HRP"
    risk_measure: str = "CVaR"


class OptimizationResponse(BaseModel):
    weights: dict[str, float] = Field(default_factory=dict)
    risk_contribution: dict[str, float] = Field(default_factory=dict)
    expected_return: float = 0.0
    expected_risk: float = 0.0
    sharpe_ratio: float = 0.0
