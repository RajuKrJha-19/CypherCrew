"""Public legal pages and the Meta data-deletion callback.

Two things are being pinned. The pages must render for someone who is NOT
logged in - Meta's reviewer fetches them anonymously, and a person who has
already removed the app cannot sign in to ask for deletion. And the
callback must refuse anything it cannot cryptographically verify, because
it deletes data: an unauthenticated endpoint that takes a user id and
wipes that account would be a gift to anyone who found the URL.
"""

import base64
import hashlib
import hmac
import json

import pytest

from app.extensions import db
from app.models import DataDeletionRequest, SocialAccount

APP_SECRET = "pytest-app-secret"


@pytest.fixture()
def meta_secret(app):
    """The callback is inert without a configured secret - set one."""
    previous = app.config.get("META_APP_SECRET")
    app.config["META_APP_SECRET"] = APP_SECRET
    yield APP_SECRET
    app.config["META_APP_SECRET"] = previous


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _signed_request(payload, secret=APP_SECRET):
    encoded = _b64(json.dumps(payload).encode())
    signature = hmac.new(secret.encode(), encoded.encode(),
                         hashlib.sha256).digest()
    return f"{_b64(signature)}.{encoded}"


def _payload(user_id="FBUSER-1"):
    return {"algorithm": "HMAC-SHA256", "issued_at": 1700000000,
            "user_id": user_id}


# --------------------------------------------------------------------------
# The documents Meta's review fetches
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/legal/privacy",
    "/legal/terms",
    "/legal/data-deletion",
])
def test_the_legal_pages_are_public(client, path):
    """No login, no redirect - a 302 to /login here fails app review."""
    resp = client.get(path)
    assert resp.status_code == 200


def test_the_privacy_policy_names_what_we_take_from_meta(client):
    body = client.get("/legal/privacy").get_data(as_text=True)
    for expected in ["Access tokens", "app-scoped user ID", "Insights",
                     "Engagement"]:
        assert expected in body, f"privacy policy should mention {expected!r}"


def test_the_privacy_policy_links_to_deletion(client):
    assert "/legal/data-deletion" in \
        client.get("/legal/privacy").get_data(as_text=True)


def test_the_deletion_page_explains_the_facebook_route(client):
    """Meta wants step-by-step instructions, not just a contact address."""
    body = client.get("/legal/data-deletion").get_data(as_text=True)
    assert "Apps and Websites" in body
    assert "Remove" in body


# --------------------------------------------------------------------------
# The callback refuses anything it cannot verify
# --------------------------------------------------------------------------

def test_a_callback_without_a_signed_request_is_rejected(client, meta_secret):
    assert client.post("/legal/data-deletion/callback").status_code == 400


def test_a_forged_signature_is_rejected(client, meta_secret):
    forged = _signed_request(_payload(), secret="not-the-app-secret")
    resp = client.post("/legal/data-deletion/callback",
                       data={"signed_request": forged})
    assert resp.status_code == 400


def test_a_tampered_payload_is_rejected(client, meta_secret):
    """Signature valid for a different payload than the one sent."""
    genuine = _signed_request(_payload("FBUSER-1"))
    signature = genuine.split(".", 1)[0]
    swapped = _b64(json.dumps(_payload("FBUSER-VICTIM")).encode())

    resp = client.post("/legal/data-deletion/callback",
                       data={"signed_request": f"{signature}.{swapped}"})
    assert resp.status_code == 400


def test_garbage_is_rejected_rather_than_crashing(client, meta_secret):
    for junk in ["", "notbase64", "a.b.c", "onlyonepart"]:
        resp = client.post("/legal/data-deletion/callback",
                           data={"signed_request": junk})
        assert resp.status_code == 400, f"{junk!r} should be refused"


def test_nothing_is_deleted_by_a_rejected_callback(session, client,
                                                   meta_secret):
    account = SocialAccount(
        platform="facebook", external_id="PAGE-1", display_name="Page",
        account_type="page", status="active",
        meta={"connected_user_id": "FBUSER-1"},
    )
    session.add(account)
    session.commit()

    client.post("/legal/data-deletion/callback",
                data={"signed_request": _signed_request(
                    _payload("FBUSER-1"), secret="wrong")})

    assert db.session.get(SocialAccount, account.id) is not None


def test_the_callback_is_inert_without_a_configured_secret(app, client):
    """Fail closed: an unsigned-verifiable endpoint must not delete."""
    previous = app.config.get("META_APP_SECRET")
    app.config["META_APP_SECRET"] = None
    try:
        resp = client.post("/legal/data-deletion/callback",
                           data={"signed_request": _signed_request(_payload())})
        assert resp.status_code == 400
    finally:
        app.config["META_APP_SECRET"] = previous


