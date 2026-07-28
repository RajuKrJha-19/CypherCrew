"""Shared Google machinery for the YouTube + Business Profile adapters.

One place for the OAuth handshake, the authorised HTTP client, token
refresh and Google-error -> typed-SocialError mapping, mirroring what
meta_common.py does for the Meta family.

Two things differ from Meta and drive most of the design here:

1. **Google access tokens live one hour.** Meta hands out Page tokens that
   effectively never expire, so the engine could treat a stored token as
   good forever. Google cannot: a post scheduled for tomorrow morning will
   run with a token that died overnight. So every exchange keeps the
   refresh token, and AccountManager.access_token refreshes just before
   use. `access_type=offline` + `prompt=consent` on the consent URL are
   what make a refresh token arrive at all - without them Google returns
   only an access token and the connection silently rots after an hour.

2. **Google does not fetch media by URL for YouTube.** Meta pulls a
   presigned URL; YouTube wants the bytes pushed to it over a resumable
   session. Business Profile is the Meta-like case and does take a URL.
"""

from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import current_app

from app.social.dto import TokenBundle
from app.social.errors import (
    AuthError, PermanentError, RateLimitError, TransientError,
)

_TIMEOUT = 30
#: Uploads stream a whole video; the default would abort mid-file.
_UPLOAD_TIMEOUT = 900

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Google reason strings that mean "slow down", not "you are broken".
_RATE_REASONS = {
    "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
    "dailyLimitExceeded", "backendError",
}
_AUTH_REASONS = {
    "authError", "unauthorized", "insufficientPermissions",
    "forbidden", "accessNotConfigured",
}


def cfg(key, default=None):
    return current_app.config.get(key, default)


class GoogleHTTPError(Exception):
    """Any non-2xx from a Google API. Carries the parsed error object so
    map_google_error can classify it."""

    def __init__(self, error, status_code):
        self.error = error or {}
        self.status_code = status_code
        super().__init__(self.error.get("message") or
                         f"Google API error {status_code}")

    @property
    def reason(self):
        errors = self.error.get("errors") or []
        if errors and isinstance(errors, list):
            return errors[0].get("reason")
        return self.error.get("status")


class GoogleClient:
    """Thin authorised JSON client. Base URL per API, since Google splits
    one product across several hosts."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _url(self, path):
        return path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _auth(token):
        return {"Authorization": f"Bearer {token}"}

    def _handle(self, resp):
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError:
            if resp.ok:
                return {}
            raise GoogleHTTPError({"message": resp.text[:300]},
                                  resp.status_code)
        if not resp.ok or "error" in body:
            error = body.get("error")
            if isinstance(error, str):
                error = {"message": body.get("error_description", error),
                         "status": error}
            raise GoogleHTTPError(error, resp.status_code)
        return body

    def get(self, path, token, params=None):
        return self._handle(requests.get(
            self._url(path), headers=self._auth(token), params=params or {},
            timeout=_TIMEOUT))

    def post(self, path, token, json=None, params=None):
        return self._handle(requests.post(
            self._url(path), headers=self._auth(token), json=json or {},
            params=params or {}, timeout=_TIMEOUT))

    def delete(self, path, token, params=None):
        return self._handle(requests.delete(
            self._url(path), headers=self._auth(token), params=params or {},
            timeout=_TIMEOUT))


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def build_google_oauth_url(state, redirect_uri, scopes):
    """Consent URL.

    access_type=offline and prompt=consent are load-bearing: without both,
    Google returns no refresh token on a repeat authorisation, and the
    connection stops working an hour later with no obvious cause.
    """
    params = {
        "client_id": cfg("GOOGLE_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _token_request(data):
    resp = requests.post(TOKEN_URL, data=data, timeout=_TIMEOUT)
    try:
        body = resp.json()
    except ValueError:
        raise PermanentError("Google returned an unreadable token response.")
    if not resp.ok:
        raise AuthError(
            body.get("error_description") or body.get("error")
            or "Google token request failed")
    return body


def exchange_google_code(code, redirect_uri):
    body = _token_request({
        "code": code,
        "client_id": cfg("GOOGLE_CLIENT_ID"),
        "client_secret": cfg("GOOGLE_CLIENT_SECRET"),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })

    refresh = body.get("refresh_token")
    if not refresh:
        # Recoverable only by re-consenting, so say what to do rather than
        # storing a token that dies in an hour.
        raise PermanentError(
            "Google did not return a refresh token. Remove this app's access "
            "at myaccount.google.com/permissions and connect again so the "
            "consent screen is shown afresh."
        )

    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=refresh,
        token_expires_at=_expiry(body.get("expires_in")),
        scopes=body.get("scope", ""),
        meta={"provider": "google"},
    )


def refresh_google_token(refresh_token):
    """New access token from a stored refresh token.

    Google does not reissue the refresh token on a normal refresh, so the
    bundle carries it back unchanged - AccountManager.store_refreshed only
    overwrites when one is present.
    """
    body = _token_request({
        "refresh_token": refresh_token,
        "client_id": cfg("GOOGLE_CLIENT_ID"),
        "client_secret": cfg("GOOGLE_CLIENT_SECRET"),
        "grant_type": "refresh_token",
    })
    return TokenBundle(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or refresh_token,
        token_expires_at=_expiry(body.get("expires_in")),
        scopes=body.get("scope", ""),
        meta={"provider": "google"},
    )


def _expiry(expires_in):
    if not expires_in:
        return None
    return datetime.utcnow() + timedelta(seconds=int(expires_in))


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

def map_google_error(exc):
    """Google failure -> typed SocialError, so the retry engine can act.

    Quota is the interesting case: YouTube's daily upload allowance is
    small, and exhausting it is a "try tomorrow", not a dead post - so it
    is transient, and the queue's backoff handles it.
    """
    if isinstance(exc, requests.exceptions.RequestException):
        return TransientError(f"Network error talking to Google: {exc}")

    if not isinstance(exc, GoogleHTTPError):
        return PermanentError(str(exc))

    status = exc.status_code
    reason = exc.reason or ""

    if status == 401 or reason in _AUTH_REASONS:
        return AuthError(str(exc))
    if status == 429 or reason in _RATE_REASONS:
        return RateLimitError(str(exc))
    if status >= 500:
        return TransientError(str(exc))
    if status == 403:
        # 403 is overloaded: quota (retry) vs permission (re-auth).
        return RateLimitError(str(exc)) if "quota" in str(exc).lower() \
            else AuthError(str(exc))
    return PermanentError(str(exc))


class GoogleBaseProvider:
    """Shared OAuth/token behaviour. Adapters mix this in alongside
    SocialProvider."""

    #: Scopes this adapter asks for at consent time.
    SCOPES = []

    def build_oauth_url(self, state, redirect_uri, scopes=None):
        return build_google_oauth_url(state, redirect_uri,
                                      scopes or self.SCOPES)

    def exchange_code(self, code, code_verifier, redirect_uri):
        return exchange_google_code(code, redirect_uri)

    def refresh_token(self, account):
        from app.social.services.accounts import AccountManager
        refresh = AccountManager.refresh_token_value(account)
        if not refresh:
            return None
        return refresh_google_token(refresh)

    def map_error(self, exc):
        return map_google_error(exc)
