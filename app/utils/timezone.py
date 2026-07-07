from datetime import datetime, timedelta

IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_now():
    return datetime.utcnow() + IST_OFFSET