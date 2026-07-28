from datetime import datetime, timedelta

IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_now():
    return datetime.utcnow() + IST_OFFSET


def ist_date(column):
    """SQL expression for the IST calendar date of a UTC-stored timestamp.

    Task.created_at is stored in UTC (model default datetime.utcnow), but the
    dashboard/performance windows are IST dates (ist_now().date()). Comparing
    date(created_at) directly drops "today's" work every night between IST
    00:00 and 05:30, when the UTC date is still yesterday. Shift into IST first.

    NOTE: completed_at is ALREADY stored in IST (set via ist_now()), so it must
    compare with a plain db.func.date(...) and must NOT use this helper.
    """
    from app.extensions import db
    return db.func.date(column + IST_OFFSET)