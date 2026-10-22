"""Regression tests for BluePeak TAG Master.

Run with:  python -m unittest discover -s tests
"""
import base64
import json
import os
import re
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import AppTestCase, FakeRequest  # noqa: E402


class SchemaTests(AppTestCase):
    def test_user_version_is_set(self):
        v = self.con.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(v, self.app.SCHEMA_VERSION)

    def test_migration_is_idempotent(self):
        # Re-running init_db must not raise and must keep the version.
        self.app.init_db()
        v = self.con.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(v, self.app.SCHEMA_VERSION)

    def test_serial_lower_column_exists(self):
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(devices)")]
        self.assertIn("serial_lower", cols)


class TagTests(AppTestCase):
    def test_build_tag(self):
        st = dict(self.app.DEFAULT_SETTINGS)
        self.assertEqual(self.app.build_tag(st, "CHB", 2026, 7), "GNET-CHB-2026-0007")

    def test_allocate_is_unique(self):
        st = dict(self.app.DEFAULT_SETTINGS)
        with self.app.tx(self.con):
            tags = self.app.allocate(self.con, st, "CHB", 2026, 5)
        self.assertEqual(len(set(tags)), 5)
        self.assertEqual(tags[0], "GNET-CHB-2026-0001")
        self.assertEqual(tags[-1], "GNET-CHB-2026-0005")


class SerialTests(AppTestCase):
    def _make_device(self, serial):
        body = {"category_id": 1, "serial_number": serial, "brand": "X"}
        return self.app.api_device_create(FakeRequest(body, self.admin()), self.con, {})

    def test_serial_lower_populated(self):
        self._make_device("AbC-123")
        row = self.con.execute("SELECT serial_lower FROM devices").fetchone()
        self.assertEqual(row["serial_lower"], "abc-123")

    def test_duplicate_serial_rejected_case_insensitive(self):
        self._make_device("SN-1")
        with self.assertRaises(self.app.ApiError) as ctx:
            self._make_device("sn-1")
        self.assertEqual(ctx.exception.status, 409)

    def test_duplicate_serial_uses_index(self):
        self._make_device("SN-2")
        plan = self.con.execute(
            "EXPLAIN QUERY PLAN SELECT tag FROM devices WHERE serial_lower=? AND id<>?",
            ("sn-2", -1)).fetchall()
        text = " ".join(str(tuple(p)) for p in plan)
        self.assertIn("idx_dev_serial_lower", text)


class BulkTests(AppTestCase):
    def test_bulk_delete_stale_ids_is_400(self):
        # No devices exist; requesting a bulk delete of a non-existent id must
        # return a clean 400, not crash with an IndexError (finding A1).
        body = {"action": "delete", "ids": [9999]}
        with self.assertRaises(self.app.ApiError) as ctx:
            self.app.api_devices_bulk(FakeRequest(body, self.admin()), self.con, {})
        self.assertEqual(ctx.exception.status, 400)

    def test_bulk_status_updates(self):
        self.app.api_device_create(FakeRequest({"category_id": 1}, self.admin()), self.con, {})
        did = self.con.execute("SELECT id FROM devices").fetchone()[0]
        body = {"action": "status", "ids": [did], "value": "In repair"}
        res = self.app.api_devices_bulk(FakeRequest(body, self.admin()), self.con, {})
        self.assertTrue(res["ok"])
        status = self.con.execute("SELECT status FROM devices WHERE id=?", (did,)).fetchone()[0]
        self.assertEqual(status, "In repair")


class MaskUrlTests(AppTestCase):
    def test_masks_apikey_and_token(self):
        self.assertEqual(self.app.mask_url("https://x?apikey=SECRET"), "https://x?apikey=***")
        self.assertEqual(self.app.mask_url("https://x?token=SECRET&z=1"), "https://x?token=***&z=1")
        self.assertEqual(self.app.mask_url("sqlitecloud://h/db?apikey=abc&token=def"),
                         "sqlitecloud://h/db?apikey=***&token=***")