# --------------------------------------------------------------------------
# A genuine callback deletes, and answers in Meta's shape
# --------------------------------------------------------------------------

def test_a_genuine_callback_deletes_and_returns_metas_shape(
        session, client, meta_secret):
    account = SocialAccount(
        platform="facebook", external_id="PAGE-2", display_name="Page",
        account_type="page", status="active",
        meta={"connected_user_id": "FBUSER-2"},
    )
    session.add(account)
    session.commit()
    account_id = account.id

    resp = client.post("/legal/data-deletion/callback",
                       data={"signed_request": _signed_request(
                           _payload("FBUSER-2"))})

    assert resp.status_code == 200
    body = resp.get_json()
    # Meta reads exactly these two keys.
    assert set(body) == {"url", "confirmation_code"}
    assert body["confirmation_code"]
    assert body["confirmation_code"] in body["url"]

    assert db.session.get(SocialAccount, account_id) is None


def test_only_the_requesting_users_channels_are_deleted(
        session, client, meta_secret):
    """The blast radius is one person's connections, not the table."""
    mine = SocialAccount(
        platform="facebook", external_id="PAGE-MINE", display_name="Mine",
        account_type="page", status="active",
        meta={"connected_user_id": "FBUSER-3"})
    theirs = SocialAccount(
        platform="facebook", external_id="PAGE-THEIRS", display_name="Theirs",
        account_type="page", status="active",
        meta={"connected_user_id": "FBUSER-OTHER"})
    session.add_all([mine, theirs])
    session.commit()
    mine_id, theirs_id = mine.id, theirs.id

    client.post("/legal/data-deletion/callback",
                data={"signed_request": _signed_request(_payload("FBUSER-3"))})

    assert db.session.get(SocialAccount, mine_id) is None
    assert db.session.get(SocialAccount, theirs_id) is not None


def test_an_unknown_user_still_gets_a_code_and_a_status_page(
        session, client, meta_secret):
    """Meta needs an answer even when we held nothing."""
    resp = client.post("/legal/data-deletion/callback",
                       data={"signed_request": _signed_request(
                           _payload("FBUSER-NEVER-SEEN"))})
    assert resp.status_code == 200
    code = resp.get_json()["confirmation_code"]

    page = client.get(f"/legal/data-deletion/status/{code}")
    assert page.status_code == 200
    assert "no data" in page.get_data(as_text=True).lower()


def test_the_status_page_is_public_and_reports_completion(
        session, client, meta_secret):
    account = SocialAccount(
        platform="instagram", external_id="IG-1", display_name="IG",
        account_type="ig_business", status="active",
        meta={"connected_user_id": "FBUSER-4"})
    session.add(account)
    session.commit()

    code = client.post(
        "/legal/data-deletion/callback",
        data={"signed_request": _signed_request(_payload("FBUSER-4"))}
    ).get_json()["confirmation_code"]

    body = client.get(f"/legal/data-deletion/status/{code}").get_data(
        as_text=True)
    assert code in body
    assert "deleted" in body.lower()


def test_an_unknown_confirmation_code_is_a_404(client):
    assert client.get(
        "/legal/data-deletion/status/nope-not-a-code").status_code == 404


# --------------------------------------------------------------------------
# The public form records rather than deletes
# --------------------------------------------------------------------------

def test_the_public_form_does_not_delete_on_an_unverified_say_so(
        session, client):
    """An anonymous form is not proof of ownership - acting on it would be
    the security hole, not the fix."""
    account = SocialAccount(
        platform="facebook", external_id="PAGE-FORM", display_name="Page",
        account_type="page", status="active",
        meta={"connected_user_id": "FBUSER-5"})
    session.add(account)
    session.commit()
    account_id = account.id

    resp = client.post("/legal/data-deletion",
                       data={"identifier": "FBUSER-5"},
                       follow_redirects=True)

    assert resp.status_code == 200
    assert db.session.get(SocialAccount, account_id) is not None

    record = DataDeletionRequest.query.filter_by(
        external_user_id="FBUSER-5").first()
    assert record is not None
    assert record.status == DataDeletionRequest.STATUS_MANUAL_REVIEW


def test_the_form_needs_an_identifier(session, client):
    before = DataDeletionRequest.query.count()
    client.post("/legal/data-deletion", data={"identifier": "  "},
                follow_redirects=True)
    assert DataDeletionRequest.query.count() == before
