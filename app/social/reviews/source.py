"""Where reviews come from and where replies go.

Two sources behind one tiny interface:
  - SimulatedReviewsSource: scripted reviews + in-effect reply acceptance, so
    the inbox + auto-reply flow works with no API. Selected in simulation mode.
  - GoogleReviewsClient: the real Business Profile Reviews API (list + reply).
    Best-effort - VERIFY against the live API when you enable access; it is
    only used once GBP_REVIEWS_SIMULATION_MODE is turned off.

A review is a plain dict: {external_id, reviewer_name, rating, comment,
created_at}.
"""
from datetime import datetime

from flask import current_app


def get_source():
    if current_app.config.get("GBP_REVIEWS_SIMULATION_MODE", True):
        return SimulatedReviewsSource()
    return GoogleReviewsClient()


class SimulatedReviewsSource:
    """Deterministic scripted reviews per account (a positive no-text, a
    positive with text, and a critical one), so a sync is repeatable and the
    guardrails are exercisable."""

    key = "simulation"

    def list_reviews(self, account):
        base = datetime(2026, 7, 1, 9, 0, 0)
        aid = getattr(account, "id", 0)
        return [
            {"external_id": f"sim-{aid}-1", "reviewer_name": "Aditi Sharma",
             "rating": 5, "comment": "", "created_at": base},
            {"external_id": f"sim-{aid}-2", "reviewer_name": "Rahul Verma",
             "rating": 5, "comment": "Fantastic service, the team was so helpful!",
             "created_at": base},
            {"external_id": f"sim-{aid}-3", "reviewer_name": "Unhappy Customer",
             "rating": 2, "comment": "Terrible experience, I want a refund.",
             "created_at": base},
        ]

    def post_reply(self, account, external_id, text):
        # Accepted into the simulator; nothing leaves the app.
        return True


class GoogleReviewsClient:
    """Real Business Profile Reviews API. Best-effort; verify on live access."""

    key = "google"

    # Business Profile API host. The review resource name already carries the
    # account/location path, so list uses the location and reply uses the name.
    _BASE = "https://mybusiness.googleapis.com/v4"

    def _token(self, account):
        from app.social.services.accounts import AccountManager
        return AccountManager.access_token(account)

    def _location_path(self, account):
        # The GBP location resource, e.g. "accounts/123/locations/456". Stored
        # on the connected account; adjust to wherever your connect flow keeps
        # it if this differs once you wire real accounts.
        meta = getattr(account, "meta", None) or {}
        return meta.get("location_path") or getattr(account, "external_id", "")

    def list_reviews(self, account):
        import requests
        token = self._token(account)
        loc = self._location_path(account)
        if not loc:
            return []
        url = f"{self._BASE}/{loc}/reviews"
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"},
            timeout=30)
        resp.raise_for_status()
        out = []
        for r in (resp.json().get("reviews") or []):
            stars = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
            out.append({
                "external_id": r.get("reviewId") or r.get("name"),
                "reviewer_name": (r.get("reviewer") or {}).get("displayName"),
                "rating": stars.get(r.get("starRating")),
                "comment": r.get("comment") or "",
                "created_at": _parse_time(r.get("createTime")),
            })
        return out

    def post_reply(self, account, external_id, text):
        import requests
        token = self._token(account)
        loc = self._location_path(account)
        # external_id is the review id; the reply endpoint is PUT .../reply.
        name = external_id if str(external_id).startswith("accounts/") \
            else f"{loc}/reviews/{external_id}"
        resp = requests.put(
            f"{self._BASE}/{name}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={"comment": text}, timeout=30)
        resp.raise_for_status()
        return True


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
            tzinfo=None)
    except (ValueError, AttributeError):
        return None
