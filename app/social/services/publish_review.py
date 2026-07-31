"""What is about to be published, answered before the button does it.

Publishing used to be a single unguarded click. Everything that decides what
actually goes out - the post type each platform resolves to, the caption after
per-channel overrides, the exact instant per channel, and whether a channel
will be refused outright - was computed AFTER the click and reported back as a
flash. The person publishing found out what they had published by reading the
result.

This module answers the same questions first, from the same functions the
publish path uses, so the review cannot drift from the outcome:

    publishing.validate_target      the provider's own pre-flight
    publishing._downscale_would_fix + transcode.available()
                                    resize-on-publish vs blocked
    media_fit.choose_post_type      what a reel actually becomes here

Nothing here writes. validate_target does call probe.ensure_measured, which
caches a media measurement when ffprobe is installed - that is a read-through
cache the publish path would have filled a moment later anyway, and it makes
the review MORE accurate rather than less.
"""

import hashlib
from datetime import datetime, timedelta

from app.social.media import transcode
from app.social.registry import get_provider
from app.social.services import publishing

#: Same offset the Studio's routes use. The team schedules in IST and a review
#: that quoted UTC would be a worse answer than no review.
IST_OFFSET = timedelta(hours=5, minutes=30)

#: What will happen to a channel when the button is pressed.
WILL_PUBLISH = "publish"
WILL_RESIZE = "resize"
WILL_BLOCK = "block"


def fingerprint(post, publish_mode="", schedule_raw=""):
    """A digest of everything the review just described.

    Handed to the browser with the review and sent back with the submission,
    so schedule_post can refuse a confirmation that no longer describes
    reality. The case this exists for is mundane and expensive: you open the
    review, a colleague edits the post in another tab, you press Confirm - and
    publish something you never saw.

    The publish mode and the typed time are folded in as well, so a review
    read as "Schedule for Friday" cannot be confirmed as "Publish now".

    Deliberately NOT included: the effective publish instant. For "publish
    now" that is the current clock, which would change between rendering the
    review and answering it and reject every honest confirmation.
    """
    parts = [
        str(post.id),
        post.status or "",
        # updated_at moves on any edit to the post itself.
        (post.updated_at or datetime.min).isoformat(),
        publish_mode or "",
        (schedule_raw or "").strip(),
    ]

    # Per target, the fields that decide what goes out. Sorted by id so the
    # relationship's ordering cannot change the digest on its own.
    for target in sorted(post.targets, key=lambda t: t.id):
        parts.append("|".join([
            str(target.id),
            target.platform or "",
            target.post_type or "",
            target.caption or "",
            target.first_comment or "",
            target.story_style or "",
            str(target.social_account_id or ""),
            (target.scheduled_for.isoformat() if target.scheduled_for else ""),
        ]))

    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:32]


def _effective_time(target, publish_now, override, now):
    """The instant this channel will actually go out - mirroring the branch in
    the schedule_post route, which is the only thing that sets it."""
    if publish_now:
        return now
    if override is not None:
        return override
    return target.scheduled_for


def _resolved_type(target):
    """(post_type, notes) the platform will really use.

    A Reel that this platform cannot take goes out as a plain video, and the
    person deserves to know that before it does, not from the permalink.
    """
    provider = get_provider(target.platform)
    caps = getattr(provider, "capabilities", None) if provider else None

    if caps is None:
        return target.post_type, []

    measurements = {}
    for asset in getattr(target, "media", None) or []:
        measurements = (asset.meta or {}).get("measurements") or {}
        if measurements:
            break

    from app.social.media import fit as media_fit

    chosen, notes = media_fit.choose_post_type(
        target.post_type, caps, measurements)
    return chosen, list(notes or [])


def _caption_for(target):
    return (target.caption or "").strip()


def build_review(post, publish_mode="schedule", schedule_override=None,
                 schedule_raw="", now=None):
    """Everything the modal needs, as plain data.

    `schedule_override` is the already-parsed UTC datetime from the form's
    time field (None when it was left blank, which means "keep each channel's
    own time"). `schedule_raw` is the untouched form value, folded into the
    fingerprint.
    """
    now = now or datetime.utcnow()
    publish_now = publish_mode == "now"
    can_resize = transcode.available()

    channels = []

    for target in sorted(post.targets, key=lambda t: t.id):
        errors = publishing.validate_target(target)
        fixable = publishing._downscale_would_fix(target) if errors else False

        if not errors:
            outcome = WILL_PUBLISH
            reasons = []
        elif fixable and can_resize:
            # ffmpeg will fix it at publish time. Not a problem the person has
            # to solve - but still worth saying, because the file that lands
            # on the platform is not byte-for-byte the one they uploaded.
            outcome = WILL_RESIZE
            reasons = errors
        else:
            outcome = WILL_BLOCK
            reasons = list(errors)
            if fixable and not can_resize:
                reasons.append(
                    "Auto-resize is off because ffmpeg is not installed on "
                    "the server.")

        resolved, notes = _resolved_type(target)
        caption = _caption_for(target)

        provider = get_provider(target.platform)
        caps = getattr(provider, "capabilities", None) if provider else None
        max_caption = getattr(caps, "max_caption_chars", None)

        when = _effective_time(target, publish_now, schedule_override, now)

        channels.append({
            "target": target,
            "account_name": (target.account.display_name
                             if getattr(target, "account", None) else None),
            "platform": target.platform,
            "outcome": outcome,
            "reasons": reasons,
            "resolved_type": resolved,
            # Only worth surfacing when it is NOT what was asked for.
            "type_changed": bool(resolved and resolved != target.post_type),
            "type_notes": notes,
            "caption": caption,
            "caption_len": len(caption),
            "max_caption": max_caption,
            "caption_over": bool(max_caption and len(caption) > max_caption),
            "first_comment": (target.first_comment or "").strip(),
            "story_style": target.story_style,
            "when": when,
            "when_ist": (when + IST_OFFSET) if when else None,
            "immediate": publish_now,
        })

    publishing_count = sum(1 for c in channels
                           if c["outcome"] in (WILL_PUBLISH, WILL_RESIZE))
    blocked_count = sum(1 for c in channels if c["outcome"] == WILL_BLOCK)

    # Channels can carry different times, and a review that showed only one
    # of them would be lying about the rest.
    distinct_times = {c["when"] for c in channels if c["when"]}

    return {
        "post": post,
        "channels": channels,
        "publish_mode": publish_mode,
        "publish_now": publish_now,
        "publishing_count": publishing_count,
        "blocked_count": blocked_count,
        "staggered": len(distinct_times) > 1,
        "can_resize": can_resize,
        # Nothing schedulable means schedule_post will mark the whole post
        # failed - the modal should say so rather than offering a Confirm
        # that only produces an error flash.
        "nothing_to_publish": publishing_count == 0,
        "fingerprint": fingerprint(post, publish_mode, schedule_raw),
    }
