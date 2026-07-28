"""Social Publishing Engine UI + JSON API.

Registered only when SOCIAL_ENGINE_ENABLED (see app/__init__.py), so the
whole surface is absent unless the engine is turned on. Gated by
can_use_social / can_connect_social_accounts - both admin roles, plus
anyone holding the matching manage_social / connect_social_accounts
permission.
"""

from datetime import datetime, timedelta

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    Client, ClientAsset, ContentVersion, PublishJob, PublishResult,
    SocialAccount, SocialAuditLog, SocialComment, SocialMediaAsset, SocialPost,
    SocialPostTarget, Task, TaskFile,
)
from app.social.dto import TokenBundle
from app.social import status as engine_status
from app.social.registry import registry
from app.social.services import (
    approval, audit, lifecycle, publishing, recovery, scheduling, task_link,
    versioning,
)
from app.social.services import engage as engage_svc
from app.social.services.accounts import AccountManager
from app.utils.permissions import (
    can_connect_social_accounts, can_manage_social_engine, can_publish,
    can_use_social, has_permission,
)
from app.utils.social_platforms import (
    PLATFORMS, label as platform_label, parse_platforms,
)


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
    if not can_use_social(current_user):
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


def _simulated_keys():
    """Platforms currently served by the SimulationProvider rather than a
    real adapter.

    Surfaced in the UI (a "Demo" badge) because the two are otherwise
    indistinguishable once a channel is connected: the connect flow, the
    composer, the queue and the published state all behave identically, and
    a post to a simulated channel reports success without anything reaching
    the platform. Someone has to be told which is which, and the page where
    channels are connected is where they'll look."""
    return {k for k, p in registry.all().items()
            if getattr(p, "is_simulation", False)}


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


# ======================================================================
# Social Studio: client-first context
# ======================================================================
# The Studio is organised around the CLIENT: a persistent switcher scopes
# every view (dashboard, calendar, queue, drafts, published, analytics) to
# one client, matching how the agency actually works ("I'm doing X's socials
# today"). The active client rides in the query string (?client=<id>) so it
# is shareable, Turbo-friendly, and needs no server-side session state.

def _studio_clients():
    """Active clients for the switcher - parents before their sub-clients."""
    return Client.ordered_with_sub_clients(status="active")


def _client_arg():
    """Validated ACTIVE-client id from ?client=, or None ("All clients").
    Active-only so routes and the context bar agree on what's selected."""
    sel_id = request.args.get("client", type=int)
    if sel_id is None:
        return None
    exists = Client.query.filter_by(id=sel_id, status="active").first()
    return sel_id if exists else None


def _scope_posts(query, client_id):
    """Filter a SocialPost query to one client (no-op when None)."""
    return query.filter(SocialPost.client_id == client_id) if client_id else query


def _scope_targets(query, client_id):
    """Filter a SocialPostTarget query to one client, via its post."""
    if not client_id:
        return query
    return (query.join(SocialPost, SocialPostTarget.social_post_id == SocialPost.id)
            .filter(SocialPost.client_id == client_id))


def _channel_client_ok(account_client_id, post_client_id):
    """Client-safety rule: a channel bound to a client may only be used for
    THAT client's posts. Agency-wide channels (no binding) and posts with no
    client are always allowed. This is the single source of truth used by
    both the server guard and (mirrored) the composer's channel filter."""
    return not (account_client_id and post_client_id
                and account_client_id != post_client_id)


def _linkable_targets(client_id, limit=40):
    """Published posts a story can be pointed at.

    Feed posts only - a story linking to a story is meaningless, and a
    post with no permalink gives the person adding the sticker nothing to
    open. Scoped to the client so one client's story can never advertise
    another's post.
    """
    q = (
        SocialPostTarget.query
        .filter(SocialPostTarget.status == "published",
                SocialPostTarget.post_type != "story",
                SocialPostTarget.permalink.isnot(None))
        .order_by(SocialPostTarget.updated_at.desc())
    )
    return _scope_targets(q, client_id).limit(limit).all()


def _linked_target_id(raw, client_id):
    """Validate a story's chosen link target, server-side.

    The composer only offers this client's published posts, but the form
    field is just an id - re-check it here rather than trusting it.
    """
    try:
        target_id = int(raw or 0)
    except (TypeError, ValueError):
        return None
    if not target_id:
        return None
    return target_id if any(
        t.id == target_id for t in _linkable_targets(client_id)
    ) else None


@social_bp.context_processor
def _inject_studio_context():
    """Give every Studio template the switcher list + the active client, so
    the context bar and sidebar work without per-route wiring. Blueprint-
    scoped and cheap; degrades to SAFE DEFAULTS (never {}), so a failure here
    can never leave `studio_clients` undefined and crash the switcher."""
    empty = {"studio_clients": [], "selected_client": None,
             "selected_client_id": None, "pending_approval_total": 0,
             "engage_open_total": 0}
    if not current_user.is_authenticated:
        return empty
    try:
        clients = _studio_clients()
        sel_id = request.args.get("client", type=int)
        selected = next((c for c in clients if c.id == sel_id), None)
        # On a single-post page (no ?client), fall back to that post's client
        # so the switcher + sidebar keep the client context after create/save.
        if selected is None and request.view_args \
                and request.view_args.get("post_id"):
            post = db.session.get(SocialPost, request.view_args["post_id"])
            if post and post.client_id:
                selected = next(
                    (c for c in clients if c.id == post.client_id), None)
        pending_total = _scope_posts(
            SocialPost.query.filter_by(status="pending_approval"),
            selected.id if selected else None).count()
        engage_open = _scope_comments(
            SocialComment.query.filter(SocialComment.is_ours.is_(False),
                                       SocialComment.status == "open"),
            selected.id if selected else None).count()
        return {
            "studio_clients": clients,
            "selected_client": selected,
            "selected_client_id": selected.id if selected else None,
            "pending_approval_total": pending_total,
            "engage_open_total": engage_open,
        }
    except Exception:  # noqa: BLE001
        return empty


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


def _to_ist_display(dt):
    """Naive UTC datetime -> human IST string, e.g. '24 Jul 2026, 14:30'."""
    if not dt:
        return "—"
    return (dt + _IST_OFFSET).strftime("%d %b %Y, %H:%M")


def _asset_preview(a):
    """A ClientAsset -> the dict the composer/library render: filename,
    category, and a short-lived presigned thumbnail URL for images. Best-
    effort: a single unsignable object never breaks the list."""
    from app.social.media import pipeline
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


def _post_thumbnail(post):
    """The first image thumbnail across a post's targets, so reviewers see the
    creative on the approval card without opening the post."""
    from app.social.media import pipeline
    for t in post.targets:
        for m in t.media:
            if (m.mime_type or "").startswith("image") and m.object_key:
                try:
                    return pipeline.presigned_url(m.object_key)
                except Exception:  # noqa: BLE001
                    return None
    return None


def _task_file_preview(tf):
    """A TaskFile (a deliverable the assignee produced) -> the composer's
    media dict, with a presigned thumbnail for images."""
    from app.social.media import pipeline
    is_image = (tf.mime_type or "").startswith("image")
    url = None
    if is_image and tf.object_key:
        try:
            url = pipeline.presigned_url(tf.object_key)
        except Exception:  # noqa: BLE001
            url = None
    return {"id": tf.id, "filename": tf.original_filename,
            "mime": tf.mime_type or "", "is_image": is_image, "url": url}


def _task_deliverable_files(task):
    """The creative for a task: its final/submission files (not references)."""
    return (
        TaskFile.query
        .filter(TaskFile.task_id == task.id,
                TaskFile.folder_type.in_(["final", "submission"]))
        .order_by(TaskFile.folder_type.desc(), TaskFile.id.asc())
        .all()
    )


