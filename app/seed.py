import os

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import User, Permission, Service


#: Display names for every live permission code, keyed by code. The codes
#: themselves - and their order on screen - come from the one catalog in
#: app/utils/permissions.py, so a code cannot exist in one place and not
#: the other.
PERMISSION_NAMES = {
    "view_all_tasks": "View All Tasks",
    "assign_tasks": "Assign Tasks",
    "manage_tasks": "Manage Tasks",
    "approve_tasks": "Approve Work (Core Review)",
    "publish_tasks": "Publish & Sign Off",
    "manage_social": "Use Social Studio",
    "connect_social_accounts": "Connect Social Accounts",
    "manage_social_engine": "Operate Publishing Engine",
    "manage_clients": "Manage Clients",
    "edit_monthly_targets": "Edit Monthly Targets",
    "view_client_stats": "View Client Stats",
    "view_reports": "View Reports",
    "view_team_performance": "View Team Performance",
    "manage_leaves": "Manage Leave",
    "manage_holidays": "Manage Holidays",
    "manage_meetings": "Manage Meetings",
    "manage_attendance": "Manage Attendance",
    "manage_users": "Manage Users",
    "manage_permissions": "Manage Permissions",
}


def seed_permissions():
    """Create any missing permission, and keep the display names current.

    Its own function so the tests can build the catalog without needing the
    DEFAULT_ADMIN_* environment variables that seed_database() insists on.

    Note the name update: this loop used to insert only, so renaming a
    permission changed nothing on any database that already had it -
    "Publish Tasks" would have gone on saying "Publish Tasks" forever,
    even though the permission now means the client-facing sign-off
    rather than what its old name implied.

    Retired codes (see permissions.DEPRECATED_CODES) are deliberately not
    created here and deliberately not deleted either: user_permissions has
    a foreign key to them, and a dead grant is harmless where a broken
    foreign key is not.
    """
    from app.utils.permissions import ALL_CODES

    for code in ALL_CODES:
        name = PERMISSION_NAMES[code]
        existing = Permission.query.filter_by(code=code).first()

        if existing is None:
            db.session.add(Permission(code=code, name=name))
        elif existing.name != name:
            existing.name = name

    db.session.commit()


def seed_database():

    seed_permissions()

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