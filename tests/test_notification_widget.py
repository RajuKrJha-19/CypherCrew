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


# ----------------------------------------------------------------------
# The arrival popup (.ntoast - see static/js/notification-toast.js)
# ----------------------------------------------------------------------
#
# Same drift, one component along: the popup builds its entire card in
# JavaScript, so a class it creates that the stylesheet does not style
# renders as unstyled text in the corner of the screen - visible, wrong,
# and with nothing anywhere to say so.

TOAST_JS = STATIC / "js" / "notification-toast.js"
POLLER_JS = STATIC / "js" / "notifications.js"


def _toast_js():
    return TOAST_JS.read_text(encoding="utf-8", errors="ignore")


def _toast_classes():
    """Every ntoast* class name the component assigns.

    Read out of className/classList assignments rather than the whole file,
    so a class named only inside a comment is not mistaken for one in use.
    """
    js = _toast_js()
    found = set()

    for chunk in re.findall(r'className\s*=\s*"([^"]+)"', js):
        found.update(chunk.split())

    for chunk in re.findall(r'className\s*=\s*"([^"]+)"\s*\+', js):
        found.update(chunk.split())

    for name in re.findall(r'classList\.(?:add|remove)\("([^"]+)"\)', js):
        found.add(name)

    return {name for name in found if name.startswith("ntoast")}


def test_the_popup_creates_at_least_the_card_and_its_stack():
    """A sanity floor: if the extractor stops finding anything, the checks
    below would pass by knowing nothing."""
    classes = _toast_classes()
    assert "ntoast" in classes
    assert "ntoast-stack" in classes
    assert len(classes) >= 8, f"only found {sorted(classes)}"


def test_every_class_the_popup_creates_is_styled():
    css = _stylesheet()

    for name in sorted(_toast_classes()):
        assert _styled_plainly(css, f".{name}"), (
            f"notification-toast.js applies .{name} but no unqualified rule "
            f"styles it - the popup will render broken with nothing to say so"
        )


@pytest.mark.parametrize("state", ["is-shown", "is-leaving", "is-mention"])
def test_the_popup_state_classes_are_styled(state):
    """These are only ever used qualified (.ntoast.is-shown), so they are
    checked for presence rather than through _styled_plainly."""
    css = _stylesheet()
    assert f".ntoast.{state}" in css, f".ntoast.{state} is not styled"


def test_an_actor_name_is_not_uppercased():
    """The kicker is an uppercase micro-label, which is right for "MENTION"
    and wrong for a person: it reads as shouting and mangles the initialisms
    inside a name. The popup marks a name .is-person to opt out, so the rule
    that undoes the transform has to exist."""
    css = _stylesheet()

    assert ".ntoast-label.is-person" in css, (
        "notification-toast.js marks an actor name .is-person but nothing "
        "styles it - the name renders SHOUTED"
    )

    rule = re.search(r"\.ntoast-label\.is-person\{(.*?)\}", css, re.S)
    assert rule and "text-transform:none" in rule.group(1).replace(" ", ""), (
        "the .is-person rule must actually cancel the uppercase"
    )
    assert 'classList.add("is-person")' in _toast_js()


def test_the_stack_sits_below_the_panels_it_shares_a_corner_with():
    """The popups land in the same top-right corner the activity and mention
    panels open into. If the stack ever outranks them it covers the list the
    user deliberately opened."""
    css = _stylesheet()

    stack = re.search(r"\.ntoast-stack\{(.*?)\}", css, re.S)
    panel = re.search(r"\.notification-panel\{(.*?)\}", css, re.S)
    assert stack and panel

    stack_z = int(re.search(r"z-index:\s*(\d+)", stack.group(1)).group(1))
    panel_z = int(re.search(r"z-index:\s*(\d+)", panel.group(1)).group(1))

    assert stack_z < panel_z, (
        f"popup stack (z-index {stack_z}) would cover the notification panel "
        f"(z-index {panel_z})"
    )


def test_the_popup_is_loaded_before_the_poller_that_calls_it():
    """notifications.js calls window.showNotificationToast. Loaded the other
    way round, the first arrival of every page load is silently dropped."""
    widget = _widget()

    toast_at = widget.find("notification-toast.js")
    poller_at = widget.find("js/notifications.js")

    assert toast_at != -1, "notification-toast.js is not loaded by the widget"
    assert poller_at != -1
    assert toast_at < poller_at


def test_the_poller_degrades_without_the_popup():
    """The bell and its sound predate the popup and must not depend on it -
    a failed asset load has to leave the notification system working."""
    js = POLLER_JS.read_text(encoding="utf-8", errors="ignore")
    assert 'typeof window.showNotificationToast !== "function"' in js, (
        "notifications.js must guard the call, or a missing popup script "
        "takes the bell down with it"
    )


def test_the_api_sends_what_the_popup_renders(app, client, login, make_user):
    """The card shows who caused the notification. A payload without the
    actor renders a card with a generic label and no avatar - not broken
    enough to notice, which is why it is pinned."""
    from app.extensions import db
    from app.models import Notification

    with app.app_context():
        recipient = make_user("video_editor")
        actor = make_user("manager", name="Asha Rao")

        db.session.add(Notification(
            user_id=recipient.id,
            actor_id=actor.id,
            title="Task moved to Core Review",
            message="Reel cut v2 is ready for you.",
            category="activity",
        ))
        db.session.commit()

        login(recipient)
        payload = client.get("/notifications/api?limit=5").get_json()

        item = payload["notifications"][0]
        for field in ("id", "title", "message", "link", "category",
                      "created_at_iso", "actor_name", "actor_initials"):
            assert field in item, f"the popup reads {field}; the API omits it"

        assert item["actor_name"] == "Asha Rao"
        assert item["actor_initials"] == "AR"


def test_a_system_notification_has_no_actor(app, client, login, make_user):
    """Plenty of notifications are raised by the app itself. The popup falls
    back to a category label, but only if these come back None rather than
    exploding on the way out."""
    from app.extensions import db
    from app.models import Notification

    with app.app_context():
        recipient = make_user("video_editor")

        db.session.add(Notification(
            user_id=recipient.id,
            title="Deadline in 2 hours",
            category="activity",
        ))
        db.session.commit()

        login(recipient)
        item = client.get("/notifications/api").get_json()["notifications"][0]

        assert item["actor_name"] is None
        assert item["actor_initials"] is None