class CloudSettingsTests(AppTestCase):
    def test_save_with_key_persists(self):
        body = {"enabled": True, "url": "https://proj.gateway.sqlite.cloud",
                "apikey": "SECRET", "database": "bluepeak.sqlite", "auto": "300", "start": True}
        res = self.app.api_cloud_put(FakeRequest(body, self.admin()), self.con, {})
        self.assertTrue(res["has_key"])
        self.assertTrue(res["enabled"])
        self.assertEqual(res["auto"], 300)
        st = self.app.get_settings(self.con)
        self.assertEqual(st["cloud_url"], "https://proj.gateway.sqlite.cloud")
        self.assertEqual(st["cloud_apikey"], "SECRET")

    def test_save_without_key_keeps_existing(self):
        first = {"enabled": True, "url": "https://x.gateway.sqlite.cloud", "apikey": "KEEP",
                 "database": "db.sqlite", "auto": "0", "start": False}
        self.app.api_cloud_put(FakeRequest(first, self.admin()), self.con, {})
        second = {"enabled": False, "url": "https://x.gateway.sqlite.cloud", "apikey": "",
                  "database": "db.sqlite", "auto": "0", "start": False}
        self.app.api_cloud_put(FakeRequest(second, self.admin()), self.con, {})
        st = self.app.get_settings(self.con)
        self.assertEqual(st["cloud_apikey"], "KEEP")
        self.assertEqual(st["cloud_enabled"], "0")

    def test_bad_url_rejected(self):
        body = {"url": "ftp://nope", "database": "db.sqlite"}
        with self.assertRaises(self.app.ApiError) as ctx:
            self.app.api_cloud_put(FakeRequest(body, self.admin()), self.con, {})
        self.assertEqual(ctx.exception.status, 400)


class CloudPushTests(AppTestCase):
    def _fake_cloud(self):
        import bluepeak_cloud as cloud
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        for (ddl,) in self.con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"):
            con.execute(ddl)
        for table in cloud.SYNC_TABLES:
            cur = self.con.execute('SELECT * FROM "%s"' % table)
            cols = [d[0] for d in cur.description]
            con.executemany(
                'INSERT INTO "%s" (%s) VALUES (%s)' % (
                    table, ", ".join('"%s"' % c for c in cols),
                    ", ".join("?" * len(cols))),
                [tuple(r) for r in cur.fetchall()])
        # A child row already in the cloud that the local copy does not have.
        con.execute("INSERT INTO devices(tag,category_id,serial_lower) VALUES(?,?,?)",
                    ("STALE-1", 1, "stale-1"))
        con.commit()

        def request(c, method, url, payload=None):
            cur = con.execute(payload["sql"])
            names = [d[0] for d in cur.description] if cur.description else []
            data = [list(r) for r in cur.fetchall()]
            out = {"data": data,
                   "metadata": {"columns": [{"name": n} for n in names]}}
            return 200, json.dumps(out).encode("utf-8")

        return cloud, con, request

    def test_push_clears_children_before_parents(self):
        cloud, con, request = self._fake_cloud()
        cfg = {"sql": "http://fake", "v2": "http://fake", "key": "k", "db": "d"}
        with mock.patch.object(cloud, "_normalise", return_value=cfg), \
                mock.patch.object(cloud, "_request", side_effect=request):
            res = cloud.push(cfg, self.app.DB_PATH)
        self.assertIn("categories", res["tables"])
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM devices WHERE tag='STALE-1'").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
                         self.con.execute("SELECT COUNT(*) FROM categories").fetchone()[0])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                         self.con.execute("SELECT COUNT(*) FROM users").fetchone()[0])


