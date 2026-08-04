"""Meta comment webhooks: the GET handshake, HMAC-SHA256 signature enforcement,
and real-time ingest of a pushed comment into Engage (dedupe + is_ours), all
dormant unless META_WEBHOOK_ENABLED. No network — we POST signed bodies straight
at the endpoint.
"""
import hashlib
import hmac
import json

from app.models import Client, SocialAccount, SocialComment, SocialPost, \
    SocialPostTarget
from app.social.tokens.vault import get_vault
from tests.conftest import PYTEST_EMAIL_PREFIX

SECRET = "whsec-test-secret"
VERIFY = "verify-token-123"


def _sig(body: bytes):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _webhook_cfg(app):
    app.config["META_APP_SECRET"] = SECRET
    app.config["META_WEBHOOK_VERIFY_TOKEN"] = VERIFY
    app.config["META_WEBHOOK_ENABLED"] = True


def _reset_cfg(app):
    app.config["META_WEBHOOK_ENABLED"] = False


def _target(session, ext_post_id, page_ext="PAGE1"):
    c = Client(client_name=f"{PYTEST_EMAIL_PREFIX}wh", status="active")
    session.add(c)
    session.commit()
    acct = SocialAccount(
        platform="facebook", external_id=page_ext, display_name=page_ext,
        account_type="page", status="active", client_id=c.id,
        token_ciphertext=get_vault().encrypt("AT"), token_key_version=1)
    session.add(acct)
    session.flush()
    post = SocialPost(title="p", status="published", source="studio",
                      client_id=c.id)
    session.add(post)
    session.flush()
    t = SocialPostTarget(
        social_post_id=post.id, social_account_id=acct.id, platform="facebook",
        post_type="image", external_post_id=ext_post_id, status="published")
    session.add(t)
    session.commit()
    return c, acct, t


def _fb_event(post_id, comment_id, from_id, message="Nice!"):
    return {"object": "page", "entry": [{"id": "PAGE1", "changes": [{
        "field": "feed", "value": {
            "item": "comment", "verb": "add", "post_id": post_id,
            "comment_id": comment_id, "from": {"id": from_id, "name": "Rahul"},
            "message": message, "created_time": 111}}]}]}


# -- GET handshake ----------------------------------------------------------

def test_get_handshake_echoes_challenge(client, app):
    app.config["META_WEBHOOK_VERIFY_TOKEN"] = VERIFY
    r = client.get("/webhooks/meta", query_string={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY,
        "hub.challenge": "abc123"})
    assert r.status_code == 200 and r.data == b"abc123"


def test_get_handshake_rejects_wrong_token(client, app):
    app.config["META_WEBHOOK_VERIFY_TOKEN"] = VERIFY
    r = client.get("/webhooks/meta", query_string={
        "hub.mode": "subscribe", "hub.verify_token": "wrong",
        "hub.challenge": "abc123"})
    assert r.status_code == 403


# -- POST signature ---------------------------------------------------------

def test_post_rejects_bad_signature(client, app, session):
    _webhook_cfg(app)
    try:
        body = json.dumps(_fb_event("PAGE1_100", "PAGE1_100_9", "999")).encode()
        r = client.post("/webhooks/meta", data=body,
                        headers={"X-Hub-Signature-256": "sha256=deadbeef",
                                 "Content-Type": "application/json"})
        assert r.status_code == 403
    finally:
        _reset_cfg(app)


# -- ingest -----------------------------------------------------------------

def test_post_ingests_new_comment(client, app, session):
    _webhook_cfg(app)
    try:
        _c, _a, t = _target(session, "PAGE1_100")
        body = json.dumps(_fb_event("PAGE1_100", "PAGE1_100_9", "999",
                                    "REALTIMEMARK")).encode()
        r = client.post("/webhooks/meta", data=body,
                        headers={"X-Hub-Signature-256": _sig(body),
                                 "Content-Type": "application/json"})
        assert r.status_code == 200
        row = SocialComment.query.filter_by(external_id="PAGE1_100_9").first()
        assert row is not None
        assert row.target_id == t.id and row.message == "REALTIMEMARK"
        assert row.is_ours is False and row.status == "open"
    finally:
        _reset_cfg(app)


def test_post_marks_our_own_comment(client, app, session):
    """A comment authored by our own page id is is_ours -> never auto-answered."""
    _webhook_cfg(app)
    try:
        _c, acct, _t = _target(session, "PAGE1_200", page_ext="PAGEOWN")
        # from.id == the page's own external_id
        body = json.dumps(_fb_event("PAGE1_200", "PAGE1_200_1", "PAGEOWN")).encode()
        r = client.post("/webhooks/meta", data=body,
                        headers={"X-Hub-Signature-256": _sig(body),
                                 "Content-Type": "application/json"})
        assert r.status_code == 200
        row = SocialComment.query.filter_by(external_id="PAGE1_200_1").first()
        assert row is not None and row.is_ours is True
    finally:
        _reset_cfg(app)


def test_post_is_idempotent(client, app, session):
    _webhook_cfg(app)
    try:
        _target(session, "PAGE1_300")
        body = json.dumps(_fb_event("PAGE1_300", "PAGE1_300_7", "999")).encode()
        hdr = {"X-Hub-Signature-256": _sig(body),
               "Content-Type": "application/json"}
        client.post("/webhooks/meta", data=body, headers=hdr)
        client.post("/webhooks/meta", data=body, headers=hdr)   # re-delivery
        rows = SocialComment.query.filter_by(external_id="PAGE1_300_7").all()
        assert len(rows) == 1
    finally:
        _reset_cfg(app)


def test_post_skips_untracked_post(client, app, session):
    _webhook_cfg(app)
    try:
        body = json.dumps(_fb_event("NOSUCH_1", "NOSUCH_1_1", "999")).encode()
        r = client.post("/webhooks/meta", data=body,
                        headers={"X-Hub-Signature-256": _sig(body),
                                 "Content-Type": "application/json"})
        assert r.status_code == 200
        assert SocialComment.query.filter_by(external_id="NOSUCH_1_1").first() is None
    finally:
        _reset_cfg(app)


def test_post_inert_when_flag_off(client, app, session):
    """Flag off -> valid signature still acks 200 but nothing is ingested."""
    app.config["META_APP_SECRET"] = SECRET
    app.config["META_WEBHOOK_ENABLED"] = False
    _target(session, "PAGE1_400")
    body = json.dumps(_fb_event("PAGE1_400", "PAGE1_400_2", "999")).encode()
    r = client.post("/webhooks/meta", data=body,
                    headers={"X-Hub-Signature-256": _sig(body),
                             "Content-Type": "application/json"})
    assert r.status_code == 200
    assert SocialComment.query.filter_by(external_id="PAGE1_400_2").first() is None
