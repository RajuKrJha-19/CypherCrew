import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect


db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()

# Rate limiting. No default limits - only routes that explicitly opt in
# (currently login + forgot-password) are throttled, so normal app usage
# is never affected. In-memory storage is per-worker but fine for the
# login use-case; set RATELIMIT_STORAGE_URI to a Redis URL to share limits
# across workers if one is ever added.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    strategy="fixed-window",
)

login_manager.login_view = "auth.login"