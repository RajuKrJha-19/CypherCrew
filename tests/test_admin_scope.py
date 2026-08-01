"""Who may administer whom, and how long a session outlives its password.

Two holes the audit found, both in the same place - the user-administration
screens - and both invisible in normal use.

**Tier.** `may_administer` read `not is_management(target)`. MANAGEMENT_ROLES
is exactly super_admin and admin, so that sentence meant "anybody who is not
one of those two may be administered by anyone holding manage_users". A
social_media_intern with that permission could open a social_media_manager's
edit form and set their password - and that account carries publish_tasks and
approve_tasks, i.e. publish access to client social profiles. The docstring
said "only downwards"; nothing compared the actor to the target.

**Session.** Flask-Login's identity was the bare user id, so resetting a
password - the standard response to a stolen laptop or a phished cookie - left
the attacker signed in. `User.get_id` now mixes in a fingerprint of the
password hash, so the old cookie stops resolving on the next request.
"""

import pytest

from app.models.user import password_fingerprint
from app.utils import roles


# ----------------------------------------------------------------------
# rank / outranks - the primitive
# ----------------------------------------------------------------------

def test_rank_follows_tier_order():
    assert roles.rank("super_admin") == 0
    assert roles.rank("admin") == 1
    assert roles.rank("social_media_manager") == 2
    assert roles.rank("social_media_intern") == 5


def test_an_unknown_role_ranks_bottom():
    """A role somebody forgot to add to the catalog must never come out
    ranking above a real one - that would be a free promotion."""
    assert roles.rank("not_a_real_role") == len(roles.TIER_ORDER) - 1

    assert not roles.outranks("not_a_real_role", "social_media_intern")


def test_outranks_is_strict():
    """Peers cannot administer each other - two admins must not be able to
    reset one another's passwords."""
    assert not roles.outranks("admin", "admin")
    assert not roles.outranks("social_media_manager", "video_editor_manager")

    assert roles.outranks("admin", "social_media_manager")
    assert roles.outranks("social_media_manager", "social_media_intern")


def test_nobody_outranks_the_owner():
    for value in roles.ALL_ROLE_VALUES:
        if value == "super_admin":
            continue
        assert not roles.outranks(value, "super_admin"), value


def test_outranks_and_assignable_by_agree():
    """Both answer "only downwards" - one for the person, one for the role
    <select>. If they drift apart, one screen fences what the other lets
    through."""
    class _Actor:
        role = "admin"

    for value in roles.assignable_by(_Actor()):
        assert roles.outranks("admin", value), (
            "%s is offered in the role picker but the person holding it "
            "could not be administered" % value
        )


# ----------------------------------------------------------------------
# may_administer, through the route guard
# ----------------------------------------------------------------------

@pytest.fixture()
def may_administer(app):
    """The real guard, with a chosen actor pushed onto the request context."""
    from flask_login import login_user

    from app.routes.users import may_administer as _guard

    def _check(actor, target):
        with app.test_request_context():
            login_user(actor)
            return _guard(target)

    return _check


def test_a_manage_users_grant_does_not_reach_a_craft_manager(
        may_administer, make_user):
    """The escalation itself. The intern holds manage_users, so they reach the
    screen; the manager's account carries publish access, so taking it is the
    prize. Before the fix this returned True."""
    intern = make_user("social_media_intern", permissions=["manage_users"])
    manager = make_user("social_media_manager")

    assert may_administer(intern, manager) is False


def test_a_manage_users_grant_does_not_reach_a_peer(may_administer, make_user):
    intern = make_user("social_media_intern", permissions=["manage_users"])
    peer = make_user("video_editor_intern")

    assert may_administer(intern, peer) is False


def test_a_manage_users_grant_does_not_reach_an_admin(
        may_administer, make_user):
    intern = make_user("social_media_intern", permissions=["manage_users"])
    admin = make_user("admin")

    assert may_administer(intern, admin) is False


def test_downwards_still_works(may_administer, make_user):
    """The guard has to keep letting the legitimate case through, or the fix
    is just a lockout."""
    manager = make_user("social_media_manager", permissions=["manage_users"])
    junior = make_user("social_media_executive")

    assert may_administer(manager, junior) is True


