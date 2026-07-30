"""Zoho People client: OAuth handshake, authorised HTTP, attendance I/O.

Two modes behind one interface:

* **Simulation** (ZOHO_SIMULATION_MODE): no network. Reads/writes the
  in-process sim_store, so the whole flow runs locally with no Zoho app.
* **Real**: refresh-token OAuth (short-lived access token refreshed just
  before use, like the Google adapter), talking to people.zoho.<dc>.

Zoho attendance datetimes are in the organisation's timezone (IST for this
deployment) and formatted dd/MM/yyyy HH:mm:ss. We store UTC, so times cross
this boundary in exactly one place: _ist_str_to_utc / _utc_to_ist_str.
"""

from datetime import datetime, timedelta

import requests
from flask import current_app

from app.utils.timezone import IST_OFFSET
from app.attendance import sim_store

_TIMEOUT = 30
_ZOHO_FMT = "%d/%m/%Y %H:%M:%S"
#: Refresh the access token this many seconds before it actually expires.
_REFRESH_MARGIN = 300


class ZohoError(Exception):
    """Any failure talking to Zoho People (network or non-2xx)."""


def _cfg(key, default=None):
    return current_app.config.get(key, default)


def simulation():
    return bool(_cfg("ZOHO_SIMULATION_MODE"))


# ---------------------------------------------------------------------------
# Datetime boundary (Zoho org-local IST string  <->  our stored UTC)
# ---------------------------------------------------------------------------

def _ist_str_to_utc(value):
    if not value:
        return None
    try:
        local = datetime.strptime(value.strip(), _ZOHO_FMT)
    except (ValueError, AttributeError):
        return None
    return local - IST_OFFSET


def _utc_to_ist_str(when):
    when = when or datetime.utcnow()
    return (when + IST_OFFSET).strftime(_ZOHO_FMT)


# ---------------------------------------------------------------------------
# OAuth (real mode)
# ---------------------------------------------------------------------------

