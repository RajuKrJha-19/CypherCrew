"""YouTube publisher (Data API v3, resumable upload).

Unlike Meta and Business Profile, YouTube will not fetch media from a URL -
the bytes have to be pushed to it. That is done with Google's resumable
upload protocol, which maps onto the engine's job state machine almost
exactly:

    start_publish  opens an upload session, stores the session URI ->
                   PENDING
    poll_publish   asks the session how much it already has, streams the
                   rest, and returns DONE with the video id (or stays
                   PENDING if the connection dropped part-way)

Doing it this way means an interrupted upload resumes from where it
stopped on the next worker cycle instead of starting a 200 MB video again.
The session URI in provider_state is the whole reason a retry is cheap.

Quota is the operational catch: an upload costs 1600 units against a
default 10,000/day project quota, so roughly six videos a day until Google
raises it. Exhausting it is classified as a rate-limit (retry tomorrow),
not a dead post.
"""

import requests

from app.social.dto import AccountInfo, Capabilities, PublishStep, StepStatus
from app.social.errors import PermanentError, TransientError
from app.social.providers.base import SocialProvider
from app.social.providers.google_common import (
    GoogleBaseProvider, GoogleClient, GoogleHTTPError, cfg,
)

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"

#: Streamed to YouTube in chunks of this size when resuming.
_CHUNK = 8 * 1024 * 1024
_UPLOAD_TIMEOUT = 900


