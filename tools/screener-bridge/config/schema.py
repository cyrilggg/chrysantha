"""Pydantic schema for preference.yaml validation."""
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    max_single_position: float = 0.08
    max_sector_concentration: float = 0.3
    min_market_cap_cny: float = 5e9


class HoldingDays(BaseModel):
    min: int = 5
    max: int = 20


class MarketsConfig(BaseModel):
    cn: bool = True
    hk: bool = True
    us: bool = True


class PreferenceConfig(BaseModel):
    risk: RiskConfig = RiskConfig()
    blacklist_sectors: list[str] = []
    preferred_holding_days: HoldingDays = HoldingDays()
    markets: MarketsConfig = MarketsConfig()

    @classmethod
    def load(cls, path: str | Path) -> "PreferenceConfig":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    def save(self, path: str | Path):
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.model_dump(), f, allow_unicode=True, default_flow_style=False)
