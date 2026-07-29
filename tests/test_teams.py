"""Cypher-Teams: channels, messages, the poll cursor and unread state.

Each test here maps to a specific way the design could quietly break.
The polling contract in particular has two failure modes that are
invisible in a single-browser click-through and obvious in daily use:
a message delivered twice, and a deletion never delivered at all.
"""

import pytest

from app.extensions import db
from app.models import TeamChannel, TeamChannelMember, TeamMessage
from app.teams.services import channels as channels_service
from app.teams.services import messages as messages_service
from app.teams.services import notify as notify_service
from app.teams.services import unread as unread_service
from app.teams.services.channels import ChannelError
from app.teams.services.messages import MessageError
from tests.conftest import PYTEST_EMAIL_PREFIX

#: `meetings` holds real rows and is deliberately never truncated, so test
#: meetings fence themselves by title exactly the way test tasks and
#: clients do. _purge_test_rows deletes on this prefix.
PYTEST_MEETING_PREFIX = PYTEST_EMAIL_PREFIX


@pytest.fixture()
def people(session, make_user):
    """Two colleagues, both active."""
    return make_user(role="admin"), make_user(role="employee")


@pytest.fixture()
def channel(session, people):
    author, _ = people
    return channels_service.create_channel("Design Team", author)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def test_create_channel_slugifies_and_enrols_creator(session, people):
    author, _ = people
    channel = channels_service.create_channel("Design Team", author)

    assert channel.key == "design-team"
    assert channel.name == "Design Team"

    member = channels_service.membership(channel.id, author.id)
    assert member is not None
    assert member.role == "owner"


def test_duplicate_channel_name_is_rejected(session, people):
    author, _ = people
    channels_service.create_channel("Design Team", author)

    with pytest.raises(ChannelError):
        channels_service.create_channel("design team", author)


def test_joining_twice_is_a_no_op(session, channel, people):
    _, other = people
    first = channels_service.add_member(channel, other)
    second = channels_service.add_member(channel, other)

    assert first.id == second.id
    assert TeamChannelMember.query.filter_by(
        channel_id=channel.id, user_id=other.id).count() == 1


def test_joining_does_not_inherit_the_backlog(session, channel, people):
    """Walking into a channel with 4,000 old messages must not hand you
    4,000 unread - the read cursor starts at the current end."""
    author, other = people
    messages_service.post_message(channel, author, "before you arrived")

    channels_service.add_member(channel, other)
    member = channels_service.membership(channel.id, other.id)

    assert member.last_read_message_id == channel.last_message_id
    assert unread_service.unread_counts(other).get(channel.id) is None


def test_private_channel_is_invisible_to_non_members(session, people):
    author, other = people
    private = channels_service.create_channel(
        "Leadership", author, visibility="private")

    assert channels_service.can_read(private, author) is True
    assert channels_service.can_read(private, other) is False


def test_public_channel_is_readable_but_not_postable_before_joining(
        session, channel, people):
    _, other = people
    assert channels_service.can_read(channel, other) is True
    assert channels_service.can_post(channel, other) is False


def test_archived_channel_is_read_only(session, channel, people):
    author, _ = people
    channels_service.archive_channel(channel)

    assert channels_service.can_read(channel, author) is True
    assert channels_service.can_post(channel, author) is False


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------

def test_dm_is_the_same_conversation_from_both_sides(session, people):
    author, other = people
    one = channels_service.get_or_create_dm(author, other)
    two = channels_service.get_or_create_dm(other, author)

    assert one.id == two.id
    assert one.kind == "dm"
    assert one.visibility == "private"
    assert TeamChannel.query.filter_by(kind="dm").count() == 1


def test_dm_has_exactly_both_members(session, people):
    author, other = people
    conversation = channels_service.get_or_create_dm(author, other)

    member_ids = {m.user_id for m in conversation.members}
    assert member_ids == {author.id, other.id}
    assert conversation.other_member(author).id == other.id
    assert conversation.other_member(other).id == author.id


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def test_posting_advances_the_channel_pointers(session, channel, people):
    author, _ = people
    message = messages_service.post_message(channel, author, "hello")

    assert channel.last_message_id == message.id
    assert channel.last_message_at == message.created_at


def test_empty_message_is_rejected(session, channel, people):
    author, _ = people
    with pytest.raises(MessageError):
        messages_service.post_message(channel, author, "   ")


def test_same_client_msg_id_posts_once(session, channel, people):
    """A retried send after a timeout must resolve to the message that
    already exists, not create a second copy."""
    author, _ = people
    first = messages_service.post_message(
        channel, author, "hello", client_msg_id="c-abc")
    second = messages_service.post_message(
        channel, author, "hello", client_msg_id="c-abc")

    assert first.id == second.id
    assert TeamMessage.query.filter_by(channel_id=channel.id).count() == 1


def test_reply_sets_thread_root_and_bumps_reply_count(session, channel, people):
    author, _ = people
    root = messages_service.post_message(channel, author, "question?")
    reply = messages_service.post_message(
        channel, author, "answer", parent_id=root.id)

    assert reply.parent_id == root.id
    assert reply.thread_root_id == root.id
    assert root.reply_count == 1


