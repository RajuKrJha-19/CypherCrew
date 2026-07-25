"""Single source of truth for client brand-asset categories.

Permanent, long-lived files that belong to the client itself - logo,
brand imagery, video, fonts, brand guidelines - as opposed to the
per-task reference/submission files under app/storage. Used by the
upload form, the asset grid on the client page, and server-side
validation, so the three can never drift apart.
"""

CATEGORIES = [
    ("logo", "Logo", "fa-solid fa-image"),
    ("image", "Brand Image", "fa-regular fa-image"),
    ("video", "Video", "fa-solid fa-video"),
    ("font", "Font", "fa-solid fa-font"),
    ("document", "Brand Guideline / Document", "fa-solid fa-file-lines"),
    ("other", "Other", "fa-solid fa-paperclip"),
]

CATEGORY_KEYS = [key for key, _label, _icon in CATEGORIES]
CATEGORY_LABELS = {key: label for key, label, _icon in CATEGORIES}
CATEGORY_ICONS = {key: icon for key, _label, icon in CATEGORIES}

DEFAULT_CATEGORY = "other"


def is_valid(category):
    return category in CATEGORY_KEYS


def label(category):
    return CATEGORY_LABELS.get(category, category.title() if category else "Other")


def icon(category):
    return CATEGORY_ICONS.get(category, "fa-solid fa-paperclip")
