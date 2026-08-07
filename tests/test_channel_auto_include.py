"""A channel that rides along on every post for its client group.

The case: a holding company (no page of its own) owns three institution pages
and one personal-brand page that is meant to carry everything the institutions
publish. Ticking that fourth channel by hand on every post is the kind of step
that gets forgotten silently - the omission only shows up later as a gap on
that page.

Two things had to change for it. The channel is pre-selected on every post for
its client group; and the client-safety rule, which allowed a channel only for
the ONE client it was bound to, now allows it across that client's family -
because the personal-brand page belongs to the holding company while the posts
are written for its institutions.

The safety property is unchanged where it matters: the hierarchy is one level
deep, so a family is a parent and its children and stops there. An unrelated
client is still refused, by the server as well as the composer.
"""
from app.extensions import db
from app.models import Client, SocialAccount
from app.routes.social import _account_client_groups, _channel_client_ok
from app.social.tokens.vault import get_vault
from tests.conftest import PYTEST_EMAIL_PREFIX


def _client(session, name, parent=None):
    c = Client(client_name=f"{PYTEST_EMAIL_PREFIX}{name}", status="active",
               parent_client_id=(parent.id if parent else None))
    session.add(c)
    session.commit()
    return c


def _account(session, name, client=None, auto=False):
    a = SocialAccount(
        platform="facebook", external_id=f"AI-{name}", display_name=name,
        account_type="page", status="active",
        client_id=(client.id if client else None), auto_include=auto,
        token_ciphertext=get_vault().encrypt("AT"), token_key_version=1)
    session.add(a)
    session.commit()
    return a


def _wipe():
    # Audit rows first: the routes under test record against the account, and
    # social_audit_logs.account_id is a real foreign key.
    from app.models import SocialAuditLog

    ids = [a.id for a in SocialAccount.query.filter(
        SocialAccount.external_id.like("AI-%")).all()]
    if ids:
        SocialAuditLog.query.filter(
            SocialAuditLog.account_id.in_(ids)).delete(
                synchronize_session=False)
        SocialAccount.query.filter(SocialAccount.id.in_(ids)).delete(
            synchronize_session=False)
    db.session.commit()
    rows = Client.query.filter(
        Client.client_name.like(f"{PYTEST_EMAIL_PREFIX}%")).all()
    for c in [r for r in rows if r.parent_client_id]:
        db.session.delete(c)
    db.session.commit()
    for c in Client.query.filter(
            Client.client_name.like(f"{PYTEST_EMAIL_PREFIX}%")).all():
        db.session.delete(c)
    db.session.commit()


# ======================================================================
# The client-safety rule, widened to the family and no further
# ======================================================================

def test_an_unbound_channel_is_allowed_anywhere(app, session):
    with app.app_context():
        assert _channel_client_ok(None, 123) is True


def test_a_post_with_no_client_allows_any_channel(app, session):
    with app.app_context():
        assert _channel_client_ok(123, None) is True


def test_a_channel_is_allowed_for_its_own_client(app, session):
    try:
        c = _client(session, "own")
        with app.app_context():
            assert _channel_client_ok(c.id, c.id) is True
    finally:
        _wipe()


def test_a_parents_channel_is_allowed_for_its_sub_client(app, session):
    """The whole point: the holding company owns the page, the institution
    writes the post."""
    try:
        holding = _client(session, "holding")
        inst = _client(session, "institution", parent=holding)
        with app.app_context():
            assert _channel_client_ok(holding.id, inst.id) is True
    finally:
        _wipe()


def test_a_sub_clients_channel_is_allowed_for_its_parent(app, session):
    try:
        holding = _client(session, "holding")
        inst = _client(session, "institution", parent=holding)
        with app.app_context():
            assert _channel_client_ok(inst.id, holding.id) is True
    finally:
        _wipe()


def test_two_sub_clients_of_one_parent_share(app, session):
    """Siblings are family - Polytechnic's post may use the group's page."""
    try:
        holding = _client(session, "holding")
        a = _client(session, "instA", parent=holding)
        b = _client(session, "instB", parent=holding)
        with app.app_context():
            assert _channel_client_ok(a.id, b.id) is True
    finally:
        _wipe()


def test_an_unrelated_client_is_still_refused(app, session):
    """The property that must not have been lost."""
    try:
        holding = _client(session, "holding")
        inst = _client(session, "institution", parent=holding)
        other = _client(session, "unrelated")
        with app.app_context():
            assert _channel_client_ok(other.id, inst.id) is False
            assert _channel_client_ok(inst.id, other.id) is False
            assert _channel_client_ok(holding.id, other.id) is False
    finally:
        _wipe()


