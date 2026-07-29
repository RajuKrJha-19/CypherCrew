"""Cypher-Teams: the HTTP surface.

Two kinds of route live here and they have different rules.

Pages render the Teams shell and are ordinary Turbo-driven navigations.
The JSON API under /teams/api is what the open tab talks to; it is polled,
so every handler in it has to stay cheap enough that two gunicorn workers
can absorb one request per active tab every couple of seconds. The work is
in services/ - these functions validate, authorise, and get out of the way.

`GET /teams/api/sync` is deliberately the only poller. See
app/teams/services/sync.py for why, and for the seam that lets a push
transport replace it without touching the client.
"""

from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Client, Meeting, TeamChannel, TeamMessage, User
from app.teams.services import attachments as attachments_service
from app.teams.services import channels as channels_service
from app.teams.services import meetings as meetings_service
from app.teams.services import messages as messages_service
from app.teams.services import notify as notify_service
from app.teams.services import presence as presence_service
from app.teams.services import sync as sync_service
from app.teams.services import unread as unread_service
from app.teams.services.attachments import AttachmentError
from app.teams.services.channels import ChannelError
from app.teams.services.meetings import MeetingError
from app.teams.services.messages import MessageError

teams_bp = Blueprint("teams", __name__, url_prefix="/teams")


# ======================================================================
# Guards
# ======================================================================
# Teams has no permission of its own: talking to your colleagues is not a
# capability that needs granting, and a chat tool half the team cannot see
# is worse than no chat tool. The module flag gates the whole feature and
# channel membership gates each conversation - that is the entire model.

def _channel_or_404(channel_id):
    channel = db.session.get(TeamChannel, channel_id)
    if channel is None:
        abort(404)
    return channel


def _readable_or_404(channel_id):
    """Private channels and DMs are invisible to non-members - 404, not
    403, so their existence isn't confirmed to someone probing ids."""
    channel = _channel_or_404(channel_id)
    if not channels_service.can_read(channel, current_user):
        abort(404)
    return channel


def _postable_or_403(channel_id):
    channel = _readable_or_404(channel_id)
    if not channels_service.can_post(channel, current_user):
        abort(403)
    return channel


# ======================================================================
# Shell context
# ======================================================================

@teams_bp.context_processor
def _inject_teams_context():
    """Sidebar state for every Teams page.

    Wrapped in a bare except and degrading to empty defaults on purpose:
    this runs before every template in the module, and a failure here would
    blank the entire shell rather than the one panel that broke. Same
    contract as the Studio's context processor.
    """
    empty = {
        "teams_channels": [],
        "teams_dms": [],
        "teams_unread_total": 0,
        "teams_active_channel_id": None,
    }
    if not current_user.is_authenticated:
        return empty
    try:
        state = unread_service.channel_state(current_user)
        active = request.view_args.get("channel_id") if request.view_args else None
        return {
            "teams_channels": [r for r in state if not r["channel"].is_dm],
            "teams_dms": [r for r in state if r["channel"].is_dm],
            "teams_unread_total": sum(1 for r in state if r["unread"]),
            "teams_active_channel_id": active,
        }
    except Exception:
        return empty


# ======================================================================
# Pages
# ======================================================================

@teams_bp.route("/")
@login_required
def index():
    """Land in the most recently active conversation, because that is
    where you were going.

    Anyone with no conversations at all gets put into #general (created on
    the spot if this is the very first visit), so the module is never an
    empty room with a "create a channel" button in it.
    """
    state = unread_service.channel_state(current_user)

    if not state:
        default = channels_service.ensure_default_channel(current_user)
        if default is not None:
            return redirect(url_for("teams.channel", channel_id=default.id))
        return redirect(url_for("teams.browse"))

    unread = next((r for r in state if r["unread"]), None)
    target = unread or state[0]
    return redirect(url_for("teams.channel",
                            channel_id=target["channel"].id))


@teams_bp.route("/c/<int:channel_id>")
@login_required
def channel(channel_id):
    channel = _readable_or_404(channel_id)

    member = channels_service.membership(channel.id, current_user.id)
    history = messages_service.latest_page(channel.id)

    if member is not None and history:
        unread_service.mark_read(current_user, channel, history[-1].id)

    member_ids = presence_service.channel_member_ids(channel.id)

    # The same list find_mentioned_users matches against, so the picker can
    # never offer a name the server will not resolve. Imported lazily to
    # keep this blueprint from pulling in the whole tasks module at import.
    from app.routes.tasks import active_user_names

    return render_template(
        "teams/channel.html",
        channel=channel,
        member=member,
        messages=history,
        cursor=history[-1].id if history else 0,
        can_post=channels_service.can_post(channel, current_user),
        can_administer=channels_service.can_administer(channel, current_user),
        presence=presence_service.statuses_for(member_ids),
        members=channel.members.all(),
        mention_users=active_user_names(),
    )


