"""The publish badge macro, and the import that nearly took the task list
down for good.

The bug: Jinja gives an imported macro the environment's GLOBALS but not
the template CONTEXT. `social_publish_badge`, `has_permission` and
`url_for` are globals, so they resolved fine - but `current_user` is not,
Flask-Login supplies it through a context processor. Imported without
`with context` it was Undefined inside the macro.

That line is only reached when a task has a FAILED, RETRYABLE publish, so
the template rendered perfectly for months and then 500'd the entire task
list the first time an Instagram post failed.

Two guards, because either alone leaves a hole:
  * every template that imports the macro does so `with context`;
  * and the macro survives without it anyway, losing the retry button
    rather than the page.
"""

from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
MACRO = TEMPLATES / "partials" / "_publish_badge.html"

#: What social_publish_badge() returns for a failed, retryable publish -
#: the state that reaches the guarded line.
RETRY_BADGE = {
    "tone": "danger",
    "icon": "fa-triangle-exclamation",
    "label": "Failed",
    "permalinks": [],
    "retry": True,
}


@pytest.fixture()
def stub_badge(app):
    """Make social_publish_badge return a retryable badge, and put the real
    one back.

    RESTORE, not pop. The first version of this deleted the global instead
    of restoring it, so every task page rendered by a later test lost the
    badge and failed - a test that broke four other files by tidying up
    wrongly.
    """
    key = "social_publish_badge"
    missing = object()
    original = app.jinja_env.globals.get(key, missing)
    app.jinja_env.globals[key] = lambda task: RETRY_BADGE
    try:
        yield
    finally:
        if original is missing:
            app.jinja_env.globals.pop(key, None)
        else:
            app.jinja_env.globals[key] = original


def _importers():
    return [
        path for path in TEMPLATES.rglob("*.html")
        if "import publish_badge" in path.read_text(encoding="utf-8",
                                                    errors="ignore")
    ]


def test_every_import_of_the_badge_carries_context():
    importers = _importers()
    assert importers, "nothing imports publish_badge any more - check this test"

    for path in importers:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "import publish_badge" in line:
                assert "with context" in line, (
                    f"{path.name} imports publish_badge without `with "
                    f"context`, so current_user is Undefined inside the "
                    f"macro and the retry branch will 500 the page"
                )


def test_the_macro_survives_being_imported_without_context(app, stub_badge):
    """The belt to the braces above.

    Rendered exactly the way a template that forgot `with context` would:
    no current_user in scope, and a badge in the retryable state.
    """
    template = app.jinja_env.from_string(
        '{% from "partials/_publish_badge.html" import publish_badge %}'
        "{{ publish_badge(task) }}"
    )

    with app.app_context():
        html = template.render(task=object())

    # The badge still renders...
    assert "Failed" in html
    # ...and the retry form is simply absent, because an unknown viewer has
    # no permissions. No exception, no blank page.
    assert "pub-retry-btn" not in html


def test_the_macro_shows_retry_for_a_permitted_viewer(app, stub_badge):
    """And with context, the button is there - so the guard above is not
    quietly disabling the feature."""
    class _Viewer:
        is_authenticated = True
        role = "super_admin"          # owner short-circuits has_permission

    template = app.jinja_env.from_string(
        '{% from "partials/_publish_badge.html" import publish_badge '
        'with context %}{{ publish_badge(task) }}'
    )

    with app.test_request_context():
        html = template.render(task=_Task(), current_user=_Viewer())

    assert "pub-retry-btn" in html


class _Task:
    id = 1
