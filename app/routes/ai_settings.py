"""Admin AI-settings screen: pick the provider + model each task uses, and a
soft on/off toggle. Management-only. API keys are NEVER edited here - they stay
in the server environment; this screen only chooses provider + model.
"""
from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request,
    url_for,
)
from flask_login import current_user, login_required

from app.ai import settings as ai_settings
from app.extensions import db
from app.models import AISettings
from app.utils.permissions import can_manage_ai

ai_settings_bp = Blueprint("ai_settings", __name__, url_prefix="/admin/ai")


def _guard():
    # The blueprint is only registered when AI_ENABLED, but guard anyway so a
    # stale link never reaches an unauthorised user.
    if not current_app.config.get("AI_ENABLED"):
        abort(404)
    if not can_manage_ai(current_user):
        abort(403)


# Tones the caption UI offers (kept in sync with the composer's allow-list).
_TONES = {"professional", "punchy", "friendly", "premium", "festive"}


def _int_in(raw, default, lo, hi, allow_zero=False):
    """Parse an int and clamp it to [lo, hi]; default on junk. allow_zero keeps
    an explicit 0 (used as 'off')."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if allow_zero and v == 0:
        return 0
    return max(lo, min(hi, v))


def _get_or_create():
    row = AISettings.query.first()
    if row is None:
        row = AISettings(enabled=True)
        db.session.add(row)
        db.session.commit()
    return row


@ai_settings_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    _guard()
    row = _get_or_create()

    if request.method == "POST":
        row.enabled = bool(request.form.get("enabled"))
        # Per-feature switches. A checkbox absent from the POST = off.
        for feat in ai_settings.FEATURES:
            setattr(row, f"{feat['key']}_enabled",
                    bool(request.form.get(f"feature_{feat['key']}")))

        cap_provider = (request.form.get("caption_provider") or "").strip().lower()
        qa_provider = (request.form.get("qa_provider") or "").strip().lower()
        reply_provider = (request.form.get("reply_provider") or "").strip().lower()
        # Allow-list the provider; anything unknown clears the override (falls
        # back to the env default) rather than storing junk.
        row.caption_provider = (cap_provider
                                if cap_provider in ai_settings.VALID_PROVIDERS
                                else None)
        row.qa_provider = (qa_provider
                           if qa_provider in ai_settings.VALID_PROVIDERS
                           else None)
        row.reply_provider = (reply_provider
                              if reply_provider in ai_settings.VALID_PROVIDERS
                              else None)
        row.caption_model = (request.form.get("caption_model") or "").strip()[:120] or None
        row.qa_model = (request.form.get("qa_model") or "").strip()[:120] or None
        row.reply_model = (request.form.get("reply_model") or "").strip()[:120] or None
        # Monthly budget cap (USD). Blank / invalid / negative -> 0 (no cap).
        try:
            budget = float(request.form.get("monthly_budget_usd") or 0)
        except (TypeError, ValueError):
            budget = 0.0
        row.monthly_budget_usd = max(0.0, budget)

        # -- Caption behaviour --
        tone = (request.form.get("caption_tone") or "").strip().lower()
        row.caption_tone = tone if tone in _TONES else None
        row.caption_variations = _int_in(request.form.get("caption_variations"), 2, 0, 3)
        row.caption_hashtags = bool(request.form.get("caption_hashtags"))

        # -- Image / performance --
        row.image_max_dim = _int_in(request.form.get("image_max_dim"),
                                    1568, 512, 4096, allow_zero=True)
        row.media_max_mb = _int_in(request.form.get("media_max_mb"), 10, 1, 50)

        # -- Review auto-reply guardrails (hard safety floors enforced) --
        block = (request.form.get("gbp_blocklist") or "").strip()
        row.gbp_min_rating = _int_in(request.form.get("gbp_min_rating"), 4, 3, 5)
        row.gbp_max_len = _int_in(request.form.get("gbp_max_len"), 200, 20, 1000)
        row.gbp_max_per_run = _int_in(request.form.get("gbp_max_per_run"), 10, 1, 100)
        row.gbp_blocklist = block or None
        want_auto = bool(request.form.get("gbp_autoreply_enabled"))
        # Never enable unattended public auto-send without a blocklist net.
        eff_block = block or (current_app.config.get("GBP_AUTOREPLY_BLOCKLIST") or "")
        if want_auto and not eff_block.strip():
            want_auto = False
            flash("Auto-reply needs a blocklist first (safety) — add words, "
                  "then turn it on.", "warning")
        row.gbp_autoreply_enabled = want_auto

        row.updated_by_id = current_user.id
        db.session.commit()

        # Audit trail for the unattended-public-reply guardrails.
        current_app.logger.info(
            "[ai-settings] review auto-reply saved by user=%s: enabled=%s "
            "min_rating=%s max_len=%s per_run=%s blocklist_words=%s",
            current_user.id, row.gbp_autoreply_enabled, row.gbp_min_rating,
            row.gbp_max_len, row.gbp_max_per_run,
            len([w for w in (row.gbp_blocklist or "").split(",") if w.strip()]))

        # Warn (don't block) on a model that isn't catalogued for its provider:
        # a typo would otherwise fail only later, at call time, with a 404.
        for task, prov in (("caption", row.caption_provider),
                           ("qa", row.qa_provider),
                           ("reply", row.reply_provider)):
            model = getattr(row, f"{task}_model", None)
            if prov and model and not ai_settings.is_known_model(prov, task, model):
                flash(f"Heads up: “{model}” isn’t in our known list for "
                      f"{prov} — it’ll be used as-is, so double-check the "
                      "spelling.", "warning")

        flash("AI settings saved.", "success")
        return redirect(url_for("ai_settings.index"))

    from app.ai import usage as ai_usage
    cfg = current_app.config
    cap_provider, cap_model = ai_settings.resolve("caption")
    qa_provider, qa_model = ai_settings.resolve("qa")
    reply_provider, reply_model = ai_settings.resolve("reply")
    # Read-only view of the Google-review auto-reply safety controls (env-owned,
    # so they stay the source of truth) - surfaced so an admin can SEE at a
    # glance whether unattended public replies are on, and on what guardrails.
    reviews_on = bool(cfg.get("GBP_REVIEWS_ENABLED"))
    # Effective guardrails (row value, else env) - used to prefill the editable
    # fields and show the env-fallback blocklist as a placeholder.
    autoreply = ai_settings.autoreply_config()
    autoreply["env_blocklist"] = cfg.get("GBP_AUTOREPLY_BLOCKLIST", "") or ""
    return render_template(
        "ai_settings/index.html",
        row=row,
        tones=sorted(_TONES),
        features=ai_settings.FEATURES,
        # Raw stored per-feature values for the checkboxes (independent of the
        # master, so pausing everything doesn't erase the per-feature prefs).
        feature_checked={f["key"]: bool(getattr(row, f"{f['key']}_enabled", True))
                         for f in ai_settings.FEATURES},
        catalog=ai_settings.catalog_for_ui(),
        effective={"caption": (cap_provider, cap_model),
                   "qa": (qa_provider, qa_model),
                   "reply": (reply_provider, reply_model)},
        reviews_on=reviews_on,
        autoreply=autoreply,
        simulation=bool(cfg.get("AI_SIMULATION_MODE")),
        enabled_now=ai_settings.is_enabled(),
        spend={"total": ai_usage.month_total_usd(),
               "budget": ai_usage.budget_usd()},
    )


@ai_settings_bp.route("/usage")
@login_required
def usage():
    _guard()
    from app.ai import usage as ai_usage
    return render_template("ai_settings/usage.html",
                           summary=ai_usage.month_summary())
