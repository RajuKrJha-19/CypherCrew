from app.extensions import db


class UserPermission(db.Model):
    __tablename__ = "user_permissions"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    permission_id = db.Column(
        db.Integer,
        db.ForeignKey("permissions.id"),
        nullable=False
    )

    permission = db.relationship(
        "Permission"
    )