import json
import logging

from mistralai import Mistral

from consensus.config import Config
from consensus.models import ModelVote
from consensus.prompt import SYSTEM_PROMPT, build_user_message
from fetcher.models import MarketSnapshot

log = logging.getLogger(__name__)

_client: Mistral | None = None


def _get_client(cfg: Config) -> Mistral:
    global _client
    if _client is None:
        _client = Mistral(api_key=cfg.mistral_api_key)
    return _client


async def call(snapshot: MarketSnapshot, cfg: Config) -> ModelVote | None:
    raw = ""
    try:
        response = await _get_client(cfg).chat.complete_async(
            model=cfg.mistral_model,
            max_tokens=512,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(snapshot)},
            ],
            response_format={"type": "json_object"},
        )
        if response.choices[0].finish_reason == "length":
            log.warning("[%s] Mistral response truncated (max_tokens)", snapshot.ticker)
            return None
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            log.warning("[%s] Mistral returned empty response", snapshot.ticker)
            return None
        return ModelVote(model=cfg.mistral_model, **json.loads(raw))
    except json.JSONDecodeError:
        log.warning("[%s] Mistral returned invalid JSON: %r", snapshot.ticker, raw)
        return None
    except Exception:
        log.exception("[%s] Mistral call failed", snapshot.ticker)
        return None