def build_oauth_url(state, redirect_uri):
    """Zoho consent URL. access_type=offline + prompt=consent are what make
    a refresh token arrive (without them the connection dies in an hour)."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": _cfg("ZOHO_CLIENT_ID"),
        "scope": _cfg("ZOHO_SCOPES", "ZOHOPEOPLE.attendance.all"),
        "redirect_uri": redirect_uri,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    base = _cfg("ZOHO_ACCOUNTS_BASE_URL", "https://accounts.zoho.com")
    return f"{base}/oauth/v2/auth?{urlencode(params)}"


def _token_request(data):
    base = _cfg("ZOHO_ACCOUNTS_BASE_URL", "https://accounts.zoho.com")
    try:
        resp = requests.post(f"{base}/oauth/v2/token", data=data,
                             timeout=_TIMEOUT)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ZohoError(f"Zoho token request failed: {exc}")
    if "error" in body:
        raise ZohoError(f"Zoho token error: {body.get('error')}")
    return body


def exchange_code(code, redirect_uri):
    """Authorization code -> {access_token, refresh_token, expires_at, meta}."""
    body = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _cfg("ZOHO_CLIENT_ID"),
        "client_secret": _cfg("ZOHO_CLIENT_SECRET"),
        "redirect_uri": redirect_uri,
    })
    refresh = body.get("refresh_token")
    if not refresh:
        raise ZohoError(
            "Zoho did not return a refresh token. Remove this app under "
            "Zoho accounts > Connected Apps and connect again so consent is "
            "shown afresh."
        )
    return {
        "access_token": body.get("access_token"),
        "refresh_token": refresh,
        "expires_at": _expiry(body.get("expires_in")),
        "meta": {"api_domain": body.get("api_domain")},
    }


def _refresh_access_token(refresh_token):
    body = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _cfg("ZOHO_CLIENT_ID"),
        "client_secret": _cfg("ZOHO_CLIENT_SECRET"),
    })
    return body.get("access_token"), _expiry(body.get("expires_in"))


def _expiry(expires_in):
    if not expires_in:
        return None
    return datetime.utcnow() + timedelta(seconds=int(expires_in))


def _access_token(connection):
    """A currently-valid access token for the org connection, refreshing via
    the stored (encrypted) refresh token when close to expiry."""
    from app.extensions import db
    from app.social.tokens.vault import get_vault

    vault = get_vault()
    needs_refresh = (
        not connection.token_ciphertext
        or connection.token_expires_at is None
        or connection.token_expires_at
        <= datetime.utcnow() + timedelta(seconds=_REFRESH_MARGIN)
    )
    if needs_refresh:
        if not connection.refresh_ciphertext:
            raise ZohoError("Zoho connection has no refresh token; reconnect.")
        refresh = vault.decrypt(connection.refresh_ciphertext)
        access, expires_at = _refresh_access_token(refresh)
        connection.token_ciphertext = vault.encrypt(access)
        connection.token_key_version = vault.version
        connection.token_expires_at = expires_at
        db.session.commit()
        return access
    return vault.decrypt(connection.token_ciphertext)


def _people_base():
    return _cfg("ZOHO_PEOPLE_API_BASE_URL", "https://people.zoho.com")


def _auth_headers(token):
    return {"Authorization": f"Zoho-oauthtoken {token}"}


# ---------------------------------------------------------------------------
# Attendance I/O (mode-aware)
# ---------------------------------------------------------------------------

def get_entries(connection, on_date=None):
    """Today's attendance as normalised dicts:
        [{email, check_in_utc, check_out_utc, entry_id}]

    Simulation reads the sim_store; real mode calls Zoho's fetch-last-entries
    report (one batch call, not per-user, to respect the 100/10-min limit)
    and normalises it. check_out_utc is None while someone is still in.
    """
    if simulation():
        out = []
        for row in sim_store.entries():
            out.append({
                "email": (row.get("email") or "").strip().lower(),
                "check_in_utc": _ist_str_to_utc(row.get("check_in")),
                "check_out_utc": _ist_str_to_utc(row.get("check_out")),
                "entry_id": row.get("entry_id"),
            })
        return out

    # --- Real Zoho (deferred until an org is connected). One batch report
    #     call per cycle; the exact endpoint/shape is verified against the
    #     customer's Zoho plan when real credentials are wired. ---
    token = _access_token(connection)
    date_str = (on_date or datetime.utcnow() + IST_OFFSET).strftime("%d-%m-%Y")
    try:
        resp = requests.get(
            f"{_people_base()}/people/api/attendance/fetchLatestAttEntries",
            headers=_auth_headers(token),
            params={"dateFormat": "dd-MM-yyyy", "date": date_str},
            timeout=_TIMEOUT,
        )
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ZohoError(f"Zoho attendance fetch failed: {exc}")
    return _normalise_entries(body)


def _normalise_entries(body):
    """Map Zoho's report payload into our normalised dicts. Tolerant of the
    several shapes Zoho uses across plans; anything unrecognised is skipped
    rather than raising."""
    out = []
    records = body if isinstance(body, list) else body.get("result") or []
    if isinstance(records, dict):
        records = list(records.values())
    for rec in records:
        if not isinstance(rec, dict):
            continue
        email = (rec.get("emailId") or rec.get("email") or "").strip().lower()
        if not email:
            continue
        # Only a genuine per-entry id - never the email as a fallback. The
        # email is identical across days, so using it as an entry id would
        # let a re-sync match a prior day's closed session. When Zoho gives no
        # id we rely on current_open_session to reconcile instead.
        raw_id = rec.get("entryId") or rec.get("id")
        out.append({
            "email": email,
            "check_in_utc": _ist_str_to_utc(
                rec.get("checkIn") or rec.get("firstIn")),
            "check_out_utc": _ist_str_to_utc(
                rec.get("checkOut") or rec.get("lastOut")),
            "entry_id": str(raw_id) if raw_id else None,
        })
    return out


def checkout(connection, user, when=None):
    """Write a check-out back to Zoho for this user. Raises ZohoError on
    failure so the caller can keep the local checkout pending + retry."""
    when = when or datetime.utcnow()
    if simulation():
        sim_store.set_checked_out(user.email, _utc_to_ist_str(when))
        return True

    token = _access_token(connection)
    emp = user.zoho_employee_id or user.email
    try:
        resp = requests.post(
            f"{_people_base()}/people/api/attendance",
            headers=_auth_headers(token),
            data={
                "checkOut": _utc_to_ist_str(when),
                "dateFormat": "dd/MM/yyyy HH:mm:ss",
                "empId": emp,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise ZohoError(f"Zoho checkout HTTP {resp.status_code}")
    except requests.RequestException as exc:
        raise ZohoError(f"Zoho checkout failed: {exc}")
    return True


def resolve_employee(connection, email):
    """Zoho employee id (erecno/empId) for an email, or None. Simulation has
    no distinct id, so the email is its own key."""
    if simulation():
        return None
    token = _access_token(connection)
    try:
        resp = requests.get(
            f"{_people_base()}/people/api/forms/employee/getRecordByEmail",
            headers=_auth_headers(token),
            params={"email": email},
            timeout=_TIMEOUT,
        )
        body = resp.json()
    except (requests.RequestException, ValueError):
        return None
    rec = body.get("response", {}).get("result") if isinstance(body, dict) else None
    if isinstance(rec, list) and rec:
        first = rec[0]
        if isinstance(first, dict):
            return str(first.get("erecno") or first.get("EmployeeID") or "") or None
    return None
