"""On-demand video poster frames.

A video has no image, so the composer preview, post media and drafts
thumbnails fell back to a generic file icon. Now that ffmpeg is available we
grab one frame (~1s in), scale it down, and cache it in R2 so every later view
is instant.

Lazy + cached: generated on first request to the /media/poster endpoint,
never during a page render, so a list of posts stays fast. If ffmpeg is
missing or a frame can't be read, it returns None and the UI keeps the generic
video icon - no worse than before.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile

from app.social.media import transcode
from app.social.media.pipeline import presigned_url
from app.storage.storage_service import StorageService

_PREFIX = "social_uploads/posters/"
_TIMEOUT_S = 60


def _poster_key(object_key):
    digest = hashlib.sha1((object_key or "").encode()).hexdigest()[:20]
    return f"{_PREFIX}{digest}.jpg"


def poster_url(object_key):
    """Presigned URL of a cached poster for a video object, generating it once
    with ffmpeg if needed. None if ffmpeg is unavailable or it fails."""
    if not object_key:
        return None
    derived = _poster_key(object_key)
    storage = StorageService()
    try:
        if storage.exists(object_key=derived):
            return presigned_url(derived)
    except Exception:  # noqa: BLE001
        pass
    if not transcode.available():
        return None
    if _generate(storage, object_key, derived):
        try:
            return presigned_url(derived)
        except Exception:  # noqa: BLE001
            return None
    return None


def _grab(ff, src, out, seek):
    """One ffmpeg frame-grab attempt. `seek` before -i is a fast, keyframe
    seek; used at ~1s, then 0s as a fallback for very short clips."""
    cmd = [ff, "-y"]
    if seek:
        cmd += ["-ss", str(seek)]
    cmd += ["-i", src, "-frames:v", "1",
            "-vf", "scale='min(640,iw)':-2", "-q:v", "4", out]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT_S,
                           check=False)
    except (subprocess.SubprocessError, OSError):
        return False
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def _generate(storage, object_key, derived):
    try:
        src = presigned_url(object_key)
    except Exception:  # noqa: BLE001
        src = None
    if not src:
        return False

    tmp = tempfile.mkdtemp(prefix="poster_")
    out = os.path.join(tmp, "poster.jpg")
    ff = transcode._resolve() or "ffmpeg"
    try:
        if not _grab(ff, src, out, seek=1) and not _grab(ff, src, out, seek=0):
            return False
        with open(out, "rb") as fh:
            storage.upload(file_obj=fh, object_key=derived,
                           content_type="image/jpeg")
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
