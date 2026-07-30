"""In-process attendance worker.

A single daemon thread that periodically (1) pulls attendance from Zoho and
(2) runs the idle-task alert pass, so nothing needs an external cron in dev.
Both operations are idempotent + row-locked, so this is safe in every
gunicorn worker and safe alongside the /internal/attendance/* cron
endpoints. Started once from create_app when ATTENDANCE_ENABLED and
ATTENDANCE_INPROCESS_WORKER are on. Off in tests.
"""

import threading
import time

_started = False
_lock = threading.Lock()


def start_attendance_worker(app):
    """Start the daemon worker thread exactly once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    sync_interval = max(30, int(app.config.get("ATTENDANCE_SYNC_INTERVAL", 120)))
    idle_interval = max(60, int(app.config.get("ATTENDANCE_IDLE_INTERVAL", 600)))

    def _loop():
        # Let boot + auto-migrations settle before the first tick.
        time.sleep(10)
        last_idle = 0.0
        while True:
            try:
                with app.app_context():
                    from app.attendance import service
                    service.sync_attendance()
            except Exception:  # noqa: BLE001 - a bad tick must never kill the loop
                _log_failure(app, "sync")

            now = time.monotonic()
            if now - last_idle >= idle_interval:
                last_idle = now
                try:
                    with app.app_context():
                        from app.services.idle_alerts import run_idle_task_alerts
                        run_idle_task_alerts()
                except Exception:  # noqa: BLE001
                    _log_failure(app, "idle-alerts")

            time.sleep(sync_interval)

    thread = threading.Thread(
        target=_loop, name="attendance-worker", daemon=True)
    thread.start()
    app.logger.info(
        "[attendance-worker] started - Zoho sync every %ss, idle check every %ss",
        sync_interval, idle_interval)


def _log_failure(app, what):
    try:
        with app.app_context():
            app.logger.exception("[attendance-worker] %s tick failed", what)
    except Exception:  # noqa: BLE001
        pass
