"""Where a client sits in the hierarchy, and how it is moved.

Sub-clients existed but were write-once: the parent was chosen at creation and
the edit page said "Re-parenting is done by recreating the client." A client
set up on its own, or under the wrong group, had to be rebuilt.

Now it is one operation - set_parent(child, parent_or_none) - offered from two
places: the client's own page (a parent picker, where the big SaaS tools put
it) and the parent's page (pull an existing client in, next to "Add
Sub-client"). Both go through the same rules, because duplicating them per
screen is how one screen ends up a level behind the others.

The structure stays one level deep with no cycles. That is not an arbitrary
rule: Client.ordered_with_sub_clients renders exactly one level of
indentation, and a grandchild would simply not be drawn.
"""
from app.extensions import db
from app.models import Client
from app.routes.clients import (
    attachable_clients, can_move_under, eligible_parents,
)
from tests.conftest import PYTEST_EMAIL_PREFIX


def _client(session, name, parent=None, status="active"):
    c = Client(client_name=f"{PYTEST_EMAIL_PREFIX}{name}", status=status,
               parent_client_id=(parent.id if parent else None))
    session.add(c)
    session.commit()
    return c


def _wipe():
    """Children first - a parent row cannot go while one points at it."""
    rows = Client.query.filter(
        Client.client_name.like(f"{PYTEST_EMAIL_PREFIX}%")).all()
    for c in [r for r in rows if r.parent_client_id]:
        db.session.delete(c)
    db.session.commit()
    for c in Client.query.filter(
            Client.client_name.like(f"{PYTEST_EMAIL_PREFIX}%")).all():
        db.session.delete(c)
    db.session.commit()


def _admin(make_user, login):
    login(make_user("admin", permissions=["manage_clients"]))


def _move(client, child, parent):
    """The client's own page: pick a parent (empty = main client)."""
    return client.post(f"/clients/{child.id}/move",
                       data={"parent_client_id": (parent.id if parent else "")},
                       follow_redirects=True)


def _attach(client, parent, child):
    """The parent's page: pull an existing client in."""
    return client.post(f"/clients/{parent.id}/attach",
                       data={"child_id": child.id}, follow_redirects=True)


# ======================================================================
# The rule, on its own
# ======================================================================

def test_clearing_a_parent_is_always_allowed(session):
    """It cannot add a level or a cycle whatever the tree looks like."""
    try:
        top = _client(session, "t")
        sub = _client(session, "s", parent=top)
        assert can_move_under(sub, None) == (True, "")
        assert can_move_under(top, None) == (True, "")
    finally:
        _wipe()


def test_a_client_cannot_be_its_own_parent(session):
    try:
        solo = _client(session, "solo")
        ok, why = can_move_under(solo, solo)
        assert ok is False and "own sub-client" in why
    finally:
        _wipe()


def test_a_sub_client_cannot_become_a_parent(session):
    try:
        top = _client(session, "t")
        sub = _client(session, "s", parent=top)
        lone = _client(session, "l")
        ok, why = can_move_under(lone, sub)
        assert ok is False and "itself a sub-client" in why
    finally:
        _wipe()


def test_a_client_with_children_cannot_be_moved(session):
    """Its children would become grandchildren - the second level."""
    try:
        holder = _client(session, "holder")
        _client(session, "kid", parent=holder)
        elsewhere = _client(session, "elsewhere")
        ok, why = can_move_under(holder, elsewhere)
        assert ok is False and "second level" in why
    finally:
        _wipe()


def test_moving_somewhere_it_already_is_is_refused(session):
    try:
        top = _client(session, "t")
        sub = _client(session, "s", parent=top)
        ok, why = can_move_under(sub, top)
        assert ok is False and "already a sub-client" in why
    finally:
        _wipe()


def test_a_missing_client_is_a_message_not_a_crash(session):
    ok, why = can_move_under(None, None)
    assert ok is False and why


# ======================================================================
# The three transitions, through the client's own page
# ======================================================================

def test_a_main_client_can_be_moved_under_a_parent(session, client,
                                                   make_user, login):
    _admin(make_user, login)
    try:
        group = _client(session, "group")
        lone = _client(session, "lone")
        _move(client, lone, group)
        db.session.refresh(lone)
        assert lone.parent_client_id == group.id
    finally:
        _wipe()


def test_a_sub_client_can_be_moved_to_a_DIFFERENT_parent(session, client,
                                                          make_user, login):
    """The transition that was impossible before - it needed a rebuild."""
    _admin(make_user, login)
    try:
        first = _client(session, "first")
        second = _client(session, "second")
        child = _client(session, "child", parent=first)
        resp = _move(client, child, second)
        assert resp.status_code == 200
        db.session.refresh(child)
        assert child.parent_client_id == second.id
        assert "moved from" in resp.get_data(as_text=True)
    finally:
        _wipe()


