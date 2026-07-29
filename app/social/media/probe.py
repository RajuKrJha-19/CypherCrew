"""Server-side media measurement with ffprobe.

The composer measures files in the browser, which is instant and covers
most posts. It has two holes that matter for an agency:

  * **Formats the browser cannot decode.** Editors deliver .mov and HEVC.
    Chrome often cannot read those, so the measurement silently comes back
    empty and the file goes out unchecked - the exact failure this whole
    area exists to prevent.
  * **Frame rate and codec.** A <video> element cannot report either, and
    Facebook Reels require 24-60 fps and H.264/H.265.

So ffprobe is the backstop, run once per media item before anything is
scheduled, and cached on SocialMediaAsset.meta.

Three rules it never breaks:

  * **It never raises.** A missing binary, a slow read, a weird container -
    all return {} and the file is treated as unmeasured, which downstream
    already handles by letting the platform judge. A probe failing must
    never stop a post going out.
  * **It never downloads the whole file.** ffprobe reads over HTTP with
    range requests and is capped by -probesize/-analyzeduration and a hard
    timeout. This is metadata, not a frame decode - which is why the
    reasoning in app/services/thumbnails.py against ffmpeg does not apply.
  * **It is optional.** With no ffprobe installed the app behaves exactly
    as it did before, so local development and any host without it are
    unaffected.
"""

import json
import shutil
import subprocess

from flask import current_app

#: Enough header to find the moov atom in a well-formed file. Instagram
#: requires moov-at-front anyway, so a file that needs more than this to
#: identify itself is one the platform would reject regardless.
_PROBE_SIZE = 8 * 1024 * 1024
_ANALYZE_US = 10 * 1_000_000        # microseconds
_TIMEOUT_S = 25


_FALLBACK_PATHS = ("/usr/bin/ffprobe", "/usr/local/bin/ffprobe",
                   "/snap/bin/ffprobe", "/opt/homebrew/bin/ffprobe")


def available():
    """Is ffprobe usable on this host?"""
    return _resolve() is not None


def _resolve():
    """Absolute path to a usable ffprobe, or None. Tries the configured
    binary / PATH first, then the usual install locations (a hardened systemd
    unit can run with a minimal PATH that omits /usr/bin)."""
    import os
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
        return current_app.config.get("FFPROBE_PATH") or "ffprobe"
    except RuntimeError:          # outside an app context
        return "ffprobe"


def probe_url(url):
    """{width, height, duration, fps, codec} for a media URL, or {}.

    Empty means "could not measure", never "bad" - see the module note.
    """
    if not url or not available():
        return {}

    command = [
        _resolve() or _binary(), "-v", "error",
        "-probesize", str(_PROBE_SIZE),
        "-analyzeduration", str(_ANALYZE_US),
        "-show_streams", "-show_format",
        "-print_format", "json",
        url,
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, timeout=_TIMEOUT_S, check=False)
        if result.returncode != 0:
            return {}
        payload = json.loads(result.stdout or b"{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        # Includes TimeoutExpired and a missing binary racing `available()`.
        return {}

    return _summarise(payload)


def _summarise(payload):
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    fmt = payload.get("format") or {}

    out = {}
    if video:
        width, height = video.get("width"), video.get("height")
        if width and height:
            out["width"], out["height"] = int(width), int(height)
        codec = video.get("codec_name")
        if codec:
            out["codec"] = codec
        fps = _fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        if fps:
            out["fps"] = fps

    duration = fmt.get("duration") or (video or {}).get("duration")
    if duration:
        try:
            out["duration"] = float(duration)
        except (TypeError, ValueError):
            pass

    size = fmt.get("size")
    if size:
        try:
            out["bytes"] = int(size)
        except (TypeError, ValueError):
            pass

    return out


def _fps(rate):
    """ffprobe reports "30000/1001" - turn it into 29.97."""
    if not rate or "/" not in str(rate):
        return None
    numerator, _, denominator = str(rate).partition("/")
    try:
        num, den = float(numerator), float(denominator)
    except ValueError:
        return None
    if den == 0:
        return None
    return round(num / den, 3)


def ensure_measured(target):
    """Fill in anything the browser could not measure, once, and cache it.

    Called before a target is validated, so the pre-flight has real numbers
    for every file - including the .mov the composer could not read. Values
    the browser DID capture are kept: they came from the actual file the
    person selected, and re-probing them buys nothing.
    """
    if not available():
        return

    from app.extensions import db
    from app.social.media import pipeline

    changed = False
    for asset in target.media:
        meta = dict(asset.meta or {})
        measurements = dict(meta.get("measurements") or {})

        # Already has everything a spec can check? Leave it alone.
        if measurements.get("width") and measurements.get("duration") \
                and measurements.get("fps"):
            continue
        # Probed before and got nothing - do not pay for it again on every
        # schedule attempt.
        if meta.get("probed"):
            continue

        try:
            url = pipeline.presigned_url(asset.object_key)
        except Exception:  # noqa: BLE001 - unreachable storage is not fatal
            continue

        probed = probe_url(url)
        meta["probed"] = True
        if probed:
            # The browser measured the real chosen file, so it wins on any
            # field it supplied; ffprobe fills the gaps (fps, codec, and
            # everything for a container Chrome could not open).
            merged = {**probed, **measurements}
            meta["measurements"] = merged
            current_app.logger.info(
                "probed media asset=%s -> %s", asset.id,
                {k: merged.get(k) for k in ("width", "height", "duration",
                                            "fps", "codec")})
        asset.meta = meta
        changed = True

    if changed:
        db.session.commit()
