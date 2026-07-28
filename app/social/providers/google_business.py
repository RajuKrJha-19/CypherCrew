"""Google Business Profile publisher (local posts).

The awkward part of this API is that Google split it across hosts at
different versions, and did NOT move everything:

    accounts   -> mybusinessaccountmanagement.googleapis.com/v1
    locations  -> mybusinessbusinessinformation.googleapis.com/v1
    localPosts -> mybusiness.googleapis.com/v4      (still v4)

So discovery runs against the v1 APIs while publishing runs against v4,
and the two disagree about names: v1 returns `locations/{id}` while v4
wants `accounts/{accountId}/locations/{locationId}`. The account id is
therefore stored alongside the location at connect time - without it a
post cannot be addressed at all.

Access to the v4 endpoint is not automatic: Google requires a separate
application for the Business Profile APIs on top of the usual OAuth setup.
Until that is granted, calls come back 403 - which maps to AuthError and
surfaces as "reconnect this channel", so see docs/DEPLOY notes rather than
guessing at the cause.

Unlike YouTube, this API does fetch media from a URL, so publishing is a
single call with no upload dance.
"""

from app.social.dto import AccountInfo, Capabilities, PublishStep, StepStatus
from app.social.errors import PermanentError
from app.social.providers.base import SocialProvider
from app.social.providers.google_common import (
    GoogleBaseProvider, GoogleClient,
)

ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
POSTS_API = "https://mybusiness.googleapis.com/v4"

#: Fields the location list must ask for - v1 returns almost nothing by
#: default and silently omits anything not named here.
_LOCATION_FIELDS = "name,title,storefrontAddress"


class GoogleBusinessProvider(GoogleBaseProvider, SocialProvider):
    key = "google_business"
    connectable = True

    SCOPES = ["https://www.googleapis.com/auth/business.manage"]

    capabilities = Capabilities(
        post_types={"text", "image"},
        # A local post carries one image and 1500 characters, and cannot be
        # threaded or carouselled.
        max_caption_chars=1500,
        supports_delete=True,
    )

    # -- Discovery ---------------------------------------------------------

    def list_publishable_accounts(self, token):
        """Every location under every account the user administers.

        A location is the thing you post to, so each becomes one channel.
        """
        accounts_client = GoogleClient(ACCOUNTS_API)
        info_client = GoogleClient(INFO_API)

        found = []
        accounts = accounts_client.get(
            "accounts", token=token, params={"pageSize": 100}
        ).get("accounts", [])

        for account in accounts:
            # "accounts/12345"
            account_name = account.get("name")
            if not account_name:
                continue

            locations = info_client.get(
                f"{account_name}/locations", token=token,
                params={"readMask": _LOCATION_FIELDS, "pageSize": 100},
            ).get("locations", [])

            for location in locations:
                # v1 gives "locations/678"; v4 needs the account prefix.
                location_name = location.get("name") or ""
                location_id = location_name.split("/")[-1]
                if not location_id:
                    continue
                found.append(AccountInfo(
                    external_id=f"{account_name}/locations/{location_id}",
                    display_name=location.get("title") or location_id,
                    account_type="location",
                    meta={
                        "account_name": account_name,
                        "location_id": location_id,
                    },
                ))
        return found

    # -- Validation --------------------------------------------------------

    def validate(self, content):
        from app.social.media import pipeline
        problems = pipeline.validate_against(self.capabilities, content)
        if not (content.caption or "").strip():
            problems.append("A Google Business post needs some text.")
        if len(content.media or []) > 1:
            problems.append(
                "A Google Business post can carry only one image.")
        return problems

    # -- Publishing --------------------------------------------------------

    def start_publish(self, target, content, token):
        parent = target.account.external_id if target.account else None
        if not parent or "/locations/" not in parent:
            raise PermanentError(
                "This Google Business channel is missing its account/location "
                "path - disconnect and connect it again.")

        body = {
            "languageCode": "en",
            "summary": self._summary(content),
            "topicType": "STANDARD",
        }

        if content.media:
            from app.social.media import pipeline
            body["media"] = [{
                "mediaFormat": "PHOTO",
                "sourceUrl": pipeline.presigned_url(
                    content.media[0].object_key),
            }]

        resp = GoogleClient(POSTS_API).post(
            f"{parent}/localPosts", token=token, json=body)

        state = resp.get("state")
        name = resp.get("name")

        # PROCESSING means Google has it but has not published it yet -
        # stay PENDING and let the worker check back rather than reporting
        # a post that is not live.
        if state == "PROCESSING":
            return PublishStep(status=StepStatus.PENDING.value,
                               provider_state={"name": name})

        return self._done(resp)

    def poll_publish(self, target, provider_state, token):
        name = provider_state.get("name")
        if not name:
            raise PermanentError("Lost the Google Business post reference.")

        resp = GoogleClient(POSTS_API).get(name, token=token)
        state = resp.get("state")

        if state == "PROCESSING":
            return PublishStep(status=StepStatus.PENDING.value,
                               provider_state=provider_state)
        if state == "REJECTED":
            raise PermanentError(
                "Google rejected this post - it likely breaches the Business "
                "Profile content policy.")
        return self._done(resp)

    @staticmethod
    def _done(resp):
        name = resp.get("name")
        return PublishStep(
            status=StepStatus.DONE.value,
            external_post_id=name,
            permalink=resp.get("searchUrl"),
        )

    @staticmethod
    def _summary(content):
        caption = (content.caption or "").strip()
        hashtags = (content.hashtags or "").strip()
        text = (caption + ("\n\n" + hashtags if hashtags else "")).strip()
        return text[:1500]

    # -- Deletion ----------------------------------------------------------

    def delete_post(self, external_post_id, token):
        # Contract matches Meta's delete_post(external_post_id, token):
        # lifecycle.remove_target passes the id STRING, not the target object.
        GoogleClient(POSTS_API).delete(external_post_id, token=token)
        return True

    # -- Analytics ---------------------------------------------------------

    def fetch_analytics(self, target, token):
        """Local-post insights live behind a separate reporting endpoint
        with its own access grant, so nothing is reported rather than
        guessing at numbers."""
        return {}
