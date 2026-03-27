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
        "models": [
            ("claude-haiku-4-5-20251001",  "Haiku 4.5 — fast & cheap (recommended)"),
            ("claude-sonnet-4-5",           "Sonnet 4.5 — balanced speed/quality"),
            ("claude-opus-4-5",             "Opus 4.5 — most capable"),
            ("claude-3-5-haiku-20241022",   "Claude 3.5 Haiku — prev-gen fast"),
            ("claude-3-5-sonnet-20241022",  "Claude 3.5 Sonnet — prev-gen balanced"),
        ],
    },
    # ── Cloud: OpenAI-compatible ─────────────────────────────────────────────
    "openai": {
        "sdk": "openai",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "label": "OpenAI",
        "local": False,
        "models": [
            ("gpt-4o-mini",   "GPT-4o Mini — fast & cheap (recommended)"),
            ("gpt-4o",        "GPT-4o — flagship"),
            ("gpt-4.1-mini",  "GPT-4.1 Mini — latest efficient"),
            ("gpt-4.1",       "GPT-4.1 — latest flagship"),
            ("o4-mini",       "o4-mini — reasoning model"),
        ],
    },
    "openrouter": {
        "sdk": "openai",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-3-5-haiku",
        "base_url": "https://openrouter.ai/api/v1",
        "label": "OpenRouter (200+ models)",
        "local": False,
        "models": [
            ("anthropic/claude-3-5-haiku",           "Claude 3.5 Haiku — fast & cheap (recommended)"),
            ("anthropic/claude-3.5-sonnet",          "Claude 3.5 Sonnet — balanced"),
            ("anthropic/claude-3-opus",              "Claude 3 Opus — most capable Claude"),
            ("google/gemini-flash-1.5",              "Gemini Flash 1.5 — fast & cheap"),
            ("google/gemini-2.0-flash",              "Gemini 2.0 Flash — latest Google"),
            ("meta-llama/llama-3.3-70b-instruct",    "Llama 3.3 70B — open-source"),
            ("mistralai/mistral-small",              "Mistral Small — efficient"),
            ("deepseek/deepseek-chat",               "DeepSeek Chat (V3) — strong coder"),
            ("qwen/qwen-2.5-72b-instruct",           "Qwen 2.5 72B — multilingual"),
            ("openai/gpt-4o-mini",                   "GPT-4o Mini via OpenRouter"),
        ],
    },
    "groq": {
        "sdk": "openai",
        "env_var": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "label": "Groq (ultra-fast inference)",
        "local": False,
        "models": [
            ("llama-3.3-70b-versatile",  "Llama 3.3 70B Versatile — best quality (recommended)"),
            ("llama-3.1-8b-instant",     "Llama 3.1 8B Instant — fastest"),
            ("llama-3.1-70b-versatile",  "Llama 3.1 70B Versatile — prev-gen large"),
            ("gemma2-9b-it",             "Gemma 2 9B — Google lightweight"),
            ("mixtral-8x7b-32768",       "Mixtral 8x7B — long context"),
            ("qwen-qwq-32b",             "Qwen QwQ 32B — reasoning"),
        ],
    },
    "mistral": {
        "sdk": "openai",
        "env_var": "MISTRAL_API_KEY",
        "default_model": "mistral-small-latest",
        "base_url": "https://api.mistral.ai/v1",
        "label": "Mistral AI",
        "local": False,
        "models": [
            ("mistral-small-latest",   "Mistral Small — efficient (recommended)"),
            ("mistral-medium-latest",  "Mistral Medium — balanced"),
            ("mistral-large-latest",   "Mistral Large — most capable"),
            ("codestral-latest",       "Codestral — code-specialised"),
            ("open-mistral-7b",        "Mistral 7B — open-weight"),
        ],
    },
    "deepseek": {
        "sdk": "openai",
        "env_var": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "label": "DeepSeek",
        "local": False,
        "models": [
            ("deepseek-chat",      "DeepSeek-V3 — strong all-rounder (recommended)"),
            ("deepseek-reasoner",  "DeepSeek-R1 — reasoning / chain-of-thought"),
        ],
    },
    "gemini": {
        "sdk": "openai",
        "env_var": "GEMINI_API_KEY",
        "default_model": "gemini-2.0-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "label": "Google Gemini",
        "local": False,
        "models": [
            ("gemini-2.0-flash",      "Gemini 2.0 Flash — fast & cheap (recommended)"),
            ("gemini-2.0-flash-lite", "Gemini 2.0 Flash Lite — lightest"),
            ("gemini-1.5-flash",      "Gemini 1.5 Flash — prev-gen fast"),
            ("gemini-1.5-flash-8b",   "Gemini 1.5 Flash 8B — smallest"),
            ("gemini-1.5-pro",        "Gemini 1.5 Pro — prev-gen capable"),
        ],
    },
    "xai": {
        "sdk": "openai",
        "env_var": "XAI_API_KEY",
        "default_model": "grok-3-mini",
        "base_url": "https://api.x.ai/v1",
        "label": "xAI (Grok)",
        "local": False,
        "models": [
            ("grok-3-mini",   "Grok 3 Mini — fast & cheap (recommended)"),
            ("grok-3",        "Grok 3 — flagship"),
            ("grok-3-fast",   "Grok 3 Fast — speed-optimised"),
            ("grok-2-1212",   "Grok 2 — prev-gen"),
        ],
    },
    "together": {
        "sdk": "openai",
        "env_var": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "base_url": "https://api.together.xyz/v1",
        "label": "Together AI",
        "local": False,
        "models": [
            ("meta-llama/Llama-3.3-70B-Instruct-Turbo",       "Llama 3.3 70B Turbo — best quality (recommended)"),
            ("meta-llama/Llama-3.1-8B-Instruct-Turbo",        "Llama 3.1 8B Turbo — fastest"),
            ("meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo", "Llama 3.1 405B Turbo — largest"),
            ("Qwen/Qwen2.5-72B-Instruct-Turbo",               "Qwen 2.5 72B Turbo — multilingual"),
            ("deepseek-ai/DeepSeek-V3",                        "DeepSeek V3 — strong coder"),
        ],
    },
    "fireworks": {
        "sdk": "openai",
        "env_var": "FIREWORKS_API_KEY",
        "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "label": "Fireworks AI",
        "local": False,
        "models": [
            ("accounts/fireworks/models/llama-v3p3-70b-instruct", "Llama 3.3 70B — best quality (recommended)"),
            ("accounts/fireworks/models/llama-v3p1-8b-instruct",  "Llama 3.1 8B — fastest"),
            ("accounts/fireworks/models/deepseek-v3",             "DeepSeek V3 — strong coder"),
            ("accounts/fireworks/models/qwen2p5-72b-instruct",    "Qwen 2.5 72B — multilingual"),
        ],
    },
    "perplexity": {
        "sdk": "openai",
        "env_var": "PERPLEXITY_API_KEY",
        "default_model": "sonar",
        "base_url": "https://api.perplexity.ai",
        "label": "Perplexity",
        "local": False,
        "models": [
            ("sonar",                "Sonar — fast & cheap (recommended)"),
            ("sonar-pro",            "Sonar Pro — more capable"),
            ("sonar-reasoning",      "Sonar Reasoning — chain-of-thought"),
            ("sonar-reasoning-pro",  "Sonar Reasoning Pro — most capable"),
        ],
    },
    "cerebras": {
        "sdk": "openai",
        "env_var": "CEREBRAS_API_KEY",
        "default_model": "llama3.1-8b",
        "base_url": "https://api.cerebras.ai/v1",
        "label": "Cerebras (fast inference)",
        "local": False,
        "models": [
            ("llama3.1-8b",    "Llama 3.1 8B — fastest (recommended)"),
            ("llama3.1-70b",   "Llama 3.1 70B — more capable"),
            ("llama-3.3-70b",  "Llama 3.3 70B — latest large"),
            ("qwen-3-32b",     "Qwen 3 32B — multilingual"),
        ],
    },
    "cohere": {
        "sdk": "openai",
        "env_var": "COHERE_API_KEY",
        "default_model": "command-r",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "label": "Cohere",
        "local": False,
        "models": [
            ("command-r",            "Command R — fast & cheap (recommended)"),
            ("command-r-plus",       "Command R+ — most capable"),
            ("command-r7b-12-2024",  "Command R7B — smallest"),
        ],
    },
    "novita": {
        "sdk": "openai",
        "env_var": "NOVITA_API_KEY",
        "default_model": "meta-llama/llama-3.1-8b-instruct",
        "base_url": "https://api.novita.ai/v3/openai",
        "label": "Novita AI",
        "local": False,
        "models": [
            ("meta-llama/llama-3.1-8b-instruct",   "Llama 3.1 8B — fast & cheap (recommended)"),
            ("meta-llama/llama-3.1-70b-instruct",  "Llama 3.1 70B — balanced"),
            ("meta-llama/llama-3.3-70b-instruct",  "Llama 3.3 70B — latest large"),
            ("Qwen/Qwen2.5-72B-Instruct",          "Qwen 2.5 72B — multilingual"),
            ("deepseek-ai/DeepSeek-V3",             "DeepSeek V3 — strong coder"),
        ],
    },
    "huggingface": {
        "sdk": "openai",
        "env_var": "HF_TOKEN",
        "default_model": "Qwen/Qwen2.5-Coder-32B-Instruct",
        "base_url": "https://api-inference.huggingface.co/v1",
        "label": "Hugging Face Inference API",
        "local": False,
        "models": [
            ("Qwen/Qwen2.5-Coder-32B-Instruct",       "Qwen 2.5 Coder 32B — code-focused (recommended)"),
            ("Qwen/Qwen2.5-72B-Instruct",             "Qwen 2.5 72B — general purpose"),
            ("meta-llama/Llama-3.1-8B-Instruct",      "Llama 3.1 8B — fast & small"),
            ("meta-llama/Llama-3.1-70B-Instruct",     "Llama 3.1 70B — balanced"),
            ("mistralai/Mistral-7B-Instruct-v0.3",    "Mistral 7B — lightweight"),
            ("microsoft/Phi-3.5-mini-instruct",        "Phi-3.5 Mini — very small"),
        ],
    },
    # ── Local / self-hosted ──────────────────────────────────────────────────
    "ollama": {
        "sdk": "openai",
        "env_var": None,
        "default_model": "",
        "base_url": "http://localhost:11434/v1",
        "label": "Ollama (local)",
        "local": True,
        "models": [
            ("qwen2.5-coder:7b",  "Qwen 2.5 Coder 7B — code-focused (recommended)"),
            ("llama3.2:3b",       "Llama 3.2 3B — very fast"),
            ("llama3.2:1b",       "Llama 3.2 1B — smallest"),
            ("llama3.1:8b",       "Llama 3.1 8B — balanced"),
            ("qwen2.5:7b",        "Qwen 2.5 7B — general purpose"),
            ("mistral:7b",        "Mistral 7B — lightweight"),
            ("phi3:mini",         "Phi-3 Mini — very small"),
        ],
    },
    "lmstudio": {
        "sdk": "openai",
        "env_var": None,
        "default_model": "",
        "base_url": "http://localhost:1234/v1",
        "label": "LM Studio (local)",
        "local": True,
        "models": [
            ("lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF",   "Llama 3.1 8B — balanced (recommended)"),
            ("lmstudio-community/Qwen2.5-7B-Instruct-GGUF",          "Qwen 2.5 7B — multilingual"),
            ("lmstudio-community/Qwen2.5-Coder-7B-Instruct-GGUF",    "Qwen 2.5 Coder 7B — code-focused"),
            ("bartowski/Phi-3.5-mini-instruct-GGUF",                  "Phi-3.5 Mini — very small"),
            ("lmstudio-community/Mistral-7B-Instruct-v0.3-GGUF",     "Mistral 7B — lightweight"),
        ],
    },
    "vllm": {
        "sdk": "openai",
        "env_var": None,
        "default_model": "",
        "base_url": "http://localhost:8000/v1",
        "label": "vLLM (self-hosted)",
        "local": True,
        "models": [
            ("meta-llama/Llama-3.1-8B-Instruct",   "Llama 3.1 8B — balanced (recommended)"),
            ("meta-llama/Llama-3.1-70B-Instruct",  "Llama 3.1 70B — more capable"),
            ("Qwen/Qwen2.5-7B-Instruct",           "Qwen 2.5 7B — multilingual"),
            ("Qwen/Qwen2.5-Coder-7B-Instruct",     "Qwen 2.5 Coder 7B — code-focused"),
            ("mistralai/Mistral-7B-Instruct-v0.3", "Mistral 7B — lightweight"),
        ],
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
