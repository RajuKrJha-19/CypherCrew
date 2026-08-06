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
        # "-latest" aliases auto-track the current model (survive Google's model
        # retirements); concrete ids are there when you want to pin one.
        "caption_models": ["gemini-flash-latest", "gemini-flash-lite-latest",
                           "gemini-3.5-flash"],
        "qa_models": ["gemini-pro-latest", "gemini-flash-latest"],
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "key_config": "OPENAI_API_KEY",
        "caption_models": ["gpt-5-mini", "gpt-4.1-mini"],
        "qa_models": ["gpt-5", "gpt-5-mini"],
    },
    {
        "key": "claude",
        "label": "Anthropic Claude",
        "key_config": "ANTHROPIC_API_KEY",
        # Sonnet = the quality sweet spot for on-brand writing + careful QA;
        # Haiku is the cheap high-volume option; Opus for the hardest checks.
        "caption_models": ["claude-sonnet-5", "claude-haiku-4-5"],
        "qa_models": ["claude-sonnet-5", "claude-opus-5"],
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


# The individually toggleable AI features. `key` is the AISettings column stem
# (<key>_enabled) and the value shown on the admin screen; `label` + `hint`
# drive that screen. Order = display order.
FEATURES = [
    {"key": "caption", "label": "Captions & alt-text",
     "hint": "The “Generate caption” and “Alt-text” buttons in Social Studio, "
             "and the “AI draft” button in Engage share this model — but each "
             "has its own switch."},
    {"key": "qa", "label": "Media QA",
     "hint": "The “Check media” button on submitted deliverables."},
    {"key": "reply", "label": "Review replies",
     "hint": "Drafting and auto-reply for Google Business reviews."},
    {"key": "comment", "label": "Comment replies",
     "hint": "The “AI draft” button in the Engage comment inbox."},
]
FEATURE_KEYS = {f["key"] for f in FEATURES}


def feature_enabled(feature):
    """Is a specific AI feature usable right now? The global master
    (is_enabled) ANDed with that feature's own soft toggle. Unknown feature or
    no settings row -> falls back to the master alone (default on)."""
    if not is_enabled():
        return False
    if feature not in FEATURE_KEYS:
        return True
    row = get_settings()
    if row is None:
        return True
    return bool(getattr(row, f"{feature}_enabled", True))


def feature_states():
    """{feature_key: bool} for every feature, for the template layer so a
    disabled feature's buttons disappear. All False when the master is off."""
    if not is_enabled():
        return {f["key"]: False for f in FEATURES}
    row = get_settings()
    return {f["key"]: (True if row is None
                       else bool(getattr(row, f"{f['key']}_enabled", True)))
            for f in FEATURES}


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


def caption_prefs():
    """Workflow prefs for caption generation: tone, how many alternatives, and
    whether to append hashtags. DB row first, sane defaults otherwise."""
    row = get_settings()
    tone = (getattr(row, "caption_tone", None) or "").strip().lower() or None
    variations = getattr(row, "caption_variations", None)
    variations = 2 if variations is None else max(0, min(3, int(variations)))
    hashtags = getattr(row, "caption_hashtags", None)
    return {"tone": tone,
            "variations": variations,
            "hashtags": True if hashtags is None else bool(hashtags)}


def image_max_dim():
    """Longest-edge px images are downscaled to for the AI call (0 = off).
    DB override, else the env default."""
    row = get_settings()
    val = getattr(row, "image_max_dim", None)
    if val is None:
        val = current_app.config.get("AI_IMAGE_MAX_DIM", 1568)
    return max(0, int(val or 0))


def media_max_mb():
    row = get_settings()
    val = getattr(row, "media_max_mb", None)
    if val is None:
        val = current_app.config.get("AI_MEDIA_MAX_MB", 10)
    return max(1, int(val or 10))


def autoreply_config():
    """Effective Google-review auto-reply guardrails: the admin-editable
    AISettings values, each falling back to its env default when the row is
    absent. Read by is_auto_safe / auto_reply_run so the dashboard controls
    real behaviour."""
    cfg = current_app.config
    row = get_settings()

    def pick(attr, env_key, default):
        val = getattr(row, attr, None) if row is not None else None
        return val if val is not None else cfg.get(env_key, default)

    raw_block = (getattr(row, "gbp_blocklist", None) if row is not None else None)
    if not raw_block:
        raw_block = cfg.get("GBP_AUTOREPLY_BLOCKLIST", "") or ""
    blocklist = [w.strip().lower() for w in raw_block.split(",") if w.strip()]

    enabled = (bool(getattr(row, "gbp_autoreply_enabled", False))
               if row is not None and getattr(row, "gbp_autoreply_enabled", None) is not None
               else bool(cfg.get("GBP_AUTOREPLY_ENABLED")))
    return {
        "enabled": enabled,
        "min_rating": int(pick("gbp_min_rating", "GBP_AUTOREPLY_MIN_RATING", 4) or 4),
        "max_len": int(pick("gbp_max_len", "GBP_AUTOREPLY_MAX_TEXT_LEN", 200) or 200),
        "max_per_run": int(pick("gbp_max_per_run", "GBP_AUTOREPLY_MAX_PER_RUN", 10) or 10),
        "blocklist": blocklist,
    }


def comment_config():
    """Effective Engage comment auto-reply guardrails. Off unless ALL of: the
    env master (ENGAGE_AUTOREPLY_ENABLED), the AI 'comment' feature, and the
    admin switch are on. Shares the review blocklist as the safety net."""
    cfg = current_app.config
    row = get_settings()
    enabled = (bool(cfg.get("ENGAGE_AUTOREPLY_ENABLED"))
               and feature_enabled("comment")
               and bool(getattr(row, "comment_autoreply_enabled", False)))
    return {
        "enabled": enabled,
        "max_len": int(getattr(row, "comment_max_len", None) or 120),
        "max_per_post": int(getattr(row, "comment_max_per_post", None) or 5),
        "answer_questions": bool(
            getattr(row, "comment_answer_questions_enabled", False)),
        # Per-post question cap for ORGANIC posts (ad posts are exempt in the
        # scan - an evergreen ad's questions are always answered). Unset -> a
        # safe default of 15 rather than unlimited, so a fresh client isn't
        # wide open; an admin who deliberately sets 0 still gets unlimited.
        "question_max_per_post": (
            15 if getattr(row, "comment_question_max_per_post", None) is None
            else int(getattr(row, "comment_question_max_per_post") or 0)),
        "blocklist": autoreply_config()["blocklist"],
    }


def automod_config():
    """Effective spam auto-moderation config. Off unless ALL of: the env master
    (ENGAGE_AUTOMOD_ENABLED), the admin switch, AND a non-empty spam blocklist -
    the same blocklist-mandatory-to-enable safety the auto-reply path uses. The
    per-client opt-in (Client.comment_automod) is checked per comment. Has its
    OWN blocklist, separate from the auto-reply/review one."""
    cfg = current_app.config
    row = get_settings()

    raw_block = getattr(row, "spam_blocklist", None) if row is not None else None
    if not raw_block:
        raw_block = cfg.get("ENGAGE_SPAM_BLOCKLIST", "") or ""
    blocklist = [w.strip().lower() for w in raw_block.split(",") if w.strip()]

    # Abuse / profanity list: matched on EVERY comment (ad lane included) and
    # auto-hidden. Separate from spam so the two can be curated apart.
    raw_abuse = getattr(row, "abuse_blocklist", None) if row is not None else None
    if not raw_abuse:
        raw_abuse = cfg.get("ENGAGE_ABUSE_BLOCKLIST", "") or ""
    abuse_blocklist = [w.strip().lower() for w in raw_abuse.split(",") if w.strip()]

    admin_on = (bool(getattr(row, "comment_automod_enabled", False))
                if row is not None else False)
    hide_links = (bool(getattr(row, "spam_hide_links", True))
                  if row is not None and getattr(row, "spam_hide_links", None) is not None
                  else True)
    return {
        # blocklist-mandatory: never auto-hide with no explicit config. EITHER
        # the spam OR the abuse list being non-empty arms it. ANDs the AI master
        # switch too: "Enable AI assist" off must stop this unattended public
        # action like every other feature in the Suite.
        "enabled": (is_enabled() and bool(cfg.get("ENGAGE_AUTOMOD_ENABLED"))
                    and admin_on and bool(blocklist or abuse_blocklist)),
        "blocklist": blocklist,
        "abuse_blocklist": abuse_blocklist,
        "hide_links": hide_links,
        "max_per_run": int(getattr(row, "automod_max_per_run", None) or 20),
    }


def is_known_model(provider_key, task_kind, model):
    """Is `model` one of `provider_key`'s catalogued models for this task? Used
    only to WARN on the settings screen (never to block) - a brand-new model
    the admin types on purpose is fine, but a typo that would 404 at call time
    gets flagged. Blank model / unknown provider -> treated as known (no warn)."""
    entry = _BY_KEY.get(provider_key)
    if not entry or not model:
        return True
    key = "qa_models" if task_kind == "qa" else "caption_models"
    return model in entry.get(key, [])


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