def _suggested_accounts_for_task(task):
    """Pre-select the client's channels that match the task's platforms (and
    are client-safe), so composing from a task is one click."""
    plats = set(parse_platforms(task.social_platforms))
    if not plats:
        return []
    return [
        a.id for a in AccountManager.list_accounts()
        if a.platform in plats and _channel_client_ok(a.client_id, task.client_id)
    ]


def create_draft_from_task(task, actor_id=None):
    """Server-side handoff: turn an approved social task into a Social Studio
    DRAFT the social team finalizes. Pre-fills the deliverable files as media
    and the client's bound channels (matching the task's platforms) as targets.
    Caption is left EMPTY on purpose - a task has no caption field yet; the
    composer shows the task description as reference instead.

    Returns {"post": SocialPost, "n_targets": int, "no_channels": bool}. When
    the client has no channels bound, the draft is still created (no targets)
    so the handoff isn't lost - the caller prompts the user to bind channels.
    """
    files = _task_deliverable_files(task)

    def _is_video(f):
        mt = (f.mime_type or "").lower()
        name = (f.original_filename or f.object_key or "").lower()
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        # Browsers often upload video as application/octet-stream, so fall back
        # to the extension rather than mislabelling a video as an image.
        return mt.startswith("video") or ext in (
            "mp4", "mov", "webm", "avi", "mkv", "m4v")

    if not files:
        post_type = "text"
    elif any(_is_video(f) for f in files):
        post_type = "video"
    elif len(files) > 1:
        post_type = "carousel"
    else:
        post_type = "image"

    post = SocialPost(
        task_id=task.id, client_id=task.client_id,
        title=task.title, base_caption="", status="draft",
        created_by_id=actor_id)
    db.session.add(post)
    db.session.flush()

    media_items = [("task_file", tf) for tf in files]

    def _add_media(target_id):
        for i, (source, obj) in enumerate(media_items):
            db.session.add(SocialMediaAsset(
                target_id=target_id, source=source, object_key=obj.object_key,
                mime_type=obj.mime_type, task_file_id=obj.id, role="main",
                sort_order=i))

    n_targets = 0
    for account_id in _suggested_accounts_for_task(task):
        account = db.session.get(SocialAccount, account_id)
        if account is None or not _channel_client_ok(
                account.client_id, post.client_id):
            continue
        t = SocialPostTarget(
            social_post_id=post.id, social_account_id=account.id,
            platform=account.platform, post_type=post_type, status="draft")
        db.session.add(t)
        db.session.flush()
        _add_media(t.id)
        n_targets += 1

    versioning.snapshot_post(post, edited_by_id=actor_id)
    audit.record("post_created_from_task", post_id=post.id, actor_id=actor_id,
                 task_id=task.id, detail={"targets": n_targets})
    return {"post": post, "n_targets": n_targets, "no_channels": n_targets == 0}


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
            "supports_first_comment": bool(caps and caps.supports_first_comment),
            "simulation": getattr(provider, "is_simulation", False),
        }
    return out


def _hashtag_sets(client_id=None):
    """Saved hashtag sets available in the composer: this client's sets plus
    the agency-wide (client-less) ones."""
    from app.models import SocialHashtagSet
    q = SocialHashtagSet.query
    if client_id:
        q = q.filter(db.or_(SocialHashtagSet.client_id == client_id,
                            SocialHashtagSet.client_id.is_(None)))
    else:
        q = q.filter(SocialHashtagSet.client_id.is_(None))
    return q.order_by(SocialHashtagSet.name).all()


@social_bp.route("/")
@login_required
def index():
    _guard()
    cid = _client_arg()
    accounts = AccountManager.list_accounts(include_revoked=False)
    now = datetime.utcnow()
    start_today = datetime(now.year, now.month, now.day)

    upcoming = (
        _scope_targets(SocialPostTarget.query, cid)
        .filter(
            SocialPostTarget.status.in_(["scheduled", "approved"]),
            SocialPostTarget.scheduled_for.isnot(None),
            SocialPostTarget.scheduled_for >= now,
        )
        .order_by(SocialPostTarget.scheduled_for.asc())
        .limit(6)
        .all()
    )
    recent_q = SocialAuditLog.query
    if cid:
        client_post_ids = [
            p.id for p in SocialPost.query
            .with_entities(SocialPost.id).filter_by(client_id=cid).all()
        ]
        recent_q = recent_q.filter(SocialAuditLog.post_id.in_(client_post_ids or [-1]))
    recent = recent_q.order_by(SocialAuditLog.created_at.desc()).limit(7).all()

    pub_q = PublishResult.query.filter(PublishResult.created_at >= start_today)
    if cid:
        pub_q = (pub_q.join(
            SocialPostTarget, PublishResult.target_id == SocialPostTarget.id)
            .join(SocialPost, SocialPostTarget.social_post_id == SocialPost.id)
            .filter(SocialPost.client_id == cid))
    published_today = pub_q.count()

    drafts_count = _scope_posts(
        SocialPost.query.filter_by(status="draft"), cid).count()
    pending_approval = _scope_posts(
        SocialPost.query.filter_by(status="pending_approval"), cid).count()

    return render_template(
        "social/index.html",
        accounts=accounts,
        groups=_grouped_accounts(accounts),
        platforms=PLATFORMS,
        available=registry.keys(),
        connectable=_connectable_keys(),
        simulated=_simulated_keys(),
        status=engine_status.engine_status(),
        upcoming=upcoming,
        recent=recent,
        published_today=published_today,
        drafts_count=drafts_count,
        pending_approval=pending_approval,
        can_approve=can_publish(current_user),
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
        simulated=_simulated_keys(),
        clients=_studio_clients(),
        has_facebook=any(a.platform == "facebook" for a in accts),
        has_instagram=any(a.platform == "instagram" for a in accts),
        status=engine_status.engine_status(),
    )


@social_bp.route("/accounts/<int:account_id>/client", methods=["POST"])
@login_required
def set_account_client(account_id):
    """Bind a channel to a client (or 'Agency-wide' = no client). This is
    what makes publishing client-safe: the composer only offers a client's
    own channels, and the server refuses a cross-client target."""
    if not can_connect_social_accounts(current_user):
        abort(403)
    account = SocialAccount.query.get_or_404(account_id)
    client_id = request.form.get("client_id", type=int)
    if client_id and not Client.query.filter_by(
            id=client_id, status="active").first():
        client_id = None
    account.client_id = client_id or None
    # An Instagram account belongs to the same brand as its parent Page -
    # keep any linked IG accounts in sync so they can't drift to another client.
    if account.platform == "facebook":
        page_id = (account.meta or {}).get("page_id") or account.external_id
        for ig in SocialAccount.query.filter_by(platform="instagram").all():
            if (ig.meta or {}).get("page_id") == page_id:
                ig.client_id = account.client_id
    audit.record("account_client_set", account_id=account.id,
                 actor_id=current_user.id,
                 detail={"client_id": account.client_id})
    db.session.commit()
    flash("Channel assignment updated.", "success")
    return redirect(url_for("social.accounts"))


@social_bp.route("/instagram/discover", methods=["POST"])
@login_required
def discover_instagram():
    """Refresh: discover IG Business accounts linked to ALREADY-connected
    Facebook Pages, using each Page's stored token. No OAuth - this is the
    Buffer/Meta Business Suite behaviour where Instagram rides on Facebook."""
    if not can_connect_social_accounts(current_user):
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
    from app.social.services import analytics_report
    from app.utils import periods

    cid = _client_arg()
    period = periods.resolve_period(request.args, allow_all=True,
                                    default="30d")

    return render_template(
        "social/analytics.html",
        accounts=AccountManager.list_accounts(include_revoked=False),
        status=engine_status.engine_status(),
        period=period,
        report=analytics_report.build_report(period, cid),
        METRICS=analytics_report.METRICS,
        to_ist_display=_to_ist_display,
    )


