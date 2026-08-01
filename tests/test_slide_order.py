"""Carousel slide order: the sequence the composer showed is the one that goes.

Before this the order was an accident of the markup - every task file first
(in checkbox order), then brand assets, then uploads - and nothing could change
it. So a carousel could not be arranged, and the cover was whichever file the
picker happened to list first.

`media_order` carries the sequence, keyed by _measure_key, which is the same
string the browser already builds to match a measurement back to its item. One
vocabulary, both sides.
"""

import re
from pathlib import Path

import pytest

from app.routes.social import _measure_key

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "app" / "templates" / "social" / "compose.html"
SOCIAL = ROOT / "app" / "routes" / "social.py"
CSS = ROOT / "app" / "static" / "css" / "style.css"


class Obj:
    def __init__(self, id=None, object_key=None, form_value=None):
        self.id = id
        self.object_key = object_key
        if form_value is not None:
            self.form_value = form_value


def _sort(items, order):
    """The server's sort, lifted out so the ordering rule can be tested
    without a request. Mirrors _apply_composer_form."""
    if not order:
        return list(items)
    rank = {key: i for i, key in enumerate(order)}
    out = list(items)
    out.sort(key=lambda item: rank.get(_measure_key(*item), len(rank)))
    return out


# ----------------------------------------------------------------------
# The key both sides build
# ----------------------------------------------------------------------

def test_the_key_matches_what_the_browser_builds():
    """The composer keys by input.name + "|" + input.value. If these two ever
    disagree the order silently stops applying - every item falls to the
    "unknown" bucket and the old markup order comes back."""
    assert _measure_key("task_file", Obj(id=7)) == "task_file_ids|7"
    assert _measure_key("client_asset", Obj(id=3)) == "asset_ids|3"
    assert _measure_key(
        "upload", Obj(object_key="social_uploads/a.jpg",
                      form_value="social_uploads/a.jpg::image/jpeg")
    ) == "upload_media|social_uploads/a.jpg::image/jpeg"


# ----------------------------------------------------------------------
# The sort
# ----------------------------------------------------------------------

def test_the_requested_order_is_honoured():
    items = [("task_file", Obj(id=1)),
             ("task_file", Obj(id=2)),
             ("task_file", Obj(id=3))]

    ordered = _sort(items, ["task_file_ids|3", "task_file_ids|1",
                            "task_file_ids|2"])

    assert [o.id for _s, o in ordered] == [3, 1, 2]


def test_order_crosses_the_three_media_sources():
    """The real gain. Uploads used to be stuck behind every task file no
    matter what, so an uploaded cover was impossible."""
    items = [("task_file", Obj(id=1)),
             ("upload", Obj(object_key="social_uploads/x.jpg",
                            form_value="social_uploads/x.jpg::image/jpeg")),
             ("client_asset", Obj(id=9))]

    ordered = _sort(items, [
        "upload_media|social_uploads/x.jpg::image/jpeg",
        "asset_ids|9",
        "task_file_ids|1",
    ])

    assert [s for s, _o in ordered] == ["upload", "client_asset", "task_file"]


def test_the_first_item_is_the_cover():
    """Whatever leads the sequence is what publishes first - the carousel
    cover, and the frame the preview shows."""
    items = [("task_file", Obj(id=1)), ("task_file", Obj(id=2))]

    ordered = _sort(items, ["task_file_ids|2", "task_file_ids|1"])

    assert ordered[0][1].id == 2


def test_an_item_the_strip_never_saw_keeps_its_place_at_the_end():
    """A brand asset carried over from an existing post is not in the strip.
    It must not jump to the front of a sort it took no part in."""
    items = [("task_file", Obj(id=1)),
             ("client_asset", Obj(id=9)),          # not in the order
             ("task_file", Obj(id=2))]

    ordered = _sort(items, ["task_file_ids|2", "task_file_ids|1"])

    assert [_measure_key(s, o) for s, o in ordered] == [
        "task_file_ids|2", "task_file_ids|1", "asset_ids|9"]


