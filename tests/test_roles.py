"""The role catalog's own invariants.

Pure functions, no database - these are the checks that would have caught
each of the ways the old three-string role model broke when a fourth value
appeared.
"""

import pytest

from app.utils import permissions as perms
from app.utils import roles


def test_catalog_has_the_expected_roles():
    assert len(roles.ROLE_LIST) == 15
    assert len(roles.ROLES) == 15, "duplicate role value in the catalog"


@pytest.mark.parametrize("role", roles.ROLE_LIST, ids=lambda r: r.value)
def test_role_value_fits_the_column(role):
    # users.role is String(50). Two values are 29 characters, which is why
    # the column was widened from 30 - Postgres errors rather than
    # truncates, and it would only error the first time somebody was
    # actually given that role.
    assert len(role.value) <= 50


@pytest.mark.parametrize("role", roles.ROLE_LIST, ids=lambda r: r.value)
def test_role_is_fully_described(role):
    assert role.label
    assert role.tier in roles.TIER_ORDER
    assert roles.badge_class(role.value) == f"role-tier-{role.tier}"
    assert roles.dashboard_endpoint(role.value).startswith("dashboard.")


@pytest.mark.parametrize("role", roles.ROLE_LIST, ids=lambda r: r.value)
def test_role_defaults_are_real_permissions(role):
    unknown = set(role.defaults) - set(perms.ALL_CODES)
    assert not unknown, f"{role.value} defaults reference unknown codes"

    retired = set(role.defaults) & perms.DEPRECATED_CODES
    assert not retired, f"{role.value} defaults reference retired codes"


def test_derived_collections_agree():
    assert set(roles.MANAGEMENT_ROLES) <= set(roles.ALL_ROLE_VALUES)
    assert set(roles.TEAM_MEMBER_ROLES) == (
        set(roles.ALL_ROLE_VALUES) - set(roles.MANAGEMENT_ROLES)
    )
    # "Every real user" has to mean every role, or people vanish from the
    # assignee, visibility, leave, meeting and transfer pickers.
    assert len(roles.ALL_ROLE_VALUES) == len(roles.ROLE_LIST)


def test_unknown_role_lands_on_the_individual_dashboard():
    # The regression this locks: dashboard.index used to fall through to
    # auth.logout, so any unrecognised role signed the user out on sign-in.
    assert roles.dashboard_endpoint("not_a_role") == "dashboard.employee"
    assert roles.dashboard_endpoint(None) == "dashboard.employee"
    assert "logout" not in roles.dashboard_endpoint("not_a_role")


def test_unknown_role_still_reads_as_words():
    assert roles.label("some_old_role") == "Some Old Role"
    assert roles.label("super_admin") == "Owner"


class _Actor:
    def __init__(self, role):
        self.role = role


def test_admin_cannot_assign_management_roles():
    assignable = roles.assignable_by(_Actor("admin"))

    assert "admin" not in assignable
    assert "super_admin" not in assignable
    assert "senior_video_editor" in assignable


def test_owner_can_assign_admin_but_not_another_owner():
    assignable = roles.assignable_by(_Actor("super_admin"))

    assert "admin" in assignable
    assert "super_admin" not in assignable


def test_non_management_can_assign_nothing():
    assert roles.assignable_by(_Actor("senior_video_editor")) == ()
    assert roles.assignable_by(None) == ()


def test_can_assign_role_rejects_anything_off_the_catalog():
    owner = _Actor("super_admin")

    assert roles.can_assign_role(owner, "junior_content_writer")
    assert not roles.can_assign_role(owner, "director_of_vibes")
    assert not roles.can_assign_role(owner, "")


def test_every_role_appears_in_exactly_one_dropdown_group():
    grouped = roles.grouped_options()
    listed = [role.value for _label, members in grouped for role in members]

    assert sorted(listed) == sorted(roles.ALL_ROLE_VALUES)
    assert len(listed) == len(set(listed))


def test_grouped_options_can_be_restricted():
    grouped = roles.grouped_options(roles.assignable_by(_Actor("admin")))
    listed = {role.value for _label, members in grouped for role in members}

    assert "admin" not in listed
    assert listed == set(roles.TEAM_MEMBER_ROLES)


def test_seniors_can_review_and_juniors_cannot():
    """The shape the whole catalog exists to express."""
    for discipline in ("video_editor", "graphic_designer",
                       "content_writer", "software_developer"):
        senior = roles.defaults_for(f"senior_{discipline}")
        junior = roles.defaults_for(f"junior_{discipline}")

        assert "approve_tasks" in senior
        assert "view_all_tasks" in senior
        # Reviewing is not publishing: the client-facing sign-off stays
        # with the managers.
        assert "publish_tasks" not in senior

        assert junior == set()


def test_team_member_has_no_defaults():
    # Everyone who predates the catalog is an `employee`, and applying
    # defaults to them must never hand out access they were not given.
    assert roles.defaults_for("employee") == set()


def test_owner_holds_no_default_rows():
    # The owner's power is the bypass in has_permission, not a set of
    # rows - rows could be revoked, locking them out of their own system.
    assert roles.defaults_for("super_admin") == set()
