"""Facebook Pages publisher (Graph API v25).

Discovers the Pages a user manages, publishes text / photo / video / reel /
carousel to a Page, and reads Page-post insights. Scheduling is engine-owned
(the worker calls publish at the due time), so this always publishes
immediately - native scheduled_publish_time is intentionally not used.
"""

from app.social.dto import AccountInfo, Capabilities, PublishStep, StepStatus
from app.social.errors import PermanentError, TransientError
from app.social.providers.meta_common import MetaBaseProvider, hosted_reel_upload


class MetaFacebookProvider(MetaBaseProvider):
    key = "facebook"
    SCOPES = [
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
        "business_management",
    ]
    capabilities = Capabilities(
        post_types={"text", "image", "video", "reel", "carousel"},
        supports_native_scheduling=True,   # available, but engine owns timing
        max_carousel=10,
        max_caption_chars=63206,
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

    def list_publishable_accounts(self, token):
        """Pages the user manages, each with its own Page token. Only Pages
        that grant the CREATE_CONTENT task can publish."""
        resp = self.graph().get("me/accounts", token=token, params={
            "fields": "id,name,access_token,tasks",
            "limit": 100,
        })
        accounts = []
        for page in resp.get("data", []):
            tasks = page.get("tasks") or []
            if tasks and "CREATE_CONTENT" not in tasks:
                continue
            accounts.append(AccountInfo(
                external_id=page["id"],
                display_name=page.get("name", page["id"]),
                account_type="page",
                access_token=page.get("access_token"),
                meta={"page_id": page["id"]},
            ))
        return accounts

    # -- Validation --------------------------------------------------------

    def validate(self, content):
        from app.social.media import pipeline
        problems = pipeline.validate_against(self.capabilities, content)
        if content.post_type in ("image", "video", "reel") and not content.media:
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

        raise PermanentError(
            f"Unsupported post type for Facebook: {content.post_type}")

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
