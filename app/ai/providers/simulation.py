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
        # Voice + tone hints keep the simulated output visibly brand/tone-aware,
        # so a tester (and the tests) can confirm those fields flow through.
        voice = f" — {ctx.brand_voice.strip()}" if ctx.brand_voice else ""
        tone = f" [{ctx.tone.strip()}]" if getattr(ctx, "tone", None) else ""
        caption = f"{headline}{voice}{tone}".strip()

        tags = _keywords(f"{brief} {ctx.industry or ''}")
        per_platform = {}
        for p in (ctx.platforms or []):
            limit = ctx.caption_limits.get(p)
            per_platform[p] = caption[:limit] if limit else caption

        variations = [f"{caption} (take 2)", f"{caption} (take 3)"]

        return CaptionResult(
            caption=caption,
            per_platform=per_platform,
            hashtags=tags,
            first_comment="",
            variations=variations,
        )

    def generate_alt_text(self, image):
        label = (image.label or "the attached image").strip()
        return f"{label}: a clear, descriptive view (simulated alt-text)."[:125]

    def check_media(self, ctx):
        # Clean by default; sentinels in the brief/facts let a test drive the
        # warning + fact-error paths without a real model.
        text = f"{ctx.brief or ''} {ctx.deliverable or ''} {ctx.facts or ''}".lower()
        findings = []
        if "simfact" in text:
            findings.append(Finding(
                severity="error", category="fact",
                message="Simulated: the phone number on the creative does not "
                        "match the official one."))
        if "simwarn" in text:
            findings.append(Finding(
                severity="warning", category="brief",
                message="Simulated: deliverable may not match the brief - "
                        "please double-check."))
        if findings:
            return findings
        return [Finding(severity="info", category="general",
                        message="Simulated check: no issues detected.")]
