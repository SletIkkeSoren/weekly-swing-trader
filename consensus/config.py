import os
from dataclasses import dataclass


@dataclass
class Config:
    mistral_api_key: str
    google_api_key: str
    openai_api_key: str
    mistral_model: str
    gemini_model: str
    openai_model: str
    input_dir: str
    output_dir: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            mistral_api_key=os.environ["MISTRAL_API_KEY"],
            google_api_key=os.environ["GOOGLE_API_KEY"],
            openai_api_key=os.environ["OPENAI_API_KEY"],
            mistral_model=os.getenv("MISTRAL_MODEL", "mistral-small-2603"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            input_dir=os.getenv("INPUT_DIR", "/tmp/fetcher"),
            output_dir=os.getenv("OUTPUT_DIR", "/tmp/consensus"),
        )
