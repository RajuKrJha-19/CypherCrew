"""Meta analytics normalisation + the cross-worker auto-refresh throttle.

The Facebook adapter must map its post_* metric names onto the canonical keys
the report reads (the bug that left FB always blank), Instagram must map
straight through, and neither may swallow errors (they must surface so
sync_recent can report them). auto_sync_recent must skip when snapshots are
fresh.
"""
from types import SimpleNamespace

import pytest

from app.social.providers.meta_facebook import MetaFacebookProvider
from app.social.providers.meta_instagram import MetaInstagramProvider


class _Graph:
    def __init__(self, data=None, boom=False):
        self._data = data or []
        self._boom = boom

    def get(self, path, token=None, params=None):
        if self._boom:
            raise RuntimeError("Graph error (missing read_insights)")
        return {"data": self._data}


def _rows(pairs):
    return [{"name": n, "values": [{"value": v}]} for n, v in pairs]


def _provider(cls, monkeypatch, graph):
    p = cls()
    monkeypatch.setattr(p, "_page_token", lambda target: "tok")
    monkeypatch.setattr(p, "graph", lambda: graph)
    return p


_TARGET = SimpleNamespace(external_post_id="PID", account=object())


# -- Facebook: normalise post_* -> canonical keys ---------------------------

def test_facebook_maps_post_metrics_to_canonical_keys(monkeypatch):
    graph = _Graph(_rows([
        ("post_impressions", 1000),
        ("post_impressions_unique", 700),
        ("post_engaged_users", 120),
        ("post_reactions_by_type_total", {"like": 30, "love": 5}),
    ]))
    p = _provider(MetaFacebookProvider, monkeypatch, graph)
    out = p.fetch_analytics(_TARGET, "tok")
    assert out == {"impressions": 1000, "reach": 700,
                   "engagement": 120, "likes": 35}   # reactions summed


def test_facebook_reactions_as_scalar_still_work(monkeypatch):
    # The emulator returns a number (not a dict) for the reactions metric.
    graph = _Graph(_rows([("post_impressions", 100),
                          ("post_reactions_by_type_total", 12)]))
    p = _provider(MetaFacebookProvider, monkeypatch, graph)
    out = p.fetch_analytics(_TARGET, "tok")
    assert out["impressions"] == 100 and out["likes"] == 12


def test_facebook_error_is_not_swallowed(monkeypatch):
    p = _provider(MetaFacebookProvider, monkeypatch, _Graph(boom=True))
    with pytest.raises(RuntimeError):
        p.fetch_analytics(_TARGET, "tok")


# -- Instagram: canonical keys pass straight through ------------------------

def test_instagram_maps_canonical_keys(monkeypatch):
    graph = _Graph(_rows([("reach", 500), ("likes", 60),
                          ("comments", 8), ("saved", 12)]))
    p = _provider(MetaInstagramProvider, monkeypatch, graph)
    out = p.fetch_analytics(_TARGET, "tok")
    assert out == {"reach": 500, "likes": 60, "comments": 8, "saved": 12}


def test_instagram_error_is_not_swallowed(monkeypatch):
    p = _provider(MetaInstagramProvider, monkeypatch, _Graph(boom=True))
    with pytest.raises(RuntimeError):
        p.fetch_analytics(_TARGET, "tok")


def test_fetch_analytics_empty_without_external_id(monkeypatch):
    p = _provider(MetaFacebookProvider, monkeypatch, _Graph(boom=True))
    assert p.fetch_analytics(SimpleNamespace(external_post_id=None), "tok") == {}


# -- auto_sync_recent throttle (cross-worker de-dup) ------------------------

def test_auto_sync_skips_when_snapshots_are_fresh(app, session, make_target):
    from app.models import SocialAnalyticsSnapshot
    from app.social.services import analytics

    _, _, target = make_target(platform="instagram")
    session.add(SocialAnalyticsSnapshot(
        target_id=target.id, external_post_id="PID", metrics={"reach": 5}))
    session.commit()

    with app.app_context():
        # A snapshot was just written, so a 30-min-throttled auto refresh skips.
        assert analytics.auto_sync_recent(1800) == {"skipped": "recent"}
