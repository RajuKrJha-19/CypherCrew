"""Social Publishing Engine UI + JSON API.

Registered only when SOCIAL_ENGINE_ENABLED (see app/__init__.py), so the
whole surface is absent unless the engine is turned on. Gated by the
manage_social / connect_social_accounts permissions.
"""

from datetime import datetime, timedelta

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    Client, ClientAsset, PublishJob, PublishResult, SocialAccount,
    SocialAuditLog, SocialMediaAsset, SocialPost, SocialPostTarget,
)
from app.social.dto import TokenBundle
from app.social import status as engine_status
from app.social.registry import registry
from app.social.services import (
    approval, audit, publishing, recovery, scheduling, versioning,
)
from app.social.services.accounts import AccountManager
from app.utils.permissions import has_permission, can_manage_social_engine
from app.utils.social_platforms import PLATFORMS, label as platform_label


social_bp = Blueprint("social", __name__, url_prefix="/social")

# The composer's datetime-local inputs are entered in IST (the team's clock);
# the engine stores/compares in UTC. Convert on the boundary.
_IST_OFFSET = timedelta(hours=5, minutes=30)

#: Statuses whose content is still fully editable.
_EDITABLE_STATUSES = ("draft", "pending_approval", "rejected")

_NO_INSTAGRAM = (
    "No Instagram Business account is linked to your Facebook Page(s). Link an "
    "Instagram Business or Creator account to a Page in Meta Business settings, "
    "then refresh again."
)


def _guard():
    if not has_permission(current_user, "manage_social"):
        abort(403)


def _engine_guard():
    """Engine ops (worker kick, retry/requeue) are owner/admin-only - normal
    publishers must never reach the internal machinery, backend included."""
    if not can_manage_social_engine(current_user):
        abort(403)


def _connectable_keys():
    """Registered platforms that have their own connect (OAuth) entry point.
    Instagram is excluded - it is discovered through the Facebook consent."""
    return [k for k in registry.keys()
            if getattr(registry.get(k), "connectable", True)]


def _grouped_accounts(accounts):
    """Group Instagram accounts UNDER their parent Facebook Page (matched by
    page_id), so the UI shows the Meta Business Suite hierarchy instead of a
    flat list. Returns [{account, children:[...]}], parents first."""
    fb_by_page = {
        (a.meta or {}).get("page_id") or a.external_id: a
        for a in accounts if a.platform == "facebook"
    }
    children, grouped_ig = {}, set()
    for a in accounts:
        if a.platform == "instagram":
            parent = fb_by_page.get((a.meta or {}).get("page_id"))
            if parent is not None:
                children.setdefault(parent.id, []).append(a)
                grouped_ig.add(a.id)

    groups = []
    for a in accounts:
        if a.platform == "facebook":
            groups.append({"account": a, "children": children.get(a.id, [])})
        elif a.platform == "instagram" and a.id in grouped_ig:
            continue  # rendered nested under its Page
        else:
            groups.append({"account": a, "children": []})
    return groups


def _parse_schedule(value):
    """datetime-local (IST) -> naive UTC datetime, or None."""
    if not value:
        return None
    try:
        ist = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return ist - _IST_OFFSET


def _to_ist_input(dt):
    """Naive UTC datetime -> datetime-local (IST) string for a form field."""
    if not dt:
        return ""
    return (dt + _IST_OFFSET).strftime("%Y-%m-%dT%H:%M")


def _capabilities_map():
    """{platform_key: capabilities} for whatever providers are registered,
    so the composer can drive per-platform post-type options client-side."""
    out = {}
    for key, provider in registry.all().items():
        caps = provider.capabilities
        out[key] = {
            "post_types": sorted(caps.post_types) if caps else [],
            "max_carousel": caps.max_carousel if caps else None,
            "max_caption_chars": caps.max_caption_chars if caps else None,
            "simulation": getattr(provider, "is_simulation", False),
        }
    return out


