"""SimulationProvider - a fully local, provider-agnostic adapter that makes
the entire Social Publishing Engine work end-to-end WITHOUT any external
platform, credentials or network.

It implements the exact same SocialProvider contract the real Meta/LinkedIn/
YouTube adapters will, with realistic per-platform Capabilities, a real
(loop-back) OAuth handshake, simulated async publishing, and deterministic
fake analytics. Publishing outcomes can be steered from the caption for
testing every path:

    #simfail   -> permanent failure (dead-letters)
    #simretry  -> transient failure (retries, then dead-letters)
    #simslow   -> async: PENDING on first attempt, DONE when polled
    (none)     -> published successfully

Enabled by config.SOCIAL_SIMULATION_MODE. When the real adapters land, they
register under the same platform keys and take over.
"""

from app.social.providers.base import SocialProvider
from app.social.dto import (
    AccountInfo, Capabilities, MediaSpec, PublishStep, StepStatus, TokenBundle,
)
from app.social.errors import PermanentError, SocialError, TransientError


SIM_PLATFORMS = ["instagram", "facebook", "linkedin", "youtube", "x",
                 "google_business"]

# Per-platform capability profiles, mirroring the real July-2026 constraints
# so the composer validates content the same way it will in production.
#
# supports_first_comment / supports_comments matter as much as post_types:
# the composer greys the First comment box out from these flags, and the
# worker skips posting one unless the flag is set. Left unset, a demo
# channel accepted a first comment, published happily, and silently threw
# it away - so they now mirror the real platforms, which all support
# commenting on a post except Google Business.
#
#: The real reel specifications, so a demo channel rejects (and accepts)
#: exactly what production would. A simulation that is more permissive
#: than the platform teaches the wrong lesson.
_IG_REEL = MediaSpec(
    aspect_min=0.01, aspect_max=10.0,
    duration_min=3, duration_max=15 * 60,
    width_max=1920, max_bytes=300 * 1024 * 1024,
    fps_min=23, fps_max=60, codecs=("h264", "hevc"),
    aspect_label="between 0.01:1 and 10:1",
    display_aspect=0.5625, display_label="9:16")
_FB_REEL = MediaSpec(
    aspect_min=0.5625, aspect_max=0.5625,
    duration_min=3, duration_max=90,
    width_min=540, height_min=960,
    fps_min=24, fps_max=60, codecs=("h264", "hevc", "vp9", "av1"),
    aspect_label="9:16",
    display_aspect=0.5625, display_label="9:16")

CAPABILITY_PROFILES = {
    "instagram": Capabilities(
        post_types={"image", "carousel", "reel", "story"},
        media_specs={"reel": _IG_REEL},
        requires_container_poll=True, max_carousel=10,
        publish_rate=(100, "24h"), story_support=True, max_caption_chars=2200,
        supports_first_comment=True, supports_comments=True),
    "facebook": Capabilities(
        post_types={"image", "video", "reel", "text", "carousel", "story"},
        media_specs={"reel": _FB_REEL, "video": MediaSpec()},
        supports_native_scheduling=True, max_carousel=10,
        max_caption_chars=63206, story_support=True,
        supports_first_comment=True, supports_comments=True,
        supports_delete=True),
    "linkedin": Capabilities(
        post_types={"text", "image", "video", "document", "carousel"},
        max_carousel=20, max_caption_chars=3000,
        supports_first_comment=True, supports_comments=True),
    "youtube": Capabilities(
        post_types={"video"},
        # No limits - YouTube takes any shape - but the PLAYER is 16:9, so a
        # vertical clip publishes fine and then plays between two pillars.
        # Mirrors the real provider; display_aspect validates nothing.
        media_specs={"video": MediaSpec(display_aspect=16 / 9,
                                        display_label="16:9")},
        supports_native_scheduling=True,
        requires_container_poll=True, max_caption_chars=5000,
        supports_first_comment=True, supports_comments=True),
    # X counts characters, not bytes, and 280 is the free/basic tier limit -
    # the composer should warn at the limit people actually publish under.
    "x": Capabilities(
        post_types={"text", "image", "video", "carousel"},
        max_carousel=4, max_caption_chars=280,
        supports_first_comment=True, supports_comments=True),
    # Google Business Profile "posts" (What's new / offers) carry one image
    # and a 1500-character body, and cannot be threaded or carouselled.
    # They also take no comments, so no first comment either.
    "google_business": Capabilities(
        post_types={"text", "image"}, max_caption_chars=1500),
}

