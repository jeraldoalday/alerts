"""Percent-encoding of monitor names in dashboard links and routes.

Monitor names allow anything but spaces and slashes, so they can carry
characters that don't survive a URL round-trip untouched. These cover both
ends of that trip: the link the index page renders, and the path the app
resolves back to a name.
"""
import threading
import urllib.request
from html import escape
from urllib.parse import quote, unquote
from wsgiref.simple_server import make_server

import pytest

from alerts.models import Monitor, MonitorType
from alerts.web import render
from alerts.web.app import DashboardApp


def _add_monitor(db, name: str) -> None:
    with db.session() as session:
        session.add(Monitor(name=name, type=MonitorType.HTTP, target="https://example.com"))


def _get(app, url_path: str):
    """Call the app with the PATH_INFO a real WSGI server would hand it.

    `wsgiref` percent-decodes the request target as latin-1 before putting it
    in the environ, so a test that passes an encoded path straight through is
    exercising a request no server ever makes -- and would have hidden the
    double-decoding the app used to do on top of it.
    """
    status = []
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": unquote(url_path, "iso-8859-1")}
    body = b"".join(app(environ, lambda s, h: status.append(s)))
    return status[0], body.decode("utf-8")


def _get_raw(app, path_info: str):
    """Call the app with PATH_INFO verbatim, for paths no encoder produced."""
    status = []
    body = b"".join(
        app({"REQUEST_METHOD": "GET", "PATH_INFO": path_info}, lambda s, h: status.append(s))
    )
    return status[0], body.decode("utf-8")


@pytest.mark.parametrize(
    ("name", "expected_href"),
    [
        ("plain", "/monitors/plain"),
        ("café-api", "/monitors/caf%C3%A9-api"),
        ("api?v=2", "/monitors/api%3Fv%3D2"),
        ("release#1", "/monitors/release%231"),
        ("a&b", "/monitors/a%26b"),
    ],
)
def test_index_links_are_percent_encoded(db, name, expected_href):
    _add_monitor(db, name)

    _, body = _get(DashboardApp(db), "/")

    assert f'href="{expected_href}"' in body


def test_ampersand_in_a_name_is_still_html_escaped_in_the_link_text(db):
    _add_monitor(db, "a&b")

    _, body = _get(DashboardApp(db), "/")

    assert "a&amp;b</a>" in body


def test_url_path_helper_encodes_slashes_too():
    """Names can't contain slashes today, but the encoder shouldn't rely on that."""
    assert render._url_path("a/b") == "a%2Fb"


def test_detail_route_decodes_a_percent_encoded_name(db):
    _add_monitor(db, "api?v=2")

    status, body = _get(DashboardApp(db), "/monitors/api%3Fv%3D2")

    assert status == "200 OK"
    assert "api?v=2" in body


def test_api_detail_route_decodes_a_percent_encoded_name(db):
    _add_monitor(db, "api?v=2")

    status, body = _get(DashboardApp(db), "/api/monitors/api%3Fv%3D2")

    assert status == "200 OK"
    assert '"name": "api?v=2"' in body


def test_latin1_transported_path_is_decoded_as_utf8(db):
    """wsgiref hands over PATH_INFO as bytes-in-a-str, per PEP 3333."""
    _add_monitor(db, "café-api")

    status, _ = _get_raw(
        DashboardApp(db), "/monitors/café-api".encode("utf-8").decode("latin-1")
    )

    assert status == "200 OK"


def test_undecodable_path_404s_instead_of_raising(db):
    status, _ = _get_raw(DashboardApp(db), "/monitors/\udcff")

    assert status == "404 Not Found"


def test_non_ascii_monitor_link_round_trips_through_a_real_server(tmp_path):
    """End to end: whatever the index renders must resolve when requested back."""
    from alerts.db import Database

    database = Database(f"sqlite:///{tmp_path / 'dashboard.db'}")
    database.create_all()
    _add_monitor(database, "café-api")

    httpd = make_server("127.0.0.1", 0, DashboardApp(database))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        index = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
        href = index.split('href="')[1].split('"')[0]
        assert href == "/monitors/caf%C3%A9-api"

        detail = urllib.request.urlopen(f"http://127.0.0.1:{port}{href}")
        assert detail.status == 200
        assert "café-api" in detail.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@pytest.mark.parametrize("name", ["cache%2Fhit", "api%20v2", "100%-uptime"])
def test_a_percent_sign_in_a_name_is_not_decoded_twice(db, name):
    """The escapes in a name are the name's, not the URL's.

    A path arrives already decoded once, by the server. Decoding it again
    turns `%2F` -- three literal characters of a monitor's name -- into a
    slash, and the lookup fails on a monitor that exists.
    """
    _add_monitor(db, name)

    status, body = _get(DashboardApp(db), f"/monitors/{quote(name, safe='')}")

    assert status == "200 OK"
    assert escape(name) in body


def test_api_detail_does_not_decode_a_percent_sign_twice(db):
    _add_monitor(db, "cache%2Fhit")

    status, body = _get(DashboardApp(db), "/api/monitors/cache%252Fhit")

    assert status == "200 OK"
    assert '"name": "cache%2Fhit"' in body


def test_a_percent_encoded_name_round_trips_through_a_real_server(tmp_path):
    """End to end, on the name that used to break: link out, resolve back."""
    from alerts.db import Database

    database = Database(f"sqlite:///{tmp_path / 'dashboard.db'}")
    database.create_all()
    _add_monitor(database, "cache%2Fhit")

    httpd = make_server("127.0.0.1", 0, DashboardApp(database))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        index = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
        href = index.split('href="')[1].split('"')[0]
        assert href == "/monitors/cache%252Fhit"

        detail = urllib.request.urlopen(f"http://127.0.0.1:{port}{href}")
        assert detail.status == 200
        assert "cache%2Fhit" in detail.read().decode("utf-8")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