def test_a_sub_client_can_be_made_a_main_client(session, client, make_user,
                                                login):
    _admin(make_user, login)
    try:
        top = _client(session, "top")
        child = _client(session, "child", parent=top)
        _move(client, child, None)
        db.session.refresh(child)
        assert child.parent_client_id is None
    finally:
        _wipe()


def test_a_refused_move_leaves_the_row_untouched(session, client, make_user,
                                                 login):
    _admin(make_user, login)
    try:
        holder = _client(session, "holder")
        _client(session, "kid", parent=holder)
        elsewhere = _client(session, "elsewhere")
        resp = _move(client, holder, elsewhere)
        assert resp.status_code == 200          # a message, never a 500
        db.session.refresh(holder)
        assert holder.parent_client_id is None
    finally:
        _wipe()


def test_siblings_and_the_old_parent_are_untouched(session, client,
                                                   make_user, login):
    _admin(make_user, login)
    try:
        first = _client(session, "first")
        second = _client(session, "second")
        moved = _client(session, "moved", parent=first)
        stayed = _client(session, "stayed", parent=first)
        _move(client, moved, second)
        db.session.refresh(first)
        db.session.refresh(stayed)
        assert stayed.parent_client_id == first.id
        assert first.parent_client_id is None
        assert first.status == "active"
    finally:
        _wipe()


# ======================================================================
# Nothing here may 404, 500, or half-apply
# ======================================================================