@social_bp.route("/analytics/sync", methods=["POST"])
@login_required
def analytics_sync():
    """Pull the latest insights on demand.

    The cron does this on a schedule, but a person looking at an empty
    screen needs a way to ask now - and, more importantly, to be told when
    the answer is "the platform refused" rather than "there is nothing".
    """
    _guard()
    from app.social.services import analytics as analytics_svc

    report = analytics_svc.sync_recent()

    if report.get("failed"):
        flash(
            f"Checked {report['checked']} post(s); {report['failed']} could "
            "not be read: " + "; ".join(report.get("errors") or []) + ".",
            "error")
    if report.get("synced"):
        flash(f"Updated insights for {report['synced']} post(s).", "success")
    elif not report.get("failed"):
        flash(
            "Nothing to update — insights are fetched for posts published "
            "through the Studio." if not report.get("checked")
            else f"Checked {report['checked']} post(s) — the platforms "
                 "reported no figures yet. Insights can take a few hours to "
                 "appear on a new post.",
            "info")

    return redirect(url_for("social.analytics", client=_client_arg()))


# ----------------------------------------------------------------------
# Approvals inbox: a single queue of posts awaiting sign-off, so managers
# don't have to open each post to act. Approve / send-back, single or bulk.
# ----------------------------------------------------------------------

def _can_approve():
    # Approving a post is the client-facing sign-off, so it is the
    # publish permission alone. It used to accept `approve_tasks` too,
    # which is now the craft gate on the task board - a senior editor
    # reviewing a cut has no business releasing a client's post.
    return can_publish(current_user)


def _notify_submit(post):
    """On submit-for-approval, ping the client's manager (the natural
    reviewer) so a pending post isn't only visible as a sidebar badge."""
    mgr_id = post.client.assigned_manager_id if post.client else None
    if mgr_id and mgr_id != current_user.id:
        audit.notify(
            mgr_id, "Social post needs approval",
            f"“{post.title or 'A post'}” is awaiting your approval.",
            link=url_for("social.post_detail", post_id=post.id),
            actor_id=current_user.id)


@social_bp.route("/approvals")
@login_required
def approvals():
    _guard()
    cid = _client_arg()
    posts = (
        _scope_posts(
            SocialPost.query.filter_by(status="pending_approval"), cid)
        .order_by(SocialPost.updated_at.asc())
        .limit(100)
        .all()
    )
    recently = (
        _scope_posts(
            SocialPost.query.filter(SocialPost.status.in_(
                ["approved", "scheduled"])), cid)
        .order_by(SocialPost.approved_at.desc().nullslast())
        .limit(8)
        .all()
    )
    return render_template(
        "social/approvals.html",
        posts=posts, recently=recently,
        thumbs={p.id: _post_thumbnail(p) for p in posts},
        can_approve=_can_approve(),
        to_ist=_to_ist_input,
    )


@social_bp.route("/approvals/bulk", methods=["POST"])
@login_required
def approvals_bulk():
    if not _can_approve():
        abort(403)
    ids = request.form.getlist("post_ids", type=int)
    action = request.form.get("action")
    if action not in ("approve", "reject") or not ids:
        flash("Select at least one post and an action.", "error")
        return redirect(url_for("social.approvals"))
    posts = SocialPost.query.filter(
        SocialPost.id.in_(ids),
        SocialPost.status == "pending_approval",
    ).all()
    reason = (request.form.get("reason") or "").strip()
    n = 0
    for post in posts:
        if action == "approve":
            approval.approve_post(post, current_user.id)
        else:
            post.status = "rejected"
            audit.record("rejected", post_id=post.id, actor_id=current_user.id,
                         task_id=post.task_id, detail={"reason": reason},
                         message=reason or None)
            if post.created_by_id and post.created_by_id != current_user.id:
                audit.notify(
                    post.created_by_id, "Social post needs changes",
                    reason or "Your post was sent back for changes.",
                    link=url_for("social.post_detail", post_id=post.id),
                    actor_id=current_user.id)
        n += 1
    db.session.commit()
    verb = "Approved" if action == "approve" else "Sent back"
    flash(f"{verb} {n} post(s).", "success")
    return redirect(url_for(
        "social.approvals",
        client=request.form.get("client", type=int) or None))


# ----------------------------------------------------------------------
# Media Library: a client's brand assets in one browsable place, reusable
# from the composer. Per-client (assets belong to a client).
# ----------------------------------------------------------------------

@social_bp.route("/library")
@login_required
def library():
    _guard()
    cid = _client_arg()
    assets = []
    if cid:
        rows = (
            ClientAsset.query.filter_by(client_id=cid)
            .order_by(ClientAsset.category, ClientAsset.created_at.desc())
            .all()
        )
        assets = [_asset_preview(a) for a in rows]
    # group by category for a tidy library
    by_category = {}
    for a in assets:
        by_category.setdefault(a["category"] or "Uncategorised", []).append(a)
    return render_template(
        "social/library.html",
        by_category=by_category,
        asset_count=len(assets),
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

    from sqlalchemy.orm import joinedload
    cid = _client_arg()
    # Eager-load target + post (avoids an N+1 across up to 400 jobs) and push
    # the client filter into the query so LIMIT applies to the client's jobs,
    # not the global 400 newest.
    query = PublishJob.query.options(
        joinedload(PublishJob.target).joinedload(SocialPostTarget.post))
    if cid:
        query = (
            query.join(SocialPostTarget,
                       PublishJob.target_id == SocialPostTarget.id)
            .join(SocialPost,
                  SocialPostTarget.social_post_id == SocialPost.id)
            .filter(SocialPost.client_id == cid))
    jobs = query.order_by(PublishJob.updated_at.desc()).limit(400).all()

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


@social_bp.route("/targets/<int:target_id>/retry", methods=["POST"])
@login_required
def retry_target(target_id):
    """Re-run a single FAILED platform for a post. Available to publishers
    (not just engine admins) so a one-platform failure on a multi-platform
    post can be fixed without touching the whole Queue."""
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)
    if target.status != "failed":
        flash("Only a failed platform can be retried.", "error")
        return redirect(url_for("social.post_detail",
                                post_id=target.social_post_id))
    job = target.job
    if job is not None and recovery.requeue_job(
            job, actor_id=current_user.id, commit=True):
        flash(f"Retrying {target.platform} — it will publish on the next "
              "worker run.", "success")
    else:
        publishing.publish_target_now(target, actor_id=current_user.id)
        flash(f"Re-queued {target.platform} for publishing.", "success")
    # Roll the parent back from failed so the UI reflects the in-flight retry.
    post = target.post
    if post and post.status == "failed":
        post.status = "publishing"
        db.session.commit()
    return redirect(url_for("social.post_detail",
                            post_id=target.social_post_id))


@social_bp.route("/posts/<int:post_id>/duplicate", methods=["POST"])
@login_required
def duplicate_post(post_id):
    """Deep-copy a post (targets + media) into a fresh draft. Agencies reuse
    creative across cadences/clients; this saves rebuilding in the composer."""
    _guard()
    src = SocialPost.query.get_or_404(post_id)
    dup = SocialPost(
        status="draft", created_by_id=current_user.id, client_id=src.client_id,
        title=((src.title or "Untitled") + " (copy)"),
        base_caption=src.base_caption)
    db.session.add(dup)
    db.session.flush()
    for t in src.targets:
        nt = SocialPostTarget(
            social_post_id=dup.id, social_account_id=t.social_account_id,
            platform=t.platform, post_type=t.post_type, caption=t.caption,
            hashtags=t.hashtags, first_comment=t.first_comment,
            status="draft", scheduled_for=None)
        db.session.add(nt)
        db.session.flush()
        for m in t.media:
            db.session.add(SocialMediaAsset(
                target_id=nt.id, source=m.source, task_file_id=m.task_file_id,
                client_asset_id=m.client_asset_id, object_key=m.object_key,
                role=m.role, sort_order=m.sort_order, alt_text=m.alt_text,
                mime_type=m.mime_type))
    versioning.snapshot_post(dup, edited_by_id=current_user.id)
    audit.record("post_duplicated", post_id=dup.id, actor_id=current_user.id,
                 detail={"from_post": src.id})
    db.session.commit()
    flash("Duplicated as a new draft — edit and go.", "success")
    return redirect(url_for("social.edit_post", post_id=dup.id))


