"""UTM tagging for links in a post's caption / first comment.

When "add UTM to links" is on, every http(s) URL gets utm_source (the actual
channel), utm_medium=social, and utm_campaign (the post's campaign) appended -
so a client sees social-driven traffic attributed in their analytics.

Applied per target, so utm_source is the real platform. Idempotent: a link
that already carries utm_source is left untouched, and trailing punctuation
("see https://x.com.") is kept out of the URL.
"""

import re
from urllib.parse import quote_plus

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_TRAILING = ").,!?;:'\""


def _slug(value):
    return quote_plus((value or "").strip())


def tag_text(text, source, campaign=None, medium="social"):
    """Return `text` with UTM params appended to each URL. No-op without a
    source or when the text has no links."""
    if not text or not source:
        return text

    def _tag(match):
        url = match.group(0)
        trail = ""
        while url and url[-1] in _TRAILING:
            trail = url[-1] + trail
            url = url[:-1]
        if not url or "utm_source=" in url.lower():
            return url + trail
        params = f"utm_source={_slug(source)}&utm_medium={_slug(medium)}"
        if campaign:
            params += f"&utm_campaign={_slug(campaign)}"
        sep = "&" if "?" in url else "?"
        return url + sep + params + trail

    return _URL_RE.sub(_tag, text)
