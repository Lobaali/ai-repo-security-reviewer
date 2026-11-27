"""Request handlers for the sample app (TEST FIXTURE - never executed)."""
# pyright: reportMissingImports=false
from flask import request

from app.utils import ping


def ping_view():
    host = request.args.get("host")
    return ping(host)