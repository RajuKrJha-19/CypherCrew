"""The AI provider contract and the plain data carried across it.

Every backend (Gemini, OpenAI, Claude, the simulator) implements AIProvider.
Business code depends on this interface only - never on a concrete backend -
exactly like app/social/providers/base.py.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MediaInput:
    """One media file to hand to the model: raw bytes + mime + a human label.

    Bytes (not URLs) so nothing that could identify a client asset leaks into
    a request log or an error message.
    """
    data: bytes
    mime_type: str
    label: str | None = None            # e.g. "slide 2", "reel cover"


@dataclass
class CaptionContext:
    brief: str = ""                     # task title + description
    industry: str | None = None
    brand_voice: str | None = None
    brand_notes: str | None = None      # do's / don'ts / guideline notes
    facts: str | None = None            # structured Client Brain (accurate names/offers/contacts)
    tone: str | None = None             # optional tone override (e.g. "punchy")
    variations: int = 2                 # how many alternative captions (0-3)
    hashtags: bool = True               # append relevant hashtags?
    platforms: list[str] = field(default_factory=list)
    caption_limits: dict = field(default_factory=dict)   # platform -> max chars
    media: list[MediaInput] = field(default_factory=list)


@dataclass
class CaptionResult:
    caption: str = ""                   # the primary caption
    per_platform: dict = field(default_factory=dict)     # platform -> caption
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)    # SEO discovery keywords
    first_comment: str = ""
    variations: list[str] = field(default_factory=list)  # alternative captions


#: The only severities the rest of the app understands. `service._worst()`
#: ranks them to decide clean vs flagged, and the UI picks an icon per value.
SEVERITIES = ("info", "warning", "error")

#: Words models reach for instead of our three. Every provider asks for
#: "info|warning|error" in its prompt, but they do not all comply - "Error",
#: "critical" and "minor" all turn up - and an unmapped value used to rank as
#: info, so a creative with a wrong phone number was recorded and shown as
#: CLEAN. Mapped here, once, for every backend.
_SEVERITY_ALIASES = {
    "critical": "error", "high": "error", "severe": "error",
    "major": "error", "blocker": "error", "fail": "error", "failure": "error",
    "medium": "warning", "moderate": "warning", "warn": "warning",
    "caution": "warning",
    "low": "info", "minor": "info", "note": "info", "notice": "info",
    "suggestion": "info", "nit": "info", "ok": "info", "pass": "info",
}


def normalise_severity(raw):
    """One of SEVERITIES. Case/whitespace-insensitive, with the common
    synonyms mapped.

    An unrecognised value becomes "warning", NOT "info": the model chose to
    raise a finding, so the one outcome that must never happen is it being
    ranked below the clean/flagged threshold and disappearing. Only a blank
    severity (the field was absent) is treated as advisory.
    """
    value = str(raw or "").strip().lower()
    if not value:
        return "info"
    if value in SEVERITIES:
        return value
    return _SEVERITY_ALIASES.get(value, "warning")


@dataclass
class Finding:
    """One media-QA observation. Advisory only - never blocks a workflow."""
    severity: str = "info"              # info | warning | error
    category: str = "general"           # brief | brand | text | spec | safe_area
    message: str = ""

    def __post_init__(self):
        # Providers build these straight from model output, so normalise at
        # the one place every backend goes through.
        self.severity = normalise_severity(self.severity)

    def as_dict(self):
        return {"severity": self.severity, "category": self.category,
                "message": self.message}


@dataclass
class ReplyContext:
    """Context for drafting a public reply - to a Google review ('review') or
    to a comment on a published social post ('comment'). The two read
    differently: reviews are rated feedback, comments are usually questions or
    reactions on a specific post, so the prompt adapts on `kind`."""
    review_text: str = ""               # the review OR comment text to reply to
    rating: int | None = None           # reviews only
    reviewer: str | None = None         # reviewer / commenter name
    business_name: str | None = None
    brand_voice: str | None = None
    brand_notes: str | None = None
    facts: str | None = None            # Client Brain: FAQs, compliance, do's/don'ts
    kind: str = "review"                # "review" | "comment"
    post_context: str | None = None     # comments: what the post is about


@dataclass
class RewriteContext:
    """Context for a quick-transform of an EXISTING caption (Shorten, Expand,
    Rephrase, More formal/casual, Add emojis, Fix grammar). Text in -> text
    out, like ReplyContext: the model edits `text` per `action`, kept on-brand
    and within the platform limits. No media - the caption is already written."""
    text: str = ""                      # the caption to rewrite
    action: str = ""                    # one of prompts._REWRITE_ACTIONS
    brand_voice: str | None = None
    brand_notes: str | None = None
    facts: str | None = None            # Client Brain: keep stated facts accurate
    tone: str | None = None
    platforms: list[str] = field(default_factory=list)
    caption_limits: dict = field(default_factory=dict)   # platform -> max chars


@dataclass
class MediaCheckContext:
    brief: str = ""
    deliverable: str | None = None
    brand_voice: str | None = None
    brand_notes: str | None = None
    #: The structured Client Brain (official phone/website/offer/disclaimer...)
    #: the fact-checker verifies the creative against. Empty = skip fact-check.
    facts: str | None = None
    guidelines: list[MediaInput] = field(default_factory=list)  # brand docs
    #: Official brand reference images (the CORRECT current logo etc.) the
    #: checker compares the deliverable against, so a wrong/altered/outdated
    #: logo can be flagged. Sent after the deliverable, before the guidelines.
    references: list[MediaInput] = field(default_factory=list)
    specs: dict = field(default_factory=dict)   # intended platform media specs
    media: list[MediaInput] = field(default_factory=list)


class AIProvider(ABC):
    """A backend the AI layer can talk to. Stateless per call."""

    key: str = "base"

    def __init__(self, *, model=None, api_key=None,
                 max_tokens=1024, timeout_s=30):
        # A provider instance is built per task (get_provider(task_kind)), so it
        # carries the single model resolved for that task.
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        # Token usage accumulated across this instance's calls, read by the
        # service after an operation to log spend. Simulation leaves it at 0.
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _add_usage(self, input_tokens, output_tokens):
        self.usage["input_tokens"] += int(input_tokens or 0)
        self.usage["output_tokens"] += int(output_tokens or 0)

    @abstractmethod
    def generate_caption(self, ctx: CaptionContext) -> CaptionResult:
        """Draft an on-brand, per-platform caption for the described post."""

    @abstractmethod
    def generate_alt_text(self, image: MediaInput) -> str:
        """One concise, descriptive alt-text line for a single image."""

    @abstractmethod
    def check_media(self, ctx: MediaCheckContext) -> list[Finding]:
        """Advisory findings on a submitted deliverable (Phase 2)."""

    @abstractmethod
    def generate_reply(self, ctx: ReplyContext) -> str:
        """A short, on-brand reply to a Google review."""

    @abstractmethod
    def rewrite_caption(self, ctx: RewriteContext) -> str:
        """Rewrite an existing caption per ctx.action; return only the new text."""
