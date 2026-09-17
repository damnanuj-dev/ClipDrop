import os
import sys

# Add project root to sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import app as flask_app

class VercelPathMiddleware:
    """
    WSGI Middleware to restore original request paths on Vercel Serverless.
    When vercel.json rewrites incoming paths to /api/index, PATH_INFO becomes /api/index.
    This middleware restores the real requested path from x-matched-path or x-forwarded-uri,
    ensuring routes like /, /health, /api/info, and /api/download match properly.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        matched = environ.get('HTTP_X_MATCHED_PATH') or environ.get('HTTP_X_FORWARDED_URI')
        if matched:
            if '?' in matched:
                matched = matched.split('?', 1)[0]
            environ['PATH_INFO'] = matched
        elif environ.get('PATH_INFO') in ('/api/index', '/api', '/api/'):
            environ['PATH_INFO'] = '/'
        return self.wsgi_app(environ, start_response)

flask_app.wsgi_app = VercelPathMiddleware(flask_app.wsgi_app)
app = flask_app
