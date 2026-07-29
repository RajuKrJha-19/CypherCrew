"""The role catalog's own invariants.

Pure functions, no database - these are the checks that would have caught
each of the ways the old three-string role model broke when a fourth value
appeared.
"""

import pytest

from app.utils import permissions as perms
from app.utils import roles


#: Owner + Admin + Team Member, plus a four-rung ladder for each of the
#: five disciplines.
EXPECTED_ROLE_COUNT = 3 + (5 * 4)

#: The ladder, lowest rung first.
LADDER_TIERS = (roles.TIER_INTERN, roles.TIER_JUNIOR,
                roles.TIER_SENIOR, roles.TIER_LEAD)


def _ladders():
    """{discipline: {tier: Role}} for every role that sits on a ladder."""
    out = {}
    for role in roles.ROLE_LIST:
        if role.discipline:
            out.setdefault(role.discipline, {})[role.tier] = role
    return out


def test_catalog_has_the_expected_roles():
    assert len(roles.ROLE_LIST) == EXPECTED_ROLE_COUNT
    assert len(roles.ROLES) == EXPECTED_ROLE_COUNT, (
        "duplicate role value in the catalog")


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
    assert "video_editor_senior" in assignable


def test_owner_can_assign_admin_but_not_another_owner():
    assignable = roles.assignable_by(_Actor("super_admin"))

    assert "admin" in assignable
    assert "super_admin" not in assignable


def test_non_management_can_assign_nothing():
    assert roles.assignable_by(_Actor("video_editor_senior")) == ()
    assert roles.assignable_by(None) == ()


def test_can_assign_role_rejects_anything_off_the_catalog():
    owner = _Actor("super_admin")

    assert roles.can_assign_role(owner, "content_writer")
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


def test_every_discipline_has_all_four_rungs():
    ladders = _ladders()

    assert set(ladders) == {
        "social_media", "video_editor", "graphic_designer",
        "content_writer", "software_developer",
    }
    for discipline, rungs in ladders.items():
        assert set(rungs) == set(LADDER_TIERS), (
            f"{discipline} is missing a rung: has {sorted(rungs)}")


def test_a_promotion_never_takes_something_away():
    """Each rung grants at least everything the rung below it does.

    The invariant that keeps the ladder meaningful. Without it, a
    thoughtfully-worded `defaults` set can quietly leave a manager with
    less access than the senior they manage, and nothing would say so.
    """
    for discipline, rungs in _ladders().items():
        for lower, higher in zip(LADDER_TIERS, LADDER_TIERS[1:]):
            below = set(rungs[lower].defaults)
            above = set(rungs[higher].defaults)

            assert below <= above, (
                f"{discipline}: {rungs[higher].value} is missing "
                f"{sorted(below - above)}, which {rungs[lower].value} has")


def test_seniors_review_but_do_not_publish():
    """Reviewing is not publishing: the client-facing sign-off stays with
    the managers."""
    for discipline, rungs in _ladders().items():
        senior = set(rungs[roles.TIER_SENIOR].defaults)

        # Social media is the exception by design - its senior grade is an
        # individual contributor (Senior Social Media Executive), and the
        # executive/manager split is exactly that approval sits above them.
        if discipline != "social_media":
            assert "approve_tasks" in senior, discipline
        assert "view_all_tasks" in senior, discipline
        assert "publish_tasks" not in senior, discipline


def test_interns_hold_nothing():
    """An intern works their own queue, and that right is implicit in the
    assignee branches rather than granted here."""
    for discipline, rungs in _ladders().items():
        assert rungs[roles.TIER_INTERN].defaults == frozenset(), discipline


def test_only_managers_may_publish():
    for discipline, rungs in _ladders().items():
        for tier_name, role in rungs.items():
            if tier_name == roles.TIER_LEAD:
                continue
            assert "publish_tasks" not in role.defaults, role.value


def _role_migration():
    """The a7c4e2f81d36 migration module, loaded by path.

    migrations/versions is not a package, so it cannot simply be imported.
    """
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parent.parent / "migrations" / "versions"
            / "a7c4e2f81d36_role_ladder_four_rungs.py")
    spec = importlib.util.spec_from_file_location("_role_ladder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_role_migration_lands_everyone_on_a_real_role():
    """Every value the migration writes has to exist in the catalog.

    A typo here would not fail anything at deploy time - it would quietly
    give a live user an unknown role, dropping them to the general tier and
    out of the dropdown groups. This is the only place that can catch it.
    """
    migration = _role_migration()
    catalog = set(roles.ALL_ROLE_VALUES)

    unknown = set(migration.FORWARD.values()) - catalog
    assert not unknown, f"migration maps onto roles that do not exist: {unknown}"

    # And it must not leave any of the values it claims to retire behind.
    stale = set(migration.FORWARD) & catalog
    assert not stale, f"catalog still contains retired values: {stale}"


def test_the_role_migration_covers_every_retired_value():
    """The reverse map is what a downgrade relies on, and the new-only map
    is what stops a downgrade leaving a value the old catalog never knew."""
    migration = _role_migration()
    catalog = set(roles.ALL_ROLE_VALUES)

    # Everything reachable by a downgrade must be an old value, and
    # everything in the current catalog except the three cross-cutting
    # roles must have somewhere to go back to.
    covered = set(migration.BACKWARD) | set(migration.NEW_ONLY)
    cross_cutting = {"super_admin", "admin", "employee"}

    assert covered == catalog - cross_cutting, (
        f"downgrade does not cover: {catalog - cross_cutting - covered}")


def test_team_member_has_no_defaults():
    # Everyone who predates the catalog is an `employee`, and applying
    # defaults to them must never hand out access they were not given.
    assert roles.defaults_for("employee") == set()


def test_owner_holds_no_default_rows():
    # The owner's power is the bypass in has_permission, not a set of
    # rows - rows could be revoked, locking them out of their own system.
    assert roles.defaults_for("super_admin") == set()
