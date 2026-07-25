import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Debug (auto-reload + the interactive Werkzeug debugger, which can
    # execute arbitrary code) is opt-in via env, so it can never be left on
    # by accident. Production serves through gunicorn (wsgi:app) and never
    # runs this file; for local dev set FLASK_DEBUG=1 in your .env.
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    # threaded=True so a request handler can make a blocking HTTP call back
    # to this same dev server (the Meta Graph emulator does exactly that);
    # without it the single-threaded dev server would deadlock. Production
    # runs under gunicorn (multiple workers), where this doesn't apply.
    app.run(debug=debug, threaded=True)
