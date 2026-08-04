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


# -- Grid drag-reorder ------------------------------------------------------

def test_grid_marks_scheduled_cells_movable(client, login, make_user, make_target):
    make_target(platform="instagram")            # scheduled -> movable
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/social/grid")
    assert b"is-movable" in r.data and b'draggable="true"' in r.data


def _add_ig_target(session, account_id, scheduled_for):
    """A second IG target on an existing account (make_target hardcodes one
    external_id, so it can't build two accounts of the same platform)."""
    from app.models import SocialMediaAsset, SocialPost, SocialPostTarget
    post = SocialPost(title="t2", base_caption="c", status="approved")
    session.add(post)
    session.flush()
    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=account_id,
        platform="instagram", post_type="image", caption="hi",
        status="scheduled", scheduled_for=scheduled_for)
    session.add(target)
    session.flush()
    session.add(SocialMediaAsset(
        target_id=target.id, source="upload", object_key="y.jpg", role="main"))
    session.commit()
    return target


def test_grid_reorder_swaps_scheduled_times(
        client, login, make_user, make_target, session):
    from datetime import timedelta

    from app.models import SocialPostTarget
    acct, _, t1 = make_target(platform="instagram")
    t2 = _add_ig_target(session, acct.id, t1.scheduled_for - timedelta(hours=2))
    id1, id2, time1, time2 = t1.id, t2.id, t1.scheduled_for, t2.scheduled_for
    assert time1 != time2
    later, earlier = max(time1, time2), min(time1, time2)

    login(make_user("employee", permissions=["manage_social"]))
    # order = [id1, id2] newest-first -> id1 takes the later slot, id2 the earlier.
    r = client.post("/social/grid/reorder", data={"order": f"{id1},{id2}"})
    assert r.status_code == 200 and r.get_json()["moved"] == 2

    session.expire_all()
    assert session.get(SocialPostTarget, id1).scheduled_for == later
    assert session.get(SocialPostTarget, id2).scheduled_for == earlier
    # The set of scheduled times is preserved — only who sits in which changed.


def test_grid_reorder_ignores_non_instagram(
        client, login, make_user, make_target):
    _, _, ig = make_target(platform="instagram")
    _, _, fake = make_target(platform="fake")
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/social/grid/reorder",
                    data={"order": f"{ig.id},{fake.id}"})
    # Only one movable IG target in the set -> nothing to swap.
    assert r.status_code == 200 and r.get_json()["moved"] == 0


def test_grid_reorder_forbidden_without_permission(client, login, make_user):
    login(make_user("employee"))                 # no manage_social — 403 pre-DB
    r = client.post("/social/grid/reorder", data={"order": "1,2"})
    assert r.status_code == 403
