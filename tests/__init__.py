"""Shared helpers for the BluePeak TAG Master test suite.

Each test gets its own throwaway SQLite database and a tiny fake HTTP request
object so the API handler functions can be called directly, with no server.
"""
import importlib
import os
import shutil
import tempfile
import unittest


def load_app(tmpdir):
    """Import bluepeak_tag_master with its DB rooted in a temp folder."""
    import bluepeak_tag_master as app
    importlib.reload(app)
    app.DB_PATH = os.path.join(tmpdir, "test.db")
    app.LOG_PATH = os.path.join(tmpdir, "test.log")
    app.BASE_DIR = tmpdir
    app._fail.clear()
    return app


class FakeRequest:
    """Stands in for the BaseHTTPRequestHandler the API handlers receive."""

    def __init__(self, body=None, user=None):
        self._body = body if body is not None else {}
        self.user = user
        self._cookie = None

    def body(self):
        return self._body


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bp-tag-test-")
        self.app = load_app(self.tmp)
        self.app.init_db()
        self.con = self.app.connect()

    def tearDown(self):
        try:
            self.con.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def admin(self):
        return dict(self.con.execute("SELECT * FROM users WHERE role='admin'").fetchone())

    def cat_id(self, name=None):
        if name:
            row = self.con.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        else:
            row = self.con.execute("SELECT id FROM categories ORDER BY id").fetchone()
        return row[0]

    def make_device(self, **fields):
        f = {"category_id": self.cat_id(), "serial_number": "SER-1"}
        f.update(fields)
        return self.app.api_device_create(FakeRequest(f, self.admin()), self.con, {})

    def dev_id(self, tag=None):
        if tag:
            return self.con.execute("SELECT id FROM devices WHERE tag=?", (tag,)).fetchone()[0]
        return self.con.execute("SELECT id FROM devices ORDER BY id").fetchone()[0]
