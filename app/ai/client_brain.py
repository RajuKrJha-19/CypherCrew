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


def facts_text(client):
    """A compact, labelled block of the AI-visible Client Brain sections, for
    the fact-checker + caption prompts. Empty when there's nothing to say."""
    brain = getattr(client, "brand_brain", None) or {}
    if not isinstance(brain, dict):
        return ""
    parts = []
    for key, label in _AI_SECTIONS:
        value = (brain.get(key) or "").strip()
        if value:
            parts.append(f"{label}:\n{value}")
    return "\n\n".join(parts)