@teams_bp.route("/browse")
@login_required
def browse():
    people = channels_service.dm_candidates(current_user)
    return render_template(
        "teams/browse.html",
        available=channels_service.browsable_channels(current_user),
        people=people,
        # Rendered server-side: with no channel open the sync tick carries
        # no presence, so this page would otherwise show everyone offline.
        presence=presence_service.statuses_for([p.id for p in people]),
    )


@teams_bp.route("/search")
@login_required
def search():
    query = (request.args.get("q") or "").strip()
    channel_id = request.args.get("channel", type=int)

    scope = None
    if channel_id:
        scope = db.session.get(TeamChannel, channel_id)
        if scope is not None and not channels_service.can_read(scope, current_user):
            scope = None

    return render_template(
        "teams/search.html",
        query=query,
        scope=scope,
        results=messages_service.search(
            current_user, query,
            channel_id=scope.id if scope is not None else None),
    )


@teams_bp.route("/channels/new", methods=["GET", "POST"])
@login_required
def channel_new():
    if request.method == "POST":
        try:
            client_id = request.form.get("client_id", type=int)
            channel = channels_service.create_channel(
                name=request.form.get("name"),
                creator=current_user,
                description=request.form.get("description"),
                visibility=request.form.get("visibility", "public"),
                client_id=client_id or None,
            )
        except ChannelError as exc:
            flash(str(exc), "error")
            return redirect(url_for("teams.channel_new"))

        flash(f"#{channel.key} created.", "success")
        return redirect(url_for("teams.channel", channel_id=channel.id))

    clients = (
        Client.query.filter_by(status="active")
        .order_by(Client.client_name.asc()).all()
    )
    return render_template("teams/channel_new.html", clients=clients)


@teams_bp.route("/dm/<int:user_id>")
@login_required
def dm(user_id):
    """Open (or create) the DM with someone. Idempotent - the channel key
    is derived from the pair, so this is safe to link to from anywhere."""
    other = db.session.get(User, user_id)
    if other is None or other.status != "active":
        abort(404)

    conversation = channels_service.get_or_create_dm(current_user, other)
    return redirect(url_for("teams.channel", channel_id=conversation.id))


# ======================================================================
# Meetings
# ======================================================================

