"""In-process attendance store for ZOHO_SIMULATION_MODE.

When there is no real Zoho app, "Zoho" attendance is whatever has been
toggled here - by the /mock/zoho dev page, or by our own checkout write-back
so the loop stays consistent. The Zoho client reads this directly (no HTTP
loopback), so the whole check-in -> sync -> top-bar path runs on localhost.

State is per-process and non-persistent, which is exactly right for a dev
simulator: restart the server and everyone is "checked out" again.
"""

import threading

_lock = threading.Lock()
# email(lower) -> {"check_in": "dd/MM/yyyy HH:mm:ss", "check_out": str|None,
#                  "entry_id": str}
_state = {}
_counter = [0]


def _next_entry_id():
    _counter[0] += 1
    return f"SIM-{_counter[0]}"


def set_checked_in(email, when_str, entry_id=None):
    """Open (or re-open) a check-in for this email. Returns the entry id."""
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        row = _state.get(email)
        if row and row.get("check_out") is None:
            # Already open - leave it as is (idempotent).
            return row["entry_id"]
        eid = entry_id or _next_entry_id()
        _state[email] = {
            "check_in": when_str,
            "check_out": None,
            "entry_id": eid,
        }
        return eid


def set_checked_out(email, when_str):
    """Close the open check-in for this email, if any."""
    email = (email or "").strip().lower()
    with _lock:
        row = _state.get(email)
        if row and row.get("check_out") is None:
            row["check_out"] = when_str
            return True
    return False


def entries():
    """Snapshot of every known entry as normalised dicts:
    {email, check_in, check_out, entry_id}."""
    with _lock:
        return [
            {"email": email, **dict(row)}
            for email, row in _state.items()
        ]


def reset():
    """Test hook: clear all simulated attendance."""
    with _lock:
        _state.clear()
        _counter[0] = 0
