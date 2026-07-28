"""Platform-agnostic data transfer objects.

These are the ONLY shapes business logic and adapters exchange. A provider
never receives an ORM model of another platform's concern, and business
logic never sees a platform-specific payload - it sees these.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class PostType(str, Enum):
    IMAGE = "image"
    CAROUSEL = "carousel"
    REEL = "reel"
    STORY = "story"
    VIDEO = "video"
    TEXT = "text"
    DOCUMENT = "document"


class StepStatus(str, Enum):
    #: The publish completed in this call.
    DONE = "done"
    #: Started but the platform is processing async - poll with provider_state.
    PENDING = "pending"
    #: This step failed (map_error will have classified the exception).
    FAILED = "failed"


@dataclass
class Capabilities:
    """What a platform can do - declared once per adapter so business logic
    and the composer can validate content without any platform knowledge."""

    post_types: set                       # of PostType values (str)
    supports_native_scheduling: bool = False
    requires_container_poll: bool = False  # async multi-step (IG / YouTube)
    max_carousel: int | None = None
    max_video_bytes: int | None = None
    max_video_seconds: int | None = None
    publish_rate: tuple | None = None      # (count, window) e.g. (100, "24h")
    story_support: bool = False
    # Can the API attach a tappable sticker/link to a story? Instagram
    # cannot (media_type=STORIES accepts image_url/video_url and nothing
    # else), so a "story that opens the post" is published as a plain
    # story plus a follow-up someone completes in the app. Declared here
    # rather than special-cased in the composer, so the day Meta opens it
    # up the adapter is the only thing that changes.
    story_link_support: bool = False
    max_caption_chars: int | None = None
    supports_first_comment: bool = False   # auto-post a first comment
    supports_delete: bool = False          # delete a published post via API
    supports_comments: bool = False        # read + reply to comments (Engage)

    def supports(self, post_type: str) -> bool:
        return post_type in self.post_types


@dataclass
class MediaRef:
    """A single media item, resolved to an R2 object key by the media
    pipeline. `source` records where it came from for audit."""

    object_key: str
    mime_type: str | None = None
    role: str = "main"          # main | thumbnail
    sort_order: int = 0
    alt_text: str | None = None
    source: str = "upload"      # task_file | client_asset | upload


@dataclass
class PostContent:
    """The resolved, ready-to-publish content for one platform target."""

    platform: str
    post_type: str
    caption: str = ""
    hashtags: str = ""
    media: list = field(default_factory=list)   # list[MediaRef]
    scheduled_for: datetime | None = None
    extra: dict = field(default_factory=dict)   # platform-specific knobs


@dataclass
class TokenBundle:
    """Result of an OAuth exchange / refresh, before encryption."""

    access_token: str
    refresh_token: str | None = None
    token_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    scopes: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class AccountInfo:
    """A publishable asset discovered after auth (Page / IG business /
    organization / channel).

    Some platforms (notably Meta) issue a distinct token PER asset - each
    Facebook Page has its own Page access token, and Instagram publishing
    uses the linked Page's token. So an AccountInfo may carry its own
    access_token/expiry, which AccountManager stores in preference to the
    handshake's user token. Platforms with one token for everything just
    leave these None."""

    external_id: str
    display_name: str
    account_type: str
    meta: dict = field(default_factory=dict)
    access_token: str | None = None
    token_expires_at: datetime | None = None


@dataclass
class PublishStep:
    """Outcome of a start_publish/poll_publish call - the unit the queue
    state machine advances on."""

    status: str                          # StepStatus value
    provider_state: dict | None = None   # persisted so a retry resumes
    external_post_id: str | None = None
    permalink: str | None = None
    error: str | None = None
