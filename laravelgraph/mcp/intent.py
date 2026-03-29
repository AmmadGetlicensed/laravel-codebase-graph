"""Structured intent analysis for PHP symbols via LLM.

Reuses the multi-provider dispatch from summarize.py — adding a new provider
to PROVIDER_REGISTRY there automatically makes it available here too.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from laravelgraph.logging import get_logger
from laravelgraph.mcp.summarize import (
    PROVIDER_REGISTRY,
    _call_anthropic,
    _call_openai_compat,
    _get_api_key,
    _get_base_url,
    _get_model,
    _resolve_provider,
)

if TYPE_CHECKING:
    from laravelgraph.config import LLMConfig

logger = get_logger(__name__)

_MAX_SOURCE_CHARS = 4000

_INTENT_SYSTEM_PROMPT = (
    "You are a PHP/Laravel code analyzer. "
    "Analyze the provided method or class and return ONLY a valid JSON object. "
    "No markdown, no prose, no code fences — raw JSON only."
)

_INTENT_PROMPT_TEMPLATE = """\
You are a PHP/Laravel code analyzer. Analyze this method/class and return ONLY a valid JSON object.

PHP code:
{source}

Return this exact JSON structure (no other text, no markdown):
{{
  "purpose": "one concise sentence describing what this code does",
  "reads": ["list of Model.property or table.column accessed for reading"],
  "writes": ["list of Model.property or table.column mutated/saved"],
  "side_effects": ["events dispatched", "jobs queued", "emails sent", "cache cleared", etc.],
  "guards": ["business rules enforced: validation constraints, status checks, ownership checks"]
}}

Keep each list item under 80 characters. If a list is empty, return []."""


def _build_intent_prompt(source: str) -> str:
    capped = source[:_MAX_SOURCE_CHARS]
    return _INTENT_PROMPT_TEMPLATE.format(source=capped)


def _parse_intent_response(text: str) -> dict | None:
    """Extract and parse the JSON object from the LLM response.

    Strips any accidental markdown fences before parsing.
    Returns None if the response cannot be parsed into the expected shape.
    """
    cleaned = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if the model disobeys
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Drop opening fence line and closing fence line
        inner = [ln for ln in lines[1:] if ln.strip() != "```"]
        cleaned = "\n".join(inner).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Intent JSON parse error", error=str(exc), raw=text[:200])
        return None

    if not isinstance(data, dict):
        logger.warning("Intent response is not a JSON object", raw=text[:200])
        return None

    # Coerce all expected list fields to lists so callers can rely on the shape
    for field in ("reads", "writes", "side_effects", "guards"):
        if field not in data:
            data[field] = []
        elif not isinstance(data[field], list):
            data[field] = [str(data[field])]

    if "purpose" not in data:
        data["purpose"] = ""

    return data


def generate_intent(
    fqn: str,
    source: str,
    cfg: "LLMConfig",
) -> tuple[dict | None, str]:
    """Generate structured intent for a PHP symbol.

    Returns (intent_dict, model_used). Never raises — returns (None, error_msg) on failure.
    """
    if not cfg.enabled:
        return None, "summaries disabled"

    provider = _resolve_provider(cfg)
    if not provider:
        return None, "no provider configured"

    if provider not in PROVIDER_REGISTRY:
        logger.warning("Unknown provider", provider=provider)
        return None, f"unknown provider: {provider}"

    info = PROVIDER_REGISTRY[provider]
    model = _get_model(provider, cfg)
    if not model:
        logger.warning(
            "No model configured for provider",
            provider=provider,
            hint="run `laravelgraph configure`",
        )
        return None, f"no model configured for provider: {provider}"

    if not source or not source.strip():
        return None, "no source provided"

    prompt = _build_intent_prompt(source)
    api_key = _get_api_key(provider, cfg)
    base_url = _get_base_url(provider, cfg)

    raw: str | None = None
    if info["sdk"] == "anthropic":
        raw = _call_anthropic(prompt, api_key, model)
    else:
        raw = _call_openai_compat(prompt, api_key or "no-key", model, base_url)

    if not raw:
        return None, f"provider {provider} returned empty response"

    intent = _parse_intent_response(raw)
    if intent is None:
        return None, f"could not parse JSON from {provider} response"

    logger.debug("Intent generated", fqn=fqn, provider=provider, model=model)
    return intent, model
