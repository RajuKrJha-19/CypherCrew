"""Instagram Business publisher (Graph API v25, Content Publishing API).

Instagram publishing is asynchronous: create a media container, poll its
status until FINISHED, then publish it. That maps cleanly onto the engine's
job state machine - start_publish returns PENDING with the container id in
provider_state, and poll_publish advances it (the worker re-drives poll on
each cycle). Supports image, carousel (2-10), reel and story.
"""

from app.social.dto import (
    AccountInfo, Capabilities, MediaSpec, PublishStep, StepStatus,
)
from app.social.errors import PermanentError, TransientError
from app.social.providers.meta_common import MetaBaseProvider


class MetaInstagramProvider(MetaBaseProvider):
    key = "instagram"
    # Instagram is DISCOVERED through the Facebook login (the unified Meta
    # consent), never connected on its own: an IG Business account is found
    # via its linked Page and published with that Page's token. So there is
    # no standalone Instagram OAuth entry point - see AccountManager /
    # social.discover_instagram for the "refresh linked Instagram" action.
    connectable = False
    # Instagram replies go to /{comment_id}/replies, not /comments.
    comment_reply_edge = "replies"
    SCOPES = [
        "instagram_basic",
        "instagram_content_publish",
        # Writing comments - the auto first comment, and replying from
        # Engage. Without it POST /{media-id}/comments is refused, and
        # because a first comment is best-effort the refusal was swallowed
        # into a log line: the feature simply did nothing, forever.
        "instagram_manage_comments",
        # Analytics reads /{media-id}/insights. fetch_analytics swallows
        # errors and returns {}, so without this the Analytics screen just
        # stays empty with nothing to explain why.
        "instagram_manage_insights",
        "pages_show_list",
        "pages_read_engagement",
        "business_management",
    ]
    capabilities = Capabilities(
        # No "video": Instagram publishes video to the feed as a REELS
        # container or not at all. media/fit.py maps an intended video
        # onto "reel" here, which is why a 9:16 clip publishes fine.
        post_types={"image", "carousel", "reel", "story"},
        media_specs={
            # Meta's published reel specification. The aspect range is
            # genuinely this wide - 9:16 is the recommendation, not the
            # requirement, unlike Facebook.
            "reel": MediaSpec(
                aspect_min=0.01, aspect_max=10.0,
                duration_min=3, duration_max=15 * 60,
                width_max=1920,
                max_bytes=300 * 1024 * 1024,
                fps_min=23, fps_max=60,
                codecs=("h264", "hevc"),
                aspect_label="between 0.01:1 and 10:1",
            ),
            "image": MediaSpec(
                aspect_min=0.8, aspect_max=1.91,      # 4:5 .. 1.91:1
                width_min=320, width_max=1440,
                max_bytes=8 * 1024 * 1024,
                aspect_label="between 4:5 and 1.91:1",
            ),
            # A story is video or image; Meta caps its width at 1920px too.
            # Without a spec here an oversized story was not caught locally
            # and failed at Meta with an opaque "container status: ERROR".
            # Kept deliberately loose (width + size only, wide aspect) so it
            # only ever catches the genuinely-oversized file - which a resize
            # then fixes - and never blocks a normal 1080x1920 story.
            "story": MediaSpec(
                aspect_min=0.01, aspect_max=10.0,
                width_max=1920,
                max_bytes=300 * 1024 * 1024,
                aspect_label="between 0.01:1 and 10:1",
            ),
        },
        requires_container_poll=True,
        max_carousel=10,
        publish_rate=(100, "24h"),
        story_support=True,
        # Meta exposes no sticker/link parameter on STORIES containers -
        # the post sticker and link sticker are app-only. A story asked to
        # link to a post publishes normally and leaves a follow-up.
        story_link_support=False,
        max_caption_chars=2200,
        supports_first_comment=True,
        supports_comments=True,
    )

    def _required_scopes(self):
        return ["instagram_content_publish", "instagram_basic"]

    # -- Discovery ---------------------------------------------------------

    def list_publishable_accounts(self, token):
        """IG Business accounts linked to the user's Pages, discovered in one
        pass off the Facebook user token during the unified Meta connect.
        Publishing uses the linked Page's token (stored per IG account)."""
        resp = self.graph().get("me/accounts", token=token, params={
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "limit": 100,
        })
        accounts = []
        for page in resp.get("data", []):
            iga = page.get("instagram_business_account")
            if not iga:
                continue
            accounts.append(self._ig_account(page["id"], iga,
                                              page.get("access_token"),
                                              page.get("name")))
        return accounts

    def discover_for_page(self, page_id, page_token, page_name=None):
        """Find the IG Business account linked to a SINGLE already-connected
        Page, using that Page's stored token. This is the "refresh linked
        Instagram" path - no OAuth, just a Graph read:
            GET /{page-id}?fields=instagram_business_account{id,username}
        Returns an AccountInfo or None (no IG linked)."""
        resp = self.graph().get(page_id, token=page_token, params={
            "fields": "instagram_business_account{id,username},name",
        })
        iga = resp.get("instagram_business_account")
        if not iga:
            return None
        return self._ig_account(page_id, iga, page_token,
                                resp.get("name") or page_name)

    @staticmethod
    def _ig_account(page_id, iga, page_token, page_name=None):
        return AccountInfo(
            external_id=iga["id"],
            display_name=iga.get("username") or page_name or iga["id"],
            account_type="ig_business",
            access_token=page_token,
            meta={"page_id": page_id, "ig_id": iga["id"]},
        )

    # -- Validation --------------------------------------------------------

    def validate(self, content):
        from app.social.media import pipeline
        problems = pipeline.validate_against(self.capabilities, content)
        if not content.media:
            problems.append(f"An Instagram {content.post_type} needs media.")
        if content.post_type == "carousel" and len(content.media) < 2:
            problems.append("An Instagram carousel needs at least 2 items.")
        return problems

    # -- Publishing (async: create container -> poll -> publish) ----------

    def start_publish(self, target, content, token):
        graph = self.graph()
        ig_id = target.account.external_id
        caption = self._full_caption(content)

        if content.post_type == "carousel":
            container_id = self._create_carousel(graph, ig_id, token, content, caption)
        elif content.post_type == "reel":
            container_id = graph.post(f"{ig_id}/media", token=token, data={
                "media_type": "REELS",
                "video_url": self._media_url(content.media[0]),
                "caption": caption,
                # Also appears in the profile feed grid, not only the Reels
                # tab. This is Meta's default, stated explicitly so a
                # change to that default cannot quietly move a client's
                # posts off their grid.
                "share_to_feed": "true",
            })["id"]
        elif content.post_type == "story":
            media = content.media[0]
            key = "video_url" if (media.mime_type or "").startswith("video") else "image_url"
            container_id = graph.post(f"{ig_id}/media", token=token, data={
                "media_type": "STORIES",
                key: self._media_url(media),
            })["id"]
        else:  # image
            container_id = graph.post(f"{ig_id}/media", token=token, data={
                "image_url": self._media_url(content.media[0]),
                "caption": caption,
            })["id"]

        return PublishStep(
            status=StepStatus.PENDING.value,
            provider_state={"container_id": container_id, "ig_id": ig_id},
        )

    def _create_carousel(self, graph, ig_id, token, content, caption):
        children = []
        for media in content.media:
            is_video = (media.mime_type or "").startswith("video")
            data = {"is_carousel_item": "true"}
            data["video_url" if is_video else "image_url"] = self._media_url(media)
            children.append(graph.post(f"{ig_id}/media", token=token, data=data)["id"])
        return graph.post(f"{ig_id}/media", token=token, data={
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
        })["id"]

    def poll_publish(self, target, provider_state, token):
        graph = self.graph()
        container_id = provider_state["container_id"]
        ig_id = provider_state["ig_id"]

        # Ask for `status` as well as `status_code`.
        #
        # status_code is one of FINISHED / IN_PROGRESS / ERROR / EXPIRED and
        # says nothing about WHY. `status` carries Meta's own sentence -
        # "moov atom not at front of file", "unsupported codec", the actual
        # reason. Requesting only the code is why every failure here read
        # "Instagram container status: ERROR" and left nobody any wiser.
        container = graph.get(
            container_id, token=token,
            params={"fields": "status_code,status"})
        status = container.get("status_code")

        if status == "FINISHED":
            published = graph.post(f"{ig_id}/media_publish", token=token,
                                  data={"creation_id": container_id})
            media_id = published["id"]
            return PublishStep(
                status=StepStatus.DONE.value,
                external_post_id=media_id,
                permalink=self._permalink(graph, media_id, token),
            )

        if status in ("IN_PROGRESS", None, "PUBLISHED"):
            # Still processing - stay PENDING so the worker polls again.
            return PublishStep(status=StepStatus.PENDING.value,
                              provider_state=provider_state)

        # ERROR or EXPIRED - a transient container error is worth one retry;
        # EXPIRED (24h) is permanent.
        detail = self._container_reason(container)
        message = f"Instagram container status: {status}"
        if detail:
            message = f"{message} - {detail}"

        if status == "ERROR":
            raise TransientError(message)
        raise PermanentError(message)

    @staticmethod
    def _container_reason(container):
        """Meta's own words for why a container failed, or None.

        `status` arrives as a prefixed blob - "Error: 2207026, The video
        format is not supported" - so the code and the prefix are stripped
        and what is left is the sentence a human can act on. Best-effort:
        an unexpected shape returns None rather than putting a raw dict in
        front of somebody trying to fix a video.
        """
        raw = (container.get("status") or "").strip()
        if not raw:
            return None

        _, _, tail = raw.partition(":")
        tail = (tail or raw).strip()

        # Drop a leading numeric error code, which means nothing to anyone
        # who is not reading Meta's error reference.
        head, _, rest = tail.partition(",")
        if head.strip().isdigit() and rest.strip():
            tail = rest.strip()

        return tail or None

    def _permalink(self, graph, media_id, token):
        try:
            resp = graph.get(media_id, token=token, params={"fields": "permalink"})
            return resp.get("permalink")
        except Exception:
            return None

    # -- Analytics ---------------------------------------------------------

    def fetch_analytics(self, target, token):
        if not target.external_post_id:
            return {}
        page_token = self._page_token(target)
        try:
            resp = self.graph().get(
                f"{target.external_post_id}/insights", token=page_token,
                params={"metric": "impressions,reach,likes,comments,saved"})
        except Exception:
            return {}
        metrics = {}
        for row in resp.get("data", []):
            values = row.get("values") or [{}]
            metrics[row.get("name")] = values[0].get("value")
        return metrics
