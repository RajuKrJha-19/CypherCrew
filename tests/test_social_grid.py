"""Grid planner (Instagram feed view) + the inline best-time hint endpoint.
Both are read-only composer/planner helpers gated by the social permission.
"""


# -- Grid planner -----------------------------------------------------------

def test_grid_renders_for_a_permitted_user(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/grid")
    assert r.status_code == 200


def test_grid_empty_state_when_no_instagram_posts(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/grid")
    assert r.status_code == 200
    assert b"No Instagram posts yet" in r.data


def test_grid_lists_instagram_targets(client, login, make_user, make_target):
    make_target(platform="instagram")           # scheduled IG target + asset
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/grid")
    assert r.status_code == 200
    assert b"ig-grid" in r.data                  # grid rendered, not empty state


def test_grid_ignores_non_instagram(client, login, make_user, make_target):
    make_target(platform="fake")                 # a non-IG channel
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/grid")
    assert r.status_code == 200
    assert b"No Instagram posts yet" in r.data   # nothing on the IG grid


def test_grid_forbidden_without_permission(client, login, make_user):
    login(make_user("employee"))                 # no manage_social
    r = client.get("/social/grid")
    assert r.status_code == 403


# -- Best-time hint ---------------------------------------------------------

def test_best_time_returns_a_slot_for_accounts(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/api/best-time?account_ids=1")
    assert r.status_code == 200
    data = r.get_json()
    # Even a channel with no configured slots gets sensible defaults, so a hint
    # is always available (value = the next open IST slot).
    assert data["value"] and data["label"]


def test_best_time_none_without_accounts(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/api/best-time")
    assert r.status_code == 200
    assert r.get_json()["value"] is None


def test_best_time_ignores_non_numeric_ids(client, login, make_user):
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/api/best-time?account_ids=abc,")
    assert r.status_code == 200
    assert r.get_json()["value"] is None


def test_best_time_forbidden_without_permission(client, login, make_user):
    login(make_user("employee"))                 # no manage_social
    r = client.get("/social/api/best-time?account_ids=1")
    assert r.status_code == 403
