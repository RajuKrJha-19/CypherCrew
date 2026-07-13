from flask import Flask
from config import Config
from app.extensions import (
    db,
    login_manager,
    migrate,
)
from app.seed import seed_database
from app.utils.text_filters import linkify_text

def create_app():

    app = Flask(__name__)

    
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from app import models
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.users import users_bp
    from app.routes.permissions import permissions_bp
    from app.routes.clients import clients_bp
    from app.routes.tasks import tasks_bp 
    from app.routes.notes import notes_bp
    from app.routes.reports import reports_bp
    from app.routes.notifications import notifications_bp
    from app.routes.calendar import calendar_bp
    from app.routes.holidays import holidays_bp
    from app.routes.meetings import meetings_bp
    from app.routes.leaves import leaves_bp


    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(permissions_bp)
    app.register_blueprint(clients_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(calendar_bp)
    app.register_blueprint(holidays_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(leaves_bp)

    with app.app_context():
        if app.config.get("AUTO_SEED", True):
            seed_database()

    from app.utils.permissions import has_permission
    app.jinja_env.globals.update(
        has_permission=has_permission
    )

    app.jinja_env.filters["linkify"] = linkify_text
    
    return app