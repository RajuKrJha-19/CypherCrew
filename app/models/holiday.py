from app.extensions import db


class Holiday(db.Model):

    __tablename__ = "holidays"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    title = db.Column(
        db.String(150),
        nullable=False
    )

    holiday_date = db.Column(
        db.Date,
        nullable=False,
        unique=True
    )

    description = db.Column(
        db.Text
    )