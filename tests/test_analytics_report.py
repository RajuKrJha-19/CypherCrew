"""The Analytics screen, and the Engage sync that sits beside it.

Both used to be incapable of telling "there is nothing" apart from "we
were not allowed to look" - Analytics because the page was a hard-coded
mockup that never read a row, Engage because two nested bare excepts threw
every error away. These tests pin the arithmetic and, just as importantly,
the honesty of the empty states.
"""

from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import (
    SocialAccount, SocialAnalyticsSnapshot, SocialComment, SocialPost,
    SocialPostTarget,
)
from app.social.services import analytics_report, engage as engage_svc
from app.social.tokens.vault import get_vault
from app.utils import periods
from tests.conftest import FakeProvider

# The `session` fixture already runs inside an app context. Pushing
# another one here would hand these tests a DIFFERENT session, and the
# rows flushed below would be invisible to the code under test.

_counter = {"n": 0}


def _period(key="all"):
    return periods.resolve_period({"period": key}, allow_all=True,
                                  default="all")


def _publish(session, metrics=None, when=None):
    """A published target on its own channel, optionally with a snapshot.

    Built by hand rather than through make_target because that fixture
    reuses one external_id, and several of these tests need two channels.
    """
    _counter["n"] += 1
    n = _counter["n"]

    account = SocialAccount(
        platform="fake", external_id=f"ACC-{n}", display_name=f"Channel {n}",
        account_type="page", status="active",
        token_ciphertext=get_vault().encrypt("AT"), token_key_version=1,
    )
    session.add(account)
    session.flush()

    post = SocialPost(title=f"post {n}", status="published")
    session.add(post)
    session.flush()

    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=account.id,
        platform="fake", post_type="image", caption="hi",
        status="published", external_post_id=f"EXT-{n}",
    )
    session.add(target)
    session.flush()

    if when:
        target.updated_at = when
        session.flush()

    if metrics is not None:
        session.add(SocialAnalyticsSnapshot(
            target_id=target.id, external_post_id=target.external_post_id,
            metrics=metrics))
        session.flush()

    return target


# --------------------------------------------------------------------------
# The append-only trap
# --------------------------------------------------------------------------

def test_only_the_latest_snapshot_counts(session):
    """Snapshots are append-only, so a post synced three times has three
    rows with growing numbers. Summing them all would report reach three
    times over - the single most likely way to get this wrong."""
    target = _publish(session, metrics={"reach": 100})
    for reach in (250, 400):
        session.add(SocialAnalyticsSnapshot(
            target_id=target.id, external_post_id="EXT-1",
            metrics={"reach": reach}))
    session.flush()

    report = analytics_report.build_report(_period())

    assert report["totals"]["reach"] == 400
    assert report["measured_count"] == 1


def test_totals_add_up_across_posts(session):
    _publish(session, metrics={"reach": 100, "likes": 5})
    _publish(session, metrics={"reach": 40, "likes": 2})

    report = analytics_report.build_report(_period())

    assert report["totals"]["reach"] == 140
    assert report["totals"]["likes"] == 7
    assert report["measured_count"] == 2


# --------------------------------------------------------------------------
# Only claim what the platform reported
# --------------------------------------------------------------------------

def test_a_metric_nobody_reported_is_not_tiled(session):
    """A zero for an unreported metric claims "you got zero reach", which
    is a different and much worse statement than "not reported"."""
    _publish(session, metrics={"likes": 5})

    report = analytics_report.build_report(_period())

    assert "likes" in report["present"]
    assert "reach" not in report["present"]


def test_a_genuine_zero_is_still_reported(session):
    """Nuance: the platform DID answer, and the answer was zero."""
    _publish(session, metrics={"reach": 0})

    report = analytics_report.build_report(_period())

    assert "reach" in report["present"]
    assert report["totals"]["reach"] == 0


def test_junk_metric_values_are_ignored(session):
    _publish(session, metrics={"reach": "n/a", "likes": 3})

    report = analytics_report.build_report(_period())

    assert "reach" not in report["present"]
    assert report["totals"]["likes"] == 3


# --------------------------------------------------------------------------
# The two different kinds of nothing
# --------------------------------------------------------------------------

def test_no_published_posts_is_its_own_empty_state(session):
    report = analytics_report.build_report(_period())

    assert report["post_count"] == 0
    assert report["measured_count"] == 0
    assert report["totals"]["reach"] == 0


def test_published_but_unmeasured_is_a_different_empty_state(session):
    """The posts exist; the platform simply hasn't reported figures yet.
    The screen has to distinguish this from "nothing published"."""
    _publish(session, metrics=None)

    report = analytics_report.build_report(_period())

    assert report["post_count"] == 1
    assert report["measured_count"] == 0


# --------------------------------------------------------------------------
# The period actually filters
# --------------------------------------------------------------------------

def test_an_old_post_is_outside_a_short_window(session):
    _publish(session, metrics={"reach": 100},
             when=datetime.utcnow() - timedelta(days=90))

    recent = analytics_report.build_report(_period("7d"))
    everything = analytics_report.build_report(_period("all"))

    assert recent["post_count"] == 0
    assert everything["post_count"] == 1


def test_channels_are_broken_out(session):
    _publish(session, metrics={"reach": 10})
    _publish(session, metrics={"reach": 20})

    report = analytics_report.build_report(_period())

    assert report["channels"]
    total = sum(row["metrics"]["reach"] for _, row in report["channels"])
    assert total == 30


# --------------------------------------------------------------------------
# Engage: "all caught up" must mean we actually looked
# --------------------------------------------------------------------------

def test_a_refused_fetch_is_reported_not_swallowed(session, monkeypatch):
    """The bug behind "there is a comment on Facebook but Engage is
    empty": both the provider and the sync threw the error away, so a
    lockout was indistinguishable from an empty inbox."""
    _publish(session)

    def boom(self, external_post_id, token, limit=50):
        raise RuntimeError("(#10) Requires pages_read_engagement")

    monkeypatch.setattr(FakeProvider, "list_comments", boom, raising=False)
    monkeypatch.setattr(FakeProvider.capabilities, "supports_comments", True,
                        raising=False)

    report = engage_svc.sync_comments()

    assert report["failed"] == 1
    assert report["errors"], "a failure must carry a reason"
    assert report["new"] == 0


def test_a_clean_run_reports_what_it_checked(session, monkeypatch):
    _publish(session)
    monkeypatch.setattr(FakeProvider, "list_comments",
                        lambda self, *a, **k: [], raising=False)
    monkeypatch.setattr(FakeProvider.capabilities, "supports_comments", True,
                        raising=False)

    report = engage_svc.sync_comments()

    assert report["failed"] == 0
    assert report["checked"] == 1
    assert report["new"] == 0


def test_new_comments_are_stored_once(session, monkeypatch):
    _publish(session)
    payload = [{"external_id": "C1", "message": "Nice!",
                "author_name": "A", "created_time": None}]
    monkeypatch.setattr(FakeProvider, "list_comments",
                        lambda self, *a, **k: payload, raising=False)
    monkeypatch.setattr(FakeProvider.capabilities, "supports_comments", True,
                        raising=False)

    first = engage_svc.sync_comments()
    second = engage_svc.sync_comments()

    assert first["new"] == 1
    # Re-syncing must not duplicate - it only refreshes fetched_at.
    assert second["new"] == 0
    assert SocialComment.query.filter_by(external_id="C1").count() == 1
