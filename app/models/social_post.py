from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class SocialPost(db.Model):
    """The content aggregate: one idea, optionally born from a Task, that
    fans out into one SocialPostTarget per (platform, account). Approval
    and version history hang off this row."""

    __tablename__ = "social_posts"

    id = db.Column(db.Integer, primary_key=True)

    task_id = db.Column(
        db.Integer, db.ForeignKey("tasks.id"), nullable=True, index=True
    )
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=True, index=True
    )

    title = db.Column(db.String(255), nullable=True)
    base_caption = db.Column(db.Text, nullable=True)

    # draft | pending_approval | approved | scheduled | publishing
    # | published | failed | partially_published
    status = db.Column(
        db.String(30), nullable=False, default="draft", index=True
    )

    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    approved_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    approved_at = db.Column(db.DateTime, nullable=True)

    # Published directly on the platform, outside Social Studio (the user hit
    # "Mark as manually published" on the task). Kept as a first-class record
    # so the Studio's Published list and the task both reflect it, badged
    # "Published outside Social Studio".
    published_externally = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    task = db.relationship("Task")
    client = db.relationship("Client")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    targets = db.relationship(
        "SocialPostTarget",
        backref="post",
        cascade="all, delete-orphan",
        lazy=True,
    )

    def __repr__(self):
        return f"<SocialPost {self.id} {self.status}>"


class SocialPostTarget(db.Model):
    """One platform variant of a SocialPost - the actual unit that gets
    published, scheduled, retried and measured. Carries the per-platform
    caption/hashtags/schedule and the outcome (external id + permalink)."""

    __tablename__ = "social_post_targets"

    id = db.Column(db.Integer, primary_key=True)

    social_post_id = db.Column(
        db.Integer, db.ForeignKey("social_posts.id"),
        nullable=False, index=True,
    )
    # Nullable so a target can be composed before an account is chosen;
    # validated as non-null before publish.
    social_account_id = db.Column(
        db.Integer, db.ForeignKey("social_accounts.id"), nullable=True
    )

    platform = db.Column(db.String(30), nullable=False)
    # image | carousel | reel | story | video | text | document
    post_type = db.Column(db.String(30), nullable=False, default="image")

    caption = db.Column(db.Text, nullable=True)
    hashtags = db.Column(db.Text, nullable=True)

    # Auto-posted as the first comment right after this target publishes
    # (the standard "hashtags/link in first comment" pattern). Best-effort:
    # a comment failure never fails the publish itself.
    first_comment = db.Column(db.Text, nullable=True)

    scheduled_for = db.Column(db.DateTime, nullable=True)  # UTC

    # draft | pending_approval | approved | scheduled | publishing
    # | published | failed
    status = db.Column(db.String(30), nullable=False, default="draft")

    external_post_id = db.Column(db.String(255), nullable=True)
    permalink = db.Column(db.String(500), nullable=True)
    last_error = db.Column(db.Text, nullable=True)

    ai_generated = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    account = db.relationship("SocialAccount")
    media = db.relationship(
        "SocialMediaAsset",
        backref="target",
        cascade="all, delete-orphan",
        lazy=True,
        order_by="SocialMediaAsset.sort_order",
    )

    __table_args__ = (
        db.Index(
            "ix_social_post_targets_sched_status",
            "scheduled_for", "status",
        ),
    )

    def __repr__(self):
        return f"<SocialPostTarget {self.id} {self.platform} {self.status}>"


class SocialMediaAsset(db.Model):
    """An ordered media item attached to a target (or, before targets are
    split out, to the post). Resolves to an R2 object key; `source` records
    whether it came from a task submission, a client brand asset, or a
    direct upload."""

    __tablename__ = "social_media_assets"

    id = db.Column(db.Integer, primary_key=True)

    social_post_id = db.Column(
        db.Integer, db.ForeignKey("social_posts.id"), nullable=True, index=True
    )
    target_id = db.Column(
        db.Integer, db.ForeignKey("social_post_targets.id"),
        nullable=True, index=True,
    )

    source = db.Column(db.String(20), nullable=False, default="upload")
    task_file_id = db.Column(
        db.Integer, db.ForeignKey("task_files.id"), nullable=True
    )
    client_asset_id = db.Column(
        db.Integer, db.ForeignKey("client_assets.id"), nullable=True
    )
    object_key = db.Column(db.String(1000), nullable=True)

    role = db.Column(db.String(20), nullable=False, default="main")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    alt_text = db.Column(db.Text, nullable=True)
    mime_type = db.Column(db.String(150), nullable=True)

    meta = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<SocialMediaAsset {self.id} {self.source}>"


class SocialHashtagSet(db.Model):
    """A reusable, named group of hashtags a team can insert into a caption
    with one click. Optionally scoped to a client (nullable = shared across
    the agency)."""

    __tablename__ = "social_hashtag_sets"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=True, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    hashtags = db.Column(db.Text, nullable=False)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    client = db.relationship("Client")

    def __repr__(self):
        return f"<SocialHashtagSet {self.name}>"
