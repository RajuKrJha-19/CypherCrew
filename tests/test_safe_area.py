"""display_aspect: the shape a platform SHOWS in, beside what it accepts.

These are two different questions and the gap between them is where work is
lost silently. Instagram takes a Reel at any aspect from 0.01 to 10.0 and then
displays it at 9:16, so a landscape clip uploads, validates, publishes - and
loses both sides, with nothing in the app having said so. The composer's
safe-area overlay draws that second number.

The rule these exist to protect: display_aspect must never become a second
validator. Widening check_spec to match a display ratio would start refusing
posts that publish perfectly well, which is the opposite of the problem.
"""

import re
from pathlib import Path

import pytest

from app.social.dto import MediaSpec
from app.social.media import fit
from app.social.providers.meta_facebook import MetaFacebookProvider
from app.social.providers.meta_instagram import MetaInstagramProvider
from app.social.providers.simulation import CAPABILITY_PROFILES
from app.social.providers.youtube import YouTubeProvider

REAL_PROVIDERS = [MetaFacebookProvider, MetaInstagramProvider, YouTubeProvider]

#: Vertical video. Every reel and story on every platform shows in this.
NINE_SIXTEEN = 0.5625


def _specs(provider_class):
    caps = provider_class.capabilities
    return (caps.media_specs or {}) if caps else {}


# ----------------------------------------------------------------------
# The data
# ----------------------------------------------------------------------

@pytest.mark.parametrize("provider_class", REAL_PROVIDERS)
def test_reels_and_stories_declare_the_frame_they_display_in(provider_class):
    """These are the types where the gap bites: both Facebook and Instagram
    show them at 9:16 regardless of what they accepted."""
    for post_type, spec in _specs(provider_class).items():
        if post_type in ("reel", "story"):
            assert spec.display_aspect == pytest.approx(NINE_SIXTEEN), (
                "%s %s has no 9:16 display frame, so the safe-area overlay "
                "cannot draw anything for it"
                % (provider_class.__name__, post_type)
            )


def test_instagram_reel_is_the_case_this_exists_for():
    """Accepts almost any shape, shows 9:16. Without display_aspect a
    landscape Reel is reported as fitting."""
    spec = _specs(MetaInstagramProvider)["reel"]

    assert spec.aspect_min == 0.01 and spec.aspect_max == 10.0
    assert spec.display_aspect == pytest.approx(NINE_SIXTEEN)


def test_youtube_has_a_display_frame_but_still_no_limits():
    """YouTube accepts any shape and letterboxes. The 16:9 preview box was a
    bare UI convention with nothing behind it until now - but giving it a
    frame must not give it a constraint."""
    spec = _specs(YouTubeProvider)["video"]

    assert spec.display_aspect == pytest.approx(16 / 9)
    assert spec.aspect_min is None and spec.aspect_max is None


@pytest.mark.parametrize("platform,post_type,provider_class", [
    ("instagram", "reel", MetaInstagramProvider),
    ("facebook", "reel", MetaFacebookProvider),
    ("youtube", "video", YouTubeProvider),
])
def test_the_simulation_profiles_match_the_real_ones(
        platform, post_type, provider_class):
    """A demo channel that behaved differently would teach the wrong lesson -
    which is what simulation.py's own comment says.

    This is not hypothetical: the YouTube profile was missed on the first
    pass, and because the registry serves the SIMULATION capabilities when
    SOCIAL_SIMULATION_MODE is on, the overlay silently never drew on a demo
    channel while working perfectly on a real one.
    """
    sim = (CAPABILITY_PROFILES[platform].media_specs or {}).get(post_type)
    real = _specs(provider_class)[post_type]

    assert sim is not None, (
        "the %s simulation profile has no %s spec, so a demo channel behaves "
        "differently from a real one" % (platform, post_type)
    )
    assert sim.display_aspect == real.display_aspect
    assert sim.display_label == real.display_label


# ----------------------------------------------------------------------
# It must not become a validator
# ----------------------------------------------------------------------

