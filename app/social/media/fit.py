"""What does this file become on this platform?

One post, one approved file, several platforms - and the platforms do not
agree on what to call it. Instagram has no "video" post type at all: a
video goes to the feed as a Reel or not at all. Facebook has both, but its
Reels are far stricter than Instagram's (9:16 exactly, 90 seconds).

So the rule is **reel first, video as the fallback**:

    1. the platform takes reels AND the file fits its reel spec -> reel
    2. the platform takes video                                 -> video
    3. neither                                                  -> blocked,
       with the number that has to change

This is why a 720x1280 clip used to publish on Facebook and get refused by
Instagram: the composer sent post_type="video" to every target, Instagram's
post_types has no "video", and our own pre-flight rejected it before Meta
ever saw it. The file was always fine.

Deciding it here, once, keeps the answer identical in three places that
would otherwise drift: the composer preview, target creation, and the
pre-flight run at schedule time.
"""


def _ratio(meta):
    width, height = meta.get("width"), meta.get("height")
    if not width or not height:
        return None
    return width / height


def _fmt_seconds(value):
    """3 -> "3s", 90 -> "90s", 900 -> "15 min"."""
    if value is None:
        return "?"
    if value >= 120:
        minutes = value / 60
        return f"{minutes:g} min"
    return f"{value:g}s"


def check_spec(spec, meta):
    """Reasons `meta` does not meet `spec`. Empty list = it fits.

    An unknown measurement is never a failure - it is checked by the
    platform instead, and its real error is surfaced. Guessing here would
    block files that are perfectly fine.
    """
    if spec is None or not meta:
        return []

    problems = []

    duration = meta.get("duration")
    if duration is not None:
        if spec.duration_min is not None and duration < spec.duration_min:
            problems.append(
                f"it is {duration:.0f}s and the minimum is "
                f"{_fmt_seconds(spec.duration_min)}")
        if spec.duration_max is not None and duration > spec.duration_max:
            problems.append(
                f"it is {duration:.0f}s and the maximum is "
                f"{_fmt_seconds(spec.duration_max)}")

    ratio = _ratio(meta)
    if ratio is not None:
        # Tolerance: a 720x1281 export is 9:16 to every human and to the
        # platform, and should not be argued with over a rounding error.
        tol = 0.02
        if spec.aspect_min is not None and ratio < spec.aspect_min - tol:
            problems.append(_aspect_problem(spec, meta))
        elif spec.aspect_max is not None and ratio > spec.aspect_max + tol:
            problems.append(_aspect_problem(spec, meta))

    width, height = meta.get("width"), meta.get("height")
    if width:
        if spec.width_min is not None and width < spec.width_min:
            problems.append(
                f"it is {width}px wide and the minimum is {spec.width_min}px")
        if spec.width_max is not None and width > spec.width_max:
            problems.append(
                f"it is {width}px wide and the maximum is {spec.width_max}px")
    if height and spec.height_min is not None and height < spec.height_min:
        problems.append(
            f"it is {height}px tall and the minimum is {spec.height_min}px")

    size = meta.get("bytes")
    if size and spec.max_bytes and size > spec.max_bytes:
        problems.append(
            f"it is {size / 1_048_576:.0f}MB and the maximum is "
            f"{spec.max_bytes / 1_048_576:.0f}MB")

    # Only present when ffprobe ran - a browser cannot report either.
    fps = meta.get("fps")
    if fps:
        if spec.fps_min is not None and fps < spec.fps_min:
            problems.append(
                f"it is {fps:g} fps and the minimum is {spec.fps_min:g}")
        if spec.fps_max is not None and fps > spec.fps_max:
            problems.append(
                f"it is {fps:g} fps and the maximum is {spec.fps_max:g}")

    codec = (meta.get("codec") or "").lower()
    if codec and spec.codecs and codec not in spec.codecs:
        problems.append(
            f"it is {codec} and this needs {' or '.join(spec.codecs)}")

    return problems


def downscale_target_width(spec, meta):
    """The width to shrink `meta` down to so it meets `spec`, or None.

    The transcode is TRIGGERED by an over-max width, and it RE-ENCODES the
    video (to h264, at a controlled quality) rather than merely scaling - so it
    fixes width AND the size AND the codec in one pass. This returns
    spec.width_max when, after modelling that re-encode, the file fully passes.

    It still returns None when the problem is something a re-encode can't fix -
    a wrong aspect ratio, too long a duration, too high an fps, or a shrink
    that would breach a minimum height/width - so a resize is never used to
    paper over a real problem.
    """
    if spec is None or not meta:
        return None
    width, wmax = meta.get("width"), spec.width_max
    if not (width and wmax and width > wmax):
        return None

    scale = wmax / width
    scaled = dict(meta)
    scaled["width"] = wmax
    if meta.get("height"):
        # -2 in the ffmpeg filter keeps the height even; mirror that here so
        # the simulated check matches what the transcode will actually make.
        scaled["height"] = max(2, (round(meta["height"] * scale) // 2) * 2)
    # The re-encode controls the output bitrate and codec, so the result meets
    # the size + codec limits regardless of the source. Model that rather than
    # scaling the source bytes by area - a 447MB clip re-encoded at 1920px is
    # comfortably under a 300MB cap, and estimating bytes by area (~353MB)
    # wrongly rejected exactly that fixable file.
    scaled.pop("bytes", None)
    scaled["codec"] = "h264"

    # What a re-encode does NOT change - aspect, duration, fps - is still
    # checked: if any of those is the problem, the scaled file fails here and
    # we return None.
    if check_spec(spec, scaled):
        return None
    return wmax


def _aspect_problem(spec, meta):
    width, height = meta.get("width"), meta.get("height")
    actual = f"{width}x{height}" if width and height else "this shape"
    wanted = spec.aspect_label or (
        f"between {spec.aspect_min:g}:1 and {spec.aspect_max:g}:1"
        if spec.aspect_min and spec.aspect_max else "a different shape")
    return f"it is {actual} and this needs {wanted}"


def choose_post_type(intended, capabilities, meta=None):
    """(post_type, notes) for one platform.

    `post_type` is None when the platform cannot take this content at all;
    `notes` then explains what would have to change. When a reel is
    downgraded to a video, `notes` says why - the person should know their
    Instagram Reel is going out as a plain Facebook video.
    """
    if capabilities is None:
        return intended, []

    meta = meta or {}

    # Only video-ish content gets remapped. An image is an image.
    if intended not in ("video", "reel"):
        if capabilities.supports(intended):
            return intended, []
        return None, [f"{intended} is not supported here."]

    reel_problems = None
    if capabilities.supports("reel"):
        reel_problems = check_spec(capabilities.spec_for("reel"), meta)
        if not reel_problems:
            return "reel", []

    if capabilities.supports("video"):
        video_problems = check_spec(capabilities.spec_for("video"), meta)
        if not video_problems:
            notes = []
            if reel_problems:
                notes.append(
                    "Going out as a video, not a reel, because "
                    + reel_problems[0] + ".")
            return "video", notes
        return None, video_problems

    if reel_problems:
        return None, reel_problems

    return None, [f"{intended} is not supported here."]


def describe(intended, capabilities, meta=None):
    """One line for the composer: what this becomes, and any caveat."""
    post_type, notes = choose_post_type(intended, capabilities, meta)
    return {
        "post_type": post_type,
        "label": {"reel": "Reel", "video": "Video", "image": "Image",
                  "carousel": "Carousel", "story": "Story",
                  "text": "Text"}.get(post_type, post_type or "Can't publish"),
        "notes": notes,
        "ok": post_type is not None,
    }