def test_a_missing_post_client_is_refused_not_crashed(app, session):
    """A deleted client id must end as False, never an exception."""
    try:
        c = _client(session, "own")
        with app.app_context():
            assert _channel_client_ok(c.id, 99999999) is False
    finally:
        _wipe()


# ======================================================================
# The group the composer is handed
# ======================================================================

def test_the_group_covers_parent_and_siblings(app, session):
    try:
        holding = _client(session, "holding")
        a = _client(session, "instA", parent=holding)
        b = _client(session, "instB", parent=holding)
        acct = _account(session, "grouppage", client=holding)
        with app.app_context():
            groups = _account_client_groups()
        assert set(groups[acct.id]) == {holding.id, a.id, b.id}
    finally:
        _wipe()


def test_a_lone_clients_group_is_just_itself(app, session):
    try:
        solo = _client(session, "solo")
        acct = _account(session, "solopage", client=solo)
        with app.app_context():
            groups = _account_client_groups()
        assert groups[acct.id] == [solo.id]
    finally:
        _wipe()


def test_an_unbound_channel_is_not_in_the_map(app, session):
    """It is offered everywhere already; a group would say nothing."""
    try:
        acct = _account(session, "agencywide", client=None)
        with app.app_context():
            groups = _account_client_groups()
        assert acct.id not in groups
    finally:
        _wipe()


# ======================================================================
# The flag itself
# ======================================================================

def test_it_is_off_by_default(app, session):
    try:
        c = _client(session, "own")
        acct = _account(session, "plain", client=c)
        assert acct.auto_include is False
    finally:
        _wipe()


def test_a_channel_can_be_marked_and_unmarked(session, client, make_user,
                                              login):
    login(make_user("admin", permissions=["manage_social"]))
    try:
        c = _client(session, "own")
        acct = _account(session, "ride", client=c)
        client.post(f"/social/accounts/{acct.id}/auto-include",
                    data={"auto_include": "1"}, follow_redirects=True)
        db.session.refresh(acct)
        assert acct.auto_include is True

        client.post(f"/social/accounts/{acct.id}/auto-include",
                    data={}, follow_redirects=True)
        db.session.refresh(acct)
        assert acct.auto_include is False
    finally:
        _wipe()


def test_an_agency_wide_channel_cannot_be_marked(session, client, make_user,
                                                 login):
    """"Every post for its client" has no subject without a client."""
    login(make_user("admin", permissions=["manage_social"]))
    try:
        acct = _account(session, "nobody", client=None)
        resp = client.post(f"/social/accounts/{acct.id}/auto-include",
                           data={"auto_include": "1"}, follow_redirects=True)
        assert "Assign this channel to a client first" in resp.get_data(as_text=True)
        db.session.refresh(acct)
        assert acct.auto_include is False
    finally:
        _wipe()


def test_unbinding_a_channel_clears_the_flag(session, client, make_user,
                                             login):
    """Otherwise the rule would linger as a statement about nothing, and come
    back the moment the channel was reassigned."""
    login(make_user("admin", permissions=["manage_social"]))
    try:
        c = _client(session, "own")
        acct = _account(session, "ride", client=c, auto=True)
        client.post(f"/social/accounts/{acct.id}/client",
                    data={"client_id": ""}, follow_redirects=True)
        db.session.refresh(acct)
        assert acct.client_id is None
        assert acct.auto_include is False
    finally:
        _wipe()


def test_marking_needs_channel_rights(session, client, make_user, login):
    login(make_user("employee"))
    try:
        c = _client(session, "own")
        acct = _account(session, "ride", client=c)
        client.post(f"/social/accounts/{acct.id}/auto-include",
                    data={"auto_include": "1"})
        db.session.refresh(acct)
        assert acct.auto_include is False
    finally:
        _wipe()


# ======================================================================
# What the composer is given
# ======================================================================

def test_the_composer_marks_a_ride_along_channel(session, client, make_user,
                                                 login):
    login(make_user("admin", permissions=["manage_social"]))
    try:
        holding = _client(session, "holding")
        inst = _client(session, "institution", parent=holding)
        _account(session, "ridealong", client=holding, auto=True)
        body = client.get("/social/compose").get_data(as_text=True)
        assert 'data-auto-include="1"' in body
        # ...and carries the family, so the filter can offer it while the post
        # is being written for the institution.
        assert f'data-group="{min(holding.id, inst.id)}' in body
    finally:
        _wipe()


def test_an_ordinary_channel_is_not_marked(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    try:
        c = _client(session, "own")
        _account(session, "plain", client=c)
        body = client.get("/social/compose").get_data(as_text=True)
        assert 'data-auto-include="1"' not in body
    finally:
        _wipe()