def _detach_post_history(post):
    """Detach audit + version rows before a post is deleted, so their FKs
    don't block the delete. Audit rows are PRESERVED (post_id/target_id
    nulled) so the trail survives; content versions are snapshots of this
    exact post, so they're removed with it. Without this, deleting any post
    that has history (every post has a 'post_created' log) would 500."""
    tids = [t.id for t in post.targets] or [-1]
    SocialAuditLog.query.filter(
        db.or_(SocialAuditLog.post_id == post.id,
               SocialAuditLog.target_id.in_(tids))
    ).update({"post_id": None, "target_id": None}, synchronize_session=False)
    ContentVersion.query.filter(
        db.or_(ContentVersion.social_post_id == post.id,
               ContentVersion.target_id.in_(tids))
    ).delete(synchronize_session=False)
    # PublishResult has no cascade; clear it too so the helper is complete
    # (the delete routes block published posts, but this keeps it robust).
    PublishResult.query.filter(
        PublishResult.target_id.in_(tids)).delete(synchronize_session=False)
    # Analytics snapshots and Engage comments also FK to the targets with no
    # cascade. A 'removed' post is deletable and its targets were live, so both
    # may exist - without clearing them the delete 500s on an FK violation.
    from app.models import SocialAnalyticsSnapshot, SocialComment
    SocialAnalyticsSnapshot.query.filter(
        SocialAnalyticsSnapshot.target_id.in_(tids)
    ).delete(synchronize_session=False)
    SocialComment.query.filter(
        SocialComment.target_id.in_(tids)
    ).delete(synchronize_session=False)


def _cancel_pending_jobs(post):
    """Delete any not-yet-run jobs for a post's targets (safe: only 'queued',
    never a job already claimed/publishing/succeeded)."""
    for t in post.targets:
        job = t.job
        if job is not None and job.state == "queued":
            db.session.delete(job)


@social_bp.route("/posts/<int:post_id>/reopen", methods=["POST"])
@login_required
def reopen_post(post_id):
    """Send an approved/scheduled post back to draft to fix it. Cancels any
    pending (not-yet-run) jobs so nothing publishes while it's being edited."""
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if post.status not in ("approved", "scheduled"):
        flash("Only an approved or scheduled post can be reopened.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
    was_scheduled = post.status == "scheduled"
    _cancel_pending_jobs(post)
    for t in post.targets:
        if t.status in ("approved", "scheduled"):
            t.status = "draft"
            t.scheduled_for = None
    post.status = "draft"
    post.approved_by_id = None
    post.approved_at = None
    audit.record("post_reopened", post_id=post.id, actor_id=current_user.id,
                 task_id=post.task_id)
    # If this was a scheduled, task-linked post, walk the task back too.
    if was_scheduled and post.task_id:
        from app.social.services import task_link
        task_link.mark_task_unscheduled(post, actor_id=current_user.id)
    db.session.commit()
    flash("Reopened as a draft. Re-submit it for approval when ready.",
          "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/posts/<int:post_id>/reschedule", methods=["POST"])
@login_required
def reschedule_post(post_id):
    """Move a scheduled post to a new time without reopening it."""
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if post.status != "scheduled":
        flash("Only a scheduled post can be rescheduled.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
    when = _parse_schedule(request.form.get("schedule"))
    if not when:
        flash("Pick a valid date and time.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
    for t in post.targets:
        t.scheduled_for = when
        if t.job is not None and t.job.state == "queued":
            t.job.next_run_at = when
    audit.record("post_rescheduled", post_id=post.id, actor_id=current_user.id,
                 task_id=post.task_id, detail={"when": when.isoformat()})
    db.session.commit()
    flash(f"Rescheduled to {_to_ist_display(when)} IST.", "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/targets/<int:target_id>/move", methods=["POST"])
@login_required
def move_target(target_id):
    """Drag-to-reschedule from the calendar: move one platform's post to a new
    day, keeping its time. Only for scheduled/approved (not yet published)."""
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)
    if target.status not in ("scheduled", "approved"):
        return jsonify(error="Only a scheduled post can be moved."), 400
    try:
        new_day = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Bad date."), 400
    # keep the existing IST time-of-day, just change the date
    old_ist = (target.scheduled_for + _IST_OFFSET) if target.scheduled_for \
        else new_day.replace(hour=10)
    new_ist = new_day.replace(hour=old_ist.hour, minute=old_ist.minute)
    target.scheduled_for = new_ist - _IST_OFFSET
    if target.job is not None and target.job.state == "queued":
        target.job.next_run_at = target.scheduled_for
    audit.record("target_moved", target_id=target.id,
                 post_id=target.social_post_id, actor_id=current_user.id,
                 detail={"date": request.form.get("date")})
    db.session.commit()
    return jsonify(ok=True, when=_to_ist_display(target.scheduled_for))


def _back_to(post_id):
    """Where a remove should return to.

    Taking a post down is something you do from the Published list as
    often as from the post itself, and bouncing to the post page each time
    loses the list you were working through. The caller names the screen
    with a token rather than a URL - a raw `next=` parameter here would be
    an open redirect on a button that deletes things.
    """
    if request.form.get("back") == "history":
        return redirect(url_for("social.history", client=_client_arg()))

    return redirect(url_for("social.post_detail", post_id=post_id))


@social_bp.route("/targets/<int:target_id>/remove", methods=["POST"])
@login_required
def remove_target(target_id):
    """Delete a published post/story from the platform (Facebook via API;
    Instagram must be removed manually) and mark it removed in the Studio,
    keeping the record for history."""
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)
    if target.status != "published":
        flash("Only a published post can be removed.", "error")
        return _back_to(target.social_post_id)
    note = lifecycle.remove_target(target, actor_id=current_user.id)
    lifecycle._rollup_removed(target.post)
    db.session.commit()
    flash(note or f"Removed the {platform_label(target.platform)} post.",
          "info" if note else "success")
    return _back_to(target.social_post_id)


@social_bp.route("/posts/<int:post_id>/remove", methods=["POST"])
@login_required
def remove_post(post_id):
    """Remove every published platform of a post at once."""
    _guard()
    post = SocialPost.query.get_or_404(post_id)

    live = [t for t in post.targets if t.status == "published"]

    if not live:
        flash("Nothing to remove - none of this post's platforms are live.",
              "error")
        return _back_to(post.id)

    notes = []
    for target in live:
        n = lifecycle.remove_target(target, actor_id=current_user.id)
        if n:
            notes.append(n)

    lifecycle._rollup_removed(post)
    db.session.commit()
    flash(" ".join(notes)
          or f"Removed from {len(live)} platform(s).",
          "info" if notes else "success")
    return _back_to(post.id)


@social_bp.route("/targets/<int:target_id>/drop", methods=["POST"])
@login_required
def drop_target(target_id):
    """Take one platform off a post that has not published on it.

    The way out of "Facebook went live, Instagram can never accept this".
    Without it the post is stuck forever: the blocked target keeps the post
    from settling, and the only alternatives were deleting a post that is
    already live on another platform, or leaving it wrong.

    Only for targets that never published - a live post is removed through
    remove_target, which also deletes it on the platform.
    """
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)

    if target.status in ("published", "removed"):
        flash("That platform is already live — use Remove instead.", "error")
        return _back_to(target.social_post_id)

    post_id = target.social_post_id
    label = platform_label(target.platform)

    SocialComment.query.filter_by(target_id=target.id).delete(
        synchronize_session=False)
    PublishJob.query.filter_by(target_id=target.id).delete(
        synchronize_session=False)
    SocialAuditLog.query.filter_by(target_id=target.id).update(
        {"target_id": None}, synchronize_session=False)

    db.session.delete(target)
    db.session.flush()

    post = db.session.get(SocialPost, post_id)
    if post is not None:
        # Removing the blocker can settle the post - a lone published
        # target now means published, not "partially".
        from app.social.queue.worker import _maybe_finalize_post
        remaining = post.targets
        if remaining:
            _maybe_finalize_post(remaining[0])

    audit.record("target_dropped", post_id=post_id, actor_id=current_user.id,
                 detail={"platform": target.platform})
    db.session.commit()
    flash(f"Removed {label} from this post.", "success")
    return _back_to(post_id)


