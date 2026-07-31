"""Facebook Page discovery: task filtering + cursor pagination.

The pagination path had no coverage, which is why an earlier version that
followed the absolute paging.next URL (mangled by the versioned client) slipped
through - the emulator returns no cursor. These pin the behaviour directly.
"""

from app.social.providers.meta_facebook import MetaFacebookProvider


class _FakeGraph:
    """Records calls and returns two cursor-paginated /me/accounts pages."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, token=None, params=None):
        self.calls.append((path, dict(params or {})))
        after = (params or {}).get("after")
        if after is None:
            return self.pages[0]
        return self.pages[1]


def _provider_with(monkeypatch, pages):
    fb = MetaFacebookProvider()
    graph = _FakeGraph(pages)
    monkeypatch.setattr(fb, "graph", lambda: graph)
    return fb, graph


def test_discovery_follows_cursor_pagination(monkeypatch):
    pages = [
        {"data": [{"id": "P1", "name": "Page 1", "tasks": ["CREATE_CONTENT"]}],
         "paging": {"next": "https://graph.facebook.com/v25.0/me/accounts?after=CUR",
                    "cursors": {"after": "CUR"}}},
        {"data": [{"id": "P2", "name": "Page 2", "tasks": ["MANAGE"]}],
         "paging": {}},
    ]
    fb, graph = _provider_with(monkeypatch, pages)

    accounts = fb.list_publishable_accounts("tok")

    assert sorted(a.external_id for a in accounts) == ["P1", "P2"]
    # Both calls hit the RELATIVE endpoint with a cursor param - never the
    # absolute paging.next URL (which the versioned client would mangle).
    assert [c[0] for c in graph.calls] == ["me/accounts", "me/accounts"]
    assert graph.calls[1][1].get("after") == "CUR"


def test_discovery_keeps_manage_and_skips_non_publishers(monkeypatch):
    pages = [
        {"data": [
            {"id": "A", "name": "Has CREATE_CONTENT", "tasks": ["ANALYZE", "CREATE_CONTENT"]},
            {"id": "B", "name": "Full control", "tasks": ["MANAGE"]},
            {"id": "C", "name": "Analyst only", "tasks": ["ANALYZE", "MODERATE"]},
            {"id": "D", "name": "No tasks field"},  # kept (lenient)
        ], "paging": {}},
        {},
    ]
    fb, _ = _provider_with(monkeypatch, pages)

    ids = sorted(a.external_id for a in fb.list_publishable_accounts("tok"))
    assert ids == ["A", "B", "D"]   # C (no publish task) is skipped


def test_discovery_skips_page_without_id(monkeypatch):
    pages = [{"data": [{"name": "No id here", "tasks": ["MANAGE"]},
                       {"id": "OK", "name": "Fine", "tasks": ["MANAGE"]}],
              "paging": {}}, {}]
    fb, _ = _provider_with(monkeypatch, pages)
    ids = [a.external_id for a in fb.list_publishable_accounts("tok")]
    assert ids == ["OK"]
