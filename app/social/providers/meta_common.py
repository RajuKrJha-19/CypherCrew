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
#
# This list must equal what App Review approved. Both directions of drift
# are silent: a scope approved but missing here is never granted, and a
# scope requested here but not approved is never granted either. Neither
# raises anything - the feature just does nothing.
META_UNIFIED_SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    # Reading the COMMENTS other people leave on a Page post - the Engage
    # inbox. pages_read_engagement is not enough: it covers the Page's own
    # content, while visitor-generated content needs this one. It is also
    # a declared dependency of instagram_basic, so the Instagram half of
    # the connect leans on it too.
    "pages_read_user_content",
    "pages_manage_posts",
    "pages_manage_engagement",
    "read_insights",
    # REQUIRED for discovering Pages owned by a Business Manager. A previous
    # change dropped this on the belief that "nothing calls a Business Manager
    # endpoint - not /me/accounts" - which is wrong: /me/accounts only returns
    # a business-portfolio Page (an agency managing a client's Page, the norm
    # here) when the token carries business_management. Without it those Pages
    # silently vanish from discovery - "Meta granted 4, Studio connected 3".
    # Must be included in the App Review submission for public/live use; in
    # Development/Testing mode it is granted to admins/devs/testers already.
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
    # Enumerating a client's ADS (act_<id>/ads) to pull comments on ad/boosted
    # posts into Engage. Read-only. Like business_management, it is granted to
    # app admins/devs/testers in Development/Testing mode without public App
    # Review; for public/live use it must be in the App Review submission. The
    # feature stays dormant behind SOCIAL_ADS_COMMENTS_ENABLED regardless.
    "ads_read",
    # Subscribing each connected Page to the app's webhooks (POST
    # /{page-id}/subscribed_apps) so real-time comment events are delivered -
    # app-level field subscriptions alone don't deliver a specific page's
    # events. Granted to admins/devs/testers in Development/Testing; part of the
    # App Review submission for public/live use. Only used when
    # META_WEBHOOK_ENABLED.
    "pages_manage_metadata",
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
        # Without this, Meta NEVER re-asks for a permission the user has
        # already declined - or one that simply was not granted on an
        # earlier authorization, which is every scope added to
        # META_UNIFIED_SCOPES after someone first connected. The dialog
        # renders, the user presses Save, the callback succeeds, and the
        # new scopes are silently absent from the token. auth_type=rerequest
        # is the documented way to make the dialog ask again.
        "auth_type": "rerequest",
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

    #: Scopes we only ask for once the feature that needs them is switched on.
    #: Least-privilege: requesting a permission whose feature is dormant is both
    #: an unnecessary data-access surface and the classic App-Review rejection
    #: ("we couldn't see this permission used"). Keyed scope -> the config flag
    #: that turns its feature on.
    _FLAG_GATED_SCOPES = {
        "ads_read": "SOCIAL_ADS_COMMENTS_ENABLED",
        "pages_manage_metadata": "META_WEBHOOK_ENABLED",
    }

    def connect_scopes(self):
        """Scopes to request at connect time. One Meta consent grants the
        whole family, so we request the union - connecting Facebook then
        also connects the Instagram Business account linked to each Page.

        Feature-gated scopes are dropped while their feature is off, so we ask
        only for what the running configuration actually uses. Meta remembers a
        granted scope, so enabling a feature later just needs a one-time
        reconnect of that channel to pick the new permission up.
        """
        cfg = current_app.config
        return [s for s in META_UNIFIED_SCOPES
                if s not in self._FLAG_GATED_SCOPES
                or cfg.get(self._FLAG_GATED_SCOPES[s])]

    def build_oauth_url(self, state, redirect_uri, scopes=None):
        return build_login_url(scopes or self.SCOPES, state, redirect_uri)

    def exchange_code(self, code, code_verifier, redirect_uri):
        token, expires_at = exchange_code_for_long_lived_token(code, redirect_uri)

        # What Meta ACTUALLY granted. Fetched once and used for both the
        # required-scope gate and the record we persist.
        granted = granted_permissions(token)

        # Validate that the permissions we need were actually granted.
        missing = set(self._required_scopes()) - granted
        if missing:
            raise PermanentError(
                "Missing required Meta permissions: " + ", ".join(sorted(missing))
            )

        # The requested scopes that did NOT come back. _required_scopes is
        # deliberately narrow (publishing only), so a missing comment or
        # insights permission does not fail the connect - which is right,
        # but it must not be invisible either: the caller flashes this so a
        # partial grant is never reported as a complete one.
        requested = self.connect_scopes() or self.SCOPES
        ungranted = [s for s in requested if s not in granted]

        return TokenBundle(
            access_token=token,
            token_expires_at=expires_at,
            # Meta's own answer, not our declared wish list. Recording
            # self.SCOPES here meant the row claimed every permission the
            # adapter wants however little the user actually granted, so a
            # dropped scope was unobservable after the fact.
            scopes=",".join(sorted(granted)),
            # The app-scoped id of the person who granted consent. Meta's
            # data-deletion callback identifies the request by exactly this
            # id and nothing else, so without it a deletion request cannot
            # be matched to anything we hold. upsert_from_oauth merges
            # bundle.meta onto the account, so it lands automatically.
            meta={"user_token": True,
                  "connected_user_id": _me_user_id(token),
                  "ungranted_scopes": ungranted},
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
        # Facebook and Instagram comments have DIFFERENT schemas: Facebook
        # returns message/from{name,picture}/created_time; Instagram returns
        # text/username/timestamp and has no `from`. Ask each for its own
        # fields (a wrong field name errors the whole call), then map with
        # `or`-fallbacks so the local emulator - which answers with the
        # Facebook shape for both - keeps working too.
        if self.key == "instagram":
            fields = "id,text,username,timestamp"
        else:
            fields = "id,message,from{id,name,picture},created_time,parent"
        resp = self.graph().get(
            f"{external_post_id}/comments", token=token,
            params={"fields": fields, "limit": limit})
        out = []
        for c in (resp.get("data") or []):
            frm = c.get("from") or {}
            pic = (((frm.get("picture") or {}).get("data")) or {}).get("url")
            out.append({
                "external_id": c.get("id"),
                "message": c.get("message") or c.get("text"),
                "author_name": frm.get("name") or c.get("username"),
                "author_id": frm.get("id"),
                "author_pic": pic,
                "parent_external_id": (c.get("parent") or {}).get("id"),
                "created_time": c.get("created_time") or c.get("timestamp"),
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

    # -- Moderation (hide is reversible; delete is permanent) --------------

    #: Field that hides/unhides a comment. Facebook uses is_hidden, Instagram
    #: uses hide; both take true/false. Needs the comment-management scope
    #: (pages_manage_engagement / instagram_manage_comments).
    comment_hide_field = "is_hidden"

    def set_comment_hidden(self, comment_external_id, token, hidden=True):
        """Hide (or unhide) a comment on the platform. Reversible."""
        if not comment_external_id:
            return False
        self.graph().post(
            comment_external_id, token=token,
            data={self.comment_hide_field: "true" if hidden else "false"})
        return True

    def delete_comment(self, comment_external_id, token):
        """Permanently delete a comment on the platform (not reversible)."""
        if not comment_external_id:
            return False
        self.graph().delete(comment_external_id, token=token)
        return True

    # -- Webhooks (per-Page subscription) ---------------------------------

    def subscribe_app_to_page(self, page_id, token, fields="feed"):
        """Subscribe this app to a Page's webhooks (POST
        /{page-id}/subscribed_apps) so Meta delivers that page's real-time
        events. App-level field subscriptions alone don't deliver a specific
        page's events without this per-page step. Needs a Page token with
        pages_manage_metadata. Idempotent - Meta upserts the subscription."""
        if not page_id:
            return False
        self.graph().post(f"{page_id}/subscribed_apps", token=token,
                          data={"subscribed_fields": fields})
        return True

    # -- Ads (comment ingestion for ad/boosted posts) ---------------------

    def list_ad_posts(self, ad_account_id, token, limit=100):
        """Distinct ad/boosted POST ids under an ad account, as a list of
        {"platform", "external_post_id"}. A creative's
        effective_object_story_id is the Facebook (dark) page post; its
        effective_instagram_media_id is the Instagram media. Needs ads_read.
        Best-effort - the caller logs failures."""
        resp = self.graph().get(f"{ad_account_id}/ads", token=token, params={
            "fields": "creative{effective_object_story_id,"
                      "effective_instagram_media_id}",
            "limit": limit,
        })
        out, seen = [], set()
        for ad in (resp.get("data") or []):
            creative = ad.get("creative") or {}
            story = creative.get("effective_object_story_id")
            media = creative.get("effective_instagram_media_id")
            if story and story not in seen:
                seen.add(story)
                out.append({"platform": "facebook", "external_post_id": story})
            if media and media not in seen:
                seen.add(media)
                out.append({"platform": "instagram", "external_post_id": media})
        return out

    #: Fields describing the post itself, per platform. Facebook Page posts and
    #: Instagram media have different names for the same three things, and
    #: asking one for the other's fields errors the whole call.
    _DETAIL_FIELDS = "message,full_picture,permalink_url"

    def fetch_post_details(self, external_post_id, token):
        """The post's own caption, picture and permalink, as
        {"caption", "thumbnail_url", "permalink"}.

        Used to give the Engage inbox something real to show above a comment,
        and to give the AI the post's caption as context. An ad post arrives
        from the ads endpoint as nothing but an id, so without this the whole
        context is the placeholder title "Ad post".

        Reads only what the app is already permitted to read
        (pages_read_engagement / instagram_basic) - no ads permission.
        Best-effort: any failure returns empty, and the caller keeps whatever
        it had.
        """
        if not external_post_id:
            return {}
        try:
            resp = self.graph().get(external_post_id, token=token,
                                    params={"fields": self._DETAIL_FIELDS})
        except Exception:  # noqa: BLE001 - a preview is never worth a failure
            return {}
        return self._map_post_details(resp)

    @staticmethod
    def _map_post_details(resp):
        return {
            "caption": resp.get("message") or None,
            "thumbnail_url": resp.get("full_picture") or None,
            "permalink": resp.get("permalink_url") or None,
        }

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