def test_reply_to_a_reply_flattens_onto_the_same_root(session, channel, people):
    author, _ = people
    root = messages_service.post_message(channel, author, "question?")
    reply = messages_service.post_message(
        channel, author, "answer", parent_id=root.id)
    nested = messages_service.post_message(
        channel, author, "follow-up", parent_id=reply.id)

    assert nested.thread_root_id == root.id


def test_only_the_author_can_edit(session, channel, people):
    author, other = people
    message = messages_service.post_message(channel, author, "mine")

    with pytest.raises(MessageError):
        messages_service.edit_message(message, other, "yours")


def test_delete_is_soft_and_clears_the_text(session, channel, people):
    author, _ = people
    message = messages_service.post_message(channel, author, "oops")
    messages_service.delete_message(message, author)

    assert message.is_deleted is True
    assert message.body is None
    # The row must survive: the id is the poll cursor, and a hard delete
    # could never be reported to a client that already holds the message.
    assert db.session.get(TeamMessage, message.id) is not None


def test_reaction_toggles_and_is_idempotent(session, channel, people):
    author, other = people
    message = messages_service.post_message(channel, author, "ship it")

    assert messages_service.toggle_reaction(message, other, "🎉") is True
    assert len(message.reactions) == 1
    assert messages_service.toggle_reaction(message, other, "🎉") is False
    assert len(message.reactions) == 0


def test_reaction_moves_the_change_cursor(session, channel, people):
    """Reactions live in another table, so SQLAlchemy's onupdate never
    fires. If the service does not bump updated_at by hand, the change
    sweep cannot see them and a reaction appears only for the person who
    clicked it."""
    author, other = people
    message = messages_service.post_message(channel, author, "ship it")
    before = message.updated_at

    messages_service.toggle_reaction(message, other, "🎉")

    assert message.updated_at > before


# ---------------------------------------------------------------------------
# The poll cursor
# ---------------------------------------------------------------------------

def test_delta_returns_only_messages_after_the_cursor(session, channel, people):
    author, _ = people
    first = messages_service.post_message(channel, author, "one")
    messages_service.post_message(channel, author, "two")
    messages_service.post_message(channel, author, "three")

    fresh, has_more = messages_service.messages_after(channel.id, first.id)

    assert [m.body for m in fresh] == ["two", "three"]
    assert has_more is False


def test_delta_reports_more_when_truncated(session, channel, people):
    author, _ = people
    for n in range(5):
        messages_service.post_message(channel, author, f"m{n}")

    fresh, has_more = messages_service.messages_after(channel.id, 0, limit=2)

    assert len(fresh) == 2
    assert has_more is True


def test_deletion_is_delivered_by_the_change_sweep(session, channel, people):
    """The whole reason updated_at exists. An id cursor can never report a
    deletion, so without this sweep a message deleted on one screen stays
    on every other screen until someone reloads."""
    author, _ = people
    message = messages_service.post_message(channel, author, "recall this")
    held_cursor = message.id
    since = message.updated_at

    messages_service.delete_message(message, author)

    fresh, _ = messages_service.messages_after(channel.id, held_cursor)
    changed = messages_service.messages_changed(
        channel.id, since, held_cursor)

    assert fresh == []                       # nothing new to append
    assert [m.id for m in changed] == [message.id]
    assert changed[0].is_deleted is True


def test_change_sweep_never_re_delivers_a_new_message(session, channel, people):
    """A message must arrive as `messages` OR `changed`, never both -
    otherwise the client appends it and then replaces it, and anything
    keyed on arrival order flickers."""
    author, _ = people
    old = messages_service.post_message(channel, author, "old")
    since = old.updated_at
    new = messages_service.post_message(channel, author, "new")

    fresh, _ = messages_service.messages_after(channel.id, old.id)
    changed = messages_service.messages_changed(channel.id, since, old.id)

    assert [m.id for m in fresh] == [new.id]
    assert new.id not in [m.id for m in changed]


# ---------------------------------------------------------------------------
# Unread
# ---------------------------------------------------------------------------

def test_unread_ignores_your_own_messages(session, channel, people):
    author, _ = people
    messages_service.post_message(channel, author, "talking to myself")

    assert unread_service.unread_counts(author).get(channel.id) is None


def test_unread_counts_a_colleagues_messages(session, channel, people):
    author, other = people
    channels_service.add_member(channel, other)
    messages_service.post_message(channel, author, "one")
    messages_service.post_message(channel, author, "two")

    assert unread_service.unread_counts(other).get(channel.id) == 2
    assert unread_service.total_unread(other) == 1


def test_marking_read_clears_unread(session, channel, people):
    author, other = people
    channels_service.add_member(channel, other)
    message = messages_service.post_message(channel, author, "look")

    unread_service.mark_read(other, channel, message.id)

    assert unread_service.unread_counts(other).get(channel.id) is None
    assert unread_service.total_unread(other) == 0


