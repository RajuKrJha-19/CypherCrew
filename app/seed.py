import os

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import User, Permission, Service


def seed_database():

    permissions = [
        ("manage_users", "Manage Users"),
        ("manage_permissions", "Manage Permissions"),
        ("manage_clients", "Manage Clients"),
        ("view_client_stats", "View Client Stats"),
        ("edit_monthly_targets", "Edit Monthly Targets"),
        ("manage_tasks", "Manage Tasks"),
        ("approve_tasks", "Approve Tasks"),
        ("publish_tasks", "Publish Tasks"),
        ("view_reports", "View Reports"),
        ("manage_reports", "Manage Reports"),
    ]

    for code, name in permissions:
        exists = Permission.query.filter_by(code=code).first()

        if not exists:
            db.session.add(
                Permission(
                    code=code,
                    name=name
                )
            )

    services = [
        "SEO",
        "Social Media Management",
        "Graphic Design",
        "Motion Graphics",
        "Video Editing",
        "Content Writing",
        "Website Development",
        "Web Design / UI UX",
        "App Development",
        "Ads Management",
        "Logo & Branding"
    ]

    for service_name in services:
        exists = Service.query.filter_by(
            name=service_name
        ).first()

        if not exists:
            db.session.add(
                Service(
                    name=service_name
                )
            )

    super_admin_name = os.getenv("DEFAULT_ADMIN_NAME")
    super_admin_email = os.getenv("DEFAULT_ADMIN_EMAIL")
    super_admin_phone = os.getenv("DEFAULT_ADMIN_PHONE")
    super_admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD")

    if not all([
        super_admin_name,
        super_admin_email,
        super_admin_phone,
        super_admin_password
    ]):
        raise RuntimeError(
            "DEFAULT_ADMIN_NAME, DEFAULT_ADMIN_EMAIL, "
            "DEFAULT_ADMIN_PHONE and DEFAULT_ADMIN_PASSWORD "
            "must be configured in .env"
        )

    super_admin_email = super_admin_email.strip().lower()

    super_admin = User.query.filter_by(
        email=super_admin_email
    ).first()

    if not super_admin:
        super_admin = User(
            name=super_admin_name,
            email=super_admin_email,
            phone=super_admin_phone,
            password_hash=generate_password_hash(
                super_admin_password
            ),
            role="super_admin",
            designation="Super Administrator",
            status="active"
        )

        db.session.add(super_admin)

    else:
        super_admin.name = super_admin_name
        super_admin.phone = super_admin_phone
        super_admin.role = "super_admin"
        super_admin.designation = "Super Administrator"
        super_admin.status = "active"

    db.session.commit()