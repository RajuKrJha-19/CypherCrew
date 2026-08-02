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
    """(system, user) for a per-platform caption draft with alternatives."""
    tone = (ctx.tone or "").strip()
    tone_line = (f" Write in a {tone} tone." if tone else "")
    system = (
        "You are a senior social media copywriter at a marketing agency. "
        "Write a scroll-stopping, on-brand caption for the post described "
        "below, using the attached media as the visual it accompanies. "
        "Honor the brand voice exactly." + tone_line + " Be concise and "
        "native to each platform. Use the CLIENT FACTS for accurate names, "
        "offers, prices and contact details; do NOT state any fact not "
        "supported by the brief, the media, or those facts. "
        "Return ONLY minified JSON of the form "
        '{"caption": str, "per_platform": {"<platform>": str}, '
        '"hashtags": [str], "first_comment": str, "variations": [str]}. '
        "'caption' is your best version; 'variations' are 2 alternative full "
        "captions taking different angles/hooks. Keep every per_platform "
        "caption within its character limit. hashtags are without the leading "
        "# and relevant, not spammy. first_comment may be empty."
    )
    brand = _brand_block(ctx)
    limits = ", ".join(
        f"{p}: {ctx.caption_limits.get(p, 'no limit')} chars"
        for p in (ctx.platforms or [])
    ) or "no specific platforms"
    user = (
        f"BRIEF:\n{ctx.brief or '(no brief provided)'}\n\n"
        + (f"BRAND:\n{brand}\n\n" if brand else "")
        + (f"CLIENT FACTS (use for accuracy, do not contradict):\n{ctx.facts}\n\n"
           if getattr(ctx, "facts", None) else "")
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


def reply_prompt(ctx):
    """(system, user) for a public reply. Returns plain text.

    Branches on ctx.kind: 'comment' (a comment on a published post - usually a
    question or reaction) vs 'review' (rated Google feedback). Comments need a
    helpful, answer-the-question tone; reviews need a gracious/empathetic one.
    """
    if getattr(ctx, "kind", "review") == "comment":
        return _comment_reply_prompt(ctx)
    system = (
        "You write short, warm, professional replies to Google reviews on "
        "behalf of a business, for its social media agency. Rules: thank the "
        "reviewer by name when given; sound human and specific, not templated; "
        "keep it 1-3 sentences. For a positive review, be gracious and invite "
        "them back. For a critical review, be genuinely empathetic, apologize "
        "for the experience, and offer to make it right offline (ask them to "
        "reach out) - but NEVER admit legal fault, NEVER share or ask for "
        "private/personal details in public, and NEVER dispute or argue. "
        "Use the CLIENT FACTS for accurate info (hours, offers, contact) and "
        "obey the brand voice and any compliance notes. Do not invent facts. "
        "Return ONLY the reply text - no quotes, no preamble, no JSON."
    )
    brand = _brand_block(ctx)
    biz = f" for {ctx.business_name}" if getattr(ctx, "business_name", None) else ""
    stars = f"{ctx.rating}-star " if ctx.rating else ""
    user = (
        f"Draft a reply{biz} to this {stars}review"
        + (f" from {ctx.reviewer}" if ctx.reviewer else "") + ":\n"
        f"\"{ctx.review_text or '(no text - a star rating only)'}\"\n\n"
        + (f"BRAND:\n{brand}\n\n" if brand else "")
        + (f"CLIENT FACTS:\n{ctx.facts}\n\n" if getattr(ctx, "facts", None) else "")
        + "Write the reply now."
    )
    return system, user


def _comment_reply_prompt(ctx):
    """(system, user) for a reply to a comment on a published social post."""
    system = (
        "You reply to comments on a brand's social media posts, on behalf of "
        "the brand, for its marketing agency. Comments are usually questions, "
        "compliments, or reactions. Rules: be warm, helpful and human, never "
        "templated; address the commenter by first name when given; keep it to "
        "1-2 short sentences suited to social media. If it is a question, "
        "answer it using the CLIENT FACTS (hours, price, offer, location, "
        "contact); if the answer is NOT in those facts, do NOT invent it - "
        "instead invite them to DM or contact the business. Thank people for "
        "compliments. For a complaint, be empathetic and take it to DM; NEVER "
        "argue, admit legal fault, or share/ask for private details in public. "
        "Obey the brand voice and any compliance notes. Return ONLY the reply "
        "text - no quotes, no preamble, no hashtags unless natural, no JSON."
    )
    brand = _brand_block(ctx)
    biz = f" for {ctx.business_name}" if getattr(ctx, "business_name", None) else ""
    user = (
        f"Draft a reply{biz} to this comment"
        + (f" from {ctx.reviewer}" if ctx.reviewer else "") + ":\n"
        f"\"{ctx.review_text or '(no text)'}\"\n\n"
        + (f"THE POST IT IS ON:\n{ctx.post_context}\n\n"
           if getattr(ctx, "post_context", None) else "")
        + (f"BRAND:\n{brand}\n\n" if brand else "")
        + (f"CLIENT FACTS (use to answer; do not contradict or invent):\n{ctx.facts}\n\n"
           if getattr(ctx, "facts", None) else "")
        + "Write the reply now."
    )
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
