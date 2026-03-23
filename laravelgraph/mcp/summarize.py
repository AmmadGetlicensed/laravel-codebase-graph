"""Multi-provider LLM summary generation for PHP symbols.

Adding a new provider = one entry in PROVIDER_REGISTRY. All OpenAI-compatible
providers (everything except Anthropic) reuse the same _call_openai_compat().

Provider resolution order (auto mode): first cloud provider whose env var is set.
Local providers (ollama, lmstudio, vllm) must be explicitly selected via
provider="<name>" — they have no API key to detect.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from laravelgraph.logging import get_logger

if TYPE_CHECKING:
    from laravelgraph.config import SummaryConfig

logger = get_logger(__name__)

# ── Provider registry ─────────────────────────────────────────────────────────
# Each entry: sdk, env_var, default_model, base_url (None = sdk default), label
# local=True  → never auto-detected, must be explicitly set as provider
# sdk="anthropic" → uses native anthropic SDK
# sdk="openai"    → uses openai SDK (works for any OpenAI-compatible API)

PROVIDER_REGISTRY: dict[str, dict] = {
    # ── Cloud: proprietary SDKs ──────────────────────────────────────────────
    "anthropic": {
        "sdk": "anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-haiku-4-5-20251001",
        "base_url": None,
        "label": "Anthropic (Claude)",
        "local": False,
    },
    # ── Cloud: OpenAI-compatible ─────────────────────────────────────────────
    "openai": {
        "sdk": "openai",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "label": "OpenAI",
        "local": False,
    },
    "openrouter": {
        "sdk": "openai",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-haiku-3",
        "base_url": "https://openrouter.ai/api/v1",
        "label": "OpenRouter (200+ models)",
        "local": False,
    },
    "groq": {
        "sdk": "openai",
        "env_var": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "label": "Groq (ultra-fast inference)",
        "local": False,
    },
    "mistral": {
        "sdk": "openai",
        "env_var": "MISTRAL_API_KEY",
        "default_model": "mistral-small-latest",
        "base_url": "https://api.mistral.ai/v1",
        "label": "Mistral AI",
        "local": False,
    },
    "deepseek": {
        "sdk": "openai",
        "env_var": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "label": "DeepSeek",
        "local": False,
    },
    "gemini": {
        "sdk": "openai",
        "env_var": "GEMINI_API_KEY",
        "default_model": "gemini-2.0-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "label": "Google Gemini",
        "local": False,
    },
    "xai": {
        "sdk": "openai",
        "env_var": "XAI_API_KEY",
        "default_model": "grok-3-mini",
        "base_url": "https://api.x.ai/v1",
        "label": "xAI (Grok)",
        "local": False,
    },
    "together": {
        "sdk": "openai",
        "env_var": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "base_url": "https://api.together.xyz/v1",
        "label": "Together AI",
        "local": False,
    },
    "fireworks": {
        "sdk": "openai",
        "env_var": "FIREWORKS_API_KEY",
        "default_model": "accounts/fireworks/models/llama-v3p1-8b-instruct",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "label": "Fireworks AI",
        "local": False,
    },
    "perplexity": {
        "sdk": "openai",
        "env_var": "PERPLEXITY_API_KEY",
        "default_model": "sonar",
        "base_url": "https://api.perplexity.ai",
        "label": "Perplexity",
        "local": False,
    },
    "cerebras": {
        "sdk": "openai",
        "env_var": "CEREBRAS_API_KEY",
        "default_model": "llama3.1-8b",
        "base_url": "https://api.cerebras.ai/v1",
        "label": "Cerebras (fast inference)",
        "local": False,
    },
    "cohere": {
        "sdk": "openai",
        "env_var": "COHERE_API_KEY",
        "default_model": "command-r",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "label": "Cohere",
        "local": False,
    },
    "novita": {
        "sdk": "openai",
        "env_var": "NOVITA_API_KEY",
        "default_model": "meta-llama/llama-3.1-8b-instruct",
        "base_url": "https://api.novita.ai/v3/openai",
        "label": "Novita AI",
        "local": False,
    },
    "huggingface": {
        "sdk": "openai",
        "env_var": "HF_TOKEN",
        "default_model": "Qwen/Qwen2.5-Coder-32B-Instruct",
        "base_url": "https://api-inference.huggingface.co/v1",
        "label": "Hugging Face Inference API",
        "local": False,
    },
    # ── Local / self-hosted ──────────────────────────────────────────────────
    "ollama": {
        "sdk": "openai",
        "env_var": None,
        "default_model": "",
        "base_url": "http://localhost:11434/v1",
        "label": "Ollama (local)",
        "local": True,
    },
    "lmstudio": {
        "sdk": "openai",
        "env_var": None,
        "default_model": "",
        "base_url": "http://localhost:1234/v1",
        "label": "LM Studio (local)",
        "local": True,
    },
    "vllm": {
        "sdk": "openai",
        "env_var": None,
        "default_model": "",
        "base_url": "http://localhost:8000/v1",
        "label": "vLLM (self-hosted)",
        "local": True,
    },
}

_SYSTEM_PROMPT = (
    "You are a Laravel codebase intelligence engine. Write concise semantic summaries "
    "of PHP symbols for developer AI agents. "
    "Rules: 2-4 sentences maximum. Focus on WHAT it does and WHY it exists — "
    "not HOW (the code shows how). Mention key business concepts (payment, booking, "
    "authentication, notification, etc.). Mention side effects (emails sent, events "
    "fired, jobs queued, database writes, external API calls). Be specific and concrete "
    "— no generic filler like 'This is a method that handles...'. "
    "Return plain prose only, no markdown, no bullet points."
)


def _build_prompt(fqn: str, node_type: str, source: str, docblock: str, max_lines: int) -> str:
    parts: list[str] = [f"PHP {node_type}: `{fqn}`"]

    if docblock:
        try:
            from laravelgraph.mcp.explain import clean_docblock
            prose = clean_docblock(docblock)
            if prose:
                parts.append(f"PHPDoc description: {prose}")
        except Exception:
            pass

    if source:
        capped = "\n".join(source.splitlines()[:max_lines])
        parts.append(f"Source code:\n```php\n{capped}\n```")

    if len(parts) == 1:
        return ""

    return (
        "\n\n".join(parts)
        + "\n\nWrite a 2-4 sentence semantic summary of what this symbol does "
        "and why it exists in this Laravel application."
    )


def _call_anthropic(prompt: str, api_key: str, model: str) -> str | None:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except ImportError:
        logger.warning("anthropic package not installed — run `pip install anthropic`")
        return None
    except Exception as e:
        logger.warning("Anthropic summary generation failed", error=str(e))
        return None


def _call_openai_compat(prompt: str, api_key: str, model: str, base_url: str) -> str | None:
    try:
        import openai
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            max_tokens=256,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content.strip()
    except ImportError:
        logger.warning("openai package not installed — run `pip install openai`")
        return None
    except Exception as e:
        logger.warning("OpenAI-compatible summary generation failed", error=str(e))
        return None


def _get_api_key(provider: str, cfg: "SummaryConfig") -> str:
    """Return API key: config override first, then env var."""
    key = cfg.api_keys.get(provider, "")
    if key:
        return key
    env_var = PROVIDER_REGISTRY[provider].get("env_var")
    if env_var:
        return os.environ.get(env_var, "")
    return ""


def _get_model(provider: str, cfg: "SummaryConfig") -> str:
    return cfg.models.get(provider, "") or PROVIDER_REGISTRY[provider]["default_model"]


def _get_base_url(provider: str, cfg: "SummaryConfig") -> str:
    url = cfg.base_urls.get(provider, "") or PROVIDER_REGISTRY[provider].get("base_url", "")
    # Normalise: ensure /v1 suffix for local providers that omit it
    if url and provider in ("ollama", "lmstudio", "vllm"):
        url = url.rstrip("/")
        if not url.endswith("/v1"):
            url = f"{url}/v1"
    return url


def _resolve_provider(cfg: "SummaryConfig") -> str | None:
    """Return active provider name, or None.

    Explicit provider="<name>": returned as-is (local or cloud).
    provider="auto": scan cloud providers in registry order for a set key.
    Local providers are never auto-detected — they have no key to check.
    """
    if cfg.provider != "auto":
        return cfg.provider if cfg.provider else None

    for name, info in PROVIDER_REGISTRY.items():
        if info.get("local"):
            continue
        if _get_api_key(name, cfg):
            return name
    return None


def provider_status(cfg: "SummaryConfig") -> dict:
    """Visibility: which providers are configured, which are available."""
    active = _resolve_provider(cfg) if cfg.enabled else None
    providers = {}
    for name, info in PROVIDER_REGISTRY.items():
        api_key = _get_api_key(name, cfg)
        is_local = info.get("local", False)
        configured = (name == cfg.provider) if is_local else bool(api_key)
        providers[name] = {
            "label": info["label"],
            "configured": configured,
            "model": _get_model(name, cfg) if configured else "",
            "base_url": _get_base_url(name, cfg),
            "env_var": info.get("env_var"),
            "requires_key": not is_local,
            "local": is_local,
        }
    return {
        "enabled": cfg.enabled,
        "active_provider": active,
        "providers": providers,
    }


def generate_summary(
    fqn: str,
    node_type: str,
    source: str = "",
    docblock: str = "",
    summary_cfg: "SummaryConfig | None" = None,
) -> tuple[str | None, str]:
    """Generate a semantic summary via the configured LLM provider.

    Returns (summary_text_or_None, provider_name_used).
    Never raises — returns (None, "") on any failure.
    """
    if summary_cfg is None:
        from laravelgraph.config import SummaryConfig as _SC
        summary_cfg = _SC()

    if not summary_cfg.enabled:
        return None, ""

    provider = _resolve_provider(summary_cfg)
    if not provider:
        return None, ""

    if provider not in PROVIDER_REGISTRY:
        logger.warning("Unknown provider", provider=provider)
        return None, ""

    info = PROVIDER_REGISTRY[provider]
    model = _get_model(provider, summary_cfg)
    if not model:
        logger.warning("No model configured for provider", provider=provider,
                       hint="run `laravelgraph configure`")
        return None, ""

    prompt = _build_prompt(fqn, node_type, source, docblock, summary_cfg.max_source_lines)
    if not prompt:
        return None, ""

    api_key = _get_api_key(provider, summary_cfg)
    base_url = _get_base_url(provider, summary_cfg)

    text: str | None = None
    if info["sdk"] == "anthropic":
        text = _call_anthropic(prompt, api_key, model)
    else:
        text = _call_openai_compat(prompt, api_key or "no-key", model, base_url)

    if text:
        logger.debug("Summary generated", fqn=fqn, provider=provider, length=len(text))
        return text, provider

    return None, ""
