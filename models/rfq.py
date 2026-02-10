from datetime import datetime

from pydantic import BaseModel, Field


class PriceQuote(BaseModel):
    """Pricing decision produced by QuoteEngine, before submission to Polymarket."""

    token_id: str
    price: float  # 0 < price < 1 (USDC per token)
    side: str  # "BUY" or "SELL" — our side
    size: float  # in conditional tokens

    @property
    def notional_usdc(self) -> float:
        return self.price * self.size


class QuoteSubmission(BaseModel):
    """Lifecycle tracker for a single quote submitted to Polymarket."""

    request_id: str
    token_id: str
    price: float
    side: str  # "BUY" or "SELL"
    size: float  # in conditional tokens
    quote_id: str | None = None  # populated after successful submission
    status: str = "pending"  # pending | active | accepted | filled | cancelled | failed
    created_at: datetime = Field(default_factory=datetime.now)
    error: str | None = None

    @property
    def notional_usdc(self) -> float:
        return self.price * self.size