def test_read_cursor_never_moves_backwards(session, channel, people):
    """A slow tab replaying an old position must not resurrect messages
    the user has already read."""
    author, other = people
    channels_service.add_member(channel, other)
    first = messages_service.post_message(channel, author, "one")
    second = messages_service.post_message(channel, author, "two")

    unread_service.mark_read(other, channel, second.id)
    unread_service.mark_read(other, channel, first.id)

    member = channels_service.membership(channel.id, other.id)
    assert member.last_read_message_id == second.id


def test_deleted_messages_do_not_count_as_unread(session, channel, people):
    author, other = people
    channels_service.add_member(channel, other)
    message = messages_service.post_message(channel, author, "ignore me")
    messages_service.delete_message(message, author)

    assert unread_service.unread_counts(other).get(channel.id) is None


def test_channel_state_is_one_query_regardless_of_channel_count(
        session, people):
    """The sidebar builds from a single joined read. This is the first
    thing that would silently regress into an N+1, and the cost would only
    show up under a 2-second poll in production."""
    from sqlalchemy import event

    author, _ = people
    for n in range(6):
        channels_service.create_channel(f"Channel {n}", author)

    statements = []

    def record(conn, cursor, statement, params, context, executemany):
        if "teams_" in statement.lower():
            statements.append(statement)

    engine = db.session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        state = unread_service.channel_state(author)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(state) == 6
    assert len(statements) == 1, f"expected 1 query, got {len(statements)}"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_sync_endpoint_returns_the_shell_payload(
        session, client, login, channel, people):
    author, _ = people
    messages_service.post_message(channel, author, "hello")
    login(author)

    response = client.get(f"/teams/api/sync?channel={channel.id}&after=0")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"

    payload = response.get_json()
    assert payload["next_poll_ms"] > 0
    assert payload["cursor"]
    assert len(payload["messages"]) == 1
    # The server renders the bubble; there is deliberately no second
    # renderer on the client to drift away from teams/_message.html.
    assert "tm-msg" in payload["messages"][0]["html"]