@social_bp.route("/")
@login_required
def index():
    _guard()
    accounts = AccountManager.list_accounts(include_revoked=False)
    now = datetime.utcnow()
    start_today = datetime(now.year, now.month, now.day)

    upcoming = (
        SocialPostTarget.query
        .filter(
            SocialPostTarget.status.in_(["scheduled", "approved"]),
            SocialPostTarget.scheduled_for.isnot(None),
            SocialPostTarget.scheduled_for >= now,
        )
        .order_by(SocialPostTarget.scheduled_for.asc())
        .limit(6)
        .all()
    )
    recent = (
        SocialAuditLog.query
        .order_by(SocialAuditLog.created_at.desc())
        .limit(7)
        .all()
    )
    published_today = (
        PublishResult.query
        .filter(PublishResult.created_at >= start_today)
        .count()
    )
    drafts_count = SocialPost.query.filter_by(status="draft").count()

    return render_template(
        "social/index.html",
        accounts=accounts,
        groups=_grouped_accounts(accounts),
        platforms=PLATFORMS,
        available=registry.keys(),
        connectable=_connectable_keys(),
        status=engine_status.engine_status(),
        upcoming=upcoming,
        recent=recent,
        published_today=published_today,
        drafts_count=drafts_count,
        has_facebook=any(a.platform == "facebook" for a in accounts),
        to_ist=_to_ist_input,
    )


@social_bp.route("/accounts")
@login_required
def accounts():
    _guard()
    accts = AccountManager.list_accounts(include_revoked=False)
    return render_template(
        "social/accounts.html",
        groups=_grouped_accounts(accts),
        accounts=accts,
        platforms=PLATFORMS,
        available=registry.keys(),
        connectable=_connectable_keys(),
        has_facebook=any(a.platform == "facebook" for a in accts),
        has_instagram=any(a.platform == "instagram" for a in accts),
        status=engine_status.engine_status(),
    )


@social_bp.route("/instagram/discover", methods=["POST"])
@login_required
def discover_instagram():
    """Refresh: discover IG Business accounts linked to ALREADY-connected
    Facebook Pages, using each Page's stored token. No OAuth - this is the
    Buffer/Meta Business Suite behaviour where Instagram rides on Facebook."""
    if not has_permission(current_user, "connect_social_accounts"):
        abort(403)
    ig = registry.get("instagram")
    fb_accounts = AccountManager.list_accounts(platform="facebook")

    if ig is None or not hasattr(ig, "discover_for_page"):
        flash("Instagram discovery isn't available in the current mode.",
              "error")
        return redirect(url_for("social.accounts"))
    if not fb_accounts:
        flash("Connect a Facebook Page first — Instagram accounts are "
              "discovered from your Pages.", "info")
        return redirect(url_for("social.accounts"))

    found = 0
    for fb in fb_accounts:
        page_id = (fb.meta or {}).get("page_id") or fb.external_id
        try:
            token = AccountManager.access_token(fb)
            info = ig.discover_for_page(page_id, token,
                                        page_name=fb.display_name)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "[ig-discover] page %s discovery failed", page_id)
            continue
        if info is None:
            continue
        bundle = TokenBundle(access_token=token, scopes=fb.scopes,
                             token_expires_at=fb.token_expires_at)
        AccountManager.upsert_from_oauth(
            "instagram", info, bundle, current_user.id)
        found += 1
    db.session.commit()

    if found:
        flash(f"Discovered {found} Instagram account(s) linked to your "
              "Pages.", "success")
    else:
        flash(_NO_INSTAGRAM, "info")
    return redirect(url_for("social.accounts"))


@social_bp.route("/analytics")
@login_required
def analytics():
    _guard()
    return render_template(
        "social/analytics.html",
        accounts=AccountManager.list_accounts(include_revoked=False),
        status=engine_status.engine_status(),
    )


# Job state -> the board column it belongs to.
_QUEUE_COLUMN = {
    "queued": "queued",
    "claimed": "publishing", "uploading": "publishing",
    "awaiting_remote": "publishing", "publishing": "publishing",
    "succeeded": "completed",
    "failed": "failed", "dead": "failed",
}


@social_bp.route("/queue")
@login_required
def queue():
    """The publishing queue as a live board: Queued · Publishing · Completed
    · Failed, filterable by platform and free-text, with retry on failures."""
    _guard()
    platform_f = (request.args.get("platform") or "").strip()
    q_search = (request.args.get("q") or "").strip()
    needle = q_search.lower()

    jobs = (
        PublishJob.query
        .order_by(PublishJob.updated_at.desc())
        .limit(400)
        .all()
    )
    buckets = {"queued": [], "publishing": [], "completed": [], "failed": []}
    for job in jobs:
        target = job.target
        plat = target.platform if target else ""
        if platform_f and plat != platform_f:
            continue
        if needle:
            title = ((target.post.title if target and target.post else "")
                     or "").lower()
            if needle not in title and needle not in (plat or "").lower():
                continue
        column = _QUEUE_COLUMN.get(job.state)
        if column:
            buckets[column].append(job)

    return render_template(
        "social/queue.html",
        buckets=buckets,
        status=engine_status.engine_status(),
        platforms=PLATFORMS,
        platform_f=platform_f,
        q_search=q_search,
    )


