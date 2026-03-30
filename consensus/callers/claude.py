import json
import logging

from anthropic import AsyncAnthropic

from consensus.config import Config
from consensus.models import ModelVote
from consensus.prompt import SYSTEM_PROMPT, build_user_message
from fetcher.models import MarketSnapshot

log = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None


def _get_client(cfg: Config) -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
    return _client


async def call(snapshot: MarketSnapshot, cfg: Config) -> ModelVote | None:
    try:
        message = await _get_client(cfg).messages.create(
            model=cfg.claude_model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_message(snapshot)}],
        )
        if message.stop_reason == "max_tokens":
            log.warning("[%s] Claude response truncated (max_tokens)", snapshot.ticker)
            return None
        raw = message.content[0].text.strip()
        if not raw:
            log.warning("[%s] Claude returned empty response", snapshot.ticker)
            return None
        return ModelVote(model=cfg.claude_model, **json.loads(raw))
    except json.JSONDecodeError:
        log.warning("[%s] Claude returned invalid JSON: %r", snapshot.ticker, raw)
        return None
    except Exception:
        log.exception("[%s] Claude call failed", snapshot.ticker)
        return None
