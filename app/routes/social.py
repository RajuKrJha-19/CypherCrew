"""Social Publishing Engine UI + JSON API.

Registered only when SOCIAL_ENGINE_ENABLED (see app/__init__.py), so the
whole surface is absent unless the engine is turned on. Gated by
can_use_social / can_connect_social_accounts - both admin roles, plus
anyone holding the matching manage_social / connect_social_accounts
permission.
"""

import json
from datetime import datetime, timedelta

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.models import (
    Client, ClientAsset, ContentVersion, PublishJob, PublishResult,
    SocialAccount, SocialAuditLog, SocialComment, SocialMediaAsset, SocialPost,
    SocialPostTarget, Task, TaskFile,
)
from app.social.dto import TokenBundle
from app.social.media import fit as media_fit
from app.social import status as engine_status
from app.social.registry import registry
from app.social.services import (
    approval, audit, lifecycle, publish_review, publishing, queue_slots,
    recovery, scheduling, task_link, versioning,
)
from app.social.services import engage as engage_svc
from app.social import utm
from app.social.services.accounts import AccountManager
from app.utils.permissions import (
    can_connect_social_accounts, can_manage_social_engine, can_publish,
    can_use_social, has_permission,
)
from app.utils.social_platforms import (
    PLATFORMS, PLATFORM_KEYS, label as platform_label, parse_platforms,
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


def _channels_by_client(groups, clients):
    """Gather the platform groups under the CLIENT each one serves.

    A flat grid of channels asks the reader to do the grouping in their
    head: "which of these eight is Hope Plus IVF's?" - and that is the
    only question this page is ever open to answer, because every other
    Studio screen is already scoped to one client. So the client is the
    heading and the channels sit under it.

    Returns [{client, accounts, groups, platforms}], clients in the
    switcher's own order (parents before their sub-clients) so this page
    and the client picker cannot disagree, and the unassigned bucket last:
    it is the pile that still needs a decision, not a client.
    """
    by_client = {}
    for g in groups:
        by_client.setdefault(g["account"].client_id, []).append(g)

    sections = []
    for client in clients:
        owned = by_client.pop(client.id, [])
        if owned:
            sections.append(_client_section(client, owned))

    # A channel bound to a client that is no longer active still has to
    # appear - dropping it would hide a live publishing target - so
    # whatever is left over joins the unassigned bucket rather than
    # vanishing with its client.
    leftover = [g for gs in by_client.values() for g in gs]
    if leftover:
        sections.append(_client_section(None, leftover))

    return sections


def _client_section(client, groups):
    accounts = [g["account"] for g in groups] + [
        child for g in groups for child in g["children"]
    ]
    return {
        "client": client,
        "groups": groups,
        "accounts": accounts,
        # Distinct platforms, in catalog order, for the header's pips.
        "platforms": [
            key for key in PLATFORM_KEYS
            if any(a.platform == key for a in accounts)
        ],
        "needs_attention": sum(
            1 for a in accounts if a.status != "active"
        ),
    }


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


def _measure_key(source, obj):
    """Match a browser measurement back to the media item it describes.

    Keyed by the form field and value rather than the object key, because
    that is all the browser has: a deliverable checkbox carries a TaskFile
    id, not an R2 key. Both sides build the same string.
    """
    if source == "task_file":
        return f"task_file_ids|{obj.id}"
    if source == "client_asset":
        return f"asset_ids|{obj.id}"
    return f"upload_media|{getattr(obj, 'form_value', obj.object_key)}"


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
    """A ClientAsset -> the dict the media library renders: filename,
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
    is_video = (tf.mime_type or "").startswith("video")
    url = None
    # Videos get a URL too, not just images. It costs nothing extra - the
    # composer reads a video's dimensions and duration with
    # preload="metadata", which fetches a few KB of header, and without a
    # URL a video deliverable could not be measured at all. That was the
    # gap: the file people actually hit this with is a video.
    if (is_image or is_video) and tf.object_key:
        try:
            url = pipeline.presigned_url(tf.object_key)
        except Exception:  # noqa: BLE001
            url = None
    return {"id": tf.id, "filename": tf.original_filename,
            "mime": tf.mime_type or "", "is_image": is_image, "url": url,
            "object_key": tf.object_key}


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


def _transcode_available():
    """Whether the server can downscale/re-encode video on publish. The
    composer uses this to decide if a too-wide reel is auto-fixable (button
    stays enabled) or a hard block (no ffmpeg -> it will be blocked at
    schedule time, so don't promise a resize)."""
    from app.social.media import transcode
    return transcode.available()


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
            # Emitted because the composer gates the "Also share to Story"
            # checkbox on it (storyPlatforms(), compose.html) - and it was
            # never in this dict, so that lookup was always undefined, the
            # list was always empty, and the checkbox was permanently
            # disabled. A finished feature - companion Story targets,
            # story_style, the story_link flow and its tests - reachable by
            # nobody, because of one absent key.
            #
            # Computed with the SAME test the server applies when it decides
            # whether to actually create the companion target (schedule_post,
            # "if caps.story_support and 'story' in caps.post_types"). Two
            # different tests would mean a tickable box that silently
            # produced no Story.
            "story_support": bool(
                caps and caps.story_support
                and "story" in (caps.post_types or set())
            ),
            "simulation": getattr(provider, "is_simulation", False),
            # The real media limits, so the composer can run the same
            # reel-first decision the server will and show the answer
            # before anyone submits.
            "media_specs": {
                ptype: {
                    "aspect_min": spec.aspect_min, "aspect_max": spec.aspect_max,
                    "duration_min": spec.duration_min,
                    "duration_max": spec.duration_max,
                    "width_min": spec.width_min, "width_max": spec.width_max,
                    "height_min": spec.height_min,
                    "max_bytes": spec.max_bytes,
                    "aspect_label": spec.aspect_label,
                    # The shape the platform SHOWS this in, which the limits
                    # above do not answer - Instagram takes a Reel at any
                    # aspect and then displays it at 9:16. Drives the
                    # composer's safe-area overlay; validates nothing.
                    "display_aspect": spec.display_aspect,
                    "display_label": spec.display_label,
                }
                for ptype, spec in ((caps.media_specs or {}) if caps else {}).items()
            },
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


def _reel_cover_url(post):
    """Presigned URL of a post's custom reel cover, for the edit preview."""
    if not post or not post.reel_cover_key:
        return ""
    try:
        from app.social.media import pipeline
        return pipeline.presigned_url(post.reel_cover_key) or ""
    except Exception:  # noqa: BLE001
        return ""


def _campaigns(client_id=None):
    """Distinct campaign labels used so far (this client's, or all), for the
    composer's autocomplete and the drafts filter."""
    q = db.session.query(SocialPost.campaign).filter(
        SocialPost.campaign.isnot(None))
    if client_id:
        q = q.filter(SocialPost.client_id == client_id)
    return sorted({c for (c,) in q.distinct().all() if c})


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
    studio_clients = _studio_clients()
    groups = _grouped_accounts(accts)
    return render_template(
        "social/accounts.html",
        groups=groups,
        # The page renders from these: one section per client, the channels
        # that serve them inside. `groups` stays for the empty-state check.
        client_sections=_channels_by_client(groups, studio_clients),
        accounts=accts,
        platforms=PLATFORMS,
        available=registry.keys(),
        connectable=_connectable_keys(),
        simulated=_simulated_keys(),
        clients=studio_clients,
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
    campaign_f = (request.args.get("campaign") or "").strip()

    return render_template(
        "social/analytics.html",
        accounts=AccountManager.list_accounts(include_revoked=False),
        status=engine_status.engine_status(),
        period=period,
        report=analytics_report.build_report(period, cid,
                                             campaign=campaign_f or None),
        campaign_f=campaign_f,
        campaigns=_campaigns(cid),
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
    """Re-run a single stuck platform for a post. Available to publishers
    (not just engine admins) so a one-platform failure on a multi-platform
    post can be fixed without touching the whole Queue.

    "blocked" counts as retryable alongside "failed". A blocked target is one
    the app itself refused before sending - a caption over the limit, media
    the platform will not take, a missing account - and the fix is to correct
    the post and try again. But the gate only accepted "failed", so once the
    composer had been fixed the target still could not be retried: the only
    ways out were Drop, or a remap that changed the post type. The one status
    whose cause is most likely to have been repaired was the one that could
    not be re-run.
    """
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)
    if target.status not in lifecycle.STUCK_TARGET_STATUSES:
        flash("Only a failed or blocked platform can be retried.", "error")
        return redirect(url_for("social.post_detail",
                                post_id=target.social_post_id))
    job = target.job
    if job is not None and recovery.requeue_job(
            job, actor_id=current_user.id, commit=True):
        flash(f"Retrying {target.platform} now — it will go out in a moment.",
              "success")
    else:
        publishing.publish_target_now(target, actor_id=current_user.id)
        flash(f"Re-queued {target.platform} — publishing now.", "success")
    # Roll the parent back from failed so the UI reflects the in-flight retry.
    post = target.post
    if post and post.status == "failed":
        post.status = "publishing"
        db.session.commit()
    # Immediate drain so the retry doesn't wait for the next worker tick.
    from app.social.queue import worker
    worker.kick_async(current_app._get_current_object())
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
    # old target id -> new target id, so a story's "link to this other post"
    # (story_link_target_id points at a SIBLING target) can be re-pointed at
    # the copied sibling instead of dangling back to the source post.
    id_map = {}
    for t in src.targets:
        nt = SocialPostTarget(
            social_post_id=dup.id, social_account_id=t.social_account_id,
            platform=t.platform, post_type=t.post_type, caption=t.caption,
            hashtags=t.hashtags, first_comment=t.first_comment,
            story_style=t.story_style,
            status="draft", scheduled_for=None)
        db.session.add(nt)
        db.session.flush()
        id_map[t.id] = nt.id
        for m in t.media:
            db.session.add(SocialMediaAsset(
                target_id=nt.id, source=m.source, task_file_id=m.task_file_id,
                client_asset_id=m.client_asset_id, object_key=m.object_key,
                role=m.role, sort_order=m.sort_order, alt_text=m.alt_text,
                mime_type=m.mime_type))
    # Second pass: remap sibling story links now every new id exists (a link
    # may point forward to a target created later in the first pass).
    for t in src.targets:
        if t.story_link_target_id and t.story_link_target_id in id_map:
            db.session.query(SocialPostTarget).filter_by(
                id=id_map[t.id]).update(
                    {"story_link_target_id": id_map[t.story_link_target_id]})
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
    # PublishJobs FK to targets with no ON DELETE. A scheduled or failed post
    # is deletable and its targets carry jobs - and a target can carry MORE
    # than one (schedule + a publish-now retry), which the scalar target.job
    # cascade can't be trusted to clear. Delete them all by target_id, the
    # same way remove_target does, or db.session.delete(post) 500s on the
    # orphaned FK.
    PublishJob.query.filter(
        PublishJob.target_id.in_(tids)).delete(synchronize_session=False)
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
    """Delete every not-yet-run job for a post's targets (safe: only 'queued',
    never a job already claimed/publishing/succeeded). Filters by target_id
    rather than the scalar target.job, so if a target somehow holds more than
    one queued job they are all cancelled, not just the newest."""
    tids = [t.id for t in post.targets]
    if tids:
        PublishJob.query.filter(
            PublishJob.target_id.in_(tids),
            PublishJob.state == "queued",
        ).delete(synchronize_session=False)


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
    if when < datetime.utcnow() - timedelta(minutes=1):
        flash("That time is in the past. Pick a future time.", "error")
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


@social_bp.route("/targets/<int:target_id>/remap", methods=["POST"])
@login_required
def remap_target(target_id):
    """Re-decide what this platform should publish, and try again.

    The repair for a target created before the reel-first mapping existed:
    an Instagram target still carrying post_type="video", which Instagram
    has no such thing as. One click turns it into the Reel it always
    should have been - without editing the post, which would be refused
    anyway once it is scheduled, and without touching a sibling platform
    that has already published.
    """
    _guard()
    target = SocialPostTarget.query.get_or_404(target_id)

    if target.status in ("published", "removed"):
        flash("That platform has already published.", "error")
        return _back_to(target.social_post_id)

    provider = registry.get(target.platform)
    caps = provider.capabilities if provider else None
    measurements = {}
    if target.media:
        measurements = (target.media[0].meta or {}).get("measurements") or {}

    new_type, notes = media_fit.choose_post_type(
        target.post_type, caps, measurements)

    if new_type is None:
        flash(
            f"{platform_label(target.platform)} still can't take this: "
            + (notes[0] if notes else "the file doesn't meet its limits")
            + ". The file itself has to change.", "error")
        return _back_to(target.social_post_id)

    if new_type == target.post_type:
        flash(
            f"{platform_label(target.platform)} is already set to publish "
            f"this as a {new_type}. Retry it, or check the reason above.",
            "info")
        return _back_to(target.social_post_id)

    old_type = target.post_type
    target.post_type = new_type
    target.last_error = None
    # Back into the queue at the post's own pace: scheduled if the post is,
    # draft otherwise, so this cannot publish something the post hasn't
    # been approved for.
    target.status = "scheduled" if target.post.status == "scheduled" \
        else "draft"
    if target.status == "scheduled" and not target.scheduled_for:
        target.scheduled_for = datetime.utcnow()

    audit.record("target_remapped", post_id=target.social_post_id,
                 target_id=target.id, actor_id=current_user.id,
                 detail={"from": old_type, "to": new_type})
    db.session.commit()

    flash(
        f"{platform_label(target.platform)} will publish this as a "
        f"{new_type} instead of a {old_type}.", "success")
    return _back_to(target.social_post_id)


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
    # "failed" and "partially_published" belong here too: a post whose targets
    # all blocked settles to "failed", and it must stay on the Posts list so
    # the team can fix/retry it - otherwise it vanishes from every list and is
    # only reachable by its direct URL.
    allowed = ["draft", "pending_approval", "approved", "scheduled",
               "rejected", "failed", "partially_published"]
    status_f = request.args.get("status")
    status_f = status_f if status_f in allowed else ""
    campaign_f = (request.args.get("campaign") or "").strip()
    q = _scope_posts(SocialPost.query, _client_arg())
    q = q.filter(SocialPost.status == status_f) if status_f \
        else q.filter(SocialPost.status.in_(allowed))
    if campaign_f:
        q = q.filter(SocialPost.campaign == campaign_f)
    posts = q.order_by(SocialPost.updated_at.desc()).limit(200).all()
    return render_template("social/drafts.html", posts=posts,
                           status_f=status_f, statuses=allowed,
                           campaign_f=campaign_f,
                           campaigns=_campaigns(_client_arg()))


@social_bp.route("/compose")
@login_required
def compose():
    _guard()
    # Default the schedule field to a sensible near-future time (one hour
    # out, IST), so "Schedule for later" - the default choice - is never left
    # blank. A blank time used to silently fall through to publishing
    # immediately; it is refused now, but a sane default avoids the dead-end.
    schedule_value = _to_ist_input(datetime.utcnow() + timedelta(hours=1))
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
        transcode_available=_transcode_available(),
        selected_account_ids=selected_account_ids,
        post_type="image",
        schedule_value=schedule_value,
        default_client_id=default_client_id,
        first_comment="",
        platform_captions={},
        platform_first_comments={},
        platform_schedules={},
        media_alt={},
        campaigns=_campaigns(default_client_id),
        reel_cover_key="", reel_thumb_offset="", reel_cover_url="",
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
    task_file_ids = []
    if first:
        task_file_ids = [m.task_file_id for m in first.media if m.task_file_id]
    # Per-platform caption overrides (any target whose caption differs from the
    # shared base) + the first comment (shared across targets).
    platform_captions = {
        t.platform: t.caption for t in post.targets
        if t.caption and t.caption != post.base_caption
    }
    first_comment = next(
        (t.first_comment for t in post.targets if t.first_comment), "")
    # Per-platform first-comment overrides = any target whose first comment
    # differs from the shared base (mirrors platform_captions).
    platform_first_comments = {
        t.platform: t.first_comment for t in post.targets
        if t.first_comment and t.first_comment != first_comment
    }
    # Per-platform schedule overrides = any target whose time differs from the
    # shared (first target's) time, as an IST datetime-local string.
    _first_when = first.scheduled_for if first else None
    platform_schedules = {
        t.platform: _to_ist_input(t.scheduled_for) for t in post.targets
        if t.scheduled_for and t.scheduled_for != _first_when
    }
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
                is_vid = (m.mime_type or "").startswith("video")
                # A URL for images AND videos, so the preview can play a video
                # on edit, not just show a poster.
                u = None
                if is_img or is_vid:
                    try:
                        u = pipeline.presigned_url(m.object_key)
                    except Exception:  # noqa: BLE001
                        u = None
                uploaded_media.append({
                    "object_key": m.object_key, "mime": m.mime_type or "",
                    "is_image": is_img, "url": u})
    # Per-media alt text (edit restore), keyed the same way the browser and
    # _measure_key build it, so each value lands back on its own input.
    media_alt = {}
    if first:
        for m in first.media:
            if not m.alt_text:
                continue
            if m.task_file_id:
                media_alt[f"task_file_ids|{m.task_file_id}"] = m.alt_text
            elif m.source == "upload" and m.object_key:
                media_alt[f"upload_media|{m.object_key}::{m.mime_type or ''}"] \
                    = m.alt_text
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
        transcode_available=_transcode_available(),
        selected_account_ids=account_ids,
        post_type=(first.post_type if first else "image"),
        schedule_value=_to_ist_input(first.scheduled_for if first else None),
        first_comment=first_comment or "",
        platform_captions=platform_captions,
        platform_first_comments=platform_first_comments,
        platform_schedules=platform_schedules,
        media_alt=media_alt,
        campaigns=_campaigns(post.client_id),
        reel_cover_key=post.reel_cover_key or "",
        reel_thumb_offset=(post.reel_thumb_offset
                           if post.reel_thumb_offset is not None else ""),
        reel_cover_url=_reel_cover_url(post),
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


def _existing_client_asset_ids(post):
    """Brand-asset media already attached to `post`, in order.

    The composer stopped offering brand assets, so this is the only source
    of them left. Read from the FIRST target, which is where the composer
    has always mirrored the shared selection from, and read before the
    rebuild below deletes the targets these rows hang off.

    Empty for a post being created, which is exactly why a post composed
    today never acquires one.
    """
    if post is None or post.id is None:
        return []

    targets = list(post.targets)
    if not targets:
        return []

    seen, out = set(), []
    for media in sorted(targets[0].media, key=lambda m: m.sort_order):
        if media.client_asset_id and media.client_asset_id not in seen:
            seen.add(media.client_asset_id)
            out.append(media.client_asset_id)
    return out


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

    # Set by the composer when the caption was drafted with AI Assist, so we
    # can later report how much output was AI-assisted. Advisory only.
    ai_assisted = bool(request.form.get("ai_assisted"))

    # Campaign label (grouping + utm_campaign) and whether to auto-tag links.
    post.campaign = (request.form.get("campaign") or "").strip()[:120] or None
    add_utm = bool(request.form.get("add_utm"))

    # Reel cover: a custom uploaded image (key), a frame picked from the video
    # (offset ms), or auto (neither). The mode radio decides which to keep so
    # switching modes can never leave a stale value behind.
    cover_mode = request.form.get("reel_cover_mode")
    if cover_mode == "upload":
        ck = (request.form.get("reel_cover_key") or "").strip()
        post.reel_cover_key = ck if ck.startswith("social_uploads/") else None
        post.reel_thumb_offset = None
    elif cover_mode == "frame":
        post.reel_cover_key = None
        try:
            off = int(request.form.get("reel_thumb_offset") or "")
            post.reel_thumb_offset = off if off >= 0 else None
        except (TypeError, ValueError):
            post.reel_thumb_offset = None
    else:                                   # auto / unset
        post.reel_cover_key = None
        post.reel_thumb_offset = None

    post_type = (request.form.get("post_type") or "image").strip()
    account_ids = request.form.getlist("account_ids", type=int)
    # ids arrive in the chosen order (carousel ordering) - preserved below.
    #
    # Brand assets are NOT read from the form: the composer no longer offers
    # them (a client's logo pack belongs to the client record, not to the
    # flow of writing a post). They are carried forward from whatever the
    # post already has instead - without that, opening an older post to fix
    # a typo would silently strip its media, and nothing would say so.
    asset_ids = _existing_client_asset_ids(post)
    task_file_ids = request.form.getlist("task_file_ids", type=int)
    publish_mode = request.form.get("publish_mode")
    publish_now = publish_mode == "now"
    use_queue = publish_mode == "queue"
    scheduled_for = (
        datetime.utcnow() if publish_now
        else _parse_schedule(request.form.get("schedule"))
    )

    # What the browser measured for each chosen file, keyed by object key.
    # Best-effort: bad JSON must not lose someone's post.
    measurements_by_key = {}
    try:
        raw = request.form.get("media_measurements")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                measurements_by_key = {
                    str(k): v for k, v in parsed.items() if isinstance(v, dict)
                }
    except (ValueError, TypeError):
        current_app.logger.warning("ignoring unreadable media_measurements")

    # Per-media alt text (accessibility), keyed the same way as measurements.
    alt_by_key = {}
    try:
        raw = request.form.get("media_alt")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                alt_by_key = {
                    str(k): str(v).strip() for k, v in parsed.items()
                    if str(v).strip()
                }
    except (ValueError, TypeError):
        current_app.logger.warning("ignoring unreadable media_alt")

    # The media that was there BEFORE this save, captured before the
    # rebuild wipes it - so a swap can be detected below.
    previous_media = sorted({
        m.object_key for t in post.targets for m in t.media if m.object_key
    })

    # Rebuild targets from scratch (drafts only) - simplest correct model.
    # Detach audit rows first: SocialAuditLog.target_id is a plain FK with no
    # cascade, so a "target_remapped" (or any) audit row pointing at a target
    # we're about to delete would otherwise 500 the save with an IntegrityError.
    old_target_ids = [t.id for t in post.targets]
    if old_target_ids:
        SocialAuditLog.query.filter(
            SocialAuditLog.target_id.in_(old_target_ids)
        ).update({"target_id": None}, synchronize_session=False)
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
            # form_value is kept so a browser measurement can be matched
            # back to this item - see _measure_key.
            media_items.append(("upload", SimpleNamespace(
                object_key=key, mime_type=(mime or None), id=None,
                form_value=raw)))

    # The slide order the composer's strip was showing. Until this existed the
    # sequence was an accident of the markup: every task file first (in
    # checkbox order), then brand assets, then uploads - so a carousel could
    # not be arranged at all, and the cover was whichever file the picker
    # happened to list first.
    #
    # Keyed by _measure_key, the same string the browser already builds to
    # match up measurements, so one vocabulary covers both.
    requested_order = [
        raw for raw in request.form.getlist("media_order") if raw
    ]
    if requested_order:
        rank = {key: i for i, key in enumerate(requested_order)}
        # Anything the strip did not know about - a brand asset carried over
        # from an existing post - keeps its position at the end rather than
        # jumping to the front on a sort it never took part in.
        media_items.sort(
            key=lambda item: rank.get(_measure_key(*item), len(rank)))

    def _add_media(target_id):
        for i, (source, obj) in enumerate(media_items):
            kw = dict(target_id=target_id, source=source,
                      object_key=obj.object_key, mime_type=obj.mime_type,
                      role="main", sort_order=i)
            if source == "task_file":
                kw["task_file_id"] = obj.id
            elif source == "client_asset":
                kw["client_asset_id"] = obj.id
            # What the file actually is, measured in the browser. Stored so
            # the pre-flight at schedule time can quote real numbers
            # without re-measuring - and so it survives on the record.
            measured = measurements_by_key.get(_measure_key(source, obj))
            if measured:
                kw["meta"] = {"measurements": measured}
            alt = alt_by_key.get(_measure_key(source, obj))
            if alt:
                kw["alt_text"] = alt
            db.session.add(SocialMediaAsset(**kw))

    def _new_target(account, ptype, caption, first_comment=None,
                    story_style="plain", story_link_target_id=None,
                    when=None):
        t = SocialPostTarget(
            social_post_id=post.id, social_account_id=account.id,
            platform=account.platform, post_type=ptype, caption=caption,
            # Not on a story: there is nothing to comment on, so storing it
            # there only makes the post page claim a first comment that was
            # never going anywhere. (The caller also gates this on the
            # channel's supports_first_comment.)
            first_comment=(None if ptype == "story" else first_comment),
            status="draft",
            # Per-channel time if the caller passed one, else the shared time.
            scheduled_for=(when if when is not None else scheduled_for),
            story_style=story_style if ptype == "story" else "plain",
            story_link_target_id=(
                story_link_target_id if ptype == "story" else None),
            ai_generated=ai_assisted,
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

    # Measurements of the first media item, taken in the browser when the
    # file was chosen (see compose.html). Drives the reel-vs-video choice
    # below; empty simply means "unmeasured", never "bad".
    first_measurement = (
        measurements_by_key.get(_measure_key(*media_items[0]))
        if media_items else None) or {}

    skipped = []
    remapped = []
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

        # One post, one approved file - but the platforms disagree about
        # what to call it. Instagram has no "video" type at all (a video
        # goes out as a Reel), and Facebook's Reels are stricter than
        # Instagram's. media/fit.py picks reel-first, falling back to
        # video, so the same content publishes everywhere it can as ONE
        # post rather than forcing a second post or a dropped platform.
        prov = registry.get(account.platform)
        caps = prov.capabilities if prov else None
        target_type, notes = media_fit.choose_post_type(
            post_type, caps, first_measurement)
        if target_type is None:
            # Keep the target so the post page can explain and offer a way
            # out, rather than silently publishing to fewer platforms.
            target_type = post_type
        if notes:
            remapped.append(f"{platform_label(account.platform)}: {notes[0]}")

        # Per-channel first comment: this platform's override if given, else
        # the shared one - but only where the channel can actually post a
        # comment (e.g. never on Google Business).
        fc_override = (
            request.form.get(f"first_comment_{account.platform}") or "").strip()
        target_fc = (fc_override or first_comment) \
            if (caps and caps.supports_first_comment) else None

        # UTM tagging: append utm_source (this platform) + campaign to every
        # link, per target, so a client's analytics attributes social traffic.
        target_caption = override or base_caption
        if add_utm:
            target_caption = utm.tag_text(
                target_caption, account.platform, campaign=post.campaign)
            if target_fc:
                target_fc = utm.tag_text(
                    target_fc, account.platform, campaign=post.campaign)

        # Per-channel schedule: this platform's own time if given, else the
        # shared one - or, in queue mode, the channel's next open slot. Ignored
        # for publish-now (everything goes ASAP). The model already stores
        # scheduled_for per target, so different channels can publish the same
        # post at their own best times / cadence.
        target_when = scheduled_for
        if not publish_now:
            chan_when = _parse_schedule(
                request.form.get(f"schedule_{account.platform}"))
            if chan_when is not None:
                target_when = chan_when       # explicit per-channel time wins
            elif use_queue:
                target_when = (queue_slots.next_open_slot(account.id)
                               or scheduled_for or datetime.utcnow())

        feed = _new_target(account, target_type, target_caption,
                           first_comment=target_fc,
                           story_style=story_style,
                           story_link_target_id=standalone_link_id,
                           when=target_when)
        n_created += 1
        # Optional companion Story (no caption - stories don't use one). It
        # ships at the same time as the feed post it accompanies.
        if also_story:
            prov = registry.get(account.platform)
            caps = prov.capabilities if prov else None
            if caps and caps.story_support and "story" in (caps.post_types or set()):
                _new_target(account, "story", None, story_style=story_style,
                            story_link_target_id=feed.id, when=target_when)

    if skipped:
        flash("Skipped channel(s) that belong to a different client: "
              + ", ".join(skipped) + ".", "info")

    # Say when a platform is getting something other than what was asked
    # for - a Reel going out as a plain Facebook video is worth knowing.
    if remapped:
        flash(" ".join(remapped), "info")

    # Media is locked once it has been submitted. Swapping the file on a
    # post that is awaiting approval would let it be approved on the
    # strength of content the reviewer never saw, so the post drops back to
    # draft and has to be resubmitted. Caption and schedule edits are
    # untouched by this - only the media matters.
    new_media = sorted({
        obj.object_key for _src, obj in media_items if obj.object_key
    })
    if (post.status == "pending_approval" and previous_media
            and new_media != previous_media):
        post.status = "draft"
        audit.record("media_changed_after_submit", post_id=post.id,
                     actor_id=current_user.id, task_id=post.task_id)
        flash(
            "The media changed, so this went back to draft — submit it "
            "again so the reviewer approves what will actually publish.",
            "info")

    # The new targets were attached by FK (social_post_id), not through the
    # relationship, so the collection loaded earlier in this function is now
    # stale (it still reads empty for a brand-new post). Expire it so callers
    # - notably the "action == submit and post.targets" gate - see the real
    # targets rather than the cached empty list, which used to leave a
    # just-submitted post sitting in draft.
    db.session.expire(post, ["targets"])

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
            is_video = (m.mime_type or "").startswith("video")
            url = None
            if is_image and m.object_key:
                try:
                    url = pipeline.presigned_url(m.object_key)
                except Exception:  # noqa: BLE001
                    url = None
            media_previews.append({
                "url": url, "is_image": is_image, "is_video": is_video,
                "object_key": m.object_key})
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

    # Schedule prefill: only pre-fill the single datetime field when EVERY
    # channel shares one time. When channels carry different (per-channel)
    # times, leave it blank - confirming "Schedule" then keeps each channel's
    # own time instead of silently collapsing them all to the first channel's.
    sched_times = [t.scheduled_for for t in post.targets if t.scheduled_for]
    distinct_times = set(sched_times)
    uniform_when = (
        next(iter(distinct_times))
        if len(distinct_times) == 1 and len(sched_times) == len(post.targets)
        else None)
    per_channel_schedule = len(distinct_times) > 1
    channel_times = (
        [(platform_label(t.platform), t.scheduled_for) for t in post.targets]
        if per_channel_schedule else [])

    return render_template(
        "social/post_detail.html",
        post=post,
        problems=problems,
        first_comment_events=first_comment_events,
        media_previews=media_previews,
        can_approve=can_publish(current_user),
        to_ist=_to_ist_input,
        to_ist_display=_to_ist_display,
        schedule_prefill=uniform_when,
        per_channel_schedule=per_channel_schedule,
        channel_times=channel_times,
    )


@social_bp.route("/posts/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)

    # Decided from the targets, not from post.status. The status string can be
    # stale: posts stranded by the rollup gap that lifecycle.post_status_from
    # closes are still sitting in the database carrying "publishing" or
    # "partially_published", and refusing on that string alone left them
    # permanently undeletable - which is half of what made them stuck.
    #
    # Only two things actually block a delete, and they are named explicitly
    # rather than derived from "is this post settled yet". Deriving it was
    # wrong in a way that mattered: an unsettled post is not necessarily a
    # busy one, and draft / pending_approval / approved / scheduled targets
    # are all unsettled - so treating "not settled" as "still publishing"
    # made ordinary drafts undeletable, which is most of what this route is
    # for.
    statuses = [t.status for t in post.targets]

    if any(s == "published" for s in statuses):
        flash("A published post cannot be deleted. Remove it from the "
              "platform first.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))

    if any(s == "publishing" for s in statuses):
        flash("This post is publishing right now - wait for it to finish.",
              "error")
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
    # Only a draft/rejected post may be submitted. Without this a stale or
    # replayed POST could regress an approved/scheduled/published post back to
    # pending_approval - the approve/reject/schedule routes are all guarded,
    # so this one must be too.
    if post.status not in ("draft", "rejected"):
        flash("This post can no longer be submitted for approval.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
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
    # Guard the current status (like approvals_bulk): a stale/direct POST must
    # not regress an already scheduled/published post back to "approved".
    if post.status != "pending_approval":
        flash("This post isn't awaiting approval.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
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
    if post.status != "pending_approval":
        flash("This post isn't awaiting approval.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))
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


@social_bp.route("/posts/<int:post_id>/review")
@login_required
def review_post(post_id):
    """What pressing Publish will actually do, as an HTML fragment.

    Fetched by publish-review.js when the schedule form is submitted. A GET
    that only reads: everything it reports comes from the same functions the
    publish path runs a moment later, so the review cannot promise one thing
    and the publish do another.
    """
    _guard()
    post = SocialPost.query.get_or_404(post_id)

    mode = request.args.get("publish_mode", "schedule")
    raw = request.args.get("schedule", "")

    review = publish_review.build_review(
        post,
        publish_mode=mode,
        schedule_override=_parse_schedule(raw),
        schedule_raw=raw,
    )

    return render_template("social/_publish_review.html", review=review,
                           post=post)


@social_bp.route("/posts/<int:post_id>/schedule", methods=["POST"])
@login_required
def schedule_post(post_id):
    _guard()
    post = SocialPost.query.get_or_404(post_id)
    if post.status != "approved":
        flash("Only an approved post can be scheduled.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))

    publish_now = request.form.get("publish_mode") == "now"
    raw_schedule = request.form.get("schedule", "")
    parsed = _parse_schedule(raw_schedule)
    now = datetime.utcnow()

    # The review the person actually read has to still describe this post.
    # Without this the dangerous case is silent: you open the review, someone
    # edits the post in another tab, you press Confirm - and publish something
    # you never saw. Mirrors how approve_task treats confirmed_platforms: the
    # modal is the UI, this is the gate.
    expected = publish_review.fingerprint(
        post, request.form.get("publish_mode", ""), raw_schedule)
    if request.form.get("review_fingerprint") != expected:
        flash("This post changed since you reviewed it — check what is going "
              "out and publish again.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))

    if not post.targets:
        flash("This post has no channels to schedule.", "error")
        return redirect(url_for("social.post_detail", post_id=post.id))

    if publish_now:
        # Explicit "publish now" - everything goes ASAP.
        for target in post.targets:
            target.scheduled_for = now
    else:
        if parsed is not None:
            # A single time typed into the form is a deliberate uniform
            # override applied to every channel.
            for target in post.targets:
                target.scheduled_for = parsed
        # else: no time in the form -> keep each channel's own scheduled time
        # (set at compose, possibly per-channel). The template blanks the
        # field precisely so this branch preserves divergent per-channel times
        # rather than collapsing them all to the first channel's.

        # A "scheduled" post must have a real future time on EVERY channel. A
        # blank one (the old "publish immediately" trap) and a past one (a
        # stale compose default, or a mistyped time) are both refused - use
        # "Publish now" to go out immediately, on purpose.
        if any(t.scheduled_for is None for t in post.targets):
            flash("Pick a date and time to schedule this post, or choose "
                  "Publish now.", "error")
            return redirect(url_for("social.post_detail", post_id=post.id))
        if any(t.scheduled_for < now - timedelta(minutes=1)
               for t in post.targets):
            flash("That schedule time is in the past. Pick a future time, or "
                  "choose Publish now.", "error")
            return redirect(url_for("social.post_detail", post_id=post.id))

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
    elif publish_now:
        flash(
            f"Publishing to {result['scheduled']} platform(s) now — it will "
            "go out in a moment.",
            "success",
        )
    else:
        flash(
            f"Scheduled {result['scheduled']} platform(s). Use "
            "“Process queue” (or the cron worker) to publish.",
            "success",
        )

    # Publish-now shouldn't wait for the next periodic worker tick: kick an
    # immediate background drain so it goes out within a second or two.
    if publish_now and result["scheduled"]:
        from app.social.queue import worker
        worker.kick_async(current_app._get_current_object())

    return redirect(url_for("social.post_detail", post_id=post.id))


@social_bp.route("/media/poster")
@login_required
def media_poster():
    """A cached video poster frame (generated once with ffmpeg). Templates
    point a video's <img> here; it 302s to the poster's presigned URL, or 404s
    so the UI keeps the generic video icon. Restricted to our own media keys."""
    _guard()
    key = (request.args.get("key") or "").strip()
    if (not key or ".." in key
            or not key.startswith(("social_uploads/", "clients/"))):
        abort(404)
    from app.social.media import poster
    url = poster.poster_url(key)
    if not url:
        abort(404)
    return redirect(url)


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
    # A presigned URL for BOTH images and videos, so the composer preview can
    # show the image or play the video inline (not just a poster).
    url = None
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


# ---------------------------------------------------------------------------
# AI assist (provider-agnostic - see app/ai/). Gated behind AI_ENABLED on top
# of the usual social guard; throttled so a stuck client can't run up a bill.
# Both routes return a DRAFT the user edits before saving - never auto-publish.
# ---------------------------------------------------------------------------

def _ai_guard():
    _guard()
    from app.ai import settings as ai_settings
    if not ai_settings.is_enabled():
        abort(503)


def _ai_error_response(exc):
    """Map a typed AI error to a clean JSON response. Never echoes a key."""
    from app.ai.errors import AIAuth, AIDisabled, AIPermanent, AITransient
    if isinstance(exc, AIDisabled):
        return jsonify(error="AI assist is not available."), 503
    if isinstance(exc, AITransient):
        return jsonify(error="The AI service is busy — try again in a moment."), 503
    if isinstance(exc, AIAuth):
        current_app.logger.error("[ai] provider auth failed")
        return jsonify(error="AI is misconfigured — contact an admin."), 502
    if isinstance(exc, AIPermanent):
        return jsonify(error="Couldn't generate that — please try again."), 502
    current_app.logger.exception("[ai] unexpected failure")
    return jsonify(error="Something went wrong with AI assist."), 500


def _csv(value):
    return [p.strip() for p in (value or "").split(",") if p.strip()]


# At most this many media items are fed to one caption call (carousel max),
# so a caller can't force reading an unbounded number of objects into memory.
_AI_MAX_CAPTION_MEDIA = 10


def _ai_can_view_task(task):
    """Task visibility, reusing the tasks blueprint's own rule (lazy import to
    avoid an import cycle). No task = nothing task-scoped to protect."""
    if task is None:
        return True
    from app.routes.tasks import can_view_task
    return can_view_task(task)


def _ai_media_allowed(object_key):
    """May the current user feed this storage object to the AI? Prevents an
    IDOR where a social-permitted user pulls an AI description/caption for a
    file they can't otherwise see:
      - ephemeral composer uploads (social_uploads/*) - unguessable keys the
        user just created through the authenticated upload route: allowed;
      - a task-file-backed key: allowed only if the user can view that task;
      - a client-asset key: allowed (client brand pages are already readable
        by any signed-in user);
      - an unknown key: denied.
    """
    if not object_key:
        return False
    if object_key.startswith("social_uploads/"):
        return True
    tf = TaskFile.query.filter_by(object_key=object_key).first()
    if tf is not None:
        return _ai_can_view_task(tf.task)
    if ClientAsset.query.filter_by(object_key=object_key).first() is not None:
        return True
    return False


@social_bp.route("/api/ai/caption", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def ai_caption_api():
    """Draft an on-brand, per-platform caption from the task brief + attached
    media + the client's brand knowledge base. Returns a draft the composer
    drops into the editable fields."""
    _ai_guard()
    from app.ai import service as ai_service

    task_id = request.form.get("task_id", type=int)
    client_id = request.form.get("client_id", type=int)
    platforms = _csv(request.form.get("platforms"))
    media_keys = _csv(request.form.get("media_keys"))

    task = Task.query.get(task_id) if task_id else None
    # The brief comes from the task, so a user must be allowed to see it.
    if task is not None and not _ai_can_view_task(task):
        return jsonify(error="You can't use that task."), 403

    client = None
    if client_id:
        client = Client.query.get(client_id)
    elif task is not None:
        client = getattr(task, "client", None)

    # Only feed media the user is allowed to see; drop the rest (caption still
    # works on the brief). Bounded count so one call can't read many objects.
    media_keys = [k for k in media_keys if _ai_media_allowed(k)][:_AI_MAX_CAPTION_MEDIA]

    brief = ""
    if task is not None:
        brief = "\n".join(p for p in (task.title, task.description) if p)

    if not brief and not media_keys:
        return jsonify(error="Add a brief (link a task) or some media first."), 400

    try:
        result = ai_service.generate_caption(
            brief=brief,
            industry=getattr(client, "industry", None),
            brand_voice=getattr(client, "brand_voice", None),
            brand_notes=getattr(client, "brand_guidelines_notes", None),
            platforms=platforms,
            media=[(k, None) for k in media_keys],
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a typed JSON response
        return _ai_error_response(exc)
    return jsonify(**result)


@social_bp.route("/api/ai/alt-text", methods=["POST"])
@login_required
@limiter.limit("120 per hour")
def ai_alt_text_api():
    """One accessible alt-text line for a single attached image."""
    _ai_guard()
    from app.ai import service as ai_service

    object_key = (request.form.get("object_key") or "").strip()
    if not object_key:
        return jsonify(error="No image selected."), 400
    # Alt-text describes the image, so gate the object strictly (see
    # _ai_media_allowed) - never describe a file the user can't see.
    if not _ai_media_allowed(object_key):
        return jsonify(error="You can't use that item."), 403
    try:
        alt = ai_service.generate_alt_text(object_key)
    except Exception as exc:  # noqa: BLE001 - mapped to a typed JSON response
        return _ai_error_response(exc)
    return jsonify(alt_text=alt)


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


def _published_post_rows(cid, limit=150):
    """One row per POST (not per target) for the Published list, Buffer-style:
    the originating task id, the title, and the platform icons it went live on.
    Posts published outside Studio are merged into the same list, flagged.
    """
    from app.utils import social_platforms as sp
    # Studio posts that have gone live on at least one platform...
    live_post_ids = {
        t.social_post_id for t in _scope_targets(
            SocialPostTarget.query.filter(
                SocialPostTarget.status.in_(["published", "removed"])), cid).all()
    }
    # ...plus posts marked published directly on the platform (no targets).
    live_post_ids.update(
        p.id for p in _scope_posts(
            SocialPost.query.filter(SocialPost.published_externally.is_(True)),
            cid).all())

    posts = (SocialPost.query
             .filter(SocialPost.id.in_(live_post_ids or {-1}))
             .order_by(SocialPost.updated_at.desc())
             .limit(limit).all())

    rows = []
    for post in posts:
        task = post.task
        del_targets = []
        if post.published_externally:
            src = (task.social_platforms_published or task.social_platforms) \
                if task else ""
            platforms, permalinks, status = sp.parse_platforms(src), {}, "external"
        else:
            live = [t for t in post.targets
                    if t.status in ("published", "removed")]
            platforms = []
            for t in live:
                if t.platform not in platforms:
                    platforms.append(t.platform)
            permalinks = {t.platform: t.permalink for t in live if t.permalink}
            # Still-live targets a user can take down per platform.
            del_targets = [{"id": t.id, "platform": t.platform}
                           for t in post.targets if t.status == "published"]
            statuses = [t.status for t in post.targets]
            # "blocked" (rate-gate refusal) settles a target just like
            # "failed" - a post live on some platforms but blocked/failed on
            # others is partially published, not cleanly published.
            if statuses and all(s == "removed" for s in statuses):
                status = "removed"
            elif any(s in ("failed", "blocked") for s in statuses) \
                    and any(s == "published" for s in statuses):
                status = "partially_published"
            else:
                status = "published"
        rows.append({
            "post": post,
            "task_code": task.task_code if task else None,
            "task_id": task.id if task else None,
            "external": post.published_externally,
            "platforms": platforms,
            "permalinks": permalinks,
            "del_targets": del_targets,
            "status": status,
            "when": post.updated_at,
        })
    return rows


@social_bp.route("/history")
@login_required
def history():
    _guard()
    cid = _client_arg()
    rows = _published_post_rows(cid)
    return render_template("social/history.html", rows=rows)


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
    from app.social.media import transcode
    engine_info = {
        "enabled": current_app.config.get("SOCIAL_ENGINE_ENABLED", False),
        "auto_worker": current_app.config.get("SOCIAL_INPROCESS_WORKER", True),
        "worker_interval": current_app.config.get("SOCIAL_WORKER_INTERVAL", 20),
        "simulation": bool(current_app.config.get("META_EMULATOR")),
        "cron_ready": bool(current_app.config.get("SOCIAL_WORKER_TOKEN")),
        "token_vault": bool(current_app.config.get("SOCIAL_TOKEN_KEY")),
        # Whether oversized videos can be auto-resized on publish (needs
        # ffmpeg on the host). Surfaced so an admin can see why a too-wide
        # reel blocked instead of resizing.
        "video_resize": transcode.available(),
    }
    # Each channel's posting-schedule slots as {account_id: [(weekday, "HH:MM")]}
    # for the "Add to queue" cadence, plus a best-time suggestion per channel.
    slots_by_account = {
        a.id: [(s.weekday, s.hhmm) for s in queue_slots.slots_for(a.id)]
        for a in accounts
    }
    suggested_by_account = {
        a.id: queue_slots.suggested_minutes(a.id) for a in accounts
    }
    return render_template(
        "social/settings.html", hashtag_sets=hashtag_sets, accounts=accounts,
        slots_by_account=slots_by_account,
        suggested_by_account=suggested_by_account,
        engine=engine_info, is_engine_admin=can_manage_social_engine(current_user))


@social_bp.route("/accounts/<int:account_id>/slots", methods=["POST"])
@login_required
def save_posting_slots(account_id):
    """Replace a channel's posting-schedule slots. The form submits one
    'slot' value per slot as 'weekday|HH:MM'; the whole set is rewritten."""
    _guard()
    account = SocialAccount.query.get_or_404(account_id)
    pairs = []
    for raw in request.form.getlist("slot"):
        day, _, hhmm = (raw or "").partition("|")
        try:
            h, m = hhmm.split(":")
            pairs.append((int(day), int(h) * 60 + int(m)))
        except (ValueError, TypeError):
            continue
    n = queue_slots.set_slots(account.id, pairs)
    flash(f"Saved {n} posting slot(s) for {account.display_name}.", "success")
    return redirect(url_for("social.settings", client=_client_arg()))


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