@social_bp.route("/jobs/<int:job_id>/requeue", methods=["POST"])
@login_required
def requeue_job(job_id):
    _engine_guard()
    job = PublishJob.query.get_or_404(job_id)
    if recovery.requeue_job(job, actor_id=current_user.id, commit=True):
        flash("Job requeued.", "success")
    else:
        flash("That job is not in a recoverable state.", "error")
    return redirect(url_for("social.queue"))


@social_bp.route("/jobs/requeue-all", methods=["POST"])
@login_required
def requeue_all():
    _engine_guard()
    count = recovery.requeue_all_dead(actor_id=current_user.id)
    flash(f"Requeued {count} job(s).", "success")
    return redirect(url_for("social.queue"))


@social_bp.route("/queue/process", methods=["POST"])
@login_required
def process_queue():
    """Kick the scheduler + worker once. In production these run on cron;
    this button drives the full loop on demand (and is how the simulation
    workflow completes locally)."""
    _engine_guard()
    from app.social.queue import worker
    enq = scheduling.enqueue_due()

    # A manual kick advances everything now - including async jobs (e.g. an
    # Instagram container) that would otherwise wait for their next poll
    # window. We force every queued job due and drain a few times so a
    # start->poll->publish chain completes in one click. In production the
    # cron worker does this naturally across its runs.
    processed = 0
    for _ in range(5):
        PublishJob.query.filter(PublishJob.state == "queued").update(
            {PublishJob.next_run_at: datetime.utcnow()})
        db.session.commit()
        drained = worker.drain()
        processed += drained["processed"]
        if drained["claimed"] == 0:
            break

    flash(f"Enqueued {enq['enqueued']} due · processed {processed} job(s).",
          "success")
    return redirect(request.referrer or url_for("social.queue"))


# ======================================================================
# Content Composer + draft / approval / schedule workflow
# ======================================================================

@social_bp.route("/drafts")
@login_required
def drafts():
    _guard()
    posts = (
        SocialPost.query
        .filter(SocialPost.status.in_(
            ["draft", "pending_approval", "rejected", "approved", "scheduled"]))
        .order_by(SocialPost.updated_at.desc())
        .limit(200)
        .all()
    )
    return render_template("social/drafts.html", posts=posts)


@social_bp.route("/compose")
@login_required
def compose():
    _guard()
    return render_template(
        "social/compose.html",
        post=None,
        accounts=AccountManager.list_accounts(),
        clients=Client.query.filter_by(status="active").order_by(
            Client.client_name).all(),
        capabilities=_capabilities_map(),
        selected_account_ids=[],
        selected_asset_ids=[],
        post_type="image",
        schedule_value="",
    )


