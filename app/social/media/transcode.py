"""Server-side video downscale (ffmpeg).

Platforms cap video width - Instagram at 1920px. A client deliverable is
often exported larger: a 2160px 9:16 reel is the right *shape*, just too many
pixels, and Meta rejects it outright rather than downscaling for us. So we
downscale it ourselves, on publish, preserving the aspect ratio.

ffmpeg is optional, exactly like ffprobe in probe.py. With no ffmpeg the app
behaves as before - the oversized target is blocked with a clear message and
the team re-exports. When ffmpeg IS present, an oversized-but-otherwise-fine
video is resized transparently and publishes with no human step.

The resized file is written to a fresh R2 object under social_uploads/derived
and used only for this one publish. Nothing in the database references it, so
the media GC reclaims it once Meta has pulled it - the original deliverable or
brand asset is never modified.
"""

import os
import shutil
import subprocess
import tempfile
from uuid import uuid4

from flask import current_app

from app.social.media import fit
from app.social.media.pipeline import presigned_url
from app.storage.storage_service import StorageService

_DERIVED_PREFIX = "social_uploads/derived/"
# A long reel re-encode - this runs in the worker, never in a web request.
_TIMEOUT_S = 15 * 60


#: Where ffmpeg lands on the common Linux installs, tried when it is not on
#: PATH - a hardened systemd unit often runs gunicorn with a minimal PATH that
#: omits /usr/bin, so `which ffmpeg` fails even though `apt install ffmpeg`
#: put it there.
_FALLBACK_PATHS = ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                   "/snap/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg")


def available():
    """Is ffmpeg usable on this host?"""
    return _resolve() is not None


def _resolve():
    """Absolute path to a usable ffmpeg, or None. Tries the configured
    binary / PATH first, then the usual install locations."""
    cand = _binary()
    found = shutil.which(cand)
    if found:
        return found
    if os.path.isabs(cand) and os.path.exists(cand):
        return cand
    for path in _FALLBACK_PATHS:
        if os.path.exists(path):
            return path
    return None


def _binary():
    try:
        return current_app.config.get("FFMPEG_PATH") or "ffmpeg"
    except RuntimeError:          # outside an app context
        return "ffmpeg"


def fit_content(content, capabilities):
    """Downscale, in place, any media in `content` too wide for its platform -
    aspect ratio preserved. No-op without ffmpeg, or when the only problems
    are ones a resize can't fix. Returns how many media were resized."""
    if capabilities is None or not available():
        return 0
    spec = capabilities.spec_for(content.post_type)
    if spec is None:
        return 0

    resized = 0
    for media in content.media:
        target_w = fit.downscale_target_width(spec, media.measurements or {})
        if target_w is None:
            continue
        derived = _downscale(media.object_key, target_w,
                             media.measurements or {})
        if derived:
            media.object_key, media.measurements = derived
            resized += 1
    return resized


def _downscale(object_key, max_width, source_meas):
    """Resize the R2 object to <= max_width wide (even height, aspect kept) and
    store it as a new derived object. Returns (derived_key, measurements), or
    None if anything failed - the caller then publishes the original and lets
    the platform surface the real error rather than silently dropping it."""
    try:
        src_url = presigned_url(object_key, expires_in=3600)
    except Exception:  # noqa: BLE001
        src_url = None
    if not src_url:
        return None

    tmp_dir = tempfile.mkdtemp(prefix="scale_")
    out_path = os.path.join(tmp_dir, "out.mp4")
    command = [
        _resolve() or _binary(), "-y",
        "-i", src_url,
        # min() never upscales; -2 keeps the height even, which h264 requires.
        "-vf", f"scale='min({max_width},iw)':-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=_TIMEOUT_S, check=False)
        if result.returncode != 0 or not os.path.exists(out_path) \
                or os.path.getsize(out_path) == 0:
            current_app.logger.warning(
                "[transcode] ffmpeg failed key=%s rc=%s", object_key,
                getattr(result, "returncode", "?"))
            return None

        derived_key = f"{_DERIVED_PREFIX}{uuid4().hex}.mp4"
        with open(out_path, "rb") as fh:
            StorageService().upload(
                file_obj=fh, object_key=derived_key, content_type="video/mp4")

        current_app.logger.info(
            "[transcode] %s -> %s (<=%spx)", object_key, derived_key, max_width)
        return derived_key, _scaled_measurements(source_meas, max_width,
                                                 out_path)
    except (subprocess.SubprocessError, OSError) as exc:
        current_app.logger.warning(
            "[transcode] error key=%s: %s", object_key, exc)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _scaled_measurements(source_meas, max_width, out_path):
    """The derived file's measurements, computed deterministically from the
    source rather than re-probed: scale='min(w,iw)':-2 makes width == max_width
    (the source was wider) and height the even-rounded proportional value.
    Duration and fps are unchanged by a resize; the codec is now h264."""
    meas = dict(source_meas or {})
    ow, oh = meas.get("width"), meas.get("height")
    if ow and oh and ow > max_width:
        scale = max_width / ow
        meas["width"] = max_width
        meas["height"] = max(2, (round(oh * scale) // 2) * 2)
    meas["codec"] = "h264"
    try:
        meas["bytes"] = os.path.getsize(out_path)
    except OSError:
        meas.pop("bytes", None)
    return meas
