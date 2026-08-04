"""The "Client Brain" - a structured, per-client knowledgebase the AI reads to
keep captions on-brand and to fact-check creatives (right phone/website/offer,
required disclaimers, do's/don'ts).

Sections are defined here, not in the schema, so a new one is a one-line add
with no migration. Stored on Client.brand_brain as {section_key: multiline
text}. Sections with ai=False (internal notes) are shown in the form but never
sent to a model.
"""

# (key, label, hint, ai)  -- order = display order on the edit-client screen.
SECTIONS = [
    ("official_phones", "Official phone numbers",
     "One per line. The checker flags a creative showing any other number.", True),
    ("official_emails", "Official emails",
     "One per line.", True),
    ("official_websites", "Official websites",
     "One per line, e.g. hopeplus.in", True),
    ("social_links", "Social media links",
     "Official handles / page URLs.", True),
    ("offers_campaigns", "Offers & campaigns",
     "Current offers, prices, campaign names and their validity dates.", True),
    ("products_services", "Products / services / courses",
     "What this client actually sells — so a wrong course/product is caught.", True),
    ("aliases_synonyms", "Aliases / synonyms / abbreviations",
     "One mapping per line, e.g. \"CSE = Computer Science & Engineering\" or "
     "\"UGC = University Grants Commission\". Lets the AI expand short forms "
     "correctly and recognise the same thing written more than one way.", True),
    ("visual_identity", "Visual identity / logo",
     "Which logo is current (colour, tagline, version) and what's outdated — so the "
     "checker can flag a wrong or old logo. Also upload the correct file under the "
     "client's Logo assets so the AI can compare against it.", True),
    ("dos", "Do's",
     "Things every creative/caption should do.", True),
    ("donts", "Don'ts",
     "Things to avoid — banned words, claims, styles.", True),
    ("disclaimers", "Mandatory disclaimers / required elements",
     "Text or elements every creative must carry (RERA no., “T&C apply”, a CTA, contact...).", True),
    ("compliance_notes", "Compliance notes",
     "Legal/regulatory constraints the AI should respect.", True),
    ("internal_notes", "Internal notes",
     "Team reference only — never sent to the AI.", False),
]

_AI_SECTIONS = [(k, label) for (k, label, _hint, ai) in SECTIONS if ai]


def from_form(form):
    """Build the brand_brain dict from an edit-client POST. Empty sections are
    dropped so the column stays lean; returns None when nothing was filled."""
    brain = {}
    for key, _label, _hint, _ai in SECTIONS:
        value = (form.get("bb_" + key) or "").strip()
        if value:
            brain[key] = value
    return brain or None


def facts_text(client, today=None):
    """A compact, labelled block of the AI-visible Client Brain sections, for
    the fact-checker + caption/reply prompts. Empty when there's nothing to say.

    Structured offers are appended, but ONLY those valid today - an expired
    offer is filtered out here so it can never reach a prompt and be promoted by
    mistake, even though it stays stored on the client."""
    brain = getattr(client, "brand_brain", None) or {}
    parts = []
    if isinstance(brain, dict):
        for key, label in _AI_SECTIONS:
            value = (brain.get(key) or "").strip()
            if value:
                parts.append(f"{label}:\n{value}")

    offers = valid_offers(client, today=today)
    if offers:
        lines = []
        for o in offers:
            line = o["text"]
            if o["until"]:
                line += f" (valid until {o['until']})"
            lines.append(f"- {line}")
        parts.append(
            "Current offers (these are valid TODAY - do NOT mention any offer "
            "or promotion not listed here, and never imply one that has "
            "ended):\n" + "\n".join(lines))
    return "\n\n".join(parts)


# -- structured, time-limited offers ----------------------------------------

MAX_OFFERS = 6


def _today_iso(today=None):
    from datetime import date
    return (today or date.today()).isoformat()


def offers_from_form(form):
    """Build the brand_offers list from an edit-client POST (rows of
    offer_text_i / offer_until_i). Empty rows dropped; None when all empty."""
    out = []
    for i in range(MAX_OFFERS):
        text = (form.get(f"offer_text_{i}") or "").strip()
        if not text:
            continue
        until = (form.get(f"offer_until_{i}") or "").strip() or None
        out.append({"text": text[:300], "until": until})
    return out or None


def valid_offers(client, today=None):
    """Offers valid TODAY: no end date, or an end date on/after today. Expired
    ones are excluded (ISO dates compare chronologically as strings)."""
    offers = getattr(client, "brand_offers", None) or []
    if not isinstance(offers, list):
        return []
    t = _today_iso(today)
    out = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        text = (o.get("text") or "").strip()
        if not text:
            continue
        until = (o.get("until") or "").strip() or None
        if until and until < t:              # ended before today -> hidden
            continue
        out.append({"text": text, "until": until})
    return out


def offers_display(client, today=None):
    """Every stored offer with an `expired` flag, for the edit screen (so the
    team can see and refresh a lapsed offer)."""
    offers = getattr(client, "brand_offers", None) or []
    if not isinstance(offers, list):
        return []
    t = _today_iso(today)
    rows = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        until = (o.get("until") or "").strip() or None
        rows.append({"text": o.get("text") or "", "until": until,
                     "expired": bool(until and until < t)})
    return rows
