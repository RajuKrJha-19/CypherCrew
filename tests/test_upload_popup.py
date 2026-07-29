"""The upload popup's server half.

The popup uploads one file at a time and only tells anyone about the batch
when Done is pressed, so the interesting behaviour is all in what does and
does not happen per file:

  * a finished upload is silent - no activity, no notification;
  * Done writes exactly one of each, whatever the batch size;
  * x deletes, and only your own just-uploaded file on this task;
  * a reference file staged before the task exists is attached on create,
    and swept if the form is abandoned.
"""

from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from app.extensions import db
from app.models import Notification, TaskActivity, TaskFile
from app.routes.tasks import REFERENCE_STAGING_PREFIX
from app.social.media import gc


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def _submission_file(task, uploader, name="clip.mp4"):
    """A TaskFile as complete_submission_multipart_upload leaves one."""
    row = TaskFile(
        task_id=task.id,
        bucket_name="test-bucket",
        storage_provider="r2",
        object_key=f"tasks/{task.id}/submission/{name}",
        original_filename=name,
        stored_filename=name,
        mime_type="video/mp4",
        file_size=1234,
        folder_type="submission",
        version=1,
        is_final=False,
        uploaded_by_id=uploader.id,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _activities(task_id, action="submission_uploaded"):
    return TaskActivity.query.filter_by(
        task_id=task_id, action=action).all()


def _notifications(task_id):
    return Notification.query.filter_by(task_id=task_id).all()


class _FakeStorage:
    """Records deletes instead of talking to R2."""

    def __init__(self, objects=()):
        self.objects = list(objects)
        self.deleted = []
        self.uploaded = []

    def list_files(self, *, prefix):
        return [o for o in self.objects
                if o["object_key"].startswith(prefix)]

    def delete(self, *, object_key):
        self.deleted.append(object_key)

    def upload(self, *, file_obj, object_key, content_type=None):
        self.uploaded.append(object_key)
        return {"bucket_name": "test-bucket", "object_key": object_key,
                "content_type": content_type, "content_length": 11}


@pytest.fixture()
def assignee_task(make_user, make_task):
    """A task whose assignee is someone OTHER than its creator, so the
    notification to the creator is actually reachable."""
    from app.models import Task

    creator = make_user("Manager")
    worker = make_user("employee")
    task = make_task(worker)
    Task.query.filter_by(id=task.id).update({"created_by_id": creator.id})
    db.session.commit()
    return task, worker, creator


# ---------------------------------------------------------------------
# One upload is silent
# ---------------------------------------------------------------------

def test_a_finished_upload_announces_nothing(assignee_task):
    """Five files must not fire five notifications at the reviewer - which
    is why the per-file route stopped writing them."""
    task, worker, _creator = assignee_task
    _submission_file(task, worker)

    assert _activities(task.id) == []
    assert _notifications(task.id) == []


def test_done_announces_the_batch_once(assignee_task, login, client):
    task, worker, creator = assignee_task
    rows = [_submission_file(task, worker, f"clip{i}.mp4") for i in range(5)]

    login(worker)
    response = client.post(
        f"/tasks/{task.id}/submission/commit",
        json={"file_ids": [r.id for r in rows]})

    assert response.status_code == 200
    assert response.get_json()["success"] is True

    activities = _activities(task.id)
    assert len(activities) == 1
    assert "5 submission file(s)" in activities[0].message

    notifications = _notifications(task.id)
    assert len(notifications) == 1
    assert notifications[0].user_id == creator.id


def test_done_with_nothing_selected_is_refused(assignee_task, login, client):
    """An empty batch must not write an activity entry saying 0 files."""
    task, worker, _creator = assignee_task
    login(worker)

    response = client.post(f"/tasks/{task.id}/submission/commit",
                           json={"file_ids": []})

    assert response.status_code == 400
    assert _activities(task.id) == []


def test_done_ignores_a_file_from_another_task(assignee_task, make_task,
                                               login, client):
    """The id list comes from the browser, so it is filtered server-side -
    otherwise a stray id would inflate the count in the activity entry."""
    task, worker, _creator = assignee_task
    mine = _submission_file(task, worker)
    other_task = make_task(worker)
    theirs = _submission_file(other_task, worker, "elsewhere.mp4")

    login(worker)
    client.post(f"/tasks/{task.id}/submission/commit",
                json={"file_ids": [mine.id, theirs.id]})

    activities = _activities(task.id)
    assert len(activities) == 1
    assert "1 submission file(s)" in activities[0].message


def test_only_the_assignee_can_commit(assignee_task, login, client):
    task, worker, creator = assignee_task
    row = _submission_file(task, worker)

    login(creator)          # the person who SET the task, not the assignee
    response = client.post(f"/tasks/{task.id}/submission/commit",
                           json={"file_ids": [row.id]})

    assert response.status_code == 403
    assert _activities(task.id) == []


# ---------------------------------------------------------------------
# x, and closing the popup
# ---------------------------------------------------------------------

def test_discard_removes_the_row_and_the_object(assignee_task, login, client,
                                                monkeypatch):
    """Cancel has to mean cancel: an aborted transfer leaves nothing
    behind, so a finished-then-cancelled one must not either."""
    task, worker, _creator = assignee_task
    row = _submission_file(task, worker)
    key = row.object_key

    fake = _FakeStorage()
    monkeypatch.setattr("app.routes.tasks.StorageService", lambda: fake)

    login(worker)
    response = client.post(
        f"/tasks/{task.id}/submission/discard/{row.id}")

    assert response.status_code == 200
    assert TaskFile.query.get(row.id) is None
    assert key in fake.deleted


def test_discard_refuses_someone_elses_file(assignee_task, make_user,
                                            login, client):
    task, worker, _creator = assignee_task
    stranger = make_user("employee")
    row = _submission_file(task, stranger, "not-mine.mp4")

    login(worker)
    response = client.post(f"/tasks/{task.id}/submission/discard/{row.id}")

    assert response.status_code == 404
    assert TaskFile.query.get(row.id) is not None


def test_discard_refuses_a_file_on_another_task(assignee_task, make_task,
                                                login, client):
    """The task id in the URL is enforced, not decorative - otherwise the
    assignee of one task could delete files from any other."""
    task, worker, _creator = assignee_task
    other_task = make_task(worker)
    row = _submission_file(other_task, worker, "elsewhere.mp4")

    login(worker)
    response = client.post(f"/tasks/{task.id}/submission/discard/{row.id}")

    assert response.status_code == 404
    assert TaskFile.query.get(row.id) is not None


# ---------------------------------------------------------------------
# Reference files: staged before the task exists
# ---------------------------------------------------------------------

def test_staging_stores_under_the_staging_prefix(make_user, login, client,
                                                 monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr("app.routes.tasks.StorageService", lambda: fake)

    login(make_user("Manager"))
    response = client.post(
        "/tasks/reference/stage",
        data={"file": (BytesIO(b"hello world"), "brief brief.pdf")},
        content_type="multipart/form-data")

    assert response.status_code == 200
    body = response.get_json()
    assert body["object_key"].startswith(REFERENCE_STAGING_PREFIX)
    # The name is sanitised into the key, but the real one comes back for
    # display and for the TaskFile row.
    assert " " not in body["object_key"]
    assert body["original_filename"] == "brief brief.pdf"
    assert fake.uploaded == [body["object_key"]]


def test_staging_needs_a_file(make_user, login, client):
    login(make_user("Manager"))
    response = client.post("/tasks/reference/stage",
                           data={}, content_type="multipart/form-data")
    assert response.status_code == 400


def test_discarding_refuses_a_key_outside_staging(make_user, login, client,
                                                  monkeypatch):
    """The prefix check is the whole guard on this route: without it a
    tampered key would point the delete at a real task's files."""
    fake = _FakeStorage()
    monkeypatch.setattr("app.routes.tasks.StorageService", lambda: fake)

    login(make_user("Manager"))
    response = client.post("/tasks/reference/discard",
                           json={"object_key": "tasks/1/reference/real.pdf"})

    assert response.status_code == 400
    assert fake.deleted == []


def test_discarding_a_staged_key_deletes_it(make_user, login, client,
                                            monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr("app.routes.tasks.StorageService", lambda: fake)
    key = REFERENCE_STAGING_PREFIX + "abc_brief.pdf"

    login(make_user("Manager"))
    response = client.post("/tasks/reference/discard",
                           json={"object_key": key})

    assert response.status_code == 200
    assert fake.deleted == [key]


def test_creating_a_task_attaches_staged_references(app, make_user, make_task,
                                                    login, client):
    """The staged object is not copied - the TaskFile row points straight
    at the staging key, which is also what keeps the GC from sweeping it."""
    from app.routes.tasks import _attach_staged_reference_files

    manager = make_user("Manager")
    task = make_task(manager)
    key = REFERENCE_STAGING_PREFIX + "abc_brief.pdf"

    with app.test_request_context(
            "/tasks/add", method="POST",
            data={"staged_reference_files":
                  f'[{{"object_key": "{key}",'
                  f' "original_filename": "brief.pdf",'
                  f' "mime_type": "application/pdf", "file_size": 11}}]'}):
        from flask_login import login_user
        login_user(manager)
        attached = _attach_staged_reference_files(task)
        db.session.commit()

    assert attached == 1
    row = TaskFile.query.filter_by(task_id=task.id,
                                   folder_type="reference").one()
    assert row.object_key == key
    assert row.original_filename == "brief.pdf"


def test_a_forged_staged_key_is_not_attached(app, make_user, make_task):
    """The list is browser-supplied, so a key pointing anywhere but the
    staging area would let the create form adopt an arbitrary object."""
    from app.routes.tasks import _attach_staged_reference_files

    manager = make_user("Manager")
    task = make_task(manager)

    with app.test_request_context(
            "/tasks/add", method="POST",
            data={"staged_reference_files":
                  '[{"object_key": "tasks/9/submission/secret.mp4",'
                  ' "original_filename": "secret.mp4"}]'}):
        from flask_login import login_user
        login_user(manager)
        attached = _attach_staged_reference_files(task)

    assert attached == 0
    assert TaskFile.query.filter_by(task_id=task.id).count() == 0


def test_unreadable_staged_json_is_ignored(app, make_user, make_task):
    from app.routes.tasks import _attach_staged_reference_files

    manager = make_user("Manager")
    task = make_task(manager)

    with app.test_request_context(
            "/tasks/add", method="POST",
            data={"staged_reference_files": "not json at all"}):
        from flask_login import login_user
        login_user(manager)
        assert _attach_staged_reference_files(task) == 0


# ---------------------------------------------------------------------
# The GC sweeps abandoned staging
# ---------------------------------------------------------------------

def test_gc_sweeps_an_abandoned_staged_file(app, make_user, make_task,
                                            monkeypatch):
    """Abandon the create form and the object is orphaned exactly like an
    abandoned composer upload, so it is swept by the same rule."""
    manager = make_user("Manager")
    task = make_task(manager)

    kept_key = REFERENCE_STAGING_PREFIX + "kept_brief.dat"
    db.session.add(TaskFile(
        task_id=task.id, object_key=kept_key, storage_provider="r2",
        bucket_name="test-bucket",
        original_filename="brief.pdf", stored_filename="kept_brief.dat",
        folder_type="reference", version=1, is_final=False,
        uploaded_by_id=manager.id, file_size=1))
    db.session.commit()

    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    fake = _FakeStorage([
        {"object_key": kept_key,
         "last_modified": now - timedelta(days=30)},        # attached
        {"object_key": REFERENCE_STAGING_PREFIX + "old_orphan.pdf",
         "last_modified": now - timedelta(days=2)},         # abandoned
        {"object_key": REFERENCE_STAGING_PREFIX + "fresh_orphan.pdf",
         "last_modified": now - timedelta(minutes=5)},      # still being filled in
    ])
    monkeypatch.setattr(gc, "StorageService", lambda: fake)

    with app.app_context():
        summary = gc.sweep(now=now)

    assert fake.deleted == [REFERENCE_STAGING_PREFIX + "old_orphan.pdf"]
    assert summary["by_prefix"][REFERENCE_STAGING_PREFIX]["deleted"] == 1
    assert summary["by_prefix"][REFERENCE_STAGING_PREFIX]["skipped_recent"] == 1


# ---------------------------------------------------------------------
# Client brand assets: one file per request
# ---------------------------------------------------------------------

@pytest.fixture()
def asset_client(app, make_user):
    """A client plus someone allowed to curate it."""
    from app.models import Client

    manager = make_user("super_admin")
    row = Client(client_name="pytest-role-assetclient", status="active")
    db.session.add(row)
    db.session.commit()
    # No teardown here: the name carries the pytest prefix, so the shared
    # purge in conftest removes the client AND its assets. Doing it twice
    # from two fixtures is where the ordering goes wrong.
    return row, manager


def _asset(client_row, uploader, name="logo.png"):
    from app.models import ClientAsset
    row = ClientAsset(
        client_id=client_row.id, bucket_name="test-bucket",
        storage_provider="r2",
        object_key=f"clients/{client_row.id}/logo/{name}",
        original_filename=name, stored_filename=name,
        mime_type="image/png", file_size=10, category="logo",
        uploaded_by_id=uploader.id)
    db.session.add(row)
    db.session.commit()
    return row


def test_one_asset_per_request(asset_client, login, client, monkeypatch):
    """The whole point: thirty or forty assets used to travel in a single
    request that took about a second per file and was killed by the proxy
    before it finished."""
    client_row, manager = asset_client
    seen = []

    def fake_upload(self, *, client, file_storage, uploaded_by_id, category):
        from app.models import ClientAsset
        seen.append(file_storage.filename)
        asset = ClientAsset(
            client_id=client.id, bucket_name="b", storage_provider="r2",
            object_key="clients/k_" + file_storage.filename,
            original_filename=file_storage.filename,
            stored_filename=file_storage.filename, mime_type="image/png",
            file_size=3, category=category, uploaded_by_id=uploaded_by_id)
        db.session.add(asset)
        return {"asset": asset, "provider_metadata": {}}

    monkeypatch.setattr(
        "app.storage.storage_service.StorageService.upload_client_asset",
        fake_upload)

    login(manager)
    response = client.post(
        f"/clients/{client_row.id}/assets/upload-one",
        data={"category": "logo", "file": (BytesIO(b"png"), "one.png")},
        content_type="multipart/form-data")

    assert response.status_code == 200
    assert response.get_json()["file_id"] is not None
    assert seen == ["one.png"]


def test_an_invalid_category_says_so(asset_client, login, client):
    """The batched route flashed 'please try again' for this; a person can
    only act on it if it names the actual problem."""
    client_row, manager = asset_client
    login(manager)

    response = client.post(
        f"/clients/{client_row.id}/assets/upload-one",
        data={"category": "not-a-category",
              "file": (BytesIO(b"png"), "one.png")},
        content_type="multipart/form-data")

    assert response.status_code == 400
    assert "asset type" in response.get_json()["message"].lower()


def test_uploading_an_asset_needs_the_manage_permission(asset_client,
                                                        make_user, login,
                                                        client):
    client_row, _manager = asset_client
    login(make_user("employee"))

    response = client.post(
        f"/clients/{client_row.id}/assets/upload-one",
        data={"category": "logo", "file": (BytesIO(b"png"), "one.png")},
        content_type="multipart/form-data")

    assert response.status_code == 403


def test_done_counts_from_the_database(asset_client, login, client):
    """The id list is browser-supplied, so the message is counted from what
    actually exists rather than from how many ids were sent."""
    client_row, manager = asset_client
    rows = [_asset(client_row, manager, f"a{i}.png") for i in range(3)]

    login(manager)
    response = client.post(
        f"/clients/{client_row.id}/assets/commit",
        json={"file_ids": [r.id for r in rows] + [999999, 1000000]})

    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_done_refuses_an_empty_batch(asset_client, login, client):
    client_row, manager = asset_client
    login(manager)
    response = client.post(f"/clients/{client_row.id}/assets/commit",
                           json={"file_ids": []})
    assert response.status_code == 400


def test_discarding_an_asset_removes_row_and_object(asset_client, login,
                                                    client, monkeypatch):
    from app.models import ClientAsset

    client_row, manager = asset_client
    row = _asset(client_row, manager)
    key = row.object_key

    fake = _FakeStorage()
    monkeypatch.setattr("app.routes.clients.StorageService", lambda: fake)

    login(manager)
    response = client.post(
        f"/clients/{client_row.id}/assets/discard/{row.id}")

    assert response.status_code == 200
    assert ClientAsset.query.get(row.id) is None
    assert fake.deleted == [key]


def test_discarding_refuses_an_asset_from_another_client(asset_client,
                                                         make_user, login,
                                                         client):
    """The client id in the URL is enforced, not decorative."""
    from app.models import Client, ClientAsset

    client_row, manager = asset_client
    other = Client(client_name="pytest-role-other", status="active")
    db.session.add(other)
    db.session.commit()
    row = _asset(other, manager)

    login(manager)
    response = client.post(
        f"/clients/{client_row.id}/assets/discard/{row.id}")

    assert response.status_code == 404
    assert ClientAsset.query.get(row.id) is not None
