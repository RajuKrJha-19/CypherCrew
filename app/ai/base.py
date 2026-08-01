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
    platforms: list[str] = field(default_factory=list)
    caption_limits: dict = field(default_factory=dict)   # platform -> max chars
    media: list[MediaInput] = field(default_factory=list)


@dataclass
class CaptionResult:
    caption: str = ""                   # the shared base caption
    per_platform: dict = field(default_factory=dict)     # platform -> caption
    hashtags: list[str] = field(default_factory=list)
    first_comment: str = ""


@dataclass
class Finding:
    """One media-QA observation. Advisory only - never blocks a workflow."""
    severity: str = "info"              # info | warning | error
    category: str = "general"           # brief | brand | text | spec | safe_area
    message: str = ""

    def as_dict(self):
        return {"severity": self.severity, "category": self.category,
                "message": self.message}


@dataclass
class MediaCheckContext:
    brief: str = ""
    deliverable: str | None = None
    brand_voice: str | None = None
    brand_notes: str | None = None
    guidelines: list[MediaInput] = field(default_factory=list)  # brand docs
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

    @abstractmethod
    def generate_caption(self, ctx: CaptionContext) -> CaptionResult:
        """Draft an on-brand, per-platform caption for the described post."""

    @abstractmethod
    def generate_alt_text(self, image: MediaInput) -> str:
        """One concise, descriptive alt-text line for a single image."""

    @abstractmethod
    def check_media(self, ctx: MediaCheckContext) -> list[Finding]:
        """Advisory findings on a submitted deliverable (Phase 2)."""
