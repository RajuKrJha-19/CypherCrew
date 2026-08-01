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

import threading
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


# A claim older than this is treated as abandoned by a dead worker. Sized
# above the longest realistic single start_publish (a large resumable YouTube
# upload can run several minutes) so a slow-but-alive upload is not requeued
# out from under itself and falsely flagged as interrupted.
_STALE_CLAIM_MINUTES = 20

# Max async status polls (~30s apart) before giving up on a remote op that
# never completes - e.g. an Instagram container stuck IN_PROGRESS. ~20 min,
# enough for normal video processing, so a genuinely stuck job settles and is
# flagged instead of polling forever with the post pinned at "publishing".
_MAX_PENDING_POLLS = 40


def _reset_stale(worker_id):
    """Return jobs abandoned by a dead worker (stuck 'claimed' past the
    stale window) to the queue.

    'claimed' is deliberately the only state swept, and that is not an
    oversight: it is the only in-flight state that ever reaches the database.
    `publishing` is assigned in memory during _process but _apply_step always
    overwrites it - with succeeded, dead or queued - before the commit, and an
    exception rolls the whole thing back. So there is no such thing as a job
    stranded in `publishing`, and sweeping for it would be dead code that
    looked like a safety net.

    What this DID get wrong is the clock. `locked_at` was stamped once, at
    claim time, and never touched again, so the window had to cover claim
    overhead plus the entire upload. A YouTube resumable upload that ran past
    it was requeued out from under a worker that was alive and succeeding -
    and because the dispatch marker survives, the resume was then treated as
    an interrupted publish and killed. A video that uploaded perfectly came
    back to the creator as "may already be live, check before retrying", and
    the target never published. _process now refreshes locked_at at the moment
    the long call begins, so this window measures the operation rather than
    the operation plus everything before it.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=_STALE_CLAIM_MINUTES)
    stale = (
        PublishJob.query
        .filter(PublishJob.state == "claimed", PublishJob.locked_at < cutoff)
        # SKIP LOCKED, like _claim: several gunicorn workers run this tick at
        # once, and a plain SELECT let two of them read the same row and both
        # requeue it. Whichever worker gets there first does the reset; the
        # others move on instead of blocking behind it.
        .with_for_update(skip_locked=True)
        .all()
    )
    for job in stale:
        job.state = "queued"
        job.locked_by = None
    if stale:
        db.session.commit()
    return len(stale)


def _rate_defer_message(platform, limit, window_seconds, next_run_at):
    """Plain-language reason a post is sitting in the queue untouched.

    The window is spelled out because the caps are not guessable and the
    tight one is the whole problem: YouTube allows six uploads per 24 hours,
    so a busy day silently parks everything after the sixth. Times are shown
    in IST, the timezone everyone reading this works in - next_run_at is UTC.

    The platform name comes from PLATFORM_LABELS, not from `.capitalize()`,
    which renders "Youtube", "Linkedin" and "Google business" - wrong in a
    sentence whose entire job is to be read and believed.
    """
    from app.utils.social_platforms import PLATFORM_LABELS
    from app.utils.timezone import IST_OFFSET

    hours = (window_seconds or 0) / 3600.0
    if hours >= 23:
        window_text = "%g hours" % round(hours)
    elif hours >= 1:
        window_text = "%g hour%s" % (round(hours), "" if round(hours) == 1 else "s")
    else:
        window_text = "%d minutes" % round((window_seconds or 0) / 60.0)

    when = (next_run_at + IST_OFFSET).strftime("%d %b %H:%M")

    return (
        "Waiting on the %s upload limit (%d per %s), which is currently used "
        "up. This post has not failed - the next attempt is at %s IST."
        % (PLATFORM_LABELS.get(platform, platform.capitalize()),
           limit, window_text, when)
    )


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


#: Single-flight state for kick_async: at most ONE kick thread runs at a
#: time, and a kick requested while one is running schedules exactly one more
#: pass (so a burst of "publish now" / "retry" clicks can never spawn a thread
#: storm and exhaust the DB pool). Coalescing is safe because a drain claims
#: ALL due jobs, so one extra pass covers every click that arrived meanwhile.
_kick_lock = threading.Lock()
_kick = {"running": False, "again": False}


def _kick_drain(app, rounds):
    with app.app_context():
        from app.social.services import scheduling
        scheduling.enqueue_due()
        for _ in range(max(1, rounds)):
            # Pull forward jobs due now / waiting on a short async poll (like
            # the manual Process-queue button) so a start -> poll -> publish
            # chain completes without waiting on the tick. Deliberately does
            # NOT pull forward a rate/backoff deferral that is minutes out.
            horizon = datetime.utcnow() + timedelta(seconds=60)
            PublishJob.query.filter(
                PublishJob.state == "queued",
                PublishJob.next_run_at <= horizon,
            ).update({PublishJob.next_run_at: datetime.utcnow()})
            db.session.commit()
            outcome = drain()
            if outcome.get("claimed", 0) == 0:
                break


def kick_async(app, rounds=3):
    """Fire an enqueue + bounded drain soon, so a "publish now" (or a manual
    retry) goes out within a second or two instead of waiting for the next
    periodic worker tick. Single-flight: concurrent calls coalesce into one
    running thread plus at most one follow-up pass, so a click burst can't
    storm threads. Safe alongside the periodic worker (claim is FOR UPDATE
    SKIP LOCKED; jobs are idempotent)."""
    with _kick_lock:
        if _kick["running"]:
            _kick["again"] = True      # a running kick will do one more pass
            return
        _kick["running"] = True
        _kick["again"] = False

    def _run():
        while True:
            try:
                _kick_drain(app, rounds)
            except Exception:  # noqa: BLE001 - never break the caller
                try:
                    with app.app_context():
                        app.logger.exception(
                            "[social] immediate publish kick failed")
                except Exception:  # noqa: BLE001
                    pass
            # Check "again" and clear "running" atomically under one lock hold,
            # so a kick that arrives in this window is never lost and never
            # spawns a second thread.
            with _kick_lock:
                if _kick["again"]:
                    _kick["again"] = False
                    continue           # do one more pass for the coalesced kicks
                _kick["running"] = False
                return

    threading.Thread(target=_run, name="social-kick", daemon=True).start()


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

    # Target-level idempotency: if this target is already published (a sibling
    # job, a double "publish now" whose two jobs carry different idempotency
    # keys, or any future enqueuer that doesn't check target.job), never
    # dispatch again - just settle this job. Complements the per-attempt
    # dispatch marker with a per-target guard against a duplicate live post.
    if target.status == "published" or target.external_post_id:
        job.state = "succeeded"
        db.session.commit()
        return {"job": job_id, "result": "already_published"}

    try:
        provider = get_provider(target.platform)
        if provider is None:
            raise PermanentError(f"No publisher enabled for {target.platform}")

        account = target.account
        if account is None or account.status != "active":
            raise AuthError("Account not connected or needs re-authorisation")

        provider_state = dict(job.provider_state or {})
        started = bool(provider_state.get("started"))

        # A previous attempt DISPATCHED start_publish but the worker died
        # before recording the outcome (deploy / restart / OOM mid-upload).
        # The post may already be live on the platform, so re-sending risks a
        # DUPLICATE and there is no remote handle to poll. Hand it to a human
        # to verify rather than silently republish. (A normal error clears the
        # marker below, so this only fires on an actual worker death.)
        if provider_state.get("dispatched") and not started:
            return _handle_interrupted(job, target)

        first_attempt = not started

        # Rate gate: reserve one slot, once. Guard on _reserved so a retry
        # (which is still not-started) doesn't reserve a second slot.
        if first_attempt and not provider_state.get("_reserved") \
                and provider.capabilities and provider.capabilities.publish_rate:
            limit, window = provider.capabilities.publish_rate
            if not ratelimit.reserve(account.id, limit, window):
                job.next_run_at = datetime.utcnow() + timedelta(minutes=30)
                job.state = "queued"
                # Say so. This branch used to defer in complete silence: the
                # job went back to 'queued', the post sat in the queue, and
                # nothing anywhere named the reason. YouTube's cap is six
                # uploads per 24 hours, so once it is spent every attempt
                # lands here and re-defers 30 minutes at a time - which from
                # the outside is indistinguishable from a broken publisher,
                # and is what "Instagram aur YouTube pe jaa hi nahi raha"
                # actually looked like.
                #
                # Written to the target as well as the job because that is
                # where someone chasing a late post looks first (the post
                # detail page lists target.last_error as the reason it is not
                # out yet). _apply_step clears it on success.
                job.last_error = _rate_defer_message(
                    target.platform, limit, window, job.next_run_at)
                target.last_error = job.last_error
                db.session.commit()
                return {"job": job_id, "result": "rate_deferred"}

            # Commit the reservation NOW, before anything slow.
            #
            # ratelimit.reserve() takes SELECT ... FOR UPDATE on this
            # account's platform_rate_budgets row, and that lock is held until
            # the transaction ends. The next commit used to be the dispatch
            # marker below - on the far side of a token refresh, a media
            # download and transcode.fit_content(), which shells out to
            # ffmpeg and can run for minutes. So every concurrent publish for
            # one Instagram account serialised behind that ffmpeg run, each
            # holding an idle-in-transaction lock; under a burst the pool
            # drained and the whole autoworker tick failed.
            #
            # Persisting _reserved in the same commit is what makes this safe
            # to do early: a crash between here and the publish leaves the
            # slot spent and the flag set, so the retry does not reserve a
            # second one. Spending a slot for a publish that never happened
            # under-counts the window by one, which is the safe direction -
            # the same trade-off _handle_interrupted already documents.
            provider_state["_reserved"] = True
            job.provider_state = dict(provider_state)
            db.session.commit()

        token = AccountManager.access_token(account)
        content = build_content(target)

        if first_attempt:
            # Downscale any video too wide for this platform (aspect ratio
            # kept), so an oversized-but-otherwise-fine reel/story publishes
            # instead of being rejected. No-op without ffmpeg. In-memory only:
            # the derived file is ephemeral and the source asset is untouched.
            from app.social.media import transcode
            transcode.fit_content(content, provider.capabilities)

            # Persist a dispatch marker BEFORE the side-effecting call, with
            # the job still 'claimed'. On a worker KILL mid-publish the marker
            # survives, _reset_stale requeues the claimed job, and the guard
            # above turns the resume into an interrupted-publish for manual
            # verification - instead of silently re-uploading and duplicating.
            # A NORMAL start_publish error propagates to _handle_failure, which
            # clears the marker so the retry is a clean first attempt - EXCEPT
            # a read-timeout, where the request may have been delivered and the
            # marker is kept so the resume is treated as interrupted. (No
            # fragile pre-raise commit here: _handle_failure owns the clear.)
            provider_state["dispatched"] = True
            job.provider_state = dict(provider_state)
            # Restart the stale clock here, not at claim time. Everything above
            # - token refresh, build_content, and especially transcode.fit_content,
            # which shells out to ffmpeg - already ran, and the upload itself is
            # still ahead. Measuring from the claim meant a large YouTube upload
            # could cross _STALE_CLAIM_MINUTES while succeeding, get requeued by
            # _reset_stale, and then be killed as an interrupted publish by the
            # dispatched guard above. Stamping it here makes the window cover the
            # publish call, which is what it was sized for.
            job.locked_at = datetime.utcnow()
            db.session.commit()

            step = provider.start_publish(target, content, token)

            provider_state["started"] = True
            provider_state.pop("dispatched", None)
            job.state = "publishing"
        else:
            job.state = "publishing"
            step = provider.poll_publish(target, provider_state, token)

        result = _apply_step(job, target, step, provider_state,
                             provider=provider, token=token)
        db.session.commit()
        return {"job": job_id, "result": result}

    except Exception as exc:  # noqa: BLE001 - normalized below
        db.session.rollback()
        return _handle_failure(job_id, exc)


def _notify_story_link_pending(target):
    """A story meant to open a post is live, but the sticker that makes it
    tappable can only be added by hand in the Instagram app.

    A story lasts 24 hours, so this cannot wait for someone to notice it on
    a dashboard - whoever created the post is told the moment it goes out.
    Best-effort: a notification failure must never fail a publish that has
    already happened on the platform.
    """
    if not target.needs_story_link:
        return
    post = target.post
    if post is None or not post.created_by_id:
        return
    try:
        link = target.story_link_url
        audit.notify(
            post.created_by_id,
            "Add the story sticker",
            f"“{post.title or 'Your story'}” is live on Instagram. Meta's API "
            "can't attach the sticker, so open the Instagram app and add the "
            "post sticker to make it tappable"
            + (f" — {link}" if link else "") + ".",
            link=f"/social/posts/{post.id}", actor_id=None,
        )
    except Exception:  # noqa: BLE001 - never fail a completed publish
        current_app.logger.exception(
            "story-link notification failed for target=%s", target.id)


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
        _notify_story_link_pending(target)
        _maybe_finalize_post(target)
        return "published"

    if status in (StepStatus.PENDING.value, "pending"):
        merged = {**provider_state, **(step.provider_state or {})}
        merged["started"] = True
        polls = int(merged.get("polls", 0)) + 1
        merged["polls"] = polls
        job.provider_state = merged
        # Cap the async poll loop: a remote op that never finishes (e.g. an IG
        # container stuck IN_PROGRESS) would otherwise poll every 30s forever
        # with the post pinned at "publishing" and never flagged. Past the
        # ceiling, settle the target as failed so it rolls up + surfaces.
        if polls >= _MAX_PENDING_POLLS:
            job.state = "dead"
            job.last_error = ("The platform did not finish processing this "
                              "media in time - try republishing.")
            target.status = "failed"
            target.last_error = job.last_error
            _maybe_finalize_post(target)
            return "poll_timeout"
        job.state = "queued"
        job.next_run_at = datetime.utcnow() + timedelta(seconds=30)
        return "pending"

    # FAILED - route through the normal failure path.
    raise PermanentError(step.error or "publish step failed")


def _handle_interrupted(job, target):
    """Terminal handler for a publish that was dispatched but never confirmed
    (the worker died mid-publish). Re-sending could duplicate the post and
    there is no remote handle to poll, so flag the target and ask the creator
    to verify on the platform. A manual retry from History is a deliberate
    choice once they have checked."""
    platform = target.platform if target is not None else "the platform"
    msg = ("Publishing was interrupted after the upload had started, so the "
           f"post may already be live on {platform}. Check it there before "
           "retrying.")
    job.state = "dead"
    job.last_error = msg

    # Deliberately do NOT release the reserved rate slot: the post may already
    # be live on the platform, so keep the slot consumed (under-counting the
    # window by one is the safe direction - it avoids letting an extra post
    # through against a possibly-already-published one).

    if target is not None:
        target.status = "failed"
        target.last_error = msg
        audit.record(
            "publish_interrupted", target_id=target.id,
            post_id=target.social_post_id, task_id=_task_id(target),
            message=msg,
        )
        post = target.post
        if post and post.created_by_id:
            audit.notify(
                post.created_by_id, "Check a possibly-published post",
                f"“{post.title or 'Your post'}” on {platform} was interrupted "
                "mid-publish and may already be live — check the platform "
                "before retrying.",
                link=f"/social/posts/{target.social_post_id}", actor_id=None)
        _maybe_finalize_post(target)

    current_app.logger.warning(
        "social publish INTERRUPTED job=%s target=%s — flagged for manual "
        "verification", job.id, target.id if target is not None else "?")
    db.session.commit()
    return {"job": job.id, "result": "interrupted"}


def _handle_failure(job_id, exc):
    job = db.session.get(PublishJob, job_id)
    if job is None:
        # The job row vanished mid-failure (deleted concurrently). Nothing to
        # reschedule; classify_and_schedule would dereference None.
        db.session.rollback()
        return {"job": job_id, "result": "missing"}
    target = db.session.get(SocialPostTarget, job.target_id) if job else None
    provider = get_provider(target.platform) if target else None

    # Reaching here means start_publish (or poll_publish) returned via an
    # exception, not a worker kill, so the dispatch marker must not survive to
    # trip the interrupted guard on the retry - clear it. The exception is a
    # read timeout, though (request sent, response lost), the publish MAY have
    # landed on the platform; keep the marker so the resume is treated as an
    # interrupted-publish (verify manually) rather than a silent duplicate.
    if job is not None and (job.provider_state or {}).get("dispatched"):
        import requests
        if not isinstance(exc, requests.exceptions.ReadTimeout):
            ps = dict(job.provider_state)
            ps.pop("dispatched", None)
            job.provider_state = ps or None

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
            # Roll the post up (failed / partially_published) and reflect the
            # "Publish failed · retry" state onto the originating task.
            _maybe_finalize_post(target)

    # last_error is a platform message, never a token - safe to log.
    current_app.logger.warning(
        "social publish %s job=%s target=%s: %s",
        outcome, job_id, (target.id if target else "?"), job.last_error,
    )
    db.session.commit()
    return {"job": job_id, "result": outcome, "error": job.last_error}


def _post_first_comment(target, step, provider, token):
    """Best-effort: auto-post the target's first comment right after it goes
    live. A failure never fails the publish - the post is already out.

    Every way this can end without a comment now leaves an audit row.
    Previously each of them was a bare `return` or a log line, so a first
    comment that was never posted looked exactly like one that was: the
    post published, the composer showed the text, and nothing said the
    step had been skipped. That is how a permanently-broken first comment
    (missing Graph scope, unsupported provider) went unnoticed.
    """
    text = (getattr(target, "first_comment", None) or "").strip()
    if not text:
        return

    def skipped(reason):
        audit.record(
            "first_comment_skipped", target_id=target.id,
            post_id=target.social_post_id, task_id=_task_id(target),
            detail={"reason": reason},
            message=f"First comment not posted: {reason}",
        )
        current_app.logger.info(
            "first comment skipped target=%s: %s", target.id, reason)

    # A story has no comments to post one on - and the composer attaches
    # the same first comment to a companion story as to its feed post.
    if target.post_type == "story":
        return skipped("stories don't take comments")

    if not step.external_post_id:
        return skipped("the platform returned no post id")

    if not (provider and token):
        return skipped("no provider or access token for this channel")

    if not (provider.capabilities
            and provider.capabilities.supports_first_comment):
        return skipped(f"{target.platform} doesn't support a first comment")

    if not hasattr(provider, "post_first_comment"):
        return skipped(f"the {target.platform} adapter can't post comments")

    try:
        comment_id = provider.post_first_comment(
            step.external_post_id, text, token)
    except Exception as exc:  # noqa: BLE001 - never break a live publish
        audit.record(
            "first_comment_failed", target_id=target.id,
            post_id=target.social_post_id, task_id=_task_id(target),
            detail={"error": str(exc)},
            message=f"First comment failed: {exc}",
        )
        current_app.logger.warning(
            "first comment failed target=%s: %s", target.id, exc)
        return

    audit.record(
        "first_comment_posted", target_id=target.id,
        post_id=target.social_post_id, task_id=_task_id(target),
        detail={"comment_id": comment_id},
    )


def _task_id(target):
    post = target.post
    return post.task_id if post else None


def _maybe_finalize_post(target):
    """Roll the parent post's status up once all its targets settle, and -
    when the post came from a task - reflect completion back onto that task
    (Client Review -> Published), so the ERP task lifecycle stays in sync."""
    from app.models import SocialPost, SocialPostTarget
    # Sibling targets of one post publish CONCURRENTLY (the drain thread pool,
    # e.g. an IG feed post + its companion Story). Without serialising here,
    # each thread set its own target "published" (uncommitted) and read the
    # others as still unpublished, so NONE ran the "all published" rollup and a
    # fully-live post was stranded at "scheduled". Lock the post row so threads
    # serialise, and read target statuses fresh from the DB (own flushed change
    # + siblings' committed changes) so the last committer finalises correctly.
    post = (db.session.query(SocialPost)
            .filter(SocialPost.id == target.social_post_id)
            .with_for_update().first())
    if post is None:
        return
    statuses = [s for (s,) in db.session.query(SocialPostTarget.status)
                .filter(SocialPostTarget.social_post_id == post.id).all()]

    # The status these targets imply, shared with lifecycle's removal rollup so
    # the two cannot disagree about what a settled post looks like. It returns
    # None while anything is still in flight.
    from app.social.services.lifecycle import post_status_from

    settled = post_status_from(statuses)
    if settled is not None:
        post.status = settled

    if all(s == "published" for s in statuses):
        # Tell the creator their (often scheduled) post is now live.
        if post.created_by_id:
            plats = sorted({t.platform for t in post.targets})
            audit.notify(
                post.created_by_id, "Post published",
                f"“{post.title or 'Your post'}” is now live on "
                + ", ".join(plats) + ".",
                link=f"/social/posts/{post.id}", actor_id=None)

    # Reflect the post's settled state on the originating task (live / in
    # queue / failed) in every case, not only on full success.
    if post.task_id:
        from app.social.services import task_link
        task_link.sync_task_from_posts(task_link._task_of(post))
