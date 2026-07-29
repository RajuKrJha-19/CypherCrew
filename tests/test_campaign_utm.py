"""Campaign grouping + UTM link tagging."""
from app.extensions import db
from app.models import SocialAccount, SocialPost
from app.social import utm


# -- UTM helper -------------------------------------------------------------

def test_utm_appends_source_medium_campaign():
    out = utm.tag_text("Shop https://vmc.com/sale now", "facebook",
                       campaign="Diwali 2026")
    assert "utm_source=facebook" in out
    assert "utm_medium=social" in out
    assert "utm_campaign=Diwali+2026" in out


def test_utm_is_idempotent():
    once = utm.tag_text("https://x.com/p", "instagram", campaign="C")
    twice = utm.tag_text(once, "instagram", campaign="C")
    assert once == twice
    assert once.count("utm_source=") == 1


def test_utm_respects_existing_query_and_trailing_punctuation():
    out = utm.tag_text("See https://x.com/p?ref=a. Thanks", "x", campaign="C")
    assert "https://x.com/p?ref=a&utm_source=x" in out     # & because ?ref exists
    assert out.endswith(". Thanks")                         # period kept out of URL


def test_utm_noop_without_links_or_source():
    assert utm.tag_text("no links here", "facebook") == "no links here"
    assert utm.tag_text("https://x.com", "") == "https://x.com"


# -- composer integration ---------------------------------------------------

def _acct(platform):
    a = SocialAccount(platform=platform, external_id=f"U-{platform}",
                      display_name=platform, account_type="page",
                      status="active")
    db.session.add(a)
    db.session.flush()
    return a


def test_campaign_stored_and_utm_applied_per_platform(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    fb = _acct("facebook").id
    ig = _acct("instagram").id
    db.session.commit()

    value = "social_uploads/x.jpg::image/jpeg"
    client.post("/social/posts", data={
        "title": "cmp", "post_type": "image",
        "caption": "Shop https://vmc.com/sale",
        "campaign": "Diwali 2026", "add_utm": "on",
        "upload_media": value,
        "account_ids": [str(fb), str(ig)],
    }, follow_redirects=True)

    post = SocialPost.query.filter_by(title="cmp").first()
    assert post.campaign == "Diwali 2026"
    by = {t.platform: t for t in post.targets}
    assert "utm_source=facebook" in by["facebook"].caption
    assert "utm_source=instagram" in by["instagram"].caption
    assert "utm_campaign=Diwali+2026" in by["facebook"].caption


def test_no_utm_leaves_links_untouched(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    fb = _acct("facebook").id
    db.session.commit()

    value = "social_uploads/x.jpg::image/jpeg"
    client.post("/social/posts", data={
        "title": "noutm", "post_type": "image",
        "caption": "Shop https://vmc.com/sale", "campaign": "C",
        "upload_media": value, "account_ids": [str(fb)],
    }, follow_redirects=True)

    post = SocialPost.query.filter_by(title="noutm").first()
    assert "utm_source" not in post.targets[0].caption


def test_drafts_campaign_filter(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    db.session.add(SocialPost(status="draft", title="AlphaPostXYZ",
                              base_caption="a", campaign="Alpha"))
    db.session.add(SocialPost(status="draft", title="BetaPostXYZ",
                              base_caption="b", campaign="Beta"))
    db.session.commit()

    d = client.get("/social/drafts?campaign=Alpha").get_data(as_text=True)
    assert "AlphaPostXYZ" in d
    assert "BetaPostXYZ" not in d
