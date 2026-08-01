"""Resolve the configured AI backend for the current app.

Fail-closed: raises AIDisabled when AI is off or the backend can't be built,
so a caller never silently proceeds without AI. Concrete providers are
imported lazily so their SDKs stay inert until actually selected.
"""
from flask import current_app

from app.ai.errors import AIDisabled


def get_provider():
    cfg = current_app.config
    if not cfg.get("AI_ENABLED"):
        raise AIDisabled("AI assist is disabled.")

    common = dict(
        caption_model=cfg.get("AI_CAPTION_MODEL"),
        qa_model=cfg.get("AI_QA_MODEL"),
        max_tokens=cfg.get("AI_MAX_TOKENS", 1024),
        timeout_s=cfg.get("AI_TIMEOUT_S", 30),
    )

    # Simulation short-circuits every real backend (no key, no network).
    if cfg.get("AI_SIMULATION_MODE"):
        from app.ai.providers.simulation import SimulationProvider
        return SimulationProvider(**common)

    provider = (cfg.get("AI_PROVIDER") or "gemini").lower()
    if provider == "gemini":
        from app.ai.providers.gemini import GeminiProvider
        return GeminiProvider(api_key=cfg.get("GEMINI_API_KEY"), **common)

    # openai / claude adapters slot in here when AI_PROVIDER is switched.
    raise AIDisabled(f"AI provider '{provider}' is not available.")
