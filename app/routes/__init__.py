"""路由蓝图注册。"""
from .api import bp as api_bp
from .backup import bp as backup_bp
from .files import bp as files_bp
from .library import bp as library_bp
from .logs import bp as logs_bp
from .pages import bp as pages_bp
from .review import bp as review_bp
from .tape import bp as tape_bp


def register_blueprints(app):
    app.register_blueprint(library_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(tape_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(backup_bp)
