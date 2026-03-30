import json
import logging

import google.generativeai as genai
from google.generativeai.types import GenerationConfig

from consensus.config import Config
from consensus.models import ModelVote
from consensus.prompt import SYSTEM_PROMPT, build_user_message
from fetcher.models import MarketSnapshot

log = logging.getLogger(__name__)

_model: genai.GenerativeModel | None = None


def _get_model(cfg: Config) -> genai.GenerativeModel:
    global _model
    if _model is None:
        genai.configure(api_key=cfg.google_api_key)
        _model = genai.GenerativeModel(
            model_name=cfg.gemini_model,
            system_instruction=SYSTEM_PROMPT,
            generation_config=GenerationConfig(response_mime_type="application/json", max_output_tokens=512),
        )
    return _model


async def call(snapshot: MarketSnapshot, cfg: Config) -> ModelVote | None:
    raw = ""
    try:
        response = await _get_model(cfg).generate_content_async(
            build_user_message(snapshot)
        )
        raw = response.text.strip()
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
