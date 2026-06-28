import sys
import os

# Make the project root importable so we can import the Flask app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import awsgi
from app import app


def handler(event, context):
    return awsgi.response(app, event, context, base64_content_types={"image/png"})
