"""Medium-severity security findings from the audit.

None of these was exploitable from a browser on the day it was found. All four
are the same shape: a fence that exists somewhere in the app and is missing
somewhere else that reaches the same thing.

  * the cron endpoints accepted their shared secret in the query string, where
    nginx, Cloudflare and gunicorn all write it to disk, and compared it with
    `==` rather than a constant-time comparison;
  * SESSION_COOKIE_SECURE defaulted to False, so a deployment that simply did
    not mention it served session cookies over plain HTTP in silence;
  * "Apply role defaults" wrote a role's whole default set with none of the
    META_CODES filtering the checkbox form applies;
  * two multipart routes took object_key straight from the request body while
    the third validated it.
"""

import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# M1 - cron secrets
# ----------------------------------------------------------------------

def test_no_guard_reads_a_secret_from_the_query_string():
    source = (ROOT / "app" / "routes" / "internal.py").read_text(
        encoding="utf-8", errors="ignore")

    for i, line in enumerate(source.splitlines(), 1):
        code = line.split("#", 1)[0]
        for bad in ('args.get("token")', "args.get('token')",
                    'args.get("secret")', "args.get('secret')"):
            assert bad not in code, (
                "internal.py:%d reads a shared secret from the URL, which "
                "every proxy in front of this app writes to its access log: "
                "%s" % (i, line.strip()))


def test_the_comparison_is_constant_time():
    from app.routes import internal

    assert "compare_digest" in inspect.getsource(internal._secret_ok)


def test_an_unset_secret_keeps_the_endpoint_closed(app):
    """Fail-closed is the property that makes a missing config safe."""
    from app.routes.internal import _secret_ok

    with app.test_request_context(headers={"X-Test": "anything"}):
        assert _secret_ok(None, "X-Test") is False
        assert _secret_ok("", "X-Test") is False


def test_a_missing_header_is_refused(app):
    from app.routes.internal import _secret_ok

    with app.test_request_context():
        assert _secret_ok("s3cret", "X-Test") is False


def test_the_right_header_is_accepted(app):
    from app.routes.internal import _secret_ok

    with app.test_request_context(headers={"X-Test": "s3cret"}):
        assert _secret_ok("s3cret", "X-Test") is True


def test_a_wrong_header_is_refused(app):
    from app.routes.internal import _secret_ok

    with app.test_request_context(headers={"X-Test": "s3cre"}):
        assert _secret_ok("s3cret", "X-Test") is False


def test_the_token_in_the_url_no_longer_works(app):
    """The behaviour that changes for operators: a cron job still passing
    ?token= must now be refused rather than quietly working."""
    from app.routes.internal import _secret_ok

    with app.test_request_context("/internal/x?token=s3cret"):
        assert _secret_ok("s3cret", "X-Reminder-Token") is False


# ----------------------------------------------------------------------
# M2 - cookie flags
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "SESSION_COOKIE_SECURE", "REMEMBER_COOKIE_SECURE",
])
def test_secure_cookies_default_on(name, monkeypatch):
    """A deployment that does not mention these must get the safe value. The
    developer opting out on localhost is the one who should have to say so."""
    import importlib

    monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    import config
    reloaded = importlib.reload(config)
    try:
        assert getattr(reloaded.Config, name) is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# ----------------------------------------------------------------------
# M5 - the defaults fence
# ----------------------------------------------------------------------

def test_apply_defaults_applies_the_meta_fence():
    from app.routes import permissions

    source = inspect.getsource(permissions.apply_defaults)

    assert "META_CODES" in source, (
        "user_permissions refuses to let a non-owner grant manage_users, and "
        "this button wrote the role's whole default set with no filter - the "
        "way around the fence"
    )
    assert "is_owner(current_user.role)" in source


def test_the_owner_is_still_unfenced():
    from app.routes import permissions

    source = inspect.getsource(permissions.apply_defaults)

    assert "if not roles.is_owner(current_user.role):" in source, (
        "the fence must apply to non-owners only, or the owner loses the "
        "ability to set up an administrator at all"
    )


def test_no_role_default_currently_contains_a_meta_code():
    """Not a fix, a tripwire. The escalation this fence closes only becomes
    reachable when someone adds a meta code to a role's defaults, and that
    edit would look entirely innocuous."""
    from app.routes.permissions import META_CODES
    from app.utils import roles

    for value in roles.ALL_ROLE_VALUES:
        if roles.is_owner(value) or roles.is_management(value):
            continue
        overlap = roles.defaults_for(value) & META_CODES
        assert not overlap, (
            "%s now starts with %s - allowed, but it means non-owners can "
            "hand that out via Apply role defaults unless the fence holds"
            % (value, sorted(overlap)))


# ----------------------------------------------------------------------
# M11 - multipart object_key
# ----------------------------------------------------------------------

class _Task:
    task_code = "CC-1"


@pytest.mark.parametrize("key,ok", [
    ("clients/acme/TASK-CC-1/submission/a.mp4", True),
    # Another task's upload - the whole point.
    ("clients/acme/TASK-CC-2/submission/a.mp4", False),
    # Right task, wrong folder.
    ("clients/acme/TASK-CC-1/brief/a.mp4", False),
    # Outside the client tree entirely.
    ("social_uploads/a.mp4", False),
    ("", False),
    (None, False),
])
def test_only_this_tasks_submission_keys_are_accepted(key, ok):
    from app.routes.tasks import _submission_key_belongs_to

    assert _submission_key_belongs_to(_Task(), key) is ok


def test_a_task_with_no_code_matches_nothing():
    """A NULL task_code would otherwise build the prefix "/TASK-/submission/"
    and match keys belonging to nobody in particular."""
    from app.routes.tasks import _submission_key_belongs_to

    class _NoCode:
        task_code = None

    assert _submission_key_belongs_to(
        _NoCode(), "clients/acme/TASK-CC-1/submission/a.mp4") is False


@pytest.mark.parametrize("route", [
    "get_submission_multipart_part_url",
    "abort_submission_multipart_upload",
])
def test_both_presign_routes_check_the_key(route):
    from app.routes import tasks

    source = inspect.getsource(getattr(tasks, route))

    assert "_submission_key_belongs_to" in source


# ----------------------------------------------------------------------
# M13 - one gate on the performance page
# ----------------------------------------------------------------------

def test_the_documented_permission_opens_the_performance_page():
    from app.routes import users

    source = inspect.getsource(users.user_performance)

    assert "can_view_team_performance" in source, (
        "the catalog says view_team_performance opens 'any individual's "
        "performance page', but the route asked for manage_users - so the "
        "grant produced a link that bounced"
    )


def test_the_per_person_fence_is_still_there():
    """Widening the entry gate must not widen whose data you can read."""
    from app.routes import users

    assert "may_administer" in inspect.getsource(users.user_performance)