def test_posting_through_the_api_stores_and_renders(
        session, client, login, channel, people):
    author, _ = people
    login(author)

    response = client.post(
        f"/teams/api/channels/{channel.id}/messages",
        json={"body": "from the wire", "client_msg_id": "c-wire"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["message"]["cid"] == "c-wire"
    assert TeamMessage.query.filter_by(client_msg_id="c-wire").count() == 1


def test_non_member_cannot_post_to_a_public_channel(
        session, client, login, channel, people):
    _, other = people
    login(other)

    response = client.post(
        f"/teams/api/channels/{channel.id}/messages",
        json={"body": "barging in"},
    )

    assert response.status_code == 403


def test_private_channel_is_a_404_not_a_403(
        session, client, login, people):
    """403 would confirm the channel exists to someone probing ids."""
    author, other = people
    private = channels_service.create_channel(
        "Leadership", author, visibility="private")
    login(other)

    assert client.get(f"/teams/c/{private.id}").status_code == 404


def test_dm_route_is_idempotent(session, client, login, people):
    author, other = people
    login(author)

    first = client.get(f"/teams/dm/{other.id}")
    second = client.get(f"/teams/dm/{other.id}")

    assert first.status_code == 302
    assert first.headers["Location"] == second.headers["Location"]
    assert TeamChannel.query.filter_by(kind="dm").count() == 1


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
# Uploads stream through a worker, so the two things that matter are the
# size cap actually holding and the stored content type being the
# sanitised one - an SVG served from a presigned URL is stored XSS.

def _upload(client, channel_id, files, body="", cid=None):
    data = {"body": body}
    if cid:
        data["client_msg_id"] = cid
    data["files"] = files
    return client.post(
        f"/teams/api/channels/{channel_id}/upload",
        data=data, content_type="multipart/form-data",
    )


@pytest.fixture()
def fake_r2(monkeypatch):
    """Capture uploads instead of talking to R2.

    Reads the stream to the end so the size cap - which counts bytes as
    boto3 pulls them - is genuinely exercised rather than bypassed.
    """
    from app.storage.storage_service import StorageService

    stored = {}

    def _upload_file(self, *, file_obj, object_key, content_type=None):
        chunks = []
        while True:
            chunk = file_obj.read(8192)
            if not chunk:
                break
            chunks.append(chunk)
        stored[object_key] = {
            "bytes": b"".join(chunks), "content_type": content_type,
        }
        return object_key

    monkeypatch.setattr(
        "app.storage.r2_provider.R2StorageProvider.upload_file", _upload_file)
    monkeypatch.setattr(StorageService, "delete",
                        lambda self, object_key: stored.pop(object_key, None))
    # Keyword-only, exactly like the real StorageService.preview_url. The
    # first version of this fake took it positionally, which let a
    # positional call site pass the tests and then render every image as a
    # file chip in the browser. A fake that is laxer than the thing it
    # replaces tests nothing.
    monkeypatch.setattr(
        StorageService, "preview_url",
        lambda self, *, object_key, expires_in=3600: f"https://r2.test/{object_key}")
    return stored


def test_a_file_with_no_caption_is_still_a_message(
        session, client, login, channel, people, fake_r2):
    from io import BytesIO
    author, _ = people
    login(author)

    response = _upload(
        client, channel.id, [(BytesIO(b"PNGDATA"), "shot.png")], body="")

    assert response.status_code == 200
    message = TeamMessage.query.order_by(TeamMessage.id.desc()).first()
    assert message.body is None
    assert len(message.attachments) == 1
    assert message.attachments[0].filename == "shot.png"


def test_upload_records_the_real_byte_count(
        session, client, login, channel, people, fake_r2):
    from io import BytesIO
    author, _ = people
    login(author)

    _upload(client, channel.id, [(BytesIO(b"x" * 5000), "big.bin")])

    attachment = TeamMessage.query.order_by(
        TeamMessage.id.desc()).first().attachments[0]
    assert attachment.size_bytes == 5000


def test_a_file_over_the_cap_is_refused(
        session, client, login, channel, people, fake_r2, app):
    """The cap is counted while streaming, so a client that lies about
    Content-Length still cannot get past it."""
    from io import BytesIO
    author, _ = people
    login(author)
    app.config["TEAMS_ATTACHMENT_MAX_MB"] = 1

    before = TeamMessage.query.count()
    response = _upload(
        client, channel.id, [(BytesIO(b"x" * (2 * 1024 * 1024)), "huge.bin")])

    assert response.status_code == 400
    assert "MB or smaller" in response.get_json()["error"]
    # No message, and nothing left behind in the bucket.
    assert TeamMessage.query.count() == before
    assert fake_r2 == {}


def test_an_svg_is_stored_as_a_download_not_an_image(
        session, client, login, channel, people, fake_r2):
    """StorageService rewrites svg+xml to octet-stream. If that were
    bypassed, the inline <img> in a bubble would point at a script served
    from a presigned URL."""
    from io import BytesIO
    author, _ = people
    login(author)

    _upload(client, channel.id,
            [(BytesIO(b"<svg onload=alert(1)>"), "evil.svg", "image/svg+xml")])

    attachment = TeamMessage.query.order_by(
        TeamMessage.id.desc()).first().attachments[0]
    assert attachment.content_type == "application/octet-stream"
    assert attachment.is_image is False


def test_attachment_keys_are_scoped_to_the_channel(
        session, client, login, channel, people, fake_r2):
    from io import BytesIO
    author, _ = people
    login(author)

    _upload(client, channel.id, [(BytesIO(b"data"), "a.png")])

    key = TeamMessage.query.order_by(
        TeamMessage.id.desc()).first().attachments[0].object_key
    assert key.startswith(f"teams/channels/{channel.id}/")
    # Prefixed so a retention rule can expire chat media without touching
    # client deliverables or social uploads.
    assert key.startswith("teams/")


def test_a_non_member_cannot_upload(
        session, client, login, channel, people, fake_r2):
    from io import BytesIO
    _, other = people
    login(other)

    response = _upload(client, channel.id, [(BytesIO(b"data"), "a.png")])

    assert response.status_code == 403
    assert fake_r2 == {}


def test_an_image_renders_inline_and_a_file_renders_as_a_chip(
        session, client, login, channel, people, fake_r2):
    """Goes through the rendered bubble, not just the row.

    The row can be perfect while the bubble is wrong - which is exactly
    what happened: preview_url was called positionally against a
    keyword-only signature, the TypeError was swallowed, and every image
    quietly degraded to a chip with no link.
    """
    from io import BytesIO
    author, _ = people
    login(author)

    _upload(client, channel.id, [(BytesIO(b"PNG"), "shot.png", "image/png")])
    _upload(client, channel.id, [(BytesIO(b"%PDF"), "brief.pdf", "application/pdf")])

    page = client.get(f"/teams/c/{channel.id}").get_data(as_text=True)

    assert "tm-file-image" in page, "image did not render inline"
    assert "https://r2.test/teams/channels/" in page, "no signed URL in the bubble"
    assert "tm-file-chip" in page, "non-image did not render as a chip"
    assert "brief.pdf" in page


def test_a_dangerous_filename_cannot_escape_the_prefix(session):
    from app.teams.services.attachments import build_object_key, safe_filename

    assert safe_filename("../../etc/passwd") == "passwd"
    assert ".." not in build_object_key(7, "../../../evil.sh")
    assert build_object_key(7, "").startswith("teams/channels/7/")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
# The rule these enforce: a busy channel must not generate a notification
# per message, or the bell becomes noise and people stop looking at it.

def test_plain_channel_message_notifies_nobody(session, channel, people):
    from app.models import Notification
    author, other = people
    channels_service.add_member(channel, other)
    before = Notification.query.count()

    message = messages_service.post_message(channel, author, "morning all")
    notify_service.notify_message(message, channel, author, link=f"/teams/c/{channel.id}#tm-{message.id}")

    assert Notification.query.count() == before


def test_mention_notifies_exactly_the_person_named(session, channel, people):
    from app.models import Notification
    author, other = people
    channels_service.add_member(channel, other)

    message = messages_service.post_message(
        channel, author, f"can you take this @{other.name}?")
    assert message.mention_user_ids == [other.id]

    notified = notify_service.notify_message(message, channel, author, link=f"/teams/c/{channel.id}#tm-{message.id}")

    assert [u.id for u in notified] == [other.id]
    row = Notification.query.filter_by(user_id=other.id).order_by(
        Notification.id.desc()).first()
    assert row.category == "mention"
    assert f"/teams/c/{channel.id}" in row.link


def test_mentioning_yourself_notifies_nobody(session, channel, people):
    author, _ = people
    message = messages_service.post_message(
        channel, author, f"note to self @{author.name}")

    assert message.mention_user_ids is None
    assert notify_service.notify_message(message, channel, author, link=f"/teams/c/{channel.id}#tm-{message.id}") == []


def test_a_dm_notifies_the_other_person(session, people):
    from app.models import Notification
    author, other = people
    conversation = channels_service.get_or_create_dm(author, other)

    message = messages_service.post_message(conversation, author, "got a minute?")
    notified = notify_service.notify_message(message, conversation, author, link=f"/teams/c/{conversation.id}#tm-{message.id}")

    assert [u.id for u in notified] == [other.id]
    row = Notification.query.filter_by(user_id=other.id).order_by(
        Notification.id.desc()).first()
    assert row.category == "activity"


def test_a_mention_inside_a_dm_reads_as_a_mention(session, people):
    """Not as generic DM traffic - otherwise it lands in the wrong panel."""
    from app.models import Notification
    author, other = people
    conversation = channels_service.get_or_create_dm(author, other)

    message = messages_service.post_message(
        conversation, author, f"@{other.name} have a look")
    notify_service.notify_message(message, conversation, author, link=f"/teams/c/{conversation.id}#tm-{message.id}")

    rows = Notification.query.filter_by(user_id=other.id).all()
    assert len(rows) == 1
    assert rows[0].category == "mention"


# ---------------------------------------------------------------------------
# Threads over the wire
# ---------------------------------------------------------------------------

def test_thread_replies_ride_the_same_sync_tick(
        session, client, login, channel, people):
    """The thread pane must not need a poller of its own."""
    author, _ = people
    root = messages_service.post_message(channel, author, "question?")
    reply = messages_service.post_message(
        channel, author, "answer", parent_id=root.id)
    login(author)

    payload = client.get(
        f"/teams/api/sync?channel={channel.id}&after=0"
        f"&thread={root.id}&tafter=0"
    ).get_json()

    assert [m["id"] for m in payload["thread"]] == [reply.id]
    # Replies must not also appear in the main list, or the channel shows
    # every thread reply inline.
    assert [m["id"] for m in payload["messages"]] == [root.id]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_only_returns_conversations_you_are_in(session, people):
    """Scoped by a join, not filtered afterwards. A private channel's text
    must never leave the database for someone who is not in it."""
    author, other = people
    private = channels_service.create_channel(
        "Leadership", author, visibility="private")
    messages_service.post_message(private, author, "confidential salary review")

    assert len(messages_service.search(author, "salary")) == 1
    assert messages_service.search(other, "salary") == []


def test_search_can_be_scoped_to_one_channel(session, channel, people):
    author, _ = people
    other_channel = channels_service.create_channel("Ops", author)
    messages_service.post_message(channel, author, "deploy on friday")
    messages_service.post_message(other_channel, author, "deploy on monday")

    everywhere = messages_service.search(author, "deploy")
    here = messages_service.search(author, "deploy", channel_id=channel.id)

    assert len(everywhere) == 2
    assert len(here) == 1
    assert here[0].channel_id == channel.id


def test_search_skips_deleted_messages(session, channel, people):
    author, _ = people
    message = messages_service.post_message(channel, author, "wrong number")
    assert len(messages_service.search(author, "wrong")) == 1

    messages_service.delete_message(message, author)
    assert messages_service.search(author, "wrong") == []


def test_search_handles_hinglish(session, channel, people):
    """The index uses the 'simple' configuration precisely so mixed-language
    messages are not stemmed and stopworded as if they were English."""
    author, _ = people
    messages_service.post_message(channel, author, "polish kro phir deploy karenge")

    assert len(messages_service.search(author, "kro")) == 1
    assert len(messages_service.search(author, "deploy")) == 1


def test_the_search_page_renders_its_results(
        session, client, login, channel, people):
    """Through the page, not just the service.

    The service tests all passed while the page 500'd: TeamMessage had no
    `channel` relationship, and the template asks every result which
    conversation it came from.
    """
    author, _ = people
    messages_service.post_message(channel, author, "transparent logo please")
    login(author)

    response = client.get("/teams/search?q=transparent")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "transparent logo please" in body
    assert channel.name in body            # the "which conversation" line
    assert f"#tm-" in body                 # deep link to the message


def test_search_loads_channel_and_author_without_an_n_plus_one(
        session, channel, people):
    """40 results must not become 81 queries."""
    from sqlalchemy import event

    author, _ = people
    for n in range(8):
        messages_service.post_message(channel, author, f"budget item {n}")

    statements = []

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = db.session.get_bind()
    # expire_all, not expunge_all: the rows must be re-read from the
    # database so a missing eager load actually shows up, but the fixture's
    # objects have to stay attached to the session. Author is touched first
    # so its own refresh does not land inside the counted window.
    db.session.expire_all()
    _ = author.id

    event.listen(engine, "before_cursor_execute", record)
    try:
        results = messages_service.search(author, "budget")
        # Touching both is what an N+1 would show up on.
        _ = [(m.channel.key, m.user.name if m.user else "") for m in results]
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(results) == 8
    assert len(statements) <= 2, f"expected 1-2 queries, got {len(statements)}"


def test_search_ignores_a_query_too_short_to_mean_anything(session, channel, people):
    author, _ = people
    messages_service.post_message(channel, author, "a short note")

    assert messages_service.search(author, "") == []
    assert messages_service.search(author, "a") == []


# ---------------------------------------------------------------------------
# Mute
# ---------------------------------------------------------------------------

def test_muting_hides_the_badge_without_leaving(session, channel, people):
    author, other = people
    channels_service.add_member(channel, other)
    messages_service.post_message(channel, author, "noise")

    assert unread_service.total_unread(other) == 1

    channels_service.set_muted(channel, other, True)

    assert unread_service.total_unread(other) == 0
    state = {r["channel"].id: r for r in unread_service.channel_state(other)}
    assert state[channel.id]["unread"] is False
    assert state[channel.id]["muted"] is True
    # Still a member - muting is not leaving.
    assert channels_service.membership(channel.id, other.id) is not None


def test_a_mention_still_notifies_in_a_muted_channel(session, channel, people):
    """Mute silences ambient traffic. Somebody typing your name is not
    ambient, and a mention nobody receives is worse than a noisy channel."""
    from app.models import Notification
    author, other = people
    channels_service.add_member(channel, other)
    channels_service.set_muted(channel, other, True)

    message = messages_service.post_message(
        channel, author, f"@{other.name} can you look?")
    notified = notify_service.notify_message(
        message, channel, author, link="/teams/")

    assert [u.id for u in notified] == [other.id]
    assert Notification.query.filter_by(
        user_id=other.id, category="mention").count() == 1


# ---------------------------------------------------------------------------
# Channel settings
# ---------------------------------------------------------------------------

def test_an_owner_can_add_someone_to_a_private_channel(
        session, client, login, people):
    """Before settings existed, a private channel could be created and then
    never joined by anyone - add_member had no route reaching it."""
    author, other = people
    private = channels_service.create_channel(
        "Leadership", author, visibility="private")
    login(author)

    response = client.post(
        f"/teams/c/{private.id}/settings",
        data={"action": "add_member", "user_id": other.id})

    assert response.status_code == 302
    assert channels_service.membership(private.id, other.id) is not None
    assert channels_service.can_read(private, other) is True


def test_general_gets_an_owner_so_it_can_be_managed(session, people):
    """It is created lazily on somebody's first visit, and the first
    version of that made them a plain member - leaving the company-wide
    channel with no owner at all, so nobody could rename it, archive it or
    add anyone to it."""
    author, other = people

    general = channels_service.ensure_default_channel(author)
    assert channels_service.can_administer(general, author) is True

    # Everyone after the first joins as an ordinary member.
    channels_service.ensure_default_channel(other)
    assert channels_service.can_administer(general, other) is False


def test_a_member_who_is_not_an_owner_cannot_change_the_channel(
        session, client, login, channel, people):
    author, other = people
    channels_service.add_member(channel, other)
    login(other)

    assert client.get(f"/teams/c/{channel.id}/settings").status_code == 200
    assert client.post(
        f"/teams/c/{channel.id}/settings",
        data={"action": "details", "name": "Hijacked"}).status_code == 403


def test_a_non_member_cannot_even_see_the_settings(
        session, client, login, channel, people):
    _, other = people
    login(other)
    assert client.get(f"/teams/c/{channel.id}/settings").status_code == 404


def test_renaming_keeps_the_handle(session, client, login, channel, people):
    """Every link anyone has shared carries the key."""
    author, _ = people
    original_key = channel.key
    login(author)

    client.post(f"/teams/c/{channel.id}/settings",
                data={"action": "details", "name": "Brand Studio"})

    assert channel.name == "Brand Studio"
    assert channel.key == original_key


def test_archiving_makes_a_channel_read_only(
        session, client, login, channel, people):
    author, _ = people
    login(author)

    client.post(f"/teams/c/{channel.id}/settings", data={"action": "archive"})

    assert channel.is_archived is True
    assert channels_service.can_post(channel, author) is False
    assert channels_service.can_read(channel, author) is True

    client.post(f"/teams/c/{channel.id}/settings", data={"action": "unarchive"})
    assert channels_service.can_post(channel, author) is True


def test_you_cannot_remove_yourself_from_the_members_list(
        session, client, login, channel, people):
    """It would silently be a "leave", which has its own button and its own
    redirect - and doing it here would strand you on a 404."""
    author, _ = people
    login(author)

    client.post(f"/teams/c/{channel.id}/settings",
                data={"action": "remove_member", "user_id": author.id})

    assert channels_service.membership(channel.id, author.id) is not None


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

@pytest.fixture()
def meeting(session, people):
    from app.utils.timezone import ist_now
    from datetime import timedelta as _td
    from app.teams.services import meetings as meetings_service

    author, other = people
    return meetings_service.schedule(
        title=f"{PYTEST_MEETING_PREFIX}Standup",
        starts_at=ist_now() + _td(minutes=5),
        organiser=author,
        participants=[other],
    )


def test_the_room_key_is_unguessable_and_not_derived_from_the_meeting(
        session, meeting):
    """On the public Jitsi the room name IS the authorisation, so anything
    derivable from the meeting is an open door."""
    from app.teams.providers.registry import get_provider

    assert meeting.room_key
    assert len(meeting.room_key) >= 32

    room = get_provider("jitsi").room_name(meeting)
    assert str(meeting.id) not in room
    assert "standup" not in room.lower()
    # And stable, or two people would end up in different rooms.
    assert get_provider("jitsi").room_name(meeting) == room


def test_two_meetings_never_share_a_room(session, people):
    from app.utils.timezone import ist_now
    from app.teams.services import meetings as meetings_service

    author, _ = people
    keys = {
        meetings_service.schedule(
            title=f"{PYTEST_MEETING_PREFIX}m{n}",
            starts_at=ist_now(), organiser=author).room_key
        for n in range(5)
    }
    assert len(keys) == 5


def test_the_organiser_is_always_invited(session, meeting, people):
    author, other = people
    invited = {p.id for p in meeting.participants}
    assert author.id in invited
    assert other.id in invited


def test_meeting_date_is_ist_and_started_at_is_utc(session, meeting):
    """The landmine this module is built around.

    meeting_date predates Teams and is IST-naive; started_at is a
    server-recorded UTC actual. Subtracting one from the other would be
    wrong by five and a half hours, so nothing may treat them as the same
    clock.
    """
    from datetime import datetime
    from app.teams.services import meetings as meetings_service
    from app.utils.timezone import IST_OFFSET, ist_now

    meetings_service.mark_started(meeting)

    # meeting_date sits near IST now, started_at near UTC now.
    assert abs((meeting.meeting_date - ist_now()).total_seconds()) < 600
    assert abs((meeting.started_at - datetime.utcnow()).total_seconds()) < 600
    # Which means they genuinely differ by the offset - the thing a naive
    # comparison would silently get wrong.
    drift = meeting.meeting_date - meeting.started_at
    assert abs(drift - IST_OFFSET).total_seconds() < 600


def test_a_meeting_opens_shortly_before_it_starts(session, people):
    from datetime import timedelta as _td
    from app.teams.services import meetings as meetings_service
    from app.utils.timezone import ist_now

    author, _ = people
    soon = meetings_service.schedule(
        title=f"{PYTEST_MEETING_PREFIX}soon", organiser=author,
        starts_at=ist_now() + _td(minutes=5))
    later = meetings_service.schedule(
        title=f"{PYTEST_MEETING_PREFIX}later", organiser=author,
        starts_at=ist_now() + _td(hours=3))

    assert meetings_service.is_joinable(soon) is True
    assert meetings_service.is_joinable(later) is False


def test_a_running_meeting_stays_joinable_past_its_end(session, people):
    """Overrunning is normal; locking out the person arriving late is not."""
    from datetime import timedelta as _td
    from app.teams.services import meetings as meetings_service
    from app.utils.timezone import ist_now

    author, _ = people
    overdue = meetings_service.schedule(
        title=f"{PYTEST_MEETING_PREFIX}overdue", organiser=author,
        starts_at=ist_now() - _td(hours=2), duration_minutes=15)

    assert meetings_service.is_joinable(overdue) is False
    meetings_service.mark_started(overdue)
    assert meetings_service.is_joinable(overdue) is True


def test_an_ended_meeting_is_not_joinable(session, meeting):
    from app.teams.services import meetings as meetings_service

    meetings_service.mark_started(meeting)
    meetings_service.end(meeting)
    assert meetings_service.is_joinable(meeting) is False


def test_a_channel_meeting_admits_the_whole_channel(
        session, channel, people):
    """"Huddle in #design" must not need everyone invited one by one."""
    from app.utils.timezone import ist_now
    from app.teams.services import meetings as meetings_service

    author, other = people
    channels_service.add_member(channel, other)
    call = meetings_service.start_now(
        f"{PYTEST_MEETING_PREFIX}huddle", author, channel=channel)

    assert meetings_service.can_join(call, other) is True


def test_a_channel_meeting_excludes_people_outside_the_channel(
        session, people, make_user):
    from app.teams.services import meetings as meetings_service

    author, _ = people
    outsider = make_user(role="content_writer")
    private = channels_service.create_channel(
        "Leadership", author, visibility="private")

    call = meetings_service.start_now(
        f"{PYTEST_MEETING_PREFIX}private", author, channel=private)

    assert meetings_service.can_join(call, author) is True
    assert meetings_service.can_join(call, outsider) is False


def test_a_meeting_with_no_channel_is_open_to_active_staff(
        session, meeting, make_user):
    """Matches what the old meetings page did - every meeting was visible
    to everyone. Narrowing that silently would strand people."""
    from app.teams.services import meetings as meetings_service

    assert meetings_service.can_join(meeting, make_user(role="video_editor")) is True
    assert meetings_service.can_join(
        meeting, make_user(role="video_editor", status="inactive")) is False


def test_starting_a_call_posts_a_card_into_the_channel(
        session, client, login, channel, people):
    author, _ = people
    login(author)

    response = client.post(f"/teams/c/{channel.id}/call")

    assert response.status_code == 302
    card = TeamMessage.query.filter_by(kind="meeting").order_by(
        TeamMessage.id.desc()).first()
    assert card is not None
    assert card.meta["event"] == "started"
    assert card.body is None


def test_the_join_page_keeps_the_room_out_of_the_url(
        session, client, login, meeting, people):
    author, _ = people
    login(author)

    response = client.get(f"/teams/meetings/{meeting.id}/join")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    # In the config blob, not in any href or the address bar.
    assert meeting.room_key in body
    assert f"/{meeting.room_key}" not in response.request.path


def test_the_old_meetings_endpoints_still_resolve(session, app, client, login,
                                                  meeting, people):
    """calendar/index.html builds four url_for('meetings.meeting_detail')
    links and dashboard.py queries Meeting in five places. Removing the
    endpoints for a tidy-up would break the calendar."""
    author, _ = people
    login(author)

    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "meetings.meeting_detail" in endpoints
    assert "meetings.list_meetings" in endpoints

    # With Teams on they redirect into it rather than rendering the old page.
    assert client.get("/meetings/").status_code == 302
    assert client.get(f"/meetings/{meeting.id}").headers["Location"].endswith(
        f"/teams/meetings/{meeting.id}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# The service tests never touch a template, so without these a broken
# Jinja reference ships silently and only shows up as a 500 in the browser.

def test_channel_page_renders_the_shell_and_history(
        session, client, login, channel, people):
    author, _ = people
    messages_service.post_message(channel, author, "rendered on the server")
    login(author)

    response = client.get(f"/teams/c/{channel.id}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "teams-sidebar" in body            # the module's own shell
    assert "rendered on the server" in body   # first paint, not a poll
    assert 'data-teams-chat' in body          # the poller's mount point
    assert "Back to CypherCrew" in body       # the way out


def test_first_paint_and_poll_render_a_message_identically(
        session, client, login, channel, people):
    """The whole point of having one server-side renderer.

    The first version of _message.html took `viewer` only from the sync
    payload, so a message drawn on page load lost its own-message controls
    and then silently grew them back on the next tick.
    """
    author, _ = people
    message = messages_service.post_message(channel, author, "same both ways")
    login(author)

    page = client.get(f"/teams/c/{channel.id}").get_data(as_text=True)
    polled = client.get(
        f"/teams/api/sync?channel={channel.id}&after=0"
    ).get_json()["messages"][0]["html"]

    for marker in ('data-edit="', 'data-delete="', "is-own"):
        assert marker in polled, f"{marker} missing from the polled copy"
        assert marker in page, f"{marker} missing from the first paint"


def test_browse_and_new_channel_pages_render(session, client, login, people):
    author, _ = people
    login(author)

    assert client.get("/teams/browse").status_code == 200
    assert client.get("/teams/channels/new").status_code == 200


def test_a_deleted_message_renders_as_a_tombstone(
        session, client, login, channel, people):
    author, _ = people
    message = messages_service.post_message(channel, author, "secret")
    messages_service.delete_message(message, author)
    login(author)

    body = client.get(f"/teams/c/{channel.id}").get_data(as_text=True)

    assert "secret" not in body
    assert "Message deleted" in body


def test_erp_sidebar_shows_teams_and_notifications_carry_the_count(
        session, client, login, channel, people):
    """The badge outside Teams rides the notifications poll every page
    already makes - there is no second app-wide poller."""
    author, other = people
    channels_service.add_member(channel, other)
    messages_service.post_message(channel, author, "ping")
    login(other)

    # Any page on the ERP shell will do - the point is that the entry is
    # in partials/sidebar.html, which base_app.html includes everywhere.
    erp_page = client.get("/notes/").get_data(as_text=True)
    assert 'href="/teams/"' in erp_page
    assert 'id="teamsNavCount"' in erp_page

    payload = client.get("/notifications/api").get_json()
    assert payload["teams_unread"] == 1


def test_first_visit_lands_in_general(session, client, login, people):
    """The module must never open as an empty room."""
    author, _ = people
    login(author)

    response = client.get("/teams/")

    assert response.status_code == 302
    general = TeamChannel.query.filter_by(key="general").first()
    assert general is not None
    assert response.headers["Location"].endswith(f"/teams/c/{general.id}")
