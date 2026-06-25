from app.extensions import db


class Service(db.Model):
    __tablename__ = "services"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(
        db.String(120),
        unique=True,
        nullable=False
    )