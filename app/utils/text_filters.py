import re
from markupsafe import Markup, escape


URL_RE = re.compile(
    r"(https?://[^\s<]+|www\.[^\s<]+)",
    re.IGNORECASE
)


def linkify_text(value):
    if not value:
        return ""

    escaped = escape(value)

    def replace_url(match):
        url = match.group(0)
        href = url

        if href.startswith("www."):
            href = "https://" + href

        return (
            f'<a href="{href}" target="_blank" '
            f'rel="noopener noreferrer" class="auto-link">'
            f'{url}</a>'
        )

    return Markup(URL_RE.sub(replace_url, str(escaped)))