class UpdateTests(AppTestCase):
    def test_version_comparison(self):
        self.assertLess(self.app._version_key("1.3.9"), self.app._version_key("1.4.0"))
        self.assertEqual(self.app._version_key("1.4"), (1, 4))
        self.assertGreater(self.app._version_key("2.0.0"), self.app._version_key("1.99.99"))

    def test_remote_http_feed_is_rejected(self):
        body = {"url": "http://example.com/update.json"}
        with self.assertRaises(self.app.ApiError) as ctx:
            self.app.api_update_check(FakeRequest(body, self.admin()), self.con, {})
        self.assertEqual(ctx.exception.status, 400)

    def test_check_reports_newer_version(self):
        manifest = {"version": "1.5.0", "url": "https://example.com/setup.exe",
                    "sha256": "a" * 64, "notes": "Fixes and polish"}
        body = {"url": "https://example.com/update.json"}
        with mock.patch.object(self.app, "_fetch_update_manifest", return_value=manifest):
            res = self.app.api_update_check(FakeRequest(body, self.admin()), self.con, {})
        self.assertTrue(res["available"])
        self.assertEqual(res["latest"], "1.5.0")
        self.assertEqual(res["version"], self.app.APP_VERSION)
        st = self.app.get_settings(self.con)
        self.assertEqual(st["update_url"], "https://example.com/update.json")

    def test_install_rejects_unsigned_manifest(self):
        manifest = {"version": "1.5.0", "url": "https://example.com/setup.exe",
                    "sha256": "", "notes": ""}
        with mock.patch.object(self.app, "_fetch_update_manifest", return_value=manifest):
            with self.assertRaises(self.app.ApiError):
                self.app.api_update_install(FakeRequest({}, self.admin()), self.con, {})


class AuditPagingTests(AppTestCase):
    def _seed(self, n):
        with self.app.tx(self.con):
            for i in range(n):
                self.con.execute(
                    "INSERT INTO activity(tag,action,details) VALUES(?,?,?)",
                    ("T%03d" % i, "Updated", "row %d" % i))

    def test_offset_returns_distinct_rows(self):
        self._seed(30)
        page1 = self.app.api_audit(FakeRequest(user=self.admin()), self.con,
                                   {"limit": ["10"], "order": ["asc"]})
        page2 = self.app.api_audit(FakeRequest(user=self.admin()), self.con,
                                   {"limit": ["10"], "offset": ["10"], "order": ["asc"]})
        ids1 = {r["id"] for r in page1["rows"]}
        ids2 = {r["id"] for r in page2["rows"]}
        self.assertEqual(len(ids1), 10)
        self.assertEqual(len(ids2), 10)
        self.assertFalse(ids1 & ids2, "pages must not overlap")

    def test_total_and_offset(self):
        self._seed(25)
        res = self.app.api_audit(FakeRequest(user=self.admin()), self.con,
                                 {"limit": ["10"], "offset": ["20"], "order": ["asc"]})
        self.assertEqual(res["total"], 25)
        self.assertEqual(len(res["rows"]), 5)
        self.assertEqual(res["offset"], 20)


class ImportTests(AppTestCase):
    def test_import_bounds(self):
        rows = [{"category": "Chromebook", "serial_number": "S%d" % i} for i in range(3)]
        res = self.app.api_import(FakeRequest({"rows": rows}, self.admin()), self.con, {})
        self.assertEqual(res["imported"], 3)
        self.assertEqual(res["skipped_count"], 0)

    def test_import_reports_bad_rows(self):
        rows = [{"category": "Chromebook", "serial_number": "A1"},
                {"category": "Nope", "serial_number": "A2"}]
        res = self.app.api_import(FakeRequest({"rows": rows}, self.admin()), self.con, {})
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["skipped_count"], 1)

    def test_import_create_only_skips_existing_tag(self):
        self.app.api_import(FakeRequest({"rows": [{"category": "Chromebook", "tag": "GNET-CHB-2026-0001"}]},
                                        self.admin()), self.con, {})
        again = self.app.api_import(
            FakeRequest({"rows": [{"category": "Chromebook", "tag": "GNET-CHB-2026-0001"}]}, self.admin()),
            self.con, {})
        self.assertEqual(again["imported"], 0)
        self.assertEqual(again["skipped_count"], 1)

    def test_import_update_mode_merges(self):
        self.app.api_import(FakeRequest({"rows": [{"category": "Chromebook", "tag": "T-1",
                                                   "location": "Lab 1"}]}, self.admin()), self.con, {})
        res = self.app.api_import(
            FakeRequest({"mode": "update", "rows": [{"category": "Chromebook", "tag": "T-1",
                                                     "location": "Lab 2"}]}, self.admin()), self.con, {})
        self.assertEqual(res["updated"], 1)
        loc = self.con.execute("SELECT location FROM devices WHERE tag='T-1'").fetchone()[0]
        self.assertEqual(loc, "Lab 2")

    def test_import_row_cap(self):
        rows = [{"category": "Chromebook"}] * (self.app.IMPORT_MAX_ROWS + 1)
        with self.assertRaises(self.app.ApiError) as ctx:
            self.app.api_import(FakeRequest({"rows": rows}, self.admin()), self.con, {})
        self.assertEqual(ctx.exception.status, 400)