# Accounts each simulated platform "unlocks" on connect.
SIM_ACCOUNTS = {
    "instagram": [("sim_ig_1", "Demo Brand (IG)", "ig_business")],
    "facebook": [("sim_fb_1", "Demo Brand Page", "page")],
    "linkedin": [("sim_li_1", "Demo Company", "organization")],
    "youtube": [("sim_yt_1", "Demo Channel", "channel")],
    "x": [("sim_x_1", "Demo Handle", "profile")],
    "google_business": [("sim_gbp_1", "Demo Location", "location")],
}


class SimulationProvider(SocialProvider):
    is_simulation = True

    def __init__(self, key):
        self.key = key
        self.capabilities = CAPABILITY_PROFILES.get(
            key, Capabilities(post_types={"image", "text"}))

    # -- OAuth (loop-back, no external hop) --------------------------------

    def build_oauth_url(self, state, redirect_uri):
        # Redirect straight back to our own callback with a simulated code,
        # exercising the real state/exchange/token-store path locally.
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}code=SIMCODE-{self.key}&state={state}"

    def exchange_code(self, code, code_verifier, redirect_uri):
        return TokenBundle(
            access_token=f"SIM-ACCESS-{self.key}",
            scopes="simulation",
            meta={"simulated": True},
        )

    def list_publishable_accounts(self, token):
        accounts = SIM_ACCOUNTS.get(
            self.key, [(f"sim_{self.key}_1", f"Demo {self.key}", "account")])
        return [
            AccountInfo(ext, name, atype, meta={"simulated": True})
            for ext, name, atype in accounts
        ]

    # -- Validation --------------------------------------------------------

    def validate(self, content):
        from app.social.media import pipeline
        problems = pipeline.validate_against(self.capabilities, content)
        needs_media = content.post_type in (
            "image", "carousel", "reel", "story", "video")
        if needs_media and not content.media:
            problems.append(
                f"A {content.post_type} post needs at least one media item.")
        return problems

    # -- Publishing (simulated) -------------------------------------------

    @staticmethod
    def _marker(content):
        text = ((content.caption or "") + " " + (content.hashtags or "")).lower()
        if "#simfail" in text:
            return "fail"
        if "#simretry" in text:
            return "retry"
        if "#simslow" in text:
            return "slow"
        return None

    def _done(self, target):
        ext = f"SIM-{self.key}-{target.id}"
        return PublishStep(
            status=StepStatus.DONE.value,
            external_post_id=ext,
            permalink=f"https://simulated.local/{self.key}/{ext}",
        )

    def start_publish(self, target, content, token):
        marker = self._marker(content)
        if marker == "fail":
            raise PermanentError("Simulated permanent failure (#simfail)")
        if marker == "retry":
            raise TransientError("Simulated transient failure (#simretry)")
        if marker == "slow":
            return PublishStep(
                status=StepStatus.PENDING.value,
                provider_state={"sim_container": f"C-{target.id}"},
            )
        return self._done(target)

    def poll_publish(self, target, provider_state, token):
        return self._done(target)

    # -- First comment -----------------------------------------------------

    def post_first_comment(self, external_post_id, text, token):
        """The worker looks for this method by name, and skipped the whole
        step when a simulated provider didn't have one - so the first
        comment vanished without a word on every demo channel.

        Steerable like publishing, so the failure path can be exercised
        without a real platform: a caption or comment carrying #simfail
        raises instead of succeeding.
        """
        text = (text or "").strip()
        if not (external_post_id and text):
            return None
        if "#simfail" in text.lower():
            raise PermanentError("Simulated first-comment failure (#simfail)")
        return f"SIM-COMMENT-{external_post_id}"

    # -- Analytics (deterministic fakes) ----------------------------------

    def fetch_analytics(self, target, token):
        s = target.id or 1
        return {
            "impressions": 1000 + (s * 137) % 9000,
            "reach": 800 + (s * 97) % 7000,
            "likes": 20 + (s * 13) % 480,
            "comments": (s * 7) % 60,
            "shares": (s * 3) % 40,
            "simulated": True,
        }

    def map_error(self, exc):
        if isinstance(exc, SocialError):
            return exc
        return PermanentError(str(exc))


def register_simulation_providers(registry, platforms=None):
    for key in (platforms or SIM_PLATFORMS):
        registry.register(SimulationProvider(key))
