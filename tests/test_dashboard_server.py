from alerts.web import server


class _FakeHttpd:
    def __init__(self, host, port, app):
        self.host = host
        self.port = port
        self.app = app
        self.served = False
        self.closed = False

    def serve_forever(self):
        self.served = True

    def server_close(self):
        self.closed = True


def test_serve_builds_app_and_serves_forever(db, monkeypatch):
    created = {}

    def fake_make_server(host, port, app):
        httpd = _FakeHttpd(host, port, app)
        created["httpd"] = httpd
        return httpd

    monkeypatch.setattr(server, "make_server", fake_make_server)

    server.serve(db, "127.0.0.1", 8080)

    httpd = created["httpd"]
    assert httpd.host == "127.0.0.1"
    assert httpd.port == 8080
    assert httpd.served is True
    assert httpd.closed is True


def test_serve_swallows_keyboard_interrupt_and_still_closes(db, monkeypatch):
    class _InterruptingHttpd(_FakeHttpd):
        def serve_forever(self):
            self.served = True
            raise KeyboardInterrupt

    created = {}

    def fake_make_server(host, port, app):
        httpd = _InterruptingHttpd(host, port, app)
        created["httpd"] = httpd
        return httpd

    monkeypatch.setattr(server, "make_server", fake_make_server)

    server.serve(db, "127.0.0.1", 8080)  # should not raise

    assert created["httpd"].closed is True