class WarrantyTests(AppTestCase):
    def test_warranty_end_saved_and_rejected_when_bad(self):
        d = self.app.api_device_create(
            FakeRequest({"category_id": self.cat_id(), "serial_number": "W1",
                         "warranty_end": "2029-01-31"}, self.admin()), self.con, {})
        got = self.con.execute("SELECT warranty_end FROM devices WHERE id=?", (d["id"],)).fetchone()[0]
        self.assertEqual(got, "2029-01-31")
        with self.assertRaises(self.app.ApiError):
            self.app.api_device_create(
                FakeRequest({"category_id": self.cat_id(), "serial_number": "W2",
                             "warranty_end": "soon"}, self.admin()), self.con, {})

    def test_stats_counts_expired(self):
        self.make_device(serial_number="EXP-1")
        self.con.execute("UPDATE devices SET warranty_end='2000-01-01' WHERE id=?",
                         (self.dev_id(),))
        self.con.commit()
        s = self.app.api_stats(FakeRequest({}, self.admin()), self.con, {})
        self.assertGreaterEqual(s["expired"], 1)

    def test_warranty_filter(self):
        self.make_device(serial_number="EXP-2", warranty_end="2000-01-01")
        self.make_device(serial_number="FINE-1", warranty_end="2099-01-01")
        res = self.app.api_devices(FakeRequest({}, self.admin()), self.con, {"warranty": ["expired"]})
        self.assertEqual(len(res["rows"]), 1)


class BackupTests(AppTestCase):
    def test_scheduled_backup_writes_once_per_day(self):
        self.app.scheduled_backup()
        folder = os.path.join(self.tmp, "backups")
        files = [n for n in os.listdir(folder) if n.startswith("auto-")]
        self.assertEqual(len(files), 1)
        # Second call on the same day must not create another.
        self.app.scheduled_backup()
        files = [n for n in os.listdir(folder) if n.startswith("auto-")]
        self.assertEqual(len(files), 1)
        st = self.app.get_settings(self.con)
        self.assertTrue(st["backup_last"])

    def test_backup_restore_round_trip(self):
        self.make_device(serial_number="KEEP-1", brand="Acer")
        blob = base64.b64encode(self.app.make_backup()).decode()
        self.make_device(serial_number="DROP-1", brand="Dell")
        self.app.api_backup_restore(FakeRequest({"data": blob}, self.admin()), self.app.connect(), {})
        con = self.app.connect()
        try:
            serials = {r[0] for r in con.execute("SELECT serial_number FROM devices")}
        finally:
            con.close()
        self.assertIn("KEEP-1", serials)
        self.assertNotIn("DROP-1", serials)

    def test_backup_restore_rejects_garbage(self):
        blob = base64.b64encode(b"not a database at all" * 40).decode()
        with self.assertRaises(self.app.ApiError) as ctx:
            self.app.api_backup_restore(FakeRequest({"data": blob}, self.admin()), self.con, {})
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
