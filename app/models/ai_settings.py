from datetime import datetime

from app.extensions import db


class AISettings(db.Model):
    """Runtime AI configuration an admin edits from the AI-settings screen -
    which provider + model each task uses, and a soft on/off toggle.

    A single row (id = 1). Any field left blank falls back to the AI_* env
    defaults in config.py, so the DB row only ever holds explicit overrides and
    the app works fine with no row at all. API keys are NEVER stored here -
    they stay in the environment; the screen only picks provider/model.
    """

    __tablename__ = "ai_settings"

    id = db.Column(db.Integer, primary_key=True)

    #: Soft switch, ANDed with the AI_ENABLED env master. Lets an admin pause
    #: ALL AI without touching the server config; env stays the hard
    #: kill-switch. When off, every AI feature is off regardless of the
    #: per-feature toggles below.
    enabled = db.Column(db.Boolean, nullable=False, default=True)

    #: Per-feature soft switches, each ANDed with `enabled`. Default on, so
    #: nothing changes until an admin turns a specific feature off. Gate both
    #: the live routes AND the buttons in the UI, so a disabled feature simply
    #: disappears. See app/ai/settings.feature_enabled().
    caption_enabled = db.Column(db.Boolean, nullable=False, default=True,
                                server_default=db.true())   # captions + alt-text
    qa_enabled = db.Column(db.Boolean, nullable=False, default=True,
                           server_default=db.true())        # media QA
    reply_enabled = db.Column(db.Boolean, nullable=False, default=True,
                              server_default=db.true())      # Google review replies
    comment_enabled = db.Column(db.Boolean, nullable=False, default=True,
                                server_default=db.true())    # Engage comment replies

    caption_provider = db.Column(db.String(30))
    caption_model = db.Column(db.String(120))
    qa_provider = db.Column(db.String(30))
    qa_model = db.Column(db.String(120))
    #: Google review replies. Left blank they ride the caption model (short,
    #: cheap text) - but replies post PUBLICLY, so an admin can pick a stronger
    #: model here without affecting captions.
    reply_provider = db.Column(db.String(30))
    reply_model = db.Column(db.String(120))

    #: Soft monthly spend cap in USD. 0 / null = no cap. When the month's
    #: estimated AI cost reaches this, the live routes refuse with a clear
    #: message until the next month or the cap is raised.
    monthly_budget_usd = db.Column(db.Float, nullable=False, default=0.0,
                                   server_default="0")

    # -- Caption behaviour (workflow tuning) --------------------------------
    caption_tone = db.Column(db.String(20))            # None/"" = auto tone
    caption_variations = db.Column(db.Integer, nullable=False, default=2,
                                   server_default="2")   # 0-3 alt captions
    caption_hashtags = db.Column(db.Boolean, nullable=False, default=True,
                                 server_default=db.true())

    # -- Image / performance ------------------------------------------------
    #: Longest edge (px) an image is downscaled to for the AI call only. 0 =
    #: off. Original published creative is never touched.
    image_max_dim = db.Column(db.Integer, nullable=False, default=1568,
                              server_default="1568")
    #: Largest media file (MB) fed to a call.
    media_max_mb = db.Column(db.Integer, nullable=False, default=10,
                             server_default="10")

    # -- Google review auto-reply guardrails (admin-editable; env is the
    #: fallback default when a column is left at its shipped value). Every knob
    #: here decides whether an UNATTENDED public reply may post, so the route
    #: layer enforces hard floors (rating >= 3, a non-empty blocklist).
    gbp_autoreply_enabled = db.Column(db.Boolean, nullable=False, default=False,
                                      server_default=db.false())
    gbp_min_rating = db.Column(db.Integer, nullable=False, default=4,
                               server_default="4")
    gbp_max_len = db.Column(db.Integer, nullable=False, default=200,
                            server_default="200")
    gbp_max_per_run = db.Column(db.Integer, nullable=False, default=10,
                                server_default="10")
    gbp_blocklist = db.Column(db.Text)                 # None = env fallback

    # -- Engage (social comment) auto-reply guardrails. Global switch is ANDed
    #: with the ENGAGE_AUTOREPLY_ENABLED env gate + a per-client opt-in. The
    #: review blocklist is reused as the shared safety net.
    comment_autoreply_enabled = db.Column(db.Boolean, nullable=False,
                                          default=False, server_default=db.false())
    #: Only comments at or below this length auto-reply (short generic ones).
    comment_max_len = db.Column(db.Integer, nullable=False, default=120,
                                server_default="120")
    #: Cap on auto-replies per post, so a viral thread can't be flooded.
    comment_max_per_post = db.Column(db.Integer, nullable=False, default=5,
                                     server_default="5")
    #: Off by default, questions go to a human. When ON, auto-reply may also
    #: answer QUESTIONS - but only when that comment's client has a Client Brain
    #: to ground the answer in (no facts -> still a human), and the generated
    #: reply passes the same output guards. Its own switch so answering
    #: questions unattended is always a deliberate opt-in.
    comment_answer_questions_enabled = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false())

    # -- Spam auto-moderation (hides matching comments). ANDed with the
    #: ENGAGE_AUTOMOD_ENABLED env gate + a per-client opt-in + a non-empty
    #: spam blocklist. Its OWN blocklist (separate from the auto-reply one).
    comment_automod_enabled = db.Column(db.Boolean, nullable=False,
                                        default=False, server_default=db.false())
    #: Comma-separated spam keywords/phrases; empty disables auto-hide.
    spam_blocklist = db.Column(db.Text)
    #: A comment carrying a link/bare-domain from a non-page author is spam.
    spam_hide_links = db.Column(db.Boolean, nullable=False,
                                default=True, server_default=db.true())
    #: Cap on how many comments one auto-mod run may hide.
    automod_max_per_run = db.Column(db.Integer, nullable=False, default=20,
                                    server_default="20")

    updated_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<AISettings enabled={self.enabled}>"
