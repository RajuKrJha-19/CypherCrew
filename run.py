import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Debug (auto-reload + the interactive Werkzeug debugger, which can
    # execute arbitrary code) is opt-in via env, so it can never be left on
    # by accident. Production serves through gunicorn (wsgi:app) and never
    # runs this file; for local dev set FLASK_DEBUG=1 in your .env.
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(debug=debug)
