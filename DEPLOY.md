# Deploying CypherCrew

The server runs a live database with real work in it. Everything below
is written so that data is never touched.

---

## What changed since the last deploy

Two things need action beyond pulling the code:

1. **A new dependency** — `Pillow`, used to generate image thumbnails.
2. **One database migration** — `e833b45cc876`, which adds two columns
   to `task_files`.

Everything else is templates, CSS and Python that ships with the pull.

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

# 3. Install dependencies (Pillow is new)
source venv/bin/activate
pip install -r requirements.txt

# 4. Apply the migration
flask db upgrade

# 5. Restart the app
sudo systemctl restart cyphercrew     # or: supervisorctl restart, or your dyno restart
```

That is the whole deploy. The app works fully at this point.

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

`migrations/versions/e833b45cc876_add_thumbnail_columns_to_task_files.py`
does exactly three things:

- adds `task_files.thumbnail_key` (nullable)
- adds `task_files.thumbnail_state` with `server_default='pending'`
- adds an index on `thumbnail_state`

All three are additive. No table is dropped, no column is removed, no
row is written or deleted. Existing rows get `thumbnail_state='pending'`
from the server default, which is accurate — none of them has a
thumbnail yet — and they are filled in lazily as files are viewed.

The `server_default` is load-bearing, not decoration: PostgreSQL rejects
a `NOT NULL` column added to a populated table without one. It was
tested against a table that already had rows.

**Do not run `flask db downgrade`.** The downgrade drops those two
columns. There is no reason to run it, and on a live database it would
throw away generated thumbnail references.

---

## Checks after deploying

```bash
flask db current      # should print: e833b45cc876 (head)
flask db heads        # should print exactly one head
```

Then in the browser:

- `/gallery/` — tiles render, filters and sort work
- `/tasks/` — board fills the window, cards drag between columns
- open a task — the side panel opens over the board
- upload an image to a task — its thumbnail appears in the gallery

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
