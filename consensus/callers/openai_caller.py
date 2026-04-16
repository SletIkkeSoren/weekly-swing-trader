import json
import logging

from openai import AsyncOpenAI

from consensus.config import Config
from consensus.models import ModelVote
from consensus.prompt import SYSTEM_PROMPT, build_user_message
from fetcher.models import MarketSnapshot

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client(cfg: Config) -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=cfg.openai_api_key)
    return _client


async def call(snapshot: MarketSnapshot, cfg: Config) -> ModelVote | None:
    try:
        response = await _get_client(cfg).chat.completions.create(
            model=cfg.openai_model,
            max_completion_tokens=512,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(snapshot)},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            log.warning("[%s] OpenAI returned empty response", snapshot.ticker)
            return None
        return ModelVote(model=cfg.openai_model, **json.loads(raw))
    except json.JSONDecodeError:
        log.warning("[%s] OpenAI returned invalid JSON: %r", snapshot.ticker, raw)
        return None
    except Exception:
        log.exception("[%s] OpenAI call failed", snapshot.ticker)
        return None
