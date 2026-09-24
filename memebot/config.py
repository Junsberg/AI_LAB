from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
PARAMS_PATH = ROOT / "strategy" / "params.yaml"


class Settings(BaseSettings):
    """Secrets and environment. Loaded from .env; never from params.yaml."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    helius_api_key: str = ""
    database_url: str = ""
    mode: Literal["paper", "live"] = "paper"
    solana_private_key: str = ""
    rugcheck_api_key: str = ""
    gmgn_api_key: str = ""

    @property
    def helius_rpc(self) -> str:
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"

    @property
    def helius_ws(self) -> str:
        return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"


class RiskCaps(BaseModel):
    """Frozen by hook: `.claude/hooks/protect-risk.py` blocks edits to this block."""

    max_position_sol: float = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    max_daily_loss_sol: float = Field(gt=0)
    hard_stop_pct: float = Field(lt=0)  # e.g. -50 → exit at -50%


class ExitRules(BaseModel):
    take_initial_at_x: float = 2.0  # sell enough to recover cost at 2x
    initial_recover_fraction: float = 0.5  # fraction of position sold at take_initial
    trailing_stop_pct: float = 35.0  # from peak, after initial recovery
    holder_drop_pct: float = 25.0  # holders fall this % from peak → exit
    volume_dead_minutes: int = 45  # no buys for N minutes → exit
    deployer_sell_exit: bool = True
    kol_sell_exit: bool = True


class SignalWeights(BaseModel):
    lineage: float = 1.0
    kol_precursor: float = 1.0
    survivor: float = 0.8
    narrative: float = 0.5


class Thresholds(BaseModel):
    enter_score: float = 0.65
    lineage_reject_below: float = 0.2  # deployer cluster score gate
    min_liquidity_sol: float = 15.0
    max_top10_holder_pct: float = 35.0
    max_bundle_pct: float = 20.0


class Params(BaseModel):
    version: int
    risk: RiskCaps
    exits: ExitRules
    weights: SignalWeights
    thresholds: Thresholds


def load_params(path: Path = PARAMS_PATH) -> Params:
    with open(path, encoding="utf-8") as f:
        return Params.model_validate(yaml.safe_load(f))


settings = Settings()
