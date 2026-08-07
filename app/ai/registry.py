"""Resolve the AI backend for a given task ('caption' vs 'qa'), honouring the
admin-editable settings (provider + model per task) with env fallback.

Fail-closed: raises AIDisabled when AI is off or the resolved backend can't be
built. Concrete providers are imported lazily so their SDKs stay inert until
actually selected.
"""
from flask import current_app

from app.ai.errors import AIDisabled

_KEY_CONFIG = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}


def get_provider(task_kind="caption"):
    cfg = current_app.config
    if not cfg.get("AI_ENABLED"):
        raise AIDisabled("AI assist is disabled.")

    # Caption drafting returns a RICH JSON object - a caption, a per-platform
    # caption for every channel, hashtags, SEO keywords, a first comment AND
    # alternative variations. On a long post that easily blows past a small
    # ceiling, and when the JSON is cut off mid-object it fails to parse and the
    # raw (truncated) JSON used to leak straight into the caption box. Give
    # caption a generous budget; QA / alt-text stay lean on the default.
    max_tokens = (cfg.get("AI_CAPTION_MAX_TOKENS", 4096)
                  if task_kind == "caption"
                  else cfg.get("AI_MAX_TOKENS", 1024))
    common = dict(
        max_tokens=max_tokens,
        timeout_s=cfg.get("AI_TIMEOUT_S", 30),
    )

    # Simulation short-circuits every real backend (no key, no network).
    if cfg.get("AI_SIMULATION_MODE"):
        from app.ai.providers.simulation import SimulationProvider
        return SimulationProvider(**common)

    from app.ai import settings as ai_settings
    provider_key, model = ai_settings.resolve(task_kind)
    api_key = cfg.get(_KEY_CONFIG.get(provider_key, ""))

    if provider_key == "gemini":
        from app.ai.providers.gemini import GeminiProvider
        return GeminiProvider(model=model, api_key=api_key, **common)
    if provider_key == "openai":
        from app.ai.providers.openai import OpenAIProvider
        return OpenAIProvider(model=model, api_key=api_key, **common)
    if provider_key == "claude":
        from app.ai.providers.claude import ClaudeProvider
        return ClaudeProvider(model=model, api_key=api_key, **common)

    # A llama-via-Groq adapter would slot in here once written.
    raise AIDisabled(f"AI provider '{provider_key}' is not available.")
