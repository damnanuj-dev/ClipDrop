import os
import sys
import urllib.parse

# Add project root to sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import app as flask_app

class VercelPathMiddleware:
    """
    WSGI Middleware to restore original request paths on Vercel Serverless.
    Supports query parameter __path= and Vercel routing headers,
    ensuring routes like /, /health, /api/info, /api/download, and static assets match properly.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        qs = environ.get('QUERY_STRING', '')
        if '__path=' in qs:
            params = urllib.parse.parse_qs(qs)
            if '__path' in params and params['__path']:
                target = params['__path'][0]
                environ['PATH_INFO'] = target
                params.pop('__path', None)
                environ['QUERY_STRING'] = urllib.parse.urlencode(params, doseq=True)
        else:
            matched = environ.get('HTTP_X_MATCHED_PATH') or environ.get('HTTP_X_FORWARDED_URI')
            if matched:
                environ['PATH_INFO'] = matched.split('?')[0]
            elif environ.get('PATH_INFO') in ('/api/index', '/api', '/api/'):
                environ['PATH_INFO'] = '/'
        return self.wsgi_app(environ, start_response)

flask_app.wsgi_app = VercelPathMiddleware(flask_app.wsgi_app)
app = flask_app
