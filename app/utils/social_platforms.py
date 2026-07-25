"""Single source of truth for the social-media platforms a task can target.

Used in three places that must never drift apart: the checkbox list on
the task assign/edit form, the "did you publish on X" confirmation
checklist shown before a Client Review task is published, and the
server-side validation that gates the actual publish.
"""

PLATFORMS = [
    ("facebook", "Facebook", "fa-brands fa-facebook"),
    ("instagram", "Instagram", "fa-brands fa-instagram"),
    ("youtube", "YouTube", "fa-brands fa-youtube"),
    ("x", "X", "fa-brands fa-x-twitter"),
    ("linkedin", "LinkedIn", "fa-brands fa-linkedin"),
]

PLATFORM_KEYS = [key for key, _label, _icon in PLATFORMS]
PLATFORM_LABELS = {key: label for key, label, _icon in PLATFORMS}
PLATFORM_ICONS = {key: icon for key, label, icon in PLATFORMS}


def parse_platforms(raw):
    """"facebook,x" -> ["facebook", "x"], dropping unknown keys and
    duplicates, in catalog order (not submission order) so the checklist
    and the stored value always list platforms the same way."""

    if not raw:
        return []

    submitted = {
        key.strip() for key in raw.split(",") if key.strip()
    }

    return [key for key in PLATFORM_KEYS if key in submitted]


def format_platforms(keys):
    """["facebook", "x"] -> "facebook,x", filtering out anything not in
    the catalog (defends against a tampered form field)."""

    return ",".join(key for key in PLATFORM_KEYS if key in (keys or []))


def label(key):
    return PLATFORM_LABELS.get(key, key)


def icon(key):
    return PLATFORM_ICONS.get(key, "fa-solid fa-share-nodes")


def confirm_payload(raw):
    """Task.social_platforms -> the {key, label, icon} list the publish
    confirm checklist needs client-side.

    A plain function call rather than inline template logic - Jinja
    expressions don't support Python comprehension syntax, so this is
    built here and just piped through |tojson in the template.
    """

    return [
        {"key": key, "label": label(key), "icon": icon(key)}
        for key in parse_platforms(raw)
    ]
