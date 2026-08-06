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

    # A free-text campaign label used to group posts across platforms for
    # reporting (e.g. "Diwali 2026"), and as the utm_campaign value when UTM
    # tagging is on. Nullable + indexed so the drafts/history filters are cheap.
    campaign = db.Column(db.String(120), nullable=True, index=True)

    # Reel cover (Instagram/Facebook reels). Either a custom uploaded cover
    # image (its R2 key -> cover_url) OR a frame picked from the video
    # (offset in MILLISECONDS -> thumb_offset). Both null = the platform's
    # default first frame.
    reel_cover_key = db.Column(db.String(1000), nullable=True)
    reel_thumb_offset = db.Column(db.Integer, nullable=True)

    # draft | pending_approval | approved | scheduled | publishing
    # | published | failed | partially_published
    status = db.Column(
        db.String(30), nullable=False, default="draft", index=True
    )

    # Where this post came from: "studio" (composed here) or "ad" (a synthetic
    # record materialised for an ad/boosted post we DIDN'T publish, so its
    # comments can surface in Engage). Ad posts are excluded from every Studio
    # list/report - they exist only to carry Engage comments.
    source = db.Column(
        db.String(20), nullable=False, default="studio",
        server_default="studio", index=True,
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
    #: The post's own picture on the platform, for the Engage preview. Meta's
    #: CDN URLs expire, so this is refreshed on each ad sync and the template
    #: hides an image that fails to load - a stale value degrades to "no
    #: picture", never to a broken one.
    thumbnail_url = db.Column(db.String(1000), nullable=True)
    last_error = db.Column(db.Text, nullable=True)

    # -- Story style ------------------------------------------------------
    # plain     : just the image/video, nothing to tap.
    # post_link : the story should carry a tappable sticker back to a feed
    #             post.
    #
    # Meta's Content Publishing API cannot attach ANY sticker or link to a
    # story - media_type=STORIES takes image_url/video_url and nothing
    # else, and the link/post stickers are app-only by Meta's choice. So
    # "post_link" publishes the story exactly like a plain one and records
    # a follow-up: someone adds the sticker in the Instagram app, then
    # marks it done here. Modelled rather than hidden, so the Studio can
    # show what was intended and chase the bit it can't automate.
    story_style = db.Column(
        db.String(20), nullable=False, default="plain",
        server_default="plain",
    )
    # The feed target this story should point at. Self-referential: for
    # "Also share to Story" it is the sibling target created alongside it.
    story_link_target_id = db.Column(
        db.Integer,
        db.ForeignKey("social_post_targets.id", ondelete="SET NULL"),
        nullable=True
    )
    story_link_done_at = db.Column(db.DateTime, nullable=True)
    story_link_done_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

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
    story_link_target = db.relationship(
        "SocialPostTarget",
        remote_side=[id],
        foreign_keys=[story_link_target_id],
    )
    story_link_done_by = db.relationship(
        "User", foreign_keys=[story_link_done_by_id]
    )

    __table_args__ = (
        db.Index(
            "ix_social_post_targets_sched_status",
            "scheduled_for", "status",
        ),
    )

    #: Post states that mean "this post has been sent on its way".
    _POST_UNDERWAY = ("scheduled", "publishing", "published",
                      "partially_published", "failed")

    @property
    def is_stuck(self):
        """Will this platform never publish without someone intervening?

        Covers the explicit states, and one implicit one: a target left at
        `draft` while its post is already scheduled or published. That is
        what a validation failure used to produce - the target was skipped
        and silently left at draft, so the post could never settle and the
        screen showed nothing actionable. New rows get `blocked`; this
        keeps the ones created before that honest too.
        """
        if self.status in ("blocked", "failed"):
            return True
        post = self.post
        return bool(
            self.status == "draft"
            and post is not None
            and post.status in self._POST_UNDERWAY
        )

    @property
    def links_to_post(self):
        """Was this story meant to be tappable through to a feed post?"""
        return self.post_type == "story" and self.story_style == "post_link"

    @property
    def needs_story_link(self):
        """Live on Instagram, but still missing the sticker only a human
        can add. This is the whole reason story_style is persisted."""
        return (
            self.links_to_post
            and self.status == "published"
            and self.story_link_done_at is None
        )

    @property
    def story_link_url(self):
        """Permalink of the post this story should open - what the person
        adding the sticker actually needs in their hand."""
        linked = self.story_link_target
        return linked.permalink if linked else None

    @property
    def live_url(self):
        """Best-effort link to view this post on the platform. The stored
        permalink when we have one (Studio-published posts); otherwise, for a
        Facebook ad/boosted target the page-post URL built from its
        page_id_post_id external id. None when there's nothing to link to (e.g.
        an Instagram ad media, whose public URL isn't derivable from the id)."""
        if self.permalink:
            return self.permalink
        ext = self.external_post_id or ""
        if self.platform == "facebook" and "_" in ext:
            page_id, _, post_id = ext.partition("_")
            if page_id and post_id:
                return f"https://www.facebook.com/{page_id}/posts/{post_id}"
        return None

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
