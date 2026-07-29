"""The topbar notification badges.

These exist because of a bug that was completely silent. The widget asked
for `.notification-dot`, the stylesheet defined `.notification-dot-marker`,
and the two had drifted apart at some point. Everything "worked": the API
returned the right unread count, notifications.js correctly removed the
`hidden` attribute, no console error, no failing test. The span simply
rendered 0x0 and transparent, so the bell never lit up and nobody could
see they had been mentioned.

Nothing in a Python test suite renders CSS, so the only guard available is
to check that the class the markup asks for is one the stylesheet actually
defines. That is narrow, but it is exactly the failure that happened.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"

WIDGET = TEMPLATES / "partials" / "notification_widget.html"
STYLESHEET = STATIC / "css" / "style.css"


def _stylesheet():
    return STYLESHEET.read_text(encoding="utf-8", errors="ignore")


def _widget():
    return WIDGET.read_text(encoding="utf-8", errors="ignore")


_COMMENTS = re.compile(r"/\*.*?\*/", re.S)


def _selectors(css):
    """Every individual selector in the stylesheet.

    Comments are stripped first, then each rule's selector list is taken as
    the text before its `{` and split on commas. Crude, but it does the one
    thing a substring search cannot: tell a rule's selector apart from a
    mention of the same string inside a comment or a declaration.
    """
    css = _COMMENTS.sub("", css)
    out = []
    for chunk in css.split("}"):
        head, brace, _ = chunk.partition("{")
        if not brace:
            continue
        head = head.strip()
        if head.startswith("@"):             # @media, @keyframes, ...
            continue
        out.extend(part.strip() for part in head.split(",") if part.strip())
    return out


def _styled_plainly(css, class_name):
    """Whether some rule styles `class_name` in its DEFAULT state.

    Qualified selectors do not count. `.notification-dot[hidden]` and
    `.notification-dot:hover` both mention the class while leaving it
    completely unstyled the rest of the time - and a check that accepted
    them would have passed against the very bug this file exists for.
    Descendant selectors like `.notification-btn .notification-dot` do
    count, because they really do style it.
    """
    token = re.escape(class_name) + r"(?![\w\-\[:(])"
    return any(re.search(token, selector) for selector in _selectors(css))


def _defines(css, selector):
    """Whether the stylesheet has a rule with exactly this selector."""
    return any(part == selector for part in _selectors(css))


def test_the_badge_classes_the_widget_uses_are_styled():
    css = _stylesheet()
    widget = _widget()

    classes = set(re.findall(r'class="([^"]*notification-dot[^"]*)"', widget))
    assert classes, "the widget no longer uses a notification-dot class"

    for value in classes:
        for name in value.split():
            assert _styled_plainly(css, f".{name}"), (
                f"{name} is used in notification_widget.html but the "
                f"stylesheet styles it in no unqualified rule - the badge "
                f"will render invisible, exactly as it did before this "
                f"test existed"
            )


def test_the_hidden_badge_is_actually_hidden():
    """The dot is positioned and sized, which beats the user agent's
    [hidden]{display:none}. Without an explicit opt-out the dot the client
    hides stays lit - the same trap the Teams sidebar counters hit."""
    css = _stylesheet()
    assert _defines(css, ".notification-dot[hidden]"), (
        "no [hidden] rule for .notification-dot - hiding it in JS will not "
        "hide it on screen"
    )


@pytest.mark.parametrize("element_id", ["notificationBadge", "mentionsBadge"])
def test_both_badges_are_present_for_the_poller(element_id):
    """notifications.js looks these up by id; a rename in the template
    would leave it silently painting nothing."""
    assert f'id="{element_id}"' in _widget()
