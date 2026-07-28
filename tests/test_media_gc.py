"""Media garbage collector: delete only orphaned AND aged social_uploads."""
from datetime import datetime, timedelta, timezone

from app.models import SocialMediaAsset
from app.social.media import gc


class _FakeStorage:
    """Stand-in for StorageService: serves a fixed listing, records deletes."""
    def __init__(self, objects):
        self.objects = objects
        self.deleted = []

    def list_files(self, *, prefix):
        return [o for o in self.objects
                if o["object_key"].startswith(prefix)]

    def delete(self, *, object_key):
        self.deleted.append(object_key)


def test_gc_deletes_only_aged_unreferenced(session, make_target, monkeypatch,
                                           app):
    _acct, _post, target = make_target()
    # A referenced upload - an asset row points at it, so it must survive
    # even though it is old.
    session.add(SocialMediaAsset(
        target_id=target.id, source="upload",
        object_key="social_uploads/keepme.jpg", role="main"))
    session.commit()

    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    objects = [
        {"object_key": "social_uploads/keepme.jpg",
         "last_modified": now - timedelta(days=30)},      # referenced -> keep
        {"object_key": "social_uploads/old_orphan.jpg",
         "last_modified": now - timedelta(days=2)},        # orphan+old -> delete
        {"object_key": "social_uploads/fresh_orphan.jpg",
         "last_modified": now - timedelta(minutes=5)},     # orphan+recent -> skip
    ]
    fake = _FakeStorage(objects)
    monkeypatch.setattr(gc, "StorageService", lambda: fake)

    with app.app_context():
        summary = gc.sweep(now=now)

    # Only the aged, unreferenced object is deleted.
    assert fake.deleted == ["social_uploads/old_orphan.jpg"]
    assert summary["deleted"] == 1
    assert summary["orphaned"] == 2        # both orphans counted
    assert summary["skipped_recent"] == 1  # the fresh one held back
    assert summary["failed"] == 0


def test_gc_dry_run_deletes_nothing(session, make_target, monkeypatch, app):
    make_target()
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    fake = _FakeStorage([
        {"object_key": "social_uploads/old_orphan.jpg",
         "last_modified": now - timedelta(days=2)},
    ])
    monkeypatch.setattr(gc, "StorageService", lambda: fake)

    with app.app_context():
        summary = gc.sweep(now=now, dry_run=True)

    assert fake.deleted == []
    assert summary["deleted"] == 0
    assert summary["orphaned"] == 1


def test_gc_survives_listing_failure(monkeypatch, app):
    class _Boom:
        def list_files(self, *, prefix):
            raise RuntimeError("R2 down")
    monkeypatch.setattr(gc, "StorageService", lambda: _Boom())

    with app.app_context():
        summary = gc.sweep()

    # A listing failure returns a zeroed summary rather than crashing cron.
    assert summary["deleted"] == 0
    assert summary["listed"] == 0
