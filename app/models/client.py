from datetime import datetime
from app.extensions import db


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    client_name = db.Column(
        db.String(150),
        nullable=False
    )

    company_name = db.Column(
        db.String(150)
    )

    phone = db.Column(
        db.String(20)
    )

    email = db.Column(
        db.String(150)
    )

    industry = db.Column(
        db.String(100)
    )

    assigned_manager_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id")
    )

    status = db.Column(
        db.String(20),
        default="active"
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    # ===========================
    # Sub-clients
    #
    # Optional, one level deep - a sub-client is a full Client row
    # (its own deliverables, targets, contact info) just linked to a
    # parent for organisation. Not required: parent_client_id stays
    # null for an ordinary top-level client. A sub-client cannot
    # itself have a parent_client_id pointing at another sub-client -
    # enforced in the add/edit client routes, not here, so a bad row
    # from outside the app doesn't crash the relationship.
    # ===========================

    parent_client_id = db.Column(
        db.Integer,
        db.ForeignKey("clients.id"),
        nullable=True
    )

    parent = db.relationship(
        "Client",
        remote_side=[id],
        backref="sub_clients"
    )

    assigned_manager = db.relationship(
        "User"
    )

    @classmethod
    def ordered_with_sub_clients(cls, status="active"):
        """Clients in the order a form dropdown should show them:
        each top-level client immediately followed by its own
        sub-clients (both groups alphabetical), so the grouping isn't
        lost the way one flat alphabetical list would lose it.

        A sub-client whose parent got filtered out by `status` (e.g.
        the parent was deactivated) still appears - just without a
        parent to be indented under - rather than silently vanishing
        from the list.
        """

        clients = cls.query.filter_by(
            status=status
        ).order_by(
            cls.client_name.asc()
        ).all()

        children_by_parent = {}

        for client in clients:
            if client.parent_client_id:
                children_by_parent.setdefault(
                    client.parent_client_id, []
                ).append(client)

        ordered = []

        for client in clients:
            if not client.parent_client_id:
                ordered.append(client)
                ordered.extend(
                    children_by_parent.get(client.id, [])
                )

        seen_ids = {client.id for client in ordered}

        for client in clients:
            if client.id not in seen_ids:
                ordered.append(client)

        return ordered