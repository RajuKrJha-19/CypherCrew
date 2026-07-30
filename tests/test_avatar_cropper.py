"""The free-crop avatar dialog, and its cross-file assumptions.

avatar-cropper.js shows the whole photo fitted into the stage and lets a
free rectangle - any position, size and aspect ratio - be drawn on it. All
of its clamping is done in CSS pixels of the stage, using a hard-coded
`STAGE`, while the stage's real size lives in style.css. Nothing connects
the two, so changing the CSS alone silently lets the crop rectangle sit
outside the visible area.

That is the same shape of bug as the `.notification-dot` /
`.notification-dot-marker` drift: no error, no exception, just a UI that
is quietly wrong. Cheap to catch here.

The rest of these pin the behaviours that make the crop FREE, because
each was a deliberate reversal of an earlier square-locked version and
each is easy to undo without noticing.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SCRIPT = STATIC / "js" / "avatar-cropper.js"
STYLES = STATIC / "css" / "style.css"

#: Every handle the script renders. Four corners move two edges each,
#: four sides move one - that split IS the free crop.
CORNERS = ["nw", "ne", "sw", "se"]
SIDES = ["n", "e", "s", "w"]


def _script():
    return SCRIPT.read_text(encoding="utf-8")


def _css():
    return STYLES.read_text(encoding="utf-8")


def _rule(selector):
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", _css(), re.S)
    assert block, f"{selector} is missing from style.css"
    return block.group(1)


def _js_number(name):
    match = re.search(r"\bvar\s+" + name + r"\s*=\s*(\d+(?:\.\d+)?)\s*;", _script())
    assert match, f"{name} is no longer declared in avatar-cropper.js"
    return float(match.group(1))


def _css_stage_width():
    block = _rule(".avatar-crop-stage")

    width = re.search(r"(?<!-)\bwidth\s*:\s*(\d+(?:\.\d+)?)px", block)
    height = re.search(r"(?<!-)\bheight\s*:\s*(\d+(?:\.\d+)?)px", block)
    assert width and height, ".avatar-crop-stage no longer sets a pixel size"

    assert width.group(1) == height.group(1), (
        "the crop stage must stay square - the script fits the photo into "
        "it and clamps both axes against the same STAGE constant"
    )
    return float(width.group(1))


def test_the_script_and_the_stylesheet_agree_on_the_stage_size():
    assert _js_number("STAGE") == _css_stage_width(), (
        "STAGE in avatar-cropper.js and the .avatar-crop-stage width in "
        "style.css have drifted apart. Every clamp in the script is in "
        "these pixels, so the crop rectangle will not line up with the "
        "box the user can actually see."
    )


def test_the_working_copy_is_larger_than_anything_we_export():
    """The working canvas is downscaled to WORK_MAX to keep a 12 MP photo
    out of memory. If it ever drops below MAX_OUTPUT the export starts
    upscaling a shrunken copy and every avatar goes soft."""
    assert _js_number("WORK_MAX") >= _js_number("MAX_OUTPUT")


def test_the_smallest_crop_still_fits_in_the_stage():
    assert 0 < _js_number("MIN_CROP") < _js_number("STAGE")


# --------------------------------------------------------------------
# What makes the crop free. Each of these was square-locked before.
# --------------------------------------------------------------------

def test_the_export_keeps_the_crop_shape_instead_of_squaring_it():
    """The saved file must have the crop's own aspect ratio. The previous
    version wrote a square `out` to both canvas dimensions, which is
    exactly the behaviour being replaced - a single shared variable here
    would silently square every crop again."""
    source = _script()

    assert re.search(r"canvas\.width\s*=\s*outW", source), \
        "the export canvas no longer takes a separate width"
    assert re.search(r"canvas\.height\s*=\s*outH", source), \
        "the export canvas no longer takes a separate height"

    # And the two must be computed from the crop's own sides.
    assert "sourceW" in source and "sourceH" in source, (
        "the export no longer measures the crop's width and height "
        "separately, so it cannot preserve a non-square crop"
    )


def test_all_eight_handles_are_rendered_and_styled():
    source = _script()
    css = _css()

    for handle in CORNERS + SIDES:
        assert re.search(r"\b" + handle + r"\s*:\s*\[", source), (
            f"handle '{handle}' is no longer declared in HANDLES - with "
            f"fewer than eight the crop cannot be resized freely"
        )
        assert f".avatar-crop-handle.ac-{handle}" in css, (
            f"ac-{handle} is rendered by avatar-cropper.js but has no "
            f"rule, so that grip sits unstyled on the crop"
        )


@pytest.mark.parametrize("corner,edges", [
    ("nw", {"left", "top"}),
    ("ne", {"right", "top"}),
    ("sw", {"left", "bottom"}),
    ("se", {"right", "bottom"}),
])
def test_each_corner_moves_exactly_its_own_two_edges(corner, edges):
    match = re.search(corner + r"\s*:\s*\[([^\]]*)\]", _script())
    assert match, f"corner {corner} is missing from HANDLES"

    found = set(re.findall(r'"(\w+)"', match.group(1)))
    assert found == edges, (
        f"corner {corner} moves {found}, expected {edges} - a corner that "
        f"moves the wrong edges resizes the crop from the wrong anchor"
    )


@pytest.mark.parametrize("side,edge", [
    ("n", "top"), ("s", "bottom"), ("w", "left"), ("e", "right"),
])
def test_each_side_handle_moves_exactly_one_edge(side, edge):
    """A side handle that moves two edges would keep the aspect ratio
    locked, which is the whole thing this dialog stopped doing."""
    match = re.search(r"\b" + side + r"\s*:\s*\[([^\]]*)\]", _script())
    assert match, f"side handle {side} is missing from HANDLES"

    found = re.findall(r'"(\w+)"', match.group(1))
    assert found == [edge], (
        f"side handle {side} moves {found}, expected exactly ['{edge}'] - "
        f"moving more than one edge re-locks the crop's proportions"
    )


def test_the_crop_rectangle_takes_the_pointer_so_it_can_be_moved():
    """It used to be pointer-events:none, because the photo moved under a
    fixed frame. Now the rectangle is what moves, so it has to be
    grabbable - and the handles on top of it too."""
    assert re.search(r"pointer-events\s*:\s*auto", _rule(".avatar-crop-frame")), (
        ".avatar-crop-frame is not pointer-events:auto, so the crop "
        "rectangle can no longer be dragged"
    )
    assert re.search(r"pointer-events\s*:\s*auto", _rule(".avatar-crop-handle")), (
        ".avatar-crop-handle must take pointer events or the crop cannot "
        "be resized"
    )


def test_the_crop_frame_is_not_a_circle():
    """The circle belongs in the preview, beside the crop. Putting a
    border-radius back on the frame is what made the dialog feel like
    fitting a photo into a hole instead of choosing part of one."""
    assert not re.search(r"border-radius\s*:\s*50%", _rule(".avatar-crop-frame")), (
        "the crop rectangle has been made circular again - the crop is "
        "meant to be free, with the circle shown only in the preview"
    )


def test_the_circular_preview_exists_and_is_round():
    """It is the only thing telling anyone that a wide crop still gets
    centre-cropped by the circular avatar slots."""
    assert ".avatar-crop-preview" in _css()
    assert re.search(r"border-radius\s*:\s*50%",
                     _rule(".avatar-crop-preview canvas")), (
        "the avatar preview is no longer round, so it stops showing what "
        "the circular avatar slots will actually crop to"
    )
    assert "paintPreview" in _script(), \
        "the preview is no longer repainted as the crop changes"


def test_dragging_on_the_stage_still_needs_touch_action_none():
    """Without it the browser claims the gesture for scrolling and the
    pointer events never arrive, so no crop can be drawn on a phone."""
    assert re.search(r"touch-action\s*:\s*none", _rule(".avatar-crop-stage")), (
        ".avatar-crop-stage lost `touch-action:none`, so dragging out a "
        "crop will silently stop working on touch devices"
    )


def test_the_profile_page_still_loads_the_cropper():
    template = (
        Path(__file__).resolve().parent.parent
        / "app" / "templates" / "users" / "profile_edit.html"
    ).read_text(encoding="utf-8")

    assert "avatar-cropper.js" in template
    # The cropper hands the crop back by replacing the file input's files,
    # so it has to be the same input it bound to.
    assert 'id="avatarInput"' in template
