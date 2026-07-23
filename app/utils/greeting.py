"""Time-of-day greeting for the dashboard headers.

Deliberately built on ist_now() rather than utcnow(): the team works
in IST, and UTC would greet someone with "Good evening" at half past
eleven in the morning.
"""

from app.utils.timezone import ist_now


def greeting_word(now=None):
    """"Good morning" / "Good afternoon" / "Good evening"."""

    hour = (now or ist_now()).hour

    if 5 <= hour < 12:
        return "Good morning"

    if 12 <= hour < 17:
        return "Good afternoon"

    return "Good evening"


def greet(user, now=None):
    """e.g. "Good morning, Raju".

    First name only - the header is a greeting, not a record card, and
    "Good morning, Raju Kr Jha" reads like a form letter.
    """

    word = greeting_word(now)

    name = (getattr(user, "name", "") or "").strip()

    if not name:
        return word

    return f"{word}, {name.split()[0]}"


def today_label(now=None):
    """e.g. "Wednesday, 23 July 2026" - the date, in IST."""

    stamp = now or ist_now()

    # %-d is not portable to Windows, so strip the zero by hand.
    return stamp.strftime("%A, %d %B %Y").replace(" 0", " ", 1)
