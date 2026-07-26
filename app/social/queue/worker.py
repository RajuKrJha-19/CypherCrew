r"""The publish worker: drains the durable queue and advances each job's
state machine. Safe to call with no providers/jobs (it no-ops).

Flow per job:
  queued --claim--> claimed --start_publish--> DONE      -> succeeded
                                          \--> PENDING   -> queued (poll later)
  on error: RetryEngine decides retry / dead / rate-defer / auth-fail.

Concurrency mirrors the thumbnail worker: a small ThreadPoolExecutor inside
the gunicorn worker, each task in its own app context + DB session. Claiming
uses FOR UPDATE SKIP LOCKED so multiple workers never grab the same job.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import PublishJob, SocialPostTarget, PublishResult
from app.social.registry import get_provider
from app.social.services.accounts import AccountManager
from app.social.services import audit
from app.social.services.publishing import build_content
from app.social.queue import retry as retry_engine
from app.social.queue import ratelimit
from app.social.errors import AuthError, PermanentError, SocialError
from app.social.dto import StepStatus


_STALE_CLAIM_MINUTES = 10


def _reset_stale(worker_id):
    """Return jobs abandoned by a dead worker (stuck 'claimed' past the
    stale window) to the queue."""
    cutoff = datetime.utcnow() - timedelta(minutes=_STALE_CLAIM_MINUTES)
    stale = (
        PublishJob.query
        .filter(PublishJob.state == "claimed", PublishJob.locked_at < cutoff)
        .all()
    )
    for job in stale:
        job.state = "queued"
        job.locked_by = None
    if stale:
        db.session.commit()
    return len(stale)


def _claim(batch, worker_id):
    now = datetime.utcnow()
    jobs = (
        PublishJob.query
        .filter(PublishJob.state == "queued", PublishJob.next_run_at <= now)
        .order_by(PublishJob.priority.asc(), PublishJob.next_run_at.asc())
        .with_for_update(skip_locked=True)
        .limit(batch)
        .all()
    )
    ids = []
    for job in jobs:
        job.state = "claimed"
        job.locked_at = now
        job.locked_by = worker_id
        ids.append(job.id)
    db.session.commit()
    return ids


def drain(batch=None, worker_id=None):
    """Claim up to `batch` due jobs and process them concurrently. Returns a
    summary dict."""
    batch = batch or current_app.config.get("SOCIAL_WORKER_BATCH", 10)
    threads = current_app.config.get("SOCIAL_WORKER_THREADS", 3)
    worker_id = worker_id or f"w-{uuid.uuid4().hex[:8]}"

    reset = _reset_stale(worker_id)
    job_ids = _claim(batch, worker_id)
    if not job_ids:
        return {"reset_stale": reset, "claimed": 0, "processed": 0, "outcomes": []}

    current_app.logger.info(
        "social worker %s: claimed %d job(s)%s",
        worker_id, len(job_ids),
        f" (reset {reset} stale)" if reset else "",
    )

    app = current_app._get_current_object()
    outcomes = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(_process_in_ctx, app, jid) for jid in job_ids]
        for fut in futures:
            outcomes.append(fut.result())

    return {
        "reset_stale": reset,
        "claimed": len(job_ids),
        "processed": len(outcomes),
        "outcomes": outcomes,
    }


def _process_in_ctx(app, job_id):
    with app.app_context():
        try:
            return _process(job_id)
        finally:
            db.session.remove()


def _process(job_id):
    job = db.session.get(PublishJob, job_id)
    if job is None:
        return {"job": job_id, "result": "missing"}
    target = db.session.get(SocialPostTarget, job.target_id)
    if target is None:
        job.state = "dead"
        job.last_error = "target missing"
        db.session.commit()
        return {"job": job_id, "result": "no_target"}

    try:
        provider = get_provider(target.platform)
        if provider is None:
            raise PermanentError(f"No publisher enabled for {target.platform}")

        account = target.account
        if account is None or account.status != "active":
            raise AuthError("Account not connected or needs re-authorisation")

        provider_state = dict(job.provider_state or {})
        first_attempt = not provider_state.get("started")

        # Rate gate: reserve one slot on the first publish attempt only.
        if first_attempt and provider.capabilities \
                and provider.capabilities.publish_rate:
            limit, window = provider.capabilities.publish_rate
            if not ratelimit.reserve(account.id, limit, window):
                job.next_run_at = datetime.utcnow() + timedelta(minutes=30)
                job.state = "queued"
                db.session.commit()
                return {"job": job_id, "result": "rate_deferred"}
            provider_state["_reserved"] = True

        token = AccountManager.access_token(account)
        content = build_content(target)

        job.state = "publishing"
        if first_attempt:
            provider_state["started"] = True
            step = provider.start_publish(target, content, token)
        else:
            step = provider.poll_publish(target, provider_state, token)

        result = _apply_step(job, target, step, provider_state,
                             provider=provider, token=token)
        db.session.commit()
        return {"job": job_id, "result": result}

    except Exception as exc:  # noqa: BLE001 - normalized below
        db.session.rollback()
        return _handle_failure(job_id, exc)


def _apply_step(job, target, step, provider_state, provider=None, token=None):
    status = getattr(step, "status", None)

    if status in (StepStatus.DONE.value, "done"):
        target.status = "published"
        target.external_post_id = step.external_post_id
        target.permalink = step.permalink
        target.last_error = None
        db.session.add(PublishResult(
            target_id=target.id,
            external_post_id=step.external_post_id,
            permalink=step.permalink,
            published_at=datetime.utcnow(),
        ))
        job.state = "succeeded"
        job.provider_state = provider_state
        audit.record(
            "published", target_id=target.id, post_id=target.social_post_id,
            task_id=_task_id(target),
            detail={"external_post_id": step.external_post_id,
                    "permalink": step.permalink},
            message="Published to " + target.platform,
        )
        current_app.logger.info(
            "social publish OK target=%s platform=%s external_id=%s",
            target.id, target.platform, step.external_post_id,
        )
        _post_first_comment(target, step, provider, token)
        _maybe_finalize_post(target)
        return "published"

    if status in (StepStatus.PENDING.value, "pending"):
        merged = {**provider_state, **(step.provider_state or {})}
        merged["started"] = True
        job.provider_state = merged
        job.state = "queued"
        job.next_run_at = datetime.utcnow() + timedelta(seconds=30)
        return "pending"

    # FAILED - route through the normal failure path.
    raise PermanentError(step.error or "publish step failed")


def _handle_failure(job_id, exc):
    job = db.session.get(PublishJob, job_id)
    target = db.session.get(SocialPostTarget, job.target_id) if job else None
    provider = get_provider(target.platform) if target else None

    error = provider.map_error(exc) if provider else exc
    if not isinstance(error, SocialError):
        error = PermanentError(str(exc))

    outcome = retry_engine.classify_and_schedule(job, error)

    if target is not None:
        target.last_error = job.last_error
        # Release a reserved rate slot on a terminal failure.
        if job.state in ("dead", "failed") and (job.provider_state or {}).get("_reserved"):
            if provider and provider.capabilities and provider.capabilities.publish_rate:
                _, window = provider.capabilities.publish_rate
                if target.social_account_id:
                    ratelimit.release(target.social_account_id, window)
        if isinstance(error, AuthError) and target.account is not None:
            AccountManager.mark_needs_reauth(target.account)
            # Tell whoever connected the channel that it needs reconnecting.
            if target.account.connected_by_id:
                audit.notify(
                    target.account.connected_by_id,
                    "Channel needs reconnecting",
                    f"{target.account.display_name} can no longer publish - "
                    "reconnect it in Social Studio → Accounts.",
                    link="/social/accounts", actor_id=None)
        if job.state in ("dead", "failed"):
            target.status = "failed"
            audit.record(
                "publish_failed", target_id=target.id,
                post_id=target.social_post_id, task_id=_task_id(target),
                detail={"error": job.last_error, "outcome": outcome},
                message="Publish failed: " + (job.last_error or ""),
            )
            post = target.post
            if post and post.created_by_id:
                audit.notify(
                    post.created_by_id, "Post failed to publish",
                    f"“{post.title or 'Your post'}” failed on "
                    f"{target.platform}: {(job.last_error or '')[:120]}",
                    link=f"/social/posts/{target.social_post_id}",
                    actor_id=None)

    # last_error is a platform message, never a token - safe to log.
    current_app.logger.warning(
        "social publish %s job=%s target=%s: %s",
        outcome, job_id, (target.id if target else "?"), job.last_error,
    )
    db.session.commit()
    return {"job": job_id, "result": outcome, "error": job.last_error}


def _post_first_comment(target, step, provider, token):
    """Best-effort: auto-post the target's first comment right after it goes
    live. A failure here is logged but never fails the publish - the post is
    already out."""
    text = (getattr(target, "first_comment", None) or "").strip()
    if not (text and provider and token and step.external_post_id):
        return
    if not (provider.capabilities and provider.capabilities.supports_first_comment):
        return
    if not hasattr(provider, "post_first_comment"):
        return
    try:
        comment_id = provider.post_first_comment(
            step.external_post_id, text, token)
        audit.record(
            "first_comment_posted", target_id=target.id,
            post_id=target.social_post_id, task_id=_task_id(target),
            detail={"comment_id": comment_id},
        )
    except Exception as exc:  # noqa: BLE001 - never break a live publish
        current_app.logger.warning(
            "first comment failed target=%s: %s", target.id, exc)


def _task_id(target):
    post = target.post
    return post.task_id if post else None


def _maybe_finalize_post(target):
    """Roll the parent post's status up once all its targets settle, and -
    when the post came from a task - reflect completion back onto that task
    (Client Review -> Published), so the ERP task lifecycle stays in sync."""
    post = target.post
    if post is None:
        return
    statuses = [t.status for t in post.targets]
    if all(s == "published" for s in statuses):
        post.status = "published"
        # Tell the creator their (often scheduled) post is now live.
        if post.created_by_id:
            plats = sorted({t.platform for t in post.targets})
            audit.notify(
                post.created_by_id, "Post published",
                f"“{post.title or 'Your post'}” is now live on "
                + ", ".join(plats) + ".",
                link=f"/social/posts/{post.id}", actor_id=None)
        if post.task_id:
            from app.social.services import task_link
            task_link.mark_task_published(post)
    elif any(s == "published" for s in statuses) and any(s == "failed" for s in statuses):
        post.status = "partially_published"
    elif all(s == "failed" for s in statuses):
        post.status = "failed"
