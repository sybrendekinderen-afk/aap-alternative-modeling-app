import sys
import os

# In the Lambda bundle, app.py is co-located with server.py at /var/task/.
# For local dev (netlify dev), app.py is two levels up at the project root.
# Add both directories so the import works in either environment.
_here = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_here, "..", ".."))
for _p in (_here, _project_root):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import awsgi
from app import app


def handler(event, context):
    return awsgi.response(app, event, context, base64_content_types={"image/png"})
