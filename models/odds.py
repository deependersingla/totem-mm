from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

OddsProvider = Literal["betfair", "oddsapi", "polymarket"]
OddsSide = Literal["back", "lay"]


class Event(BaseModel):
    sport_key: str
    name: str
    start_time: datetime
    provider_ids: dict[OddsProvider, str] = Field(default_factory=dict)


class Market(BaseModel):
    market_type: str = Field(description="e.g. match_winner, h2h")
    provider_ids: dict[OddsProvider, str] = Field(default_factory=dict)


class Outcome(BaseModel):
    name: str
    provider_ids: dict[OddsProvider, str] = Field(default_factory=dict)


class OddsQuote(BaseModel):
    market_ref: str = Field(description="provider-specific market id")
    outcome_name: str
    price: Decimal
    side: OddsSide = "back"
    size: Decimal | None = None
    provider: OddsProvider
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class MarketWithOdds(BaseModel):
    event: Event
    market: Market
    outcomes: list[Outcome] = Field(default_factory=list)
    quotes: list[OddsQuote] = Field(default_factory=list)
