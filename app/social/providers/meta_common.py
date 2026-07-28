"""Shared Meta (Graph API) machinery for the Facebook + Instagram adapters.

One place for: the versioned Graph HTTP client, the OAuth login/exchange
flow (short-lived -> long-lived), scope/permission validation, token
refresh, and Meta error-code -> typed-SocialError mapping. Both adapters
subclass MetaBaseProvider so none of this is duplicated, and nothing
platform-specific leaks past the SocialProvider interface.

The Graph base URLs are config-driven (META_GRAPH_BASE_URL /
META_OAUTH_BASE_URL), so the identical code runs against graph.facebook.com
in production or the local emulator in tests - the provider can't tell.
"""

import hashlib
import hmac
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import current_app

from app.social.providers.base import SocialProvider
from app.social.dto import TokenBundle
from app.social.errors import (
    AuthError, PermanentError, RateLimitError, TransientError,
)


_TIMEOUT = 30
_UPLOAD_TIMEOUT = 120

# Meta error codes -> classification.
_AUTH_CODES = {190, 102, 10, 200, 210, 803}
_RATE_CODES = {4, 17, 32, 613, 80001, 80002, 80003, 80004, 80006, 80008}
_RATE_SUBCODES = {2446079, 2207051}
#: The file itself is unacceptable - Meta's documented Reels rejections.
#: 1363040 is the aspect ratio, 1363127 a resolution below the minimum.
#: Retrying the same bytes will fail identically, so these are permanent
#: and the message (Meta's own) tells the person what to re-export.
_CONTENT_CODES = {1363040, 1363127}

# The unified Meta connect requests the union of Facebook-Page and Instagram
# scopes, so a SINGLE consent connects Facebook Pages AND their linked
# Instagram Business accounts in one pass (Meta Business Suite style). The
# Instagram scopes are not "required" for a Facebook connect (see each
# provider's _required_scopes), so Facebook still connects even if the user
# declines Instagram - we just can't discover Instagram until they grant it.
#
# THIS is the list that reaches the consent screen (OAuthManager.start ->
# connect_scopes). There are three scope lists in play and they mean
# different things:
#
#   META_UNIFIED_SCOPES  - what the user is ASKED for at consent time
#   Provider.SCOPES      - what that adapter needs to do all its work
#   _required_scopes()   - the subset without which a connect is refused
#
# A scope missing from THIS list is never granted, however carefully it is
# declared elsewhere - which is how the comment and insights scopes were
# approved-but-absent. test_meta_scopes.py pins the union so the lists
# cannot drift apart again.
META_UNIFIED_SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "pages_manage_engagement",
    "read_insights",
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
]


class MetaGraphError(Exception):
    """Raised for any non-2xx / error-body Graph response. Carries the raw
    Meta error object so map_error can classify it."""

    def __init__(self, error, status_code):
        self.error = error or {}
        self.status_code = status_code
        super().__init__(self.error.get("message", "Meta Graph error"))


def _cfg(key, default=None):
    return current_app.config.get(key, default)


class MetaGraph:
    """Thin versioned wrapper over the Graph REST API."""

    def __init__(self):
        self.base = _cfg("META_GRAPH_BASE_URL").rstrip("/")
        self.version = _cfg("META_GRAPH_VERSION")

    def _url(self, path):
        return f"{self.base}/{self.version}/{str(path).lstrip('/')}"

    @staticmethod
    def _auth(token):
        """Returns (headers, appsecret_proof) for an authenticated call.

        The token travels in the Authorization: Bearer header (never a query
        param, so it can't leak into logs), and every authenticated call
        carries appsecret_proof so the app is safe even with Meta's "Require
        App Secret for Server API calls" enabled. Unauthenticated calls
        (token=None, e.g. the code/token exchange which uses client_secret)
        get neither."""
        if not token:
            return {}, None
        return {"Authorization": f"Bearer {token}"}, appsecret_proof(token)

    def _handle(self, resp):
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            raise MetaGraphError(
                body.get("error") if isinstance(body, dict) else None,
                resp.status_code,
            )
        return body

    def get(self, path, token=None, params=None):
        p = dict(params or {})
        headers, proof = self._auth(token)
        if proof:
            p["appsecret_proof"] = proof
        return self._handle(requests.get(
            self._url(path), params=p, headers=headers,
            timeout=_TIMEOUT, allow_redirects=False))

    def post(self, path, token=None, data=None, timeout=None):
        d = dict(data or {})
        headers, proof = self._auth(token)
        params = {"appsecret_proof": proof} if proof else None
        return self._handle(requests.post(
            self._url(path), data=d, params=params, headers=headers,
            timeout=timeout or _UPLOAD_TIMEOUT, allow_redirects=False))

    def delete(self, path, token=None):
        headers, proof = self._auth(token)
        params = {"appsecret_proof": proof} if proof else None
        return self._handle(requests.delete(
            self._url(path), params=params, headers=headers,
            timeout=_TIMEOUT, allow_redirects=False))


