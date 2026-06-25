from app.extensions import db


class Permission(db.Model):
    __tablename__ = "permissions"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    code = db.Column(
        db.String(100),
        unique=True,
        nullable=False
    )

    name = db.Column(
        db.String(150),
        nullable=False
    )