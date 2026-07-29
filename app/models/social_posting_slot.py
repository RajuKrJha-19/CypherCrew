from datetime import datetime

from app.extensions import db


class SocialPostingSlot(db.Model):
    """One recurring time slot in a channel's posting schedule.

    A channel's "posting schedule" (Buffer's core idea) is just its set of
    weekly slots - e.g. Mon/Wed/Fri at 10:00, 13:00, 17:00. "Add to queue"
    drops a post into the next slot that isn't already taken, so the team
    fills a consistent cadence without hand-picking a datetime every time.

    `weekday` is Python's convention (Monday=0 … Sunday=6). `minute` is
    minutes since midnight IST (0-1439) - the whole app schedules in IST and
    converts to UTC at the edge, so the slot is stored the way a person set it.
    """

    __tablename__ = "social_posting_slots"

    id = db.Column(db.Integer, primary_key=True)
    social_account_id = db.Column(
        db.Integer, db.ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    weekday = db.Column(db.Integer, nullable=False)     # Mon=0 … Sun=6
    minute = db.Column(db.Integer, nullable=False)      # 0-1439, IST

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    account = db.relationship("SocialAccount")

    __table_args__ = (
        db.UniqueConstraint(
            "social_account_id", "weekday", "minute",
            name="uq_posting_slot_account_day_minute",
        ),
    )

    @property
    def hhmm(self):
        return f"{self.minute // 60:02d}:{self.minute % 60:02d}"

    def __repr__(self):
        return f"<SocialPostingSlot acct={self.social_account_id} " \
               f"wd={self.weekday} {self.hhmm}>"
