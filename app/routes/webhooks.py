"""Inbound Meta (Facebook / Instagram) webhooks — real-time comment ingestion.

Instead of polling every post for new comments, Meta pushes a comment the
moment it is posted. We verify the push cryptographically (X-Hub-Signature-256,
HMAC-SHA256 over the raw body with META_APP_SECRET), materialise the comment
into Engage, and run the SAME guarded auto-mod + auto-reply that the cron/manual
path uses — but scan-only, so a real-time reply costs no extra API polling.

Fully dormant unless META_WEBHOOK_ENABLED is on. The GET handshake still answers
so the subscription can be verified in the Meta dashboard before go-live; the
POST always verifies the signature and only PROCESSES when the flag is on
(returning 200 regardless, so Meta does not retry or disable a paused hook).
"""
import hashlib
import hmac

from flask import Blueprint, abort, current_app, request

from app.extensions import csrf

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


def _signature_ok():
    """True iff the request carries a valid X-Hub-Signature-256 for our app
    secret. Fails closed when the secret is unset (never trust an unsigned
    push)."""
    secret = current_app.config.get("META_APP_SECRET")
    if not secret:
        return False
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@webhooks_bp.route("/meta", methods=["GET"])
def meta_verify():
    """Subscription handshake: Meta calls this once with a challenge to echo."""
    token = current_app.config.get("META_WEBHOOK_VERIFY_TOKEN")
    args = request.args
    if (token and args.get("hub.mode") == "subscribe"
            and args.get("hub.verify_token") == token):
        return args.get("hub.challenge", ""), 200
    abort(403)


def _iter_comments(payload):
    """Yield ingest kwargs for each NEW comment in a Meta webhook payload,
    normalising Facebook `feed` and Instagram `comments` shapes. Anything that
    isn't an added comment (edits, likes, other fields) is skipped."""
    obj = payload.get("object")
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            field = change.get("field")
            v = change.get("value") or {}
            if obj == "page" and field == "feed":
                if v.get("item") != "comment" or v.get("verb") not in (None, "add"):
                    continue
                frm = v.get("from") or {}
                yield {
                    "platform": "facebook",
                    "external_post_id": v.get("post_id"),
                    "external_id": v.get("comment_id"),
                    "author_id": frm.get("id"),
                    "author_name": frm.get("name"),
                    "message": v.get("message"),
                    "parent_external_id": v.get("parent_id"),
                    "created_time": v.get("created_time"),
                }
            elif obj == "instagram" and field == "comments":
                frm = v.get("from") or {}
                media = v.get("media") or {}
                yield {
                    "platform": "instagram",
                    "external_post_id": media.get("id"),
                    "external_id": v.get("id"),
                    "author_id": frm.get("id"),
                    "author_name": frm.get("username"),
                    "message": v.get("text"),
                    "parent_external_id": v.get("parent_id"),
                    "created_time": v.get("created_time"),
                }


@webhooks_bp.route("/meta", methods=["POST"])
@csrf.exempt
def meta_events():
    """Signed comment push. Verify, ingest, then guarded real-time processing."""
    if not _signature_ok():
        abort(403)
    # Ack even when paused, so Meta doesn't retry or auto-disable the hook.
    if not current_app.config.get("META_WEBHOOK_ENABLED"):
        return "", 200

    from app.social.services import engage
    payload = request.get_json(silent=True) or {}
    affected = set()
    for kwargs in _iter_comments(payload):
        if not kwargs.get("external_post_id") or not kwargs.get("external_id"):
            continue
        try:
            comment, created = engage.ingest_comment_event(
                kwargs.pop("platform"), kwargs.pop("external_post_id"), **kwargs)
        except Exception:  # noqa: BLE001 - one bad event never drops the batch
            current_app.logger.exception("[meta-webhook] ingest failed")
            continue
        # Only a genuinely new, not-ours comment is worth reacting to.
        if created and comment is not None and not comment.is_ours:
            post = comment.target.post if comment.target else None
            if post is not None and post.client_id:
                affected.add(post.client_id)

    # Real-time, scan-only: no extra polling. Each helper self-gates on its own
    # feature/admin/per-client switches, so this is inert unless configured.
    for client_id in affected:
        try:
            engage.automod_scan(client_id)
            engage.auto_reply_scan(client_id)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "[meta-webhook] real-time processing failed for client %s",
                client_id)
    return "", 200
