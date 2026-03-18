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
        )
