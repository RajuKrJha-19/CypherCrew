"""Facebook Pages publisher (Graph API v25).

Discovers the Pages a user manages, publishes text / photo / video / reel /
carousel to a Page, and reads Page-post insights. Scheduling is engine-owned
(the worker calls publish at the due time), so this always publishes
immediately - native scheduled_publish_time is intentionally not used.
"""

from app.social.dto import (
    AccountInfo, Capabilities, MediaSpec, PublishStep, StepStatus,
)
from app.social.errors import PermanentError, TransientError
from app.social.providers.meta_common import MetaBaseProvider, hosted_reel_upload


class MetaFacebookProvider(MetaBaseProvider):
    key = "facebook"
    SCOPES = [
        "pages_show_list",
        "pages_read_engagement",
        # Engage reads GET /{post-id}/comments, which returns content other
        # people wrote. pages_read_engagement only covers what the PAGE
        # posted; visitor-generated content is this permission. Without it
        # the inbox is refused, and list_comments raises rather than
        # reporting "you're all caught up" while locked out.
        "pages_read_user_content",
        "pages_manage_posts",
        # Writing comments - the auto first comment, and replying from
        # Engage. Without it POST /{post-id}/comments is refused, and
        # because a first comment is best-effort the refusal was swallowed
        # into a log line: the feature simply did nothing, forever.
        "pages_manage_engagement",
        # Analytics reads /{post-id}/insights. Same trap as the comment
        # scope: fetch_analytics swallows errors and returns {}, so without
        # this the Analytics screen is simply always empty.
        "read_insights",
    ]
    capabilities = Capabilities(
        post_types={"text", "image", "video", "reel", "carousel", "story"},
        media_specs={
            # Facebook Reels are far stricter than Instagram's: 9:16
            # exactly, 90 seconds, and a floor on resolution. This is why
            # the fallback to a plain video post has to exist - a
            # landscape or long clip is a perfectly good Facebook video
            # and simply cannot be a Facebook Reel.
            "reel": MediaSpec(
                aspect_min=0.5625, aspect_max=0.5625,   # 9:16
                duration_min=3, duration_max=90,
                width_min=540, height_min=960,
                fps_min=24, fps_max=60,
                codecs=("h264", "hevc", "vp9", "av1"),
                aspect_label="9:16",
                display_aspect=0.5625, display_label="9:16",
            ),
            # A Page video has no comparable published limit worth
            # enforcing here; anything the reel spec rejects lands here.
            "video": MediaSpec(),
            # A story is photo or video; Facebook (like Instagram) caps the
            # width at 1920px. Kept deliberately loose - only the genuinely
            # oversized file is caught, and the on-publish downscaler then
            # fixes it (see media/transcode.py) - so a normal 1080x1920
            # story is never blocked and a photo story is left to Meta.
            "story": MediaSpec(
                aspect_min=0.01, aspect_max=10.0,
                width_max=1920,
                max_bytes=300 * 1024 * 1024,
                aspect_label="between 0.01:1 and 10:1",
                display_aspect=0.5625, display_label="9:16",
            ),
        },
        supports_native_scheduling=True,   # available, but engine owns timing
        max_carousel=10,
        max_caption_chars=63206,
        story_support=True,
        # FB, like IG, exposes no sticker/link parameter on a story via the
        # API, so a "story that links to a post" leaves the same in-app
        # follow-up (needs_story_link) rather than attaching a live sticker.
        story_link_support=False,
        supports_first_comment=True,
        supports_delete=True,
        supports_comments=True,
    )

    def delete_post(self, external_post_id, token):
        """Delete a Facebook Page post (or Reel/video) via the Graph API."""
        self.graph().delete(external_post_id, token=token)
        return True

    def _required_scopes(self):
        return ["pages_manage_posts", "pages_show_list"]

    # -- Discovery ---------------------------------------------------------

    #: Page-level tasks that permit publishing. CREATE_CONTENT is the direct
    #: one; MANAGE (full control) implies it, and Meta occasionally returns
    #: MANAGE without listing CREATE_CONTENT - dropping such a page would
    #: silently lose a Page the user can absolutely publish to.
    _PUBLISH_TASKS = {"CREATE_CONTENT", "MANAGE"}

    def list_publishable_accounts(self, token):
        """Pages the user manages, each with its own Page token. Only Pages
        the user can publish to (CREATE_CONTENT or MANAGE task) are kept.

        Pages skipped for lack of a publish task are logged BY NAME - a
        silently-dropped Page is exactly the "Meta says 4, Studio says 3"
        confusion, so make it visible in the logs at least.
        """
        params = {"fields": "id,name,access_token,tasks", "limit": 100}
        resp = self.graph().get("me/accounts", token=token, params=params)
        data = list(resp.get("data", []))
        # Follow cursor pagination so an account with many Pages isn't
        # truncated. We re-request the SAME endpoint with the `after` cursor
        # rather than following paging.next: paging.next is an ABSOLUTE URL,
        # and MetaGraph._url would prepend base+version to it, producing a
        # malformed request that would crash discovery. Capped so a provider
        # that always echoes a cursor can never loop forever.
        guard = 0
        while guard < 20:
            paging = resp.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not (paging.get("next") and after):
                break
            guard += 1
            resp = self.graph().get(
                "me/accounts", token=token, params={**params, "after": after})
            data.extend(resp.get("data", []))

        accounts, skipped = [], []
        for page in data:
            page_id = page.get("id")
            if not page_id:
                continue  # a Page with no id is unusable - skip defensively
            name = page.get("name") or page_id
            tasks = page.get("tasks") or []
            # tasks present but none of them permit publishing -> skip.
            if tasks and not (self._PUBLISH_TASKS & set(tasks)):
                skipped.append((name, tasks))
                continue
            accounts.append(AccountInfo(
                external_id=page_id,
                display_name=name,
                account_type="page",
                access_token=page.get("access_token"),
                meta={"page_id": page_id},
            ))

        if skipped:
            try:
                from flask import current_app
                current_app.logger.warning(
                    "[meta_facebook] skipped %d Page(s) with no publish task "
                    "(the user's role there can't create content): %s",
                    len(skipped),
                    "; ".join(f"{name} tasks={tasks}" for name, tasks in skipped),
                )
            except Exception:  # noqa: BLE001 - logging must never break connect
                pass
        return accounts

    # -- Validation --------------------------------------------------------

    def validate(self, content):
        from app.social.media import pipeline
        problems = pipeline.validate_against(self.capabilities, content)
        if content.post_type in ("image", "video", "reel", "story") \
                and not content.media:
            problems.append(f"A {content.post_type} post needs a media file.")
        if content.post_type == "text" and not (content.caption or "").strip():
            problems.append("A text post needs a caption.")
        return problems

    # -- Publishing --------------------------------------------------------
    # Text / photo / carousel are synchronous (return DONE). Video and Reels
    # are ASYNCHRONOUS on Facebook: the upload is accepted, then Meta
    # processes it, so we return PENDING and poll status until it is live.

    def start_publish(self, target, content, token):
        graph = self.graph()
        page_id = target.account.external_id
        caption = self._full_caption(content)

        if content.post_type == "text" or not content.media:
            resp = graph.post(f"{page_id}/feed", token=token,
                              data={"message": caption})
            return self._done(resp.get("id"))

        if content.post_type == "image":
            resp = graph.post(f"{page_id}/photos", token=token, data={
                "url": self._media_url(content.media[0]),
                "caption": caption,
            })
            return self._done(resp.get("post_id") or resp.get("id"))

        if content.post_type == "carousel":
            return self._done(
                self._publish_carousel(graph, page_id, token, content, caption))

        if content.post_type == "reel":
            video_id = self._start_reel(graph, page_id, token, content, caption)
            return PublishStep(status=StepStatus.PENDING.value,
                              provider_state={"video_id": video_id, "kind": "reel"})

        if content.post_type == "video":
            resp = graph.post(f"{page_id}/videos", token=token, data={
                "file_url": self._media_url(content.media[0]),
                "description": caption,
            })
            video_id = resp.get("id")
            if not video_id:
                raise PermanentError("Facebook did not return a video id.")
            return PublishStep(status=StepStatus.PENDING.value,
                              provider_state={"video_id": video_id, "kind": "video"})

        if content.post_type == "story":
            return self._publish_story(graph, page_id, token, content)

        raise PermanentError(
            f"Unsupported post type for Facebook: {content.post_type}")

    def _publish_story(self, graph, page_id, token, content):
        """A Facebook Page Story. Photo stories publish synchronously; video
        stories use the same 3-phase upload as Reels. A story carries no
        caption (the Graph story endpoints take none)."""
        media = content.media[0]
        if (media.mime_type or "").startswith("video"):
            video_id = self._start_video_story(graph, page_id, token, content)
            return PublishStep(status=StepStatus.PENDING.value,
                              provider_state={"video_id": video_id,
                                              "kind": "story"})
        # Photo story: upload the photo unpublished, then post it as a story.
        up = graph.post(f"{page_id}/photos", token=token, data={
            "url": self._media_url(media),
            "published": "false",
        })
        photo_id = up.get("id")
        if not photo_id:
            raise PermanentError("Facebook did not accept the story photo.")
        resp = graph.post(f"{page_id}/photo_stories", token=token,
                         data={"photo_id": photo_id})
        return self._done(resp.get("post_id") or resp.get("id"))

    def _start_video_story(self, graph, page_id, token, content):
        """3-phase video-story upload, mirroring _start_reel but with no
        caption. poll_publish then advances it exactly like a Reel/video."""
        start = graph.post(f"{page_id}/video_stories", token=token,
                          data={"upload_phase": "start"})
        video_id = start.get("video_id")
        upload_url = start.get("upload_url")
        if not (video_id and upload_url):
            raise PermanentError("Facebook did not start the story upload.")

        hosted_reel_upload(upload_url, token, self._media_url(content.media[0]))

        graph.post(f"{page_id}/video_stories", token=token, data={
            "upload_phase": "finish",
            "video_id": video_id,
        })
        return video_id

    def _done(self, post_id):
        if not post_id:
            raise PermanentError("Facebook did not return a post id.")
        return PublishStep(
            status=StepStatus.DONE.value,
            external_post_id=post_id,
            permalink=f"https://www.facebook.com/{post_id}",
        )

    def _start_reel(self, graph, page_id, token, content, caption):
        """Official 3-phase Reels flow: start -> hosted upload -> finish."""
        start = graph.post(f"{page_id}/video_reels", token=token,
                          data={"upload_phase": "start"})
        video_id = start.get("video_id")
        upload_url = start.get("upload_url")
        if not (video_id and upload_url):
            raise PermanentError("Facebook did not start the Reel upload.")

        hosted_reel_upload(upload_url, token, self._media_url(content.media[0]))

        graph.post(f"{page_id}/video_reels", token=token, data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
        })
        return video_id

    def poll_publish(self, target, provider_state, token):
        """Advance an async video/Reel: publish only once processing reaches
        `ready`."""
        graph = self.graph()
        video_id = provider_state["video_id"]

        info = graph.get(video_id, token=token, params={"fields": "status"})
        status = info.get("status") or {}
        video_status = status.get("video_status")

        if video_status == "ready":
            return PublishStep(
                status=StepStatus.DONE.value,
                external_post_id=video_id,
                permalink=self._video_permalink(graph, video_id, token),
            )
        if video_status in ("processing", "upload_complete", None):
            return PublishStep(status=StepStatus.PENDING.value,
                              provider_state=provider_state)
        # error / expired
        raise TransientError(f"Facebook video status: {video_status}")

    def _video_permalink(self, graph, video_id, token):
        try:
            resp = graph.get(video_id, token=token,
                            params={"fields": "permalink_url"})
            permalink = resp.get("permalink_url")
        except Exception:
            permalink = None
        if permalink and permalink.startswith("/"):
            permalink = "https://www.facebook.com" + permalink
        return permalink or f"https://www.facebook.com/{video_id}"

    def _publish_carousel(self, graph, page_id, token, content, caption):
        """Upload each photo unpublished, then attach them to one feed post."""
        media_fbids = []
        for media in content.media:
            up = graph.post(f"{page_id}/photos", token=token, data={
                "url": self._media_url(media),
                "published": "false",
            })
            media_fbids.append(up["id"])
        attached = {
            f"attached_media[{i}]": '{"media_fbid":"%s"}' % fbid
            for i, fbid in enumerate(media_fbids)
        }
        resp = graph.post(f"{page_id}/feed", token=token,
                         data={"message": caption, **attached})
        return resp.get("id")

    # -- Analytics ---------------------------------------------------------

    def fetch_analytics(self, target, token):
        if not target.external_post_id:
            return {}
        page_token = self._page_token(target)
        try:
            resp = self.graph().get(f"{target.external_post_id}/insights",
                                    token=page_token, params={
                "metric": "post_impressions,post_engaged_users,post_reactions_by_type_total",
            })
        except Exception:
            return {}
        metrics = {}
        for row in resp.get("data", []):
            values = row.get("values") or [{}]
            metrics[row.get("name")] = values[0].get("value")
        return metrics