def test_check_spec_ignores_display_aspect_entirely():
    """The whole safety property. A 16:9 clip against a spec that accepts
    anything but displays 9:16 must still validate - it publishes fine, it
    just gets cropped by the platform."""
    spec = MediaSpec(aspect_min=0.01, aspect_max=10.0,
                     display_aspect=NINE_SIXTEEN, display_label="9:16")

    problems = fit.check_spec(spec, {"width": 1920, "height": 1080})

    assert problems == [], (
        "display_aspect leaked into validation - this would start refusing "
        "posts that publish perfectly well"
    )


def test_a_landscape_reel_still_passes_instagram_validation():
    """The end-to-end version of the rule, against the real spec."""
    spec = _specs(MetaInstagramProvider)["reel"]

    assert fit.check_spec(spec, {"width": 1920, "height": 1080,
                                 "duration": 20}) == []


def test_facebooks_hard_aspect_limit_is_untouched():
    """Facebook really does require exactly 9:16 for a reel, and that must
    keep failing - the display frame sits beside the limit, not over it."""
    spec = _specs(MetaFacebookProvider)["reel"]

    problems = fit.check_spec(spec, {"width": 1920, "height": 1080,
                                     "duration": 20})

    assert problems, "Facebook's 9:16 reel requirement stopped being enforced"


@pytest.mark.parametrize("provider_class", REAL_PROVIDERS)
def test_a_display_frame_never_narrows_an_accept_range(provider_class):
    """If display_aspect fell outside what the spec accepts, the overlay
    would be drawing a frame the platform would reject anyway."""
    for post_type, spec in _specs(provider_class).items():
        if not spec.display_aspect:
            continue
        if spec.aspect_min is not None:
            assert spec.display_aspect >= spec.aspect_min - 0.02, (
                "%s %s displays at a ratio it would refuse"
                % (provider_class.__name__, post_type))
        if spec.aspect_max is not None:
            assert spec.display_aspect <= spec.aspect_max + 0.02, (
                "%s %s displays at a ratio it would refuse"
                % (provider_class.__name__, post_type))


# ----------------------------------------------------------------------
# It has to reach the browser
# ----------------------------------------------------------------------

def test_the_capabilities_map_ships_the_display_frame(app):
    """The composer can only draw what the route sends it."""
    from app.routes.social import _capabilities_map

    with app.app_context():
        caps = _capabilities_map()

    reel = None
    for platform in ("instagram", "facebook"):
        specs = (caps.get(platform) or {}).get("media_specs") or {}
        if "reel" in specs:
            reel = specs["reel"]
            break

    assert reel is not None, "no reel spec reached the composer"
    assert "display_aspect" in reel
    assert "display_label" in reel
    assert reel["display_aspect"] == pytest.approx(NINE_SIXTEEN)


# ----------------------------------------------------------------------
# The composer wiring
# ----------------------------------------------------------------------

def _compose():
    return (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "social" / "compose.html").read_text(encoding="utf-8",
                                                   errors="ignore")


def test_the_overlay_is_built_inside_render_preview():
    """renderPreview() rewrites media.className AND media.innerHTML on every
    render, so an overlay attached from anywhere else is wiped by the next
    keystroke. This is the easiest way to get the feature subtly wrong."""
    source = _compose()

    body = source.split("function renderPreview()")[1].split("\n    }")[0]

    assert "safeArea(" in body, "the overlay is not computed in renderPreview"
    assert "is-safe" in body, "the safe-area class is not applied there"
    assert "pp-cut" in body, "the danger bands are not built there"
    assert "pp-keep" in body, "the keep frame is not built there"


def test_the_toggle_and_its_styles_exist():
    source = _compose()
    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "css"
           / "style.css").read_text(encoding="utf-8", errors="ignore")

    assert 'id="safeToggle"' in source
    assert 'id="safeNote"' in source

    for klass in (".safe-toggle", ".pp-cut", ".pp-keep",
                  ".pp-safe-note", ".pp-media.is-safe"):
        assert klass in css, "%s is not styled" % klass


def test_the_overlay_only_draws_where_there_is_a_frame():
    """A platform that publishes at whatever shape it is given has nothing to
    draw, and a toggle that changes nothing is worse than no toggle."""
    source = _compose()

    body = source.split("function safeArea(")[1].split("\n    }")[0]

    assert "display_aspect" in body
    assert "return null" in body, (
        "safeArea must bail out when there is no frame, no media or no "
        "measurement yet"
    )
