"""
Pydantic data models for screener-bridge.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MarketRegime(str, Enum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"


class Market(str, Enum):
    CN = "cn"
    HK = "hk"
    US = "us"


class MacroRegime(BaseModel):
    regime: MarketRegime
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    indicators: dict = {}
    suggested_exposure: float = Field(ge=0.0, le=1.0)
    stale: bool = False
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class SectorScore(BaseModel):
    sector: str
    market: Market
    score: float
    excess_return_5d: float
    fund_flow_rank: int
    volume_ratio: float


class SectorRanking(BaseModel):
    top_sectors: list[SectorScore]
    all_scores: list[SectorScore]
    generated_at: datetime
    regime_context: Optional[MacroRegime] = None


class ScreenedStock(BaseModel):
    symbol: str
    name: str
    market: Market
    sector: str
    score: float
    turnover_rate: float
    pe_ttm: float
    volume_ratio: float
    market_cap: float
    filters_passed: list[str]


class ScreeningResult(BaseModel):
    regime: Optional[MacroRegime] = None
    top_sectors: list[SectorScore] = []
    candidates: list[ScreenedStock] = []
    excluded_count: int = 0
    total_scanned: int = 0
    elapsed_ms: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class ScreenRequest(BaseModel):
    markets: list[Market] = [Market.CN, Market.HK, Market.US]
    sectors: list[str] = []
    start_from: str = "macro"
    top_n: int = Field(default=20, ge=5, le=50)


class ErrorResponse(BaseModel):
    error: str
    detail: str
    partial_results: Optional[ScreeningResult] = None
