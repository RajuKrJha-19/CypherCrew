"""The "Is this a social media post?" question on the task form.

It used to be a checkbox, and the parser only asked whether the field was
present. A Yes/No pair posts a value either way - so "No" arrives as the
string "0", which the old bool(...) read as true. These tests pin the
value-aware reading, because getting it wrong marks every task ever
created as a social media post and silently changes its whole workflow.
"""

import pytest

from app.routes.tasks import parse_social_media_fields


def _parse(app, form):
    with app.test_request_context("/tasks/add", method="POST", data=form):
        return parse_social_media_fields()


# ------------------------------------------------------------------
# The trap
# ------------------------------------------------------------------

def test_answering_no_is_not_social_media(app):
    """"0" is a non-empty string; bool() called it True."""
    is_social, platforms, error = _parse(app, {"is_social_media": "0"})

    assert is_social is False
    assert platforms == ""
    assert error is None


def test_answering_no_ignores_any_platforms_left_ticked(app):
    """Switching the answer back to No must not smuggle the platforms the
    person ticked while it said Yes."""
    is_social, platforms, error = _parse(app, {
        "is_social_media": "0",
        "social_platforms": ["instagram", "facebook"],
    })

    assert is_social is False
    assert platforms == ""
    assert error is None


def test_answering_yes_with_platforms(app):
    is_social, platforms, error = _parse(app, {
        "is_social_media": "1",
        "social_platforms": ["instagram"],
    })

    assert is_social is True
    assert "instagram" in platforms
    assert error is None


def test_answering_yes_without_platforms_is_refused(app):
    """A social task with no platform cannot be sent to the Studio, so it
    is caught at the form rather than at approval time."""
    is_social, platforms, error = _parse(app, {"is_social_media": "1"})

    assert is_social is None
    assert platforms is None
    assert "platform" in error.lower()


def test_the_error_names_the_question_that_is_on_screen(app):
    """It used to say 'uncheck "This task is for social media"', which is
    no longer anywhere on the form."""
    _is_social, _platforms, error = _parse(app, {"is_social_media": "1"})

    assert "social media post" in error.lower()
    assert "uncheck" not in error.lower()


# ------------------------------------------------------------------
# Still tolerant of the checkbox spelling
# ------------------------------------------------------------------

def test_an_absent_answer_is_refused(app):
    """The question is now MANDATORY with nothing pre-selected, so an absent
    (or blank) answer is refused rather than silently defaulting to No."""
    is_social, platforms, error = _parse(app, {})
    assert is_social is None and platforms is None
    assert "yes" in error.lower() and "no" in error.lower()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "On", "YES"])
def test_the_yes_spellings_all_count(app, value):
    is_social, _platforms, _error = _parse(app, {
        "is_social_media": value, "social_platforms": ["instagram"]})
    assert is_social is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "Off", "NO"])
def test_the_no_spellings_all_count(app, value):
    is_social, platforms, error = _parse(app, {"is_social_media": value})
    assert is_social is False
    assert platforms == ""
    assert error is None


@pytest.mark.parametrize("value", ["", "  ", "maybe", "xyz"])
def test_a_blank_or_unrecognised_answer_is_refused(app, value):
    """Only an explicit Yes/No counts; a blank or junk value is refused."""
    is_social, platforms, error = _parse(app, {"is_social_media": value})
    assert is_social is None and platforms is None and error


# ------------------------------------------------------------------
# The form actually renders the question
# ------------------------------------------------------------------

def test_the_create_form_asks_the_question(app, make_user, login, client):
    manager = make_user("super_admin")
    login(manager)
    html = client.get("/tasks/add").get_data(as_text=True)

    assert "Is this a social media post?" in html
    assert 'type="radio"' in html
    assert 'name="is_social_media"' in html
    assert 'value="0"' in html and 'value="1"' in html
    # NOTHING is pre-selected any more — the question is mandatory, so the
    # person has to make a deliberate choice rather than accept a default.
    assert "checked" not in html.split('name="is_social_media"')[1][:400]
    assert "This task is for social media" not in html