def test_a_non_numeric_parent_id_is_a_message(session, client, make_user,
                                              login):
    _admin(make_user, login)
    try:
        lone = _client(session, "lone")
        resp = client.post(f"/clients/{lone.id}/move",
                           data={"parent_client_id": "not-a-number"},
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(lone)
        assert lone.parent_client_id is None
    finally:
        _wipe()


def test_a_parent_that_no_longer_exists_is_a_message(session, client,
                                                     make_user, login):
    """A stale <select> in another tab must not 404 the page."""
    _admin(make_user, login)
    try:
        lone = _client(session, "lone")
        resp = client.post(f"/clients/{lone.id}/move",
                           data={"parent_client_id": "99999999"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "no longer exists" in resp.get_data(as_text=True)
        db.session.refresh(lone)
        assert lone.parent_client_id is None
    finally:
        _wipe()


def test_attaching_nothing_is_a_message(session, client, make_user, login):
    _admin(make_user, login)
    try:
        group = _client(session, "group")
        resp = client.post(f"/clients/{group.id}/attach",
                           data={"child_id": ""}, follow_redirects=True)
        assert resp.status_code == 200
        assert "Pick a client" in resp.get_data(as_text=True)
    finally:
        _wipe()


def test_moving_an_unknown_client_404s_rather_than_500s(session, client,
                                                        make_user, login):
    _admin(make_user, login)
    resp = client.post("/clients/99999999/move",
                       data={"parent_client_id": ""})
    assert resp.status_code == 404


def test_both_routes_are_post_only(session, client, make_user, login):
    """A state change must not sit behind a GET a prefetch can follow."""
    _admin(make_user, login)
    try:
        c = _client(session, "solo")
        assert client.get(f"/clients/{c.id}/move").status_code == 405
        assert client.get(f"/clients/{c.id}/attach").status_code == 405
    finally:
        _wipe()


def test_moving_needs_client_rights(session, client, make_user, login):
    login(make_user("employee"))
    try:
        group = _client(session, "group")
        lone = _client(session, "lone")
        _move(client, lone, group)
        db.session.refresh(lone)
        assert lone.parent_client_id is None
    finally:
        _wipe()


def test_attaching_needs_client_rights(session, client, make_user, login):
    login(make_user("employee"))
    try:
        group = _client(session, "group")
        lone = _client(session, "lone")
        _attach(client, group, lone)
        db.session.refresh(lone)
        assert lone.parent_client_id is None
    finally:
        _wipe()


# ======================================================================
# The parent's own page pulls a client in
# ======================================================================

def test_the_parent_page_can_pull_an_existing_client_in(session, client,
                                                        make_user, login):
    _admin(make_user, login)
    try:
        group = _client(session, "group")
        lone = _client(session, "lone")
        _attach(client, group, lone)
        db.session.refresh(lone)
        assert lone.parent_client_id == group.id
    finally:
        _wipe()


def test_the_parent_page_refuses_a_client_with_children(session, client,
                                                        make_user, login):
    _admin(make_user, login)
    try:
        group = _client(session, "group")
        holder = _client(session, "holder")
        kid = _client(session, "kid", parent=holder)
        _attach(client, group, holder)
        db.session.refresh(holder)
        db.session.refresh(kid)
        assert holder.parent_client_id is None, "two levels were created"
        assert kid.parent_client_id == holder.id
    finally:
        _wipe()


# ======================================================================
# The pickers offer exactly what the rules allow
# ======================================================================

def test_the_parent_picker_excludes_what_would_be_refused(session, app):
    try:
        me = _client(session, "me")
        ok = _client(session, "eligible")
        sub = _client(session, "sub", parent=ok)
        inactive = _client(session, "inactive", status="inactive")
        with app.test_request_context():
            names = {c.client_name for c in eligible_parents(me)}
        assert f"{PYTEST_EMAIL_PREFIX}eligible" in names
        assert f"{PYTEST_EMAIL_PREFIX}me" not in names        # itself
        assert f"{PYTEST_EMAIL_PREFIX}sub" not in names       # is a sub-client
        assert f"{PYTEST_EMAIL_PREFIX}inactive" not in names  # archived
    finally:
        _wipe()


def test_a_client_with_children_is_offered_no_parents(session, app):
    """It cannot move anywhere, so a picker would be a dead end."""
    try:
        holder = _client(session, "holder")
        _client(session, "kid", parent=holder)
        with app.test_request_context():
            assert eligible_parents(holder) == []
    finally:
        _wipe()


def test_the_attach_picker_excludes_what_would_be_refused(session, app):
    try:
        parent = _client(session, "parent")
        ok = _client(session, "eligible")
        already = _client(session, "already", parent=parent)
        holder = _client(session, "holder")
        _client(session, "kid", parent=holder)
        with app.test_request_context():
            names = {c.client_name for c in attachable_clients(parent)}
        assert f"{PYTEST_EMAIL_PREFIX}eligible" in names
        assert f"{PYTEST_EMAIL_PREFIX}already" not in names   # already here
        assert f"{PYTEST_EMAIL_PREFIX}holder" not in names    # has children
        assert f"{PYTEST_EMAIL_PREFIX}parent" not in names    # itself
    finally:
        _wipe()


def test_the_attach_picker_offers_a_client_held_elsewhere(session, app):
    """Re-parenting is allowed now, so a client under ANOTHER parent is a
    legitimate thing to pull in."""
    try:
        here = _client(session, "here")
        there = _client(session, "there")
        theirs = _client(session, "theirs", parent=there)
        with app.test_request_context():
            names = {c.client_name for c in attachable_clients(here)}
        assert f"{PYTEST_EMAIL_PREFIX}theirs" in names
    finally:
        _wipe()


def test_a_sub_client_page_offers_nothing_to_attach(session, app):
    try:
        top = _client(session, "top")
        sub = _client(session, "sub", parent=top)
        with app.test_request_context():
            assert attachable_clients(sub) == []
    finally:
        _wipe()


# ======================================================================
# What the screens say
# ======================================================================

def test_the_edit_page_offers_the_parent_picker(session, client, make_user,
                                                login):
    _admin(make_user, login)
    try:
        top = _client(session, "top")
        child = _client(session, "child", parent=top)
        body = client.get(f"/clients/{child.id}/edit").get_data(as_text=True)
        assert "Parent client" in body
        assert "No parent (main client)" in body
        assert f"/clients/{child.id}/move" in body
    finally:
        _wipe()


def test_a_parent_client_is_told_why_it_cannot_move(session, client,
                                                    make_user, login):
    """A picker it may not use would be a dead end, so it gets the reason
    instead."""
    _admin(make_user, login)
    try:
        holder = _client(session, "holder")
        _client(session, "kid", parent=holder)
        body = client.get(f"/clients/{holder.id}/edit").get_data(as_text=True)
        assert "parent client" in body
        assert "one level deep" in body
        assert f"/clients/{holder.id}/move" not in body
    finally:
        _wipe()


def test_the_stale_recreate_instruction_is_gone(session, client, make_user,
                                                login):
    _admin(make_user, login)
    try:
        top = _client(session, "top")
        child = _client(session, "child", parent=top)
        body = client.get(f"/clients/{child.id}/edit").get_data(as_text=True)
        assert "recreating the client" not in body
    finally:
        _wipe()


def test_a_parent_client_is_labelled_on_its_own_page(session, client,
                                                     make_user, login):
    """Which side of the relationship a client is on was only visible from the
    child; the parent said nothing until you scrolled to its list."""
    _admin(make_user, login)
    try:
        parent = _client(session, "parent")
        _client(session, "kid", parent=parent)
        body = client.get(f"/clients/{parent.id}").get_data(as_text=True)
        assert "parent client" in body
        assert "1 sub-client" in body
    finally:
        _wipe()


def test_a_lone_client_is_not_labelled_a_parent(session, client, make_user,
                                                login):
    _admin(make_user, login)
    try:
        solo = _client(session, "solo")
        body = client.get(f"/clients/{solo.id}").get_data(as_text=True)
        assert "parent client" not in body
    finally:
        _wipe()
