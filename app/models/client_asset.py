from datetime import datetime

from app.extensions import db


class ClientAsset(db.Model):
    """A permanent brand asset (logo, image, video, font, brand
    guideline document) that belongs to the client itself, not to any
    one task. See app.utils.client_assets for the category catalog.
    """

    __tablename__ = "client_assets"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    client_id = db.Column(
        db.Integer,
        db.ForeignKey("clients.id"),
        nullable=False,
        index=True
    )

    category = db.Column(
        db.String(20),
        nullable=False,
        default="other"
    )

    bucket_name = db.Column(
        db.String(100),
        nullable=False
    )

    storage_provider = db.Column(
        db.String(30),
        nullable=False,
        default="r2"
    )

    object_key = db.Column(
        db.String(1000),
        nullable=False,
        unique=True
    )

    original_filename = db.Column(
        db.String(255),
        nullable=False
    )

    stored_filename = db.Column(
        db.String(255),
        nullable=False
    )

    mime_type = db.Column(
        db.String(150)
    )

    file_size = db.Column(
        db.BigInteger,
        default=0
    )

    uploaded_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    client = db.relationship(
        "Client",
        backref=db.backref(
            "assets",
            lazy=True,
            order_by="ClientAsset.category, ClientAsset.created_at.desc()",
            cascade="all, delete-orphan"
        )
    )

    uploaded_by = db.relationship(
        "User"
    )

    def __repr__(self):
        return f"<ClientAsset {self.original_filename}>"
