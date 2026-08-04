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
        except Exception:  # noqa: BLE001 - one ad account never aborts the rest
            current_app.logger.exception(
                "[engage-ads] listing ads failed for %s", ad_account.external_id)
            continue

        for item in items or []:
            ext = item.get("external_post_id")
            if not ext:
                continue
            # Already tracked (a Studio post, or a prior ad sync)? Skip - the
            # unique-ish external_post_id keeps this idempotent across runs.
            if SocialPostTarget.query.filter_by(external_post_id=ext).first():
                continue
            owner = _resolve_owner(item, ad_account)
            if owner is None:
                continue
            post = SocialPost(
                source="ad", published_externally=True,
                client_id=(owner.client_id or ad_account.client_id),
                status="published", title="Ad post")
            db.session.add(post)
            db.session.flush()
            db.session.add(SocialPostTarget(
                social_post_id=post.id, social_account_id=owner.id,
                platform=item["platform"], post_type="image",
                external_post_id=ext, status="published"))
            discovered += 1

    db.session.commit()
    return {"discovered": discovered}


def sync_ad_comments(client_id=None):
    """Full ad pass for the cron: discover ad posts, then let the normal comment
    sync read them."""
    from app.social.services import engage
    out = sync_ad_targets(client_id)
    if out.get("skipped"):
        return out
    engage.sync_comments(client_id)
    return out