def appsecret_proof(token):
    """HMAC-SHA256 of the access token keyed by the app secret - Meta's
    proof that a server-side call is genuinely from our app."""
    secret = _cfg("META_APP_SECRET")
    if not (secret and token):
        return None
    return hmac.new(
        secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def hosted_reel_upload(upload_url, token, file_url):
    """Phase 2 of the Reels flow: tell the returned rupload endpoint to
    fetch the (hosted) video by URL. Uses the OAuth header the reels upload
    host expects, plus appsecret_proof."""
    headers = {"Authorization": f"OAuth {token}", "file_url": file_url}
    params = {}
    proof = appsecret_proof(token)
    if proof:
        params["appsecret_proof"] = proof
    resp = requests.post(upload_url, headers=headers, params=params,
                         timeout=_UPLOAD_TIMEOUT, allow_redirects=False)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
        raise MetaGraphError(
            body.get("error") if isinstance(body, dict) else None,
            resp.status_code)
    return body


def build_login_url(scopes, state, redirect_uri):
    """The Facebook Login for Business consent URL."""
    base = _cfg("META_OAUTH_BASE_URL").rstrip("/")
    version = _cfg("META_GRAPH_VERSION")
    params = {
        "client_id": _cfg("META_APP_ID"),
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": ",".join(scopes),
        "response_type": "code",
    }
    return f"{base}/{version}/dialog/oauth?{urlencode(params)}"


def exchange_code_for_long_lived_token(code, redirect_uri):
    """Authorization code -> short-lived user token -> long-lived (~60d)
    user token. Returns (access_token, expires_at)."""
    graph = MetaGraph()
    app_id = _cfg("META_APP_ID")
    secret = _cfg("META_APP_SECRET")

    short = graph.get("oauth/access_token", params={
        "client_id": app_id, "client_secret": secret,
        "redirect_uri": redirect_uri, "code": code,
    })
    long_lived = graph.get("oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": secret, "fb_exchange_token": short["access_token"],
    })
    token = long_lived["access_token"]
    expires_in = long_lived.get("expires_in")
    expires_at = (
        datetime.utcnow() + timedelta(seconds=int(expires_in))
        if expires_in else None
    )
    return token, expires_at


def _me_user_id(user_token):
    """App-scoped id of the person who just authorised us.

    Best-effort: a connect must not fail because this lookup did. Without
    it the only cost is that an automated deletion request for that person
    falls back to manual handling, which the deletion page covers.
    """
    try:
        return MetaGraph().get("me", token=user_token,
                               params={"fields": "id"}).get("id")
    except Exception:  # noqa: BLE001 - never break a working connect
        return None


def granted_permissions(user_token):
    """Set of permissions the user actually granted (declined ones are
    filtered out), for scope validation."""
    graph = MetaGraph()
    resp = graph.get("me/permissions", token=user_token)
    return {
        row["permission"]
        for row in resp.get("data", [])
        if row.get("status") == "granted"
    }


def map_meta_error(exc):
    """Meta exception -> typed SocialError so the retry engine can act."""
    if isinstance(exc, MetaGraphError):
        code = exc.error.get("code")
        subcode = exc.error.get("error_subcode")
        message = exc.error.get("message", "Meta Graph error")
        if code in _AUTH_CODES:
            return AuthError(message, code=code)
        if code in _CONTENT_CODES:
            # The file is wrong, not the request. Retrying an identical
            # upload cannot help - the source file has to change - so this
            # must not be classified as transient however Meta's HTTP
            # status happens to come back. The message carries Meta's own
            # wording, which names the actual problem.
            return PermanentError(message, code=code)
        if code in _RATE_CODES or subcode in _RATE_SUBCODES:
            return RateLimitError(message, code=code)
        if exc.status_code >= 500:
            return TransientError(message, code=code)
        return PermanentError(message, code=code)
    if isinstance(exc, requests.exceptions.RequestException):
        return TransientError(str(exc))
    return PermanentError(str(exc))


