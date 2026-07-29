import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

class Config:

    # Flask

    SECRET_KEY = os.getenv("SECRET_KEY")

    # Database

    SQLALCHEMY_DATABASE_URI = os.getenv(
    "DATABASE_URL"
)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Connection health. Managed Postgres closes idle connections after a
    # few minutes; without pre-ping the next request grabs a dead socket
    # from the pool and 500s ("server closed the connection"). pool_pre_ping
    # validates a connection before use, pool_recycle retires it well under
    # the provider's idle window, and a per-connection statement_timeout
    # stops a single runaway query from pinning a worker for the full
    # gunicorn timeout. All values are env-overridable so they can be tuned
    # in production without a code change.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", "280")),
        # Sized against gunicorn's threads (see gunicorn.conf.py). With
        # threaded workers several requests share one process's pool, so
        # leaving this at SQLAlchemy's default of 5 would have threads
        # queueing for a connection under exactly the load the threads were
        # added to absorb. pool_size x workers is the connection count the
        # database sees, so it is deliberately modest rather than generous.
        "pool_size": int(os.getenv("DB_POOL_SIZE", "6")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "6")),
        "connect_args": {
            "options": (
                "-c statement_timeout="
                + os.getenv("DB_STATEMENT_TIMEOUT_MS", "30000")
            ),
        },
    }

    # Cap the size of a single request body. Large video *submissions*
    # bypass this entirely - they upload part-by-part straight to R2 via
    # the presigned multipart flow and never stream through the app. This
    # only bounds the direct-upload paths (reference files, the no-JS
    # submission fallback, avatars), so one enormous request can't exhaust
    # the app server's memory/disk. Deliberately generous (2 GB) so it
    # never blocks a real reference file; raise MAX_UPLOAD_MB if needed.
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "2048")) * 1024 * 1024

    # Session & Cookies

    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv(
        "SESSION_COOKIE_SAMESITE",
        "Lax",
    )
    SESSION_COOKIE_SECURE = (
        os.getenv(
            "SESSION_COOKIE_SECURE",
            "False",
        ).lower()
        == "true"
    )
    REMEMBER_COOKIE_SECURE = (
        os.getenv(
            "REMEMBER_COOKIE_SECURE",
            "False",
        ).lower()
        == "true"
    )

    # Seed

    AUTO_SEED = (
        os.getenv(
            "AUTO_SEED",
            "True",
        ).lower()
        == "true"
    )

    # Cloudflare R2

    R2_ACCOUNT_ID = os.getenv(
        "R2_ACCOUNT_ID"
    )
    R2_BUCKET_NAME = os.getenv(
        "R2_BUCKET_NAME"
    )
    R2_ACCESS_KEY_ID = os.getenv(
        "R2_ACCESS_KEY_ID"
    )
    R2_SECRET_ACCESS_KEY = os.getenv(
        "R2_SECRET_ACCESS_KEY"
    )
    R2_ENDPOINT_URL = os.getenv(
        "R2_ENDPOINT_URL"
    )

    # Email (SMTP) - optional. When these are unset the app still runs;
    # password reset and email notifications simply no-op (and log) until
    # credentials are provided. For Gmail use an App Password.
    MAIL_SERVER = os.getenv("MAIL_SERVER")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "True").lower() == "true"
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER")

    # ------------------------------------------------------------------
    # Social Publishing Engine
    # ------------------------------------------------------------------
    # Master feature flag. OFF by default: with it off nothing about the
    # engine is wired into request handling, so the app behaves exactly as
    # before. Turn on per-environment once credentials + the worker cron
    # are in place.
    SOCIAL_ENGINE_ENABLED = (
        os.getenv("SOCIAL_ENGINE_ENABLED", "False").lower() == "true"
    )

    # Token vault key(s) for Fernet encryption of platform access/refresh
    # tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Comma-separate multiple keys to rotate (first = primary/encrypt, the
    # rest still decrypt). MUST live outside the database. If unset, the
    # vault is disabled and no tokens can be stored (connect flow refuses).
    SOCIAL_TOKEN_KEY = os.getenv("SOCIAL_TOKEN_KEY")
    SOCIAL_TOKEN_KEY_VERSION = int(os.getenv("SOCIAL_TOKEN_KEY_VERSION", "1"))

    # Simulation mode: register the SimulationProvider for every platform
    # that does NOT have a real adapter configured. The full compose ->
    # approve -> schedule -> publish -> analytics loop works locally with no
    # external credentials. Real adapters (below) take over their platforms.
    SOCIAL_SIMULATION_MODE = (
        os.getenv("SOCIAL_SIMULATION_MODE", "True").lower() == "true"
    )

    # ---- Meta (Facebook Pages + Instagram) provider ----
    # Real integration goes live once these are set (App Review + Business
    # Verification are prerequisites on Meta's side). The provider code is
    # identical whether it talks to graph.facebook.com or the local Graph
    # emulator - only the base URLs differ.
    META_APP_ID = os.getenv("META_APP_ID")
    META_APP_SECRET = os.getenv("META_APP_SECRET")

    # Server-side media measurement. Optional: with no ffprobe installed
    # the app behaves exactly as it did before, relying on the browser's
    # measurement alone. Installing it (apt install ffmpeg) adds the two
    # things a browser cannot do - reading .mov/HEVC deliverables, and
    # reporting frame rate and codec. See app/social/media/probe.py.
    FFPROBE_PATH = os.getenv("FFPROBE_PATH", "ffprobe")

    # ffmpeg (same package as ffprobe) powers on-publish video downscaling:
    # a client's 2160px-wide reel is resized to Instagram's 1920px max,
    # aspect preserved, instead of being rejected. Optional - with no ffmpeg
    # an oversized video is blocked with a clear "re-export smaller" message.
    # See app/social/media/transcode.py.
    FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

    # --- Google (YouTube + Business Profile) ---------------------------
    # One OAuth client covers both; each adapter registers only when its
    # own switch is on, so one can go live while the other waits for
    # Google's separate API approval.
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    YOUTUBE_ENABLED = os.getenv("YOUTUBE_ENABLED", "true").lower() == "true"
    GOOGLE_BUSINESS_ENABLED = os.getenv(
        "GOOGLE_BUSINESS_ENABLED", "true").lower() == "true"

    # Applied to every uploaded video. 22 is "People & Blogs", the safe
    # default; privacy is public because the engine already gates publishing
    # behind approval, so an upload reaching YouTube is meant to be live.
    YOUTUBE_CATEGORY_ID = os.getenv("YOUTUBE_CATEGORY_ID", "22")
    YOUTUBE_PRIVACY_STATUS = os.getenv("YOUTUBE_PRIVACY_STATUS", "public")
    # Google requires this declaration on every upload. Agency content is
    # not child-directed by default; set true only if it genuinely is.
    YOUTUBE_MADE_FOR_KIDS = os.getenv(
        "YOUTUBE_MADE_FOR_KIDS", "false").lower() == "true"

    # Shown on the public legal pages (/legal/privacy, /legal/terms,
    # /legal/data-deletion), which Meta's app review reads. Configurable so
    # a deployment states its real operator and a contact that is actually
    # monitored - a policy naming the wrong entity is a rejection reason.
    LEGAL_COMPANY_NAME = os.getenv("LEGAL_COMPANY_NAME", "CypherCrew")
    LEGAL_CONTACT_EMAIL = os.getenv(
        "LEGAL_CONTACT_EMAIL", "dev.cypherms@gmail.com")
    LEGAL_LAST_UPDATED = os.getenv("LEGAL_LAST_UPDATED", "28 July 2026")
    META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")
    META_GRAPH_BASE_URL = os.getenv(
        "META_GRAPH_BASE_URL", "https://graph.facebook.com")
    META_OAUTH_BASE_URL = os.getenv(
        "META_OAUTH_BASE_URL", "https://www.facebook.com")

    # Mounts a local Graph API emulator at /mock/graph and points the Meta
    # provider at it, so the entire real code path (OAuth, discovery,
    # container flow, publishing, insights) is browser-testable with no real
    # Meta app.
    #
    # Hard safety rule: the emulator is FORCE-DISABLED whenever real Meta
    # credentials (META_APP_ID) are present. So even if META_EMULATOR=true is
    # left set by accident in production, the mock Graph API can never mount
    # or intercept traffic once you configure a real app.
    META_EMULATOR = (
        os.getenv("META_EMULATOR", "False").lower() == "true"
        and not os.getenv("META_APP_ID")
    )

    # Shared secrets for the cron-triggered internal endpoints that drain
    # the publish queue / run the scheduler / sync analytics / refresh
    # tokens. Same pattern as REMINDER_TOKEN: fail closed when unset.
    SOCIAL_WORKER_TOKEN = os.getenv("SOCIAL_WORKER_TOKEN")

    # Number of jobs a single worker drain claims per run, and the size of
    # the in-process thread pool that runs them (kept small - it runs inside
    # a gunicorn worker, mirroring the thumbnail pool).
    SOCIAL_WORKER_BATCH = int(os.getenv("SOCIAL_WORKER_BATCH", "10"))
    SOCIAL_WORKER_THREADS = int(os.getenv("SOCIAL_WORKER_THREADS", "3"))

    # Grace window (hours) before the media GC deletes an unreferenced
    # social_uploads/ object - long enough that an in-flight composer upload
    # is never mistaken for an orphan.
    SOCIAL_UPLOAD_GC_HOURS = int(os.getenv("SOCIAL_UPLOAD_GC_HOURS", "24"))

    # Public base URL used to build OAuth redirect URIs (e.g.
    # https://crew.cypherms.com). Falls back to the request host when unset.
    SOCIAL_PUBLIC_BASE_URL = os.getenv("SOCIAL_PUBLIC_BASE_URL")

    # In-process background worker. When on (default), a daemon thread inside
    # the app periodically enqueues due scheduled posts and drains the publish
    # queue - so scheduled posts publish AUTOMATICALLY with no external cron.
    # Safe alongside external cron too (the queue is claim-based + idempotent).
    # Turn OFF in tests, or in production if you drive the /internal/social/*
    # endpoints from a real scheduler instead.
    SOCIAL_INPROCESS_WORKER = (
        os.getenv("SOCIAL_INPROCESS_WORKER", "True").lower() == "true"
    )
    # Seconds between background worker ticks.
    SOCIAL_WORKER_INTERVAL = int(os.getenv("SOCIAL_WORKER_INTERVAL", "20"))

    # ------------------------------------------------------------------
    # Cypher-Teams (chat)
    # ------------------------------------------------------------------
    # Master feature flag, same contract as SOCIAL_ENGINE_ENABLED: OFF by
    # default, and with it off the blueprint is never registered, so /teams
    # 404s and the app behaves exactly as it did before the module existed.
    TEAMS_ENABLED = (
        os.getenv("TEAMS_ENABLED", "False").lower() == "true"
    )

    # Adaptive polling cadence (milliseconds) handed to the client. There is
    # no websocket/SSE layer - a chat view polls one endpoint and slows itself
    # down as attention drifts, so an idle tab costs almost nothing. Tunable
    # per environment without a code change if the worker pool gets tight.
    TEAMS_POLL_ACTIVE_MS = int(os.getenv("TEAMS_POLL_ACTIVE_MS", "2000"))
    TEAMS_POLL_HIDDEN_MS = int(os.getenv("TEAMS_POLL_HIDDEN_MS", "15000"))
    TEAMS_POLL_IDLE_MS = int(os.getenv("TEAMS_POLL_IDLE_MS", "30000"))

    # Seconds of silence after which a member is shown as away / offline.
    TEAMS_PRESENCE_ONLINE_SECONDS = int(
        os.getenv("TEAMS_PRESENCE_ONLINE_SECONDS", "60"))
    TEAMS_PRESENCE_AWAY_SECONDS = int(
        os.getenv("TEAMS_PRESENCE_AWAY_SECONDS", "300"))

    # Chat attachments are bounded FAR below MAX_CONTENT_LENGTH (2 GB, sized
    # for reference-file uploads). Without its own cap, one dragged file
    # could push a multi-gigabyte body through a gunicorn worker.
    TEAMS_ATTACHMENT_MAX_MB = int(os.getenv("TEAMS_ATTACHMENT_MAX_MB", "25"))