class YouTubeProvider(GoogleBaseProvider, SocialProvider):
    key = "youtube"
    connectable = True

    SCOPES = [
        # Upload a video. The narrow upload-only scope is deliberate: it is
        # the least Google will accept for publishing.
        "https://www.googleapis.com/auth/youtube.upload",
        # Read the channel list at connect time, and video statistics.
        "https://www.googleapis.com/auth/youtube.readonly",
        # Comments (first comment + the Engage inbox). force-ssl is the only
        # scope that permits writing them.
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]

    capabilities = Capabilities(
        post_types={"video"},
        requires_container_poll=True,
        supports_native_scheduling=True,
        max_caption_chars=5000,
        # Google's documented ceiling is 100 uploads/day, but the 10,000
        # unit project quota bites first at ~6. The lower, honest number is
        # what the composer should warn against.
        publish_rate=(6, "24h"),
        supports_first_comment=True,
        supports_comments=True,
        supports_delete=True,
    )

    # -- Discovery ---------------------------------------------------------

    def list_publishable_accounts(self, token):
        resp = GoogleClient(API).get("channels", token=token, params={
            "part": "id,snippet", "mine": "true",
        })
        accounts = []
        for item in resp.get("items", []):
            snippet = item.get("snippet") or {}
            accounts.append(AccountInfo(
                external_id=item["id"],
                display_name=snippet.get("title") or item["id"],
                account_type="channel",
                meta={"channel_id": item["id"]},
            ))
        return accounts

    # -- Validation --------------------------------------------------------

    def validate(self, content):
        from app.social.media import pipeline
        problems = pipeline.validate_against(self.capabilities, content)
        if not content.media:
            problems.append("A YouTube post needs a video file.")
        elif not (content.media[0].mime_type or "").startswith("video"):
            problems.append("YouTube only accepts a video file.")
        if not (content.caption or "").strip():
            problems.append("A YouTube video needs a title.")
        return problems

    # -- Publishing --------------------------------------------------------

    def start_publish(self, target, content, token):
        """Open a resumable session. No bytes move yet."""
        media = content.media[0]
        source_url = self._source_url(media)
        size = self._remote_size(source_url)

        title, description = self._title_and_description(content)
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "categoryId": cfg("YOUTUBE_CATEGORY_ID", "22"),
            },
            "status": {
                "privacyStatus": cfg("YOUTUBE_PRIVACY_STATUS", "public"),
                # Required by Google; the agency declares this per channel,
                # not per post, so it comes from config.
                "selfDeclaredMadeForKids": bool(
                    cfg("YOUTUBE_MADE_FOR_KIDS", False)),
            },
        }

        resp = requests.post(
            UPLOAD_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": media.mime_type or "video/*",
                **({"X-Upload-Content-Length": str(size)} if size else {}),
            },
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=body,
            timeout=60,
        )
        if not resp.ok:
            raise GoogleHTTPError(
                (resp.json().get("error") if resp.content else None),
                resp.status_code)

        session_uri = resp.headers.get("Location")
        if not session_uri:
            raise TransientError(
                "YouTube did not return an upload session URI.")

        return PublishStep(
            status=StepStatus.PENDING.value,
            provider_state={
                "session_uri": session_uri,
                "source_url": source_url,
                "size": size,
                "mime": media.mime_type or "video/*",
            },
        )

    def poll_publish(self, target, provider_state, token):
        """Push the remaining bytes. DONE when YouTube returns the video."""
        session_uri = provider_state["session_uri"]
        size = provider_state.get("size")
        mime = provider_state.get("mime") or "video/*"

        offset = self._session_offset(session_uri, size, token)
        if offset is None:
            # The session answered with the finished video resource.
            return self._done(provider_state.get("video_id"), token)

        video_id = self._upload_from(session_uri, provider_state["source_url"],
                                     offset, size, mime, token)
        if video_id is None:
            # Interrupted - stay PENDING and resume next cycle.
            return PublishStep(status=StepStatus.PENDING.value,
                               provider_state=provider_state)
        return self._done(video_id, token)

    def _done(self, video_id, token):
        if not video_id:
            raise TransientError("YouTube upload finished without a video id.")
        return PublishStep(
            status=StepStatus.DONE.value,
            external_post_id=video_id,
            permalink=f"https://www.youtube.com/watch?v={video_id}",
        )

    # -- Resumable upload helpers -----------------------------------------

    @staticmethod
    def _source_url(media):
        from app.social.media import pipeline
        return pipeline.presigned_url(media.object_key)

    @staticmethod
    def _remote_size(source_url):
        """Total size, so the session can be opened with a known length and
        an interrupted upload can be resumed accurately."""
        try:
            head = requests.head(source_url, timeout=30, allow_redirects=True)
            return int(head.headers["Content-Length"])
        except Exception:  # noqa: BLE001 - length is an optimisation
            return None

    def _session_offset(self, session_uri, size, token):
        """How many bytes YouTube already holds.

        None means the upload is already complete.
        """
        resp = requests.put(
            session_uri,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Length": "0",
                "Content-Range": f"bytes */{size if size else '*'}",
            },
            timeout=60,
        )
        if resp.status_code in (200, 201):
            return None
        if resp.status_code == 308:
            rng = resp.headers.get("Range")
            # No Range header means nothing has been stored yet.
            return int(rng.split("-")[1]) + 1 if rng else 0
        if resp.status_code == 404:
            raise TransientError(
                "YouTube upload session expired; it will start again.")
        raise GoogleHTTPError(
            (resp.json().get("error") if resp.content else None),
            resp.status_code)

    def _upload_from(self, session_uri, source_url, offset, size, mime, token):
        """Stream the object from storage straight into the session.

        Streamed rather than buffered: a video is exactly the kind of file
        that should never be held in the web process's memory.
        """
        headers = {"Authorization": f"Bearer {token}", "Content-Type": mime}
        get_headers = {"Range": f"bytes={offset}-"} if offset else {}

        with requests.get(source_url, stream=True, timeout=120,
                          headers=get_headers) as src:
            src.raise_for_status()
            if size:
                headers["Content-Length"] = str(size - offset)
                headers["Content-Range"] = \
                    f"bytes {offset}-{size - 1}/{size}"

            resp = requests.put(
                session_uri, headers=headers,
                data=src.iter_content(chunk_size=_CHUNK),
                timeout=_UPLOAD_TIMEOUT,
            )

        if resp.status_code in (200, 201):
            return (resp.json() or {}).get("id")
        if resp.status_code == 308:
            return None
        raise GoogleHTTPError(
            (resp.json().get("error") if resp.content else None),
            resp.status_code)

    @staticmethod
    def _title_and_description(content):
        """YouTube has a title and a description; the composer has one
        caption. First line is the title, the rest the description - the
        convention every YouTube tool uses."""
        caption = (content.caption or "").strip()
        hashtags = (content.hashtags or "").strip()
        title, _, rest = caption.partition("\n")
        description = rest.strip()
        if hashtags:
            description = (description + "\n\n" + hashtags).strip()
        return (title or caption or "Untitled"), description

    # -- Deletion ----------------------------------------------------------

    def delete_post(self, target, token):
        GoogleClient(API).delete("videos", token=token,
                                 params={"id": target.external_post_id})
        return True

    # -- Comments ----------------------------------------------------------

    def post_first_comment(self, external_post_id, text, token):
        text = (text or "").strip()
        if not (external_post_id and text):
            return None
        resp = GoogleClient(API).post(
            "commentThreads", token=token, params={"part": "snippet"},
            json={"snippet": {
                "videoId": external_post_id,
                "topLevelComment": {"snippet": {"textOriginal": text}},
            }})
        return resp.get("id")

    def list_comments(self, external_post_id, token, limit=50):
        if not external_post_id:
            return []
        try:
            resp = GoogleClient(API).get("commentThreads", token=token, params={
                "part": "snippet", "videoId": external_post_id,
                "maxResults": min(limit, 100), "order": "time",
            })
        except Exception:  # noqa: BLE001 - one bad video must not abort a sync
            return []

        comments = []
        for item in resp.get("items", []):
            top = ((item.get("snippet") or {})
                   .get("topLevelComment") or {}).get("snippet") or {}
            comments.append({
                "external_id": item.get("id"),
                "message": top.get("textOriginal") or top.get("textDisplay"),
                "author_name": top.get("authorDisplayName"),
                "author_id": ((top.get("authorChannelId") or {}).get("value")),
                "parent_external_id": None,
                "created_time": top.get("publishedAt"),
            })
        return comments

    def reply_to_comment(self, comment_external_id, text, token):
        resp = GoogleClient(API).post(
            "comments", token=token, params={"part": "snippet"},
            json={"snippet": {
                "parentId": comment_external_id,
                "textOriginal": text,
            }})
        return resp.get("id")

    # -- Analytics ---------------------------------------------------------

    def fetch_analytics(self, target, token):
        if not target.external_post_id:
            return {}
        try:
            resp = GoogleClient(API).get("videos", token=token, params={
                "part": "statistics", "id": target.external_post_id,
            })
        except Exception:  # noqa: BLE001
            return {}
        items = resp.get("items") or []
        if not items:
            return {}
        stats = items[0].get("statistics") or {}
        return {
            "impressions": _int(stats.get("viewCount")),
            "views": _int(stats.get("viewCount")),
            "likes": _int(stats.get("likeCount")),
            "comments": _int(stats.get("commentCount")),
            "favourites": _int(stats.get("favoriteCount")),
        }


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