class MetaBaseProvider(SocialProvider):
    """Shared OAuth / token / error behaviour. Subclasses set SCOPES,
    capabilities, and the publishing/discovery specifics."""

    SCOPES: list = []

    #: Providers sharing one OAuth consent. A single Meta login unlocks
    #: Facebook Pages AND their linked Instagram accounts, so the OAuth
    #: manager requests the union of scopes and runs discovery across every
    #: provider in the group (see OAuthManager.finish_all).
    connect_group = "meta"

    #: Edge used to reply to a comment. Facebook replies via
    #: /{comment_id}/comments; Instagram uses /{comment_id}/replies.
    comment_reply_edge = "comments"

    def graph(self):
        return MetaGraph()

    # -- OAuth -------------------------------------------------------------

    def connect_scopes(self):
        """Scopes to request at connect time. One Meta consent grants the
        whole family, so we request the union - connecting Facebook then
        also connects the Instagram Business account linked to each Page."""
        return META_UNIFIED_SCOPES

    def build_oauth_url(self, state, redirect_uri, scopes=None):
        return build_login_url(scopes or self.SCOPES, state, redirect_uri)

    def exchange_code(self, code, code_verifier, redirect_uri):
        token, expires_at = exchange_code_for_long_lived_token(code, redirect_uri)

        # Validate that the permissions we need were actually granted.
        missing = set(self._required_scopes()) - granted_permissions(token)
        if missing:
            raise PermanentError(
                "Missing required Meta permissions: " + ", ".join(sorted(missing))
            )

        return TokenBundle(
            access_token=token,
            token_expires_at=expires_at,
            scopes=",".join(self.SCOPES),
            # The app-scoped id of the person who granted consent. Meta's
            # data-deletion callback identifies the request by exactly this
            # id and nothing else, so without it a deletion request cannot
            # be matched to anything we hold. upsert_from_oauth merges
            # bundle.meta onto the account, so it lands automatically.
            meta={"user_token": True,
                  "connected_user_id": _me_user_id(token)},
        )

    def _required_scopes(self):
        """Publishing-critical scopes to verify post-consent (subset of
        SCOPES)."""
        return self.SCOPES

    def refresh_token(self, account):
        # Page tokens derived from a long-lived user token do not expire, so
        # there is nothing to refresh for Page/IG accounts. (User-token
        # refresh, if ever needed, re-exchanges fb_exchange_token.)
        return None

    # -- Errors ------------------------------------------------------------

    def map_error(self, exc):
        return map_meta_error(exc)

    # -- Media helpers -----------------------------------------------------

    @staticmethod
    def _media_url(media_ref):
        """A public, short-lived URL Meta can fetch the media from (R2
        presigned). Meta pulls media by URL; large files never stream
        through our app."""
        from app.social.media import pipeline
        return pipeline.presigned_url(media_ref.object_key)

    @staticmethod
    def _full_caption(content):
        caption = (content.caption or "").strip()
        hashtags = (content.hashtags or "").strip()
        return (caption + ("\n\n" + hashtags if hashtags else "")).strip()

    def _page_token(self, target):
        """Decrypt the stored per-Page token for this target's account."""
        from app.social.services.accounts import AccountManager
        return AccountManager.access_token(target.account)

    # -- First comment -----------------------------------------------------

    def post_first_comment(self, external_post_id, text, token):
        """Post `text` as a comment on the just-published post/media - the
        'hashtags / link in the first comment' pattern. Works for both a
        Facebook Page post and an Instagram media id via /{id}/comments.

        Note: on real Meta this needs the comment-management permission
        (pages_manage_engagement for Pages, instagram_manage_comments for
        IG). It is best-effort - the worker never fails a publish because a
        first comment couldn't be posted."""
        text = (text or "").strip()
        if not (external_post_id and text):
            return None
        resp = self.graph().post(f"{external_post_id}/comments", token=token,
                                 data={"message": text})
        return resp.get("id")

    # -- Engage: read + reply to comments ---------------------------------

    def list_comments(self, external_post_id, token, limit=50):
        """Recent comments on a published post/media, newest first, as
        normalized dicts: external_id, message, author_name, author_id,
        parent_external_id, created_time.

        Graph errors are RAISED, not swallowed. This used to return [] on
        any failure, which - combined with the caller's own bare except -
        meant a refused request was indistinguishable from a post with no
        comments: Engage reported "you're all caught up" while actually
        being locked out. sync_comments catches per post, so one bad post
        still cannot abort the run; it just gets reported now.
        """
        if not external_post_id:
            return []
        resp = self.graph().get(
            f"{external_post_id}/comments", token=token,
            params={
                "fields": "id,message,from,username,created_time,parent",
                "limit": limit,
            })
        out = []
        for c in (resp.get("data") or []):
            frm = c.get("from") or {}
            out.append({
                "external_id": c.get("id"),
                "message": c.get("message"),
                "author_name": frm.get("name") or c.get("username"),
                "author_id": frm.get("id"),
                "parent_external_id": (c.get("parent") or {}).get("id"),
                "created_time": c.get("created_time"),
            })
        return out

    def reply_to_comment(self, comment_external_id, text, token):
        """Publish a reply to a comment. Returns the new comment id, or None.
        Needs the comment-management permission on real Meta
        (pages_manage_engagement / instagram_manage_comments)."""
        text = (text or "").strip()
        if not (comment_external_id and text):
            return None
        resp = self.graph().post(
            f"{comment_external_id}/{self.comment_reply_edge}", token=token,
            data={"message": text})
        return resp.get("id")

    # -- Delete / existence -----------------------------------------------

    def delete_post(self, external_post_id, token):
        """Delete a published post on the platform. Default: not supported
        (Instagram media cannot be deleted via the Graph API)."""
        raise PermanentError(
            "Deleting a published post isn't supported for this platform via "
            "the API - remove it directly on the platform.")

    def post_exists(self, external_post_id, token):
        """True if the post still exists on the platform. A clear not-found
        (Meta code 100/803) means it was deleted there; any other error is
        treated as 'still exists' so a transient glitch never wrongly marks a
        live post as removed."""
        if not external_post_id:
            return True
        try:
            self.graph().get(external_post_id, token=token,
                             params={"fields": "id"})
            return True
        except MetaGraphError as exc:
            code = exc.error.get("code")
            if code in (100, 803) or exc.status_code == 404:
                return False
            return True
        except requests.exceptions.RequestException:
            return True
