"""Deterministic no-network backend for localhost + tests.

Returns scripted-but-plausible output derived from the context, so the whole
compose -> generate flow is exercisable with no API key, and tests can assert
on stable results. Selected whenever AI_SIMULATION_MODE is on.
"""
import re

from app.ai.base import AIProvider, CaptionResult, Finding


_STOP = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
         "our", "your", "this", "that", "is", "are", "we", "you"}


def _keywords(text, limit=4):
    words = re.findall(r"[A-Za-z][A-Za-z0-9']+", (text or "").lower())
    seen, out = set(), []
    for w in words:
        if w in _STOP or len(w) < 4 or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


class SimulationProvider(AIProvider):
    key = "simulation"

    def generate_caption(self, ctx):
        brief = (ctx.brief or "").strip()
        headline = (brief.splitlines()[0].strip() if brief
                    else "Your latest update")[:200]
        # A voice hint keeps the simulated output visibly brand-aware so a
        # tester can confirm the brand field is flowing through.
        voice = f" — {ctx.brand_voice.strip()}" if ctx.brand_voice else ""
        caption = f"{headline}{voice}".strip()

        tags = _keywords(f"{brief} {ctx.industry or ''}")
        per_platform = {}
        for p in (ctx.platforms or []):
            limit = ctx.caption_limits.get(p)
            per_platform[p] = caption[:limit] if limit else caption

        return CaptionResult(
            caption=caption,
            per_platform=per_platform,
            hashtags=tags,
            first_comment="",
        )

    def generate_alt_text(self, image):
        label = (image.label or "the attached image").strip()
        return f"{label}: a clear, descriptive view (simulated alt-text)."[:125]

    def check_media(self, ctx):
        # Clean by default; a sentinel in the brief lets a test drive the
        # findings path without a real model.
        text = f"{ctx.brief or ''} {ctx.deliverable or ''}".lower()
        if "simwarn" in text:
            return [Finding(severity="warning", category="brief",
                            message="Simulated: deliverable may not match the "
                                    "brief - please double-check.")]
        return [Finding(severity="info", category="general",
                        message="Simulated check: no issues detected.")]
