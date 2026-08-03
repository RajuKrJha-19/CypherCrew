"""Who may open whose performance page.

The page is gated twice: `view_team_performance` (or manage_users) to reach the
route at all, then a per-person fence. That fence was may_administer(), which
is strictly downward - so it refused your OWN page too, and an admin could read
every report's numbers but not their own. Reading your own figures carries none
of the escalation risk may_administer() guards, so self is now always allowed;
everything else about the fence is unchanged.
"""


def _perf(client, user_id):
    return client.get(f"/users/{user_id}/performance", follow_redirects=False)


# -- the fix: your own page ------------------------------------------------

def test_admin_can_open_their_own_performance(client, make_user, login):
    admin = make_user("admin")
    login(admin)
    assert _perf(client, admin.id).status_code == 200


def test_lead_with_the_permission_can_open_their_own(client, make_user, login):
    lead = make_user("social_media_manager",
                     permissions=["view_team_performance"])
    login(lead)
    assert _perf(client, lead.id).status_code == 200


def test_owner_still_sees_their_own(client, make_user, login):
    owner = make_user("super_admin")
    login(owner)
    assert _perf(client, owner.id).status_code == 200


# -- unchanged: everything that was already refused stays refused ----------

def test_employee_without_the_permission_still_cannot_open_their_own(
        client, make_user, login):
    """Self-access rides on top of the route's own permission gate - it does
    not become a way in for someone who could never reach the page."""
    emp = make_user("employee")
    login(emp)
    assert _perf(client, emp.id).status_code == 302


def test_admin_still_cannot_open_a_peer_admins_page(client, make_user, login):
    actor = make_user("admin")
    peer = make_user("admin")
    login(actor)
    assert _perf(client, peer.id).status_code == 302


def test_admin_can_still_open_a_reports_page(client, make_user, login):
    actor = make_user("admin")
    report = make_user("employee")
    login(actor)
    assert _perf(client, report.id).status_code == 200