def _meeting_or_404(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None:
        abort(404)
    if not meetings_service.can_join(meeting, current_user):
        abort(404)
    return meeting


@teams_bp.route("/meetings")
@login_required
def meetings():
    return render_template(
        "teams/meetings.html",
        live=meetings_service.live_for(current_user),
        upcoming=meetings_service.upcoming_for(current_user),
        past=meetings_service.past_for(current_user),
    )


@teams_bp.route("/meetings/new", methods=["GET", "POST"])
@login_required
def meeting_new():
    if request.method == "POST":
        try:
            meeting = meetings_service.schedule(
                title=request.form.get("title"),
                # The form is a datetime-local, which the browser gives us
                # in the user's own clock - IST for this team, and exactly
                # what meeting_date stores. See services/meetings.py.
                starts_at=_parse_local(request.form.get("meeting_date")),
                organiser=current_user,
                participants=_users(request.form.getlist("participant_ids")),
                agenda=request.form.get("agenda"),
                duration_minutes=request.form.get("duration_minutes", type=int),
                client_id=request.form.get("client_id", type=int),
                channel=meetings_service.channel_for(
                    request.form.get("channel_id", type=int)),
            )
        except MeetingError as exc:
            flash(str(exc), "error")
            return redirect(url_for("teams.meeting_new"))

        _announce_meeting(meeting, "scheduled")
        flash(f"“{meeting.title}” scheduled.", "success")
        return redirect(url_for("teams.meeting_detail", meeting_id=meeting.id))

    return render_template(
        "teams/meeting_new.html",
        people=meetings_service.invitable_users(exclude_id=current_user.id),
        clients=Client.query.filter_by(status="active")
        .order_by(Client.client_name.asc()).all(),
        channels=[r["channel"] for r in unread_service.channel_state(current_user)
                  if not r["channel"].is_dm],
    )


@teams_bp.route("/meetings/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):
    meeting = _meeting_or_404(meeting_id)
    return render_template(
        "teams/meeting_detail.html",
        meeting=meeting,
        joinable=meetings_service.is_joinable(meeting),
        live=meetings_service.is_live(meeting),
        ends_at=meetings_service.ends_at(meeting),
        is_organiser=meeting.created_by_id == current_user.id,
    )


@teams_bp.route("/meetings/<int:meeting_id>/join")
@login_required
def meeting_join(meeting_id):
    meeting = _meeting_or_404(meeting_id)

    if not meetings_service.is_joinable(meeting):
        flash("That meeting is not open.", "error")
        return redirect(url_for("teams.meeting_detail", meeting_id=meeting.id))

    context = meetings_service.join_context(meeting, current_user)
    if context is None:
        flash("No meeting provider is configured on this server.", "error")
        return redirect(url_for("teams.meeting_detail", meeting_id=meeting.id))

    was_live = meetings_service.is_live(meeting)
    meetings_service.mark_started(meeting)
    if not was_live:
        _announce_meeting(meeting, "started")

    return render_template(
        "teams/meeting_room.html",
        meeting=meeting,
        embed=context["embed"],
        fallback_url=context["fallback_url"],
    )


@teams_bp.route("/meetings/<int:meeting_id>/end", methods=["POST"])
@login_required
def meeting_end(meeting_id):
    meeting = _meeting_or_404(meeting_id)
    # Only the organiser closes a meeting for everyone. Anyone else leaving
    # is just them closing a tab.
    if meeting.created_by_id != current_user.id:
        abort(403)

    meetings_service.end(meeting)
    flash("Meeting ended.", "success")
    return redirect(url_for("teams.meeting_detail", meeting_id=meeting.id))


@teams_bp.route("/c/<int:channel_id>/call", methods=["POST"])
@login_required
def channel_call(channel_id):
    """Start a call from a conversation, inviting everyone in it."""
    channel = _postable_or_403(channel_id)

    meeting = meetings_service.start_now(
        title=request.form.get("title") or f"Call in {channel.display_name(current_user)}",
        organiser=current_user,
        channel=channel,
    )
    _announce_meeting(meeting, "started")

    return redirect(url_for("teams.meeting_join", meeting_id=meeting.id))


def _announce_meeting(meeting, event):
    """Drop a meeting card into the channel the meeting belongs to.

    A system message rather than a table of its own: it is a thing that
    happened in the conversation, it should sit in the history where it
    happened, and it costs no schema.
    """
    if not meeting.channel_id:
        return
    channel = db.session.get(TeamChannel, meeting.channel_id)
    if channel is None:
        return
    try:
        messages_service.post_message(
            channel=channel,
            author=current_user,
            body=None,
            kind="meeting",
            meta={"meeting_id": meeting.id, "event": event,
                  "title": meeting.title},
            has_attachments=True,      # "this message needs no body"
        )
    except Exception:                                        # noqa: BLE001
        current_app.logger.exception("[teams-meeting] announce failed")


def _users(ids):
    wanted = [i for i in (_as_int(v) for v in ids) if i]
    if not wanted:
        return []
    return User.query.filter(User.id.in_(wanted), User.status == "active").all()


def _parse_local(value):
    """A browser datetime-local string -> naive datetime in the same clock.

    Deliberately NOT converted to UTC: meeting_date is IST-naive, and the
    calendar and dashboard both read it that way.
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


# ======================================================================
# Channel membership
# ======================================================================

@teams_bp.route("/c/<int:channel_id>/join", methods=["POST"])
@login_required
def join_channel(channel_id):
    channel = _channel_or_404(channel_id)

    # You can only walk into a public channel. Private ones need an invite,
    # and a DM is not something you can join at all.
    if channel.kind != "channel" or channel.visibility != "public":
        abort(404)
    if channel.is_archived:
        flash("That channel is archived.", "error")
        return redirect(url_for("teams.browse"))

    channels_service.add_member(channel, current_user)
    return redirect(url_for("teams.channel", channel_id=channel.id))


@teams_bp.route("/c/<int:channel_id>/settings", methods=["GET", "POST"])
@login_required
def channel_settings(channel_id):
    channel = _readable_or_404(channel_id)
    if channel.is_dm:
        abort(404)

    # Membership is enough to look; changing anything is the owner's.
    if channels_service.membership(channel.id, current_user.id) is None:
        abort(404)

    if request.method == "POST":
        if not channels_service.can_administer(channel, current_user):
            abort(403)
        try:
            _apply_channel_settings(channel, request.form)
        except ChannelError as exc:
            flash(str(exc), "error")
        return redirect(url_for("teams.channel_settings", channel_id=channel.id))

    return render_template(
        "teams/channel_settings.html",
        channel=channel,
        members=channels_service.members_of(channel),
        addable=channels_service.addable_users(channel),
        can_administer=channels_service.can_administer(channel, current_user),
        muted=channels_service.is_muted(channel.id, current_user.id),
    )


def _apply_channel_settings(channel, form):
    """One POST handler for the settings form's several buttons.

    Dispatched on an `action` field rather than split across routes: they
    all edit the same object, they all return to the same page, and four
    routes would mean four permission checks to keep in step.
    """
    action = (form.get("action") or "details").strip()

    if action == "details":
        channels_service.rename_channel(
            channel, form.get("name"), form.get("description"))
        flash("Channel updated.", "success")

    elif action == "add_member":
        person = db.session.get(User, _as_int(form.get("user_id")))
        if person is None or person.status != "active":
            raise ChannelError("That person cannot be added.")
        channels_service.add_member(channel, person)
        flash(f"{person.name} added.", "success")

    elif action == "remove_member":
        person = db.session.get(User, _as_int(form.get("user_id")))
        if person is None:
            raise ChannelError("That person is not in this channel.")
        if person.id == current_user.id:
            # Removing yourself through the members list would silently be
            # a "leave", which has its own button and its own redirect.
            raise ChannelError("Use Leave channel to remove yourself.")
        channels_service.remove_member(channel, person)
        flash(f"{person.name} removed.", "success")

    elif action == "archive":
        channels_service.archive_channel(channel)
        flash("Channel archived. It is now read-only.", "success")

    elif action == "unarchive":
        channels_service.unarchive_channel(channel)
        flash("Channel restored.", "success")


@teams_bp.route("/c/<int:channel_id>/mute", methods=["POST"])
@login_required
def channel_mute(channel_id):
    """Mute is per person, so it needs membership - not ownership."""
    channel = _readable_or_404(channel_id)
    muted = request.form.get("muted") == "1"
    channels_service.set_muted(channel, current_user, muted)

    flash("Channel muted." if muted else "Channel unmuted.", "success")
    return redirect(request.referrer
                    or url_for("teams.channel", channel_id=channel.id))


@teams_bp.route("/c/<int:channel_id>/leave", methods=["POST"])
@login_required
def leave_channel(channel_id):
    channel = _readable_or_404(channel_id)
    if channel.is_dm:
        abort(404)

    channels_service.remove_member(channel, current_user)
    flash(f"You left #{channel.key}.", "success")
    return redirect(url_for("teams.index"))


# ======================================================================
# JSON API
# ======================================================================
# CSRF is handled globally: static/js/csrf.js attaches X-CSRFToken to every
# mutating fetch, and CSRFProtect validates it. Nothing here is exempt, and
# nothing here should be.

@teams_bp.route("/api/sync")
@login_required
def api_sync():
    """One tick. The only endpoint the open tab polls."""
    channel_id = request.args.get("channel", type=int)
    channel = None
    if channel_id:
        channel = db.session.get(TeamChannel, channel_id)
        if channel is None or not channels_service.can_read(channel, current_user):
            # Don't 404 a poll - the channel may have just been archived or
            # the user removed. Answer with the shell state so the sidebar
            # still updates and the client can navigate away on its own.
            channel = None

    payload = sync_service.build_sync_payload(
        current_user,
        channel=channel,
        after_id=request.args.get("after", type=int) or 0,
        since=request.args.get("since"),
        thread_root_id=request.args.get("thread", type=int),
        thread_after_id=request.args.get("tafter", type=int) or 0,
        typing=request.args.get("typing") == "1",
        focused=request.args.get("focus", "1") != "0",
    )

    response = jsonify(payload)
    # A poll response is never reusable, and an intermediary caching one
    # would freeze the conversation.
    response.headers["Cache-Control"] = "no-store"
    return response


@teams_bp.route("/api/channels/<int:channel_id>/messages", methods=["POST"])
@login_required
def api_post_message(channel_id):
    channel = _postable_or_403(channel_id)
    data = request.get_json(silent=True) or request.form

    try:
        message = messages_service.post_message(
            channel=channel,
            author=current_user,
            body=data.get("body"),
            client_msg_id=data.get("client_msg_id"),
            parent_id=_as_int(data.get("parent_id")),
        )
    except MessageError as exc:
        return jsonify({"error": str(exc)}), 400

    presence_service.clear_typing(current_user, channel.id)
    # The author has by definition read their own message.
    unread_service.mark_read(current_user, channel, message.id)
    # Mentions and DMs only - see the module docstring in services/notify.py
    # for why ordinary channel traffic is deliberately silent.
    notify_service.notify_message(
        message, channel, current_user,
        link=url_for("teams.channel", channel_id=channel.id) + f"#tm-{message.id}",
    )

    return jsonify({
        "ok": True,
        "message": sync_service.render_message(message, current_user),
    })


#: A single message cannot carry an unbounded number of files - the bubble
#: stops being readable, and each one costs a presigned URL to render.
MAX_ATTACHMENTS = 10


@teams_bp.route("/api/channels/<int:channel_id>/upload", methods=["POST"])
@login_required
def api_upload(channel_id):
    """Post a message with files attached.

    One request rather than an upload endpoint plus a send endpoint: the
    message and its files have to arrive together or a failed second call
    leaves an orphaned upload nobody can see, and a message that flickers
    on screen without its image.
    """
    channel = _postable_or_403(channel_id)

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify({"error": "No file was provided."}), 400
    if len(files) > MAX_ATTACHMENTS:
        return jsonify({
            "error": f"Up to {MAX_ATTACHMENTS} files per message."
        }), 400

    # Store first, then write rows. An upload that fails half way through
    # leaves objects in the bucket but no message - which the media GC
    # collects - rather than a message referring to a file that is not there.
    stored = []
    try:
        for file_storage in files:
            stored.append(attachments_service.store(file_storage, channel))
    except AttachmentError as exc:
        for item in stored:
            attachments_service.discard(item["object_key"])
        return jsonify({"error": str(exc)}), 400

    try:
        message = messages_service.post_message(
            channel=channel,
            author=current_user,
            body=request.form.get("body"),
            client_msg_id=request.form.get("client_msg_id"),
            parent_id=_as_int(request.form.get("parent_id")),
            has_attachments=True,
            commit=False,
        )
        attachments_service.attach(message, stored, commit=False)
        db.session.commit()
    except MessageError as exc:
        db.session.rollback()
        for item in stored:
            attachments_service.discard(item["object_key"])
        return jsonify({"error": str(exc)}), 400

    presence_service.clear_typing(current_user, channel.id)
    unread_service.mark_read(current_user, channel, message.id)
    notify_service.notify_message(
        message, channel, current_user,
        link=url_for("teams.channel", channel_id=channel.id) + f"#tm-{message.id}",
    )

    return jsonify({
        "ok": True,
        "message": sync_service.render_message(message, current_user),
    })


@teams_bp.route("/api/messages/<int:message_id>", methods=["PATCH", "DELETE"])
@login_required
def api_edit_message(message_id):
    message = db.session.get(TeamMessage, message_id)
    if message is None:
        abort(404)
    _readable_or_404(message.channel_id)

    try:
        if request.method == "DELETE":
            messages_service.delete_message(message, current_user)
        else:
            data = request.get_json(silent=True) or request.form
            messages_service.edit_message(
                message, current_user, data.get("body"))
    except MessageError as exc:
        return jsonify({"error": str(exc)}), 403

    return jsonify({
        "ok": True,
        "message": sync_service.render_message(message, current_user),
    })


@teams_bp.route("/api/messages/<int:message_id>/react", methods=["POST"])
@login_required
def api_react(message_id):
    message = db.session.get(TeamMessage, message_id)
    if message is None:
        abort(404)
    _postable_or_403(message.channel_id)

    data = request.get_json(silent=True) or request.form
    try:
        added = messages_service.toggle_reaction(
            message, current_user, data.get("emoji"))
    except MessageError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "ok": True,
        "added": added,
        "message": sync_service.render_message(message, current_user),
    })


@teams_bp.route("/api/channels/<int:channel_id>/read", methods=["POST"])
@login_required
def api_mark_read(channel_id):
    channel = _readable_or_404(channel_id)
    data = request.get_json(silent=True) or request.form

    member = unread_service.mark_read(
        current_user, channel, _as_int(data.get("up_to")))

    return jsonify({
        "ok": True,
        "last_read_message_id": member.last_read_message_id if member else None,
    })


def _as_int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
