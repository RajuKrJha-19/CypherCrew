"""The Apps group in the ERP sidebar.

Social Studio and Cypher-Teams each have their own dashboard and their own
sidebar; opening one leaves the ERP shell. They are grouped under an
"Apps" heading so that reads as a product switch rather than another page.

The heading is rendered by its own {% if %}, separate from the two links,
and that is the thing worth pinning: both links are behind feature flags,
so on a server with both off a heading with nothing under it would sit at
the foot of the nav pointing at empty space. No error, no traceback -
exactly the kind of thing that ships.
"""

import re

from pathlib import Path

import pytest

SIDEBAR = (
    Path(__file__).resolve().parent.parent
    / "app" / "templates" / "partials" / "sidebar.html"
).read_text(encoding="utf-8")


def _render(client, login, make_user):
    """Any page renders the full sidebar; the dashboard is the cheapest."""
    user = make_user("admin", permissions=["manage_tasks"])
    login(user)
    response = client.get("/", follow_redirects=True)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_the_apps_heading_is_gated_on_the_same_flags_as_its_links():
    """Read from the template rather than rendered output, because the
    case that matters - both features off - is not the configuration the
    suite runs under."""
    heading = re.search(
        r"\{%\s*if\s+([^%]*?)\s*%\}\s*<span class=\"sidebar-nav-label sidebar-nav-label-apps\">",
        SIDEBAR,
    )
    assert heading, (
        "the Apps heading is no longer wrapped in an {% if %} - with both "
        "feature flags off it will render above nothing"
    )

    condition = heading.group(1)
    assert "show_social" in condition and "show_teams" in condition, (
        f"the Apps heading is gated on `{condition}`, which does not "
        f"consider both apps; it must not appear when neither link does"
    )


@pytest.mark.parametrize("name", ["show_social", "show_teams"])
def test_each_link_reuses_the_same_flag_the_heading_checks(name):
    """If a link re-derives its own condition, the heading and the link
    can disagree and the group empties out again."""
    assert re.search(r"\{%\s*set\s+" + name + r"\s*=", SIDEBAR), (
        f"{name} is no longer computed once - the heading and the link "
        f"must share it or they will drift"
    )
    assert re.search(r"\{%\s*if\s+" + name + r"\s*%\}", SIDEBAR), (
        f"{name} is set but no link is gated on it"
    )


def test_the_apps_render_with_the_product_classes(app, client, login, make_user):
    html = _render(client, login, make_user)

    if app.config.get("TEAMS_ENABLED"):
        assert "nav-app-teams" in html, (
            "Teams is enabled but is not rendered as an app - it will look "
            "like an ordinary page in the nav"
        )
        # The unread badge is painted by notifications.js into this id.
        assert 'id="teamsNavCount"' in html

    assert "sidebar-nav-label-apps" in html, \
        "at least one app is enabled, so the Apps heading must be present"


def test_the_apps_keep_the_plain_active_class(app, client, login, make_user):
    """The product classes are additive. Replacing `active` with something
    bespoke would lose the accent rail every other nav row gets."""
    html = _render(client, login, make_user)

    for match in re.finditer(r'class="(nav-app[^"]*)"', html):
        classes = match.group(1).split()
        assert "nav-app" in classes
        assert any(c.startswith("nav-app-") for c in classes), (
            f'{match.group(1)} has no product class, so it gets the app '
            f'layout with no colour'
        )
