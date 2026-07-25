"""Social Publishing Engine UI + JSON API.

Registered only when SOCIAL_ENGINE_ENABLED (see app/__init__.py), so the
whole surface is absent unless the engine is turned on. Gated by the
manage_social / connect_social_accounts permissions.
"""

from datetime import datetime, timedelta

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    Client, ClientAsset, PublishJob, SocialAccount, SocialMediaAsset,
    SocialPost, SocialPostTarget,
)
from app.social import status as engine_status
from app.social.registry import registry
from app.social.services import (
    approval, audit, publishing, recovery, scheduling, versioning,
)
from app.social.services.accounts import AccountManager
from app.utils.permissions import has_permission
from app.utils.social_platforms import PLATFORMS, label as platform_label


social_bp = Blueprint("social", __name__, url_prefix="/social")

# The composer's datetime-local inputs are entered in IST (the team's clock);
# the engine stores/compares in UTC. Convert on the boundary.
_IST_OFFSET = timedelta(hours=5, minutes=30)

#: Statuses whose content is still fully editable.
_EDITABLE_STATUSES = ("draft", "pending_approval", "rejected")


def _guard():
    if not has_permission(current_user, "manage_social"):
        abort(403)


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
    return render_template(
        "social/index.html",
        accounts=accounts,
        platforms=PLATFORMS,
        available=registry.keys(),
        status=engine_status.engine_status(),
    )


@social_bp.route("/queue")
@login_required
def queue():
    """Failure Recovery: the dead-letter / failed jobs an operator can
    requeue."""
    _guard()
    jobs = recovery.dead_jobs(limit=200)
    return render_template(
        "social/queue.html",
        jobs=jobs,
        status=engine_status.engine_status(),
    )


@social_bp.route("/jobs/<int:job_id>/requeue", methods=["POST"])
@login_required
def requeue_job(job_id):
    _guard()
    job = PublishJob.query.get_or_404(job_id)
    if recovery.requeue_job(job, actor_id=current_user.id, commit=True):
        flash("Job requeued.", "success")
    else:
        flash("That job is not in a recoverable state.", "error")
    return redirect(url_for("social.queue"))


@social_bp.route("/jobs/requeue-all", methods=["POST"])
@login_required
def requeue_all():
    _guard()
    count = recovery.requeue_all_dead(actor_id=current_user.id)
    flash(f"Requeued {count} job(s).", "success")
    return redirect(url_for("social.queue"))


@social_bp.route("/queue/process", methods=["POST"])
@login_required
def process_queue():
    """Kick the scheduler + worker once. In production these run on cron;
    this button drives the full loop on demand (and is how the simulation
    workflow completes locally)."""
    _guard()
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
    return jsonify(assets=[
        {"id": a.id, "filename": a.original_filename,
         "mime": a.mime_type or "", "category": a.category}
        for a in assets
    ])


@social_bp.route("/calendar")
@login_required
def calendar():
    _guard()
    posts = (
        SocialPost.query
        .order_by(SocialPost.created_at.desc())
        .limit(100)
        .all()
    )
    return render_template("social/calendar.html", posts=posts)


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
