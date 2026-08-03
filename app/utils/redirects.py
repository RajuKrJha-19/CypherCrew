"""Safe "bounce the user back where they came from" helper.

`redirect(request.referrer or ...)` sends the browser to a header the app does
not control. A page on another origin can cross-post a form here (a CSRF
failure, or an oversized upload), and the handler would then redirect the
victim straight back to that origin - an open redirect wearing this app's
domain. The impact is modest, since the visitor was already on the other page,
but the fix costs nothing: keep the referrer only when it points at us.
"""
from urllib.parse import urlparse

from flask import request, url_for


def safe_referrer(fallback_endpoint="dashboard.index", **values):
    """The request's referrer when it is same-origin, else `fallback_endpoint`.

    Relative referrers ("/tasks/12") are kept as-is - they cannot leave the
    site. Anything absolute has to match the host this request came in on.
    """
    referrer = request.referrer or ""
    if referrer:
        parsed = urlparse(referrer)
        if not parsed.netloc and parsed.path.startswith("/"):
            return referrer                   # relative -> can't go off-site
        if parsed.netloc == urlparse(request.host_url).netloc:
            return referrer
    return url_for(fallback_endpoint, **values)
