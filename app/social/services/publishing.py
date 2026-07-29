"""PublishingService - the single choke-point that turns approved content
into queued work. Validates each target against its platform Capabilities,
snapshots a version, and either schedules it or enqueues an immediate job.
Publishing can only happen through here (or the scheduler), so it can never
be bypassed.
"""

from datetime import datetime

from app.extensions import db
from app.models import PublishJob
from app.social.registry import get_provider
from app.social.services import approval, scheduling, versioning, audit
from app.social.media import pipeline, probe, fit, transcode
from app.social.dto import PostContent


def build_content(target) -> PostContent:
    """Resolve a target into the platform-agnostic content the provider
    consumes (media keys resolved from R2 / TaskFile / ClientAsset)."""
    return PostContent(
        platform=target.platform,
        post_type=target.post_type,
        caption=target.caption or "",
        hashtags=target.hashtags or "",
        media=pipeline.resolve_media(target.media),
        scheduled_for=target.scheduled_for,
    )


def validate_target(target) -> list[str]:
    """Pre-flight the target against its provider's Capabilities. Returns a
    list of human-readable problems (empty = ok). If the provider isn't
    loaded yet (pre-Phase-1), returns a single 'not available' note rather
    than raising."""
    provider = get_provider(target.platform)
    if provider is None:
        return [f"The {target.platform} publisher is not enabled yet."]
    if not target.social_account_id:
        return ["No connected account selected for this target."]

    # Measure anything the browser could not - a .mov or HEVC deliverable
    # that Chrome cannot decode, and the frame rate/codec no browser can
    # report at all. No-ops when ffprobe is not installed, and never
    # raises, so this can only ever add information.
    probe.ensure_measured(target)

    return provider.validate(build_content(target))


def _downscale_will_fix(target):
    """True if every problem with this target is a too-wide video that a
    downscale on publish will fix (and at least one is). Requires ffmpeg to be
    usable - without it the resize can't happen, so the target must still be
    blocked rather than scheduled on a promise we can't keep."""
    if not transcode.available():
        return False
    provider = get_provider(target.platform)
    caps = provider.capabilities if provider else None
    if caps is None:
        return False
    content = build_content(target)
    if not caps.supports(content.post_type):
        return False
    spec = caps.spec_for(content.post_type)
    if spec is None:
        return False

    needed = False
    for media in content.media:
        if not fit.check_spec(spec, media.measurements or {}):
            continue                                   # already fits
        if fit.downscale_target_width(spec, media.measurements or {}) is None:
            return False                               # a resize won't fix it
        needed = True
    return needed


def schedule_post(post, actor_id=None):
    """Validate + snapshot + move each target to 'scheduled'. Requires the
    post to be approved."""
    approval.require_approved(post)
    versioning.snapshot_post(post, edited_by_id=actor_id)

    problems = {}
    for target in post.targets:
        errs = validate_target(target)
        if errs and _downscale_will_fix(target):
            # The only problem is a video too wide for this platform; the
            # worker downscales it on publish (aspect kept). Schedule it
            # rather than blocking a file we can fix ourselves.
            scheduling.schedule_target(
                target, target.scheduled_for or datetime.utcnow(), actor_id)
            target.last_error = None
            audit.record("auto_resize_planned", target_id=target.id,
                         post_id=post.id, actor_id=actor_id,
                         detail={"problems": errs})
            continue
        if errs:
            # "blocked", not left at "draft". Draft means "nobody has
            # submitted this yet"; this target HAS been submitted and
            # cannot go out as it stands. Leaving it at draft is why a post
            # could sit at Scheduled forever with one platform quietly dead
            # and no way to act on it - the rollup had nothing to settle
            # against and the UI had no state to offer a fix for.
            problems[target.id] = errs
            target.status = "blocked"
            target.last_error = " ".join(errs)
            continue
        # scheduled_for should already be set on the target; default now.
        scheduling.schedule_target(
            target, target.scheduled_for or datetime.utcnow(), actor_id
        )

    scheduled = len(post.targets) - len(problems)

    # Only call it scheduled if something actually is. A post where every
    # platform was blocked has nothing queued, and saying "Scheduled" would
    # leave someone waiting for a publish that can never happen.
    post.status = "scheduled" if scheduled else "failed"

    audit.record("scheduled", post_id=post.id, actor_id=actor_id,
                 task_id=post.task_id, detail={"problems": problems} or None)
    db.session.commit()
    return {"scheduled": scheduled, "problems": problems}


def publish_target_now(target, actor_id=None):
    """Enqueue an immediate publish for one target (idempotent for this
    instant)."""
    key = f"tgt-{target.id}-now-{int(datetime.utcnow().timestamp())}"
    if not PublishJob.query.filter_by(idempotency_key=key).first():
        db.session.add(PublishJob(
            target_id=target.id, state="queued",
            idempotency_key=key, next_run_at=datetime.utcnow(),
        ))
    target.status = "publishing"
    audit.record("publish_now", target_id=target.id,
                 post_id=target.social_post_id, actor_id=actor_id)
    db.session.commit()
    return target
