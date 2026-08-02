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
    #: AI without touching the server config; env stays the hard kill-switch.
    enabled = db.Column(db.Boolean, nullable=False, default=True)

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

    updated_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<AISettings enabled={self.enabled}>"
