import os
from dataclasses import dataclass


@dataclass
class Config:
    anthropic_api_key: str
    google_api_key: str
    claude_model: str
    gemini_model: str
    gemini_model_2: str
    input_dir: str
    output_dir: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            google_api_key=os.environ["GOOGLE_API_KEY"],
            claude_model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            gemini_model_2=os.getenv("GEMINI_MODEL_2", "gemini-2.5-flash"),
            input_dir=os.getenv("INPUT_DIR", "/tmp/fetcher"),
            output_dir=os.getenv("OUTPUT_DIR", "/tmp/consensus"),
        )
