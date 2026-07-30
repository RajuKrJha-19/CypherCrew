"""Attendance: Zoho People bridge + idle-task alerts.

Runs in simulation mode (conftest sets ZOHO_SIMULATION_MODE), so the whole
check-in -> sync -> status -> idle-alert loop is exercised with no network.
"""

from datetime import datetime, timedelta

import pytest

from app.attendance import service, sim_store
from app.extensions import db
from app.models import AttendanceSession, Notification
from app.services.idle_alerts import run_idle_task_alerts
from app.utils import roles
from app.utils.timezone import ist_now


@pytest.fixture(autouse=True)
def _reset_attendance_state(app):
    """Isolate the shared singleton settings row and the sim store between
    tests, so a test that disables alerts or checks someone in on Zoho can't
    leak into the next."""
    with app.app_context():
        sim_store.reset()
        s = service.get_settings()
        s.idle_alerts_enabled = True
        s.grace_min = 15
        s.repeat_min = 10
        s.escalate_enabled = True
        s.escalate_after = 3
        s.snooze_min = 15
        s.buzzer_enabled = True
        s.buzzer_volume = 70
        db.session.commit()
    yield


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _open_session(user, minutes_ago=0, source="software"):
    s = AttendanceSession(
        user_id=user.id, source=source,
        check_in_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
    db.session.add(s)
    db.session.commit()
    return s


def _running_task(make_task, user):
    from app.utils import task_status
    task = make_task(user)
    task.status = task_status.IN_PROGRESS
    task.timer_started_at = datetime.utcnow()
    db.session.commit()
    return task


# --------------------------------------------------------------------------
# Source + check-in/out rules
# --------------------------------------------------------------------------

def test_source_defaults_to_zoho(make_user):
    user = make_user("video_editor")
    assert service.source_of(user) == "zoho"


def test_source_software_when_set(make_user):
    user = make_user("video_editor")
    user.checkin_source = "software"
    db.session.commit()
    assert service.source_of(user) == "software"


def test_software_user_can_check_in(make_user):
    user = make_user("video_editor")
    user.checkin_source = "software"
    db.session.commit()
    service.checkin_user(user)
    assert service.current_open_session(user.id) is not None


def test_zoho_user_cannot_check_in(make_user):
    user = make_user("video_editor")  # defaults to zoho
    try:
        service.checkin_user(user)
        assert False, "zoho-source check-in should be refused"
    except service.AttendanceError:
        pass


def test_check_in_is_idempotent(make_user):
    user = make_user("video_editor")
    user.checkin_source = "software"
    db.session.commit()
    a = service.checkin_user(user)
    b = service.checkin_user(user)
    assert a.id == b.id
    assert AttendanceSession.query.filter_by(
        user_id=user.id, check_out_at=None).count() == 1


def test_checkout_closes_session(make_user):
    user = make_user("video_editor")
    user.checkin_source = "software"
    db.session.commit()
    service.checkin_user(user)
    service.checkout_user(user)
    assert service.current_open_session(user.id) is None


def test_zoho_checkout_writes_back_to_sim(make_user):
    sim_store.reset()
    user = make_user("video_editor")  # zoho source
    # Simulate a Zoho check-in, sync it in, then check out from the app.
    sim_store.set_checked_in(user.email, ist_now().strftime("%d/%m/%Y %H:%M:%S"))
    service.sync_attendance()
    assert service.current_open_session(user.id) is not None

    service.checkout_user(user)
    assert service.current_open_session(user.id) is None
    # The write-back closed the simulated Zoho entry too.
    entry = next(e for e in sim_store.entries() if e["email"] == user.email.lower())
    assert entry["check_out"] is not None


# --------------------------------------------------------------------------
# Sync (Zoho -> local sessions)
# --------------------------------------------------------------------------

def test_sync_opens_and_closes_zoho_session(make_user):
    sim_store.reset()
    user = make_user("content_writer")  # zoho source
    sim_store.set_checked_in(user.email, ist_now().strftime("%d/%m/%Y %H:%M:%S"))

    res = service.sync_attendance()
    assert res["opened"] == 1
    assert service.current_open_session(user.id) is not None

    sim_store.set_checked_out(user.email, ist_now().strftime("%d/%m/%Y %H:%M:%S"))
    res = service.sync_attendance()
    assert res["closed"] == 1
    assert service.current_open_session(user.id) is None


def test_sync_is_idempotent(make_user):
    sim_store.reset()
    user = make_user("content_writer")
    sim_store.set_checked_in(user.email, ist_now().strftime("%d/%m/%Y %H:%M:%S"))
    service.sync_attendance()
    service.sync_attendance()  # again
    assert AttendanceSession.query.filter_by(user_id=user.id).count() == 1


def test_sync_skips_software_users(make_user):
    sim_store.reset()
    user = make_user("content_writer")
    user.checkin_source = "software"
    db.session.commit()
    sim_store.set_checked_in(user.email, ist_now().strftime("%d/%m/%Y %H:%M:%S"))
    res = service.sync_attendance()
    # Software users are not Zoho-managed - their Zoho entry is ignored.
    assert service.current_open_session(user.id) is None


# --------------------------------------------------------------------------
# Idle-task alerts
# --------------------------------------------------------------------------

def test_idle_alert_fires_after_grace(make_user):
    user = make_user("video_editor")
    _open_session(user, minutes_ago=20)  # past the 15-min grace
    res = run_idle_task_alerts()
    assert res["alerted"] == 1
    assert Notification.query.filter_by(user_id=user.id).count() == 1


def test_no_alert_within_grace(make_user):
    user = make_user("video_editor")
    _open_session(user, minutes_ago=5)  # inside grace
    res = run_idle_task_alerts()
    assert res["alerted"] == 0


def test_no_alert_when_task_in_progress(make_user, make_task):
    user = make_user("video_editor")
    _open_session(user, minutes_ago=30)
    _running_task(make_task, user)
    res = run_idle_task_alerts()
    assert res["alerted"] == 0
    assert Notification.query.filter_by(user_id=user.id).count() == 0


def test_alert_not_repeated_within_interval(make_user):
    user = make_user("video_editor")
    _open_session(user, minutes_ago=20)
    run_idle_task_alerts()
    res = run_idle_task_alerts()  # immediately again
    assert res["alerted"] == 0
    assert Notification.query.filter_by(user_id=user.id).count() == 1


def test_snooze_suppresses_alert(make_user):
    user = make_user("video_editor")
    session = _open_session(user, minutes_ago=20)
    session.snooze_until = datetime.utcnow() + timedelta(minutes=15)
    db.session.commit()
    res = run_idle_task_alerts()
    assert res["alerted"] == 0


def test_escalation_notifies_manager(app, make_user):
    # A discipline manager exists to receive the escalation.
    manager = make_user("video_editor_manager")
    user = make_user("video_editor")
    session = _open_session(user, minutes_ago=200)

    # Drive enough non-repeating idle rounds to cross the escalation
    # threshold by ageing last_idle_alert_at back each time.
    escalated_total = 0
    for _ in range(4):
        res = run_idle_task_alerts()
        escalated_total += res["escalated"]
        s = AttendanceSession.query.get(session.id)
        if s.last_idle_alert_at:
            s.last_idle_alert_at = s.last_idle_alert_at - timedelta(minutes=30)
        if s.last_escalated_at:
            s.last_escalated_at = s.last_escalated_at - timedelta(hours=1)
        db.session.commit()

    assert escalated_total >= 1
    assert Notification.query.filter_by(
        user_id=manager.id, title="Team member idle").count() >= 1


def test_manager_role_values_maps_discipline():
    values = roles.manager_role_values("video_editor")
    assert "video_editor_manager" in values
    # Admins are NOT unioned in here (that would flood them); they are only
    # the fallback in _managers_for when a person has no discipline lead.
    assert "admin" not in values


def test_escalation_falls_back_to_admin_without_lead(make_user):
    from app.services.idle_alerts import _managers_for
    admin = make_user("admin")
    user = make_user("employee")  # general role, no discipline lead
    recipients = _managers_for(user)
    assert admin.id in [r.id for r in recipients]


# --------------------------------------------------------------------------
# Concurrency + idempotency guards
# --------------------------------------------------------------------------

def test_one_open_session_per_user_enforced(make_user):
    from sqlalchemy.exc import IntegrityError
    user = make_user("video_editor")
    db.session.add(AttendanceSession(user_id=user.id, source="software"))
    db.session.commit()
    db.session.add(AttendanceSession(user_id=user.id, source="software"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_sync_does_not_resurrect_closed_session(make_user):
    user = make_user("content_writer")  # zoho source
    # A closed session from "yesterday" carrying an entry id that the sim
    # store will reuse (the email-fallback bug scenario).
    closed = AttendanceSession(
        user_id=user.id, source="zoho",
        check_in_at=datetime.utcnow() - timedelta(days=1, hours=2),
        check_out_at=datetime.utcnow() - timedelta(days=1),
        zoho_entry_id="SIM-1")
    db.session.add(closed)
    db.session.commit()
    closed_id = closed.id

    sim_store.set_checked_in(user.email, ist_now().strftime("%d/%m/%Y %H:%M:%S"))
    service.sync_attendance()

    open_s = service.current_open_session(user.id)
    assert open_s is not None
    assert open_s.id != closed_id            # a NEW open session, not resurrected
    closed = AttendanceSession.query.get(closed_id)
    assert closed.check_out_at is not None    # the old one stays closed


# --------------------------------------------------------------------------
# Admin-tunable settings
# --------------------------------------------------------------------------

def test_disabling_idle_alerts_stops_them(make_user):
    settings = service.get_settings()
    settings.idle_alerts_enabled = False
    db.session.commit()

    user = make_user("video_editor")
    _open_session(user, minutes_ago=30)
    res = run_idle_task_alerts()
    assert res.get("disabled") is True
    assert Notification.query.filter_by(user_id=user.id).count() == 0


def test_grace_from_settings_is_respected(make_user):
    settings = service.get_settings()
    settings.grace_min = 60          # long grace
    db.session.commit()

    user = make_user("video_editor")
    _open_session(user, minutes_ago=30)  # 30 < 60 -> still in grace
    res = run_idle_task_alerts()
    assert res["alerted"] == 0


def test_working_resets_idle_timestamp(make_user, make_task):
    user = make_user("video_editor")
    session = _open_session(user, minutes_ago=30)
    run_idle_task_alerts()
    session = AttendanceSession.query.get(session.id)
    assert session.last_idle_alert_at is not None

    _running_task(make_task, user)
    run_idle_task_alerts()
    session = AttendanceSession.query.get(session.id)
    assert session.last_idle_alert_at is None
    assert session.idle_alert_count == 0


def test_save_settings_route(make_user, login, client):
    login(make_user("admin", permissions=["manage_attendance"]))
    client.post("/attendance/settings", data={
        "grace_min": "20", "repeat_min": "5", "snooze_min": "30",
        "escalate_after": "4",   # idle_alerts_enabled checkbox omitted -> off
    }, follow_redirects=True)
    settings = service.get_settings()
    assert settings.grace_min == 20
    assert settings.repeat_min == 5
    assert settings.snooze_min == 30
    assert settings.idle_alerts_enabled is False
    assert settings.escalate_enabled is False


def test_save_settings_persists_buzzer(make_user, login, client):
    login(make_user("admin", permissions=["manage_attendance"]))
    client.post("/attendance/settings", data={
        "idle_alerts_enabled": "on", "grace_min": "15", "repeat_min": "10",
        "snooze_min": "15", "escalate_after": "3",
        "buzzer_enabled": "on", "buzzer_volume": "40",
    }, follow_redirects=True)
    settings = service.get_settings()
    assert settings.buzzer_enabled is True
    assert settings.buzzer_volume == 40


def test_idle_notification_flagged_for_buzzer(make_user, login, client):
    user = make_user("video_editor")
    _open_session(user, minutes_ago=30)
    run_idle_task_alerts()

    c = login(user)
    data = c.get("/notifications/api?category=activity&limit=10").get_json()
    # The buzzer config rides the poll, and the idle item is flagged so the
    # browser plays the distinct buzzer for it.
    assert data["attendance_buzzer"] is not None
    assert data["attendance_buzzer"]["enabled"] is True
    assert any(item["is_idle_alert"] for item in data["notifications"])


# --------------------------------------------------------------------------
# Routes + status API
# --------------------------------------------------------------------------

def test_status_endpoint_shape(make_user, login):
    user = make_user("video_editor")
    user.checkin_source = "software"
    db.session.commit()
    c = login(user)
    r = c.get("/attendance/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["checked_in"] is False
    assert data["source"] == "software"
    assert data["can_checkin"] is True


def test_checkin_route_software(make_user, login):
    user = make_user("video_editor")
    user.checkin_source = "software"
    db.session.commit()
    c = login(user)
    r = c.post("/attendance/checkin")
    assert r.status_code == 200
    assert r.get_json()["checked_in"] is True


def test_checkin_route_blocks_zoho_user(make_user, login):
    user = make_user("video_editor")  # zoho source
    c = login(user)
    r = c.post("/attendance/checkin")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
    assert service.current_open_session(user.id) is None


# --------------------------------------------------------------------------
# Internal endpoint auth
# --------------------------------------------------------------------------

def test_internal_sync_requires_token(client):
    assert client.post("/internal/attendance/sync").status_code == 403
    r = client.post("/internal/attendance/sync",
                    headers={"X-Zoho-Token": "test-zoho-token"})
    assert r.status_code == 200
    assert r.get_json()["success"] is True


def test_internal_idle_alerts_requires_token(client):
    assert client.post("/internal/attendance/idle-alerts").status_code == 403
    r = client.post("/internal/attendance/idle-alerts",
                    headers={"X-Zoho-Token": "test-zoho-token"})
    assert r.status_code == 200


def test_webhook_requires_secret(client):
    # No ZOHO_WEBHOOK_SECRET configured in tests -> always 403.
    assert client.post("/internal/attendance/webhook").status_code == 403
