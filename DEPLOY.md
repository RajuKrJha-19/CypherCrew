# Deploying CypherCrew

The server runs a live database with real work in it. Everything below
is written so that data is never touched.

---

## What changed since the last deploy

One thing needs action beyond pulling the code:

**One database migration** — `c3f7a91b60d4`, which widens `users.role`
and adds a unique constraint plus audit columns to `user_permissions`.
It is required: two of the new role values are 29 characters and the old
column held 30, so the first person given one would otherwise fail to
save.

No new dependencies. Everything else is templates, CSS and Python that
ships with the pull.

---

## Deploy

Run these on the server, in this order.

```bash
# 1. Back up the database first. Nothing below deletes data, but a
#    backup is what makes that claim cheap to verify.
pg_dump "$DATABASE_URL" > ~/cyphercrew-backup-$(date +%F-%H%M).sql

# 2. Get the code
cd /path/to/CypherCrew
git pull origin main

# 3. Install dependencies
source venv/bin/activate
pip install -r requirements.txt

# 4. Apply the migration
flask db upgrade

# 5. Restart the app
sudo systemctl restart cyphercrew     # or: supervisorctl restart, or your dyno restart
```

That is the whole deploy. The app works fully at this point.

---

## Social Studio configuration

Studio is driven entirely by environment variables, and every one of them
defaults to *off*. This is the section to read when a channel says
**"Coming soon"** or **"Not enabled on this server"** — that state means
no publishing adapter is registered for it, which is configuration, not
missing code.

| Variable | Default | What it does |
|---|---|---|
| `SOCIAL_ENGINE_ENABLED` | `False` | Master switch. Off means the whole Studio — routes, nav item, everything — is absent. |
| `SOCIAL_SIMULATION_MODE` | `True` | Registers a demo adapter for every platform that has no real one. This is what makes YouTube, X, LinkedIn and Google Business connectable. |
| `SOCIAL_TOKEN_KEY` | unset | Fernet key encrypting stored platform tokens. Without it no account can be connected at all. |
| `SOCIAL_WORKER_TOKEN` | unset | Shared secret for the `/internal/social/*` cron endpoints. Without it they stay closed (403). |
| `SOCIAL_PUBLIC_BASE_URL` | request host | The base the OAuth redirect URI is built from. Set it behind a proxy. |
| `META_APP_ID` / `META_APP_SECRET` | unset | Real Facebook + Instagram publishing. Set these and the real adapters take over those two keys; everything else stays on whatever `SOCIAL_SIMULATION_MODE` provides. |

### Why a channel shows "Coming soon"

A platform is connectable only when an adapter is registered for it. That
happens in one of two ways:

- a **real** adapter, when its credentials are configured (today: Meta,
  via `META_APP_ID`), or
- the **simulation** adapter, when `SOCIAL_SIMULATION_MODE` is on.

So a channel greyed out on a server almost always means
`SOCIAL_SIMULATION_MODE=false` is set there, and that platform has no real
credentials. Setting it to `true` brings YouTube, X, LinkedIn and Google
Business up immediately, each marked **Demo** in the UI: the post travels
the full pipeline — approval, scheduling, the queue, the timeline — and
nothing is sent to the platform.

Facebook and Instagram are unaffected by that switch when `META_APP_ID` is
set: real adapters claim those two keys first, and simulation only fills
what is left. Verified:

```
SOCIAL_SIMULATION_MODE unset/true, META_APP_ID set
  -> facebook, instagram   real
     youtube, x, linkedin, google_business   demo

SOCIAL_SIMULATION_MODE=false, META_APP_ID set
  -> facebook, instagram   real
     everything else       "Coming soon"
```

### Checking it on a running server

```bash
flask shell
>>> from app.social.registry import registry
>>> sorted(registry.keys())
```

Whatever that prints is exactly what the Channels page will offer.

---

## Optional: pre-generate thumbnails

```bash
flask thumbnails-backfill
```

This is **optional**. Any file without a thumbnail generates one the
first time somebody views it, so the gallery is correct either way —
running this just moves that work off the first viewer.

It reads each existing image from R2 and writes a small WEBP beside it.
It never modifies or deletes an original.

For a large library, do it in batches:

```bash
flask thumbnails-backfill --limit 200     # repeat until it reports 0 pending
```

---

## Why the migration is safe

`migrations/versions/c3f7a91b60d4_role_catalog_and_permission_integrity.py`
does four things:

- widens `users.role` from `varchar(30)` to `varchar(50)`
- de-duplicates `user_permissions`, then adds a unique constraint on
  `(user_id, permission_id)`
- adds `granted_at` and `granted_by_id` to `user_permissions`, both
  nullable
- indexes `users.role`

Only the de-duplication deletes anything, and only exact duplicate grants
— the same permission held twice by the same person, which the old
delete-all-then-reinsert save could produce on a double submit. It keeps
the earliest of each pair. No permission is lost: holding a grant twice
and holding it once mean the same thing.

Everything else is additive, and `granted_at`/`granted_by_id` are
nullable because rows that predate them have no honest value.

The whole revision is written defensively — it inspects the live schema
before each step and skips what is already there, so it is safe to run
twice. That matters more than usual here: `users` and `user_permissions`
predate Alembic entirely, so the migration chain has never described them
and cannot recreate them.

**Do not run `flask db downgrade`.** Narrowing `users.role` back to 30
cannot hold the new role values, so the downgrade resets anyone on one of
them to `employee` before shrinking the column. That is real data loss.

---

## Checks after deploying

```bash
flask db current      # should print: c3f7a91b60d4 (head)
flask db heads        # should print exactly one head
```

Then in the browser:

- `/gallery/` — tiles render, filters and sort work
- `/tasks/` — board fills the window, cards drag between columns
- open a task — the side panel opens over the board
- upload an image to a task — its thumbnail appears in the gallery
- `/users/add` — the role dropdown lists all 15 roles, grouped
- `/social/accounts` — every channel is either connectable or explains
  why it is not (see the Social Studio section above)

---

## Things worth knowing

**`AUTO_SEED` runs on every boot.** It is idempotent: it inserts
permissions, services and the default super admin only when they are
missing. It never deletes. It does, however, overwrite the name, phone,
role, designation and status of the user matching
`DEFAULT_ADMIN_EMAIL` from the `.env` values on each start — so keep
those in sync with what that account should say. Set `AUTO_SEED=False`
in `.env` to turn it off entirely.

**Secrets are not in the repository.** `config.py` reads everything
from environment variables and `.env` is gitignored and has never been
committed. The server keeps its own `.env`; `git pull` will not touch
it.

**Thumbnail generation runs in-process.** There is no Celery or Redis
here — uploads hand the work to a small thread pool inside the gunicorn
worker. Nothing extra needs to be running for it to work.
