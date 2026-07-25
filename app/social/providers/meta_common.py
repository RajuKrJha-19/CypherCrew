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

    def graph(self):
        return MetaGraph()

    # -- OAuth -------------------------------------------------------------

    def build_oauth_url(self, state, redirect_uri):
        return build_login_url(self.SCOPES, state, redirect_uri)

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
            meta={"user_token": True},
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