def test_an_admin_may_administer_a_craft_manager(may_administer, make_user):
    admin = make_user("admin")
    manager = make_user("social_media_manager")

    assert may_administer(admin, manager) is True


def test_an_admin_may_not_administer_another_admin(may_administer, make_user):
    admin = make_user("admin")
    other = make_user("admin")

    assert may_administer(admin, other) is False


def test_the_owner_may_administer_anybody(may_administer, make_user):
    owner = make_user("super_admin")

    for role in ("admin", "social_media_manager", "employee"):
        assert may_administer(owner, make_user(role)) is True, role


# ----------------------------------------------------------------------
# The edit route, not just the predicate
# ----------------------------------------------------------------------

def test_the_edit_route_refuses_the_escalation(client, login, make_user):
    """The predicate is only worth what the route does with it."""
    intern = make_user("social_media_intern", permissions=["manage_users"])
    manager = make_user("social_media_manager")
    before = manager.password_hash

    login(intern)
    response = client.post(
        "/users/edit/%d" % manager.id,
        data={"name": manager.name, "email": manager.email,
              "role": manager.role, "status": "active",
              "password": "attacker-chosen-password"},
        follow_redirects=False,
    )

    assert response.status_code in (302, 403)

    from app.models import User
    assert User.query.get(manager.id).password_hash == before, (
        "the password was changed by somebody who does not outrank the target"
    )


# ----------------------------------------------------------------------
# Session identity
# ----------------------------------------------------------------------

def _signed_in(client):
    """Is this client still authenticated?

    Probed on "/" and read from the redirect target rather than the status
    code: dashboard.index redirects EITHER way - to the role's dashboard when
    signed in, to the login page when not - so 302 on its own says nothing.

    The `g` pop is the same one the `login` fixture documents. Flask-Login
    caches the resolved user on `g`, which belongs to the APP context, and a
    test holds one app context open across several requests - so without this
    the second probe answers from the cache and load_user is never consulted.
    In production every request gets a fresh app context, so this is a test
    artefact and not something the app has to do.
    """
    from flask import g, has_app_context

    if has_app_context():
        g.pop("_login_user", None)

    response = client.get("/", follow_redirects=False)
    if response.status_code != 302:
        return response.status_code == 200
    return "/auth/login" not in response.headers.get("Location", "")


def test_get_id_binds_the_password_to_the_session(make_user):
    user = make_user("employee")

    assert user.get_id() == "%d|%s" % (
        user.id, password_fingerprint(user.password_hash))


def test_get_id_changes_when_the_password_changes(make_user, session):
    from werkzeug.security import generate_password_hash

    user = make_user("employee")
    before = user.get_id()

    user.password_hash = generate_password_hash("a-different-password")
    session.commit()

    assert user.get_id() != before


def test_a_session_opened_under_the_old_password_stops_working(
        client, login, make_user, session):
    """The whole point: reset the password and the stolen cookie dies."""
    from werkzeug.security import generate_password_hash

    user = make_user("employee")
    login(user)

    assert _signed_in(client)

    user.password_hash = generate_password_hash("reset-after-the-laptop-went")
    session.commit()

    assert not _signed_in(client)


def test_a_bare_id_cookie_is_rejected(client, make_user):
    """Cookies written before this shipped carry a bare id. They must not keep
    working, or the change does nothing for exactly the sessions it exists to
    end."""
    user = make_user("employee")

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True

    assert not _signed_in(client)


def test_a_garbage_identity_does_not_raise(client):
    """load_user parses the cookie; a malformed one must return anonymous,
    not 500."""
    with client.session_transaction() as sess:
        sess["_user_id"] = "not-a-number|deadbeef"
        sess["_fresh"] = True

    assert not _signed_in(client)


def test_another_users_session_survives_the_reset(
        client, login, make_user, session):
    from werkzeug.security import generate_password_hash

    victim = make_user("employee")
    bystander = make_user("employee")

    login(bystander)
    victim.password_hash = generate_password_hash("only-the-victim-changed")
    session.commit()

    assert _signed_in(client)
