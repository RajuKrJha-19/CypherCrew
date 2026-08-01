"""Instagram poll_publish idempotency on a mid-publish worker death (M-7).

media_publish is the one poll-path call that both goes live and mints a new
id. If the worker dies after it succeeds but before the id is recorded, the
container's status_code is now PUBLISHED. A resume must recover the live
media's id - never re-issue media_publish (which would duplicate the post).
"""
from types import SimpleNamespace

from app.social.dto import StepStatus
from app.social.providers.meta_instagram import MetaInstagramProvider


class _Graph:
    """Records POSTs so the test can assert media_publish is NOT re-issued."""

    def __init__(self, status_code, recent_media=None):
        self._status_code = status_code
        self._recent_media = recent_media if recent_media is not None else []
        self.posts = []

    def get(self, path, token=None, params=None):
        params = params or {}
        if str(path).endswith("/media"):
            return {"data": self._recent_media}
        if "status_code" in (params.get("fields") or ""):
            return {"id": path, "status_code": self._status_code}
        if (params.get("fields")) == "permalink":
            return {"permalink": f"https://instagram.com/p/{path}"}
        return {"id": path}

    def post(self, path, token=None, data=None):
        self.posts.append((path, dict(data or {})))
        return {"id": "SHOULD_NOT_HAPPEN"}


def _ig(monkeypatch, graph):
    ig = MetaInstagramProvider()
    monkeypatch.setattr(ig, "graph", lambda: graph)
    return ig


def _state():
    return {"container_id": "CONT_1", "ig_id": "IG_1"}


def test_published_container_recovers_media_id_without_republishing(monkeypatch):
    # The prior (interrupted) attempt already published; container == PUBLISHED
    # and the live media is the account's most recent item.
    g = _Graph("PUBLISHED", recent_media=[{"id": "MEDIA_9"}])
    ig = _ig(monkeypatch, g)

    step = ig.poll_publish(SimpleNamespace(), _state(), "tok")

    assert step.status == StepStatus.DONE.value
    assert step.external_post_id == "MEDIA_9"
    # The crux: media_publish was NOT called a second time.
    assert not any("media_publish" in p for p, _ in g.posts)


def test_published_but_unqueryable_stays_pending_never_republishes(monkeypatch):
    # Live, but the media isn't listable yet - keep polling, don't re-publish.
    g = _Graph("PUBLISHED", recent_media=[])
    ig = _ig(monkeypatch, g)

    step = ig.poll_publish(SimpleNamespace(), _state(), "tok")

    assert step.status == StepStatus.PENDING.value
    assert not any("media_publish" in p for p, _ in g.posts)


def test_finished_container_publishes_normally(monkeypatch):
    # Happy path is unchanged: FINISHED -> media_publish -> DONE in one poll.
    g = _Graph("FINISHED")

    def post(path, token=None, data=None):
        g.posts.append((path, dict(data or {})))
        return {"id": "NEW_MEDIA"}

    monkeypatch.setattr(g, "post", post)
    ig = _ig(monkeypatch, g)

    step = ig.poll_publish(SimpleNamespace(), _state(), "tok")

    assert step.status == StepStatus.DONE.value
    assert step.external_post_id == "NEW_MEDIA"
    assert any("media_publish" in p for p, _ in g.posts)


def test_in_progress_stays_pending(monkeypatch):
    g = _Graph("IN_PROGRESS")
    ig = _ig(monkeypatch, g)

    step = ig.poll_publish(SimpleNamespace(), _state(), "tok")

    assert step.status == StepStatus.PENDING.value
    assert g.posts == []
