"""Posting queue: per-channel slots + 'add to queue' next-open-slot."""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import (SocialAccount, SocialPost, SocialPostTarget,
                        SocialPostingSlot)
from app.social.services import queue_slots

_IST = timedelta(hours=5, minutes=30)


def _acct(platform="facebook"):
    a = SocialAccount(platform=platform, external_id=f"Q-{platform}",
                      display_name=platform, account_type="page",
                      status="active")
    db.session.add(a)
    db.session.flush()
    return a


def test_default_slots_used_when_none_set(session):
    a = _acct()
    db.session.commit()
    after = datetime(2026, 8, 3, 3, 0)
    s = queue_slots.next_open_slot(a.id, after=after)
    assert s > after
    ist = s + _IST
    assert ist.weekday() in (0, 1, 2, 3, 4)                 # weekday default
    assert (ist.hour, ist.minute) in [(10, 0), (13, 0), (17, 0)]


def test_custom_slots_and_taken_slot_is_skipped(session):
    a = _acct()
    db.session.commit()
    after = datetime(2026, 8, 3, 3, 30)                     # IST 09:00
    wd = (after + _IST).weekday()
    queue_slots.set_slots(a.id, [(wd, 600), (wd, 780)])     # 10:00, 13:00

    s1 = queue_slots.next_open_slot(a.id, after=after)
    ist1 = s1 + _IST
    assert (ist1.hour, ist1.minute) == (10, 0) and ist1.weekday() == wd

    # Occupy 10:00 -> the next open slot is 13:00.
    post = SocialPost(status="scheduled", title="x", base_caption="c")
    db.session.add(post)
    db.session.flush()
    db.session.add(SocialPostTarget(
        social_post_id=post.id, social_account_id=a.id, platform="facebook",
        post_type="image", status="scheduled", scheduled_for=s1))
    db.session.commit()

    s2 = queue_slots.next_open_slot(a.id, after=after)
    ist2 = s2 + _IST
    assert (ist2.hour, ist2.minute) == (13, 0) and ist2.weekday() == wd


def test_set_slots_dedups_and_validates(session):
    a = _acct()
    db.session.commit()
    # dup (0,600); bad weekday 9; bad minute 1500; valid (1,540)
    n = queue_slots.set_slots(
        a.id, [(0, 600), (0, 600), (9, 10), (2, 1500), (1, 540)])
    assert n == 2
    got = {(s.weekday, s.minute)
           for s in SocialPostingSlot.query.filter_by(social_account_id=a.id)}
    assert got == {(0, 600), (1, 540)}


def test_add_to_queue_assigns_the_next_slot(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    a = _acct()
    db.session.flush()
    queue_slots.set_slots(a.id, [(0, 600)])                 # Mon 10:00 only
    db.session.commit()

    value = "social_uploads/x.jpg::image/jpeg"
    client.post("/social/posts", data={
        "title": "q1", "post_type": "image", "caption": "hi",
        "upload_media": value,
        "account_ids": [str(a.id)],
        "publish_mode": "queue",
    }, follow_redirects=True)

    post = SocialPost.query.filter_by(title="q1").first()
    t = post.targets[0]
    assert t.scheduled_for is not None
    ist = t.scheduled_for + _IST
    assert ist.weekday() == 0 and (ist.hour, ist.minute) == (10, 0)
    assert t.scheduled_for > datetime.utcnow()              # in the future


def test_save_posting_slots_route(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    a = _acct()
    db.session.commit()
    client.post(f"/social/accounts/{a.id}/slots",
                data={"slot": ["0|10:00", "2|13:30"]}, follow_redirects=True)
    got = {(s.weekday, s.minute)
           for s in SocialPostingSlot.query.filter_by(social_account_id=a.id)}
    assert got == {(0, 600), (2, 810)}
