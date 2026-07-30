"""Local Zoho People emulator, mounted at /mock/zoho in simulation mode.

Stands in for a real Zoho org so the whole check-in -> sync -> top-bar ->
idle-alert loop is exercisable on localhost with no Zoho app. This is a
dev-only tool: it is registered only when ATTENDANCE_ENABLED and
ZOHO_SIMULATION_MODE are both on (and simulation is force-disabled the
moment real Zoho credentials are present).
"""

from flask import (
    Blueprint, redirect, render_template_string, request, url_for,
)
from flask_login import login_required

from app.attendance import service, sim_store
from app.models import User
from app.utils.permissions import can_manage_attendance
from app.utils.timezone import ist_now

zoho_emulator_bp = Blueprint("zoho_emulator", __name__, url_prefix="/mock/zoho")

_ZOHO_FMT = "%d/%m/%Y %H:%M:%S"

_PAGE = """
<!doctype html><meta charset="utf-8">
<title>Zoho People emulator</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem}
  h1{font-size:1.25rem} table{width:100%;border-collapse:collapse}
  td,th{padding:.5rem;border-bottom:1px solid #ddd;text-align:left;font-size:.9rem}
  .in{color:#0a7d33;font-weight:600}.out{color:#888}
  button{padding:.35rem .7rem;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer}
  form{display:inline}
</style>
<h1>Zoho People emulator <small>(simulation mode)</small></h1>
<p>Toggle a Zoho-source employee's attendance. Our sync picks it up on the
next poll (or immediately, below).</p>
<table>
  <tr><th>Employee</th><th>Zoho status</th><th></th></tr>
  {% for u in users %}
  <tr>
    <td>{{ u.name }}<br><small>{{ u.email }}</small></td>
    <td>{% if u.email|lower in open_emails %}<span class="in">Checked in</span>
        {% else %}<span class="out">Checked out</span>{% endif %}</td>
    <td>
      {% if u.email|lower in open_emails %}
        <form method="post" action="{{ url_for('zoho_emulator.checkout') }}">
          <input type="hidden" name="email" value="{{ u.email }}">
          <button type="submit">Simulate check-out</button></form>
      {% else %}
        <form method="post" action="{{ url_for('zoho_emulator.checkin') }}">
          <input type="hidden" name="email" value="{{ u.email }}">
          <button type="submit">Simulate check-in</button></form>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
"""


def _guard():
    return can_manage_attendance()


@zoho_emulator_bp.route("/", methods=["GET"])
@login_required
def index():
    if not _guard():
        return redirect(url_for("dashboard.index"))
    users = User.query.filter(User.status == "active").order_by(
        User.name).all()
    users = [u for u in users if service.source_of(u) == "zoho"]
    open_emails = {
        e["email"] for e in sim_store.entries() if e.get("check_out") is None
    }
    return render_template_string(_PAGE, users=users, open_emails=open_emails)


@zoho_emulator_bp.route("/checkin", methods=["POST"])
@login_required
def checkin():
    if not _guard():
        return redirect(url_for("dashboard.index"))
    email = request.form.get("email", "")
    sim_store.set_checked_in(email, ist_now().strftime(_ZOHO_FMT))
    # Reflect it immediately rather than waiting for the poll.
    service.sync_attendance()
    return redirect(url_for("zoho_emulator.index"))


@zoho_emulator_bp.route("/checkout", methods=["POST"])
@login_required
def checkout():
    if not _guard():
        return redirect(url_for("dashboard.index"))
    email = request.form.get("email", "")
    sim_store.set_checked_out(email, ist_now().strftime(_ZOHO_FMT))
    service.sync_attendance()
    return redirect(url_for("zoho_emulator.index"))
