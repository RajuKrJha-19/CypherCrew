"""Instagram carousel: every child slide is validated, not just the first
(M-6). The generic pipeline measures only content.media[0] against the
(absent) carousel spec, so an out-of-spec later slide would otherwise reach
Meta and fail mid-publish, leaving a partial carousel on the grid.
"""
from app.social.dto import MediaRef, PostContent
from app.social.providers.meta_instagram import MetaInstagramProvider


def _carousel(media):
    return PostContent(platform="instagram", post_type="carousel", media=media)


def _img(key, w, h):
    return MediaRef(object_key=key, mime_type="image/jpeg",
                    measurements={"width": w, "height": h, "bytes": 500_000})


def test_all_carousel_children_are_checked_not_just_the_first():
    ig = MetaInstagramProvider()
    # First slide fine (1:1); third slide a 6:1 banner - way past 1.91:1.
    content = _carousel([
        _img("a.jpg", 1080, 1080),
        _img("b.jpg", 1080, 1350),
        _img("c.jpg", 3000, 500),
    ])
    problems = ig.validate(content)
    assert any("Carousel item 3" in p for p in problems), problems
    assert not any("Carousel item 1" in p for p in problems)
    assert not any("Carousel item 2" in p for p in problems)


def test_a_clean_carousel_passes():
    ig = MetaInstagramProvider()
    content = _carousel([
        _img("a.jpg", 1080, 1080),
        _img("b.jpg", 1080, 1350),
    ])
    assert ig.validate(content) == []


def test_unmeasured_children_are_left_to_meta():
    ig = MetaInstagramProvider()
    content = _carousel([
        MediaRef(object_key="a.jpg", mime_type="image/jpeg"),
        MediaRef(object_key="b.jpg", mime_type="image/jpeg"),
    ])
    # No measurements -> no local spec failure (platform judges instead).
    assert not any("Carousel item" in p for p in ig.validate(content))
