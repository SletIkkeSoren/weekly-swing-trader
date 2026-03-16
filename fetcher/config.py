import os
from dataclasses import dataclass, field


@dataclass
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    tickers: list[str]
    lookback_days: int
    output_dir: str
    data_feed: str  # "iex" (free) or "sip" (paid)

    @classmethod
    def from_env(cls) -> "Config":
        raw_tickers = os.environ["TICKERS"]
        return cls(
            alpaca_api_key=os.environ["ALPACA_API_KEY"],
            alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
            # Default to paper trading sandbox
            alpaca_base_url=os.getenv(
                "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
            ),
            tickers=[t.strip() for t in raw_tickers.split(",") if t.strip()],
            # 252 trading days ≈ 1 year; needed for HV and long EMAs
            lookback_days=int(os.getenv("LOOKBACK_DAYS", "252")),
            output_dir=os.getenv("OUTPUT_DIR", "/tmp/fetcher"),
            data_feed=os.getenv("ALPACA_DATA_FEED", "iex"),
        )
