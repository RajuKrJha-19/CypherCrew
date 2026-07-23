"""Global search.

One box, reachable from every page, that spans the things people
actually look for in this workflow - tasks first, then clients, people
and the searcher's own notes. Everything here is permission-scoped at
the query level: an employee only ever sees their own tasks and notes
and no client or people directory, exactly as the rest of the app
already gates those sections. The suggest endpoint backs the sidebar
dropdown; the results page is where a full search with entity tabs
lives.
"""

import re

from flask import Blueprint, render_template, request, jsonify, url_for
from flask_login import login_required, current_user
from sqlalchemy import or_, cast, String

from app.models import Task, Client, User, Note
from app.utils.permissions import has_permission

search_bp = Blueprint("search", __name__, url_prefix="/search")

ADMIN_ROLES = ("admin", "super_admin")

#: Below this length a query is too noisy to be useful - two characters
#: is the point most directory searches start returning signal.
MIN_QUERY = 2


def _can(permission):
    """A manager-or-permitted check, matching how the sidebar and the
    individual modules decide who may see the clients / people lists."""
    return current_user.role in ADMIN_ROLES or has_permission(current_user, permission)


def _snippet(text, length=90):
    if not text:
        return ""
    # Notes can hold light HTML; flatten it so the preview is one clean
    # line rather than stray tags.
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:length] + ("…" if len(clean) > length else "")


def _scoped_tasks():
    """Every task for a manager; only the viewer's own or shared tasks
    otherwise - the same rule as tasks.get_task_base_query()."""
    if _can("manage_tasks"):
        return Task.query

    return Task.query.filter(
        or_(
            Task.assigned_to_id == current_user.id,
            Task.visible_to.any(User.id == current_user.id),
        )
    )


def search_tasks(term, limit):
    like = f"%{term}%"
    code = term.replace("#", "")

    # Outer join so tasks without a client still match on their own
    # fields - the existing task search inner-joins and silently drops
    # them.
    rows = (
        _scoped_tasks()
        .outerjoin(Client, Task.client_id == Client.id)
        .filter(
            or_(
                Task.title.ilike(like),
                Task.description.ilike(like),
                Task.status.ilike(like),
                Task.priority.ilike(like),
                cast(Task.task_code, String).ilike(f"%{code}%"),
                Client.client_name.ilike(like),
            )
        )
        .order_by(Task.id.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "type": "task",
            "url": url_for("tasks.task_detail", task_id=task.id),
            "title": task.title,
            "code": task.task_code,
            "subtitle": task.client.client_name if task.client else "No client",
            "status": task.status,
            "priority": task.priority,
        }
        for task in rows
    ]


def search_clients(term, limit):
    if not _can("manage_clients"):
        return []

    like = f"%{term}%"

    rows = (
        Client.query.filter(
            or_(
                Client.client_name.ilike(like),
                Client.company_name.ilike(like),
                Client.email.ilike(like),
                Client.industry.ilike(like),
            )
        )
        .order_by(Client.client_name.asc())
        .limit(limit)
        .all()
    )

    return [
        {
            "type": "client",
            "url": url_for("clients.client_detail", client_id=client.id),
            "title": client.client_name,
            "subtitle": client.company_name or client.industry or "Client",
            "status": client.status,
        }
        for client in rows
    ]


def search_users(term, limit):
    if not _can("manage_users"):
        return []

    like = f"%{term}%"

    rows = (
        User.query.filter(
            or_(
                User.name.ilike(like),
                User.email.ilike(like),
                User.designation.ilike(like),
            )
        )
        .order_by(User.name.asc())
        .limit(limit)
        .all()
    )

    return [
        {
            "type": "user",
            "url": url_for("users.user_performance", user_id=user.id),
            "title": user.name,
            "subtitle": user.designation or user.role.replace("_", " ").title(),
            "status": user.role.replace("_", " ").title(),
        }
        for user in rows
    ]


def search_notes(term, limit):
    like = f"%{term}%"

    rows = (
        Note.query.filter(
            Note.user_id == current_user.id,
            Note.is_deleted == False,
            or_(
                Note.title.ilike(like),
                Note.content.ilike(like),
            ),
        )
        .order_by(Note.id.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "type": "note",
            "url": url_for("notes.edit_note", note_id=note.id),
            "title": note.title or "Untitled note",
            "subtitle": _snippet(note.content) or "Note",
        }
        for note in rows
    ]


#: The groups, in the order they surface. Each carries the fetcher so
#: the suggest dropdown and the full results page stay in lockstep.
SEARCH_GROUPS = [
    ("task", "Tasks", search_tasks),
    ("client", "Clients", search_clients),
    ("user", "People", search_users),
    ("note", "Notes", search_notes),
]


@search_bp.route("/suggest")
@login_required
def suggest():
    """Compact, grouped results for the sidebar dropdown."""

    term = (request.args.get("q") or "").strip()

    if len(term) < MIN_QUERY:
        return jsonify(groups=[], total=0, query=term)

    groups = []

    for key, label, fetch in SEARCH_GROUPS:
        limit = 6 if key == "task" else 4
        items = fetch(term, limit)

        if items:
            groups.append({"type": key, "label": label, "items": items})

    total = sum(len(group["items"]) for group in groups)

    return jsonify(
        groups=groups,
        total=total,
        query=term,
        results_url=url_for("search.results", q=term),
    )


@search_bp.route("/")
@login_required
def results():
    """The full search page: every match, with entity tabs to narrow."""

    term = (request.args.get("q") or "").strip()
    active_type = (request.args.get("type") or "all").strip()

    sections = []
    counts = {"all": 0}

    if len(term) >= MIN_QUERY:
        for key, label, fetch in SEARCH_GROUPS:
            items = fetch(term, 50)
            counts[key] = len(items)
            counts["all"] += len(items)

            if active_type in ("all", key):
                sections.append({"type": key, "label": label, "items": items})

    return render_template(
        "search/results.html",
        term=term,
        active_type=active_type,
        sections=sections,
        counts=counts,
        min_query=MIN_QUERY,
    )
