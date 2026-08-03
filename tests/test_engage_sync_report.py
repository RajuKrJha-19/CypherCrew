"""Engage comment-sync failure reporting: unreadable posts are grouped into a
compact, plain-language summary (never a raw Graph dump with object ids), and
classified so an actionable auth problem reads differently from a post that is
simply gone from the platform.
"""
from app.social.errors import (
    AuthError, PermanentError, RateLimitError, TransientError,
)
from app.social.services import engage


class _FakeProvider:
    """Stands in for a real adapter: map_error just returns the typed error we
    want _classify to see (exactly what a provider's map_error would yield)."""
    def __init__(self, mapped):
        self._mapped = mapped

    def map_error(self, exc):
        return self._mapped


# -- _classify: raw Graph error -> (category, plain reason) ------------------

def test_graph_object_gone_is_unavailable_not_a_raw_dump():
    # The exact error from the screenshot: code 100 "Object with ID ... does
    # not exist, cannot be loaded due to missing permissions..." (subcode 33).
    raw = ("Unsupported get request. Object with ID "
           "'1213801861819921_122099583681407324' does not exist, cannot be "
           "loaded due to missing permissions, or does not support this "
           "operation. Please read the Graph API documentation.")
    prov = _FakeProvider(PermanentError(raw, code=100))
    category, reason = engage._classify(prov, Exception())
    assert category == "unavailable"
    # No raw Graph text, no object id leaked into the user-facing reason.
    assert "Object with ID" not in reason
    assert "1213801861819921" not in reason
    assert "no longer available" in reason


def test_gone_detected_by_message_when_code_absent():
    prov = _FakeProvider(PermanentError(
        "Unsupported get request. Object does not exist."))
    category, _ = engage._classify(prov, Exception())
    assert category == "unavailable"


def test_auth_error_is_actionable_reconnect():
    prov = _FakeProvider(AuthError("token expired", code=190))
    category, reason = engage._classify(prov, Exception())
    assert category == "auth"
    assert "reconnect" in reason.lower()


def test_rate_limit_maps_to_its_own_bucket():
    prov = _FakeProvider(RateLimitError("slow down", code=4))
    category, _ = engage._classify(prov, Exception())
    assert category == "rate_limit"


def test_other_permanent_error_is_refused_generic():
    prov = _FakeProvider(TransientError("500 boom"))
    category, reason = engage._classify(prov, Exception())
    assert category == "refused"
    assert "refused" in reason.lower()


# -- failure_summary: compact, grouped, ordered -----------------------------

def test_failure_summary_groups_and_orders_by_actionability():
    report = {"by_reason": {"unavailable": 5, "auth": 1}}
    summary = engage.failure_summary(report)
    # Auth (actionable) listed before the merely-gone posts; reads correctly
    # at count 1 ("1 on a channel...", not "1 need...").
    assert summary == ("1 on a channel that needs reconnecting, "
                       "5 no longer available on the platform")


def test_failure_summary_empty_when_nothing_failed():
    assert engage.failure_summary({"by_reason": {}}) == ""
    assert engage.failure_summary({}) == ""


def test_failure_summary_never_contains_raw_ids_or_graph_text():
    # Even with a pile of gone posts, the summary is one short clause.
    report = {"by_reason": {"unavailable": 6}}
    summary = engage.failure_summary(report)
    assert summary == "6 no longer available on the platform"
    assert "Object with ID" not in summary
