import os
from dataclasses import dataclass


@dataclass
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    input_dir: str
    output_dir: str
    order_type: str      # "market" or "limit"
    limit_buffer: float  # multiplier on ask for limit price, e.g. 1.05
    dry_run: bool
    discord_webhook_url: str  # optional; empty = no execution notifications
    options_feed: str         # "indicative" (free) or "opra" (paid)
    require_market_open: bool  # refuse to submit orders while the market is closed
    max_approval_age_hours: float  # refuse to execute a stale approved.json
    allow_market_fallback: bool    # if no ask quote, send a market order instead of skipping

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            alpaca_api_key=os.environ["ALPACA_API_KEY"],
            alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
            alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            input_dir=os.getenv("INPUT_DIR", "/tmp/gate"),
            output_dir=os.getenv("OUTPUT_DIR", "/tmp/executor"),
            order_type=os.getenv("ORDER_TYPE", "limit"),
            limit_buffer=float(os.getenv("LIMIT_PRICE_BUFFER", "1.05")),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            options_feed=os.getenv("ALPACA_OPTIONS_FEED", "indicative"),
            require_market_open=os.getenv("REQUIRE_MARKET_OPEN", "true").lower() == "true",
            max_approval_age_hours=float(os.getenv("MAX_APPROVAL_AGE_HOURS", "6")),
            allow_market_fallback=os.getenv("ALLOW_MARKET_FALLBACK", "false").lower() == "true",
        )
