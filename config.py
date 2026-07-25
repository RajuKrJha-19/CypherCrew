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