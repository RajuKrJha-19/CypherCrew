"""The originating task's code is visible on every Studio surface.

Published (history) already carried it; a post is chased back to its task from
the Posts list and the Queue board just as often, so the code has to be on all
three. A post composed directly in Studio has no task and reads "Direct" rather
than an empty cell.
"""
from app.extensions import db
from app.models import PublishJob, SocialPost


def _posts_page(client):
    return client.get("/social/drafts").get_data(as_text=True)


def _queue_page(client):
    return client.get("/social/queue").get_data(as_text=True)


def test_posts_list_shows_the_task_code(session, client, make_user, login,
                                        make_task):
    user = make_user("admin", permissions=["manage_social"])
    login(user)
    task = make_task(user)

    session.add(SocialPost(status="draft", title="TaskLinkedPostXYZ",
                           base_caption="a", task_id=task.id))
    session.commit()

    body = _posts_page(client)
    assert "TaskLinkedPostXYZ" in body
    assert f"#{task.task_code}" in body
    # ...and it links to the task, not just prints the number.
    assert f"/tasks/{task.id}" in body


def test_posts_list_marks_a_studio_composed_post_as_direct(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    session.add(SocialPost(status="draft", title="DirectPostXYZ",
                           base_caption="a"))
    session.commit()

    body = _posts_page(client)
    assert "DirectPostXYZ" in body
    assert "Direct" in body


def test_queue_card_shows_the_task_code(session, client, make_user, login,
                                        make_task, make_target):
    user = make_user("admin", permissions=["manage_social"])
    login(user)
    task = make_task(user)
    _acct, post, target = make_target()
    post.task_id = task.id
    session.add(PublishJob(target_id=target.id, state="queued",
                           idempotency_key=f"pytest-taskid-{target.id}"))
    session.commit()

    body = _queue_page(client)
    assert f"#{task.task_code}" in body
    assert f"/tasks/{task.id}" in body


def test_queue_card_marks_a_studio_composed_post_as_direct(
        session, client, make_user, login, make_target):
    login(make_user("admin", permissions=["manage_social"]))
    _acct, _post, target = make_target()          # no task attached
    session.add(PublishJob(target_id=target.id, state="queued",
                           idempotency_key=f"pytest-direct-{target.id}"))
    session.commit()

    assert "Direct" in _queue_page(client)


def test_queue_does_not_n_plus_one_on_the_task(session, client, make_user,
                                               login, make_task, make_target):
    """The board renders up to 400 cards, so the task must come back with the
    job - a lazy load per card would be 400 extra queries.

    Each card gets a DISTINCT task on purpose: with one shared task the
    identity map would serve every card after the first and a lazy load would
    look eager.
    """
    from datetime import datetime, timedelta

    from app.models import SocialPostTarget

    user = make_user("admin", permissions=["manage_social"])
    login(user)
    # One account (its (platform, external_id) is unique), many posts.
    acct, first_post, first_target = make_target()

    codes = []
    for i in range(4):
        task = make_task(user)
        codes.append(task.task_code)
        post = SocialPost(title=f"n1-{i}", base_caption="c", status="approved",
                          task_id=task.id)
        session.add(post)
        session.flush()
        target = SocialPostTarget(
            social_post_id=post.id, social_account_id=acct.id,
            platform="fake", post_type="image", caption="hi",
            status="scheduled",
            scheduled_for=datetime.utcnow() + timedelta(hours=1))
        session.add(target)
        session.flush()
        session.add(PublishJob(target_id=target.id, state="queued",
                               idempotency_key=f"pytest-n1-{target.id}"))
    session.commit()

    task_queries = []
    from sqlalchemy import event
    engine = db.session.get_bind()

    def _record(conn, cursor, statement, params, ctx, many):
        if "FROM tasks" in statement:
            task_queries.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        body = _queue_page(client)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    for code in codes:
        assert f"#{code}" in body
    # The jobs query joins every task in; nothing re-selects one per card.
    assert len(task_queries) <= 1, (
        f"expected the tasks to be eager-loaded, got {len(task_queries)} "
        f"separate task queries for {len(codes)} cards")
