"""A confirm() may not be built out of template data.

The pattern that made this necessary:

    onsubmit="return confirm('Disconnect {{ a.display_name }}?')"

which assembles a JavaScript string literal inside an HTML attribute out of
user-authored text. Jinja escapes the name for HTML - an apostrophe becomes
&#39; - and the HTML parser turns it back into ' BEFORE the JavaScript is
parsed. A Page called "O'Brien Dental" therefore produced

    return confirm('Disconnect O'Brien Dental?')

a syntax error. The handler never compiled, no dialog appeared, and the
form submitted immediately: the channel disconnected with no confirmation.

Nothing failed loudly. The page rendered, the button worked, and the only
symptom was a safety prompt that quietly stopped existing - on precisely
the destructive actions it guards. So the guard is a test rather than a
convention: data-confirm carries the message as an attribute VALUE, which
the parser hands over as text and never as code.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"

#: onsubmit="return confirm('…')" - the message between the quotes.
INLINE_CONFIRM = re.compile(
    r"""onsubmit\s*=\s*"return confirm\('(.*?)'\);?"\s*(?=[\s>])""", re.S)


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def _offenders():
    """(template, message) for every inline confirm carrying template data."""
    found = []
    for path in _templates():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for message in INLINE_CONFIRM.findall(text):
            if "{{" in message or "{%" in message:
                found.append((path.name, message.strip()[:70]))
    return found


def test_no_confirm_dialog_is_built_from_template_data():
    offenders = _offenders()

    assert not offenders, (
        "these build a JS string out of template data inside an HTML "
        "attribute, so an apostrophe in the value silently removes the "
        "confirmation dialog - use data-confirm=\"…\" instead:\n  "
        + "\n  ".join("%s: %s" % pair for pair in offenders)
    )


def test_the_handler_that_makes_data_confirm_work_exists():
    """data-confirm is inert markup without it - every one of these forms
    would submit straight through with no prompt at all."""
    script = (Path(__file__).resolve().parent.parent
              / "app" / "static" / "js" / "confirm-submit.js")

    assert script.exists(), "confirm-submit.js is missing"

    source = script.read_text(encoding="utf-8", errors="ignore")
    assert "data-confirm" in source
    assert "preventDefault" in source

    # Capture phase, or Turbo picks the submission up before the answer.
    assert re.search(r"addEventListener\(\s*[\"']submit[\"'].*?true\s*\)",
                     source, re.S), (
        "the listener must run in the capture phase, ahead of Turbo"
    )


def test_the_handler_is_loaded_app_wide():
    """It is loaded from base.html because data-confirm is used across the
    ERP and the Studio, which have different shells."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8",
                                               errors="ignore")
    assert "confirm-submit.js" in base


@pytest.mark.parametrize("template,needle", [
    ("social/accounts.html", "Disconnect"),
    ("permissions/user_permissions.html", "Reset"),
    ("social/settings.html", "hashtag set"),
])
def test_the_dialogs_that_carry_a_free_text_name_survived_the_move(
        template, needle):
    """These four interpolate names a person typed - a client, a colleague,
    a hashtag set - so they are the ones the bug actually reached. Pin that
    they still ask before doing the destructive thing."""
    text = (TEMPLATES / template).read_text(encoding="utf-8", errors="ignore")

    confirms = re.findall(r'data-confirm="([^"]*)"', text)

    assert any(needle in c for c in confirms), (
        "%s no longer asks for confirmation (looked for %r in %s)"
        % (template, needle, confirms)
    )


def test_data_confirm_messages_never_contain_a_raw_double_quote():
    """The attribute is double-quoted, so a literal " in the message would
    end it early and spray the rest into the tag as attributes."""
    bad = []
    for path in _templates():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for message in re.findall(r'data-confirm="([^"]*)"', text):
            if '"' in message:
                bad.append((path.name, message[:60]))

    assert not bad, bad
