import json
import logging

from google import genai
from google.genai import types

from consensus.config import Config
from consensus.models import ModelVote
from consensus.prompt import SYSTEM_PROMPT, build_user_message
from fetcher.models import MarketSnapshot

log = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client(cfg: Config) -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=cfg.google_api_key)
    return _client


async def call(snapshot: MarketSnapshot, cfg: Config) -> ModelVote | None:
    raw = ""
    try:
        response = await _get_client(cfg).aio.models.generate_content(
            model=cfg.gemini_model,
            contents=build_user_message(snapshot),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                max_output_tokens=4096,
            ),
        )
        raw = (response.text or "").strip()
        if not raw:
            log.warning("[%s] Gemini returned empty response", snapshot.ticker)
            return None
        return ModelVote(model=cfg.gemini_model, **json.loads(raw))
    except json.JSONDecodeError:
        log.warning("[%s] Gemini returned invalid JSON: %r", snapshot.ticker, raw)
        return None
    except Exception:
        log.exception("[%s] Gemini call failed", snapshot.ticker)
        return None
