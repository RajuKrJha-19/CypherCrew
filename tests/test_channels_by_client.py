"""Channels are grouped by the CLIENT they serve.

The Channels page answers one question - "what can the Studio publish for
X?" - because every other Studio screen is already scoped to a client. A
flat grid made the reader do that grouping in their head, which is the one
thing they came here not to do.

These pin the grouping itself, not the markup: which client a channel
lands under, what happens to an unassigned one, and the two cases that
would silently drop a live publishing target off the page.
"""

import pytest

from app.routes.social import _channels_by_client, _grouped_accounts


class FakeClient:
    def __init__(self, id, name, code=None):
        self.id = id
        self.client_name = name
        self.code = code or name[:3].upper()


class FakeAccount:
    def __init__(self, id, platform, name, client_id=None, page_id=None,
                 status="active"):
        self.id = id
        self.platform = platform
        self.display_name = name
        self.client_id = client_id
        self.status = status
        self.meta = {"page_id": page_id} if page_id else {}
        self.external_id = "ext-%s" % id


ACME = FakeClient(1, "Acme Health")
BETA = FakeClient(2, "Beta Schools")


def _sections(accounts, clients=(ACME, BETA)):
    return _channels_by_client(_grouped_accounts(accounts), list(clients))


def test_a_clients_channels_land_in_one_section():
    """The whole point: a Page, its Instagram and a YouTube channel that all
    serve one client appear together, not scattered by platform."""
    accounts = [
        FakeAccount(1, "facebook", "Acme Page", client_id=1, page_id="p1"),
        FakeAccount(2, "instagram", "acme_ig", client_id=1, page_id="p1"),
        FakeAccount(3, "youtube", "Acme Channel", client_id=1),
    ]

    sections = _sections(accounts)

    assert len(sections) == 1
    assert sections[0]["client"] is ACME
    assert len(sections[0]["accounts"]) == 3, (
        "the nested Instagram has to count as one of the client's channels"
    )


def test_two_clients_do_not_bleed_into_each_other():
    accounts = [
        FakeAccount(1, "facebook", "Acme Page", client_id=1, page_id="p1"),
        FakeAccount(2, "youtube", "Beta Channel", client_id=2),
    ]

    sections = _sections(accounts)

    assert [s["client"].client_name for s in sections] == [
        "Acme Health", "Beta Schools"]
    assert len(sections[0]["accounts"]) == 1
    assert len(sections[1]["accounts"]) == 1


def test_sections_follow_the_client_switchers_order():
    """Both read from _studio_clients(), which puts a parent before its own
    sub-clients. If this page re-sorted, the two would disagree about where
    a client sits."""
    accounts = [
        FakeAccount(1, "facebook", "Beta Page", client_id=2, page_id="p1"),
        FakeAccount(2, "facebook", "Acme Page", client_id=1, page_id="p2"),
    ]

    sections = _sections(accounts, clients=(BETA, ACME))

    assert [s["client"] for s in sections] == [BETA, ACME]


def test_a_client_with_no_channels_gets_no_section():
    """An empty heading for every client on the books would bury the ones
    that actually publish."""
    accounts = [FakeAccount(1, "facebook", "Acme Page", client_id=1)]

    sections = _sections(accounts)

    assert len(sections) == 1
    assert sections[0]["client"] is ACME


def test_unassigned_channels_go_last():
    """The unassigned bucket is a pile that still needs a decision, not a
    client - so it sorts after the real ones rather than alphabetically
    among them."""
    accounts = [
        FakeAccount(1, "linkedin", "Agency LinkedIn", client_id=None),
        FakeAccount(2, "facebook", "Acme Page", client_id=1),
    ]

    sections = _sections(accounts)

    assert sections[-1]["client"] is None
    assert sections[-1]["accounts"][0].display_name == "Agency LinkedIn"


def test_a_channel_bound_to_an_inactive_client_still_appears():
    """_studio_clients() lists ACTIVE clients only. A channel bound to a
    deactivated one would otherwise vanish from this page while remaining a
    live publishing target - the worst outcome available."""
    accounts = [
        FakeAccount(1, "facebook", "Ghost Page", client_id=999),
    ]

    sections = _sections(accounts)

    assert len(sections) == 1, "the channel was dropped off the page"
    assert sections[0]["client"] is None
    assert sections[0]["accounts"][0].display_name == "Ghost Page"


def test_platform_pips_are_distinct_and_in_catalog_order():
    """The header pips answer "can I reach this client on Instagram?" at a
    glance, so a client with three Pages must not show three Facebook pips."""
    accounts = [
        FakeAccount(1, "youtube", "Acme TV", client_id=1),
        FakeAccount(2, "facebook", "Acme Page", client_id=1, page_id="p1"),
        FakeAccount(3, "instagram", "acme_ig", client_id=1, page_id="p1"),
        FakeAccount(4, "facebook", "Acme Page 2", client_id=1, page_id="p2"),
    ]

    sections = _sections(accounts)

    assert sections[0]["platforms"] == ["facebook", "instagram", "youtube"], (
        "pips must be de-duplicated and follow the platform catalog's order"
    )


def test_channels_needing_attention_are_counted():
    """A revoked or expired channel inside a client's section is the reason
    to open it, so the heading says how many."""
    accounts = [
        FakeAccount(1, "facebook", "Acme Page", client_id=1, page_id="p1"),
        FakeAccount(2, "instagram", "acme_ig", client_id=1, page_id="p1",
                    status="needs_reauth"),
        FakeAccount(3, "youtube", "Acme TV", client_id=1),
    ]

    sections = _sections(accounts)

    assert sections[0]["needs_attention"] == 1, (
        "a nested Instagram needing reauth has to be counted too"
    )


def test_no_channels_at_all_is_no_sections():
    assert _sections([]) == []
