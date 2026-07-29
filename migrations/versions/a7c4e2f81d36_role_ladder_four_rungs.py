"""Rewrite users.role onto the four-rung ladder.

The catalog used to be two rungs per discipline (senior_* / junior_*).
It is now four - Intern, the craft grade, the senior craft grade, and
Manager - so the stored strings have to move with it. A role value that is
not in the catalog still degrades safely (roles.get returns None, the label
is prettified, the tier falls to "general"), but the person would drop out
of the dropdowns' grouping and land on the wrong badge, so leaving the old
values in the table is not an option.

What this does NOT touch: user_permissions. Permissions are granted per
user and are the real source of what someone may do; role defaults only
apply when an account is created or when somebody presses "Apply role
defaults". So this migration changes titles, never access - nobody gains
or loses a capability by running it.

Data-only and idempotent: re-running maps nothing, because the old values
no longer exist after the first pass.

Revision ID: a7c4e2f81d36
Revises: a3d81f5c2b74
"""

import sqlalchemy as sa
from alembic import op

revision = "a7c4e2f81d36"
down_revision = "a3d81f5c2b74"
branch_labels = None
depends_on = None


#: old value -> new value.
#:
#: Two judgement calls worth naming:
#:
#:  * Both social media *manager* grades collapse onto the single Manager
#:    rung. The ladder has one manager per discipline by design; a "Junior
#:    Social Media Manager" was already running a pod, and demoting them to
#:    Senior Executive to fit the shape would misdescribe the job.
#:  * senior_social_media_executive and junior_social_media_manager both
#:    land near each other by title, which is expected - the old catalog
#:    had two parallel social ladders and this one has a single ladder.
FORWARD = {
    "senior_social_media_manager": "social_media_manager",
    "junior_social_media_manager": "social_media_manager",
    "senior_social_media_executive": "social_media_senior_executive",
    "junior_social_media_executive": "social_media_executive",

    "senior_video_editor": "video_editor_senior",
    "junior_video_editor": "video_editor",

    "senior_graphic_designer": "graphic_designer_senior",
    "junior_graphic_designer": "graphic_designer",

    "senior_content_writer": "content_writer_senior",
    "junior_content_writer": "content_writer",

    "senior_software_developer": "software_developer_senior",
    "junior_software_developer": "software_developer",
}

#: Reverse map for downgrade. The two social manager grades collapsed on
#: the way forward, so this cannot restore which of them a person held -
#: it sends both back to the senior grade. Noted rather than hidden: a
#: lossy downgrade is fine here (it is a title, and permissions are
#: untouched), but it should not be a surprise.
BACKWARD = {
    "social_media_manager": "senior_social_media_manager",
    "social_media_senior_executive": "senior_social_media_executive",
    "social_media_executive": "junior_social_media_executive",

    "video_editor_senior": "senior_video_editor",
    "video_editor": "junior_video_editor",

    "graphic_designer_senior": "senior_graphic_designer",
    "graphic_designer": "junior_graphic_designer",

    "content_writer_senior": "senior_content_writer",
    "content_writer": "junior_content_writer",

    "software_developer_senior": "senior_software_developer",
    "software_developer": "junior_software_developer",
}

#: New rungs with no predecessor. Nobody can be holding these yet, so the
#: downgrade parks them on the nearest old grade rather than leaving a
#: value the previous catalog never knew.
NEW_ONLY = {
    "social_media_intern": "junior_social_media_executive",
    "video_editor_intern": "junior_video_editor",
    "video_editor_manager": "senior_video_editor",
    "graphic_designer_intern": "junior_graphic_designer",
    "graphic_designer_manager": "senior_graphic_designer",
    "content_writer_intern": "junior_content_writer",
    "content_writer_manager": "senior_content_writer",
    "software_developer_intern": "junior_software_developer",
    "engineering_manager": "senior_software_developer",
}


def _remap(mapping):
    """Apply a value map to users.role, one UPDATE per pair.

    Bound parameters rather than interpolation, and a plain UPDATE rather
    than a CASE: the table is small, and this reads as what it is.
    """
    bind = op.get_bind()
    if "users" not in sa.inspect(bind).get_table_names():
        return

    for old, new in mapping.items():
        bind.execute(
            sa.text("UPDATE users SET role = :new WHERE role = :old"),
            {"new": new, "old": old},
        )


def upgrade():
    _remap(FORWARD)


def downgrade():
    _remap({**BACKWARD, **NEW_ONLY})
