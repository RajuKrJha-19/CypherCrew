from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    request,
    jsonify
)

from flask_login import (
    login_required,
    current_user
)

from app.extensions import db
from app.models import Note

notes_bp = Blueprint(
    "notes",
    __name__,
    url_prefix="/notes"
)


@notes_bp.route("/")
@login_required
def list_notes():

    pinned_notes = (
        Note.query
        .filter_by(
            user_id=current_user.id,
            is_deleted=False,
            is_archived=False,
            is_pinned=True
        )
        .order_by(Note.updated_at.desc())
        .all()
    )

    notes = (
        Note.query
        .filter_by(
            user_id=current_user.id,
            is_deleted=False,
            is_archived=False,
            is_pinned=False
        )
        .order_by(Note.updated_at.desc())
        .all()
    )

    return render_template(
        "notes/list.html",
        pinned_notes=pinned_notes,
        notes=notes
    )


@notes_bp.route("/new", methods=["POST"])
@login_required
def new_note():

    note = Note(
        user_id=current_user.id,
        title="Untitled Note",
        content=""
    )

    db.session.add(note)
    db.session.commit()

    return redirect(
        url_for(
            "notes.edit_note",
            note_id=note.id
        )
    )


@notes_bp.route("/<int:note_id>")
@login_required
def edit_note(note_id):

    note = Note.query.filter_by(
        id=note_id,
        user_id=current_user.id
    ).first_or_404()

    return render_template(
        "notes/editor.html",
        note=note
    )


@notes_bp.route("/<int:note_id>/autosave", methods=["POST"])
@login_required
def autosave(note_id):

    note = Note.query.filter_by(
        id=note_id,
        user_id=current_user.id
    ).first_or_404()

    data = request.get_json()

    note.title = data.get("title", note.title)
    note.content = data.get("content", note.content)

    db.session.commit()

    return jsonify(
        success=True,
        message="Saved"
    )


@notes_bp.route("/<int:note_id>/pin", methods=["POST"])
@login_required
def toggle_pin(note_id):

    note = Note.query.filter_by(
        id=note_id,
        user_id=current_user.id
    ).first_or_404()

    note.is_pinned = not note.is_pinned

    db.session.commit()

    return redirect(
        url_for("notes.list_notes")
    )


@notes_bp.route("/<int:note_id>/archive", methods=["POST"])
@login_required
def archive_note(note_id):

    note = Note.query.filter_by(
        id=note_id,
        user_id=current_user.id
    ).first_or_404()

    note.is_archived = True

    db.session.commit()

    return redirect(
        url_for("notes.list_notes")
    )

@notes_bp.route("/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id):

    note = Note.query.filter_by(
        id=note_id,
        user_id=current_user.id
    ).first_or_404()

    note.is_deleted = True

    db.session.commit()

    return redirect(url_for("notes.list_notes"))

@notes_bp.route("/trash")
@login_required
def trash():

    notes = (
        Note.query
        .filter_by(
            user_id=current_user.id,
            is_deleted=True
        )
        .order_by(Note.updated_at.desc())
        .all()
    )

    return render_template(
        "notes/trash.html",
        notes=notes
    )