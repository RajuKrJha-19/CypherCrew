"""Runtime AI configuration: which provider + model each task uses, resolved
from the admin-editable AISettings row with the AI_* env values as fallback.

Also the small provider catalog the admin screen renders. API keys live in the
environment only - this module reports whether each provider's key is present
(for the UI indicator + validation), never the key itself.
"""
from flask import current_app

# Only providers that have a built adapter are selectable. To offer another
# (Claude, Llama-via-Groq), add a row here and the matching provider adapter.
PROVIDERS = [
    {
        "key": "gemini",
        "label": "Google Gemini",
        "key_config": "GEMINI_API_KEY",
        "caption_models": ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "qa_models": ["gemini-2.5-pro", "gemini-2.5-flash"],
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "key_config": "OPENAI_API_KEY",
        "caption_models": ["gpt-5-mini", "gpt-4.1-mini"],
        "qa_models": ["gpt-5", "gpt-5-mini"],
    },
]
_BY_KEY = {p["key"]: p for p in PROVIDERS}

VALID_PROVIDERS = set(_BY_KEY)


def get_settings():
    """The single AISettings row, or None (then env defaults apply)."""
    from app.models import AISettings
    return AISettings.query.first()


def is_enabled():
    """AI available right now? The env AI_ENABLED master ANDed with the admin
    soft toggle. Used by routes + templates so the whole surface hides/refuses
    together."""
    if not current_app.config.get("AI_ENABLED"):
        return False
    row = get_settings()
    return True if row is None else bool(row.enabled)


def resolve(task_kind):
    """(provider_key, model) for a task ('qa', 'reply', or caption/alt-text).
    DB override first, then the AI_* env defaults.

    The model default is provider-aware: if the provider was overridden (in the
    DB or env) but no model given, we must NOT fall back to the global
    AI_*_MODEL env default - that is a Gemini id, and handing it to OpenAI
    would 404. Instead we use that provider's own first suggested model.

    'reply' (Google review replies) shares the caption env defaults - replies
    are short text like captions - but takes its own DB override when set, so
    an admin can put public replies on a stronger model than captions.
    """
    cfg = current_app.config
    row = get_settings()
    if task_kind == "qa":
        row_provider = getattr(row, "qa_provider", None)
        row_model = getattr(row, "qa_model", None)
        env_provider = cfg.get("AI_QA_PROVIDER") or cfg.get("AI_PROVIDER")
        env_model = cfg.get("AI_QA_MODEL")
        models_key = "qa_models"
    elif task_kind == "reply":
        row_provider = getattr(row, "reply_provider", None)
        row_model = getattr(row, "reply_model", None)
        env_provider = cfg.get("AI_CAPTION_PROVIDER") or cfg.get("AI_PROVIDER")
        env_model = cfg.get("AI_CAPTION_MODEL")
        models_key = "caption_models"
    else:
        row_provider = getattr(row, "caption_provider", None)
        row_model = getattr(row, "caption_model", None)
        env_provider = cfg.get("AI_CAPTION_PROVIDER") or cfg.get("AI_PROVIDER")
        env_model = cfg.get("AI_CAPTION_MODEL")
        models_key = "caption_models"

    provider = (row_provider or env_provider or "gemini").lower()

    if row_model:
        model = row_model
    elif provider == (env_provider or "").lower():
        # Provider matches its env default -> the env model default belongs to
        # it, so use it.
        model = env_model
    else:
        # Provider was overridden without a model -> use that provider's own
        # first suggested model (never the mismatched env default).
        entry = _BY_KEY.get(provider)
        suggestions = entry.get(models_key) if entry else None
        model = suggestions[0] if suggestions else env_model
    return provider, model


def key_present(provider_key):
    """Is the API key for this provider configured (in the environment)?"""
    entry = _BY_KEY.get(provider_key)
    return bool(entry and current_app.config.get(entry["key_config"]))


def catalog_for_ui():
    """Provider rows for the settings screen: label, whether the key is present,
    and suggested models per task."""
    return [
        {
            "key": p["key"],
            "label": p["label"],
            "has_key": key_present(p["key"]),
            "caption_models": p["caption_models"],
            "qa_models": p["qa_models"],
            # Replies are short text - the caption-tier suggestions fit.
            "reply_models": p["caption_models"],
        }
        for p in PROVIDERS
    ]
