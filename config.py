import os
from datetime import timedelta
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
    # Default ON, and opt OUT for local development - the reverse of how this
    # started. Defaulting to False meant a production environment that simply
    # did not mention these variables shipped session cookies over plain HTTP,
    # with nothing in the logs or the boot output to say so. A missing setting
    # should cost a developer an inconvenience on localhost, not cost
    # production its session security in silence.
    SESSION_COOKIE_SECURE = (
        os.getenv(
            "SESSION_COOKIE_SECURE",
            "True",
        ).lower()
        == "true"
    )
    REMEMBER_COOKIE_SECURE = (
        os.getenv(
            "REMEMBER_COOKIE_SECURE",
            "True",
        ).lower()
        == "true"
    )

    # Upper bound on a signed-in session. Sessions are deliberately NOT
    # permanent (the cookie dies when the browser closes, which is the
    # stricter default), so this is the cap that applies if anything ever
    # sets session.permanent - and the duration of a remember-me cookie.
    #
    # It is not what stops a stolen session: that is User.get_id, which binds
    # the session identity to the current password hash so changing a password
    # ends every other open session immediately rather than at expiry.
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    REMEMBER_COOKIE_DURATION = timedelta(hours=12)

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

    # ------------------------------------------------------------------
    # Attendance (Zoho People bridge + idle-task alerts)
    # ------------------------------------------------------------------
    # Master feature flag, same contract as SOCIAL_ENGINE_ENABLED / TEAMS:
    # OFF by default. With it off the attendance blueprint is never
    # registered, the top-bar check-in widget never renders and the
    # background worker never starts - the app behaves exactly as before.
    ATTENDANCE_ENABLED = (
        os.getenv("ATTENDANCE_ENABLED", "False").lower() == "true"
    )

    # Zoho People OAuth app (a single org-level connection an admin
    # authorises once). The refresh token is stored Fernet-encrypted via the
    # same vault as the social tokens (SOCIAL_TOKEN_KEY), so that key must be
    # set to connect a real Zoho org. Data-centre picks the API + accounts
    # hosts: com | in | eu | com.au | jp ...
    ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
    ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
    ZOHO_DC = os.getenv("ZOHO_DC", "com")
    ZOHO_ACCOUNTS_BASE_URL = os.getenv(
        "ZOHO_ACCOUNTS_BASE_URL", f"https://accounts.zoho.{ZOHO_DC}")
    ZOHO_PEOPLE_API_BASE_URL = os.getenv(
        "ZOHO_PEOPLE_API_BASE_URL", f"https://people.zoho.{ZOHO_DC}")
    # Scope Zoho grants for reading + writing attendance entries.
    ZOHO_SCOPES = os.getenv("ZOHO_SCOPES", "ZOHOPEOPLE.attendance.all")

    # Simulation mode: a local Zoho People emulator mounted at /mock/zoho,
    # so the whole check-in/out + sync flow is exercisable on localhost with
    # no real Zoho app - mirrors META_EMULATOR. Hard safety rule: FORCE-
    # DISABLED the moment real Zoho credentials (ZOHO_CLIENT_ID) are present,
    # so a stray ZOHO_SIMULATION_MODE=true can never intercept prod traffic.
    ZOHO_SIMULATION_MODE = (
        os.getenv("ZOHO_SIMULATION_MODE", "True").lower() == "true"
        and not os.getenv("ZOHO_CLIENT_ID")
    )

    # Shared secret for the cron-triggered /internal/attendance/* endpoints
    # (poll sync + idle-alerts). Same fail-closed contract as REMINDER_TOKEN:
    # unset => the endpoints stay closed (403).
    ZOHO_SYNC_TOKEN = os.getenv("ZOHO_SYNC_TOKEN")
    # Verifies the inbound Zoho People automation webhook. Unset => the
    # webhook endpoint rejects everything (403), and polling remains the
    # guaranteed path.
    ZOHO_WEBHOOK_SECRET = os.getenv("ZOHO_WEBHOOK_SECRET")

    # Idle-task alert tuning. An employee who is checked in but has no task
    # In Progress is nudged every REPEAT minutes (after a GRACE window from
    # check-in), and their manager is looped in after ESCALATE_AFTER repeats.
    ATTENDANCE_IDLE_GRACE_MIN = int(
        os.getenv("ATTENDANCE_IDLE_GRACE_MIN", "15"))
    ATTENDANCE_IDLE_REPEAT_MIN = int(
        os.getenv("ATTENDANCE_IDLE_REPEAT_MIN", "10"))
    ATTENDANCE_ESCALATE_AFTER = int(
        os.getenv("ATTENDANCE_ESCALATE_AFTER", "3"))
    # How long a "Snooze" suppresses the idle alert, in minutes.
    ATTENDANCE_SNOOZE_MIN = int(os.getenv("ATTENDANCE_SNOOZE_MIN", "15"))

    # In-process background worker: a daemon thread that periodically pulls
    # attendance from Zoho (SYNC_INTERVAL) and runs the idle-task check
    # (IDLE_INTERVAL), so nothing needs an external cron in dev. Idempotent +
    # row-locked, so it is safe in every gunicorn worker and alongside the
    # /internal/attendance/* cron endpoints. Off in tests.
    ATTENDANCE_INPROCESS_WORKER = (
        os.getenv("ATTENDANCE_INPROCESS_WORKER", "True").lower() == "true"
    )
    ATTENDANCE_SYNC_INTERVAL = int(
        os.getenv("ATTENDANCE_SYNC_INTERVAL", "120"))
    ATTENDANCE_IDLE_INTERVAL = int(
        os.getenv("ATTENDANCE_IDLE_INTERVAL", "600"))

    # ------------------------------------------------------------------
    # AI Assist (provider-agnostic: caption + alt-text now, media QA next)
    # ------------------------------------------------------------------
    # Master feature flag, same contract as SOCIAL_ENGINE_ENABLED / TEAMS /
    # ATTENDANCE: OFF by default. With it off, no AI route is registered and
    # the composer shows no "Generate" button - the app behaves exactly as
    # before. Turn on per-environment once a provider key is set.
    AI_ENABLED = os.getenv("AI_ENABLED", "False").lower() == "true"

    # Which backend the AI layer talks to. The app is provider-agnostic (an
    # adapter per backend, like the social providers), so this + the per-task
    # model strings are the only switch needed to move between Gemini / OpenAI
    # / Claude. Default Gemini: cheapest capable vision + a real free tier.
    AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()

    # Per-task provider + model. Captions/alt-text can run on a different
    # backend from media QA (e.g. cheap Gemini Flash for captions, a stronger
    # model for QA). These are the DEFAULTS; the admin AI-settings screen can
    # override them at runtime without a restart (env stays the fallback). Set
    # the CURRENT model id for your provider - these are sensible defaults, not
    # pinned guarantees. Provider falls back to AI_PROVIDER when unset.
    AI_CAPTION_PROVIDER = os.getenv("AI_CAPTION_PROVIDER", AI_PROVIDER).lower()
    AI_QA_PROVIDER = os.getenv("AI_QA_PROVIDER", AI_PROVIDER).lower()
    # "-latest" aliases track the current model, so a Google deprecation (e.g.
    # gemini-2.5-* being retired) never 404s a fresh deploy. Override per-task
    # from the admin AI screen or these env vars for a pinned/stronger model.
    AI_CAPTION_MODEL = os.getenv("AI_CAPTION_MODEL", "gemini-flash-latest")
    AI_QA_MODEL = os.getenv("AI_QA_MODEL", "gemini-pro-latest")

    # Provider API keys. Only the one for AI_PROVIDER is needed. Like every
    # other secret here they are read at call time and NEVER logged.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

    # Output ceiling per AI call (a caption/checklist is small), request
    # timeout, and a hard cap on how large a media file we send to the model
    # (protects cost + latency; oversized media is skipped with a clear note).
    # Headroom so a "thinking" model (Gemini 3.x etc.) doesn't spend the whole
    # budget reasoning and truncate the JSON body -> "unreadable" responses.
    AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "2048"))
    # Vision calls (caption/QA with an image) can take longer than plain text,
    # so give the provider room; still well under the gunicorn worker timeout.
    AI_TIMEOUT_S = int(os.getenv("AI_TIMEOUT_S", "60"))
    AI_MEDIA_MAX_MB = int(os.getenv("AI_MEDIA_MAX_MB", "10"))

    # Simulation mode: the AI layer returns scripted captions/alt-text/findings
    # instead of calling a real provider, so the whole flow is exercisable on
    # localhost and in tests with no key and no network - mirrors META_EMULATOR
    # / ZOHO_SIMULATION_MODE. Hard safety rule: FORCE-DISABLED the moment any
    # real provider key is present, so a stray flag can never intercept a real
    # deployment into returning canned output.
    AI_SIMULATION_MODE = (
        os.getenv("AI_SIMULATION_MODE", "True").lower() == "true"
        and not (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
        )
    )

    # ------------------------------------------------------------------
    # Google Business Profile - Reviews (AI reply inbox + guarded auto-reply)
    # ------------------------------------------------------------------
    # Master flag, same contract as the other modules: OFF by default. With it
    # off no reviews route is registered and nothing syncs.
    GBP_REVIEWS_ENABLED = (
        os.getenv("GBP_REVIEWS_ENABLED", "False").lower() == "true")

    # Simulation: reviews come from a local scripted source and replies are
    # accepted into it (no real API), so the whole inbox + auto-reply flow is
    # testable now. Flip to false ONCE the Business Profile Reviews API is
    # enabled for the Google app and the real client is wired - separate from
    # the post-publishing access, so it is NOT auto-derived from GOOGLE_CLIENT_ID.
    GBP_REVIEWS_SIMULATION_MODE = (
        os.getenv("GBP_REVIEWS_SIMULATION_MODE", "True").lower() == "true")

    # Auto-reply is a SEPARATE, opt-in switch on top of the reply inbox, and a
    # global kill-switch: even a per-client toggle does nothing while this is
    # off. Off by default - public review replies are reputation-critical.
    GBP_AUTOREPLY_ENABLED = (
        os.getenv("GBP_AUTOREPLY_ENABLED", "False").lower() == "true")

    # Guardrails for what may be auto-replied (everything else -> human queue):
    # only high ratings, only short/no-text reviews, never if the text hits the
    # blocklist, and a hard per-run cap so a sync can't fire a flood.
    GBP_AUTOREPLY_MIN_RATING = int(
        os.getenv("GBP_AUTOREPLY_MIN_RATING", "4"))
    GBP_AUTOREPLY_MAX_TEXT_LEN = int(
        os.getenv("GBP_AUTOREPLY_MAX_TEXT_LEN", "200"))
    GBP_AUTOREPLY_MAX_PER_RUN = int(
        os.getenv("GBP_AUTOREPLY_MAX_PER_RUN", "10"))
    # Comma-separated words that force a review to the human queue - complaints,
    # legal, and health/compliance-sensitive terms that must never be answered
    # by an unattended bot.
    GBP_AUTOREPLY_BLOCKLIST = os.getenv(
        "GBP_AUTOREPLY_BLOCKLIST",
        "refund,lawyer,legal,sue,court,scam,fraud,complaint,worst,terrible,"
        "rude,cheat,hospital,doctor,patient,injury,medical,clinic,lawsuit")
