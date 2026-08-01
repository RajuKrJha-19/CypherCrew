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

#: Deliberately left at Flask-Login's default ("basic") rather than "strong".
#: Strong protection binds the session to a hash of the User-Agent plus
#: request.remote_addr - and this app runs behind Cloudflare with no ProxyFix,
#: so remote_addr is whichever Cloudflare edge node forwarded the request and
#: varies between requests from one user. Under "strong" that reads as a
#: hijacked session and signs people out at random.
#:
#: What actually stops a stolen session here is User.get_id, which binds the
#: session identity to the current password hash - so changing a password ends
#: every other open session, without depending on the client's address.