def test_no_order_leaves_everything_exactly_as_it_was():
    """An older draft, or a browser where the strip never rendered, must
    behave the way it always did rather than shuffling."""
    items = [("task_file", Obj(id=5)), ("task_file", Obj(id=1)),
             ("upload", Obj(object_key="k", form_value="k::image/png"))]

    assert _sort(items, []) == items


def test_a_stale_key_in_the_order_is_ignored():
    """Untick a slide and submit before the strip re-renders: the dropped key
    must not drag anything with it or throw."""
    items = [("task_file", Obj(id=1)), ("task_file", Obj(id=2))]

    ordered = _sort(items, ["task_file_ids|99", "task_file_ids|2",
                            "task_file_ids|1"])

    assert [o.id for _s, o in ordered] == [2, 1]


def test_duplicate_keys_do_not_lose_an_item():
    items = [("task_file", Obj(id=1)), ("task_file", Obj(id=2))]

    ordered = _sort(items, ["task_file_ids|1", "task_file_ids|1",
                            "task_file_ids|2"])

    assert len(ordered) == 2


# ----------------------------------------------------------------------
# The route reads it, and the composer sends it
# ----------------------------------------------------------------------

def test_the_route_reads_media_order():
    source = SOCIAL.read_text(encoding="utf-8", errors="ignore")

    assert 'request.form.getlist("media_order")' in source, (
        "the server no longer reads the sequence - the strip would move slides "
        "on screen and change nothing that publishes"
    )
    assert "_measure_key(*item)" in source


def test_the_composer_sends_one_field_per_slide():
    """getlist needs repeated fields; a single delimited value would arrive as
    one opaque string and sort nothing."""
    source = COMPOSE.read_text(encoding="utf-8", errors="ignore")

    assert 'id="mediaOrderFields"' in source
    assert 'name="media_order"' in source
    assert "mediaOrder" in source


def test_the_strip_is_built_from_the_order_not_the_dom():
    """If the strip rendered from DOM order it would show one sequence and
    submit another."""
    source = COMPOSE.read_text(encoding="utf-8", errors="ignore")

    body = source.split("function renderSlideStrip()")[1].split("\n    }")[0]
    assert "selectedMediaInputs()" in body
    assert "slide-chip" in body
    assert "Cover" in body


def test_selection_reads_through_the_order():
    """Everything downstream - the cover, the preview, the badges, the
    measurement - goes through selectedMediaInputs()."""
    source = COMPOSE.read_text(encoding="utf-8", errors="ignore")

    body = source.split("function selectedMediaInputs()")[1].split("\n    }")[0]
    assert "mediaOrder" in body, (
        "selectedMediaInputs() ignores the sequence, so reordering would "
        "change the strip and nothing else"
    )


def test_the_strip_only_appears_with_something_to_order():
    source = COMPOSE.read_text(encoding="utf-8", errors="ignore")
    body = source.split("function renderSlideStrip()")[1].split("\n    }")[0]

    assert "items.length < 2" in body, (
        "one slide has no sequence to arrange, and an empty strip is a "
        "heading over nothing"
    )


@pytest.mark.parametrize("klass", [
    ".slide-strip", ".slide-chip", ".slide-num", ".slide-cover",
    ".slide-chip.is-dragging", ".slide-chip.is-over",
])
def test_the_strip_is_styled(klass):
    assert klass in CSS.read_text(encoding="utf-8", errors="ignore"), (
        "%s is applied but never styled" % klass
    )


def test_drag_prevents_default_on_dragover():
    """Without preventDefault on dragover the drop event never fires and the
    chips look draggable while doing nothing."""
    source = COMPOSE.read_text(encoding="utf-8", errors="ignore")
    body = source.split("function bindSlideDrag()")[1].split("\n    })();")[0]

    assert "dragover" in body and "preventDefault" in body
    assert "setData" in body, (
        "Firefox will not start a drag without dataTransfer payload"
    )
