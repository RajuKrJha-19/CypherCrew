"""Prompt builders shared by every backend, so the wording that shapes output
lives in one reviewable place (not scattered per provider).

Each returns (system_text, user_text). Media is attached by the provider
alongside the user text.
"""
import json


def _brand_block(ctx):
    parts = []
    if getattr(ctx, "industry", None):
        parts.append(f"Client industry: {ctx.industry}.")
    if ctx.brand_voice:
        parts.append(f"Brand voice: {ctx.brand_voice}")
    if ctx.brand_notes:
        parts.append(f"Brand do's / don'ts and guideline notes:\n{ctx.brand_notes}")
    return "\n".join(parts)


def caption_prompt(ctx):
    """(system, user) for a per-platform caption draft."""
    system = (
        "You are a senior social media copywriter at a marketing agency. "
        "Write a scroll-stopping, on-brand caption for the post described "
        "below, using the attached media as the visual it accompanies. "
        "Honor the brand voice exactly. Be concise and native to each "
        "platform; do not invent facts not supported by the brief or media. "
        "Return ONLY minified JSON of the form "
        '{"caption": str, "per_platform": {"<platform>": str}, '
        '"hashtags": [str], "first_comment": str}. '
        "Keep every per_platform caption within its character limit. "
        "hashtags are without the leading # and relevant, not spammy. "
        "first_comment may be empty."
    )
    brand = _brand_block(ctx)
    limits = ", ".join(
        f"{p}: {ctx.caption_limits.get(p, 'no limit')} chars"
        for p in (ctx.platforms or [])
    ) or "no specific platforms"
    user = (
        f"BRIEF:\n{ctx.brief or '(no brief provided)'}\n\n"
        + (f"BRAND:\n{brand}\n\n" if brand else "")
        + f"TARGET PLATFORMS AND CAPTION LIMITS: {limits}\n\n"
        "Draft the caption now."
    )
    return system, user


def alt_text_prompt(image):
    system = (
        "You write concise, accurate image alt-text for accessibility. "
        "Describe what is visibly in the image in one plain sentence "
        "(<=125 characters). No 'image of', no marketing language, no "
        "guessing at text you cannot read. Return only the sentence."
    )
    label = f" (context: {image.label})" if getattr(image, "label", None) else ""
    user = f"Write alt-text for the attached image{label}."
    return system, user


def media_check_prompt(ctx):
    """(system, user) for the media QA + fact-check pass (structured findings).

    The model reads the text inside the creative itself (no separate OCR), so
    it can verify phone/website/email/offer against the Client Brain facts and
    flag missing mandatory elements.
    """
    system = (
        "You are a meticulous creative QA reviewer at a marketing agency. "
        "Review the attached deliverable on two fronts. "
        "(1) QUALITY: content vs the brief, brand-guideline violations "
        "(colours, logo usage, tone), visible spelling/typo/grammar errors, "
        "text too close to the edge (safe-area), weak CTA visibility, and "
        "mismatch with the intended spec. "
        "(2) FACTS: read the text shown IN the creative and check it against "
        "the CLIENT FACTS below - flag any phone number, email, website, "
        "offer, price, date, product or campaign that does NOT match the "
        "official facts, and flag any REQUIRED element that is missing "
        "(mandatory disclaimer, a CTA, contact details). Only fact-check "
        "against facts that are actually provided; never invent a rule. "
        "If it looks clean, say so. Return ONLY minified JSON: "
        '{"findings": [{"severity": "info|warning|error", '
        '"category": "brief|brand|text|spec|safe_area|fact", "message": str}]}. '
        "Use category \"fact\" and severity \"error\" for a wrong or missing "
        "phone/website/email/offer/disclaimer."
    )
    brand = _brand_block(ctx)
    specs = json.dumps(ctx.specs) if ctx.specs else "(none provided)"
    user = (
        f"BRIEF:\n{ctx.brief or '(no brief)'}\n\n"
        f"DELIVERABLE TYPE: {ctx.deliverable or '(unspecified)'}\n\n"
        + (f"BRAND:\n{brand}\n\n" if brand else "")
        + (f"CLIENT FACTS (verify the creative against these):\n{ctx.facts}\n\n"
           if getattr(ctx, "facts", None) else "")
        + f"INTENDED OUTPUT SPEC: {specs}\n\n"
        "Review the attached deliverable and list findings."
    )
    return system, user
