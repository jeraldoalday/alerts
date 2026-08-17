"""Runs `DashboardApp` behind the stdlib WSGI reference server.

No async runtime or extra dependency needed: the dashboard is read-only and
low-traffic (an operator glancing at monitor status), so a blocking
single-process server is enough.
"""
from __future__ import annotations

import logging
from wsgiref.simple_server import make_server

from alerts.db import Database
from alerts.web.app import DashboardApp

logger = logging.getLogger(__name__)


def serve(database: Database, host: str, port: int) -> None:
    app = DashboardApp(database)
    httpd = make_server(host, port, app)
    logger.info("dashboard listening on http://%s:%s", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
