"""Ad / boosted-post comment ingestion.

Discovers a client's ad/boosted posts (via their mapped ad accounts) and
materialises a lightweight source="ad" SocialPost + SocialPostTarget for each,
so the EXISTING comment sync / reply / moderation handle their comments with no
changes. Dormant unless SOCIAL_ADS_COMMENTS_ENABLED is on and an ad account is
mapped to a client.

The heavy lifting - which Meta edge, which fields - lives in the provider's
list_ad_posts; this service only resolves the owning connected account and
materialises records. Facebook dark posts resolve by the page-id prefix of the
story id; Instagram ad media resolve to the ad account's client's IG account.
"""
from flask import current_app

from app.extensions import db
from app.models import SocialAccount, SocialPost, SocialPostTarget
from app.social.registry import get_provider


def _enabled():
    return bool(current_app.config.get("SOCIAL_ADS_COMMENTS_ENABLED"))


def _ad_accounts(client_id=None):
    q = SocialAccount.query.filter_by(account_type="ad_account", status="active")
    if client_id:
        q = q.filter_by(client_id=client_id)
    return q.all()


def _resolve_owner(item, ad_account):
    """The connected Page/IG SocialAccount that owns this ad post, or None (in
    which case we can't get a token to read/reply, so we skip it)."""
    platform = item.get("platform")
    ext = item.get("external_post_id") or ""
    if platform == "facebook":
        page_id = ext.split("_")[0] if "_" in ext else None
        if not page_id:
            return None
        return SocialAccount.query.filter_by(
            platform="facebook", external_id=page_id).first()
    if platform == "instagram":
        # IG media ids carry no page prefix; use the ad account's client's IG
        # account (the account whose token can read that media's comments).
        if not ad_account.client_id:
            return None
        return SocialAccount.query.filter_by(
            platform="instagram", account_type="ig_business",
            client_id=ad_account.client_id).first()
    return None


def sync_ad_targets(client_id=None):
    """Materialise source="ad" targets for the client's ad posts; the existing
    sync_comments then reads their comments. Best-effort + idempotent (dedup by
    external_post_id). Returns counts."""
    if not _enabled():
        return {"discovered": 0, "skipped": "disabled"}
    from app.social.services.accounts import AccountManager

    discovered = 0
    for ad_account in _ad_accounts(client_id):
        provider = get_provider(ad_account.platform)
        if provider is None or not hasattr(provider, "list_ad_posts"):
            continue
        try:
            token = AccountManager.access_token(ad_account)
            items = provider.list_ad_posts(ad_account.external_id, token)
        except Exception as exc:  # noqa: BLE001 - one ad account never aborts the rest
            # The ad account carries the ~60-day USER token (it's what holds
            # ads_read). When it expires, list_ad_posts fails with an auth
            # error and discovery would silently stop. Flag the account
            # needs_reauth so it surfaces on the Channels screen with a
            # reconnect prompt, exactly like a Page whose token lapsed - rather
            # than failing quietly forever.
            from app.social.errors import AuthError
            try:
                mapped = provider.map_error(exc)
            except Exception:  # noqa: BLE001
                mapped = exc
            if isinstance(mapped, AuthError) and ad_account.status != "needs_reauth":
                ad_account.status = "needs_reauth"
                db.session.commit()
            current_app.logger.exception(
                "[engage-ads] listing ads failed for %s", ad_account.external_id)
            continue

        for item in items or []:
            ext = item.get("external_post_id")
            if not ext:
                continue
            owner = _resolve_owner(item, ad_account)
            if owner is None:
                continue
            existing = SocialPostTarget.query.filter_by(
                external_post_id=ext).first()
            if existing is not None:
                # Already tracked (a Studio post, or a prior ad sync). Still
                # refresh the preview: Meta's picture URLs expire, and ad posts
                # discovered before this existed have no caption at all - which
                # is what left the AI writing replies with no idea what the
                # post said.
                _refresh_details(existing, owner)
            else:
                post = SocialPost(
                    source="ad", published_externally=True,
                    client_id=(owner.client_id or ad_account.client_id),
                    status="published", title="Ad post")
                db.session.add(post)
                db.session.flush()
                target = SocialPostTarget(
                    social_post_id=post.id, social_account_id=owner.id,
                    platform=item["platform"], post_type="image",
                    external_post_id=ext, status="published")
                db.session.add(target)
                db.session.flush()
                _refresh_details(target, owner)
                discovered += 1
            # Commit THIS post before the next item's Graph call. _refresh_details
            # makes a Graph request per item; a single commit at the end would
            # hold this post's INSERT/UPDATE row locks on social_post_targets
            # open across every following network call - the exact contention
            # that sync_comments was just fixed to avoid. A per-item commit also
            # means one bad post can't roll back the whole discovery run.
            db.session.commit()

    return {"discovered": discovered}


def _refresh_details(target, owner):
    """Pull the post's own caption / picture / permalink onto `target`.

    Best-effort and non-destructive: a failed or empty read leaves whatever is
    already stored, so a Meta hiccup never blanks a caption we had.
    """
    from app.social.services.accounts import AccountManager

    provider = get_provider(target.platform)
    if provider is None or not hasattr(provider, "fetch_post_details"):
        return
    try:
        token = AccountManager.access_token(owner)
        details = provider.fetch_post_details(target.external_post_id, token)
    except Exception:  # noqa: BLE001 - a preview is never worth a failed sync
        current_app.logger.warning(
            "[engage-ads] could not read post details for %s",
            target.external_post_id)
        return
    if not details:
        return
    # Caption/permalink describe the post itself. Fill them for an AD post, or
    # to BACKFILL any target that has none yet - but never REPLACE a value a
    # human edited in Studio on a boosted post. The same dark post can be a
    # boosted Studio post whose caption was edited after publishing; refreshing
    # it from the platform every ad sync would silently undo that edit. Only the
    # thumbnail (which expires) is refreshed unconditionally.
    is_ad = getattr(getattr(target, "post", None), "source", None) == "ad"
    if details.get("caption") and (is_ad or not (target.caption or "").strip()):
        target.caption = details["caption"]          # caption col is Text - no cap
    if details.get("permalink") and (is_ad or not target.permalink):
        # permalink is String(500); a longer value would raise on commit and,
        # with the per-item commit below, drop just this post. Skip an
        # over-length one rather than store a truncated (broken) link.
        if len(details["permalink"]) <= 500:
            target.permalink = details["permalink"]
    # The picture URL is refreshed even when it was already set - Meta's CDN
    # links expire, so the newest one is always the most likely to render. It's
    # String(1000); Meta's signed URLs can exceed that, so guard the length and
    # degrade to "no picture" (the template already hides a broken image)
    # instead of raising on commit.
    if details.get("thumbnail_url") and len(details["thumbnail_url"]) <= 1000:
        target.thumbnail_url = details["thumbnail_url"]


def sync_ad_comments(client_id=None):
    """Full ad pass for the cron: discover ad posts, then let the normal comment
    sync read them."""
    from app.social.services import engage
    out = sync_ad_targets(client_id)
    if out.get("skipped"):
        return out
    engage.sync_comments(client_id)
    return out
