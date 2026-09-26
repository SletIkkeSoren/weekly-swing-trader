import os
from dataclasses import dataclass


@dataclass
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    stock_feed: str            # "iex" — the free plan refuses SIP bars younger than 15 min
    options_feed: str          # "indicative" (free) or "opra" (paid)
    risk_fraction: float       # premium spent per trade, fraction of current equity
    premium_min: float
    premium_max: float
    take_profit: float         # limit sell at entry × this
    max_quote_spread: float    # skip contracts whose bid/ask is wider than this
    max_day_trades: int        # PDT: round trips allowed per rolling 5 days under $25k
    state_dir: str
    dry_run: bool
    discord_webhook_url: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            alpaca_api_key=os.environ["ALPACA_API_KEY"],
            alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
            alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            stock_feed=os.getenv("ALPACA_DATA_FEED", "iex"),
            options_feed=os.getenv("ALPACA_OPTIONS_FEED", "indicative"),
            risk_fraction=float(os.getenv("RISK_FRACTION", "0.20")),
            premium_min=float(os.getenv("PREMIUM_MIN", "0.80")),
            premium_max=float(os.getenv("PREMIUM_MAX", "1.10")),
            take_profit=float(os.getenv("TAKE_PROFIT", "2.0")),
            max_quote_spread=float(os.getenv("MAX_QUOTE_SPREAD", "0.05")),
            max_day_trades=int(os.getenv("MAX_DAY_TRADES", "3")),
            state_dir=os.getenv("STATE_DIR", "/tmp/zerodte"),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        )