@social_bp.route("/targets/<int:target_id>/story-link-done", methods=["POST"])
@login_required
def story_link_done(target_id):
    """Tick off the one step of a linked story the API cannot do.

    A story asked to open a feed post publishes as a plain story - Meta
    exposes no sticker parameter - so someone adds the post sticker in the
    Instagram app and confirms it here. Recorded with who and when, so
    "did anyone actually do it?" has an answer.
    """
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)

    if not target.links_to_post:
        flash("That story wasn't set to open a post.", "error")
        return _back_to(target.social_post_id)

    if target.story_link_done_at:
        flash("Already marked done.", "info")
        return _back_to(target.social_post_id)

    target.story_link_done_at = datetime.utcnow()
    target.story_link_done_by_id = current_user.id
    audit.record("story_link_completed", post_id=target.social_post_id,
                 target_id=target.id, actor_id=current_user.id,
                 detail={"linked_target_id": target.story_link_target_id})
    db.session.commit()
    flash("Marked done — the story now points at the post.", "success")
    return _back_to(target.social_post_id)


@social_bp.route("/targets/<int:target_id>/story-link-undo", methods=["POST"])
@login_required
def story_link_undo(target_id):
    """Reopen a follow-up ticked off by mistake."""
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)
    target.story_link_done_at = None
    target.story_link_done_by_id = None
    audit.record("story_link_reopened", post_id=target.social_post_id,
                 target_id=target.id, actor_id=current_user.id)
    db.session.commit()
    flash("Reopened — the sticker still needs adding.", "info")
    return _back_to(target.social_post_id)


@social_bp.route("/history/sync", methods=["POST"])
@login_required
def sync_history():
    """Detect posts deleted directly on the platform and flag them removed."""
    _guard()
    cid = _client_arg()
    n = lifecycle.sync_published(cid)
    flash(
        f"Checked with the platforms — {n} post(s) had been removed there."
        if n else "All published posts are still live on their platforms.",
        "info")
    return redirect(url_for("social.history", client=cid))


@social_bp.route("/queue/process", methods=["POST"])
@login_required
def process_queue():
    """Kick the scheduler + worker once. In production these run on cron;
    this button drives the full loop on demand (and is how the simulation
    workflow completes locally)."""
    _engine_guard()
    from app.social.queue import worker
    enq = scheduling.enqueue_due()

    # A manual kick advances jobs that are due now or waiting on a short async
    # poll (e.g. an Instagram container, next_run_at ~30s out) so a
    # start->poll->publish chain completes in one click. It deliberately does
    # NOT pull forward jobs a limiter/backoff pushed minutes out - forcing a
    # rate-limited or failing job to re-hit the platform immediately is exactly
    # what the deferral exists to prevent. In production the cron worker
    # advances everything naturally across its runs.
    processed = 0
    for _ in range(5):
        horizon = datetime.utcnow() + timedelta(seconds=60)
        PublishJob.query.filter(
            PublishJob.state == "queued",
            PublishJob.next_run_at <= horizon,
        ).update({PublishJob.next_run_at: datetime.utcnow()})
        db.session.commit()
        drained = worker.drain()
        processed += drained["processed"]
        if drained["claimed"] == 0:
            break

    # Said "Enqueued 0 due · processed 0 job(s)" in green, which reads as a
    # success while telling the reader nothing happened - and in engine
    # vocabulary they have no reason to know.
    if processed:
        flash(f"Published {processed} queued item(s).", "success")
    elif enq["enqueued"]:
        flash(f"{enq['enqueued']} item(s) picked up — they publish in the "
              "next moment or two.", "info")
    else:
        flash("Nothing was due. Anything scheduled for later goes out "
              "automatically at its time.", "info")
    return redirect(request.referrer or url_for("social.queue"))


# ======================================================================
# Content Composer + draft / approval / schedule workflow
# ======================================================================

@social_bp.route("/drafts")
@login_required
def drafts():
    _guard()
    allowed = ["draft", "pending_approval", "approved", "scheduled",
               "rejected"]
    status_f = request.args.get("status")
    status_f = status_f if status_f in allowed else ""
    q = _scope_posts(SocialPost.query, _client_arg())
    q = q.filter(SocialPost.status == status_f) if status_f \
        else q.filter(SocialPost.status.in_(allowed))
    posts = q.order_by(SocialPost.updated_at.desc()).limit(200).all()
    return render_template("social/drafts.html", posts=posts,
                           status_f=status_f, statuses=allowed)


