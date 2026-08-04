"""In-process background worker.

Makes scheduled posts publish AUTOMATICALLY without any external cron: a
single daemon thread wakes every SOCIAL_WORKER_INTERVAL seconds and, inside an
app context, (1) enqueues targets whose scheduled time has arrived and (2)
drains the publish queue. Because the queue is claim-based (FOR UPDATE SKIP
LOCKED) and enqueue is idempotent, this is safe to run in every gunicorn
worker and safe alongside the external /internal/social/* cron endpoints.

Started once from create_app when SOCIAL_ENGINE_ENABLED and
SOCIAL_INPROCESS_WORKER are on. The manual "Process queue" button and the cron
endpoints keep working; this just means nobody has to press anything.
"""

import threading
import time

_started = False
_lock = threading.Lock()
#: Wall-clock of the last analytics refresh this process attempted (throttle).
_last_analytics = 0.0


def start_background_worker(app):
    """Start the daemon worker thread exactly once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    interval = max(5, int(app.config.get("SOCIAL_WORKER_INTERVAL", 20)))
    # Insights refresh on a much slower cadence than publishing (default 30 min)
    # so the Analytics tab stays current WITHOUT an external cron. Best-effort +
    # cross-process de-duped in analytics.auto_sync_recent, so all gunicorn
    # workers running this is safe.
    analytics_interval = max(300, int(
        app.config.get("SOCIAL_ANALYTICS_INTERVAL", 1800)))

    def _loop():
        global _last_analytics
        # Let boot + auto-migrations settle before the first tick.
        time.sleep(8)
        while True:
            try:
                with app.app_context():
                    from app.social.services import scheduling
                    from app.social.queue import worker
                    scheduling.enqueue_due()
                    worker.drain()
                    now = time.time()
                    if now - _last_analytics >= analytics_interval:
                        _last_analytics = now
                        from app.social.services import analytics
                        analytics.auto_sync_recent(analytics_interval)
            except Exception:  # noqa: BLE001 - a bad tick must never kill the loop
                try:
                    with app.app_context():
                        app.logger.exception("[social-autoworker] tick failed")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(interval)

    thread = threading.Thread(
        target=_loop, name="social-autoworker", daemon=True)
    thread.start()
    app.logger.info(
        "[social-autoworker] started - auto-publishing every %ss", interval)