@social_bp.route("/posts/<int:post_id>/edit")
@login_required
def edit_post(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if post.status not in _EDITABLE_STATUSES:
        flash("This post can no longer be edited.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
    # Reconstruct the shared form state from the post's targets.
    account_ids = [t.social_account_id for t in post.targets if t.social_account_id]
    first = post.targets[0] if post.targets else None
    asset_ids = []
    if first:
        asset_ids = [m.client_asset_id for m in first.media if m.client_asset_id]
    return render_template(
        "social/compose.html",
        post=post,
        accounts=AccountManager.list_accounts(),
        clients=Client.query.filter_by(status="active").order_by(
            Client.client_name).all(),
        capabilities=_capabilities_map(),
        selected_account_ids=account_ids,
        selected_asset_ids=asset_ids,
        post_type=(first.post_type if first else "image"),
        schedule_value=_to_ist_input(first.scheduled_for if first else None),
    )


def _apply_composer_form(post):
    """Build/rebuild a post's targets + media from the composer form. Used
    by both create and edit (edit only runs for editable statuses)."""
    post.title = (request.form.get("title") or "").strip() or None
    client_id = request.form.get("client_id", type=int)
    post.client_id = client_id
    post.base_caption = (request.form.get("caption") or "").strip() or None

    post_type = (request.form.get("post_type") or "image").strip()
    account_ids = request.form.getlist("account_ids", type=int)
    asset_ids = request.form.getlist("asset_ids", type=int)
    publish_now = request.form.get("publish_mode") == "now"
    scheduled_for = (
        datetime.utcnow() if publish_now
        else _parse_schedule(request.form.get("schedule"))
    )

    # Rebuild targets from scratch (drafts only) - simplest correct model.
    for t in list(post.targets):
        db.session.delete(t)
    db.session.flush()

    assets = (
        ClientAsset.query.filter(ClientAsset.id.in_(asset_ids)).all()
        if asset_ids else []
    )
    for account_id in account_ids:
        account = db.session.get(SocialAccount, account_id)
        if account is None:
            continue
        target = SocialPostTarget(
            social_post_id=post.id,
            social_account_id=account.id,
            platform=account.platform,
            post_type=post_type,
            caption=post.base_caption,
            status="draft",
            scheduled_for=scheduled_for,
        )
        db.session.add(target)
        db.session.flush()
        for i, asset in enumerate(assets):
            db.session.add(SocialMediaAsset(
                target_id=target.id,
                source="client_asset",
                client_asset_id=asset.id,
                object_key=asset.object_key,
                mime_type=asset.mime_type,
                role="main",
                sort_order=i,
            ))
    return len(account_ids)


@social_bp.route("/posts", methods=["POST"])
@login_required
def create_post():
    _guard()
    post = SocialPost(status="draft", created_by_id=current_user.id)
    db.session.add(post)
    db.session.flush()
    n = _apply_composer_form(post)
    versioning.snapshot_post(post, edited_by_id=current_user.id)
    audit.record("post_created", post_id=post.id, actor_id=current_user.id,
                 task_id=post.task_id, detail={"targets": n})
    db.session.commit()
    flash("Draft created.", "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/posts/<int:post_id>", methods=["POST"])
@login_required
def update_post(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if post.status not in _EDITABLE_STATUSES:
        flash("This post can no longer be edited.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
    _apply_composer_form(post)
    if post.status == "rejected":
        post.status = "draft"
    versioning.snapshot_post(post, edited_by_id=current_user.id)
    audit.record("post_updated", post_id=post.id, actor_id=current_user.id,
                 task_id=post.task_id)
    db.session.commit()
    flash("Draft updated.", "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/posts/<int:post_id>")
@login_required
def post_detail(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    # Per-target pre-flight problems (provider-validated), for the UI.
    problems = {t.id: publishing.validate_target(t) for t in post.targets}
    return render_template(
        "social/post_detail.html",
        post=post,
        problems=problems,
        can_approve=has_permission(current_user, "approve_tasks")
        or has_permission(current_user, "publish_tasks"),
        to_ist=_to_ist_input,
    )


@social_bp.route("/posts/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if post.status in ("publishing", "published", "partially_published"):
        flash("A published post cannot be deleted.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
    audit.record("post_deleted", post_id=None, actor_id=current_user.id,
                 detail={"post_id": post.id, "title": post.title})
    db.session.delete(post)
    db.session.commit()
    flash("Draft deleted.", "success")
    return redirect(url_for("social.drafts"))


@social_bp.route("/posts/<int:post_id>/submit", methods=["POST"])
@login_required
def submit_post(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if not post.targets:
        flash("Add at least one platform before submitting.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
    post.status = "pending_approval"
    audit.record("submitted_for_approval", post_id=post.id,
                 actor_id=current_user.id, task_id=post.task_id)
    db.session.commit()
    flash("Submitted for approval.", "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/posts/<int:post_id>/approve", methods=["POST"])
@login_required
def approve_post(post_id):
    if not (has_permission(current_user, "approve_tasks")
            or has_permission(current_user, "publish_tasks")):
        abort(403)
    post = SocialPost.query.get_or_404(post_id)
    approval.approve_post(post, current_user.id)
    if post.created_by_id and post.created_by_id != current_user.id:
        audit.notify(
            post.created_by_id, "Social post approved",
            f"Your post “{post.title or 'untitled'}” was approved.",
            link=url_for("social.post_detail", post_id=post.id),
            actor_id=current_user.id,
        )
    db.session.commit()
    flash("Approved.", "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/posts/<int:post_id>/reject", methods=["POST"])
@login_required
def reject_post(post_id):
    if not (has_permission(current_user, "approve_tasks")
            or has_permission(current_user, "publish_tasks")):
        abort(403)
    post = SocialPost.query.get_or_404(post_id)
    reason = (request.form.get("reason") or "").strip()
    post.status = "rejected"
    audit.record("rejected", post_id=post.id, actor_id=current_user.id,
                 task_id=post.task_id, detail={"reason": reason},
                 message=reason or None)
    if post.created_by_id and post.created_by_id != current_user.id:
        audit.notify(
            post.created_by_id, "Social post needs changes",
            (reason or "Your post was sent back for changes."),
            link=url_for("social.post_detail", post_id=post.id),
            actor_id=current_user.id,
        )
    db.session.commit()
    flash("Sent back for changes.", "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/posts/<int:post_id>/schedule", methods=["POST"])
@login_required
def schedule_post(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if post.status != "approved":
        flash("Only an approved post can be scheduled.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))

    publish_now = request.form.get("publish_mode") == "now"
    when = request.form.get("schedule")
    for target in post.targets:
        if publish_now:
            target.scheduled_for = datetime.utcnow()
        elif when:
            target.scheduled_for = _parse_schedule(when)
        elif target.scheduled_for is None:
            target.scheduled_for = datetime.utcnow()

    result = publishing.schedule_post(post, actor_id=current_user.id)
    if result["problems"]:
        flash(
            "Some targets could not be scheduled - check the validation "
            "notes on each platform.", "error",
        )
    else:
        flash(
            f"Scheduled {result['scheduled']} platform(s). Use "
            "“Process queue” (or the cron worker) to publish.",
            "success",
        )
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/api/clients/<int:client_id>/assets")
@login_required
def client_assets_api(client_id):
    _guard()
    assets = (
        ClientAsset.query
        .filter_by(client_id=client_id)
        .order_by(ClientAsset.category, ClientAsset.created_at.desc())
        .all()
    )
    from app.social.media import pipeline

    def _preview(a):
        # A short-lived presigned URL so the composer can show a real
        # thumbnail + live preview. Best-effort: never break the list if a
        # single object can't be signed.
        is_image = (a.mime_type or "").startswith("image")
        url = None
        if is_image:
            try:
                url = pipeline.presigned_url(a.object_key)
            except Exception:  # noqa: BLE001
                url = None
        return {
            "id": a.id, "filename": a.original_filename,
            "mime": a.mime_type or "", "category": a.category,
            "is_image": is_image, "url": url,
        }

    return jsonify(assets=[_preview(a) for a in assets])


@social_bp.route("/calendar")
@login_required
def calendar():
    """A real month grid of scheduled/published targets, bucketed by their
    IST day (the team's clock), colour-coded per platform."""
    import calendar as _cal

    _guard()
    now_ist = datetime.utcnow() + _IST_OFFSET
    year = request.args.get("y", type=int) or now_ist.year
    month = request.args.get("m", type=int) or now_ist.month
    if month < 1:
        year, month = year - 1, 12
    elif month > 12:
        year, month = year + 1, 1

    first_ist = datetime(year, month, 1)
    next_ist = datetime(year + (1 if month == 12 else 0),
                        1 if month == 12 else month + 1, 1)
    # scheduled_for is stored UTC; shift the IST month window back to UTC.
    start_utc, end_utc = first_ist - _IST_OFFSET, next_ist - _IST_OFFSET

    targets = (
        SocialPostTarget.query
        .filter(
            SocialPostTarget.scheduled_for.isnot(None),
            SocialPostTarget.scheduled_for >= start_utc,
            SocialPostTarget.scheduled_for < end_utc,
        )
        .order_by(SocialPostTarget.scheduled_for.asc())
        .all()
    )
    by_day = {}
    for t in targets:
        by_day.setdefault((t.scheduled_for + _IST_OFFSET).day, []).append(t)

    weeks = _cal.Calendar(firstweekday=6).monthdayscalendar(year, month)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    return render_template(
        "social/calendar.html",
        year=year, month=month, month_name=_cal.month_name[month],
        weeks=weeks, by_day=by_day,
        today=(now_ist.day if now_ist.year == year and now_ist.month == month
               else 0),
        prev_y=prev_y, prev_m=prev_m, next_y=next_y, next_m=next_m,
        to_ist=_to_ist_input,
        status=engine_status.engine_status(),
    )


@social_bp.route("/history")
@login_required
def history():
    _guard()
    targets = (
        SocialPostTarget.query
        .order_by(SocialPostTarget.updated_at.desc())
        .limit(100)
        .all()
    )
    return render_template("social/history.html", targets=targets)


@social_bp.route("/accounts/<int:account_id>/disconnect", methods=["POST"])
@login_required
def disconnect_account(account_id):
    if not has_permission(current_user, "connect_social_accounts"):
        abort(403)
    account = SocialAccount.query.get_or_404(account_id)
    AccountManager.disconnect(account)
    audit.record(
        "account_disconnected", account_id=account.id,
        actor_id=current_user.id, commit=True,
    )
    flash(f"Disconnected {account.display_name}.", "success")
    return redirect(url_for("social.index"))