@social_bp.route("/compose")
@login_required
def compose():
    _guard()
    # A date from the calendar ("compose on this day") pre-fills the schedule.
    schedule_value = ""
    date_arg = request.args.get("date")
    if date_arg:
        try:
            datetime.strptime(date_arg, "%Y-%m-%d")
            schedule_value = f"{date_arg}T10:00"
        except ValueError:
            pass

    # Compose FROM a task: link the post, pre-fill the client + matching
    # channels, and load the task's deliverable files as the media.
    task = None
    task_assets = []
    selected_account_ids = []
    default_client_id = _client_arg()
    task_id_arg = request.args.get("task", type=int)
    if task_id_arg:
        task = Task.query.get_or_404(task_id_arg)
        task_assets = [_task_file_preview(f)
                       for f in _task_deliverable_files(task)]
        selected_account_ids = _suggested_accounts_for_task(task)
        default_client_id = task.client_id or default_client_id

    return render_template(
        "social/compose.html",
        post=None,
        task=task,
        task_assets=task_assets,
        uploaded_media=[],
        selected_task_file_ids=[a["id"] for a in task_assets],
        accounts=AccountManager.list_accounts(),
        clients=Client.query.filter_by(status="active").order_by(
            Client.client_name).all(),
        capabilities=_capabilities_map(),
        selected_account_ids=selected_account_ids,
        selected_asset_ids=[],
        post_type="image",
        schedule_value=schedule_value,
        default_client_id=default_client_id,
        first_comment="",
        platform_captions={},
        hashtag_sets=_hashtag_sets(default_client_id),
        story_style="plain",
        story_link_target_id=None,
        linkable_targets=_linkable_targets(default_client_id),
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
    asset_ids, task_file_ids = [], []
    if first:
        asset_ids = [m.client_asset_id for m in first.media if m.client_asset_id]
        task_file_ids = [m.task_file_id for m in first.media if m.task_file_id]
    # Per-platform caption overrides (any target whose caption differs from the
    # shared base) + the first comment (shared across targets).
    platform_captions = {
        t.platform: t.caption for t in post.targets
        if t.caption and t.caption != post.base_caption
    }
    first_comment = next(
        (t.first_comment for t in post.targets if t.first_comment), "")
    story_target = next(
        (t for t in post.targets if t.post_type == "story"), None)
    # If this post came from a task, keep its deliverable files available.
    task, task_assets = None, []
    if post.task_id:
        task = db.session.get(Task, post.task_id)
        if task is not None:
            task_assets = [_task_file_preview(f)
                           for f in _task_deliverable_files(task)]
    # Reconstruct directly-uploaded media so an edit keeps it.
    uploaded_media = []
    if first:
        from app.social.media import pipeline
        for m in first.media:
            if m.source == "upload" and m.object_key:
                is_img = (m.mime_type or "").startswith("image")
                u = None
                if is_img:
                    try:
                        u = pipeline.presigned_url(m.object_key)
                    except Exception:  # noqa: BLE001
                        u = None
                uploaded_media.append({
                    "object_key": m.object_key, "mime": m.mime_type or "",
                    "is_image": is_img, "url": u})
    return render_template(
        "social/compose.html",
        post=post,
        task=task,
        task_assets=task_assets,
        uploaded_media=uploaded_media,
        selected_task_file_ids=task_file_ids,
        accounts=AccountManager.list_accounts(),
        clients=Client.query.filter_by(status="active").order_by(
            Client.client_name).all(),
        capabilities=_capabilities_map(),
        selected_account_ids=account_ids,
        selected_asset_ids=asset_ids,
        post_type=(first.post_type if first else "image"),
        schedule_value=_to_ist_input(first.scheduled_for if first else None),
        first_comment=first_comment or "",
        platform_captions=platform_captions,
        hashtag_sets=_hashtag_sets(post.client_id),
        # The style lives on the story target - which is `first` for a
        # standalone story, and the companion beside it otherwise. A
        # companion's link is rebuilt on save (it points at its sibling),
        # so only a standalone story restores a chosen target id.
        story_style=(story_target.story_style if story_target else "plain"),
        story_link_target_id=(
            first.story_link_target_id
            if first is not None and first.post_type == "story" else None),
        linkable_targets=_linkable_targets(post.client_id),
    )


def _apply_composer_form(post):
    """Build/rebuild a post's targets + media from the composer form. Used
    by both create and edit (edit only runs for editable statuses)."""
    post.title = (request.form.get("title") or "").strip() or None
    client_id = request.form.get("client_id", type=int)
    post.client_id = client_id
    # Link the post back to the originating task (compose-from-task), so a
    # real publish can move that task to Published.
    post.task_id = request.form.get("task_id", type=int) or None
    base_caption = (request.form.get("caption") or "").strip() or None
    post.base_caption = base_caption
    first_comment = (request.form.get("first_comment") or "").strip() or None

    post_type = (request.form.get("post_type") or "image").strip()
    account_ids = request.form.getlist("account_ids", type=int)
    # ids arrive in the chosen order (carousel ordering) - preserved below.
    asset_ids = request.form.getlist("asset_ids", type=int)
    task_file_ids = request.form.getlist("task_file_ids", type=int)
    publish_now = request.form.get("publish_mode") == "now"
    scheduled_for = (
        datetime.utcnow() if publish_now
        else _parse_schedule(request.form.get("schedule"))
    )

    # Rebuild targets from scratch (drafts only) - simplest correct model.
    for t in list(post.targets):
        db.session.delete(t)
    db.session.flush()

    # Resolve media from BOTH sources - the task's deliverable files first
    # (they are the creative), then any client brand assets - preserving order.
    # Client safety (mirrors the channel rule below): media may only come from
    # the POST's own client, so a tampered id can't attach Client A's
    # confidential deliverable/brand asset to Client B's post.
    post_cid = post.client_id
    asset_by_id = {
        a.id: a for a in ClientAsset.query.filter(
            ClientAsset.id.in_(asset_ids)).all()
        if post_cid and a.client_id == post_cid
    } if asset_ids else {}
    tf_by_id = {
        f.id: f for f in TaskFile.query.filter(
            TaskFile.id.in_(task_file_ids)).all()
        if post_cid and f.task and f.task.client_id == post_cid
    } if task_file_ids else {}
    media_items = [("task_file", tf_by_id[i]) for i in task_file_ids if i in tf_by_id]
    media_items += [("client_asset", asset_by_id[i]) for i in asset_ids if i in asset_by_id]
    # Directly-uploaded media (not from a task/brand asset): "object_key::mime".
    from types import SimpleNamespace
    for raw in request.form.getlist("upload_media"):
        key, _, mime = raw.partition("::")
        if key.startswith("social_uploads/"):
            media_items.append(("upload", SimpleNamespace(
                object_key=key, mime_type=(mime or None), id=None)))

    def _add_media(target_id):
        for i, (source, obj) in enumerate(media_items):
            kw = dict(target_id=target_id, source=source,
                      object_key=obj.object_key, mime_type=obj.mime_type,
                      role="main", sort_order=i)
            if source == "task_file":
                kw["task_file_id"] = obj.id
            elif source == "client_asset":
                kw["client_asset_id"] = obj.id
            db.session.add(SocialMediaAsset(**kw))

    def _new_target(account, ptype, caption, story_style="plain",
                    story_link_target_id=None):
        t = SocialPostTarget(
            social_post_id=post.id, social_account_id=account.id,
            platform=account.platform, post_type=ptype, caption=caption,
            # Not on a story: there is nothing to comment on, so storing it
            # there only makes the post page claim a first comment that was
            # never going anywhere.
            first_comment=(None if ptype == "story" else first_comment),
            status="draft",
            scheduled_for=scheduled_for,
            story_style=story_style if ptype == "story" else "plain",
            story_link_target_id=(
                story_link_target_id if ptype == "story" else None),
        )
        db.session.add(t)
        db.session.flush()
        _add_media(t.id)
        return t

    # "Also share to Story": in addition to the feed post, publish the same
    # media as a Story on platforms that support it (Instagram). Only when the
    # main post isn't already a story and there's media to show.
    also_story = bool(request.form.get("also_story")) \
        and post_type not in ("story", "text") and media_items

    # plain | post_link. "post_link" means the story is supposed to be
    # tappable through to a feed post - which no platform lets us do via
    # API, so it becomes a follow-up after publishing (see
    # SocialPostTarget.needs_story_link).
    story_style = "post_link" \
        if request.form.get("story_style") == "post_link" else "plain"

    # A standalone story links to a post that already exists; the
    # companion story links to the feed target created beside it, so that
    # id only becomes known inside the loop.
    standalone_link_id = None
    if post_type == "story" and story_style == "post_link":
        standalone_link_id = _linked_target_id(
            request.form.get("story_link_target_id"), post.client_id)
        if standalone_link_id is None:
            story_style = "plain"
            flash("That story had no post to link to, so it was saved as a "
                  "plain story.", "info")

    skipped = []
    n_created = 0
    for account_id in account_ids:
        account = db.session.get(SocialAccount, account_id)
        if account is None:
            continue
        # Client safety: stop publishing Client A's post to Client B's Page,
        # even if the form is tampered with.
        if not _channel_client_ok(account.client_id, post.client_id):
            skipped.append(account.display_name)
            continue
        override = (request.form.get(f"caption_{account.platform}") or "").strip()
        feed = _new_target(account, post_type, override or base_caption,
                           story_style=story_style,
                           story_link_target_id=standalone_link_id)
        n_created += 1
        # Optional companion Story (no caption - stories don't use one).
        if also_story:
            prov = registry.get(account.platform)
            caps = prov.capabilities if prov else None
            if caps and caps.story_support and "story" in (caps.post_types or set()):
                _new_target(account, "story", None, story_style=story_style,
                            story_link_target_id=feed.id)

    if skipped:
        flash("Skipped channel(s) that belong to a different client: "
              + ", ".join(skipped) + ".", "info")
    return n_created


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
    if request.form.get("action") == "submit" and post.targets:
        post.status = "pending_approval"
        audit.record("submitted_for_approval", post_id=post.id,
                     actor_id=current_user.id, task_id=post.task_id)
        _notify_submit(post)
        db.session.commit()
        flash("Submitted for approval.", "success")
        return redirect(url_for("social.post_detail", post_id=post.id))
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
    if request.form.get("action") == "submit" and post.targets \
            and post.status in ("draft",):
        post.status = "pending_approval"
        audit.record("submitted_for_approval", post_id=post.id,
                     actor_id=current_user.id, task_id=post.task_id)
        _notify_submit(post)
        db.session.commit()
        flash("Saved & submitted for approval.", "success")
        return redirect(url_for("social.post_detail", post_id=post.id))
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
    # Media thumbnails so a reviewer signs off on the actual creative, not a
    # bare count. Media is the same across a post's targets - use the first.
    media_previews = []
    if post.targets and post.targets[0].media:
        from app.social.media import pipeline
        for m in post.targets[0].media:
            is_image = (m.mime_type or "").startswith("image")
            url = None
            if is_image and m.object_key:
                try:
                    url = pipeline.presigned_url(m.object_key)
                except Exception:  # noqa: BLE001
                    url = None
            media_previews.append({"url": url, "is_image": is_image})
    # What actually became of the first comment. It is posted after the
    # publish and can be skipped or refused for reasons the composer can't
    # see (a missing Graph scope, a provider that can't comment), so the
    # outcome is read back from the audit trail rather than assumed.
    first_comment_events = (
        SocialAuditLog.query
        .filter(SocialAuditLog.post_id == post.id,
                SocialAuditLog.action.in_(["first_comment_posted",
                                           "first_comment_failed",
                                           "first_comment_skipped"]))
        .order_by(SocialAuditLog.id.desc())
        .all()
    ) if post.targets else []

    return render_template(
        "social/post_detail.html",
        post=post,
        problems=problems,
        first_comment_events=first_comment_events,
        media_previews=media_previews,
        can_approve=can_publish(current_user),
        to_ist=_to_ist_input,
        to_ist_display=_to_ist_display,
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
    _detach_post_history(post)
    db.session.delete(post)
    db.session.commit()
    flash("Draft deleted.", "success")
    return redirect(url_for("social.drafts"))


@social_bp.route("/posts/bulk", methods=["POST"])
@login_required
def posts_bulk():
    """Bulk submit-for-approval or delete across a client's draft backlog."""
    _guard()
    ids = request.form.getlist("post_ids", type=int)
    action = request.form.get("action")
    client = request.form.get("client", type=int) or None
    if action not in ("submit", "delete") or not ids:
        flash("Select at least one post and an action.", "error")
        return redirect(url_for("social.drafts", client=client))
    posts = SocialPost.query.filter(SocialPost.id.in_(ids)).all()
    n = 0
    for post in posts:
        if action == "submit":
            if post.status == "draft" and post.targets:
                post.status = "pending_approval"
                audit.record("submitted_for_approval", post_id=post.id,
                             actor_id=current_user.id, task_id=post.task_id)
                _notify_submit(post)
                n += 1
        elif post.status not in ("publishing", "published",
                                 "partially_published"):
            audit.record("post_deleted", post_id=None,
                         actor_id=current_user.id,
                         detail={"post_id": post.id, "title": post.title})
            _detach_post_history(post)
            db.session.delete(post)
            n += 1
    db.session.commit()
    flash(f"{'Submitted' if action == 'submit' else 'Deleted'} {n} post(s).",
          "success")
    return redirect(url_for("social.drafts", client=client))


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
    _notify_submit(post)
    db.session.commit()
    flash("Submitted for approval.", "success")
    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/posts/<int:post_id>/approve", methods=["POST"])
@login_required
def approve_post(post_id):
    if not can_publish(current_user):
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
    if not can_publish(current_user):
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
    if post.task_id:
        # Reflect the new state on the task now: "Scheduled" for a future time,
        # or straight into the "In publish queue" (Published) lane for now.
        task_link.sync_task_from_posts(
            task_link._task_of(post), actor_id=current_user.id)
        db.session.commit()
    if result["problems"]:
        # Name the platform and the reason. "Check the validation notes"
        # made someone hunt down a table to find out that Instagram cannot
        # take a video - which the message may as well just say.
        by_id = {t.id: t for t in post.targets}
        lines = [
            f"{platform_label(by_id[tid].platform)}: {' '.join(errs)}"
            for tid, errs in result["problems"].items() if tid in by_id
        ]
        flash(
            (f"Scheduled {result['scheduled']} platform(s). "
             if result["scheduled"] else "Nothing could be scheduled. ")
            + "Not scheduled — " + "; ".join(lines),
            "error" if not result["scheduled"] else "info",
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
    return jsonify(assets=[_asset_preview(a) for a in assets])


@social_bp.route("/api/upload", methods=["POST"])
@login_required
def upload_media():
    """Direct media upload for posts/stories that DIDN'T come from a task -
    e.g. a photo/video a client sent us to publish. Stored in R2 under a
    dedicated prefix (never mixed with task files or brand assets) and
    attached to the post as source='upload'."""
    _guard()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file provided."), 400
    mime = (f.mimetype or "").lower()
    if not (mime.startswith("image") or mime.startswith("video")):
        return jsonify(error="Only images and videos can be uploaded."), 400
    import os
    import re as _re
    from uuid import uuid4
    from app.storage.storage_service import StorageService, StorageServiceError
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_",
                   os.path.basename(f.filename))[:80] or "upload"
    object_key = f"social_uploads/{uuid4().hex}_{safe}"
    try:
        # Stream the file straight to R2 rather than f.read() (which pulls the
        # whole body - up to MAX_CONTENT_LENGTH, and this accepts video - into
        # memory and can OOM the worker under a few concurrent uploads).
        StorageService().upload(
            file_obj=f.stream, object_key=object_key, content_type=mime)
    except (StorageServiceError, Exception):  # noqa: BLE001
        current_app.logger.exception("[social-upload] store failed")
        return jsonify(error="Upload failed — please try again."), 500
    url = None
    if mime.startswith("image"):
        try:
            from app.social.media import pipeline
            url = pipeline.presigned_url(object_key)
        except Exception:  # noqa: BLE001
            url = None
    return jsonify(object_key=object_key, mime=mime, filename=safe,
                   is_image=mime.startswith("image"), url=url)


@social_bp.route("/api/mentions")
@login_required
def mentions_api():
    """Mentionable handles for the composer's @-autocomplete: the brands/
    channels the agency manages (connected accounts). Useful for tagging a
    sister brand or cross-promo. Optional ?q= filter, ?platform= to prefer a
    platform's handles first."""
    _guard()
    q = (request.args.get("q") or "").strip().lower()
    prefer = (request.args.get("platform") or "").strip()
    out = []
    for a in AccountManager.list_accounts(include_revoked=False):
        handle = (a.display_name or "").lstrip("@")
        if not handle or (q and q not in handle.lower()):
            continue
        out.append({
            "handle": handle,
            "label": a.display_name,
            "platform": a.platform,
            "type": a.account_type,
        })
    # Preferred-platform handles first, then alphabetical.
    out.sort(key=lambda m: (m["platform"] != prefer, m["label"].lower()))
    return jsonify(suggestions=out[:8])


@social_bp.route("/api/hashtag-sets", methods=["POST"])
@login_required
def create_hashtag_set():
    """Save the current hashtags as a reusable, named set (AJAX from the
    composer; CSRF is attached automatically by csrf.js)."""
    _guard()
    from app.models import SocialHashtagSet
    name = (request.form.get("name") or "").strip()[:120]
    hashtags = (request.form.get("hashtags") or "").strip()
    client_id = request.form.get("client_id", type=int)
    if not name or not hashtags:
        return jsonify(error="A name and at least one hashtag are required."), 400
    # Never orphan a set to a bogus/inactive client id.
    if client_id and not Client.query.filter_by(
            id=client_id, status="active").first():
        client_id = None
    row = SocialHashtagSet(
        name=name, hashtags=hashtags, client_id=client_id or None,
        created_by_id=current_user.id)
    db.session.add(row)
    db.session.commit()
    return jsonify(id=row.id, name=row.name, hashtags=row.hashtags)


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
        _scope_targets(SocialPostTarget.query, _client_arg())
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
    cid = _client_arg()
    targets = (
        _scope_targets(SocialPostTarget.query, cid)
        .order_by(SocialPostTarget.updated_at.desc())
        .limit(100)
        .all()
    )
    # Posts published directly on the platform (outside Studio) have no targets,
    # so surface them as their own rows to keep the Published list complete.
    external_posts = (
        _scope_posts(
            SocialPost.query.filter(SocialPost.published_externally.is_(True)),
            cid)
        .order_by(SocialPost.updated_at.desc())
        .limit(50)
        .all()
    )
    return render_template("social/history.html", targets=targets,
                           external_posts=external_posts)


# ======================================================================
# Engage - comments inbox
# ======================================================================

def _scope_comments(query, client_id):
    """Filter a SocialComment query to one client, via target -> post."""
    query = query.join(
        SocialPostTarget,
        SocialComment.target_id == SocialPostTarget.id)
    if client_id:
        query = (query.join(
            SocialPost, SocialPostTarget.social_post_id == SocialPost.id)
            .filter(SocialPost.client_id == client_id))
    return query


@social_bp.route("/engage")
@login_required
def engage():
    """The comments inbox, as a two-pane inbox rather than a stack of cards.

    Selecting a comment is a plain link (`?c=<id>`) and the detail pane is
    rendered server-side, so the whole screen works without JavaScript and
    a conversation is a shareable URL. The JS in the template is only
    keyboard navigation on top.
    """
    from sqlalchemy.orm import joinedload

    from app.models import SocialComment

    _guard()
    cid = _client_arg()

    status_f = request.args.get("status")
    status_f = status_f if status_f in ("open", "done") else "open"
    platform_f = (request.args.get("platform") or "").strip()
    search = (request.args.get("q") or "").strip()

    base = _scope_comments(
        SocialComment.query.filter(SocialComment.is_ours.is_(False)), cid)

    q = base.filter(SocialComment.status == status_f)
    if platform_f:
        q = q.filter(SocialComment.platform == platform_f)
    if search:
        needle = f"%{search}%"
        q = q.filter(db.or_(SocialComment.message.ilike(needle),
                            SocialComment.author_name.ilike(needle)))

    comments = (
        q.options(joinedload(SocialComment.target)
                  .joinedload(SocialPostTarget.account),
                  joinedload(SocialComment.target)
                  .joinedload(SocialPostTarget.post))
        .order_by(SocialComment.created_at.desc())
        .limit(200)
        .all()
    )

    # Our replies, grouped by the comment they answered, so the detail pane
    # can show the exchange in order.
    ext_ids = [c.external_id for c in comments]
    replies = {}
    if ext_ids:
        for r in (SocialComment.query
                  .filter(SocialComment.is_ours.is_(True),
                          SocialComment.parent_external_id.in_(ext_ids))
                  .order_by(SocialComment.created_at.asc()).all()):
            replies.setdefault(r.parent_external_id, []).append(r)

    # The conversation on the right. Defaults to the first in the list so
    # the pane is never blank while the list has something in it.
    selected = None
    wanted = request.args.get("c", type=int)
    if wanted:
        selected = next((c for c in comments if c.id == wanted), None)
    if selected is None and comments:
        selected = comments[0]

    counts = {
        "open": base.filter(SocialComment.status == "open").count(),
        "done": base.filter(SocialComment.status == "done").count(),
    }
    # Distinct in SQL, not by loading every comment to read one column off
    # each - this list only exists to populate a filter dropdown.
    platforms = sorted(
        p for (p,) in base.with_entities(SocialComment.platform).distinct()
    )

    return render_template(
        "social/engage.html",
        comments=comments, replies=replies, selected=selected,
        status_f=status_f, platform_f=platform_f, search=search,
        counts=counts, platforms=platforms,
        open_total=counts["open"],
    )


@social_bp.route("/engage/sync", methods=["POST"])
@login_required
def engage_sync():
    _guard()
    report = engage_svc.sync_comments(_client_arg())

    # "All caught up" is only honest when we actually managed to look.
    # A run where every request was refused used to say exactly that.
    if report["failed"]:
        flash(
            f"Checked {report['checked']} post(s), {report['failed']} could "
            "not be read: " + "; ".join(report["errors"]) + ".",
            "error")
    if report["new"]:
        flash(f"Fetched {report['new']} new comment(s) from the platforms.",
              "success")
    elif not report["failed"]:
        if not report["checked"]:
            flash(
                "Nothing to check yet — comments are fetched for posts "
                "published through the Studio, and there aren't any on a "
                "channel that supports comments.", "info")
        else:
            flash(f"Checked {report['checked']} post(s) — no new comments.",
                  "info")

    return redirect(url_for("social.engage", client=_client_arg()))


def _engage_back(comment_id=None):
    """Back to the inbox with the filters - and optionally the open
    conversation - the person was actually looking at. Losing those on
    every reply is what makes an inbox tiring to work through."""
    return url_for(
        "social.engage",
        client=_client_arg(),
        status=request.form.get("status") or None,
        platform=request.form.get("platform") or None,
        q=request.form.get("q") or None,
        c=comment_id,
    )


@social_bp.route("/engage/<int:comment_id>/reply", methods=["POST"])
@login_required
def engage_reply(comment_id):
    _guard()
    from app.models import SocialComment
    comment = SocialComment.query.get_or_404(comment_id)
    text = (request.form.get("message") or "").strip()
    if not text:
        flash("Write a reply first.", "error")
    else:
        ext = engage_svc.reply(comment, text, actor_id=current_user.id)
        flash("Reply posted." if ext else
              "Couldn't post the reply — check the channel connection.",
              "success" if ext else "error")
    return redirect(_engage_back(comment.id))


@social_bp.route("/engage/<int:comment_id>/done", methods=["POST"])
@login_required
def engage_done(comment_id):
    _guard()
    from app.models import SocialComment
    comment = SocialComment.query.get_or_404(comment_id)
    engage_svc.mark_done(comment, done=(comment.status != "done"))
    # Not back to this comment: it has just left the list you were working
    # through, so returning to it would show an empty pane. The next one
    # is what you want.
    return redirect(_engage_back())


# ======================================================================
# Studio settings
# ======================================================================

@social_bp.route("/settings")
@login_required
def settings():
    _guard()
    from app.models import SocialHashtagSet
    cid = _client_arg()
    # All sets visible for the active scope (this client's + agency-wide).
    hashtag_sets = (_hashtag_sets(cid) if cid
                    else SocialHashtagSet.query.order_by(
                        SocialHashtagSet.name).all())
    accounts = AccountManager.list_accounts()
    engine_info = {
        "enabled": current_app.config.get("SOCIAL_ENGINE_ENABLED", False),
        "auto_worker": current_app.config.get("SOCIAL_INPROCESS_WORKER", True),
        "worker_interval": current_app.config.get("SOCIAL_WORKER_INTERVAL", 20),
        "simulation": bool(current_app.config.get("META_EMULATOR")),
        "cron_ready": bool(current_app.config.get("SOCIAL_WORKER_TOKEN")),
        "token_vault": bool(current_app.config.get("SOCIAL_TOKEN_KEY")),
    }
    return render_template(
        "social/settings.html", hashtag_sets=hashtag_sets, accounts=accounts,
        engine=engine_info, is_engine_admin=can_manage_social_engine(current_user))


@social_bp.route("/settings/hashtag-sets/<int:set_id>/delete", methods=["POST"])
@login_required
def delete_hashtag_set(set_id):
    _guard()
    from app.models import SocialHashtagSet
    hs = SocialHashtagSet.query.get_or_404(set_id)
    db.session.delete(hs)
    db.session.commit()
    flash("Hashtag set deleted.", "success")
    return redirect(url_for("social.settings", client=_client_arg()))


@social_bp.route("/accounts/<int:account_id>/disconnect", methods=["POST"])
@login_required
def disconnect_account(account_id):
    if not can_connect_social_accounts(current_user):
        abort(403)
    account = SocialAccount.query.get_or_404(account_id)
    AccountManager.disconnect(account)
    audit.record(
        "account_disconnected", account_id=account.id,
        actor_id=current_user.id, commit=True,
    )
    flash(f"Disconnected {account.display_name}.", "success")
    return redirect(url_for("social.index"))
