"""Picking several files in the composer: selection, sequence, and removal.

Three reported problems that turned out to be two causes.

Uploading five images left only the LAST one ticked, because mediaChanged
unchecked every other item whenever the post type was not "carousel" - and the
default type is "image". With one item selected the slide strip never appeared
(it needs two), so "multi-select doesn't work" and "the sequence can't be set"
were the same bug seen from two angles.

Separately, tiles were appended when their upload FINISHED, so five files
landed in completion order - a small image beats a big one - and the sequence
was random even once more than one survived.

And there was no way to take a wrongly-picked file back out of the grid.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "app" / "templates" / "social" / "compose.html"
CSS = ROOT / "app" / "static" / "css" / "style.css"


def _source():
    return COMPOSE.read_text(encoding="utf-8", errors="ignore")


def _fn(source, signature):
    """The body of a JS function declared in the template."""
    return source.split(signature)[1].split("\n    }")[0]


# ======================================================================
# 1. A second image makes it a carousel instead of dropping the first
# ======================================================================

def test_the_picker_allows_more_than_one_file():
    assert 'id="uploadInput"' in _source()
    line = [ln for ln in _source().splitlines() if 'id="uploadInput"' in ln][0]
    assert "multiple" in line


def test_a_second_image_switches_to_carousel():
    body = _fn(_source(), "function mediaChanged(inp){")
    assert 'value="carousel"' in body, (
        "nothing looks for the carousel radio, so a second pick still just "
        "unchecks the first")
    assert "carousel.checked=true" in body
    # The switch has to notify the type handler, or applyType/preview never run.
    assert "dispatchEvent" in body


def test_the_others_are_only_unchecked_when_it_really_is_single_media():
    """A video, or a channel with no carousel option, still collapses to one."""
    body = _fn(_source(), "function mediaChanged(inp){")
    assert "isVideoInput" in body, "video is not distinguished from images"
    # The uncheck now lives in the else branch, not at the top of the function.
    assert "else" in body
    assert "others.forEach" in body


def test_the_sequence_strip_needs_two_items_which_is_why_this_mattered():
    """Pinning the link between the two symptoms: the strip is hidden below
    two items, so losing the earlier picks also lost the ordering UI."""
    body = _fn(_source(), "function renderSlideStrip(){")
    assert "items.length < 2" in body


# ======================================================================
# 2. Tiles keep the order they were picked in
# ======================================================================

def test_a_finished_upload_takes_its_placeholder_s_place():
    body = _fn(_source(), "function addUploaded(d, slot){")
    assert "replaceChild" in body, (
        "appending puts the grid in upload-completion order, so the slide "
        "sequence depends on which file happened to finish first")


def test_the_placeholder_is_passed_to_the_upload_handler():
    body = _fn(_source(), "function uploadFiles(files){")
    assert "addUploaded(d, ph)" in body


def test_a_failed_upload_names_the_file_it_failed_on():
    """With several in flight at once, "Upload failed." alone does not say
    which one to try again."""
    body = _fn(_source(), "function uploadFiles(files){")
    assert "file.name" in body


# ======================================================================
# 3. A wrongly-picked file can be removed
# ======================================================================

def test_the_remove_button_exists_on_a_freshly_uploaded_tile():
    body = _fn(_source(), "function addUploaded(d, slot){")
    assert "data-remove" in body


def test_the_remove_button_exists_on_a_server_rendered_tile():
    """Edit mode renders the already-uploaded media from Jinja, not JS - both
    paths need the button or it disappears when you reopen a draft."""
    source = _source()
    grid = source.split('id="uploadGrid"')[1].split("</div>")[0]
    assert "data-remove" in grid


def test_removing_does_not_toggle_the_tile():
    """The tile is a <label> around the checkbox, so a click anywhere in it
    selects/deselects. Without both guards the X would also tick something on
    its way out."""
    source = _source()
    handler = source.split('ev.target.closest("[data-remove]")')[1][:600]
    assert "preventDefault" in handler
    assert "stopPropagation" in handler


def test_removing_refreshes_the_sequence():
    """The numbers on the remaining tiles, and the slide strip, have to close
    the gap the removed item left."""
    source = _source()
    handler = source.split('ev.target.closest("[data-remove]")')[1][:600]
    assert "lab.remove()" in handler
    assert "refreshMediaSelection()" in handler


def test_removing_unticks_before_it_detaches():
    """syncMediaOrder reconciles against what is still CHECKED; dropping the
    node while it counts as selected would leave a dead key in the order."""
    source = _source()
    handler = source.split('ev.target.closest("[data-remove]")')[1][:600]
    assert "cb.checked = false" in handler


def test_the_remove_button_is_styled_and_does_not_collide():
    """Three corners are already occupied: .ord and the crop button top-left,
    the selected tick top-right."""
    css = CSS.read_text(encoding="utf-8", errors="ignore")
    assert ".cmp-asset-rm{" in css
    block = css.split(".cmp-asset-rm{")[1].split("}")[0]
    assert "bottom:6px" in block and "right:6px" in block
