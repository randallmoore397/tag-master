#!/usr/bin/env python3
"""
BluePeak TAG Master
-------------------
A local, single-page web app that generates unique asset TAG codes for ICT
devices and stores every device (and the TAG assigned to it) in SQLite, with
optional two-way synchronisation to a SQLite Cloud database.

Run:    python bluepeak_tag_master.py        (Windows: double-click Start-BluePeak-TAG-Master.bat)
Or:     BluePeak TAG Master.exe              (built executable, no Python needed)
Opens:  http://127.0.0.1:8765

Needs only Python 3.8+. No packages to install. Data lives in
bluepeak_tag_master.db next to this script, and can be pushed to / pulled
from SQLite Cloud from Settings once a connection string is saved there.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile
import zlib
from contextlib import contextmanager
from http.cookies import SimpleCookie
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import bluepeak_cloud

APP_NAME = "BluePeak TAG Master"
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "bluepeak_tag_master.db")
LOG_PATH = os.path.join(BASE_DIR, "bluepeak_tag_master.log")
LOGO_NAME = "TAG Master Pro Logo.png"
_OLD_DB = os.path.join(BASE_DIR, "gnet_tag_master.db")
if os.path.exists(_OLD_DB) and not os.path.exists(DB_PATH):
    for _suffix in ("", "-wal", "-shm"):
        if os.path.exists(_OLD_DB + _suffix):
            os.rename(_OLD_DB + _suffix, DB_PATH + _suffix)
DEFAULT_PORT = 8765
APP_VERSION = "1.4.1"
SCHEMA_VERSION = 4
UPDATE_MAX_BYTES = 250 * 1024 * 1024
UPDATE_CHECK_HOURS = 12

STATUSES = ["In stock", "Assigned", "In repair", "Lost", "Retired"]

DEFAULT_SETTINGS = {
    "prefix": "GNET",
    "sep": "-",
    "pad": "4",
    "include_year": "1",
    "label_footer": "Property of IPREP. Do not remove.",
    "label_size": "md",
    "label_brand": "BluePeak",
    "label_paper": "a4",
    "label_code": "barcode",
    "cloud_enabled": "0",
    "cloud_url": "",
    "cloud_apikey": "",
    "cloud_database": "bluepeak_tag_master.sqlite",
    "cloud_auto": "0",
    "cloud_start": "1",
    "cloud_last_push": "",
    "cloud_last_pull": "",
    "cloud_last_error": "",
    "cloud_last_error_at": "",
    "cloud_fp": "",
    "update_url": "",
    "update_auto": "1",
    "update_last_check": "",
    "update_latest": "",
    "update_notes": "",
    "update_error": "",
}

CLOUD_KEYS = ("cloud_enabled", "cloud_url", "cloud_apikey", "cloud_database", "cloud_auto",
              "cloud_start", "cloud_last_push", "cloud_last_pull", "cloud_last_error",
              "cloud_last_error_at", "cloud_fp")
AUTO_SYNC_CHOICES = ("0", "30", "60", "300", "900")
_LOG_LOCK = threading.Lock()


def write_log(message):
    try:
        line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(message).strip())
        with _LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def resource_path(name):
    for folder in (BASE_DIR, BUNDLE_DIR):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return os.path.join(BASE_DIR, name)


def logo_bytes():
    """The app logo, read from next to the script or from inside the .exe."""
    path = resource_path(LOGO_NAME)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return data if data.startswith(b"\x89PNG") else None

DEFAULT_CATEGORIES = [
    ("Chromebook", "CHB"),
    ("Laptop", "LTP"),
    ("Desktop", "DST"),
    ("Monitor", "MON"),
    ("Router", "RTR"),
    ("Network Switch", "SWT"),
    ("Access Point", "APT"),
    ("Server", "SRV"),
    ("Printer", "PRN"),
    ("Projector", "PRJ"),
    ("Tablet", "TAB"),
    ("CCTV Camera", "CCT"),
    ("Biometric Device", "BIO"),
    ("UPS / Power", "UPS"),
    ("Other", "OTH"),
]

DEVICE_TEXT_FIELDS = [
    ("brand", 80), ("model", 80), ("serial_number", 80), ("mac_address", 40),
    ("assigned_to", 120), ("department", 80), ("location", 80), ("notes", 1000),
]
FIELD_LABELS = {
    "brand": "Brand", "model": "Model", "serial_number": "Serial number",
    "mac_address": "MAC address", "status": "Status", "assigned_to": "Assigned to",
    "department": "Department", "location": "Location",
    "purchase_date": "Purchase date",
    "warranty_end": "Warranty ends", "notes": "Notes",
}


class ApiError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def tx(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def init_db():
    created = False
    con = connect()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS devices(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL UNIQUE,
                category_id INTEGER NOT NULL REFERENCES categories(id),
                brand TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                serial_number TEXT NOT NULL DEFAULT '',
                mac_address TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'In stock',
                assigned_to TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                purchase_date TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE INDEX IF NOT EXISTS idx_dev_cat ON devices(category_id);
            CREATE INDEX IF NOT EXISTS idx_dev_status ON devices(status);
            CREATE INDEX IF NOT EXISTS idx_dev_batch ON devices(batch_id);
            CREATE INDEX IF NOT EXISTS idx_dev_serial ON devices(serial_number);
            CREATE TABLE IF NOT EXISTS counters(
                key TEXT PRIMARY KEY, last INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS activity(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now')),
                device_id INTEGER,
                tag TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS idx_act_device ON activity(device_id);
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                full_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'viewer',
                password_hash TEXT NOT NULL,
                must_change INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_login TEXT);
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL);
            """
        )
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        if con.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            con.executemany("INSERT INTO categories(name,code) VALUES(?,?)", DEFAULT_CATEGORIES)
        _migrate(con)
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO users(username,full_name,role,password_hash,must_change) VALUES(?,?,?,?,1)",
                (DEFAULT_ADMIN[0], "Administrator", "admin", hash_password(DEFAULT_ADMIN[1])))
            created = True
    finally:
        con.close()
    return created


def _column_names(con, table):
    return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]


def _migrate(con):
    """Apply additive schema migrations, tracked with PRAGMA user_version.

    Each migration is idempotent by design (CREATE ... IF NOT EXISTS / column
    checks) so an unversioned database created by an older build is upgraded
    safely on first run.
    """
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    # -- migration 1: activity columns added by the audit-log feature --------
    have = _column_names(con, "activity")
    for col, ddl in (("user", "TEXT NOT NULL DEFAULT ''"), ("role", "TEXT NOT NULL DEFAULT ''"),
                     ("ip", "TEXT NOT NULL DEFAULT ''"), ("entity", "TEXT NOT NULL DEFAULT ''"),
                     ("changes", "TEXT NOT NULL DEFAULT ''"), ("parent_id", "INTEGER"),
                     ("category", "TEXT NOT NULL DEFAULT ''")):
        if col not in have:
            con.execute("ALTER TABLE activity ADD COLUMN %s %s" % (col, ddl))
            if col == "category":
                con.execute(
                    "UPDATE activity SET category=COALESCE((SELECT c.name FROM devices d JOIN categories c "
                    "ON c.id=d.category_id WHERE d.id=activity.device_id),'') WHERE device_id IS NOT NULL")
    con.execute("CREATE INDEX IF NOT EXISTS idx_act_ts ON activity(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_act_cat ON activity(category)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_act_parent ON activity(parent_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_act_user ON activity(user)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_act_action ON activity(action)")

    # -- migration 2: normalised serial column for fast duplicate checks -----
    have_dev = _column_names(con, "devices")
    if "serial_lower" not in have_dev:
        con.execute("ALTER TABLE devices ADD COLUMN serial_lower TEXT NOT NULL DEFAULT ''")
        con.execute("UPDATE devices SET serial_lower=LOWER(serial_number)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_serial_lower ON devices(serial_lower)")
    # NOCASE indexes so sorting and filtering by these columns avoids a scan.
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_brand_nc ON devices(brand COLLATE NOCASE)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_model_nc ON devices(model COLLATE NOCASE)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_loc_nc ON devices(location COLLATE NOCASE)")

    # -- migration 3: optional device photo stored as base64 -----
    have_dev = _column_names(con, "devices")
    if "image" not in have_dev:
        con.execute("ALTER TABLE devices ADD COLUMN image TEXT NOT NULL DEFAULT ''")
    # NOCASE indexes so sorting and filtering by these columns avoids a scan.
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_brand_nc ON devices(brand COLLATE NOCASE)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_model_nc ON devices(model COLLATE NOCASE)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_loc_nc ON devices(location COLLATE NOCASE)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_assigned_nc ON devices(assigned_to COLLATE NOCASE)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_dept_nc ON devices(department COLLATE NOCASE)")

    # -- migration 3: warranty end date for lifecycle reporting --------------
    if "warranty_end" not in have_dev:
        con.execute("ALTER TABLE devices ADD COLUMN warranty_end TEXT NOT NULL DEFAULT ''")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_warranty ON devices(warranty_end)")

    con.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)


def get_settings(con):
    st = dict(DEFAULT_SETTINGS)
    for r in con.execute("SELECT key,value FROM settings"):
        st[r["key"]] = r["value"]
    return st


def settings_out(st):
    return {
        "prefix": st["prefix"],
        "sep": st["sep"],
        "pad": int(st["pad"]),
        "include_year": st["include_year"] == "1",
        "label_footer": st["label_footer"],
        "label_size": st["label_size"],
        "label_brand": st["label_brand"],
        "label_paper": st["label_paper"],
        "label_code": st["label_code"],
    }


_ENTITY = {"Users": "user", "Settings": "settings", "Sign in": "session", "Sign out": "session",
           "Password changed": "session", "Failed sign-in": "session", "Locked": "session",
           "Blocked sign-in": "session", "Backup": "system", "Export": "system",
           "Printed": "labels", "PDF": "labels"}


def log(con, action, details="", device_id=None, tag="", entity="", changes=None, parent=None, category=""):
    """Write one audit entry: who (from the signed-in user), what, when, and the field-level changes."""
    entity = entity or _ENTITY.get(action) or ("device" if (device_id or tag) else "system")
    cur = con.execute(
        "INSERT INTO activity(device_id,tag,action,details,user,role,ip,entity,changes,parent_id,category) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (device_id, tag, action, details, getattr(_ctx, "username", ""), getattr(_ctx, "role", ""),
         getattr(_ctx, "ip", ""), entity, json.dumps(changes, ensure_ascii=False) if changes else "", parent, category))
    return cur.lastrowid


def log_children(con, parent, kids):
    """Per-device audit rows that belong to one bulk action.
    kids = (device_id, tag, action, details, changes_json[, category])."""
    u, r, ip = getattr(_ctx, "username", ""), getattr(_ctx, "role", ""), getattr(_ctx, "ip", "")
    con.executemany(
        "INSERT INTO activity(device_id,tag,action,details,changes,category,user,role,ip,entity,parent_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,'device',?)",
        ((k[0], k[1], k[2], k[3], k[4], k[5] if len(k) > 5 else "", u, r, ip, parent) for k in kids))


# --------------------------------------------------------------------------
# Accounts, sessions and permissions
# --------------------------------------------------------------------------
_ctx = threading.local()
COOKIE = "bp_session"
SESSION_IDLE = 8 * 3600
SESSION_MAX = 7 * 86400
ROLES = {"admin": "Administrator", "technician": "Technician", "viewer": "Viewer"}
DEFAULT_ADMIN = ("admin", "BluePeak@2026")
USERNAME_RX = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
_fail = {}
_fail_lock = threading.Lock()
_dummy = []


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def hash_password(pw):
    salt = os.urandom(16)
    if hasattr(hashlib, "scrypt"):
        try:
            dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
            return "scrypt$16384$8$1$%s$%s" % (_b64(salt), _b64(dk))
        except (ValueError, MemoryError):
            pass
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 390000, 32)
    return "pbkdf2$390000$%s$%s" % (_b64(salt), _b64(dk))


def verify_password(pw, stored):
    try:
        parts = stored.split("$")
        if parts[0] == "scrypt":
            _, n, r, p, salt, dk = parts
            want = base64.b64decode(dk)
            got = hashlib.scrypt(pw.encode("utf-8"), salt=base64.b64decode(salt),
                                 n=int(n), r=int(r), p=int(p), dklen=len(want))
        elif parts[0] == "pbkdf2":
            _, it, salt, dk = parts
            want = base64.b64decode(dk)
            got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), base64.b64decode(salt), int(it), len(want))
        else:
            return False
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def dummy_hash():
    if not _dummy:
        _dummy.append(hash_password(secrets.token_hex(8)))
    return _dummy[0]


def check_password_rules(pw, username=""):
    if len(pw) < 8:
        raise ApiError(400, "Password must be at least 8 characters.")
    if len(pw) > 128:
        raise ApiError(400, "Password must be 128 characters or fewer.")
    if username and pw.lower() == username.lower():
        raise ApiError(400, "Password cannot be the same as the username.")
    if pw == DEFAULT_ADMIN[1]:
        raise ApiError(400, "Choose a password different from the default one.")


def public_user(u):
    return {"id": u["id"], "username": u["username"], "full_name": u["full_name"],
            "role": u["role"], "must_change": bool(u["must_change"])}


def user_row_out(r):
    return {"id": r["id"], "username": r["username"], "full_name": r["full_name"], "role": r["role"],
            "active": bool(r["active"]), "must_change": bool(r["must_change"]),
            "last_login": r["last_login"], "created_at": r["created_at"]}


def cookie_value(token, max_age):
    return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (COOKIE, token, max_age)


def current_user(h, con):
    raw = h.headers.get("Cookie")
    if not raw:
        return None
    try:
        morsel = SimpleCookie(raw).get(COOKIE)
    except Exception:
        return None
    if not morsel:
        return None
    th = hashlib.sha256(morsel.value.encode("utf-8")).hexdigest()
    now = int(time.time())
    row = con.execute(
        "SELECT u.*, s.last_seen AS s_seen, s.created_at AS s_created FROM sessions s "
        "JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (th,)).fetchone()
    if not row:
        return None
    if not row["active"] or now - row["s_seen"] > SESSION_IDLE or now - row["s_created"] > SESSION_MAX:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        return None
    if now - row["s_seen"] > 60:
        con.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, th))
    u = dict(row)
    u["token_hash"] = th
    return u


def authorize(user, perm):
    if perm == "public":
        return
    if not user:
        raise ApiError(401, "Please sign in.", "auth")
    if user["must_change"] and perm != "self":
        raise ApiError(403, "Choose a new password to continue.", "must_change")
    if perm == "write" and user["role"] not in ("admin", "technician"):
        raise ApiError(403, "Your role can look things up but not make changes.")
    if perm == "admin" and user["role"] != "admin":
        raise ApiError(403, "Only administrators can do that.")


def require(h, role):
    if not h.user or h.user["role"] != role:
        raise ApiError(403, "Only administrators can do that.")


def count_admins(con, exclude=0):
    return con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?", (exclude,)).fetchone()[0]


def api_session(h, con, q):
    return {"user": public_user(h.user) if h.user else None}


def api_login(h, con, q):
    d = h.body()
    username = str(d.get("username", "")).strip()[:64]
    pw = str(d.get("password", ""))[:200]
    key = username.lower()
    now = time.time()
    with _fail_lock:
        rec = _fail.get(key)
        if rec and rec[1] > now:
            raise ApiError(429, "Too many failed attempts. Try again in %d minute(s)." % math.ceil((rec[1] - now) / 60))
    row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    ok = verify_password(pw, row["password_hash"] if row else dummy_hash())
    if not (row and ok):
        locked = False
        with _fail_lock:
            rec = _fail.setdefault(key, [0, 0])
            rec[0] += 1
            if rec[0] >= 5:
                rec[0], rec[1], locked = 0, now + 300, True
        if row:
            log(con, "Failed sign-in", "Wrong password for %s" % row["username"], entity="session")
            if locked:
                log(con, "Locked", "Too many failed sign-ins for %s. Locked for 5 minutes." % row["username"],
                    entity="session")
        raise ApiError(401, "Wrong username or password.")
    if not row["active"]:
        log(con, "Blocked sign-in", "Disabled account %s tried to sign in" % row["username"], entity="session")
        raise ApiError(403, "This account is disabled. Ask an administrator.")
    with _fail_lock:
        _fail.pop(key, None)
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    t = int(time.time())
    with tx(con):
        con.execute("DELETE FROM sessions WHERE last_seen<? OR created_at<?", (t - SESSION_IDLE, t - SESSION_MAX))
        con.execute("INSERT INTO sessions(token_hash,user_id,created_at,last_seen) VALUES(?,?,?,?)", (th, row["id"], t, t))
        con.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (row["id"],))
        _ctx.username = row["username"]
        _ctx.role = row["role"]
        log(con, "Sign in", "")
    h._cookie = cookie_value(token, SESSION_MAX)
    return {"user": public_user(row)}


def api_logout(h, con, q):
    if h.user:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (h.user["token_hash"],))
        log(con, "Sign out", "")
    h._cookie = cookie_value("", 0)
    return {"ok": True}


def api_account_password(h, con, q):
    d = h.body()
    cur, new = str(d.get("current", "")), str(d.get("new", ""))
    row = con.execute("SELECT * FROM users WHERE id=?", (h.user["id"],)).fetchone()
    if not verify_password(cur, row["password_hash"]):
        raise ApiError(400, "Your current password is not correct.")
    check_password_rules(new, row["username"])
    if new == cur:
        raise ApiError(400, "The new password must be different from the current one.")
    newhash = hash_password(new)
    with tx(con):
        con.execute("UPDATE users SET password_hash=?, must_change=0 WHERE id=?", (newhash, row["id"]))
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (row["id"], h.user["token_hash"]))
        log(con, "Password changed", "")
    u = dict(row)
    u["must_change"] = 0
    return {"user": public_user(u)}


def api_users(h, con, q):
    rows = con.execute("SELECT * FROM users ORDER BY (role='admin') DESC, username COLLATE NOCASE")
    return {"users": [user_row_out(r) for r in rows]}


def api_user_create(h, con, q):
    d = h.body()
    username = str(d.get("username", "")).strip()
    if not USERNAME_RX.match(username):
        raise ApiError(400, "Username must be 3 to 32 letters, numbers, dots, dashes or underscores.")
    role = d.get("role", "viewer")
    if role not in ROLES:
        raise ApiError(400, "Choose a role.")
    full = str(d.get("full_name", "")).strip()[:80] or username
    pw = str(d.get("password", ""))
    check_password_rules(pw, username)
    pwhash = hash_password(pw)
    must = 1 if d.get("must_change", True) else 0
    with tx(con):
        try:
            con.execute("INSERT INTO users(username,full_name,role,password_hash,must_change) VALUES(?,?,?,?,?)",
                        (username, full, role, pwhash, must))
        except sqlite3.IntegrityError:
            raise ApiError(409, "That username is already taken.")
        log(con, "Users", "Created %s as %s" % (username, ROLES[role]))
    return {"ok": True}


def api_user_update(h, con, q, uid):
    uid = int(uid)
    d = h.body()
    with tx(con):
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise ApiError(404, "User not found.")
        full = str(d.get("full_name", row["full_name"])).strip()[:80] or row["username"]
        role = d.get("role", row["role"])
        active = 1 if d.get("active", bool(row["active"])) else 0
        if role not in ROLES:
            raise ApiError(400, "Choose a role.")
        if uid == h.user["id"] and (role != row["role"] or not active):
            raise ApiError(409, "You cannot change your own role or disable your own account.")
        if row["role"] == "admin" and (role != "admin" or not active) and count_admins(con, uid) == 0:
            raise ApiError(409, "At least one active administrator is required.")
        con.execute("UPDATE users SET full_name=?, role=?, active=? WHERE id=?", (full, role, active, uid))
        if not active or role != row["role"]:
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        notes = []
        if role != row["role"]:
            notes.append("role %s to %s" % (ROLES[row["role"]], ROLES[role]))
        if active != row["active"]:
            notes.append("account " + ("enabled" if active else "disabled"))
        if full != row["full_name"]:
            notes.append("name changed")
        if notes:
            log(con, "Users", "%s: %s" % (row["username"], ", ".join(notes)))
    return {"ok": True}


def api_user_password(h, con, q, uid):
    uid = int(uid)
    d = h.body()
    if uid == h.user["id"]:
        raise ApiError(400, "Use Change password in your account menu for your own password.")
    pw = str(d.get("password", ""))
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise ApiError(404, "User not found.")
    check_password_rules(pw, row["username"])
    pwhash = hash_password(pw)
    with tx(con):
        con.execute("UPDATE users SET password_hash=?, must_change=? WHERE id=?",
                    (pwhash, 1 if d.get("must_change", True) else 0, uid))
        con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        log(con, "Users", "Password reset for %s" % row["username"])
    return {"ok": True}


def api_user_delete(h, con, q, uid):
    uid = int(uid)
    if uid == h.user["id"]:
        raise ApiError(409, "You cannot delete your own account.")
    with tx(con):
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise ApiError(404, "User not found.")
        if row["role"] == "admin" and row["active"] and count_admins(con, uid) == 0:
            raise ApiError(409, "At least one active administrator is required.")
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        log(con, "Users", "Deleted %s" % row["username"])
    return {"ok": True}


# --------------------------------------------------------------------------
# TAG generation
# --------------------------------------------------------------------------
def build_tag(st, code, year, seq):
    parts = [st["prefix"], code]
    if st["include_year"] == "1":
        parts.append(str(year))
    parts.append(str(seq).zfill(int(st["pad"])))
    return st["sep"].join(p for p in parts if p != "")


def counter_key(st, code, year):
    return "%s|%s" % (code.upper(), year if st["include_year"] == "1" else 0)


def next_seq(con, st, code, year):
    row = con.execute("SELECT last FROM counters WHERE key=?", (counter_key(st, code, year),)).fetchone()
    last = row["last"] if row else 0
    while True:
        last += 1
        if not con.execute("SELECT 1 FROM devices WHERE tag=?", (build_tag(st, code, year, last),)).fetchone():
            return last


def allocate(con, st, code, year, qty):
    """Reserve `qty` brand-new tags. Call inside a transaction."""
    key = counter_key(st, code, year)
    row = con.execute("SELECT last FROM counters WHERE key=?", (key,)).fetchone()
    last = row["last"] if row else 0
    tags = []
    while len(tags) < qty:
        last += 1
        tag = build_tag(st, code, year, last)
        if con.execute("SELECT 1 FROM devices WHERE tag=?", (tag,)).fetchone():
            continue
        tags.append(tag)
    con.execute(
        "INSERT INTO counters(key,last) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET last=excluded.last",
        (key, last),
    )
    return tags


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
DEVICE_SELECT = (
    "SELECT d.*, c.name AS category_name, c.code AS category_code "
    "FROM devices d JOIN categories c ON c.id=d.category_id"
)
SORTS = {
    "tag": "d.tag", "brand": "d.brand COLLATE NOCASE", "model": "d.model COLLATE NOCASE", "category": "c.name COLLATE NOCASE", "device": "d.brand COLLATE NOCASE, d.model COLLATE NOCASE",
    "serial": "d.serial_number COLLATE NOCASE", "assigned": "d.assigned_to COLLATE NOCASE",
    "location": "d.location COLLATE NOCASE", "status": "d.status",
    "created_at": "d.id", "updated_at": "d.updated_at",
}
SEARCH_COLS = ["d.tag", "d.brand", "d.model", "d.serial_number", "d.mac_address",
               "d.assigned_to", "d.department", "d.location", "d.notes", "c.name"]


def g(q, name, default=""):
    v = q.get(name)
    return (v[0] if v else default).strip() if isinstance(default, str) else default


def build_filters(q):
    where, args = [], []
    for term in g(q, "q").split():
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(" + " OR ".join("%s LIKE ? ESCAPE '\\'" % c for c in SEARCH_COLS) + ")")
        args.extend([like] * len(SEARCH_COLS))
    cat = g(q, "category")
    if cat.isdigit():
        where.append("d.category_id=?")
        args.append(int(cat))
    if g(q, "status"):
        where.append("d.status=?")
        args.append(g(q, "status"))
    if g(q, "location"):
        where.append("d.location=? COLLATE NOCASE")
        args.append(g(q, "location"))
    if g(q, "batch"):
        where.append("d.batch_id=?")
        args.append(g(q, "batch"))
    war = g(q, "warranty")
    if war == "expired":
        where.append("d.warranty_end<>'' AND d.warranty_end<?")
        args.append(now_utc().strftime("%Y-%m-%d"))
    elif war == "soon":
        today = now_utc()
        where.append("d.warranty_end<>'' AND d.warranty_end>=? AND d.warranty_end<=?")
        args.extend([today.strftime("%Y-%m-%d"),
                     (today.date() + __import__("datetime").timedelta(days=90)).strftime("%Y-%m-%d")])
    return (" WHERE " + " AND ".join(where)) if where else "", args


def order_clause(q):
    col = SORTS.get(g(q, "sort"), SORTS["created_at"])
    direction = "ASC" if g(q, "dir").lower() == "asc" else "DESC"
    parts = [c.strip() + " " + direction for c in col.split(",")]
    return " ORDER BY " + ", ".join(parts) + ", d.id " + direction


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def clean_fields(data):
    f = {}
    for k, n in DEVICE_TEXT_FIELDS:
        f[k] = str(data.get(k) or "").strip()[:n]
    mac = f["mac_address"]
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if mac and len(digits) == 12 and re.fullmatch(r"[0-9A-Fa-f:.\-\s]+", mac):
        f["mac_address"] = ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()
    pd = str(data.get("purchase_date") or "").strip()
    if pd and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pd):
        raise ApiError(400, "Purchase date must look like 2026-09-30.")
    f["purchase_date"] = pd
    we = str(data.get("warranty_end") or "").strip()
    if we and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", we):
        raise ApiError(400, "Warranty end date must look like 2029-09-30.")
    f["warranty_end"] = we
    status = str(data.get("status") or "In stock").strip()
    if status not in STATUSES:
        raise ApiError(400, "Unknown status: %s" % status)
    if f["assigned_to"] and status == "In stock":
        status = "Assigned"
    if not f["assigned_to"] and status == "Assigned":
        status = "In stock"
    f["status"] = status
    return f


def check_serial(con, serial, exclude_id=None):
    if not serial:
        return
    r = con.execute(
        "SELECT tag FROM devices WHERE serial_lower=? AND id<>?",
        (serial.lower(), exclude_id if exclude_id else -1),
    ).fetchone()
    if r:
        raise ApiError(409, "Serial number %s already belongs to %s." % (serial, r["tag"]))


def get_category(con, cid):
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        raise ApiError(400, "Choose a device type.")
    cat = con.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
    if not cat:
        raise ApiError(400, "That device type no longer exists.")
    return cat


def parse_year(st, v):
    if st["include_year"] != "1":
        return 0
    try:
        y = int(v)
    except (TypeError, ValueError):
        y = datetime.now().year
    if y < 1990 or y > 2999:
        raise ApiError(400, "Year must be between 1990 and 2999.")
    return y


def now_utc():
    return datetime.now(timezone.utc)


def ids_from_body(con, body):
    if body.get("all_matching"):
        filt = {k: [str(v)] for k, v in (body.get("filters") or {}).items() if v not in (None, "")}
        where, args = build_filters(filt)
        return [r[0] for r in con.execute("SELECT d.id FROM devices d JOIN categories c ON c.id=d.category_id" + where, args)]
    ids = body.get("ids") or []
    return [int(i) for i in ids if str(i).isdigit()]


def chunks(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------
# API handlers   fn(h, con, q, *route_args)
# --------------------------------------------------------------------------
def api_meta(h, con, q):
    st = get_settings(con)
    cats = rows_to_dicts(con.execute(
        "SELECT c.id,c.name,c.code,COUNT(d.id) AS devices FROM categories c "
        "LEFT JOIN devices d ON d.category_id=c.id GROUP BY c.id ORDER BY c.name COLLATE NOCASE"))
    locs = [r[0] for r in con.execute("SELECT DISTINCT location FROM devices WHERE location<>'' ORDER BY location COLLATE NOCASE")]
    deps = [r[0] for r in con.execute("SELECT DISTINCT department FROM devices WHERE department<>'' ORDER BY department COLLATE NOCASE")]
    return {"settings": settings_out(st), "categories": cats, "statuses": STATUSES,
            "locations": locs, "departments": deps, "year": datetime.now().year,
            "db_file": os.path.basename(DB_PATH),
            "db_path": DB_PATH if h.user["role"] == "admin" else "",
            "cloud_enabled": st.get("cloud_enabled") == "1",
            "cloud_database": st.get("cloud_database", ""),
            "backup_last": st.get("backup_last", "") if h.user["role"] == "admin" else "",
            "user": public_user(h.user), "app": APP_NAME, "version": APP_VERSION}


def api_stats(h, con, q):
    total = con.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    by_status = {s: 0 for s in STATUSES}
    for r in con.execute("SELECT status,COUNT(*) n FROM devices GROUP BY status"):
        by_status[r["status"]] = r["n"]
    by_cat = rows_to_dicts(con.execute(
        "SELECT c.name,c.code,COUNT(d.id) n FROM categories c LEFT JOIN devices d ON d.category_id=c.id "
        "GROUP BY c.id ORDER BY n DESC, c.name COLLATE NOCASE"))
    recent = rows_to_dicts(con.execute(DEVICE_SELECT + " ORDER BY d.id DESC LIMIT 8"))
    today = now_utc().strftime("%Y-%m-%d")
    soon = (now_utc().date() + __import__("datetime").timedelta(days=90)).strftime("%Y-%m-%d")
    expiring = con.execute(
        "SELECT COUNT(*) FROM devices WHERE warranty_end<>'' AND warranty_end<=? AND warranty_end>=?",
        (soon, today)).fetchone()[0]
    expired = con.execute(
        "SELECT COUNT(*) FROM devices WHERE warranty_end<>'' AND warranty_end<?", (today,)).fetchone()[0]
    return {"total": total, "by_status": by_status, "by_category": by_cat, "recent": recent,
            "expiring_soon": expiring, "expired": expired}


def api_preview(h, con, q):
    st = get_settings(con)
    cat = get_category(con, g(q, "category_id"))
    year = parse_year(st, g(q, "year"))
    try:
        qty = max(1, int(g(q, "quantity") or 1))
    except ValueError:
        qty = 1
    seq = next_seq(con, st, cat["code"], year)
    return {"first": build_tag(st, cat["code"], year, seq),
            "last": build_tag(st, cat["code"], year, seq + qty - 1),
            "category": cat["name"], "quantity": qty}


def api_devices(h, con, q):
    where, args = build_filters(q)
    if g(q, "ids_only"):
        return {"ids": [r[0] for r in con.execute(
            "SELECT d.id FROM devices d JOIN categories c ON c.id=d.category_id" + where, args)]}
    total = con.execute("SELECT COUNT(*) FROM devices d JOIN categories c ON c.id=d.category_id" + where, args).fetchone()[0]
    try:
        size = int(g(q, "size") or 50)
        page = max(1, int(g(q, "page") or 1))
    except ValueError:
        size, page = 50, 1
    sql = DEVICE_SELECT + where + order_clause(q)
    if size > 0:
        sql += " LIMIT ? OFFSET ?"
        args = args + [size, (page - 1) * size]
    return {"rows": rows_to_dicts(con.execute(sql, args)), "total": total, "page": page, "size": size}


def api_devices_by_ids(h, con, q):
    ids = ids_from_body(con, h.body())
    out = []
    for part in chunks(ids):
        out += rows_to_dicts(con.execute(
            DEVICE_SELECT + " WHERE d.id IN (%s)" % ",".join("?" * len(part)), part))
    out.sort(key=lambda r: r["tag"])
    return {"rows": out}


FIELD_LABELS.update({"tag": "Tag", "category_name": "Device type", "created_at": "Created", "category_id": "Device type",
                     "warranty_end": "Warranty ends"})
FIELD_KEYS = ["brand", "model", "serial_number", "mac_address", "assigned_to", "department",
              "location", "purchase_date", "warranty_end", "notes", "status"]
SNAP_SKIP = {"id", "batch_id", "updated_at", "category_id", "category_code"}
BULK_KEYS = ("brand", "model", "department", "location", "purchase_date", "warranty_end", "status", "category_id")


def clean_tag(v):
    t = str(v or "").strip().upper()
    if not t:
        raise ApiError(400, "The tag cannot be empty.")
    if len(t) > 60 or not re.fullmatch(r"[A-Z0-9][A-Z0-9._/\-]*", t):
        raise ApiError(400, "A tag can use letters, numbers and . _ / - only, up to 60 characters.")
    return t


def parse_changes(s):
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def snapshot(row):
    return [{"f": FIELD_LABELS.get(k, k), "o": str(row[k]), "n": ""}
            for k in row.keys() if k not in SNAP_SKIP and row[k] not in (None, "")]


def change_text(changes):
    parts = []
    for c in changes:
        if c["f"] == "Notes":
            parts.append("Notes edited")
        else:
            parts.append("%s: %s \u2192 %s" % (c["f"], c["o"] or "empty", c["n"] or "empty"))
    return "; ".join(parts)


def api_device_get(h, con, q, did):
    r = con.execute(DEVICE_SELECT + " WHERE d.id=?", (int(did),)).fetchone()
    if not r:
        raise ApiError(404, "Device not found.")
    hist = []
    for x in con.execute(
            "SELECT id,ts,action,details,user,role,changes FROM activity WHERE device_id=? ORDER BY id DESC LIMIT 100",
            (int(did),)):
        item = dict(x)
        item["changes"] = parse_changes(x["changes"])
        hist.append(item)
    return {"device": dict(r), "history": hist}


def api_device_create(h, con, q):
    data = h.body()
    st = get_settings(con)
    f = clean_fields(data)
    with tx(con):
        cat = get_category(con, data.get("category_id"))
        check_serial(con, f["serial_number"])
        custom = str(data.get("tag") or "").strip()
        if custom:
            tag = clean_tag(custom)
            if con.execute("SELECT 1 FROM devices WHERE tag=?", (tag,)).fetchone():
                raise ApiError(409, "The tag %s is already used by another device." % tag)
        else:
            year = parse_year(st, data.get("year"))
            tag = allocate(con, st, cat["code"], year, 1)[0]
        cols = ["tag", "category_id"] + list(f.keys()) + ["serial_lower"]
        con.execute(
            "INSERT INTO devices(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
            [tag, cat["id"]] + list(f.values()) + [f["serial_number"].lower()])
        did = con.execute("SELECT id FROM devices WHERE tag=?", (tag,)).fetchone()[0]
        changes = [{"f": "Tag", "o": "", "n": tag}, {"f": "Device type", "o": "", "n": cat["name"]}]
        changes += [{"f": FIELD_LABELS[k], "o": "", "n": v} for k, v in f.items()
                    if v and not (k == "status" and v == "In stock")]
        detail = "%s added" % cat["name"]
        if f["assigned_to"]:
            detail += ", assigned to " + f["assigned_to"]
        log(con, "Created", detail, did, tag, "device", changes, category=cat["name"])
    return {"id": did, "tag": tag}


def api_device_update(h, con, q, did):
    did = int(did)
    data = h.body()
    with tx(con):
        old = con.execute(DEVICE_SELECT + " WHERE d.id=?", (did,)).fetchone()
        if not old:
            raise ApiError(404, "Device not found.")
        merged = {k: (data[k] if k in data else old[k]) for k in FIELD_KEYS}
        if "status" in data and "assigned_to" not in data and data["status"] == "In stock":
            merged["assigned_to"] = ""
        if "status" in data and data["status"] == "Assigned" and not str(merged["assigned_to"] or "").strip():
            raise ApiError(400, "Enter who the device is assigned to before setting it to Assigned.")
        f = clean_fields(merged)
        tag = old["tag"]
        if "tag" in data:
            tag = clean_tag(data["tag"])
            if tag != old["tag"] and con.execute("SELECT 1 FROM devices WHERE tag=?", (tag,)).fetchone():
                raise ApiError(409, "The tag %s is already used by another device." % tag)
        cat_id, cat_name = old["category_id"], old["category_name"]
        if "category_id" in data and str(data["category_id"]) != str(old["category_id"]):
            cat = get_category(con, data["category_id"])
            cat_id, cat_name = cat["id"], cat["name"]
        check_serial(con, f["serial_number"], did)
        con.execute(
            "UPDATE devices SET tag=?, category_id=?, " + ",".join("%s=?" % k for k in f) +
            ", serial_lower=?, updated_at=datetime('now') WHERE id=?",
            [tag, cat_id] + list(f.values()) + [f["serial_number"].lower(), did])
        changes = []
        if tag != old["tag"]:
            changes.append({"f": "Tag", "o": old["tag"], "n": tag})
        if cat_name != old["category_name"]:
            changes.append({"f": "Device type", "o": old["category_name"], "n": cat_name})
        for k in FIELD_KEYS:
            if (old[k] or "") != f[k]:
                changes.append({"f": FIELD_LABELS[k], "o": old[k] or "", "n": f[k]})
        if changes:
            names = {c["f"] for c in changes}
            if "Assigned to" in names:
                action = "Assigned" if f["assigned_to"] else "Returned"
            elif names == {"Status"}:
                action = "Status changed"
            elif "Tag" in names:
                action = "Tag changed"
            else:
                action = "Updated"
            log(con, action, change_text(changes), did, tag, "device", changes, category=cat_name)
    return {"ok": True}


def api_device_delete(h, con, q, did):
    did = int(did)
    with tx(con):
        old = con.execute(DEVICE_SELECT + " WHERE d.id=?", (did,)).fetchone()
        if not old:
            raise ApiError(404, "Device not found.")
        con.execute("DELETE FROM devices WHERE id=?", (did,))
        log(con, "Deleted", "%s removed. The tag is retired and will not be reused." % old["category_name"],
            did, old["tag"], "device", snapshot(old), category=old["category_name"])
    return {"ok": True}


def api_devices_bulk(h, con, q):
    body = h.body()
    action = body.get("action")
    if action not in ("delete", "status", "edit"):
        raise ApiError(400, "Unknown bulk action.")
    if action == "delete":
        require(h, "admin")
    fields = {}
    if action == "status":
        fields = {"status": body.get("value")}
    elif action == "edit":
        given = body.get("fields") or {}
        for k in BULK_KEYS:
            v = given.get(k)
            if v not in (None, ""):
                fields[k] = str(v).strip()
        if not fields:
            raise ApiError(400, "Choose at least one field to change.")
    if fields:
        if "status" in fields and (fields["status"] not in STATUSES or fields["status"] == "Assigned"):
            raise ApiError(400, "Choose a status other than Assigned. To assign a device, open it and enter who has it.")
        for k in ("brand", "model", "department", "location"):
            if k in fields:
                fields[k] = fields[k][:80]
        if "purchase_date" in fields and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["purchase_date"]):
            raise ApiError(400, "Purchase date must look like 2026-09-30.")
        if "warranty_end" in fields and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["warranty_end"]):
            raise ApiError(400, "Warranty end date must look like 2029-09-30.")
    with tx(con):
        ids = ids_from_body(con, body)
        if not ids:
            raise ApiError(400, "Nothing is selected.")
        cat = get_category(con, fields["category_id"]) if "category_id" in fields else None
        rows = []
        for part in chunks(ids):
            rows += con.execute(DEVICE_SELECT + " WHERE d.id IN (%s)" % ",".join("?" * len(part)), part).fetchall()
        rows.sort(key=lambda r: r["tag"])
        n = len(rows)
        if not rows:
            raise ApiError(400, "Those devices no longer exist.")
        span = rows[0]["tag"] if n == 1 else "%s to %s" % (rows[0]["tag"], rows[-1]["tag"])
        kids = []
        if action == "delete":
            summary = log(con, "Deleted", "%d devices (%s). Tags will not be reused." % (n, span), None, "", "device")
            kids = [(r["id"], r["tag"], "Deleted", "Removed in a bulk delete", json.dumps(snapshot(r), ensure_ascii=False),
                     r["category_name"]) for r in rows]
            for part in chunks(ids):
                con.execute("DELETE FROM devices WHERE id IN (%s)" % ",".join("?" * len(part)), part)
        else:
            updates = []
            for r in rows:
                new, ch = {}, []
                for k, v in fields.items():
                    if k == "category_id":
                        if r["category_id"] != cat["id"]:
                            new["category_id"] = cat["id"]
                            ch.append({"f": "Device type", "o": r["category_name"], "n": cat["name"]})
                    elif (r[k] or "") != v:
                        new[k] = v
                        ch.append({"f": FIELD_LABELS[k], "o": r[k] or "", "n": v})
                if fields.get("status") == "In stock" and r["assigned_to"]:
                    new["assigned_to"] = ""
                    ch.append({"f": "Assigned to", "o": r["assigned_to"], "n": ""})
                if ch:
                    updates.append((r["id"], new))
                    kids.append((r["id"], r["tag"], "Updated", change_text(ch) + " (bulk edit)",
                                 json.dumps(ch, ensure_ascii=False), cat["name"] if cat else r["category_name"]))
            for did, new in updates:
                con.execute("UPDATE devices SET " + ",".join("%s=?" % k for k in new) +
                            ", updated_at=datetime('now') WHERE id=?", list(new.values()) + [did])
            desc = ", ".join("%s to %s" % (FIELD_LABELS.get(k, "Device type"), cat["name"] if k == "category_id" else v)
                             for k, v in fields.items())
            summary = log(con, "Updated", "Bulk edit of %d devices (%s): %s. %d changed." % (n, span, desc, len(kids)),
                          None, "", "device")
        log_children(con, summary, kids)
    return {"ok": True, "count": n, "changed": len(kids)}


def api_generate(h, con, q):
    data = h.body()
    st = get_settings(con)
    try:
        qty = int(data.get("quantity"))
    except (TypeError, ValueError):
        raise ApiError(400, "Enter how many tags to generate.")
    if qty < 1:
        raise ApiError(400, "Quantity must be at least 1.")
    loc = str(data.get("location") or "").strip()[:80]
    dep = str(data.get("department") or "").strip()[:80]
    brand = str(data.get("brand") or "").strip()[:80]
    model = str(data.get("model") or "").strip()[:80]
    batch = now_utc().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)
    with tx(con):
        cat = get_category(con, data.get("category_id"))
        year = parse_year(st, data.get("year"))
        tags = allocate(con, st, cat["code"], year, qty)
        img = str(data.get("image") or "").strip()[:2**16]  # base64, limit size
        con.executemany(
            "INSERT INTO devices(tag,category_id,brand,model,status,location,department,batch_id,image) "
            "VALUES(?,?,?,?, 'In stock',?,?,?,?)",
            ((t, cat["id"], brand, model, loc, dep, batch, img) for t in tags))
        summary = log(con, "Generated", "%d %s tags: %s to %s" % (qty, cat["name"], tags[0], tags[-1]),
                      None, tags[0], "device", category=cat["name"])
        made = con.execute("SELECT id,tag FROM devices WHERE batch_id=?", (batch,)).fetchall()
        extra = (", " + loc) if loc else ""
        log_children(con, summary, [(r["id"], r["tag"], "Created", "Tag generated for %s%s" % (cat["name"], extra), "", cat["name"])
                                    for r in made])
    return {"batch_id": batch, "count": qty, "first": tags[0], "last": tags[-1],
            "tags": tags[:100], "category": cat["name"], "location": loc}


IMPORT_MAX_ROWS = 20000
IMPORT_BATCH = 500


def api_import(h, con, q):
    data = h.body()
    rows = data.get("rows") or []
    if len(rows) > IMPORT_MAX_ROWS:
        raise ApiError(400, "Too many rows in one import (%d). Split the file into batches of %d."
                       % (len(rows), IMPORT_MAX_ROWS))
    update_existing = str(data.get("mode") or "").lower() == "update"
    dry_run = bool(data.get("dry_run"))
    st = get_settings(con)
    cats = {}
    for c in con.execute("SELECT * FROM categories"):
        cats[c["name"].lower()] = c
        cats[c["code"].lower()] = c
    if dry_run:
        would_add, would_update, problems = 0, 0, []
        for i, r in enumerate(rows, start=2):
            try:
                cat = cats.get(str(r.get("category") or "").strip().lower())
                if not cat:
                    raise ApiError(400, "unknown device type \u201c%s\u201d" % (r.get("category") or ""))
                f = clean_fields(r)
                tag = str(r.get("tag") or "").strip()
                if tag:
                    tag = clean_tag(tag)
                    if con.execute("SELECT 1 FROM devices WHERE tag=?", (tag,)).fetchone():
                        if update_existing:
                            would_update += 1
                        else:
                            raise ApiError(409, "tag %s already exists" % tag)
                    else:
                        would_add += 1
                else:
                    would_add += 1
            except ApiError as e:
                problems.append("Row %d: %s" % (i, e.message))
        return {"dry_run": True, "would_import": would_add, "would_update": would_update,
                "skipped": problems[:100], "skipped_count": len(problems)}
    imported, updated, skipped, made = 0, 0, [], []
    year = datetime.now().year if st["include_year"] == "1" else 0
    for start in range(0, len(rows), IMPORT_BATCH):
        batch = rows[start:start + IMPORT_BATCH]
        with tx(con):
            for off, r in enumerate(batch):
                i = start + off + 2
                try:
                    cat = cats.get(str(r.get("category") or "").strip().lower())
                    if not cat:
                        raise ApiError(400, "unknown device type \u201c%s\u201d" % (r.get("category") or ""))
                    f = clean_fields(r)
                    tag = str(r.get("tag") or "").strip()
                    existing = None
                    if tag:
                        tag = clean_tag(tag)
                        existing = con.execute("SELECT id FROM devices WHERE tag=?", (tag,)).fetchone()
                        if existing and not update_existing:
                            raise ApiError(409, "tag %s already exists" % tag)
                    else:
                        tag = allocate(con, st, cat["code"], year, 1)[0]
                    check_serial(con, f["serial_number"], existing["id"] if existing else None)
                    if existing:
                        cols = list(f.keys()) + ["serial_lower"]
                        con.execute(
                            "UPDATE devices SET category_id=?, " + ",".join("%s=?" % k for k in cols) +
                            ", updated_at=datetime('now') WHERE id=?",
                            [cat["id"]] + list(f.values()) + [f["serial_number"].lower(), existing["id"]])
                        made.append((existing["id"], tag, cat["name"], "Updated"))
                        updated += 1
                    else:
                        cols = ["tag", "category_id"] + list(f.keys()) + ["serial_lower"]
                        cur = con.execute(
                            "INSERT INTO devices(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                            [tag, cat["id"]] + list(f.values()) + [f["serial_number"].lower()])
                        made.append((cur.lastrowid, tag, cat["name"], "Created"))
                        imported += 1
                except ApiError as e:
                    skipped.append("Row %d: %s" % (i, e.message))
            summary = log(con, "Imported", "%d added, %d updated, %d skipped" % (imported, updated, len(skipped)),
                          None, "", "device")
            log_children(con, summary, [(did, tag, act, "%s from CSV (%s)" % (act, cn), "", cn)
                                        for did, tag, cn, act in made])
            made = []
    return {"imported": imported, "updated": updated, "skipped": skipped[:50],
            "skipped_count": len(skipped)}


UBASE = {"sm": 1.0, "md": 1.4, "lg": 2.0}
PT = 72.0 / 25.4
HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
HELV_B = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
C128 = ["212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213", "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132", "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211", "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313", "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331", "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111", "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111", "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141", "114131", "311141", "411131", "211412", "211214", "211232", "2331112"]
NAVY = "0.0431 0.1020 0.2706"
MUTED = "0.3686 0.4157 0.5373"


def _tw(text, table, size):
    return sum((table[ord(c) - 32] if 32 <= ord(c) < 127 else 556) for c in text) * size / 1000.0


def _fit(text, table, size, max_w):
    if _tw(text, table, size) <= max_w:
        return text
    while text and _tw(text + "...", table, size) > max_w:
        text = text[:-1]
    return (text + "...") if text else ""


def _pdf_str(s):
    b = s.encode("cp1252", "replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def code128_bars(text):
    vals, total = [104], 104
    for i, ch in enumerate(text):
        v = ord(ch) - 32
        if v < 0 or v > 95:
            v = 0
        vals.append(v)
        total += v * (i + 1)
    vals += [total % 103, 106]
    x, bars = 10, []
    for v in vals:
        for i, c in enumerate(C128[v]):
            w = int(c)
            if i % 2 == 0:
                bars.append((x, w))
            x += w
    return bars, x + 10


def draw_label(ops, ox, oy, ph, u, tag, cat, brand, footer, code="barcode"):
    """Draw one TAG label. ox/oy = top-left corner in mm on a page ph mm tall; u = size factor."""
    def px(x):
        return (ox + x) * PT

    def py(y):
        return (ph - (oy + y)) * PT

    w, h = 50 * u, 25 * u
    r = 1.6 * u * PT
    k = 0.5523 * r
    L, R, T, B = px(0), px(w), py(0), py(h)
    segs = [("m", L + r, T), ("l", R - r, T), ("c", R - r + k, T, R, T - r + k, R, T - r), ("l", R, B + r),
            ("c", R, B + r - k, R - r + k, B, R - r, B), ("l", L + r, B),
            ("c", L + r - k, B, L, B + r - k, L, B + r), ("l", L, T - r), ("c", L, T - r + k, L + r - k, T, L + r, T)]
    path = " ".join(" ".join("%.2f" % v for v in s[1:]) + " " + s[0] for s in segs) + " h"
    ops.append(("q %s W n 0.7843 0.0627 0.1804 rg %.2f %.2f %.2f %.2f re f Q" % (path, L, B, 1.5 * u * PT, T - B)).encode())
    ops.append(("0.5412 0.5804 0.6902 RG %.3f w %s S" % (0.25 * PT, path)).encode())

    def text(font, size_mm, color, x_mm, y_mm, s, table=None, align="l"):
        size = size_mm * PT
        wpt = len(s) * 0.6 * size if table is None else _tw(s, table, size)
        x = px(x_mm)
        if align == "c":
            x -= wpt / 2
        elif align == "r":
            x -= wpt
        ops.append(("BT /%s %.3f Tf %s rg %.2f %.2f Td (" % (font, size, color, x, py(y_mm))).encode()
                   + _pdf_str(s) + b") Tj ET")

    cl, cr = 4 * u, 47.6 * u
    cw = cr - cl
    cat_txt = _fit(cat or "", HELV, 1.9 * u * PT, cw * 0.45 * PT)
    cat_w = _tw(cat_txt, HELV, 1.9 * u * PT) / PT
    brand_txt = _fit(brand or "", HELV_B, 2.5 * u * PT, (cw - cat_w - 1.5 * u) * PT)
    if brand_txt:
        text("F2", 2.5 * u, NAVY, cl, 4.0 * u, brand_txt, HELV_B)
    if cat_txt:
        text("F1", 1.9 * u, MUTED, cr, 4.0 * u, cat_txt, HELV, "r")
    if code == "qr":
        mat = qr_matrix(tag)
        n = len(mat)
        q = 15.6 * u
        mm = q / (n + 4)
        parts = ["0 g"]
        for y, row in enumerate(mat):
            x = 0
            while x < n:
                if row[x]:
                    x2 = x
                    while x2 < n and row[x2]:
                        x2 += 1
                    parts.append("%.3f %.3f %.3f %.3f re" % (px(cl + (x + 2) * mm), py(5.4 * u + (y + 3) * mm),
                                                             (x2 - x) * mm * PT, mm * PT))
                    x = x2
                else:
                    x += 1
        parts.append("f")
        ops.append(" ".join(parts).encode())
        fs = min(3.3, 26.5 / (len(tag) * 0.6))
        text("F3", fs * u, NAVY, (cl + q + 1.5 * u + cr) / 2, 13.2 * u + 0.3 * fs * u, tag, None, "c")
    else:
        bars, total = code128_bars(tag)
        mw = cw / total
        by, bh = py(14.84 * u), 8.6 * u * PT
        parts = ["0 g"]
        for xm, wm in bars:
            parts.append("%.3f %.3f %.3f %.3f re" % (px(cl + xm * mw), by, wm * mw * PT, bh))
        parts.append("f")
        ops.append(" ".join(parts).encode())
        fs = min(3.3, 43.0 / (len(tag) * 0.62))
        text("F3", fs * u, NAVY, cl + cw / 2, 16.53 * u + 0.85 * fs * u, tag, None, "c")
    foot = _fit(footer or "", HELV_B, 1.7 * u * PT, cw * PT)
    if foot:
        text("F2", 1.7 * u, NAVY, cl + cw / 2, 23.15 * u, foot, HELV_B, "c")


def build_pdf(pages, pw_mm, ph_mm):
    W, H = pw_mm * PT, ph_mm * PT
    out = io.BytesIO()
    offsets = []

    def add(b):
        offsets.append(out.tell())
        out.write(b)

    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    kids = " ".join("%d 0 R" % (7 + 2 * i) for i in range(len(pages)))
    add(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    add(("2 0 obj\n<< /Type /Pages /Kids [%s] /Count %d >>\nendobj\n" % (kids, len(pages))).encode())
    for i, name in enumerate(("Helvetica", "Helvetica-Bold", "Courier-Bold")):
        add(("%d 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>\nendobj\n"
             % (3 + i, name)).encode())
    add(b"6 0 obj\n<< /Title (BluePeak TAG labels) /Creator (BluePeak TAG Master) /Producer (BluePeak TAG Master) >>\nendobj\n")
    for i, ops in enumerate(pages):
        pid, cid = 7 + 2 * i, 8 + 2 * i
        add(("%d 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /Font << /F1 3 0 R "
             "/F2 4 0 R /F3 5 0 R >> >> /Contents %d 0 R >>\nendobj\n" % (pid, W, H, cid)).encode())
        data = zlib.compress(b"\n".join(ops))
        add(("%d 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % (cid, len(data))).encode()
            + data + b"\nendstream\nendobj\n")
    xref = out.tell()
    n = len(offsets) + 1
    out.write(("xref\n0 %d\n0000000000 65535 f \n" % n).encode())
    for o in offsets:
        out.write(("%010d 00000 n \n" % o).encode())
    out.write(("trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n, xref)).encode())
    return out.getvalue()


def build_labels_pdf(con, body):
    ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
    if not ids:
        raise ApiError(400, "There are no labels to save.")
    size = body.get("size", "md")
    if size not in UBASE:
        size = "md"
    lay = body.get("layout") or {}
    try:
        W, H = float(lay["W"]), float(lay["H"])
        cols, rows = int(lay["cols"]), int(lay["rows"])
        margin, gap = float(lay.get("margin", 8)), float(lay.get("gap", 3))
        k, roll = float(lay.get("k", 1)), bool(lay.get("roll"))
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "The page layout is not valid.")
    if not (20 <= W <= 1500 and 20 <= H <= 1500 and 1 <= cols <= 300 and 1 <= rows <= 300
            and 0 <= margin <= 100 and 0 <= gap <= 100 and 0.3 <= k <= 1.0001):
        raise ApiError(400, "The page layout is not valid.")
    u = UBASE[size] * k
    lw, lh = 50 * u, 25 * u
    gw = cols * lw + (cols - 1) * gap
    if gw > W + 0.01 or rows * lh + (rows - 1) * gap > H + 0.01:
        raise ApiError(400, "The labels do not fit on that page size.")
    st = get_settings(con)
    found = {}
    for part in chunks(ids):
        for r in con.execute(DEVICE_SELECT + " WHERE d.id IN (%s)" % ",".join("?" * len(part)), part):
            found[r["id"]] = r
    devs = [found[i] for i in ids if i in found]
    if not devs:
        raise ApiError(400, "Those devices no longer exist.")
    per = cols * rows
    x0 = 0 if roll else max(margin, (W - gw) / 2.0)
    y0 = 0 if roll else margin
    pages = []
    for start in range(0, len(devs), per):
        ops = []
        for j, d in enumerate(devs[start:start + per]):
            draw_label(ops, x0 + (j % cols) * (lw + gap), y0 + (j // cols) * (lh + gap), H, u,
                       d["tag"], d["category_name"], st["label_brand"], st["label_footer"], st["label_code"])
        pages.append(ops)
    data = build_pdf(pages, W, H)
    n = len(devs)
    span = devs[0]["tag"] if n == 1 else "%s to %s" % (devs[0]["tag"], devs[-1]["tag"])
    log(con, "PDF", "%d label%s saved as PDF (%s), %d page%s" % (n, "" if n == 1 else "s", span, len(pages),
                                                              "" if len(pages) == 1 else "s"),
        None, devs[0]["tag"] if n == 1 else "", "labels")
    return data, "BluePeak-labels-%s.pdf" % now_utc().strftime("%Y%m%d-%H%M")


# --------------------------------------------------------------------------
# QR code encoder (byte mode, error correction level M, versions 1 to 6)
# --------------------------------------------------------------------------
_QR_ECC = {1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4)}
_QR_RAW = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_QR_CACHE = {}


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _qr_penalty(m):
    n = len(m)
    cols = [[m[y][x] for y in range(n)] for x in range(n)]
    res = 0
    for lines in (m, cols):
        for line in lines:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        res += 3 + (run - 5)
                    run = 1
            if run >= 5:
                res += 3 + (run - 5)
    for y in range(n - 1):
        for x in range(n - 1):
            c = m[y][x]
            if c == m[y][x + 1] and c == m[y + 1][x] and c == m[y + 1][x + 1]:
                res += 3
    p1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    p2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for lines in (m, cols):
        for line in lines:
            li = [1 if v else 0 for v in line]
            for i in range(n - 10):
                seg = li[i:i + 11]
                if seg == p1:
                    res += 40
                if seg == p2:
                    res += 40
    dark = sum(1 for row in m for v in row if v)
    total = n * n
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return res + k * 10


def qr_matrix(text):
    """Return the QR code for `text` as a list of rows of booleans (True = dark module)."""
    if text in _QR_CACHE:
        return _QR_CACHE[text]
    data = text.encode("utf-8")
    ver = 0
    for v in range(1, 7):
        cap = _QR_RAW[v] - _QR_ECC[v][0] * _QR_ECC[v][1]
        if 12 + len(data) * 8 <= cap * 8:
            ver = v
            break
    if not ver:
        raise ValueError("Text is too long for a QR code label")
    ecc_len, nblocks = _QR_ECC[ver]
    raw = _QR_RAW[ver]
    capbits = (raw - ecc_len * nblocks) * 8
    bits = []

    def put(val, n):
        bits.extend(((val >> i) & 1) for i in range(n - 1, -1, -1))

    put(4, 4)
    put(len(data), 8)
    for b in data:
        put(b, 8)
    put(0, min(4, capbits - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capbits:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    cw = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    # error correction + interleaving
    nshort = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        dlen = short_len - ecc_len + (0 if i < nshort else 1)
        dat = cw[k:k + dlen]
        k += dlen
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= nshort:
                final.append(blk[i])
    size = ver * 4 + 17
    mods = [[False] * size for _ in range(size)]
    isfn = [[False] * size for _ in range(size)]

    def setfn(x, y, dark):
        mods[y][x] = dark
        isfn[y][x] = True

    for i in range(size):
        setfn(6, i, i % 2 == 0)
        setfn(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                dist = max(abs(dx), abs(dy))
                xx, yy = cx + dx, cy + dy
                if 0 <= xx < size and 0 <= yy < size:
                    setfn(xx, yy, dist not in (2, 4))
    pos = _QR_ALIGN[ver]
    n = len(pos)
    for i in range(n):
        for j in range(n):
            if not ((i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0)):
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        setfn(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        data5 = mask  # error correction level M has format bits 00
        rem = data5
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        fb = ((data5 << 10) | rem) ^ 0x5412

        def bit(i):
            return ((fb >> i) & 1) == 1
        for i in range(0, 6):
            setfn(8, i, bit(i))
        setfn(8, 7, bit(6))
        setfn(8, 8, bit(7))
        setfn(7, 8, bit(8))
        for i in range(9, 15):
            setfn(14 - i, 8, bit(i))
        for i in range(0, 8):
            setfn(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setfn(8, size - 15 + i, bit(i))
        setfn(8, size - 8, True)

    draw_format(0)
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not isfn[y][x] and i < len(final) * 8:
                    mods[y][x] = ((final[i >> 3] >> (7 - (i & 7))) & 1) == 1
                    i += 1
        right -= 2

    def apply_mask(mk):
        for y in range(size):
            for x in range(size):
                if mk == 0:
                    inv = (x + y) % 2 == 0
                elif mk == 1:
                    inv = y % 2 == 0
                elif mk == 2:
                    inv = x % 3 == 0
                elif mk == 3:
                    inv = (x + y) % 3 == 0
                elif mk == 4:
                    inv = (x // 3 + y // 2) % 2 == 0
                elif mk == 5:
                    inv = x * y % 2 + x * y % 3 == 0
                elif mk == 6:
                    inv = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv and not isfn[y][x]:
                    mods[y][x] = not mods[y][x]

    best, best_pen = 0, None
    for mk in range(8):
        apply_mask(mk)
        draw_format(mk)
        pen = _qr_penalty(mods)
        if best_pen is None or pen < best_pen:
            best, best_pen = mk, pen
        apply_mask(mk)
    apply_mask(best)
    draw_format(best)
    _QR_CACHE[text] = mods
    if len(_QR_CACHE) > 5000:
        _QR_CACHE.clear()
    return mods


# --------------------------------------------------------------------------
# Excel workbook writer (no third-party packages): formulas, tables, conditional formats and charts
# --------------------------------------------------------------------------
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def xesc(s):
    s = _XML_BAD.sub("", str(s))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def xcol(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xserial(dt):
    delta = dt - datetime(1899, 12, 30)
    return delta.days + delta.seconds / 86400.0


def xnum(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return repr(float(v))


_XFMT = {"int": (3, None), "pct": (None, "0.0%"), "date": (None, "yyyy\\-mm\\-dd"),
         "dt": (None, "yyyy\\-mm\\-dd\\ hh:mm"), "mon": (None, "mmm\\ yyyy")}


class XStyles:
    def __init__(self):
        self.fonts = ['<font><sz val="11"/><color rgb="FF0F1B3A"/><name val="Calibri"/><family val="2"/></font>']
        self.fills = ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>']
        self.borders = ['<border><left/><right/><top/><bottom/><diagonal/></border>']
        self.numfmts = {}
        self.xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']
        self.memo = {}
        self.dxfs = []

    @staticmethod
    def _idx(lst, x):
        if x in lst:
            return lst.index(x)
        lst.append(x)
        return len(lst) - 1

    def get(self, bold=False, italic=False, size=11, color="0F1B3A", fill=None, border=None, fmt=None,
            h=None, v=None, wrap=False):
        key = (bold, italic, size, color, fill, border, fmt, h, v, wrap)
        if key in self.memo:
            return self.memo[key]
        font = '<font>%s%s<sz val="%s"/><color rgb="FF%s"/><name val="Calibri"/><family val="2"/></font>' % (
            "<b/>" if bold else "", "<i/>" if italic else "", size, color)
        fi = self._idx(self.fonts, font)
        fl = 0
        if fill:
            fl = self._idx(self.fills, '<fill><patternFill patternType="solid"><fgColor rgb="FF%s"/>'
                                       '<bgColor indexed="64"/></patternFill></fill>' % fill)
        bd = 0
        if border:
            sides = {}
            for ss, style, col in border:
                for ch in ss:
                    sides[ch] = (style, col)
            parts = []
            for ch, tag in (("l", "left"), ("r", "right"), ("t", "top"), ("b", "bottom")):
                if ch in sides:
                    parts.append('<%s style="%s"><color rgb="FF%s"/></%s>' % (tag, sides[ch][0], sides[ch][1], tag))
                else:
                    parts.append("<%s/>" % tag)
            bd = self._idx(self.borders, "<border>%s<diagonal/></border>" % "".join(parts))
        nf = 0
        if fmt:
            bid, code = _XFMT[fmt]
            if bid is not None:
                nf = bid
            else:
                if code not in self.numfmts:
                    self.numfmts[code] = 164 + len(self.numfmts)
                nf = self.numfmts[code]
        al = ""
        if h or v or wrap:
            al = "<alignment%s%s%s/>" % (' horizontal="%s"' % h if h else "", ' vertical="%s"' % v if v else "",
                                         ' wrapText="1"' if wrap else "")
        xf = '<xf numFmtId="%d" fontId="%d" fillId="%d" borderId="%d" xfId="0" applyNumberFormat="1" applyFont="1" ' \
             'applyFill="1" applyBorder="1"%s>%s</xf>' % (nf, fi, fl, bd, ' applyAlignment="1"' if al else "", al) \
            if al else '<xf numFmtId="%d" fontId="%d" fillId="%d" borderId="%d" xfId="0" applyNumberFormat="1" ' \
                       'applyFont="1" applyFill="1" applyBorder="1"/>' % (nf, fi, fl, bd)
        self.xfs.append(xf)
        self.memo[key] = len(self.xfs) - 1
        return self.memo[key]

    def dxf(self, font_color, fill):
        self.dxfs.append('<dxf><font><color rgb="FF%s"/></font><fill><patternFill><bgColor rgb="FF%s"/></patternFill></fill></dxf>'
                         % (font_color, fill))
        return len(self.dxfs) - 1

    def xml(self):
        nf = ""
        if self.numfmts:
            nf = '<numFmts count="%d">%s</numFmts>' % (len(self.numfmts), "".join(
                '<numFmt numFmtId="%d" formatCode="%s"/>' % (i, xesc(c)) for c, i in self.numfmts.items()))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">%s'
                '<fonts count="%d">%s</fonts><fills count="%d">%s</fills><borders count="%d">%s</borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="%d">%s</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                '<dxfs count="%d">%s</dxfs>'
                '<tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>'
                '</styleSheet>') % (nf, len(self.fonts), "".join(self.fonts), len(self.fills), "".join(self.fills),
                                    len(self.borders), "".join(self.borders), len(self.xfs), "".join(self.xfs),
                                    len(self.dxfs), "".join(self.dxfs))


class XBook:
    def __init__(self):
        self.styles = XStyles()
        self.strings, self.sidx, self.nrefs = [], {}, 0

    def sst(self, s):
        self.nrefs += 1
        if s not in self.sidx:
            self.sidx[s] = len(self.strings)
            self.strings.append(s)
        return self.sidx[s]

    def sst_xml(self):
        items = "".join('<si><t xml:space="preserve">%s</t></si>' % xesc(s) for s in self.strings)
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main" count="%d" uniqueCount="%d">%s</sst>' % (self.nrefs, len(self.strings), items))


class XSheet:
    def __init__(self, book, name):
        self.book, self.name = book, name
        self.cells, self.heights, self.widths, self.merges = {}, {}, {}, []

    def put(self, r, c, v=None, s=0, f=None):
        self.cells[(r, c)] = (v, s, f)

    def fill(self, r, c1, c2, s):
        for c in range(c1, c2 + 1):
            if (r, c) not in self.cells:
                self.cells[(r, c)] = (None, s, None)

    def merge(self, r1, c1, r2, c2):
        self.merges.append("%s%d:%s%d" % (xcol(c1), r1, xcol(c2), r2))

    def _cell(self, r, c, v, s, f):
        ref = "%s%d" % (xcol(c), r)
        sa = ' s="%d"' % s if s else ""
        if f is not None:
            fx = "<f>%s</f>" % xesc(f)
            if isinstance(v, str):
                return '<c r="%s"%s t="str">%s<v>%s</v></c>' % (ref, sa, fx, xesc(v))
            return '<c r="%s"%s>%s<v>%s</v></c>' % (ref, sa, fx, xnum(v))
        if v is None:
            return '<c r="%s"%s/>' % (ref, sa)
        if isinstance(v, str):
            return '<c r="%s"%s t="s"><v>%d</v></c>' % (ref, sa, self.book.sst(v))
        return '<c r="%s"%s><v>%s</v></c>' % (ref, sa, xnum(v))

    def xml(self, tab_color=None, grid=True, freeze=0, fit=False, landscape=False, drawing=None, table=None,
            extra="", selected=False):
        rows = {}
        for (r, c), val in self.cells.items():
            rows.setdefault(r, {})[c] = val
        maxr = max(rows) if rows else 1
        maxc = max(c for (_, c) in self.cells) if self.cells else 1
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats.org/'
               'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
        pr = ""
        if tab_color:
            pr += '<tabColor rgb="FF%s"/>' % tab_color
        if fit:
            pr += '<pageSetUpPr fitToPage="1"/>'
        if pr:
            out.append("<sheetPr>%s</sheetPr>" % pr)
        out.append('<dimension ref="A1:%s%d"/>' % (xcol(maxc), maxr))
        view = '<sheetView workbookViewId="0"%s%s>' % ("" if grid else ' showGridLines="0"', ' tabSelected="1"' if selected else "")
        if freeze:
            view += '<pane ySplit="%d" topLeftCell="A%d" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft"/>' % (freeze, freeze + 1)
        out.append("<sheetViews>%s</sheetView></sheetViews>" % view)
        out.append('<sheetFormatPr defaultRowHeight="15"/>')
        if self.widths:
            out.append("<cols>%s</cols>" % "".join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (c, c, w)
                                                   for c, w in sorted(self.widths.items())))
        out.append("<sheetData>")
        for r in sorted(rows):
            ht = self.heights.get(r)
            out.append('<row r="%d"%s>' % (r, ' ht="%s" customHeight="1"' % ht if ht else ""))
            for c in sorted(rows[r]):
                out.append(self._cell(r, c, *rows[r][c]))
            out.append("</row>")
        out.append("</sheetData>")
        if self.merges:
            out.append('<mergeCells count="%d">%s</mergeCells>' % (len(self.merges), "".join('<mergeCell ref="%s"/>' % m for m in self.merges)))
        out.append(extra)
        out.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>')
        if fit:
            out.append('<pageSetup orientation="%s" fitToWidth="1" fitToHeight="0"/>' % ("landscape" if landscape else "portrait"))
        if drawing:
            out.append('<drawing r:id="%s"/>' % drawing)
        if table:
            out.append('<tableParts count="1"><tablePart r:id="%s"/></tableParts>' % table)
        out.append("</worksheet>")
        return "".join(out)


def _str_cache(vals):
    return '<c:strCache><c:ptCount val="%d"/>%s</c:strCache>' % (
        len(vals), "".join('<c:pt idx="%d"><c:v>%s</c:v></c:pt>' % (i, xesc(v)) for i, v in enumerate(vals)))


def _num_cache(vals, fmt="General"):
    return '<c:numCache><c:formatCode>%s</c:formatCode><c:ptCount val="%d"/>%s</c:numCache>' % (
        xesc(fmt), len(vals), "".join('<c:pt idx="%d"><c:v>%s</c:v></c:pt>' % (i, xnum(v)) for i, v in enumerate(vals)))


def chart_xml(kind, title, cat, series, horizontal=False, stacked=False, legend=None, labels=True):
    """cat = (ref, values, is_numeric, format). series = [(name, name_ref, val_ref, values, colour_or_list)]"""
    cref, cvals, cnum, cfmt = cat
    if cnum:
        catxml = "<c:cat><c:numRef><c:f>%s</c:f>%s</c:numRef></c:cat>" % (cref, _num_cache(cvals, cfmt))
    else:
        catxml = "<c:cat><c:strRef><c:f>%s</c:f>%s</c:strRef></c:cat>" % (cref, _str_cache(cvals))
    lbl_txt = '<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="900" b="1"/></a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr>'
    sers = []
    for i, (name, nref, vref, vals, color) in enumerate(series):
        if nref:
            tx = "<c:tx><c:strRef><c:f>%s</c:f>%s</c:strRef></c:tx>" % (nref, _str_cache([name]))
        else:
            tx = "<c:tx><c:v>%s</c:v></c:tx>" % xesc(name)
        dl = ""
        if labels:
            pos = "" if kind == "doughnut" else ('<c:dLblPos val="ctr"/>' if stacked else '<c:dLblPos val="outEnd"/>')
            dl = ('<c:dLbls><c:numFmt formatCode="#,##0;-#,##0;;" sourceLinked="0"/><c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>'
                  '%s%s<c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/><c:showSerName val="0"/>'
                  '<c:showPercent val="0"/><c:showBubbleSize val="0"/></c:dLbls>') % (lbl_txt, pos)
        if kind == "doughnut":
            dpts = "".join('<c:dPt><c:idx val="%d"/><c:bubble3D val="0"/><c:spPr><a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
                           '<a:ln w="19050"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln></c:spPr></c:dPt>' % (j, col)
                           for j, col in enumerate(color))
            sers.append('<c:ser><c:idx val="%d"/><c:order val="%d"/>%s%s%s%s<c:val><c:numRef><c:f>%s</c:f>%s</c:numRef></c:val></c:ser>'
                        % (i, i, tx, dpts, dl, catxml, vref, _num_cache(vals)))
        else:
            sers.append('<c:ser><c:idx val="%d"/><c:order val="%d"/>%s<c:spPr><a:solidFill><a:srgbClr val="%s"/></a:solidFill></c:spPr>'
                        '<c:invertIfNegative val="0"/>%s%s<c:val><c:numRef><c:f>%s</c:f>%s</c:numRef></c:val></c:ser>'
                        % (i, i, tx, color, dl, catxml, vref, _num_cache(vals)))
    line = '<c:spPr><a:ln w="9525"><a:solidFill><a:srgbClr val="DCE1EC"/></a:solidFill></a:ln></c:spPr>'
    if kind == "doughnut":
        plot = ('<c:doughnutChart><c:varyColors val="1"/>%s<c:firstSliceAng val="0"/><c:holeSize val="58"/></c:doughnutChart>'
                % "".join(sers))
    else:
        grouping = "stacked" if stacked else "clustered"
        overlap = '<c:overlap val="100"/>' if stacked else ""
        cat_ax = ('<c:catAx><c:axId val="5001"/><c:scaling><c:orientation val="%s"/></c:scaling><c:delete val="0"/>'
                  '<c:axPos val="%s"/><c:numFmt formatCode="%s" sourceLinked="0"/><c:majorTickMark val="none"/>'
                  '<c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>%s<c:crossAx val="5002"/><c:crosses val="autoZero"/>'
                  '<c:auto val="1"/><c:lblAlgn val="ctr"/><c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/></c:catAx>'
                  % ("maxMin" if horizontal else "minMax", "l" if horizontal else "b",
                     xesc(cfmt if cnum else "General"), line))
        val_ax = ('<c:valAx><c:axId val="5002"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/>'
                  '<c:axPos val="%s"/><c:majorGridlines><c:spPr><a:ln w="6350"><a:solidFill><a:srgbClr val="E3E7F0"/></a:solidFill></a:ln></c:spPr></c:majorGridlines>'
                  '<c:numFmt formatCode="#,##0" sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/>'
                  '<c:tickLblPos val="nextTo"/><c:spPr><a:ln><a:noFill/></a:ln></c:spPr><c:crossAx val="5001"/>'
                  '<c:crosses val="%s"/><c:crossBetween val="between"/></c:valAx>'
                  % ("b" if horizontal else "l", "max" if horizontal else "autoZero"))
        plot = ('<c:barChart><c:barDir val="%s"/><c:grouping val="%s"/><c:varyColors val="0"/>%s<c:gapWidth val="55"/>%s'
                '<c:axId val="5001"/><c:axId val="5002"/></c:barChart>%s%s'
                % ("bar" if horizontal else "col", grouping, "".join(sers), overlap, cat_ax, val_ax))
    leg = ""
    if legend:
        leg = '<c:legend><c:legendPos val="%s"/><c:overlay val="0"/></c:legend>' % legend
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<c:roundedCorners val="0"/><c:chart><c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:pPr>'
            '<a:defRPr sz="1300" b="1"/></a:pPr><a:r><a:rPr lang="en-US" sz="1300" b="1"><a:solidFill><a:srgbClr val="13296B"/></a:solidFill></a:rPr>'
            '<a:t>%s</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title><c:autoTitleDeleted val="0"/>'
            '<c:plotArea><c:layout/>%s</c:plotArea>%s<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart>'
            '<c:spPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:ln w="9525"><a:solidFill><a:srgbClr val="DCE1EC"/></a:solidFill></a:ln></c:spPr>'
            '<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="900"/></a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr>'
            '</c:chartSpace>' % (xesc(title), plot, leg))


def drawing_xml(anchors):
    parts = []
    for i, (c0, r0, c1, r1) in enumerate(anchors, start=1):
        parts.append(
            '<xdr:twoCellAnchor><xdr:from><xdr:col>%d</xdr:col><xdr:colOff>76200</xdr:colOff><xdr:row>%d</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
            '<xdr:to><xdr:col>%d</xdr:col><xdr:colOff>-76200</xdr:colOff><xdr:row>%d</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>'
            '<xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="%d" name="Chart %d"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr>'
            '<xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
            '<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId%d"/>'
            '</a:graphicData></a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor>' % (c0, r0, c1, r1, i + 1, i, i))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">%s</xdr:wsDr>' % "".join(parts))


def _add_months(d, n):
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def _xl_top(values, n):
    groups = {}
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        g_ = groups.setdefault(v.lower(), [v, 0])
        g_[1] += 1
    items = sorted(groups.values(), key=lambda x: (-x[1], x[0].lower()))
    safe = [(v, c) for v, c in items if not any(ch in v for ch in "*?~") and v[0] not in "=<>"]
    return safe[:n]


STATUS_HEX = {"In stock": "1F4FD8", "Assigned": "16794E", "In repair": "D98A00", "Lost": "B4162B", "Retired": "8A94B0"}


def build_xlsx(con, who):
    now = datetime.now()
    today = now.date()
    devs = [dict(r) for r in con.execute(DEVICE_SELECT + " ORDER BY d.tag")]
    N = len(devs)
    type_names = [r[0] for r in con.execute("SELECT name FROM categories ORDER BY name COLLATE NOCASE")]
    bk = XBook()
    S = bk.styles
    NAVY, INK, MUTED, LINE, SOFT = "13296B", "0F1B3A", "5E6A89", "DCE1EC", "F5F7FB"
    thin = (("b", "thin", LINE),)
    s_title = S.get(bold=True, size=24, color=NAVY, v="center")
    s_h2 = S.get(bold=True, size=14, color=INK, v="center")
    s_sub = S.get(size=10, color=MUTED, h="right", v="center")
    s_note = S.get(italic=True, size=10, color=MUTED, wrap=True, v="top")
    s_sec = S.get(bold=True, size=13, color=NAVY, border=(("b", "medium", "C8102E"),), v="center")
    s_hdr = S.get(bold=True, color="FFFFFF", fill=NAVY, h="center", v="center", wrap=True)
    s_hdr_l = S.get(bold=True, color="FFFFFF", fill=NAVY, h="left", v="center")
    s_txt = S.get(border=thin)
    s_int = S.get(border=thin, fmt="int", h="right")
    s_pct = S.get(border=thin, fmt="pct", h="right")
    s_mon = S.get(border=thin, fmt="mon", h="left")
    s_year = S.get(border=thin, h="left")
    tot = (("t", "thin", NAVY),)
    s_tt = S.get(bold=True, fill=SOFT, border=tot)
    s_ti = S.get(bold=True, fill=SOFT, border=tot, fmt="int", h="right")
    s_tp = S.get(bold=True, fill=SOFT, border=tot, fmt="pct", h="right")
    s_ih = S.get(bold=True, color="FFFFFF", fill=NAVY, v="center")
    s_dt = S.get(fmt="dt", h="left")
    s_date = S.get(fmt="date", h="left")

    # ---------------------------------------------------------------- Inventory sheet
    inv = XSheet(bk, "Inventory")
    heads = ["Tag", "Device type", "Brand", "Model", "Serial number", "MAC address", "Status", "Assigned to",
             "Department", "Location", "Purchase date", "Warranty ends", "Notes", "Created", "Last updated"]
    keys = ["tag", "category_name", "brand", "model", "serial_number", "mac_address", "status", "assigned_to",
            "department", "location", "purchase_date", "warranty_end", "notes", "created_at", "updated_at"]
    for i, hd in enumerate(heads, start=1):
        inv.put(1, i, hd, s_ih)
    inv.heights[1] = 26
    created, purch = [], []
    for ri, d in enumerate(devs, start=2):
        for ci, k in enumerate(keys, start=1):
            v = d[k]
            if k in ("created_at", "updated_at"):
                try:
                    ser = xserial(datetime.strptime(v, "%Y-%m-%d %H:%M:%S"))
                    inv.put(ri, ci, ser, s_dt)
                    if k == "created_at":
                        created.append(ser)
                except (TypeError, ValueError):
                    created.append(None) if k == "created_at" else None
            elif k == "purchase_date" or k == "warranty_end":
                ser = None
                if v:
                    try:
                        ser = xserial(datetime.strptime(v, "%Y-%m-%d"))
                        inv.put(ri, ci, int(ser), s_date)
                        ser = int(ser)
                    except ValueError:
                        inv.put(ri, ci, str(v))
                if k == "purchase_date":
                    purch.append(ser)
            elif v:
                inv.put(ri, ci, str(v))
    last = N + 1 if N else 2
    for i, wd in enumerate([21, 16, 14, 17, 18, 19, 12, 20, 16, 17, 14, 14, 32, 18, 18], start=1):
        inv.widths[i] = wd
    dvmax = max(5000, N + 1000)
    dx_in = S.dxf("1F4FD8", "E3EBFF")
    dx_as = S.dxf("16794E", "DDF3E8")
    dx_rp = S.dxf("9A5B00", "FDEFD0")
    dx_lo = S.dxf("B4162B", "FCE5E8")
    dx_rt = S.dxf("5E6A89", "E8EBF2")
    cf = '<conditionalFormatting sqref="G2:G%d">%s</conditionalFormatting>' % (dvmax, "".join(
        '<cfRule type="cellIs" dxfId="%d" priority="%d" operator="equal"><formula>"%s"</formula></cfRule>' % (dx, i + 1, nm)
        for i, (dx, nm) in enumerate([(dx_in, "In stock"), (dx_as, "Assigned"), (dx_rp, "In repair"), (dx_lo, "Lost"), (dx_rt, "Retired")])))
    dv = ('<dataValidations count="1"><dataValidation type="list" allowBlank="1" showErrorMessage="1" '
          'errorTitle="Status" error="Choose a status from the list." sqref="G2:G%d"><formula1>"In stock,Assigned,In repair,Lost,Retired"</formula1>'
          '</dataValidation></dataValidations>' % dvmax)

    # ---------------------------------------------------------------- Stats sheet
    st = XSheet(bk, "Stats")
    st.widths = {1: 34, 2: 14, 3: 12, 4: 12, 5: 12, 6: 12, 7: 12, 8: 12}
    st.put(1, 1, "Statistics", s_title)
    st.heights[1] = 36
    st.put(2, 1, "Every number on this sheet is a live formula over the Inventory sheet, so it updates as soon as you add or edit a row.", s_note)
    st.merge(2, 1, 2, 8)
    st.heights[2] = 30
    cur = [4]
    refs = {}

    def sec(title, ncols):
        r = cur[0]
        st.put(r, 1, title, s_sec)
        st.fill(r, 2, ncols, s_sec)
        st.heights[r] = 24
        cur[0] += 1

    def hdr(titles):
        r = cur[0]
        for i, t in enumerate(titles, start=1):
            st.put(r, i, t, s_hdr_l if i == 1 else s_hdr)
        st.heights[r] = 22
        cur[0] += 1
        return r

    # status block
    stat_counts = {s: sum(1 for d in devs if d["status"] == s) for s in STATUSES}
    ssum = sum(stat_counts.values())
    sec("By status", 3)
    hdr(["Status", "Devices", "Share"])
    s1 = cur[0]
    stot = s1 + len(STATUSES)
    for sname in STATUSES:
        r = cur[0]
        st.put(r, 1, sname, s_txt)
        st.put(r, 2, stat_counts[sname], s_int, "COUNTIFS(Devices[Status],$A%d)" % r)
        st.put(r, 3, (stat_counts[sname] / ssum) if ssum else 0, s_pct, "IF($B$%d=0,0,B%d/$B$%d)" % (stot, r, stot))
        cur[0] += 1
    s2 = cur[0] - 1
    r = cur[0]
    st.put(r, 1, "Total", s_tt)
    st.put(r, 2, ssum, s_ti, "SUM(B%d:B%d)" % (s1, s2))
    st.put(r, 3, 1 if ssum else 0, s_tp, "SUM(C%d:C%d)" % (s1, s2))
    cur[0] += 2
    refs["status"] = (s1, s2)

    # device type block
    tlow = lambda s: (s or "").strip().lower()
    type_counts, matrix = {}, {}
    for d in devs:
        k = tlow(d["category_name"])
        type_counts[k] = type_counts.get(k, 0) + 1
        matrix[(k, d["status"])] = matrix.get((k, d["status"]), 0) + 1
    order = sorted(type_names, key=lambda n: (-type_counts.get(n.lower(), 0), n.lower()))
    sec("By device type", 8)
    th = hdr(["Device type", "Devices", "Share"] + STATUSES)
    t1 = cur[0]
    ttot = t1 + len(order) + 1
    for nm in order:
        r = cur[0]
        cnt = type_counts.get(nm.lower(), 0)
        st.put(r, 1, nm, s_txt)
        st.put(r, 2, cnt, s_int, "COUNTIFS(Devices[Device type],$A%d)" % r)
        st.put(r, 3, (cnt / N) if N else 0, s_pct, "IF($B$%d=0,0,B%d/$B$%d)" % (ttot, r, ttot))
        for j, sname in enumerate(STATUSES):
            col = 4 + j
            st.put(r, col, matrix.get((nm.lower(), sname), 0), s_int,
                   "COUNTIFS(Devices[Device type],$A%d,Devices[Status],%s$%d)" % (r, xcol(col), th))
        cur[0] += 1
    r = cur[0]
    listed = sum(type_counts.get(nm.lower(), 0) for nm in order)
    st.put(r, 1, "Other / not listed", s_txt)
    st.put(r, 2, N - listed, s_int, "COUNTA(Devices[Tag])-SUM(B%d:B%d)" % (t1, r - 1) if order else "COUNTA(Devices[Tag])")
    st.put(r, 3, ((N - listed) / N) if N else 0, s_pct, "IF($B$%d=0,0,B%d/$B$%d)" % (ttot, r, ttot))
    for j, sname in enumerate(STATUSES):
        col = 4 + j
        lst = sum(matrix.get((nm.lower(), sname), 0) for nm in order)
        st.put(r, col, stat_counts[sname] - lst, s_int,
               ("COUNTIFS(Devices[Status],%s$%d)-SUM(%s%d:%s%d)" % (xcol(col), th, xcol(col), t1, xcol(col), r - 1)) if order
               else "COUNTIFS(Devices[Status],%s$%d)" % (xcol(col), th))
    t2 = r
    cur[0] += 1
    r = cur[0]
    st.put(r, 1, "Total", s_tt)
    st.put(r, 2, N, s_ti, "SUM(B%d:B%d)" % (t1, t2))
    st.put(r, 3, 1 if N else 0, s_tp, "SUM(C%d:C%d)" % (t1, t2))
    for j, sname in enumerate(STATUSES):
        col = 4 + j
        st.put(r, col, stat_counts[sname], s_ti, "SUM(%s%d:%s%d)" % (xcol(col), t1, xcol(col), t2))
    cur[0] += 2
    type_labels = order + ["Other / not listed"]
    type_vals = [type_counts.get(nm.lower(), 0) for nm in order] + [N - listed]
    type_status_vals = {sname: [matrix.get((nm.lower(), sname), 0) for nm in order] +
                               [stat_counts[sname] - sum(matrix.get((nm.lower(), sname), 0) for nm in order)] for sname in STATUSES}
    refs["type"] = (t1, t2, th)
    types_in_use = sum(1 for nm in order if type_counts.get(nm.lower(), 0) > 0)

    def group_block(title, head, field, col_name, topn, notset=True, other="Other", share_head="Share"):
        top = _xl_top([d[field] for d in devs], topn)
        sec(title, 3)
        hdr([head, "Devices", share_head])
        g1 = cur[0]
        gtot = g1 + len(top) + 1 + (1 if notset else 0)
        for nm, cnt in top:
            r = cur[0]
            st.put(r, 1, nm, s_txt)
            st.put(r, 2, cnt, s_int, "COUNTIFS(Devices[%s],$A%d)" % (col_name, r))
            cur[0] += 1
        blanks = sum(1 for d in devs if not (d[field] or "").strip())
        nonblank = N - blanks
        topsum = sum(c for _, c in top)
        r = cur[0]
        base = "COUNTIF(Devices[%s],\"?*\")" % col_name if not notset else "COUNTA(Devices[Tag])-COUNTIFS(Devices[%s],\"\")" % col_name
        st.put(r, 1, other, s_txt)
        st.put(r, 2, nonblank - topsum, s_int, ("%s-SUM(B%d:B%d)" % (base, g1, r - 1)) if top else base)
        cur[0] += 1
        if notset:
            r = cur[0]
            st.put(r, 1, "(not set)", s_txt)
            st.put(r, 2, blanks, s_int, "COUNTIFS(Devices[%s],\"\")" % col_name)
            cur[0] += 1
        g2 = cur[0] - 1
        denom = nonblank + (blanks if notset else 0)
        vals = [c for _, c in top] + [nonblank - topsum] + ([blanks] if notset else [])
        labels = [nm for nm, _ in top] + [other] + (["(not set)"] if notset else [])
        for k_, rr in enumerate(range(g1, g2 + 1)):
            st.put(rr, 3, (vals[k_] / denom) if denom else 0, s_pct, "IF($B$%d=0,0,B%d/$B$%d)" % (gtot, rr, gtot))
        r = cur[0]
        st.put(r, 1, "Total", s_tt)
        st.put(r, 2, denom, s_ti, "SUM(B%d:B%d)" % (g1, g2))
        st.put(r, 3, 1 if denom else 0, s_tp, "SUM(C%d:C%d)" % (g1, g2))
        cur[0] += 2
        return g1, g2, labels, vals

    loc = group_block("By location", "Location", "location", "Location", 12, True, "Other locations")
    dep = group_block("By department", "Department", "department", "Department", 10, True, "Other departments")
    brd = group_block("By brand", "Brand", "brand", "Brand", 10, True, "Other brands")
    ppl = group_block("Devices held by person", "Assigned to", "assigned_to", "Assigned to", 10, False, "Others", "Share of assigned")

    # monthly additions
    base = today.replace(day=1)
    sec("Devices added per month (last 12 months)", 3)
    hdr(["Month", "Devices added"])
    m1 = cur[0]
    mon_ser, mon_vals = [], []
    for k in range(11, -1, -1):
        start, end = _add_months(base, -k), _add_months(base, -(k - 1))
        s_ser = xserial(datetime(start.year, start.month, 1))
        e_ser = xserial(datetime(end.year, end.month, 1))
        cnt = sum(1 for c in created if c is not None and s_ser <= c < e_ser)
        r = cur[0]
        st.put(r, 1, s_ser, s_mon, "EDATE(DATE(YEAR(TODAY()),MONTH(TODAY()),1),-%d)" % k)
        st.put(r, 2, cnt, s_int, 'COUNTIFS(Devices[Created],">="&A%d,Devices[Created],"<"&EDATE(A%d,1))' % (r, r))
        mon_ser.append(s_ser)
        mon_vals.append(cnt)
        cur[0] += 1
    m2 = cur[0] - 1
    r = cur[0]
    st.put(r, 1, "Total", s_tt)
    st.put(r, 2, sum(mon_vals), s_ti, "SUM(B%d:B%d)" % (m1, m2))
    cur[0] += 2

    # purchases per year
    sec("Devices purchased per year", 3)
    hdr(["Year", "Devices purchased"])
    y1 = cur[0]
    yr_labels, yr_vals = [], []
    for k in range(7, -1, -1):
        yr = today.year - k
        lo, hi = xserial(datetime(yr, 1, 1)), xserial(datetime(yr + 1, 1, 1))
        cnt = sum(1 for p in purch if p is not None and lo <= p < hi)
        r = cur[0]
        st.put(r, 1, yr, s_year, "YEAR(TODAY())-%d" % k)
        st.put(r, 2, cnt, s_int, 'COUNTIFS(Devices[Purchase date],">="&DATE(A%d,1,1),Devices[Purchase date],"<"&DATE(A%d+1,1,1))' % (r, r))
        yr_labels.append(yr)
        yr_vals.append(cnt)
        cur[0] += 1
    y2 = cur[0] - 1
    first_lo = xserial(datetime(today.year - 7, 1, 1))
    older = sum(1 for p in purch if p is not None and p < first_lo)
    r = cur[0]
    st.put(r, 1, "Earlier years", s_txt)
    st.put(r, 2, older, s_int, 'COUNTIFS(Devices[Purchase date],"<"&DATE(A%d,1,1))' % y1)
    cur[0] += 1
    r = cur[0]
    nodate = N - sum(1 for p in purch if p is not None)
    st.put(r, 1, "No purchase date", s_txt)
    st.put(r, 2, nodate, s_int, "COUNTA(Devices[Tag])-COUNT(Devices[Purchase date])")
    cur[0] += 1
    r = cur[0]
    st.put(r, 1, "Total", s_tt)
    st.put(r, 2, sum(yr_vals) + older + nodate, s_ti, "SUM(B%d:B%d)" % (y1, r - 1))

    # ---------------------------------------------------------------- Summary sheet
    sm = XSheet(bk, "Summary")
    for c in range(1, 13):
        sm.widths[c] = 13.5
    sm.put(1, 1, "BluePeak TAG Master", s_title)
    sm.heights[1] = 38
    sm.put(2, 1, "Inventory summary", s_h2)
    sm.put(2, 7, "Exported %s by %s" % (now.strftime("%d %b %Y, %H:%M"), (who or {}).get("full_name") or (who or {}).get("username") or "the app"), s_sub)
    sm.merge(2, 7, 2, 12)
    sm.heights[2] = 24
    sm.put(3, 1, "The figures and charts on the Summary and Stats sheets are live formulas over the Inventory sheet. Edit, add or delete rows "
                 "there and everything refreshes by itself. To bring in newer data from the app, export again.", s_note)
    sm.merge(3, 1, 3, 12)
    sm.heights[3] = 32
    created_ok = [c for c in created if c is not None]
    new30 = sum(1 for c in created_ok if c >= xserial(datetime(today.year, today.month, today.day)) - 30)
    newyr = sum(1 for c in created_ok if c >= xserial(datetime(today.year, 1, 1)))
    no_serial = N - sum(1 for d in devs if (d["serial_number"] or "").strip())
    no_loc = sum(1 for d in devs if not (d["location"] or "").strip())
    tl, tr = refs["type"][0], refs["type"][1] - 1

    def tile(top, i, label, val, f, color, fmt, sub):
        c = 2 * i + 1
        wb_ = (("lr", "thick", "FFFFFF"),)
        sl = S.get(bold=True, size=10, color=MUTED, fill=SOFT, h="center", v="center", border=wb_)
        sv = S.get(bold=True, size=26, color=color, fill=SOFT, h="center", v="center", fmt=fmt, border=wb_)
        ss = S.get(size=9, color=MUTED, fill=SOFT, h="center", v="center", border=wb_ + (("b", "medium", color),))
        sm.put(top, c, label, sl)
        sm.fill(top, c + 1, c + 1, sl)
        sm.merge(top, c, top, c + 1)
        sm.put(top + 1, c, val, sv, f)
        sm.fill(top + 1, c + 1, c + 1, sv)
        sm.merge(top + 1, c, top + 1, c + 1)
        sm.put(top + 2, c, sub, ss)
        sm.fill(top + 2, c + 1, c + 1, ss)
        sm.merge(top + 2, c, top + 2, c + 1)
        sm.heights[top] = 22
        sm.heights[top + 1] = 42
        sm.heights[top + 2] = 20

    cnt_f = lambda s: 'COUNTIFS(Devices[Status],"%s")' % s
    tile(5, 0, "Total devices", N, "COUNTA(Devices[Tag])", NAVY, "int", "in the inventory")
    tile(5, 1, "In stock", stat_counts["In stock"], cnt_f("In stock"), "1F4FD8", "int", "ready to issue")
    tile(5, 2, "Assigned", stat_counts["Assigned"], cnt_f("Assigned"), "16794E", "int", "in use")
    tile(5, 3, "In repair", stat_counts["In repair"], cnt_f("In repair"), "D98A00", "int", "being fixed")
    tile(5, 4, "Lost", stat_counts["Lost"], cnt_f("Lost"), "B4162B", "int", "missing")
    tile(5, 5, "Retired", stat_counts["Retired"], cnt_f("Retired"), "8A94B0", "int", "taken out of use")
    tile(9, 0, "Assigned rate", (stat_counts["Assigned"] / N) if N else 0, "IF(A6=0,0,E6/A6)", NAVY, "pct", "assigned / total")
    tile(9, 1, "Added in 30 days", new30, 'COUNTIFS(Devices[Created],">="&(TODAY()-30))', NAVY, "int", "new devices")
    tile(9, 2, "Added this year", newyr, 'COUNTIFS(Devices[Created],">="&DATE(YEAR(TODAY()),1,1))', NAVY, "int", "since 1 January")
    tile(9, 3, "No serial number", no_serial, 'COUNTA(Devices[Tag])-COUNTIF(Devices[Serial number],"?*")', "D98A00", "int", "need one")
    tile(9, 4, "No location", no_loc, 'COUNTIFS(Devices[Location],"")', "D98A00", "int", "need one")
    tile(9, 5, "Device types in use", types_in_use, "SUMPRODUCT(--(Stats!$B$%d:$B$%d>0))" % (tl, tr), NAVY, "int", "with devices")
    sm.put(13, 1, "Charts", s_sec)
    sm.fill(13, 2, 12, s_sec)
    sm.heights[13] = 24
    sm.put(71, 1, "Data source: the Inventory sheet (table name Devices). Dates and times are UTC.", s_note)
    sm.merge(71, 1, 71, 12)

    def rng(col, a, b):
        return "Stats!$%s$%d:$%s$%d" % (col, a, col, b)

    s1_, s2_ = refs["status"]
    t1_, t2_, th_ = refs["type"]
    charts = [
        chart_xml("bar", "Devices by type", (rng("A", t1_, t2_), type_labels, False, "General"),
                  [("Devices", None, rng("B", t1_, t2_), type_vals, NAVY)], horizontal=True),
        chart_xml("doughnut", "Devices by status", (rng("A", s1_, s2_), list(STATUSES), False, "General"),
                  [("Devices", None, rng("B", s1_, s2_), [stat_counts[s] for s in STATUSES], [STATUS_HEX[s] for s in STATUSES])],
                  legend="r"),
        chart_xml("bar", "Status of each device type", (rng("A", t1_, t2_), type_labels, False, "General"),
                  [(s, "Stats!$%s$%d" % (xcol(4 + j), th_), rng(xcol(4 + j), t1_, t2_), type_status_vals[s], STATUS_HEX[s])
                   for j, s in enumerate(STATUSES)], horizontal=True, stacked=True, legend="b", labels=False),
        chart_xml("bar", "Devices by location", (rng("A", loc[0], loc[1]), loc[2], False, "General"),
                  [("Devices", None, rng("B", loc[0], loc[1]), loc[3], "3D5AFE")], horizontal=True),
        chart_xml("bar", "Devices added per month", (rng("A", m1, m2), mon_ser, True, "mmm yy"),
                  [("Devices added", None, rng("B", m1, m2), mon_vals, NAVY)]),
        chart_xml("bar", "Devices purchased per year", (rng("A", y1, y2), yr_labels, True, "General"),
                  [("Devices purchased", None, rng("B", y1, y2), yr_vals, "C8102E")]),
    ]
    anchors = []
    for k in range(6):
        row0 = 13 + 19 * (k // 2)
        anchors.append((0 if k % 2 == 0 else 6, row0, 6 if k % 2 == 0 else 12, row0 + 18))

    # ---------------------------------------------------------------- package
    tbl = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'id="1" name="Devices" displayName="Devices" ref="A1:N%d" totalsRowShown="0"><autoFilter ref="A1:N%d"/><tableColumns count="14">%s</tableColumns>'
           '<tableStyleInfo name="TableStyleLight1" showFirstColumn="0" showLastColumn="0" showRowStripes="1" showColumnStripes="0"/></table>'
           % (last, last, "".join('<tableColumn id="%d" name="%s"/>' % (i, xesc(hd)) for i, hd in enumerate(heads, start=1))))
    RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheets_xml = {
        "xl/worksheets/sheet1.xml": sm.xml(tab_color="13296B", grid=False, fit=True, landscape=True, drawing="rId1", selected=True),
        "xl/worksheets/sheet2.xml": st.xml(tab_color="3D5AFE", grid=False, fit=True),
        "xl/worksheets/sheet3.xml": inv.xml(tab_color="C8102E", freeze=1, table="rId1", extra=cf + dv),
    }
    files = dict(sheets_xml)
    files["xl/worksheets/_rels/sheet1.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="%s/drawing" Target="../drawings/drawing1.xml"/></Relationships>' % RT)
    files["xl/worksheets/_rels/sheet3.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="%s/table" Target="../tables/table1.xml"/></Relationships>' % RT)
    files["xl/tables/table1.xml"] = tbl
    files["xl/drawings/drawing1.xml"] = drawing_xml(anchors)
    files["xl/drawings/_rels/drawing1.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>'
        % "".join('<Relationship Id="rId%d" Type="%s/chart" Target="../charts/chart%d.xml"/>' % (i, RT, i) for i in range(1, 7)))
    for i, cx in enumerate(charts, start=1):
        files["xl/charts/chart%d.xml" % i] = cx
    files["xl/styles.xml"] = S.xml()
    files["xl/sharedStrings.xml"] = bk.sst_xml()
    files["xl/workbook.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="%s"><workbookPr/><bookViews><workbookView activeTab="0"/></bookViews><sheets>'
        '<sheet name="Summary" sheetId="1" r:id="rId1"/><sheet name="Stats" sheetId="2" r:id="rId2"/><sheet name="Inventory" sheetId="3" r:id="rId3"/>'
        '</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>' % RT)
    files["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="%s/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="%s/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="%s/worksheet" Target="worksheets/sheet3.xml"/><Relationship Id="rId4" Type="%s/styles" Target="styles.xml"/>'
        '<Relationship Id="rId5" Type="%s/sharedStrings" Target="sharedStrings.xml"/></Relationships>' % ((RT,) * 5))
    files["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="%s/extended-properties" Target="docProps/app.xml"/></Relationships>' % (RT, RT))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files["docProps/core.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:title>BluePeak TAG Master inventory</dc:title><dc:creator>BluePeak TAG Master</dc:creator>'
        '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created></cp:coreProperties>' % stamp)
    files["docProps/app.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>BluePeak TAG Master</Application></Properties>')
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    for i in range(1, 4):
        ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i)
    ct.append('<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
              '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
              '<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>'
              '<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>')
    for i in range(1, 7):
        ct.append('<Override PartName="/xl/charts/chart%d.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>' % i)
    ct.append('<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
              '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>')
    files["[Content_Types].xml"] = "".join(ct)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        order_ = ["[Content_Types].xml", "_rels/.rels"] + [k for k in files if k not in ("[Content_Types].xml", "_rels/.rels")]
        for name in order_:
            zf.writestr(name, files[name].encode("utf-8") if isinstance(files[name], str) else files[name])
    return buf.getvalue(), "BluePeak-inventory-%s.xlsx" % now_utc().strftime("%Y%m%d-%H%M"), N


# --------------------------------------------------------------------------
# Snapshots: rebuild a device as it was at any point in its history, and restore it
# --------------------------------------------------------------------------
STATE_ORDER = ["tag", "category_name", "brand", "model", "serial_number", "mac_address", "status",
               "assigned_to", "department", "location", "purchase_date", "warranty_end", "notes"]
LABEL_TO_KEY = {"Tag": "tag", "Device type": "category_name", "Brand": "brand", "Model": "model",
                "Serial number": "serial_number", "MAC address": "mac_address", "Status": "status",
                "Assigned to": "assigned_to", "Department": "department", "Location": "location",
                "Purchase date": "purchase_date", "Warranty ends": "warranty_end", "Notes": "notes"}
STATE_LABELS = {k: v for v, k in LABEL_TO_KEY.items()}
EDIT_ACTIONS = ("Updated", "Assigned", "Returned", "Status changed", "Tag changed", "Restored")


def current_state(con, did):
    r = con.execute(DEVICE_SELECT + " WHERE d.id=?", (did,)).fetchone()
    return {k: (r[k] or "") for k in STATE_ORDER} if r else None


def state_at(con, did, entry_id, when):
    """Rebuild the device as it was just before or just after one history entry, by undoing later changes."""
    rows = con.execute("SELECT id,action,changes FROM activity WHERE device_id=? ORDER BY id DESC", (did,)).fetchall()
    if not any(r["id"] == entry_id for r in rows):
        raise ApiError(404, "That history entry does not belong to this device.")
    state = current_state(con, did)
    for r in rows:
        if r["id"] < entry_id or (r["id"] == entry_id and when == "after"):
            break
        ch = parse_changes(r["changes"])
        act = r["action"]
        if act == "Deleted":
            if not ch:
                return None, "This deletion was recorded without details, so the record cannot be rebuilt."
            state = {k: "" for k in STATE_ORDER}
            for c in ch:
                k = LABEL_TO_KEY.get(c.get("f"))
                if k:
                    state[k] = c.get("o") or ""
        elif act == "Created" or any(c.get("f") == "Record" and c.get("n") == "restored" for c in ch):
            state = None
        elif ch:
            if state is None:
                return None, "The history for this device has a gap, so that version cannot be rebuilt."
            for c in ch:
                k = LABEL_TO_KEY.get(c.get("f"))
                if k:
                    state[k] = c.get("o") or ""
        elif act in EDIT_ACTIONS:
            return None, "This part of the history was recorded before change details were kept, so it cannot be rebuilt."
    if state is None:
        return None, "The device did not exist at that point."
    return state, ""


def snapshot_diff(snap, cur):
    out = []
    for k in STATE_ORDER:
        then = snap.get(k, "") if snap else ""
        now = cur.get(k, "") if cur else ""
        out.append({"key": k, "label": STATE_LABELS[k], "then": then, "now": now, "changed": then != now})
    return out


def _snap_args(src_):
    try:
        return int(src_.get("device")), int(src_.get("audit")), ("before" if src_.get("when") == "before" else "after")
    except (TypeError, ValueError):
        raise ApiError(400, "Choose a history entry first.")


def api_snapshot(h, con, q):
    did, aid, when = _snap_args({"device": g(q, "device"), "audit": g(q, "audit"), "when": g(q, "when")})
    entry = con.execute("SELECT id,ts,user,role,action,details,tag,device_id FROM activity WHERE id=?", (aid,)).fetchone()
    if not entry or entry["device_id"] != did:
        raise ApiError(404, "That history entry was not found.")
    cur = current_state(con, did)
    snap, reason = state_at(con, did, aid, when)
    out = {"device_id": did, "tag": (cur or {}).get("tag") or entry["tag"], "when": when, "entry": dict(entry),
           "exists": cur is not None, "available": snap is not None, "reason": reason, "snapshot": snap, "current": cur,
           "diff": snapshot_diff(snap, cur) if snap else [], "category_missing": False}
    out["has_changes"] = bool(snap) and (cur is None or any(snap[k] != cur[k] for k in STATE_ORDER))
    if snap and snap["category_name"] and not con.execute(
            "SELECT 1 FROM categories WHERE name=? COLLATE NOCASE", (snap["category_name"],)).fetchone():
        out["category_missing"] = True
    return out


def api_snapshot_restore(h, con, q):
    did, aid, when = _snap_args(h.body())
    with tx(con):
        entry = con.execute("SELECT id,ts,user,action,device_id FROM activity WHERE id=?", (aid,)).fetchone()
        if not entry or entry["device_id"] != did:
            raise ApiError(404, "That history entry was not found.")
        snap, reason = state_at(con, did, aid, when)
        cur = current_state(con, did)
        if snap is None:
            if cur is not None and reason.startswith("The device did not exist"):
                old = con.execute(DEVICE_SELECT + " WHERE d.id=?", (did,)).fetchone()
                con.execute("DELETE FROM devices WHERE id=?", (did,))
                aid2 = log(con, "Deleted", "%s removed again by undoing a restore." % old["category_name"], did,
                           old["tag"], "device", snapshot(old), category=old["category_name"])
                return {"ok": True, "audit_id": aid2, "removed": True}
            raise ApiError(400, reason or "That version cannot be restored.")
        cat = con.execute("SELECT * FROM categories WHERE name=? COLLATE NOCASE", (snap["category_name"],)).fetchone()
        if not cat:
            raise ApiError(409, "The device type %s no longer exists. Add it back in Settings first." % snap["category_name"])
        tag = clean_tag(snap["tag"])
        f = clean_fields({k: snap[k] for k in FIELD_KEYS})
        if con.execute("SELECT 1 FROM devices WHERE tag=? AND id<>?", (tag, did)).fetchone():
            raise ApiError(409, "The tag %s now belongs to another device, so this version cannot be restored." % tag)
        check_serial(con, f["serial_number"], did)
        newstate = {"tag": tag, "category_name": cat["name"]}
        newstate.update(f)
        label = "%s the %s by %s on %s UTC" % ("after" if when == "after" else "before",
                                               entry["action"], entry["user"] or "system", entry["ts"])
        if cur is None:
            cols = ["id", "tag", "category_id"] + list(f.keys()) + ["serial_lower"]
            con.execute("INSERT INTO devices(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                        [did, tag, cat["id"]] + list(f.values()) + [f["serial_number"].lower()])
            changes = [{"f": "Record", "o": "deleted", "n": "restored"}] + \
                      [{"f": STATE_LABELS[k], "o": "", "n": newstate[k]} for k in STATE_ORDER if newstate[k]]
            aid2 = log(con, "Restored", "Deleted device brought back, as it was %s." % label, did, tag, "device",
                       changes, category=cat["name"])
            return {"ok": True, "audit_id": aid2, "recreated": True}
        changes = [{"f": STATE_LABELS[k], "o": cur[k], "n": newstate[k]} for k in STATE_ORDER if cur[k] != newstate[k]]
        if not changes:
            raise ApiError(400, "Nothing to restore: the record already matches that version.")
        con.execute("UPDATE devices SET tag=?, category_id=?, " + ",".join("%s=?" % k for k in f) +
                    ", serial_lower=?, updated_at=datetime('now') WHERE id=?",
                    [tag, cat["id"]] + list(f.values()) + [f["serial_number"].lower(), did])
        aid2 = log(con, "Restored", "Restored the version %s. %s" % (label, change_text(changes)), did, tag, "device",
                   changes, category=cat["name"])
    return {"ok": True, "audit_id": aid2}


def api_labels_log(h, con, q):
    d = h.body()
    try:
        n = max(0, int(d.get("count", 0)))
    except (TypeError, ValueError):
        n = 0
    first, last = str(d.get("first", ""))[:60], str(d.get("last", ""))[:60]
    log(con, "Printed", "Sent %d label%s to the printer (%s)" % (n, "" if n == 1 else "s",
                                                                  first if n == 1 else "%s to %s" % (first, last)),
        None, first if n == 1 else "", "labels")
    return {"ok": True}


def csv_safe(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def tz_mod(q):
    try:
        tz = max(-840, min(840, int(g(q, "tz") or 0)))
    except ValueError:
        tz = 0
    return "%+d minutes" % tz


def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def audit_filters(q):
    where, args = [], []
    tzm = tz_mod(q)
    p = g(q, "parent")
    if p.isdigit():
        where.append("a.parent_id=?")
        args.append(int(p))
    elif g(q, "all") != "1":
        where.append("a.parent_id IS NULL")
    for term in g(q, "q").split():
        like = _like(term)
        where.append("(a.tag LIKE ? ESCAPE '\\' OR a.details LIKE ? ESCAPE '\\' OR a.action LIKE ? ESCAPE '\\' "
                     "OR a.user LIKE ? ESCAPE '\\' OR a.changes LIKE ? ESCAPE '\\' "
                     "OR a.device_id IN (SELECT id FROM devices WHERE tag LIKE ? ESCAPE '\\'))")
        args.extend([like] * 6)
    if g(q, "tag"):
        pre = _like(g(q, "tag").upper())[1:]
        where.append("(a.tag LIKE ? ESCAPE '\\' OR a.device_id IN (SELECT id FROM devices WHERE tag LIKE ? ESCAPE '\\'))")
        args.extend([pre, pre])
    for key, col in (("user", "a.user"), ("role", "a.role"), ("action", "a.action"),
                     ("entity", "a.entity"), ("category", "a.category")):
        if g(q, key):
            where.append(col + "=?")
            args.append(g(q, key))
    acts = [x.strip() for x in g(q, "actions").split(",") if x.strip()]
    if acts:
        where.append("a.action IN (%s)" % ",".join("?" * len(acts)))
        args.extend(acts)
    ts_ok = re.compile(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?")
    if ts_ok.fullmatch(g(q, "from_ts")):
        where.append("a.ts>=?")
        args.append(g(q, "from_ts"))
    if ts_ok.fullmatch(g(q, "to_ts")):
        where.append("a.ts<=?")
        args.append(g(q, "to_ts"))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", g(q, "from")):
        where.append("date(a.ts)>=?")
        args.append(g(q, "from"))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", g(q, "to")):
        where.append("date(a.ts)<=?")
        args.append(g(q, "to"))
    if re.fullmatch(r"\d{4}", g(q, "year")):
        where.append("strftime('%Y', datetime(a.ts, ?))=?")
        args.extend([tzm, g(q, "year")])
    if re.fullmatch(r"(0?[1-9]|1[0-2])", g(q, "month")):
        where.append("strftime('%m', datetime(a.ts, ?))=?")
        args.extend([tzm, "%02d" % int(g(q, "month"))])
    hm = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
    tf, tt = g(q, "tod_from"), g(q, "tod_to")
    if hm.fullmatch(tf) and hm.fullmatch(tt):
        if tf <= tt:
            where.append("strftime('%H:%M', datetime(a.ts, ?)) BETWEEN ? AND ?")
            args.extend([tzm, tf, tt])
        else:
            where.append("(strftime('%H:%M', datetime(a.ts, ?)) >= ? OR strftime('%H:%M', datetime(a.ts, ?)) <= ?)")
            args.extend([tzm, tf, tzm, tt])
    elif hm.fullmatch(tf):
        where.append("strftime('%H:%M', datetime(a.ts, ?)) >= ?")
        args.extend([tzm, tf])
    elif hm.fullmatch(tt):
        where.append("strftime('%H:%M', datetime(a.ts, ?)) <= ?")
        args.extend([tzm, tt])
    if g(q, "has_changes") == "1":
        where.append("a.changes<>''")
    return (" WHERE " + " AND ".join(where)) if where else "", args


def api_audit(h, con, q):
    where, args = audit_filters(q)
    total = con.execute("SELECT COUNT(*) FROM activity a" + where, args).fetchone()[0]
    try:
        limit = max(1, min(1000, int(g(q, "limit") or 100)))
    except ValueError:
        limit = 100
    try:
        offset = max(0, int(g(q, "offset") or 0))
    except ValueError:
        offset = 0
    order = "ASC" if g(q, "order") == "asc" else "DESC"
    rows = []
    for r in con.execute(
            "SELECT a.id,a.ts,a.user,a.role,a.action,a.entity,a.tag,a.details,a.changes,a.ip,a.device_id,a.category,"
            "(SELECT COUNT(*) FROM activity c WHERE c.parent_id=a.id) AS children "
            "FROM activity a" + where + " ORDER BY a.id " + order + " LIMIT ? OFFSET ?",
            args + [limit, offset]):
        d = dict(r)
        d["changes"] = parse_changes(d["changes"])
        rows.append(d)
    tzm = tz_mod(q)
    if g(q, "facets") == "0":
        facets = {"users": [], "actions": [], "categories": [], "years": []}
    else:
        facets = {
            "users": [r[0] for r in con.execute(
                "SELECT DISTINCT user FROM activity WHERE user<>'' ORDER BY user COLLATE NOCASE")],
            "actions": [r[0] for r in con.execute("SELECT DISTINCT action FROM activity ORDER BY action")],
            "categories": [r[0] for r in con.execute(
                "SELECT DISTINCT category FROM activity WHERE category<>'' ORDER BY category COLLATE NOCASE")],
            "years": [r[0] for r in con.execute(
                "SELECT DISTINCT strftime('%Y', datetime(ts, ?)) AS y FROM activity ORDER BY y DESC", (tzm,))],
        }
    return {"rows": rows, "total": total, "limit": limit, "offset": offset, **facets}


def audit_csv(con, q):
    where, args = audit_filters(q)
    order = "ASC" if g(q, "order") == "asc" else "DESC"
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Time (UTC)", "User", "Role", "Action", "Area", "Device type", "Tag", "Details",
                "Changes (field: before -> after)", "Computer"])
    for r in con.execute("SELECT a.* FROM activity a" + where + " ORDER BY a.id " + order, args):
        ch = "; ".join("%s: %s -> %s" % (c.get("f"), c.get("o"), c.get("n")) for c in parse_changes(r["changes"]))
        w.writerow([r["ts"], csv_safe(r["user"]), r["role"], r["action"], r["entity"], csv_safe(r["category"]),
                    csv_safe(r["tag"]), csv_safe(r["details"]), csv_safe(ch), r["ip"]])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


SETTING_LABELS = {"prefix": "Tag prefix", "sep": "Separator", "pad": "Number length", "include_year": "Year in tag",
                  "label_footer": "Label footer text", "label_size": "Default label size",
                  "label_brand": "Name on labels", "label_paper": "Default paper", "label_code": "Code on labels"}


def api_settings_put(h, con, q):
    d = h.body()
    old = get_settings(con)
    prefix = str(d.get("prefix", old["prefix"])).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{0,12}", prefix):
        raise ApiError(400, "Prefix can use letters and numbers only, up to 12 characters.")
    sep = str(d.get("sep", old["sep"]))
    if sep not in ("-", "_", ".", "/", ""):
        raise ApiError(400, "Separator must be -, _, . or / or empty.")
    try:
        pad = int(d.get("pad", old["pad"]))
    except (TypeError, ValueError):
        raise ApiError(400, "Number length must be between 1 and 10.")
    if not 1 <= pad <= 10:
        raise ApiError(400, "Number length must be between 1 and 10.")
    size = d.get("label_size", old["label_size"])
    if size not in ("sm", "md", "lg"):
        size = "md"
    paper = d.get("label_paper", old["label_paper"])
    if paper not in ("a4", "letter"):
        paper = "a4"
    code = d.get("label_code", old["label_code"])
    if code not in ("barcode", "qr"):
        code = "barcode"
    new = {"prefix": prefix, "sep": sep, "pad": str(pad),
           "include_year": ("1" if d.get("include_year") else "0") if "include_year" in d else old["include_year"],
           "label_footer": str(d.get("label_footer", old["label_footer"]))[:60], "label_size": size,
           "label_brand": str(d.get("label_brand", old["label_brand"])).strip()[:24], "label_paper": paper,
           "label_code": code}
    changes = []
    for k, v in new.items():
        if str(old.get(k, "")) != str(v):
            ov, nv = old.get(k, ""), v
            if k == "include_year":
                ov, nv = ("yes" if ov == "1" else "no"), ("yes" if nv == "1" else "no")
            if k == "label_code":
                ov, nv = ("QR code" if ov == "qr" else "Barcode"), ("QR code" if nv == "qr" else "Barcode")
            changes.append({"f": SETTING_LABELS[k], "o": str(ov), "n": str(nv)})
    with tx(con):
        for k, v in new.items():
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        if changes:
            log(con, "Settings", change_text(changes), None, "", "settings", changes)
    return {"settings": settings_out(get_settings(con))}


# --------------------------------------------------------------------------
# SQLite Cloud
# --------------------------------------------------------------------------
_cloud_lock = threading.Lock()
_cloud_first_done = threading.Event()


def cloud_config(con):
    st = get_settings(con)
    try:
        auto = int(st.get("cloud_auto") or 0)
    except ValueError:
        auto = 0
    if str(auto) not in AUTO_SYNC_CHOICES:
        auto = 0
    return {"url": st.get("cloud_url", ""), "apikey": st.get("cloud_apikey", ""),
            "database": st.get("cloud_database", ""), "enabled": st.get("cloud_enabled") == "1",
            "start": st.get("cloud_start") == "1", "auto": auto, "fp": st.get("cloud_fp", "")}


def mask_url(url):
    return re.sub(r"(?i)(apikey=|token=)[^&\"]+", r"\1***", str(url or ""))


def _put_settings(con, values):
    with tx(con):
        for k, v in values.items():
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def cloud_out(con):
    st = get_settings(con)
    cfg = cloud_config(con)
    try:
        local_fp = bluepeak_cloud.fingerprint(DB_PATH)
        local_counts = bluepeak_cloud.counts(DB_PATH)
    except Exception as e:
        write_log("Cloud status failed: %s" % e)
        local_fp, local_counts = "", {}
    return {"enabled": cfg["enabled"], "url": mask_url(cfg["url"]), "database": cfg["database"],
            "has_key": bool(cfg["apikey"]), "auto": cfg["auto"], "start": cfg["start"],
            "last_push": st.get("cloud_last_push", ""), "last_pull": st.get("cloud_last_pull", ""),
            "last_error": st.get("cloud_last_error", ""), "last_error_at": st.get("cloud_last_error_at", ""),
            "pending": bool(local_fp != (cfg["fp"] or "")), "counts": local_counts}


def _cfg_from(con, d):
    cfg = cloud_config(con)
    if d.get("url") is not None:
        cfg["url"] = str(d.get("url") or "").strip()[:400]
    if d.get("database") is not None:
        cfg["database"] = str(d.get("database") or "").strip()[:80]
    if d.get("apikey"):
        cfg["apikey"] = str(d.get("apikey") or "").strip()[:400]
    return cfg


def _sync_error(kind, err):
    msg = str(err).strip()[:400]
    write_log("SQLite Cloud %s failed: %s" % (kind, msg))
    con = connect()
    try:
        _put_settings(con, {"cloud_last_error": msg,
                            "cloud_last_error_at": now_utc().strftime("%Y-%m-%d %H:%M:%S")})
    finally:
        con.close()


def _backup_local(tag):
    folder = os.path.join(BASE_DIR, "backups")
    try:
        os.makedirs(folder, exist_ok=True)
        name = "%s-%s.db" % (tag, now_utc().strftime("%Y%m%d-%H%M%S"))
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(make_backup())
        files = sorted((os.path.join(folder, n) for n in os.listdir(folder)
                        if n.startswith(tag + "-") and n.endswith(".db")), key=os.path.getmtime)
        for old in files[:-5]:
            os.remove(old)
        return name
    except Exception as e:
        write_log("Backup %s failed: %s" % (tag, e))
        return None


def _prune_backups(folder, keep=7):
    try:
        files = sorted((os.path.join(folder, n) for n in os.listdir(folder)
                        if n.startswith("auto-") and n.endswith(".db")), key=os.path.getmtime)
        for old in files[:-keep]:
            os.remove(old)
    except OSError:
        pass


def scheduled_backup():
    """Write a rotating nightly copy of the database to the backups folder."""
    con = connect()
    try:
        st = get_settings(con)
        last = st.get("backup_last", "")
    finally:
        con.close()
    today = now_utc().strftime("%Y-%m-%d")
    if last[:10] == today:
        return
    folder = os.path.join(BASE_DIR, "backups")
    try:
        os.makedirs(folder, exist_ok=True)
        name = "auto-%s.db" % now_utc().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(make_backup())
        _prune_backups(folder)
        con = connect()
        try:
            _put_settings(con, {"backup_last": now_utc().strftime("%Y-%m-%d %H:%M:%S")})
        finally:
            con.close()
        write_log("Nightly backup written: %s" % name)
    except Exception as e:
        write_log("Nightly backup failed: %s" % e)


def backup_loop():
    while True:
        try:
            scheduled_backup()
        except Exception as e:
            write_log("Backup loop: %s" % e)
        time.sleep(3600)


def cloud_run(kind, cfg, quiet=False):
    """One full sync step: 'push' copies local tables up, 'pull' copies them down."""
    if kind == "pull":
        _backup_local("before-pull")
    with _cloud_lock:
        if kind == "push":
            res = bluepeak_cloud.push(cfg, DB_PATH)
        else:
            res = bluepeak_cloud.pull(cfg, DB_PATH)
    con = connect()
    try:
        if not quiet:
            what = "Pushed" if kind == "push" else "Pulled"
            where = "to" if kind == "push" else "from"
            log(con, "Sync", "%s %d rows %s SQLite Cloud (%s)" % (
                what, res.get("rows", 0), where, ", ".join(res.get("tables", []))))
        if kind == "push":
            # Record the fingerprint of the exact snapshot that was uploaded, so
            # edits made while the upload was in flight are not marked as synced.
            fp = res.get("fingerprint") or bluepeak_cloud.fingerprint(DB_PATH)
        else:
            fp = bluepeak_cloud.fingerprint(DB_PATH)
        _put_settings(con, {
            ("cloud_last_push" if kind == "push" else "cloud_last_pull"): now_utc().strftime("%Y-%m-%d %H:%M:%S"),
            "cloud_fp": fp,
            "cloud_last_error": "", "cloud_last_error_at": ""})
    finally:
        con.close()
    return res


def api_cloud_get(h, con, q):
    return cloud_out(con)


def api_cloud_put(h, con, q):
    d = h.body()
    old = cloud_config(con)
    url = str(d.get("url", old["url"]) or "").strip()[:400]
    if url and not url.startswith(("http://", "https://", "sqlitecloud://")):
        raise ApiError(400, "Enter a sqlitecloud:// connection string or an http(s) address.")
    db = str(d.get("database", old["database"]) or "").strip()[:80]
    if db and not bluepeak_cloud.DB_NAME_RX.match(db):
        raise ApiError(400, "Database name can use letters, numbers, dots, dashes and spaces only.")
    key = str(d.get("apikey") or "").strip()[:400]
    auto = str(d.get("auto", old["auto"]) or "0")
    if auto not in AUTO_SYNC_CHOICES:
        raise ApiError(400, "Automatic push interval is not valid.")
    values = {"cloud_enabled": "1" if d.get("enabled") else "0", "cloud_url": url,
              "cloud_database": db or DEFAULT_SETTINGS["cloud_database"], "cloud_auto": auto,
              "cloud_start": "1" if d.get("start") else "0"}
    if key:
        values["cloud_apikey"] = key
    labels = {"cloud_enabled": "Cloud sync", "cloud_url": "Cloud address",
              "cloud_database": "Cloud database", "cloud_auto": "Automatic push",
              "cloud_start": "Sync when the app starts"}
    old_map = {"cloud_enabled": "1" if old["enabled"] else "0", "cloud_url": old["url"],
               "cloud_database": old["database"], "cloud_auto": str(old["auto"]),
               "cloud_start": "1" if old["start"] else "0"}
    changes = []
    for k, v in values.items():
        if k not in labels:
            continue
        ov, nv = str(old_map.get(k, "")), str(v)
        if ov == nv:
            continue
        if k in ("cloud_enabled", "cloud_start"):
            ov, nv = ("off" if ov == "1" else "on"), ("off" if nv == "1" else "on")
        if k == "cloud_auto":
            ov = "never" if ov == "0" else "every %s seconds" % ov
            nv = "never" if nv == "0" else "every %s seconds" % nv
        if k == "cloud_url":
            ov, nv = mask_url(ov), mask_url(nv)
        changes.append({"f": labels[k], "o": ov[:160], "n": nv[:160]})
    if key:
        changes.append({"f": "SQLite Cloud API key", "o": "set" if old["apikey"] else "empty", "n": "updated"})
    _put_settings(con, values)
    if changes:
        log(con, "Settings", change_text(changes), None, "", "settings", changes)
    return cloud_out(con)


def api_cloud_test(h, con, q):
    try:
        res = bluepeak_cloud.test(_cfg_from(con, h.body()))
    except bluepeak_cloud.CloudError as e:
        raise ApiError(400, str(e))
    res["endpoint"] = mask_url(res.get("endpoint", ""))
    return res


def api_cloud_push(h, con, q):
    try:
        res = cloud_run("push", _cfg_from(con, h.body()))
    except bluepeak_cloud.CloudError as e:
        _sync_error("push", e)
        raise ApiError(400, str(e))
    except Exception as e:
        _sync_error("push", e)
        raise ApiError(500, "Push to SQLite Cloud failed: %s" % e)
    res["ok"] = True
    return res


def api_cloud_pull(h, con, q):
    try:
        res = cloud_run("pull", _cfg_from(con, h.body()))
    except bluepeak_cloud.CloudError as e:
        _sync_error("pull", e)
        raise ApiError(400, str(e))
    except Exception as e:
        _sync_error("pull", e)
        raise ApiError(500, "Pull from SQLite Cloud failed: %s" % e)
    res["ok"] = True
    res["backup"] = True
    return res


# --------------------------------------------------------------------------
# App updates
# --------------------------------------------------------------------------
def _version_key(value):
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in nums[:4]) if nums else (0,)


def _check_update_url(url):
    url = str(url or "").strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ApiError(400, "Enter an https:// address for the update file.")
    if parts.scheme == "http" and (parts.hostname or "").lower() not in ("127.0.0.1", "localhost"):
        raise ApiError(400, "For safety, the update address must use https://.")
    return url


def _fetch_update_manifest(url):
    url = _check_update_url(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "BluePeak-TagMaster/%s" % APP_VERSION, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(1024 * 1024)
    except urllib.error.HTTPError as e:
        raise ApiError(400, "The update server returned HTTP %d." % e.code)
    except Exception as e:
        raise ApiError(400, "Could not reach the update server: %s" % e)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise ApiError(400, "The update file is not valid JSON.")
    if not isinstance(data, dict):
        raise ApiError(400, "The update file is not valid.")
    version = str(data.get("version") or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", version):
        raise ApiError(400, "The update file has no valid version number.")
    package = str(data.get("url") or "").strip()
    if package:
        package = _check_update_url(package)
    digest = str(data.get("sha256") or "").strip().lower()
    if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ApiError(400, "The update file has an invalid SHA-256 checksum.")
    return {"version": version, "url": package, "sha256": digest,
            "notes": str(data.get("notes") or "").strip()[:800]}


def _update_result(con):
    st = get_settings(con)
    latest = st.get("update_latest", "")
    return {"version": APP_VERSION, "url": st.get("update_url", ""),
            "auto": st.get("update_auto", "1") == "1",
            "last_check": st.get("update_last_check", ""),
            "latest": latest, "notes": st.get("update_notes", ""),
            "error": st.get("update_error", ""),
            "available": bool(latest) and _version_key(latest) > _version_key(APP_VERSION)}


def _run_update_check(con, url=None):
    st = get_settings(con)
    feed = str(url if url is not None else st.get("update_url", "")).strip()
    if not feed:
        raise ApiError(400, "Enter the update address in Settings first.")
    feed = _check_update_url(feed)
    try:
        man = _fetch_update_manifest(feed)
    except ApiError as e:
        _put_settings(con, {"update_last_check": now_utc().strftime("%Y-%m-%d %H:%M:%S"),
                            "update_error": str(e)})
        raise
    newer = _version_key(man["version"]) > _version_key(APP_VERSION)
    _put_settings(con, {
        "update_last_check": now_utc().strftime("%Y-%m-%d %H:%M:%S"),
        "update_latest": man["version"] if newer else "",
        "update_notes": man["notes"] if newer else "",
        "update_error": ""})
    return man, newer


def api_update_get(h, con, q):
    return _update_result(con)


def api_update_put(h, con, q):
    d = h.body()
    old = get_settings(con)
    url = str(d.get("url", old.get("update_url", "")) or "").strip()[:500]
    if url:
        url = _check_update_url(url)
    auto = "1" if d.get("auto") else "0"
    if url != old.get("update_url", "") or auto != old.get("update_auto", "1"):
        _put_settings(con, {"update_url": url, "update_auto": auto})
        log(con, "Settings", "App update settings changed", None, "", "settings")
    return _update_result(con)


def api_update_check(h, con, q):
    d = h.body()
    url = d.get("url")
    if url is not None and str(url).strip():
        url = _check_update_url(str(url).strip())
        _put_settings(con, {"update_url": url})
    elif url is not None:
        url = ""
    _run_update_check(con, url)
    return _update_result(con)


def api_update_install(h, con, q):
    st = get_settings(con)
    feed = str(h.body().get("url") or st.get("update_url", "")).strip()
    man = _fetch_update_manifest(feed)
    if _version_key(man["version"]) <= _version_key(APP_VERSION):
        raise ApiError(400, "You are already using the latest version.")
    package = man["url"]
    if not package:
        raise ApiError(400, "The update file does not contain a download address.")
    if not package.lower().split("?")[0].endswith(".exe"):
        raise ApiError(400, "The update package must be a Windows installer (.exe).")
    if not man["sha256"]:
        raise ApiError(400, "The update file has no SHA-256 checksum, so the download cannot be verified.")
    path = os.path.join(BASE_DIR, ".update-%s.exe" % secrets.token_hex(6))
    req = urllib.request.Request(package, headers={
        "User-Agent": "BluePeak-TagMaster/%s" % APP_VERSION})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as fh:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > UPDATE_MAX_BYTES:
                    raise ApiError(400, "The update package is too large.")
                digest.update(chunk)
                fh.write(chunk)
    except ApiError:
        _remove_file(path)
        raise
    except Exception as e:
        _remove_file(path)
        raise ApiError(400, "Could not download the update: %s" % e)
    if total < 1024 or digest.hexdigest() != man["sha256"]:
        _remove_file(path)
        raise ApiError(400, "The downloaded update did not match its checksum. Nothing was installed.")
    log(con, "Settings", "Updating TAG Master from %s to %s" % (APP_VERSION, man["version"]),
        None, "", "settings")
    cmd = 'ping 127.0.0.1 -n 3 >nul & start "" "%s" /SILENT /NORESTART' % path.replace('"', "")
    try:
        subprocess.Popen(["cmd", "/c", cmd], close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        _remove_file(path)
        raise ApiError(500, "The installer could not be started: %s" % e)
    # The installer waits for this process to exit before replacing the program
    # files, so quit the server (and this process) using the normal exit route.
    threading.Timer(1.0, _shutdown_for_update).start()
    return {"ok": True, "version": man["version"]}


def _shutdown_for_update():
    write_log("Updating: shutting down for the installer.")
    time.sleep(0.8)
    os._exit(0)


def _remove_file(path):
    for _ in range(6):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)


def update_loop():
    # Let the startup cloud pull finish before checking. Otherwise a pull can
    # overwrite update_last_check/update_latest right after the check writes it.
    _cloud_first_done.wait(180)
    while True:
        try:
            con = connect()
            try:
                st = get_settings(con)
                if st.get("update_auto") == "1" and st.get("update_url"):
                    last = st.get("update_last_check", "")
                    due = True
                    if last:
                        try:
                            then = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            due = (datetime.now(timezone.utc) - then).total_seconds() >= UPDATE_CHECK_HOURS * 3600
                        except ValueError:
                            due = True
                    if due:
                        try:
                            _run_update_check(con)
                        except ApiError as e:
                            write_log("Update check failed: %s" % e)
            finally:
                con.close()
        except Exception as e:
            write_log("Update check: %s" % e)
        time.sleep(1800)


def api_exit(h, con, q):
    """Stop the app (used by the packaged .exe, which has no console window)."""
    log(con, "Settings", "Application stopped by %s" % (getattr(_ctx, "username", "") or "a user"))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    write_log("Application stopped from the web interface.")
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return {"ok": True}


def clean_cat(d):
    name = str(d.get("name", "")).strip()[:40]
    code = re.sub(r"[^A-Za-z0-9]", "", str(d.get("code", ""))).upper()
    if not name:
        raise ApiError(400, "Enter a device type name.")
    if not 2 <= len(code) <= 5:
        raise ApiError(400, "Code must be 2 to 5 letters or numbers.")
    return name, code


def api_cat_create(h, con, q):
    name, code = clean_cat(h.body())
    with tx(con):
        try:
            con.execute("INSERT INTO categories(name,code) VALUES(?,?)", (name, code))
        except sqlite3.IntegrityError:
            raise ApiError(409, "A device type with that name or code already exists.")
        log(con, "Settings", "Added device type %s (%s)" % (name, code), None, "", "category",
            [{"f": "Device type", "o": "", "n": "%s (%s)" % (name, code)}])
    return {"ok": True}


def api_cat_update(h, con, q, cid):
    name, code = clean_cat(h.body())
    with tx(con):
        old = con.execute("SELECT * FROM categories WHERE id=?", (int(cid),)).fetchone()
        if not old:
            raise ApiError(404, "Device type not found.")
        try:
            con.execute("UPDATE categories SET name=?,code=? WHERE id=?", (name, code, int(cid)))
        except sqlite3.IntegrityError:
            raise ApiError(409, "A device type with that name or code already exists.")
        ch = []
        if old["name"] != name:
            ch.append({"f": "Type name", "o": old["name"], "n": name})
        if old["code"] != code:
            ch.append({"f": "Type code", "o": old["code"], "n": code})
        if ch:
            log(con, "Settings", "Device type edited. " + change_text(ch) + ". Existing tags are unchanged.",
                None, "", "category", ch)
    return {"ok": True}


def api_cat_delete(h, con, q, cid):
    with tx(con):
        row = con.execute("SELECT name,code FROM categories WHERE id=?", (int(cid),)).fetchone()
        if not row:
            raise ApiError(404, "Device type not found.")
        n = con.execute("SELECT COUNT(*) FROM devices WHERE category_id=?", (int(cid),)).fetchone()[0]
        if n:
            raise ApiError(409, "%d devices use %s. Move or delete them first." % (n, row["name"]))
        con.execute("DELETE FROM categories WHERE id=?", (int(cid),))
        log(con, "Settings", "Removed device type %s" % row["name"], None, "", "category",
            [{"f": "Device type", "o": "%s (%s)" % (row["name"], row["code"]), "n": ""}])
    return {"ok": True}


RESTORE_MAX = 512 * 1024 * 1024


def api_backup_restore(h, con, q):
    """Replace the live database with an uploaded SQLite backup (admin only)."""
    data = h.body()
    try:
        raw = base64.b64decode(data.get("data") or "", validate=True)
    except Exception:
        raise ApiError(400, "That file could not be read. Choose a .db backup made by TAG Master.")
    if len(raw) < 512 or raw[:16] != b"SQLite format 3\x00":
        raise ApiError(400, "That is not a TAG Master database file.")
    if len(raw) > RESTORE_MAX:
        raise ApiError(400, "That backup is too large to restore.")
    tmp = os.path.join(BASE_DIR, ".restore_tmp.db")
    with open(tmp, "wb") as fh:
        fh.write(raw)
    try:
        if not {"devices", "categories", "users", "settings"} <= _check_db_tables(tmp):
            raise ApiError(400, "That database is missing TAG Master tables.")
        _backup_local("pre-restore")  # safety copy of the current data
        con.commit()
        con.close()
        con = connect()
        src = sqlite3.connect(tmp)
        try:
            src.backup(con)
        finally:
            src.close()
        _migrate(con)
        log(con, "Backup", "Database restored from an uploaded backup file")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"ok": True}


ROUTES = [
    ("GET", r"^/api/session$", api_session, "public"),
    ("POST", r"^/api/login$", api_login, "public"),
    ("POST", r"^/api/logout$", api_logout, "public"),
    ("POST", r"^/api/account/password$", api_account_password, "self"),
    ("GET", r"^/api/users$", api_users, "admin"),
    ("POST", r"^/api/users$", api_user_create, "admin"),
    ("PUT", r"^/api/users/(\d+)$", api_user_update, "admin"),
    ("POST", r"^/api/users/(\d+)/password$", api_user_password, "admin"),
    ("DELETE", r"^/api/users/(\d+)$", api_user_delete, "admin"),
    ("GET", r"^/api/meta$", api_meta, "any"),
    ("GET", r"^/api/stats$", api_stats, "any"),
    ("GET", r"^/api/preview$", api_preview, "any"),
    ("GET", r"^/api/devices$", api_devices, "any"),
    ("POST", r"^/api/devices$", api_device_create, "write"),
    ("POST", r"^/api/devices/by-ids$", api_devices_by_ids, "any"),
    ("POST", r"^/api/devices/bulk$", api_devices_bulk, "write"),
    ("GET", r"^/api/devices/(\d+)$", api_device_get, "any"),
    ("PUT", r"^/api/devices/(\d+)$", api_device_update, "write"),
    ("DELETE", r"^/api/devices/(\d+)$", api_device_delete, "admin"),
    ("POST", r"^/api/generate$", api_generate, "write"),
    ("POST", r"^/api/import$", api_import, "write"),
    ("POST", r"^/api/labels/log$", api_labels_log, "any"),
    ("GET", r"^/api/audit$", api_audit, "admin"),
    ("GET", r"^/api/snapshot$", api_snapshot, "admin"),
    ("POST", r"^/api/snapshot/restore$", api_snapshot_restore, "admin"),
    ("PUT", r"^/api/settings$", api_settings_put, "admin"),
    ("GET", r"^/api/cloud$", api_cloud_get, "admin"),
    ("PUT", r"^/api/cloud$", api_cloud_put, "admin"),
    ("POST", r"^/api/cloud/test$", api_cloud_test, "admin"),
    ("POST", r"^/api/cloud/push$", api_cloud_push, "admin"),
    ("POST", r"^/api/cloud/pull$", api_cloud_pull, "admin"),
    ("GET", r"^/api/update$", api_update_get, "admin"),
    ("PUT", r"^/api/update$", api_update_put, "admin"),
    ("POST", r"^/api/update/check$", api_update_check, "admin"),
    ("POST", r"^/api/update/install$", api_update_install, "admin"),
    ("POST", r"^/api/exit$", api_exit, "admin"),
    ("POST", r"^/api/categories$", api_cat_create, "admin"),
    ("PUT", r"^/api/categories/(\d+)$", api_cat_update, "admin"),
    ("DELETE", r"^/api/categories/(\d+)$", api_cat_delete, "admin"),
    ("POST", r"^/api/backup/restore$", api_backup_restore, "admin"),
]
ROUTES = [(m, re.compile(p), fn, perm) for m, p, fn, perm in ROUTES]


def export_csv(con, q):
    where, args = build_filters(q)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Tag", "Device type", "Brand", "Model", "Serial number", "MAC address", "Status",
                "Assigned to", "Department", "Location", "Purchase date", "Warranty ends", "Notes", "Created (UTC)"])
    for r in con.execute(DEVICE_SELECT + where + " ORDER BY d.tag", args):
        w.writerow([csv_safe(r["tag"]), r["category_name"], csv_safe(r["brand"]), csv_safe(r["model"]),
                    csv_safe(r["serial_number"]), r["mac_address"], r["status"], csv_safe(r["assigned_to"]),
                    csv_safe(r["department"]), csv_safe(r["location"]), r["purchase_date"], r["warranty_end"],
                    csv_safe(r["notes"]), r["created_at"]])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def make_backup():
    tmp = os.path.join(BASE_DIR, ".backup_tmp.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    src = connect()
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(tmp, "rb") as fh:
        data = fh.read()
    os.remove(tmp)
    return data


def _check_db_tables(path):
    check = sqlite3.connect(path)
    try:
        return {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        check.close()


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "BluePeakTagMaster/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- plumbing ---------------------------------------------------------
    def body(self):
        if self._body is None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200 * 1024 * 1024:
                raise ApiError(413, "Request is too large.")
            raw = self.rfile.read(n) if n else b""
            try:
                self._body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                raise ApiError(400, "Invalid request.")
        return self._body

    _cookie = None
    user = None

    def send(self, status, payload, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self._cookie:
            self.send_header("Set-Cookie", self._cookie)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        if host not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
            return False
        return True

    def handle_any(self):
        self._body = None
        self._cookie = None
        self.user = None
        _ctx.username = ""
        _ctx.role = ""
        _ctx.ip = self.client_address[0]
        try:
            if not self.host_ok():
                raise ApiError(403, "Open this app from http://127.0.0.1:%d" % self.server.server_address[1])
            url = urlparse(self.path)
            path, q = url.path, parse_qs(url.query, keep_blank_values=True)
            method = self.command
            if method == "HEAD":
                method = "GET"
            if method == "GET" and path in ("/", "/index.html"):
                return self.send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if method == "GET" and path in ("/logo.png", "/favicon.ico", "/apple-touch-icon.png"):
                png = logo_bytes()
                if not png:
                    raise ApiError(404, "Logo not found.")
                return self.send(200, png, "image/png", {"Cache-Control": "public, max-age=86400"})
            if method in ("POST", "PUT", "DELETE") and self.headers.get("X-Requested-With") != "BluePeak":
                raise ApiError(403, "Request blocked.")
            special, fn, groups, perm = None, None, (), None
            if method == "GET" and path == "/api/export.csv":
                special, perm = "csv", "write"
            elif method == "GET" and path == "/api/backup":
                special, perm = "backup", "admin"
            elif method == "GET" and path == "/api/backup/cloud":
                special, perm = "cloudbackup", "admin"
            elif method == "GET" and path == "/api/audit.csv":
                special, perm = "auditcsv", "admin"
            elif method == "POST" and path == "/api/labels/pdf":
                special, perm = "pdf", "any"
            elif method == "GET" and path == "/api/export.xlsx":
                special, perm = "xlsx", "write"
            else:
                for m, rx, f, p in ROUTES:
                    if m == method:
                        mt = rx.match(path)
                        if mt:
                            fn, groups, perm = f, mt.groups(), p
                            break
                if fn is None:
                    raise ApiError(404, "Not found.")
            con = connect()
            try:
                user = current_user(self, con)
                self.user = user
                _ctx.username = user["username"] if user else ""
                _ctx.role = user["role"] if user else ""
                authorize(user, perm)
                if special == "csv":
                    data = export_csv(con, q)
                    log(con, "Export", "Device list exported to CSV")
                    name = "BluePeak-devices-%s.csv" % now_utc().strftime("%Y%m%d-%H%M")
                    return self.send(200, data, "text/csv; charset=utf-8",
                                     {"Content-Disposition": 'attachment; filename="%s"' % name})
                if special == "backup":
                    data = make_backup()
                    log(con, "Backup", "Database backup downloaded")
                    name = "BluePeak-TagMaster-backup-%s.db" % now_utc().strftime("%Y%m%d-%H%M")
                    return self.send(200, data, "application/octet-stream",
                                     {"Content-Disposition": 'attachment; filename="%s"' % name})
                if special == "cloudbackup":
                    cfg = cloud_config(con)
                    if not cloud_ready(cfg):
                        raise ApiError(400, "SQLite Cloud is not switched on or not fully set up yet.")
                    try:
                        data = bluepeak_cloud.download(cfg)
                    except Exception as e:
                        raise ApiError(502, str(e))
                    log(con, "Backup", "SQLite Cloud database downloaded")
                    base = os.path.splitext(os.path.basename(DB_PATH))[0]
                    name = "%s-cloud-%s.db" % (base, now_utc().strftime("%Y%m%d-%H%M"))
                    return self.send(200, data, "application/octet-stream",
                                     {"Content-Disposition": 'attachment; filename="%s"' % name})
                if special == "auditcsv":
                    data = audit_csv(con, q)
                    log(con, "Export", "Audit log exported to CSV")
                    name = "BluePeak-audit-log-%s.csv" % now_utc().strftime("%Y%m%d-%H%M")
                    return self.send(200, data, "text/csv; charset=utf-8",
                                     {"Content-Disposition": 'attachment; filename="%s"' % name})
                if special == "xlsx":
                    data, name, count = build_xlsx(con, user)
                    log(con, "Export", "Whole inventory exported to Excel (%d devices)" % count)
                    return self.send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                     {"Content-Disposition": 'attachment; filename="%s"' % name})
                if special == "pdf":
                    data, name = build_labels_pdf(con, self.body())
                    return self.send(200, data, "application/pdf",
                                     {"Content-Disposition": 'attachment; filename="%s"' % name})
                result = fn(self, con, q, *groups)
            finally:
                con.close()
            return self.send(200, result)
        except ApiError as e:
            payload = {"error": e.message}
            if e.code:
                payload["code"] = e.code
            self.send(e.status, payload)
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            write_log("Unhandled error on %s %s: %s" % (self.command, self.path, e))
            self.send(500, {"error": "Something went wrong. The details were written to the log file."})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any


# --------------------------------------------------------------------------
# Background SQLite Cloud sync
# --------------------------------------------------------------------------
def cloud_ready(cfg):
    return bool(cfg["enabled"] and cfg["url"] and cfg["apikey"] and cfg["database"])


def _startup_sync(cfg):
    try:
        have = bluepeak_cloud.remote_tables(cfg)
    except Exception as e:
        _sync_error("connect", e)
        return
    try:
        if have & set(bluepeak_cloud.SYNC_TABLES):
            cloud_run("pull", cfg, quiet=True)
        else:
            cloud_run("push", cfg, quiet=True)
    except Exception as e:
        _sync_error("start", e)


def cloud_loop():
    first, last_auto = True, 0.0
    while True:
        try:
            con = connect()
            try:
                cfg = cloud_config(con)
            finally:
                con.close()
            if not cloud_ready(cfg):
                first = True
                _cloud_first_done.set()
            elif first:
                first, last_auto = False, time.time()
                try:
                    if cfg["start"]:
                        _startup_sync(cfg)
                finally:
                    _cloud_first_done.set()
            elif cfg["auto"] and time.time() - last_auto >= cfg["auto"]:
                last_auto = time.time()
                try:
                    if bluepeak_cloud.fingerprint(DB_PATH) != (cfg["fp"] or ""):
                        cloud_run("push", cfg, quiet=True)
                except Exception as e:
                    _sync_error("auto", e)
        except Exception as e:
            write_log("Cloud sync: %s" % e)
        time.sleep(8)


def main():
    port, open_browser = DEFAULT_PORT, True
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--no-browser":
            open_browser = False
        elif a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
    created = init_db()
    threading.Thread(target=backup_loop, name="backup", daemon=True).start()
    threading.Thread(target=update_loop, name="updates", daemon=True).start()
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            server.daemon_threads = True
            port = p
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("Could not find a free port. Close other programs and try again.")
    url = "http://127.0.0.1:%d" % port
    con = connect()
    try:
        cfg = cloud_config(con)
    finally:
        con.close()
    print("")
    print("  %s" % APP_NAME)
    print("  ------------------------------------------")
    print("  Running at   %s" % url)
    print("  Database     %s" % DB_PATH)
    if cloud_ready(cfg):
        print("  SQLite Cloud %s  (database %s)" % (mask_url(cfg["url"]), cfg["database"]))
        threading.Thread(target=cloud_loop, name="cloud-sync", daemon=True).start()
    if created:
        print("")
        print("  First sign-in   username: %s   password: %s" % DEFAULT_ADMIN)
        print("  You will be asked to choose a new password straight away.")
    print("")
    if FROZEN:
        print("  This window can be closed. Stop the app from its user menu (Exit).")
    else:
        print("  Keep this window open while you use the app.")
        print("  Press Ctrl+C to stop.")
    print("")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Your data is saved in the database file.")
    finally:
        server.server_close()


def fatal_error(exc):
    write_log("Fatal error:\n%s" % traceback.format_exc())
    if not FROZEN:
        return
    try:
        import ctypes
        text = ("%s could not start.\n\n%s\n\nDetails were written to:\n%s"
                % (APP_NAME, exc, LOG_PATH))
        ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, 0x10)
    except Exception:
        pass


# --------------------------------------------------------------------------
# The single page UI
# --------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BluePeak TAG Master</title>
<link rel="icon" href="/logo.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
:root{
  --font:"Manrope","Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
  --font-heading:"Aptos","Aptos Display",-apple-system,"Segoe UI Variable Display","Segoe UI",system-ui,Roboto,Arial,sans-serif;
  --mono:"JetBrains Mono","Cascadia Mono","SF Mono",ui-monospace,Consolas,monospace;
  --bg:#EDF0F6; --surface:#FFFFFF; --surface-2:#F5F7FB; --ink:#0F1B3A; --muted:#5E6A89; --line:#DCE1EC;
  --primary:#13296B; --primary-ink:#FFFFFF; --primary-soft:#E5EBFA;
  --red:#C8102E;
  --ok:#16794E; --ok-soft:#DDF3E8; --warn:#9A5B00; --warn-soft:#FDEFD0; --bad:#B4162B; --bad-soft:#FCE5E8;
  --info:#1F4FD8; --info-soft:#E3EBFF; --mute-soft:#E8EBF2;
  --side:#0B1A45; --side-ink:#C5CFEE;
  --shadow:0 1px 2px rgba(15,27,58,.05),0 10px 28px -14px rgba(15,27,58,.22);
  color-scheme:light;
}
[data-theme=dark]{
  --bg:#070E26; --surface:#0F1A3D; --surface-2:#142250; --ink:#E9EDFA; --muted:#97A3C7; --line:#233363;
  --primary:#5B7BFF; --primary-ink:#FFFFFF; --primary-soft:#1B2C66;
  --red:#FF4D63;
  --ok:#4CD39A; --ok-soft:#10392F; --warn:#F2B34D; --warn-soft:#3B2D10; --bad:#FF6B7E; --bad-soft:#44192A;
  --info:#7FA0FF; --info-soft:#1B2C66; --mute-soft:#1D2A55;
  --side:#050B1E; --side-ink:#AEB9DD;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 12px 30px -14px rgba(0,0,0,.6);
  color-scheme:dark;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:var(--font);background:var(--bg);color:var(--ink);font-size:14.5px;line-height:1.5;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
a{color:inherit;text-decoration:none}
h1,h2,h3,p{margin:0}
h1,h2,h3{font-family:var(--font-heading)}
:focus-visible{outline:2px solid var(--primary);outline-offset:2px}
svg.i{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;flex:none}

/* layout */
.app{display:grid;grid-template-columns:252px minmax(0,1fr);min-height:100vh}
.side{background:var(--side);color:var(--side-ink);position:sticky;top:0;height:100vh;display:flex;flex-direction:column;padding:22px 14px}
.brand{display:flex;gap:12px;align-items:center;padding:4px 10px 24px}
.brand b{display:block;color:#fff;font-size:16px;font-weight:800;letter-spacing:.01em;line-height:1.15}
.brand span{font-size:12.5px;color:#8E9BC7}
.brand img{width:46px;height:46px;object-fit:contain;background:#fff;border-radius:12px;padding:5px;box-shadow:0 6px 18px -8px rgba(0,0,0,.7);flex:none}
#nav a{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:10px;color:var(--side-ink);font-weight:600;margin-bottom:3px;transition:background .15s}
#nav a:hover{background:rgba(255,255,255,.08)}
#nav a.on{background:#fff;color:#0B1A45}
#nav a.on svg{color:#C8102E}
.side-foot{margin-top:auto;padding:12px 10px 0;font-size:12.5px;color:#8E9BC7;border-top:1px solid rgba(255,255,255,.08)}
.side-foot .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3DD68C;margin-right:8px}
.main{min-width:0}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:16px;padding:14px 32px;background:color-mix(in srgb,var(--bg) 86%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.top h1{font-size:21px;font-weight:800;letter-spacing:-.015em;flex:1}
.content{padding:28px 32px 70px;max-width:1380px}
.searchbox{position:relative}
.searchbox svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.searchbox .inp{padding-left:38px;border-radius:999px}
.top .searchbox{width:min(360px,38vw)}
.top kbd{position:absolute;right:12px;top:50%;transform:translateY(-50%);font-family:var(--mono);font-size:11.5px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--muted);background:var(--surface-2)}

/* controls */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line);background:var(--surface);padding:9px 15px;border-radius:10px;font-weight:700;cursor:pointer;transition:background .15s,filter .15s,transform .05s;white-space:nowrap}
.btn:hover{background:var(--surface-2)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--primary);border-color:var(--primary);color:var(--primary-ink)}
.btn.primary:hover{filter:brightness(1.14);background:var(--primary)}
.btn.danger{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,var(--line))}
.btn.danger:hover{background:var(--bad-soft)}
.btn.lg{padding:13px 24px;font-size:15.5px;border-radius:12px}
.btn.sm{padding:6px 11px;font-size:13px;border-radius:8px}
.btn[disabled]{opacity:.55;cursor:not-allowed}
.icon-btn{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:9px;border:1px solid transparent;background:transparent;cursor:pointer;color:var(--muted)}
.icon-btn:hover{background:var(--surface-2);color:var(--ink)}
.linkbtn{background:none;border:0;padding:0;color:var(--primary);font-weight:700;cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.inp{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--surface);min-width:0}
.inp:focus{outline:2px solid color-mix(in srgb,var(--primary) 55%,transparent);outline-offset:0;border-color:var(--primary)}
textarea.inp{resize:vertical;min-height:84px}
.field{display:flex;flex-direction:column;gap:6px;min-width:0}
.field>span{font-weight:700;font-size:13px}
.field:has([required])>span::after{content:" *";color:var(--bad);font-weight:800}
.field small{color:var(--muted);font-size:12.5px}
.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.span2{grid-column:1/-1}
.sub{color:var(--muted)}
.dim{color:var(--muted)}
.spacer{flex:1}
.chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font-size:12.5px;font-weight:700;white-space:nowrap}
.chip::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.chip.info{background:var(--info-soft);color:var(--info)}
.chip.ok{background:var(--ok-soft);color:var(--ok)}
.chip.warn{background:var(--warn-soft);color:var(--warn)}
.chip.bad{background:var(--bad-soft);color:var(--bad)}
.chip.mute{background:var(--mute-soft);color:var(--muted)}
.chip-btn{border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:4px 13px;font-weight:700;cursor:pointer;font-size:13px}
.chip-btn:hover{border-color:var(--primary);color:var(--primary)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:-6px}

/* inventory page */
.inv-sub{margin:-4px 0 12px}
.inv-filters{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:12px}
.inv-chips{display:flex;gap:8px;flex-wrap:wrap}
.inv-sel{display:flex;gap:10px;flex-wrap:wrap}
.inv-sel .inp{width:auto;min-width:155px}
.chip-btn.on{background:var(--primary);border-color:var(--primary);color:var(--primary-ink)}
.chip-btn i{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--sc,var(--ink));margin-right:3px;vertical-align:0}
.fbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 14px}
.fbar .pill{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--line);background:var(--surface-2);border-radius:999px;padding:3px 4px 3px 12px;font-size:12.5px;font-weight:700}
.fbar .pill button{display:grid;place-items:center;width:22px;height:22px;border:0;border-radius:50%;background:none;cursor:pointer;color:var(--muted)}
.fbar .pill button:hover{background:var(--bad-soft);color:var(--bad)}
.emp{display:flex;flex-direction:column;gap:6px;align-items:center}
.empty-row .emp b{font-size:15.5px;color:var(--ink)}
.empty-row .emp .btn{color:var(--ink)}
.emp-act{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin-top:8px}
@media (max-width:760px){.inv-sel{width:100%}.inv-sel .inp{flex:1;min-width:0}.inv-filters{justify-content:flex-start}}
.tagcode{font-family:var(--mono);font-weight:500;font-size:13px;white-space:nowrap}
.tagcode .tp{color:var(--muted)}
.tagcode b{font-weight:700;color:var(--ink)}

/* panels */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:22px;box-shadow:var(--shadow)}
.panel.flush{padding:0;overflow:hidden}
.panel h2{font-size:17px;font-weight:800;letter-spacing:-.01em}
.panel .sub+*,.panel h2+*{margin-top:0}
.ph{margin-bottom:4px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(196px,1fr));gap:16px;margin-bottom:20px}
.kpi{position:relative;display:flex;flex-direction:column;gap:12px;text-align:left;border:1px solid var(--line);background:linear-gradient(155deg,color-mix(in srgb,var(--kc,var(--primary)) 9%,var(--surface)),var(--surface) 58%);border-radius:18px;padding:17px 18px 15px;cursor:pointer;box-shadow:var(--shadow);overflow:hidden;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--kc,var(--primary))}
.kpi::after{content:"";position:absolute;right:-46px;top:-58px;width:140px;height:140px;border-radius:50%;background:color-mix(in srgb,var(--kc,var(--primary)) 13%,transparent);pointer-events:none}
.kpi:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--kc,var(--primary)) 45%,var(--line));box-shadow:0 2px 4px rgba(15,27,58,.05),0 18px 34px -18px rgba(15,27,58,.4)}
.kpi .kpi-top{display:flex;align-items:center;gap:10px;position:relative;z-index:1}
.kpi .kpi-ico{display:grid;place-items:center;width:34px;height:34px;border-radius:11px;background:color-mix(in srgb,var(--kc,var(--primary)) 15%,var(--surface));color:var(--kc,var(--primary));border:1px solid color-mix(in srgb,var(--kc,var(--primary)) 26%,transparent);flex:none}
.kpi .kpi-label{color:var(--muted);font-weight:800;font-size:11.5px;letter-spacing:.075em;text-transform:uppercase;line-height:1.3}
.kpi b{display:block;font-size:38px;line-height:1;font-weight:800;letter-spacing:-.04em;position:relative;z-index:1}
.kpi .kpi-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--muted);font-weight:600;font-size:12.5px;position:relative;z-index:1;min-height:18px}
.kpi .kpi-arrow{color:var(--kc,var(--primary));opacity:.55;transition:transform .16s ease,opacity .16s ease}
.kpi:hover .kpi-arrow{opacity:1;transform:translateX(3px)}
.dash-hero{position:relative;display:flex;align-items:center;justify-content:space-between;gap:22px;flex-wrap:wrap;background:linear-gradient(120deg,var(--side),#17357F 62%,#2450B8);color:#fff;border-radius:20px;padding:24px 28px;margin-bottom:20px;overflow:hidden;box-shadow:0 18px 38px -24px rgba(9,20,52,.8)}
.dash-hero::after{content:"";position:absolute;right:-60px;top:-90px;width:280px;height:280px;border-radius:50%;background:radial-gradient(circle,rgba(130,165,255,.32),transparent 68%);pointer-events:none}
.dash-hero-copy{position:relative;z-index:1;min-width:0}
.dash-hero .eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:#AFC3FF}
.dash-hero h2{font-size:clamp(22px,2.2vw,30px);font-weight:800;letter-spacing:-.025em;margin:7px 0 6px;line-height:1.14}
.dash-hero p{color:#B9C7EC;max-width:62ch;font-size:13.5px}
.dash-hero-actions{position:relative;z-index:1;display:flex;gap:10px;flex-wrap:wrap}
.dash-hero-actions .btn{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.28);color:#fff}
.dash-hero-actions .btn:hover{background:rgba(255,255,255,.2)}
.dash-hero-actions .btn.primary{background:#fff;border-color:#fff;color:#0B1A45}
.dash-hero-actions .btn.primary:hover{background:#EAF0FF}
.panel-lead{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:14px}
.panel-lead .panel-title{display:flex;flex-direction:column;gap:3px;min-width:0}
.panel-lead .panel-title .sub{font-size:12.5px}
.panel-badge{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:11px;background:var(--primary-soft);color:var(--primary);flex:none}
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:20px;margin-bottom:20px}
.bars{display:flex;flex-direction:column;gap:12px;margin-top:16px}
.bar-row{display:grid;grid-template-columns:130px 1fr 44px;gap:12px;align-items:center}
.bar-row .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-row .tr{height:10px;border-radius:99px;background:var(--mute-soft);overflow:hidden}
.bar-row .tr i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--primary),color-mix(in srgb,var(--primary) 55%,#7E9BFF));transition:width .4s ease}
.bar-row .n{text-align:right;font-weight:800}
.donut-wrap{display:flex;align-items:center;gap:22px;margin-top:14px;flex-wrap:wrap}
.donut{width:150px;height:150px;flex:none}
.donut .d-n{font-size:26px;font-weight:800;fill:var(--ink)}
.donut .d-l{font-size:10px;fill:var(--muted)}
.legend{display:flex;flex-direction:column;gap:8px;flex:1;min-width:150px}
.legend button{display:flex;align-items:center;gap:10px;border:0;background:none;padding:2px 0;cursor:pointer;text-align:left}
.legend i{width:10px;height:10px;border-radius:3px;flex:none}
.legend span{flex:1;font-weight:600}
.legend b{font-weight:800}
.recent{list-style:none;margin:14px 0 0;padding:0}
.recent li{display:grid;grid-template-columns:230px 1fr auto;gap:14px;align-items:center;padding:11px 10px;margin:0 -10px;border-top:1px solid var(--line);border-radius:10px;transition:background .15s ease}
.recent li:hover{background:var(--surface-2)}
.recent li:first-child{border-top:0}
.empty{text-align:center;padding:64px 20px;max-width:620px;margin:20px auto}
.empty h2{font-size:24px;margin:26px 0 8px;font-weight:800;letter-spacing:-.02em}
.empty p{color:var(--muted);margin-bottom:24px}
.empty .lbwrap{display:flex;justify-content:center}

/* generate */
.gen{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(320px,.9fr);gap:22px;align-items:start}
.pv{position:sticky;top:86px}
.pv .stage{background:repeating-linear-gradient(45deg,var(--surface-2),var(--surface-2) 10px,var(--bg) 10px,var(--bg) 20px);border-radius:12px;padding:26px 12px;display:flex;justify-content:center;margin:16px 0}
.pv dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:8px 16px}
.pv dt{color:var(--muted);font-weight:600}
.pv dd{margin:0;text-align:right}
.form-actions{margin-top:22px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.result{margin-top:22px}
.res-head{display:flex;gap:16px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;margin-bottom:16px}
.res-actions{display:flex;gap:8px;flex-wrap:wrap}
.pad{padding:12px 4px 0}

/* toolbar & table */
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.toolbar .searchbox{flex:1;min-width:200px}
.toolbar select.inp{width:auto;min-width:0}
.notice{background:var(--primary-soft);border-radius:12px;padding:10px 16px;margin-bottom:14px;font-weight:600}
.bulk{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--primary);color:var(--primary-ink);border-radius:12px;padding:10px 14px;margin-bottom:14px}
.bulk .btn{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.25);color:#fff}
.bulk .btn:hover{background:rgba(255,255,255,.22)}
.bulk select{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);color:#fff;border-radius:8px;padding:6px 8px}
.bulk select option{color:#000}
.bulk .linkbtn{color:#fff}
.table-wrap{overflow:auto;max-width:100%}
.tbl{width:100%;border-collapse:collapse;min-width:900px}
.tbl th{position:sticky;top:0;background:var(--surface-2);text-align:left;font-size:13px;color:var(--muted);font-weight:700;padding:11px 14px;border-bottom:1px solid var(--line);white-space:nowrap;user-select:none}
.tbl th[data-sort]{cursor:pointer}
.tbl th[data-sort]:hover{color:var(--ink)}
.tbl th .ar{margin-left:4px;color:var(--primary)}
.tbl td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
.tbl tbody tr{cursor:default}
.tbl tbody tr:hover{background:var(--surface-2)}
.tbl tbody tr.sel{background:var(--primary-soft)}
.tbl tbody tr:last-child td{border-bottom:0}
.tbl .c-chk{width:42px;padding-right:0}
.tbl .c-act{width:46px;text-align:right}
.tbl.compact td,.tbl.compact th{padding:8px 14px}
.mono-sm{font-family:var(--mono);font-size:12.5px}
input[type=checkbox]{width:17px;height:17px;accent-color:var(--primary);cursor:pointer}
.pager{display:flex;gap:12px;align-items:center;padding:12px 16px;border-top:1px solid var(--line);flex-wrap:wrap}
.pager .inp{width:auto;padding:6px 8px}
.empty-row td{text-align:center;color:var(--muted);padding:44px 10px!important;cursor:default}

/* activity */
.timeline{list-style:none;margin:0;padding:0}
.timeline li{display:grid;grid-template-columns:150px 110px 130px 220px 1fr;gap:14px;padding:12px 18px;border-top:1px solid var(--line);align-items:baseline}
.timeline li:first-child{border-top:0}
.timeline time{color:var(--muted);font-size:13px}

/* settings */
.settings{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
.settings .full{grid-column:1/-1}
.sample{display:flex;align-items:center;gap:14px;background:var(--surface-2);border:1px dashed var(--line);border-radius:12px;padding:14px 16px;margin-top:6px}
.sample .tagcode{font-size:17px}
.check{display:flex;gap:10px;align-items:center;font-weight:600}
.row-actions{display:flex;gap:4px;justify-content:flex-end}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 18px;margin:12px 0 0}
.kv dt{color:var(--muted);font-weight:600}
.kv dd{margin:0;word-break:break-all}

/* dialogs */
dialog{border:0;border-radius:18px;padding:0;background:var(--surface);color:var(--ink);box-shadow:0 30px 80px -20px rgba(3,8,30,.6);width:min(960px,94vw);max-height:92vh;overflow:visible}
dialog:has(> .dlg.sm){width:min(556px,94vw)}
dialog::backdrop{background:rgba(7,14,38,.58);backdrop-filter:blur(2px)}
.dlg{display:flex;flex-direction:column;max-height:92vh}
.dlg.sm{padding:26px;width:min(460px,92vw);margin:0 auto;min-width:0;overflow-y:auto;overscroll-behavior:contain}
.dlg.sm h2{margin-bottom:8px;font-size:19px}
.dlg.sm p{color:var(--muted)}
.dlg-head{display:flex;justify-content:space-between;gap:16px;padding:22px 26px 6px}
.dlg-head h2{font-size:20px;font-weight:800;letter-spacing:-.015em}
.dlg-body{padding:16px 26px;overflow:auto}
.dlg-body.two{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(250px,1fr);gap:26px}
.dlg-foot{display:flex;gap:10px;align-items:center;flex-wrap:wrap;flex-shrink:0;padding:16px 26px;border-top:1px solid var(--line)}
.dlg.sm .dlg-foot{padding:22px 0 2px;border:0;justify-content:flex-end;position:sticky;bottom:0;width:100%;background:var(--surface)}
.dside{display:flex;flex-direction:column;gap:18px}
.dside .stage{background:var(--surface-2);border-radius:12px;padding:18px 10px;display:flex;justify-content:center;overflow:hidden}
/* history panel */
.hist-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.hist-head span:first-child{font-size:15px;font-weight:800}
.hist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;max-height:320px;overflow:auto}
.hist li{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--surface-2);display:flex;flex-direction:column;gap:8px}
.hist li>header{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.hist li .act{display:flex;align-items:center;gap:8px}
.hist li .act b{font-size:14px}
.hist li .meta{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12.5px}
.hist .changes{display:flex;flex-direction:column;gap:6px}
.hist .chg-row{display:grid;grid-template-columns:130px 1fr;gap:10px;align-items:start}
.hist .chg-row .k{color:var(--muted);font-weight:700}
.hist .chg-row .v{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.hist .chg-row .old{color:var(--muted);text-decoration:line-through}
.hist .chg-row .arrow{color:var(--muted)}
.hist .hist-foot{display:flex;align-items:center;justify-content:space-between;gap:10px}
.snap-link{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:4px 10px;font-weight:700;font-size:12.5px;text-decoration:none;color:var(--ink)}
.hist li{font-size:13px;border-left:3px solid var(--line);padding-left:12px}
.hist b{display:block}
.hist time{color:var(--muted);font-size:12px}
.form-err{margin:0 26px 14px;background:var(--bad-soft);color:var(--bad);border-radius:10px;padding:10px 14px;font-weight:600}
.lbl-grid{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;background:var(--surface-2);border-radius:12px;padding:20px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.seg button{border:0;background:var(--surface);padding:8px 14px;font-weight:700;cursor:pointer}
.seg button.on{background:var(--primary);color:var(--primary-ink)}
.imp-box{border:2px dashed var(--line);border-radius:14px;padding:22px;text-align:center}
.errlist{max-height:150px;overflow:auto;background:var(--surface-2);border-radius:10px;padding:10px 14px;font-size:13px;margin-top:12px}

#toasts{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:10px;z-index:100}
.toast{background:var(--ink);color:var(--surface);padding:12px 18px;border-radius:12px;font-weight:600;box-shadow:var(--shadow);max-width:420px;animation:tin .22s ease-out}
.toast.bad{background:var(--bad);color:#fff}
.toast.out{opacity:0;transform:translateY(8px);transition:.35s}
@keyframes tin{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.loading{color:var(--muted);padding:40px 0;text-align:center}
.errbar{display:flex;align-items:center;gap:12px;background:var(--bad-soft);color:var(--bad);border-bottom:1px solid var(--bad);padding:10px 24px;font-weight:600;font-size:14px}
.errbar span{flex:1;min-width:0;overflow-wrap:anywhere}
.updbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:linear-gradient(90deg,color-mix(in srgb,var(--info) 11%,var(--surface)),var(--surface) 70%);border-bottom:1px solid color-mix(in srgb,var(--info) 32%,var(--line));padding:10px 24px;font-weight:600;font-size:13.5px}
.updbar[hidden]{display:none}
.updbar .upd-ico{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:var(--info-soft);color:var(--info);flex:none}
.updbar .upd-txt{flex:1;min-width:200px}
.updbar .upd-txt b{font-weight:800}
.updbar .upd-txt .sub{font-size:12.5px;font-weight:500}

/* the TAG label (a physical sticker, so it always stays white) */
.label{--u:1.4;--fs:3.2;width:calc(var(--u)*50mm);height:calc(var(--u)*25mm);background:#fff;color:#0B1A45;border:.25mm solid #8A94B0;border-radius:calc(var(--u)*1.6mm);padding:calc(var(--u)*1.8mm) calc(var(--u)*2.4mm) calc(var(--u)*1.4mm) calc(var(--u)*4mm);display:flex;flex-direction:column;justify-content:space-between;font-family:var(--font);position:relative;overflow:hidden;box-sizing:border-box;flex:none}
.label::before{content:"";position:absolute;left:0;top:0;bottom:0;width:calc(var(--u)*1.5mm);background:#C8102E}
.label.sm{--u:1}.label.md{--u:1.4}.label.lg{--u:2}
.lb-head{display:flex;justify-content:space-between;align-items:baseline;gap:6px;line-height:1.1}
.lb-head b{font-size:calc(var(--u)*2.5mm);font-weight:800;letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.lb-head span{font-size:calc(var(--u)*1.9mm);font-weight:600;color:#5E6A89;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-bar svg{display:block;width:100%;height:calc(var(--u)*8.6mm)}
.lb-tag{font-family:var(--mono);font-weight:700;font-size:calc(var(--u)*var(--fs)*1mm);text-align:center;white-space:nowrap;line-height:1.1}
.lb-foot{font-size:calc(var(--u)*1.7mm);color:#0F1B3A;font-weight:800;letter-spacing:.005em;text-align:center;line-height:1.1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#printRoot{display:none}
@media print{
  html,body{background:#fff!important;margin:0!important;padding:0!important}
  .app,#toasts,dialog{display:none!important}
  #printRoot{display:block!important}
  .pp{break-after:page;page-break-after:always}
  .pp:last-child{break-after:auto;page-break-after:auto}
  .label{break-inside:avoid;page-break-inside:avoid}
  .label,.label *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}

/* accounts */
.login{position:fixed;inset:0;z-index:200;display:grid;grid-template-columns:minmax(0,1.05fr) minmax(390px,.95fr);background:var(--bg);overflow:hidden;overflow-x:hidden}
.login[hidden]{display:none}
.login-art{position:relative;display:grid;grid-template-rows:auto minmax(0,1fr) auto;gap:26px;padding:clamp(28px,3.6vw,54px);background:linear-gradient(140deg,#081538 0%,#0F2A6B 55%,#1D46A8 100%);color:#fff;min-height:0;overflow:hidden}
.login-art::before{content:"";position:absolute;right:-130px;top:-150px;width:420px;height:420px;border-radius:50%;background:radial-gradient(circle,rgba(120,155,255,.32),transparent 68%);pointer-events:none}
.login-art::after{content:"";position:absolute;left:-160px;bottom:-220px;width:460px;height:460px;border-radius:50%;border:1px solid rgba(255,255,255,.1);pointer-events:none}
.login-brand{position:relative;z-index:1;display:flex;align-items:center;gap:15px;width:auto;min-width:0}
.login-brand img{width:68px;height:68px;flex:none;object-fit:contain;background:#fff;border-radius:17px;padding:9px;box-shadow:0 20px 38px -18px rgba(0,0,0,.75)}
.login-brand .brand-copy{display:flex;flex-direction:column;gap:2px;min-width:0}
.login-brand .brand-copy b{font-size:18px;font-weight:800;letter-spacing:.01em;line-height:1.15}
.login-brand .brand-copy span{font-size:10.5px;color:#A8B9E8;font-weight:800;letter-spacing:.16em;text-transform:uppercase}
.login-main{position:relative;z-index:1;display:flex;flex-direction:column;justify-content:center;gap:26px;min-height:0;min-width:0}
.login-art h2{font-family:var(--font-heading);font-size:clamp(30px,3.2vw,46px);line-height:1.08;letter-spacing:-.02em;width:100%;max-width:15ch;font-weight:400;margin:0;text-wrap:balance}
.login-art .stage{padding:0;align-self:flex-start;max-width:100%;overflow:hidden}
.login-art .stage .label{transform:rotate(-2deg);box-shadow:0 24px 50px -18px rgba(0,0,0,.65)}
.login-art p{position:relative;z-index:1;color:#AEBDE5;max-width:48ch;margin:0;font-size:13.5px;line-height:1.6}
.login-form{width:min(430px,100%);margin:auto;padding:clamp(26px,3.4vw,46px) clamp(20px,2.8vw,40px);display:flex;flex-direction:column;gap:15px;overflow:auto;max-height:100%;background:var(--surface);border:1px solid var(--line);border-radius:22px;box-shadow:0 24px 60px -32px rgba(7,14,38,.45)}
.login-form h2{font-size:28px;font-weight:800;letter-spacing:-.025em}
.login-form>p{margin-top:-6px;margin-bottom:6px}
.login-form .btn.lg{width:100%;margin-top:4px}
.login .form-err{margin:0}
.pw-wrap{position:relative;display:flex;align-items:center}
.pw-wrap .inp{width:100%;min-width:0;flex:1;padding-right:44px}
.pw-eye{position:absolute;right:5px;top:50%;transform:translateY(-50%);display:grid;place-items:center;width:34px;height:32px;border:0;border-radius:8px;background:none;color:var(--muted);cursor:pointer}
.pw-eye:hover{color:var(--ink);background:var(--surface-2)}
.usermenu{position:relative}
.userbtn{display:flex;align-items:center;gap:10px;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:4px 14px 4px 4px;cursor:pointer}
.avatar{width:30px;height:30px;border-radius:50%;background:var(--primary);color:var(--primary-ink);display:grid;place-items:center;font-weight:800;font-size:13px;flex:none}
.userbtn .who{display:flex;flex-direction:column;text-align:left;line-height:1.15}
.userbtn .who b{font-size:13px}
.userbtn .who small{color:var(--muted);font-size:11.5px}
.pop{position:absolute;right:0;top:calc(100% + 8px);min-width:210px;background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:6px;z-index:20}
.pop[hidden]{display:none}
.pop button{display:flex;gap:10px;align-items:center;width:100%;border:0;background:none;padding:9px 12px;border-radius:8px;cursor:pointer;font-weight:600;text-align:left}
.pop button:hover{background:var(--surface-2)}
.roles{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:20px}
.roles .panel{padding:16px 18px;box-shadow:none}
.pwgen{display:flex;gap:8px}
.pwgen .inp{flex:1;width:auto;min-width:0}
.fs{border:0;padding:0;margin:0;min-width:0}
.dlg-body.two.pr{grid-template-columns:minmax(260px,.85fr) minmax(0,1.15fr)}
.pcontrols{display:flex;flex-direction:column;gap:18px}
.ppreview{display:flex;flex-direction:column;align-items:center;background:var(--surface-2);border-radius:12px;padding:16px;min-width:0;overflow:hidden}
.paper-wrap{overflow:hidden;border-radius:3px;box-shadow:0 6px 24px -8px rgba(0,0,0,.35);background:#fff;flex:none}
.paper{background:#fff;box-sizing:border-box;transform-origin:top left}
.pgrid{display:grid;justify-content:start;align-content:start}
.pnote{background:var(--surface-2);border-radius:10px;padding:10px 14px;font-weight:600}
.pnote.warn{background:var(--warn-soft);color:var(--warn)}
.seg button[disabled]{opacity:.45;cursor:not-allowed}
@media (max-width:980px){.login{grid-template-columns:1fr;overflow-y:auto}.login-art{padding:24px 22px 26px;gap:16px;grid-template-rows:auto auto;overflow:visible}.login-main{gap:14px}.login-art h2{font-size:clamp(25px,5.2vw,34px);max-width:22ch}.login-art .stage{display:none}.login-art p{font-size:13px;max-width:60ch}.login-brand img{width:56px;height:56px;padding:8px;border-radius:14px}.login-form{width:100%;margin:0;padding:26px 20px 30px;border:0;border-radius:0;box-shadow:none;background:transparent;overflow:visible;max-height:none}.roles{grid-template-columns:1fr}.userbtn .who{display:none}.dlg-body.two.pr{grid-template-columns:1fr}}
@media (max-width:1100px){.login{height:100dvh;overflow:hidden}.login{overflow-y:auto}}

/* inline editing, history, audit */
td.ed{cursor:text}
td.ed:hover{background:var(--primary-soft)}
.cell-in{padding:5px 8px!important;min-width:110px;border-radius:7px!important}
.taglink{border:0;background:none;padding:0;cursor:pointer;text-align:left}
.taglink:hover .tagcode{text-decoration:underline;text-underline-offset:3px}
.hint{margin:-4px 0 12px}
.tagin{font-family:var(--mono);text-transform:uppercase;font-weight:600}
.chg{font-size:13px;margin-top:2px;word-break:break-word}
.chg span,.chg s{color:var(--muted)}
.hist{max-height:300px}
.audit{list-style:none;margin:0;padding:0}
.arow{border-top:1px solid var(--line)}
.arow:first-child{border-top:0}
.ahead{display:grid;grid-template-columns:172px 165px 135px 190px 1fr 20px;gap:14px;padding:12px 18px;align-items:baseline}
.ahead.has{cursor:pointer}
.ahead.has:hover{background:var(--surface-2)}
.ahead time{color:var(--muted);font-size:13px;white-space:nowrap}
.ahead .car{color:var(--muted)}
.adetail{padding:6px 18px 18px;background:var(--surface-2)}
.chtbl{border-collapse:collapse;min-width:min(560px,100%);margin:8px 0}
.chtbl th,.chtbl td{text-align:left;padding:7px 12px;border:1px solid var(--line);font-size:13px;vertical-align:top;word-break:break-word}
.chtbl th{background:var(--surface);color:var(--muted)}
.meta{margin:6px 0 10px}
.kids{margin-top:10px;display:flex;flex-direction:column;gap:4px;max-height:240px;overflow:auto}
.kid{display:flex;gap:12px;font-size:13px}
.dt{display:flex;align-items:center;gap:8px}
.dt .inp{width:auto}
.tbl td.c-act{white-space:nowrap}
@media (max-width:900px){.ahead{grid-template-columns:1fr;gap:2px}}

/* QR label, activity filters, snapshots */
.label.qr .lb-mid{display:flex;align-items:center;gap:calc(var(--u)*1.5mm);height:calc(var(--u)*15.6mm)}
.label.qr .lb-qr{flex:none;width:calc(var(--u)*15.6mm);height:calc(var(--u)*15.6mm)}
.label.qr .lb-qr svg{display:block;width:100%;height:100%}
.label.qr .lb-tag{flex:1;min-width:0;text-align:center}
.filters{margin-bottom:16px;display:flex;flex-direction:column;gap:14px}
.frow{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.frow .grow{flex:1;min-width:240px}
.fgrid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.chiprow{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chiprow .lab{color:var(--muted);font-weight:700;font-size:13px;margin-right:4px}
.chip-btn.on{background:var(--primary);border-color:var(--primary);color:var(--primary-ink)}
/* snapshot/history improved */
.snap-shell{display:flex;flex-direction:column;gap:16px}
.snap-hero{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.snap-hero-left{display:flex;flex-direction:column;gap:6px}
.snap-hero .tag{font-family:var(--mono);font-size:14px;color:var(--ink);background:var(--primary-soft);padding:2px 6px;border-radius:4px;display:inline-block;margin:4px 0}
.snap-meta{display:flex;gap:10px;flex-wrap:wrap;align-items:center;color:var(--muted)}
.snap-pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--surface-2);border-radius:999px;padding:4px 10px;font-weight:700;font-size:12.5px}
.snap-grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.snap-card{grid-column:1/-1;background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow)}
.snap-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line)}
.snap-card-head h3{font-size:16px;font-weight:800}
.snap-table{width:100%;border-collapse:collapse;table-layout:fixed}
.snap-table th{text-align:left;font-size:12.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);padding:10px 18px;background:var(--surface-2);border-bottom:1px solid var(--line);white-space:nowrap}
.snap-table td{padding:12px 18px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-word}
.snap-table tr{transition:background .15s}
.snap-table tr:nth-child(even){background:var(--surface-2)}
.snap-table .field{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.snap-table .val{font-size:13px}
.snap-table tr.chg{background:linear-gradient(90deg,rgba(59,130,246,.08),transparent)}
.snap-table tr.chg .val.old{color:var(--muted)}
.snap-table tr.chg .val.new{font-weight:700;color:var(--ink)}
.snap-empty{padding:18px;text-align:center;color:var(--muted)}
.snap-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
@media (max-width:760px){.snap-table th,.snap-table td{padding:10px 14px}}
.snban.warn{background:var(--warn-soft);color:var(--warn)}
.snban.ok{background:var(--ok-soft);color:var(--ok);display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.snban.ok{background:var(--ok-soft) !important;color:var(--ok) !important}
.snban>span:first-child{font-weight:600}
.snhead{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:12px}
@media (max-width:1100px){.fgrid4{grid-template-columns:repeat(2,minmax(0,1fr))}}

/* responsive */
@media (max-width:1100px){.grid2,.gen,.settings{grid-template-columns:1fr}.pv{position:static}}
@media (max-width:900px){
  .app{grid-template-columns:1fr}
  .side{position:static;height:auto;flex-direction:row;align-items:center;padding:10px 12px;gap:8px;overflow-x:auto}
  .brand{padding:0 10px 0 0}.brand span,.side-foot{display:none}
  .brand img{width:38px;height:38px;padding:4px;border-radius:10px}
  #nav{display:flex;gap:4px}#nav a{margin:0;white-space:nowrap}
  .top{padding:12px 16px}.content{padding:20px 16px 60px}
  .dlg-body.two{grid-template-columns:1fr}
  .recent li{grid-template-columns:1fr}
  .timeline li{grid-template-columns:1fr;gap:2px}
}
@media (max-width:560px){.fgrid{grid-template-columns:1fr}.kpis{grid-template-columns:1fr}.top .searchbox{display:none}.bar-row{grid-template-columns:90px 1fr 36px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <img src="/logo.png" alt="" width="46" height="46" onerror="this.remove()">
      <div><b>TAG Master</b><span>BluePeak</span></div>
    </div>
    <nav id="nav" aria-label="Main"></nav>
    <div class="side-foot"><span class="dot" id="syncDot"></span><span id="syncTxt">Database in use</span><div id="sideDb" style="margin-top:4px;font-family:var(--mono);font-size:11.5px;word-break:break-all"></div></div>
  </aside>
  <div class="main">
    <header class="top">
      <h1 id="title">Overview</h1>
      <div class="searchbox"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input id="gsearch" class="inp" type="search" placeholder="Find a tag, serial or person" aria-label="Find a tag, serial or person"><kbd>/</kbd></div>
      <button class="icon-btn" id="scanBtn" title="Scan or type a tag to open it" aria-label="Scan a tag"><svg class="i" viewBox="0 0 24 24"><path d="M4 7V5a1 1 0 0 1 1-1h2M17 4h2a1 1 0 0 1 1 1v2M20 17v2a1 1 0 0 1-1 1h-2M7 20H5a1 1 0 0 1-1-1v-2"/><path d="M4 12h16"/></svg></button>
      <button class="icon-btn" id="themeBtn" title="Switch light or dark theme" aria-label="Switch theme"></button>
      <div class="usermenu"><button class="userbtn" id="userBtn" aria-haspopup="true"></button><div class="pop" id="userPop" hidden><button id="popPw"></button><button id="popOut"></button><button id="popExit" hidden></button></div></div>
    </header>
    <div id="errbar" class="errbar" hidden role="alert"></div>
    <div id="updbar" class="updbar" hidden role="status"></div>
    <main id="view" class="content"></main>
  </div>
</div>
<div id="login" class="login" hidden>
  <div class="login-art">
    <div class="login-brand">
      <img src="/logo.png" alt="BluePeak TAG Master" width="68" height="68" onerror="this.style.display='none'">
      <div class="brand-copy"><b>TAG Master</b><span>BluePeak</span></div>
    </div>
    <div class="login-main">
      <h2>Every device tagged and accounted for.</h2>
      <div class="stage" id="loginLabel"></div>
    </div>
    <p>Create, print and track asset tags for all ICT equipment from one place.</p>
  </div>
  <form class="login-form" id="loginForm" autocomplete="on">
    <h2>Sign in</h2>
    <p class="sub">Use the account your administrator gave you.</p>
    <label class="field"><span>Username</span><input class="inp" name="username" autocomplete="username" required autocapitalize="off" spellcheck="false"></label>
    <label class="field"><span>Password</span><input class="inp" type="password" name="password" autocomplete="current-password" required></label>
    <div class="form-err" id="loginErr" hidden></div>
    <button class="btn primary lg" type="submit" id="loginBtn">Sign in</button>
  </form>
</div>
<dialog id="dlgPw"></dialog>
<dialog id="dlgScan"></dialog>
<dialog id="dlgBulk"></dialog>
<dialog id="dlgSnap"></dialog>
<dialog id="dlgUser"></dialog>
<dialog id="dlgDevice"></dialog>
<dialog id="dlgLabels"></dialog>
<dialog id="dlgConfirm"></dialog>
<dialog id="dlgImport"></dialog>
<dialog id="dlgCat"></dialog>
<div id="toasts" aria-live="polite"></div>
<div id="printRoot"></div>
<noscript>This app needs JavaScript. Please use a current version of Chrome, Edge or Firefox.</noscript>

<script>
(()=>{'use strict';
/* ---------- helpers ---------- */
const STATUSES=['In stock','Assigned','In repair','Lost','Retired'];
const STATUS_CLASS={'In stock':'info','Assigned':'ok','In repair':'warn','Lost':'bad','Retired':'mute'};
const STATUS_COLOR={'In stock':'var(--info)','Assigned':'var(--ok)','In repair':'var(--warn)','Lost':'var(--bad)','Retired':'var(--muted)'};
const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ICONS={
 search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 printer:'<path d="M6 9V3h12v6"/><rect x="6" y="14" width="12" height="7" rx="1"/><path d="M6 17H4a1 1 0 0 1-1-1v-5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v5a1 1 0 0 1-1 1h-2"/>',
 download:'<path d="M12 3v12m0 0-4-4m4 4 4-4"/><path d="M4 20h16"/>',
 upload:'<path d="M12 15V3m0 0-4 4m4-4 4 4"/><path d="M4 20h16"/>',
 trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
 moon:'<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
 sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
 grid:'<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
 tag:'<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V4a1 1 0 0 1 1-1h9l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="8" cy="8" r="1.3"/>',
 list:'<path d="M4 6h16M4 12h16M4 18h10"/>',
 pulse:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',
 sliders:'<path d="M4 6h8M18 6h2M4 12h2M12 12h8M4 18h10M20 18h0"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
 x:'<path d="M6 6l12 12M18 6 6 18"/>',
 edit:'<path d="M4 20h4L19 9l-4-4L4 16z"/>',
 copy:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
 users:'<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5"/>',
 ky:'<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9m-3 3 3 3"/>',
 out:'<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 8l-4 4 4 4M6 12h10"/>',
 db:'<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
 eye:'<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
 eyeOff:'<path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3 3.9M6.6 6.6A17.3 17.3 0 0 0 2 12s3.6 7 10 7a9.9 9.9 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M2 2l20 20"/>',
 arrow:'<path d="M5 12h14M13 6l6 6-6 6"/>',
 box:'<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 check:'<path d="m5 13 4 4L19 7"/>',
 shield:'<path d="M12 3l7 3v6c0 4.4-3 7.4-7 9-4-1.6-7-4.6-7-9V6z"/><path d="m9 12 2 2 4-4"/>',
 refresh:'<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'
};
const icon=(n,s)=>`<svg class="i" viewBox="0 0 24 24"${s?` style="width:${s}px;height:${s}px"`:''} aria-hidden="true">${ICONS[n]||''}</svg>`;
function pwToggles(root){
  $$('input[type=password]',root||document).forEach(i=>{
    if(i.closest('.pw-wrap'))return;
    const w=document.createElement('div');w.className='pw-wrap';
    i.parentNode.insertBefore(w,i);w.append(i);
    const b=document.createElement('button');b.type='button';b.className='pw-eye';
    const set=show=>{i.type=show?'text':'password';b.innerHTML=icon(show?'eyeOff':'eye');b.title=show?'Hide password':'Show password';b.setAttribute('aria-label',b.title)};
    set(false);b.onclick=()=>set(i.type==='password');
    w.append(b);
  });
}
const qs=o=>Object.entries(o).filter(([k,v])=>v!==''&&v!==null&&v!==undefined).map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(v)).join('&');
const fmtDT=ts=>{if(!ts)return '';const d=new Date(ts.replace(' ','T')+'Z');return d.toLocaleString(undefined,{day:'numeric',month:'short',year:'numeric',hour:'numeric',minute:'2-digit'})};
const chip=s=>`<span class="chip ${STATUS_CLASS[s]||'mute'}">${esc(s)}</span>`;
const tagParts=t=>{const m=/^(.*?)(\d+)$/.exec(t||'');return m?`<span class="tagcode"><span class="tp">${esc(m[1])}</span><b>${esc(m[2])}</b></span>`:`<span class="tagcode">${esc(t)}</span>`};
const num=n=>Number(n).toLocaleString();
const dl=url=>{const a=document.createElement('a');a.href=url;a.download='';document.body.append(a);a.click();a.remove()};

async function api(path,{method='GET',body}={}){
  const headers={'X-Requested-With':'BluePeak'};if(body)headers['Content-Type']='application/json';
  const res=await fetch('/api'+path,{method,headers,body:body?JSON.stringify(body):undefined,credentials:'same-origin'});
  let data=null;try{data=await res.json()}catch(e){}
  if(!res.ok){
    if(res.status===401&&data&&data.code==='auth')showLogin('Your session ended. Please sign in again.');
    throw new Error((data&&data.error)||('Request failed ('+res.status+')'));
  }
  return data;
}
function toast(msg,kind){const t=document.createElement('div');t.className='toast '+(kind||'');t.textContent=msg;$('#toasts').append(t);setTimeout(()=>t.classList.add('out'),3400);setTimeout(()=>t.remove(),3900)}
function errorBanner(msg){
  const el=$('#errbar');if(!el)return;
  el.innerHTML=`<span>${esc(msg)}</span><button class="btn sm" id="errCopy">Copy</button><button class="btn sm" id="errClose">Dismiss</button>`;
  el.hidden=false;
  $('#errClose').onclick=()=>{el.hidden=true};
  $('#errCopy').onclick=()=>{navigator.clipboard.writeText(msg).then(()=>toast('Error copied'),()=>{})};
}
function fail(msg){errorBanner(msg);toast(msg,'bad')}

/* ---------- state ---------- */
const S={user:null,settings:{},categories:[],locations:[],departments:[],year:new Date().getFullYear(),
  inv:{page:1,size:50,q:'',category:'',status:'',location:'',batch:'',warranty:'',sort:'created_at',dir:'desc',total:0,rows:[],sel:new Set(),allMatching:false}};
async function loadMeta(){const m=await api('/meta');S.settings=m.settings;S.categories=m.categories;S.locations=m.locations;S.departments=m.departments;S.year=m.year;S.dbFile=m.db_file;S.cloud={enabled:!!m.cloud_enabled,database:m.cloud_database||''};
  if(m.user&&m.user.role==='admin'){try{const c=await api('/cloud');S.cloud={enabled:!!c.enabled,database:c.database||S.cloud.database,pending:!!c.pending,last_error:c.last_error||''}}catch(e){}}
  paintSync(S.cloud)}
function paintSync(c){
  const dot=$('#syncDot'),txt=$('#syncTxt');if(!dot||!txt)return;
  if(c)S.cloud=c;
  const cloud=S.cloud||{enabled:false},nm=cloud.database||'cloud database',loc=S.dbFile||'local database';
  if(cloud.enabled&&cloud.last_error){dot.style.background='var(--bad)';txt.textContent='SQLite Cloud "'+nm+'" - last sync failed'}
  else if(cloud.enabled&&cloud.pending){dot.style.background='var(--warn)';txt.textContent='SQLite Cloud "'+nm+'" - changes waiting'}
  else if(cloud.enabled){dot.style.background='#3DD68C';txt.textContent='SQLite Cloud "'+nm+'" - in use'}
  else{dot.style.background='#3DD68C';txt.textContent='Local "'+loc+'" - in use on this computer'}
  paintSideDb();
}
function paintSideDb(){const el=$('#sideDb');if(!el)return;const cloud=S.cloud||{},loc=S.dbFile||'local database';
  el.textContent=(cloud.enabled&&cloud.database)?('Local: '+loc+'  |  Cloud: '+cloud.database):('Local: '+loc)}
async function loadUpdate(){
  if(!S.user||S.user.role!=='admin'){paintUpdate(null);return}
  try{const u=await api('/update');S.update=u;paintUpdate(u)}catch(e){}
}
function paintUpdate(u){
  const bar=$('#updbar');if(!bar)return;
  if(!u||!u.available){bar.hidden=true;bar.innerHTML='';return}
  bar.innerHTML=`<span class="upd-ico">${icon('refresh',16)}</span><span class="upd-txt">Version <b>${esc(u.latest)}</b> of TAG Master is available.${u.notes?`<div class="sub">${esc(u.notes)}</div>`:''}</span>${can('admin')?'<button class="btn sm primary" id="updNow">Update now</button>':''}<button class="btn sm" id="updLater">Later</button>`;
  bar.hidden=false;
  const now=$('#updNow',bar);if(now)now.onclick=installUpdate;
  $('#updLater',bar).onclick=()=>{bar.hidden=true};
}
async function installUpdate(){
  const u=S.update||{};
  const ok=await confirmBox({title:'Update TAG Master?',body:`TAG Master will close, install version <b>${esc(u.latest||'')}</b> and start again. Your database and settings are kept.`,ok:'Update now'});
  if(!ok)return;
  toast('Downloading update...');
  try{await api('/update/install',{method:'POST',body:{}});toast('Installing update...')}
  catch(e){fail(e.message)}
}
function cloudStatHTML(c){
  const counts=c.counts||{};
  return `<dt>Cloud connection</dt><dd>${c.enabled?(c.url?esc(c.url)+' &rarr; <b>'+esc(c.database||'')+'</b>':'Enabled, but no address saved yet'):'Cloud sync is off'}</dd>
    <dt>Last push</dt><dd>${c.last_push?fmtDT(c.last_push):'Never'}</dd>
    <dt>Last pull</dt><dd>${c.last_pull?fmtDT(c.last_pull):'Never'}</dd>
    <dt>Local copy</dt><dd>${num(counts.devices||0)} devices, ${num(counts.users||0)} users, ${num(counts.activity||0)} audit entries</dd>
    <dt>Waiting to sync</dt><dd>${c.pending?'<span style="color:var(--warn);font-weight:700">Yes - push your changes when you are ready</span>':'No'}</dd>
    ${c.last_error?`<dt>Last error</dt><dd style="color:var(--bad);font-weight:600">${esc(c.last_error)}${c.last_error_at?`<div class="sub" style="font-weight:400">${fmtDT(c.last_error_at)}</div>`:''}</dd>`:''}`;
}
function previewTag(st,code,year,seq){const p=[st.prefix,code];if(st.include_year)p.push(year);p.push(String(seq).padStart(st.pad,'0'));return p.filter(x=>x!=='').join(st.sep)}

/* ---------- Code 128 barcode (subset B) ---------- */
const C128=['212222','222122','222221','121223','121322','131222','122213','122312','132212','221213','221312','231212','112232','122132','122231','113222','123122','123221','223211','221132','221231','213212','223112','312131','311222','321122','321221','312212','322112','322211','212123','212321','232121','111323','131123','131321','112313','132113','132311','211313','231113','231311','112133','112331','132131','113123','113321','133121','313121','211331','231131','213113','213311','213131','311123','311321','331121','312113','312311','332111','314111','221411','431111','111224','111422','121124','121421','141122','141221','112214','112412','122114','122411','142112','142211','241211','221114','413111','241112','134111','111242','121142','121241','114212','124112','124211','411212','421112','421211','212141','214121','412121','111143','111341','131141','114113','114311','411113','411311','113141','114131','311141','411131','211412','211214','211232','2331112'];
function code128(text){
  const vals=[104];let sum=104;
  [...text].forEach((ch,i)=>{let v=ch.charCodeAt(0)-32;if(v<0||v>95)v=0;vals.push(v);sum+=v*(i+1)});
  vals.push(sum%103);vals.push(106);
  let x=10,bars='';
  vals.forEach(v=>{const p=C128[v];for(let i=0;i<p.length;i++){const w=+p[i];if(i%2===0)bars+=`<rect x="${x}" y="0" width="${w}" height="50"/>`;x+=w}});
  return `<svg viewBox="0 0 ${x+10} 50" preserveAspectRatio="none" fill="#000" shape-rendering="crispEdges" role="img" aria-label="Barcode for ${esc(text)}">${bars}</svg>`;
}
/* ---------- QR code encoder (byte mode, level M, versions 1 to 6) ---------- */
const QR_ECC={1:[10,1],2:[16,1],3:[26,1],4:[18,2],5:[24,2],6:[16,4]},QR_RAW={1:26,2:44,3:70,4:100,5:134,6:172},QR_ALIGN={1:[],2:[6,18],3:[6,22],4:[6,26],5:[6,30],6:[6,34]};
const qrCache=new Map();
function gfMul(x,y){let z=0;for(let i=7;i>=0;i--){z=(z<<1)^((z>>>7)*0x11D);z^=((y>>>i)&1)*x}return z}
function rsDivisor(deg){const r=new Array(deg-1).fill(0).concat([1]);let root=1;for(let i=0;i<deg;i++){for(let j=0;j<r.length;j++){r[j]=gfMul(r[j],root);if(j+1<r.length)r[j]^=r[j+1]}root=gfMul(root,2)}return r}
function rsRemainder(data,div){const r=new Array(div.length).fill(0);for(const b of data){const f=b^r.shift();r.push(0);div.forEach((c,i)=>{r[i]^=gfMul(c,f)})}return r}
function qrPenalty(m){
  const n=m.length;let res=0;const cols=[];
  for(let x=0;x<n;x++){const c=[];for(let y=0;y<n;y++)c.push(m[y][x]);cols.push(c)}
  for(const lines of [m,cols])for(const line of lines){let run=1;for(let i=1;i<n;i++){if(line[i]===line[i-1])run++;else{if(run>=5)res+=3+(run-5);run=1}}if(run>=5)res+=3+(run-5)}
  for(let y=0;y<n-1;y++)for(let x=0;x<n-1;x++){const c=m[y][x];if(c===m[y][x+1]&&c===m[y+1][x]&&c===m[y+1][x+1])res+=3}
  const p1=[1,0,1,1,1,0,1,0,0,0,0],p2=[0,0,0,0,1,0,1,1,1,0,1];
  for(const lines of [m,cols])for(const line of lines){const li=line.map(v=>v?1:0);for(let i=0;i<=n-11;i++){let a=true,b=true;for(let k=0;k<11;k++){if(li[i+k]!==p1[k])a=false;if(li[i+k]!==p2[k])b=false}if(a)res+=40;if(b)res+=40}}
  let dark=0;for(const row of m)for(const v of row)if(v)dark++;
  const total=n*n,k=Math.floor((Math.abs(dark*20-total*10)+total-1)/total)-1;
  return res+k*10;
}
function qrMatrix(text){
  if(qrCache.has(text))return qrCache.get(text);
  const data=[...new TextEncoder().encode(text)];
  let ver=0;
  for(let v=1;v<=6;v++){const cap=QR_RAW[v]-QR_ECC[v][0]*QR_ECC[v][1];if(12+data.length*8<=cap*8){ver=v;break}}
  if(!ver)throw new Error('Text is too long for a QR code label');
  const [eccLen,nblocks]=QR_ECC[ver],raw=QR_RAW[ver],capbits=(raw-eccLen*nblocks)*8;
  const bits=[];const put=(val,n)=>{for(let i=n-1;i>=0;i--)bits.push((val>>>i)&1)};
  put(4,4);put(data.length,8);data.forEach(b=>put(b,8));
  put(0,Math.min(4,capbits-bits.length));put(0,(8-bits.length%8)%8);
  let pad=0xEC;while(bits.length<capbits){put(pad,8);pad^=0xEC^0x11}
  const cw=[];for(let i=0;i<bits.length;i+=8){let v=0;for(let k=0;k<8;k++)v=(v<<1)|bits[i+k];cw.push(v)}
  const nshort=nblocks-raw%nblocks,shortLen=Math.floor(raw/nblocks),div=rsDivisor(eccLen),blocks=[];let k0=0;
  for(let i=0;i<nblocks;i++){const dlen=shortLen-eccLen+(i<nshort?0:1);let dat=cw.slice(k0,k0+dlen);k0+=dlen;const ecc=rsRemainder(dat,div);if(i<nshort)dat=dat.concat([0]);blocks.push(dat.concat(ecc))}
  const fin=[];for(let i=0;i<blocks[0].length;i++)blocks.forEach((blk,j)=>{if(i!==shortLen-eccLen||j>=nshort)fin.push(blk[i])});
  const size=ver*4+17,mods=[],isfn=[];
  for(let y=0;y<size;y++){mods.push(new Array(size).fill(false));isfn.push(new Array(size).fill(false))}
  const setfn=(x,y,dark)=>{mods[y][x]=dark;isfn[y][x]=true};
  for(let i=0;i<size;i++){setfn(6,i,i%2===0);setfn(i,6,i%2===0)}
  for(const [cx,cy] of [[3,3],[size-4,3],[3,size-4]])for(let dy=-4;dy<=4;dy++)for(let dx=-4;dx<=4;dx++){const dist=Math.max(Math.abs(dx),Math.abs(dy)),xx=cx+dx,yy=cy+dy;if(xx>=0&&xx<size&&yy>=0&&yy<size)setfn(xx,yy,dist!==2&&dist!==4)}
  const pos=QR_ALIGN[ver],n=pos.length;
  for(let i=0;i<n;i++)for(let j=0;j<n;j++){if(!((i===0&&j===0)||(i===0&&j===n-1)||(i===n-1&&j===0))){for(let dy=-2;dy<=2;dy++)for(let dx=-2;dx<=2;dx++)setfn(pos[i]+dx,pos[j]+dy,Math.max(Math.abs(dx),Math.abs(dy))!==1)}}
  const drawFormat=mask=>{
    const d5=mask;let rem=d5;for(let i=0;i<10;i++)rem=(rem<<1)^((rem>>>9)*0x537);
    const fb=((d5<<10)|rem)^0x5412,bit=i=>((fb>>>i)&1)===1;
    for(let i=0;i<6;i++)setfn(8,i,bit(i));
    setfn(8,7,bit(6));setfn(8,8,bit(7));setfn(7,8,bit(8));
    for(let i=9;i<15;i++)setfn(14-i,8,bit(i));
    for(let i=0;i<8;i++)setfn(size-1-i,8,bit(i));
    for(let i=8;i<15;i++)setfn(8,size-15+i,bit(i));
    setfn(8,size-8,true);
  };
  drawFormat(0);
  let bi=0,right=size-1;
  while(right>=1){
    if(right===6)right=5;
    for(let vert=0;vert<size;vert++)for(let j=0;j<2;j++){
      const x=right-j,upward=((right+1)&2)===0,y=upward?size-1-vert:vert;
      if(!isfn[y][x]&&bi<fin.length*8){mods[y][x]=((fin[bi>>>3]>>>(7-(bi&7)))&1)===1;bi++}
    }
    right-=2;
  }
  const applyMask=mk=>{
    for(let y=0;y<size;y++)for(let x=0;x<size;x++){
      let inv;
      switch(mk){case 0:inv=(x+y)%2===0;break;case 1:inv=y%2===0;break;case 2:inv=x%3===0;break;case 3:inv=(x+y)%3===0;break;
        case 4:inv=(Math.floor(x/3)+Math.floor(y/2))%2===0;break;case 5:inv=x*y%2+x*y%3===0;break;
        case 6:inv=(x*y%2+x*y%3)%2===0;break;default:inv=((x+y)%2+x*y%3)%2===0}
      if(inv&&!isfn[y][x])mods[y][x]=!mods[y][x];
    }
  };
  let best=0,bestPen=null;
  for(let mk=0;mk<8;mk++){applyMask(mk);drawFormat(mk);const pen=qrPenalty(mods);if(bestPen===null||pen<bestPen){best=mk;bestPen=pen}applyMask(mk)}
  applyMask(best);drawFormat(best);
  qrCache.set(text,mods);if(qrCache.size>5000)qrCache.clear();
  return mods;
}
function qrSvg(text){
  const m=qrMatrix(text),n=m.length;let d='';
  m.forEach((row,y)=>{let x=0;while(x<n){if(row[x]){let x2=x;while(x2<n&&row[x2])x2++;d+=`M${x+2} ${y+2}h${x2-x}v1h-${x2-x}z`;x=x2}else x++}});
  return `<svg viewBox="0 0 ${n+4} ${n+4}" shape-rendering="crispEdges" role="img" aria-label="QR code for ${esc(text)}"><path d="${d}" fill="#000"/></svg>`;
}
function brandName(){const b=S.settings&&S.settings.label_brand;return b==null?'BluePeak':b}
function labelHTML(tag,cat,size,k,image){
  let qr=!!(S.settings&&S.settings.label_code==='qr'),qrs='';
  if(qr){try{qrs=qrSvg(tag)}catch(e){qr=false}}
  const fs=(qr?Math.min(3.3,26.5/(tag.length*0.6)):Math.min(3.3,43/(tag.length*0.62))).toFixed(2);
  const sty=`--fs:${fs}${k&&k!==1?';--u:'+(({sm:1,md:1.4,lg:2})[size]*k).toFixed(3):''}`;
  const head=`<div class="lb-head"><b>${esc(brandName())}</b><span>${esc(cat||'')}</span></div>`;
  const foot=`<div class="lb-foot">${esc((S.settings&&S.settings.label_footer)||'')}</div>`;
  let img='';
  if(image){img=`<img src="${image}" style="max-width:calc(100% - 4mm);max-height:18mm;object-fit:contain;margin:2mm auto;display:block"/>`}
  if(qr)return `<div class="label qr ${size}" style="${sty}">${head}<div class="lb-mid"><div class="lb-qr">${qrs}</div><div class="lb-tag">${esc(tag)}</div></div>${foot}${img}</div>`;
  return `<div class="label ${size}" style="${sty}">${head}<div class="lb-bar">${code128(tag)}</div><div class="lb-tag">${esc(tag)}</div>${foot}${img}</div>`;
}

/* ---------- dialogs ---------- */
function openDialog(id,html){let d=$('#'+id);if(!d){d=document.createElement('dialog');d.id=id;document.body.append(d)}d.innerHTML=html;d.onclick=e=>{if(e.target===d)d.close()};$$('[data-close]',d).forEach(b=>b.onclick=()=>d.close());pwToggles(d);if(!d.open)d.showModal();return d}
function confirmBox({title,body,ok='Confirm',danger=false}){
  return new Promise(res=>{
    const d=$('#dlgConfirm');d.returnValue='';
    openDialog('dlgConfirm',`<div class="dlg sm"><h2>${esc(title)}</h2><p>${body}</p><div class="dlg-foot"><button class="btn" data-r="0">Cancel</button><button class="btn ${danger?'danger':'primary'}" data-r="1">${esc(ok)}</button></div></div>`);
    d.onclose=()=>res(d.returnValue==='1');
    $$('[data-r]',d).forEach(b=>b.onclick=()=>d.close(b.dataset.r));
  });
}

/* ---------- labels: page layout, preview, print and PDF ---------- */
const UBASE={sm:1,md:1.4,lg:2};
const PAPER={a4:[210,297,'A4'],letter:[215.9,279.4,'Letter']};
function pageLayout(n,o){
  const margin=Math.max(0,+o.margin||0),gap=Math.max(0,+o.gap||0);
  const bw=50*UBASE[o.size],bh=25*UBASE[o.size];
  if(o.paper==='roll')return{k:1,lw:bw,lh:bh,cols:1,rows:1,per:1,W:bw,H:bh,margin:0,gap:0,pages:n,roll:true};
  let [W,H]=PAPER[o.paper];if(o.orient==='landscape')[W,H]=[H,W];
  const aw=W-2*margin-0.5,ah=H-2*margin-1;
  const calc=k=>{const lw=bw*k,lh=bh*k;const cols=Math.max(0,Math.floor((aw+gap)/(lw+gap))),rows=Math.max(0,Math.floor((ah+gap)/(lh+gap)));return{k,lw,lh,cols,rows,per:cols*rows}};
  let L=calc(1);
  if(o.fit&&L.per<n){for(let k=0.95;k>=0.599;k-=0.05){L=calc(k);if(L.per>=n)break}}
  return{...L,W,H,margin,gap,pages:L.per?Math.ceil(n/L.per):0};
}
async function apiBlob(path,body){
  const res=await fetch('/api'+path,{method:'POST',headers:{'X-Requested-With':'BluePeak','Content-Type':'application/json'},body:JSON.stringify(body),credentials:'same-origin'});
  if(!res.ok){let m='Request failed';try{m=(await res.json()).error||m}catch(e){}if(res.status===401)showLogin('Your session ended. Please sign in again.');throw new Error(m)}
  const cd=res.headers.get('Content-Disposition')||'';const name=(/filename="([^"]+)"/.exec(cd)||[])[1]||'labels.pdf';
  const blob=await res.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();
  setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},1500);
}
function openLabels(devs){
  devs=(devs||[]).filter(d=>d&&d.tag&&d.id);
  if(!devs.length){toast('There is nothing to print.','bad');return}
  const o={size:S.settings.label_size||'md',orient:'portrait',margin:8,gap:3,fit:true};
  try{const s=JSON.parse(localStorage.getItem('bp-print')||'{}');['size','orient','margin','gap','fit'].forEach(k=>{if(s[k]!==undefined)o[k]=s[k]})}catch(e){}
  o.paper=S.settings.label_paper==='letter'?'letter':'a4';
  if(!UBASE[o.size])o.size='md';
  const n=devs.length;
  const seg=(k,items)=>`<div class="seg" data-k="${k}">${items.map(([v,l])=>`<button type="button" data-v="${v}">${l}</button>`).join('')}</div>`;
  const d=openDialog('dlgLabels',`<div class="dlg"><div class="dlg-head"><div><h2>Print or save labels</h2><p class="sub">${num(n)} label${n>1?'s':''} selected</p></div><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <div class="dlg-body two pr">
      <div class="pcontrols">
        <div class="field"><span>Label size</span>${seg('size',[['sm','Small'],['md','Medium'],['lg','Large']])}<small>Small 50 x 25 mm, Medium 70 x 35 mm, Large 100 x 50 mm</small></div>
        <div class="field"><span>Paper</span>${seg('paper',[['a4','A4'],['letter','Letter'],['roll','Label roll']])}<small>A4 is the default. Use Label roll for a label printer: one label per page.</small></div>
        <div class="field"><span>Orientation</span>${seg('orient',[['portrait','Portrait'],['landscape','Landscape']])}</div>
        <div class="fgrid"><label class="field"><span>Page margin (mm)</span><input class="inp" type="number" id="pMargin" min="0" max="30" step="1"></label><label class="field"><span>Gap between labels (mm)</span><input class="inp" type="number" id="pGap" min="0" max="20" step="0.5"></label></div>
        <label class="check"><input type="checkbox" id="pFit"> Shrink labels to fit everything on one page</label>
        <div class="pnote" id="pNote"></div>
      </div>
      <div class="ppreview"><div id="pPaper"></div><p class="sub" style="margin-top:10px;text-align:center" id="pCap"></p></div>
    </div>
    <div class="dlg-foot"><span class="spacer"></span><button class="btn" data-close>Close</button><button class="btn" id="lbPdf">${icon('download')} Save as PDF</button><button class="btn primary" id="lbPrint"></button></div></div>`);
  const grid=(L,list)=>`<div style="display:grid;grid-template-columns:repeat(${L.cols},${L.lw}mm);grid-auto-rows:${L.lh}mm;gap:${L.gap}mm;justify-content:center;align-content:start">${list.map(x=>labelHTML(x.tag,x.category_name,o.size,L.k,x.image)).join('')}</div>`;
  const paint=()=>{
    const L=pageLayout(n,o);
    $$('.seg',d).forEach(sg=>$$('button',sg).forEach(b=>{b.classList.toggle('on',String(o[sg.dataset.k])===b.dataset.v);if(sg.dataset.k==='orient')b.disabled=o.paper==='roll'}));
    $('#pMargin',d).disabled=$('#pGap',d).disabled=$('#pFit',d).disabled=o.paper==='roll';
    const pr=$('#lbPrint',d),pdf=$('#lbPdf',d),paper=$('#pPaper',d);
    let note='',warn=false;
    if(!L.per){
      paper.innerHTML='';pr.disabled=pdf.disabled=true;pr.textContent='Nothing fits';
      $('#pNote',d).className='pnote warn';$('#pNote',d).textContent='A single label is larger than the printable area. Reduce the margin or choose a smaller label.';$('#pCap',d).textContent='';return;
    }
    if(L.roll)note=`One label per page, ${num(n)} page${n>1?'s':''}, each the size of the label.`;
    else if(L.pages===1)note=`All ${num(n)} label${n>1?'s':''} fit on one page in a ${L.cols} x ${L.rows} grid${L.k<0.999?`, shrunk to ${Math.round(L.k*100)}% of the chosen size`:''}.`;
    else{note=`${num(L.per)} labels per page (${L.cols} x ${L.rows} grid), ${num(L.pages)} pages.`;if(o.fit)note+=' Even the smallest size does not fit them all on one page.'}
    if(L.k<0.8){warn=true;note+=' Labels are small now, so test that your scanner can read them.'}
    $('#pNote',d).className='pnote'+(warn?' warn':'');$('#pNote',d).textContent=note;
    const px=3.7795,target=L.roll?280:Math.min(360,(d.clientWidth||900)*0.42),s=target/(L.W*px);
    paper.innerHTML=`<div class="paper-wrap" style="width:${L.W*px*s}px;height:${L.H*px*s}px"><div class="paper" style="width:${L.W}mm;height:${L.H}mm;padding:${L.margin}mm;transform:scale(${s})">${grid(L,devs.slice(0,L.per))}</div></div>`;
    $('#pCap',d).textContent=`Page 1 of ${num(L.pages)}`;
    pr.disabled=pdf.disabled=false;pr.innerHTML=`${icon('printer')} Print${L.pages>1?` (${num(L.pages)} pages)`:''}`;
  };
  $$('.seg',d).forEach(sg=>sg.onclick=e=>{const b=e.target.closest('button');if(!b||b.disabled)return;o[sg.dataset.k]=b.dataset.v;paint()});
  $('#pMargin',d).value=o.margin;$('#pGap',d).value=o.gap;$('#pFit',d).checked=!!o.fit;
  $('#pMargin',d).oninput=e=>{o.margin=e.target.value;paint()};
  $('#pGap',d).oninput=e=>{o.gap=e.target.value;paint()};
  $('#pFit',d).onchange=e=>{o.fit=e.target.checked;paint()};
  const remember=()=>{try{localStorage.setItem('bp-print',JSON.stringify({size:o.size,orient:o.orient,margin:o.margin,gap:o.gap,fit:o.fit}))}catch(e){}};
  $('#lbPdf',d).onclick=async()=>{
    const L=pageLayout(n,o);if(!L.per)return;remember();
    const b=$('#lbPdf',d),t=b.innerHTML;b.disabled=true;b.textContent='Creating PDF...';
    try{await apiBlob('/labels/pdf',{ids:devs.map(x=>x.id),size:o.size,layout:{W:L.W,H:L.H,margin:L.margin,gap:L.gap,cols:L.cols,rows:L.rows,k:L.k,roll:!!L.roll}});toast('PDF saved to your downloads folder')}
    catch(err){toast(err.message,'bad')}
    b.disabled=false;b.innerHTML=t;
  };
  $('#lbPrint',d).onclick=()=>{
    const L=pageLayout(n,o);if(!L.per)return;remember();
    let html='';
    for(let i=0;i<n;i+=L.per)html+=`<div class="pp"${L.roll?` style="height:${(L.H-0.3).toFixed(2)}mm;overflow:hidden"`:''}>${grid(L,devs.slice(i,i+L.per))}</div>`;
    const root=$('#printRoot');root.innerHTML=html;
    let st=$('#pageStyle');if(!st){st=document.createElement('style');st.id='pageStyle';document.head.append(st)}
    st.textContent=L.roll?`@page{size:${L.W}mm ${L.H}mm;margin:0}`:`@page{size:${PAPER[o.paper][2]} ${o.orient};margin:${L.margin}mm}`;
    const done=()=>{root.innerHTML='';window.removeEventListener('afterprint',done)};
    window.addEventListener('afterprint',done);
    api('/labels/log',{method:'POST',body:{count:n,first:devs[0].tag,last:devs[n-1].tag}}).catch(()=>{});
    setTimeout(()=>window.print(),80);
  };
  paint();
}

/* ---------- device editor ---------- */
function histHTML(history){
  if(!history.length)return '<li class="dim">No changes recorded yet.</li>';
  return history.map(h=>{
    const listy=h.changes&&h.changes.length&&!['Created','Deleted'].includes(h.action);
    const body=listy?h.changes.map(c=>c.f==='Notes'?'<div class="chg-row"><span class="k">Notes</span><div class="v"><span class="dim">Edited</span></div></div>':`<div class="chg-row"><span class="k">${esc(c.f)}</span><div class="v"><span class="old">${esc(c.o||'empty')}</span><span class="arrow">â†’</span><b>${esc(c.n||'empty')}</b></div></div>`).join(''):`<div class="chg-row"><span class="k">Summary</span><div class="v"><span>${esc(h.details)}</span></div></div>`;
    return `<li><header><div class="act"><span class="chip ${h.action==='Created'?'ok':h.action==='Deleted'?'bad':'info'}">${esc(h.action)}</span><b>${esc(h.action==='Created'?'Created':'Updated')}</b></div><div class="meta"><span>${esc(h.user||'system')}</span><span>â€¢</span><time>${fmtDT(h.ts)}</time></div></header><div class="changes">${body}</div><div class="hist-foot"><span></span>${can('admin')&&h.id?`<button type="button" class="snap-link snap" data-aid="${h.id}">${icon('search',14)} View snapshot</button>`:''}</div></li>`}).join('');
}
function scanDialog(){
  const d=openDialog('dlgScan',`<form class="dlg sm" autocomplete="off"><h2>Scan or enter a tag</h2><p class="sub">Point a USB or Bluetooth barcode scanner at the label, or type the tag and press Enter. The device opens straight away.</p><label class="field" style="margin-top:14px"><span>Tag</span><input class="inp tagin" name="tag" required autofocus placeholder="BLUE-CHB-2026-0001" spellcheck="false"></label><div class="form-err" id="scErr" hidden style="margin-top:12px"></div><div class="dlg-foot"><button type="button" class="btn" data-close>Close</button><button class="btn primary" type="submit">Open device</button></div></form>`);
  const inp=$('[name=tag]',d);setTimeout(()=>inp.focus(),60);
  $('form',d).onsubmit=async e=>{
    e.preventDefault();$('#scErr',d).hidden=true;
    const tag=inp.value.trim();if(!tag)return;
    try{
      const r=await api('/devices?'+qs({q:tag,size:1}));
      const hit=r.rows.find(x=>x.tag.toLowerCase()===tag.toLowerCase())||r.rows[0];
      if(!hit)throw new Error('No device has the tag '+tag+'.');
      d.close();openDevice(hit.id);
    }catch(err){const x=$('#scErr',d);x.hidden=false;x.textContent=err.message;inp.select()}
  };
}

async function openDevice(id,presetCat){
  const isNew=!id,W=can('write');let dev,history=[];
  if(!isNew){const r=await api('/devices/'+id);dev=r.device;history=r.history}
  else dev={category_id:presetCat||(S.categories[0]&&S.categories[0].id),status:'In stock'};
  const v=k=>esc(dev[k]||'');
  const d=openDialog('dlgDevice',`<form class="dlg" id="devForm" autocomplete="off">
    <div class="dlg-head"><div><h2>${isNew?'Add a device':W?'Edit device':'Device details'}</h2><p class="sub">${isNew?'The tag is created automatically unless you type your own.':tagParts(dev.tag)}</p></div><button type="button" class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <div class="dlg-body two">
      <fieldset class="fgrid fs" ${W?'':'disabled'}>
        <label class="field span2"><span>Tag</span><input class="inp tagin" name="tag" value="${v('tag')}" maxlength="60" spellcheck="false" ${isNew?'placeholder="Created automatically"':'required'}><small>${isNew?'Optional. Leave empty and the next tag for the chosen type is created for you.':'You can change the tag. If you do, the label on the device has to be replaced.'}</small></label>
        <label class="field span2"><span>Device type</span><select class="inp" name="category_id" required>${S.categories.map(c=>`<option value="${c.id}" ${c.id==dev.category_id?'selected':''}>${esc(c.name)} (${esc(c.code)})</option>`).join('')}</select>${isNew?'':'<small>Changing the type does not change the tag.</small>'}</label>
        <label class="field"><span>Brand</span><input class="inp" name="brand" value="${v('brand')}" placeholder="Asus"></label>
        <label class="field"><span>Model</span><input class="inp" name="model" value="${v('model')}" placeholder="Vivobook 15"></label>
        <label class="field"><span>Serial number</span><input class="inp" name="serial_number" value="${v('serial_number')}"></label>
        <label class="field"><span>MAC address</span><input class="inp" name="mac_address" value="${v('mac_address')}" placeholder="AA:BB:CC:DD:EE:FF"></label>
        <label class="field"><span>Assigned to</span><input class="inp" name="assigned_to" value="${v('assigned_to')}" placeholder="Name of person or room"><small>Entering a name marks the device as Assigned.</small></label>
        <label class="field"><span>Status</span><select class="inp" name="status">${STATUSES.map(s=>`<option ${s===dev.status?'selected':''}>${s}</option>`).join('')}</select></label>
        <label class="field"><span>Department</span><input class="inp" name="department" value="${v('department')}" list="dlDep"></label>
        <label class="field"><span>Location</span><input class="inp" name="location" value="${v('location')}" list="dlLoc"></label>
        <label class="field"><span>Purchase date</span><input class="inp" type="date" name="purchase_date" value="${v('purchase_date')}"></label>
        <label class="field"><span>Warranty ends</span><input class="inp" type="date" name="warranty_end" value="${v('warranty_end')}"></label>
        <label class="field span2"><span>Notes</span><textarea class="inp" name="notes">${v('notes')}</textarea></label>
        <datalist id="dlDep">${S.departments.map(x=>`<option value="${esc(x)}">`).join('')}</datalist><datalist id="dlLoc">${S.locations.map(x=>`<option value="${esc(x)}">`).join('')}</datalist>
      </fieldset>
      <div class="dside">
        <div><div class="stage" id="sideLabel"></div>${isNew?'<p class="sub" style="margin-top:8px;text-align:center">Label preview</p>':`<div style="text-align:center;margin-top:10px"><button type="button" class="btn sm" id="devPrint">${icon('printer',16)} Print label</button></div>`}</div>
        ${isNew?'':`<div><div class="hist-head"><span>History</span>${can('admin')?'<span class="sub">Compare changes over time</span>':''}</div><ul class="hist">${histHTML(history)}</ul></div>`}
      </div>
    </div>
    <div class="form-err" id="formErr" hidden></div>
    <div class="dlg-foot">${isNew||!can('admin')?'':'<button type="button" class="btn danger" id="devDel">Delete device</button>'}<span class="spacer"></span><button type="button" class="btn" data-close>${W?'Cancel':'Close'}</button>${W?`<button class="btn primary" type="submit">${isNew?'Create device':'Save changes'}</button>`:''}</div>
  </form>`);
  const f=$('#devForm',d),side=$('#sideLabel',d);
  const catName=()=>{const c=S.categories.find(x=>x.id==f.category_id.value);return c?c.name:''};
  let pvT;
  const paintSide=()=>{
    const t=f.tag.value.trim().toUpperCase();
    if(t){side.innerHTML=labelHTML(t,catName(),'md',dev.image||'');return}
    if(!isNew){side.innerHTML=labelHTML(dev.tag,catName(),'md',dev.image||'');return}
    clearTimeout(pvT);
    pvT=setTimeout(async()=>{try{const p=await api('/preview?'+qs({category_id:f.category_id.value,year:S.year,quantity:1}));if(!f.tag.value.trim())side.innerHTML=labelHTML(p.first,p.category,'md',p.image)}catch(e){side.textContent=e.message}},120);
  };
  f.tag.oninput=paintSide;f.category_id.onchange=paintSide;paintSide();
  $$('.snap',d).forEach(b=>b.onclick=()=>openSnapshot(dev.id,+b.dataset.aid,'after'));
  if(!isNew){
    $('#devPrint',d).onclick=()=>openLabels([dev]);
    if($('#devDel',d))$('#devDel',d).onclick=async()=>{
      const ok=await confirmBox({title:'Delete this device?',body:`<b>${esc(dev.tag)}</b> will be removed from the inventory. The tag is retired and will never be issued again. The deletion is recorded in the audit log.`,ok:'Delete device',danger:true});
      if(!ok)return;
      try{await api('/devices/'+id,{method:'DELETE'});d.close();toast('Deleted '+dev.tag);await loadMeta();route()}catch(e){toast(e.message,'bad')}
    };
  }
  f.onsubmit=async e=>{
    e.preventDefault();
    const body=Object.fromEntries(new FormData(f));
    if(isNew)body.year=S.year;
    else{
      const nt=(body.tag||'').trim().toUpperCase();
      if(nt!==dev.tag){
        const ok=await confirmBox({title:'Change this tag?',body:`The tag will change from <b>${esc(dev.tag)}</b> to <b>${esc(nt)}</b>. The label stuck on the device will no longer match, so it has to be replaced.`,ok:'Change tag'});
        if(!ok)return;
      }
    }
    try{
      if(isNew){const r=await api('/devices',{method:'POST',body});toast('Created '+r.tag)}
      else{await api('/devices/'+id,{method:'PUT',body});toast('Changes saved')}
      d.close();await loadMeta();route();
    }catch(err){const x=$('#formErr',d);x.hidden=false;x.textContent=err.message}
  };
}

/* ---------- dashboard ---------- */
function donut(by,total){
  const R=52,C=2*Math.PI*R;let off=0;
  const segs=STATUSES.map(s=>{const v=by[s]||0;if(!v)return '';const len=v/total*C;const o=`<circle r="${R}" cx="70" cy="70" fill="none" stroke="${STATUS_COLOR[s]}" stroke-width="18" stroke-dasharray="${len} ${C-len}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)"/>`;off+=len;return o}).join('');
  return `<svg viewBox="0 0 140 140" class="donut" role="img" aria-label="Devices by status"><circle r="${R}" cx="70" cy="70" fill="none" stroke="var(--mute-soft)" stroke-width="18"/>${segs}<text x="70" y="72" text-anchor="middle" class="d-n">${num(total)}</text><text x="70" y="88" text-anchor="middle" class="d-l">devices</text></svg>`;
}
function goInventory(filters){
  const   I=S.inv;Object.assign(I,{page:1,q:'',category:'',status:'',location:'',batch:'',warranty:''},filters||{});clearSel();
  if(location.hash==='#/inventory')route();else location.hash='#/inventory';
}
async function viewDashboard(el){
  const st=await api('/stats');
  if(!st.total){
    const cat=S.categories[0];
    el.innerHTML=`<div class="empty"><div class="lbwrap">${labelHTML(previewTag(S.settings,cat?cat.code:'CHB',S.year,1),cat?cat.name:'Chromebook','md')}</div><h2>Tag your first batch of devices</h2><p>Choose a device type, enter how many you have, and TAG Master creates a unique tag for each one and saves it in the database.</p>${can('write')?`<a class="btn primary lg" href="#/generate">${icon('tag')} Generate tags</a>`:''}</div>`;return;
  }
  const b=st.by_status||{},max=Math.max(1,...st.by_category.map(c=>c.n));
  const ntypes=st.by_category.length,assigned=b['Assigned']||0,instock=b['In stock']||0,pct=st.total?Math.round(assigned/st.total*100):0;
  const kpi=(n,l,c,f,ic,foot)=>`<button class="kpi" style="--kc:${c}" data-f='${JSON.stringify(f)}'><span class="kpi-top"><span class="kpi-ico">${icon(ic,17)}</span><span class="kpi-label">${l}</span></span><b>${num(n)}</b><span class="kpi-foot"><span>${foot||''}</span><span class="kpi-arrow">${icon('arrow',15)}</span></span></button>`;
  const wf=st.expired?{warranty:'expired'}:{warranty:'soon'};
  const wkpi=(st.expired||st.expiring_soon)?kpi(st.expired||st.expiring_soon,st.expired?'Warranty expired':'Warranty expiring',st.expired?'var(--bad)':'var(--warn)',wf,'shield',st.expired?num(st.expired)+' need attention':'Renewals coming up'):'';
  const first=String((S.user&&(S.user.full_name||S.user.username))||'').trim().split(/\s+/)[0];
  el.innerHTML=`<section class="dash-hero">
    <div class="dash-hero-copy"><span class="eyebrow">${icon('pulse',13)} Live inventory overview</span><h2>${first?'Welcome back, '+esc(first):'Inventory at a glance'}</h2><p>${num(st.total)} devices across ${num(ntypes)} types. ${num(instock)} ready to issue, ${num(assigned)} currently assigned${pct?' ('+pct+'% of the inventory)':''}.</p></div>
    <div class="dash-hero-actions">${can('write')?`<a class="btn primary" href="#/generate">${icon('tag')} Generate tags</a>`:''}<a class="btn" href="#/inventory">${icon('list')} Open inventory</a></div>
  </section>
  <div class="kpis">${kpi(st.total,'Total devices','var(--primary)',{},'box',num(ntypes)+' device type'+(ntypes===1?'':'s'))}${kpi(assigned,'Assigned','var(--ok)',{status:'Assigned'},'users',pct+'% of inventory')}${kpi(instock,'In stock','var(--info)',{status:'In stock'},'tag','Ready to issue')}${kpi(b['In repair']||0,'In repair','var(--warn)',{status:'In repair'},'sliders','Being serviced')}${wkpi}</div>
  <div class="grid2">
    <section class="panel"><div class="panel-lead"><div class="panel-title"><h2>Devices by type</h2><p class="sub">Where your equipment is concentrated.</p></div><span class="panel-badge">${icon('grid')}</span></div><div class="bars">${st.by_category.filter(c=>c.n).map(c=>`<div class="bar-row"><span class="nm">${esc(c.name)}</span><span class="tr"><i style="width:${Math.max(3,c.n/max*100)}%"></i></span><span class="n">${num(c.n)}</span></div>`).join('')}</div></section>
    <section class="panel"><div class="panel-lead"><div class="panel-title"><h2>Status</h2><p class="sub">The current condition of every device.</p></div><span class="panel-badge">${icon('pulse')}</span></div><div class="donut-wrap">${donut(b,st.total)}<div class="legend">${STATUSES.map(s=>`<button data-f='${JSON.stringify({status:s})}'><i style="background:${STATUS_COLOR[s]}"></i><span>${s}</span><b>${num(b[s])}</b></button>`).join('')}</div></div></section>
  </div>
  <section class="panel"><div class="panel-lead"><div class="panel-title"><h2>Latest tags</h2><p class="sub">The most recently created devices.</p></div><a class="btn sm" href="#/inventory">${icon('list',15)} View inventory</a></div><ul class="recent">${st.recent.map(d=>`<li data-id="${d.id}" style="cursor:pointer">${tagParts(d.tag)}<span>${esc(d.category_name)}${d.brand||d.model?' <span class="dim">'+esc((d.brand+' '+d.model).trim())+'</span>':''}</span><span class="dim">${fmtDT(d.created_at)}</span></li>`).join('')}</ul></section>`;
  $$('[data-f]',el).forEach(b=>b.onclick=()=>goInventory(JSON.parse(b.dataset.f)));
  $$('.recent li',el).forEach(li=>li.onclick=()=>openDevice(+li.dataset.id));
}

/* ---------- generate ---------- */
async function viewGenerate(el){
  const st=S.settings;
  if(!S.categories.length){el.innerHTML=`<div class="empty"><h2>Add a device type first</h2><p>Tags are built from a device type such as Chromebook or Router.</p><a class="btn primary" href="#/settings">Open settings</a></div>`;return}
  el.innerHTML=`<div class="gen">
    <form id="genForm" class="panel" autocomplete="off">
      <h2 class="ph">Generate a batch of tags</h2>
      <p class="sub" style="margin-bottom:20px">Each tag is unique, saved to the database as soon as it is created, and never issued twice.</p>
      <div class="fgrid">
        <label class="field span2"><span>Device type</span><select id="gCat" class="inp" required>${S.categories.map(c=>`<option value="${c.id}">${esc(c.name)} (${esc(c.code)})</option>`).join('')}</select></label>
        <label class="field"><span>Quantity</span><input id="gQty" class="inp" type="number" min="1" step="1" value="10" inputmode="numeric" required><small>There is no upper limit.</small></label>
        ${st.include_year?`<label class="field"><span>Year</span><input id="gYear" class="inp" type="number" min="1990" max="2999" value="${S.year}"></label>`:'<div></div>'}
        <div class="chips span2">${[1,5,10,25,50,100,250,500].map(n=>`<button type="button" class="chip-btn" data-n="${n}">${n}</button>`).join('')}</div>
        <label class="field"><span>Location</span><input id="gLoc" class="inp" list="dlLoc" placeholder="Computer Lab 1"></label>
        <label class="field"><span>Department</span><input id="gDep" class="inp" list="dlDep" placeholder="Grade 7"></label>
        <label class="field"><span>Brand</span><input id="gBrand" class="inp" placeholder="Asus"></label>
        <label class="field"><span>Model</span><input id="gModel" class="inp" placeholder="Vivobook 15"></label>
        <label class="field span2"><span>Device photo (optional)</span><input id="gImg" class="inp" type="file" accept="image/*"><small>Attach a picture of the device. This is optional and can be added later.</small></label>
        <datalist id="dlDep">${S.departments.map(x=>`<option value="${esc(x)}">`).join('')}</datalist><datalist id="dlLoc">${S.locations.map(x=>`<option value="${esc(x)}">`).join('')}</datalist>
      </div>
      <p class="sub" style="margin-top:10px">Location, department, brand and model are optional and apply to every device in this batch. Serial numbers can be added later in the inventory.</p>
      <div class="form-actions"><button class="btn primary lg" id="gGo" type="submit">${icon('tag')} Generate tags</button><span id="gErr" style="color:var(--bad);font-weight:600"></span></div>
    </form>
    <aside class="panel pv"><h2>Label preview</h2><div class="stage" id="pvLabel"></div><dl><dt>First tag</dt><dd id="pvFirst" class="tagcode"></dd><dt>Last tag</dt><dd id="pvLast" class="tagcode"></dd><dt>Format</dt><dd class="dim" id="pvFmt"></dd></dl></aside>
  </div><div id="genResult"></div>`;
  const cat=$('#gCat'),qty=$('#gQty'),yr=$('#gYear');let timer;
  const refresh=()=>{clearTimeout(timer);timer=setTimeout(async()=>{
    const q=Math.max(1,parseInt(qty.value)||1);
    try{const p=await api('/preview?'+qs({category_id:cat.value,year:yr?yr.value:'',quantity:q}));
      if(!$('#pvLabel'))return;
      $('#pvLabel').innerHTML=labelHTML(p.first,p.category,'md',p.image);$('#pvFirst').textContent=p.first;$('#pvLast').textContent=p.last;
      const c=S.categories.find(x=>x.id==cat.value);$('#pvFmt').textContent=previewTag(S.settings,'CODE',yr?yr.value:S.year,1).replace('CODE',c?c.code:'CODE');
    }catch(e){if($('#gErr'))$('#gErr').textContent=e.message}
  },180)};
  [cat,qty,yr].forEach(x=>x&&x.addEventListener('input',refresh));
  $$('.chip-btn').forEach(b=>b.onclick=()=>{qty.value=b.dataset.n;refresh()});
  refresh();
  const imgInput=$('#gImg');let imgData=null;
  if(imgInput){
    imgInput.onchange=async e=>{
      const f=e.target.files[0];if(!f)return;
      if(f.size>5*1024*1024){toast('Image is too large (max 5 MB).','bad');e.target.value='';return}
      const r=new FileReader();
      r.onload=()=>{imgData=String(r.result);toast('Device photo attached');};
      r.readAsDataURL(f);
    };
  }
  $('#genForm').onsubmit=async e=>{
    e.preventDefault();$('#gErr').textContent='';
    const n=parseInt(qty.value);if(!(n>=1)){$('#gErr').textContent='Enter a quantity of 1 or more.';return}
    if(n>5000){const ok=await confirmBox({title:'Generate '+num(n)+' tags?',body:'That is a big batch. All of them will be saved to the database right away.',ok:'Generate '+num(n)});if(!ok)return}
    const btn=$('#gGo');btn.disabled=true;btn.lastChild.textContent=' Generating...';
    try{
      const r=await api('/generate',{method:'POST',body:{category_id:cat.value,quantity:n,year:yr?yr.value:'',location:$('#gLoc').value,department:$('#gDep').value,brand:$('#gBrand').value,model:$('#gModel').value,image:imgData}});
      toast(num(r.count)+' tags generated');await loadMeta();showResult(r);refresh();
    }catch(err){$('#gErr').textContent=err.message}
    btn.disabled=false;btn.lastChild.textContent=' Generate tags';
  };
}
function showResult(r){
  const box=$('#genResult');
  box.innerHTML=`<section class="panel result"><div class="res-head"><div><h2>${num(r.count)} ${esc(r.category)} tag${r.count>1?'s':''} generated</h2><p class="sub" style="margin-top:4px">${tagParts(r.first)} ${r.count>1?'to '+tagParts(r.last):''}</p></div>
    <div class="res-actions"><button class="btn primary" id="rPrint">${icon('printer')} Print labels</button><button class="btn" id="rCopy">${icon('copy')} Copy tags</button><button class="btn" id="rCsv">${icon('download')} Export CSV</button><button class="btn" id="rOpen">${icon('list')} Open in inventory</button></div></div>
    <div class="table-wrap"><table class="tbl compact"><thead><tr><th>Tag</th><th>Device type</th><th>Location</th><th>Status</th></tr></thead><tbody>${r.tags.map(t=>`<tr style="cursor:default"><td>${tagParts(t)}</td><td>${esc(r.category)}</td><td>${esc(r.location)||'<span class="dim">-</span>'}</td><td>${chip('In stock')}</td></tr>`).join('')}</tbody></table></div>
    ${r.count>r.tags.length?`<p class="sub pad">Showing the first ${r.tags.length}. All ${num(r.count)} are saved in the inventory.</p>`:''}</section>`;
  const fetchAll=()=>api('/devices?'+qs({batch:r.batch_id,size:0,sort:'tag',dir:'asc'}));
  $('#rPrint').onclick=async()=>{try{openLabels((await fetchAll()).rows)}catch(e){toast(e.message,'bad')}};
  $('#rCopy').onclick=async()=>{const rows=(await fetchAll()).rows;try{await navigator.clipboard.writeText(rows.map(x=>x.tag).join('\n'));toast(num(rows.length)+' tags copied')}catch(e){toast('Copy was blocked by the browser. Use Export CSV instead.','bad')}};
  $('#rCsv').onclick=()=>dl('/api/export.csv?'+qs({batch:r.batch_id}));
  $('#rOpen').onclick=()=>goInventory({batch:r.batch_id});
  box.scrollIntoView({behavior:'smooth',block:'start'});
}

/* ---------- inventory ---------- */
const clearSel=()=>{S.inv.sel=new Set();S.inv.allMatching=false};
const invParams=()=>{const I=S.inv;return{q:I.q,category:I.category,status:I.status,location:I.location,batch:I.batch,warranty:I.warranty}};
const selCount=()=>S.inv.allMatching?S.inv.total:S.inv.sel.size;
const selPayload=()=>S.inv.allMatching?{all_matching:true,filters:invParams()}:{ids:[...S.inv.sel]};
async function selRows(){return S.inv.allMatching?(await api('/devices?'+qs({...invParams(),size:0,sort:'tag',dir:'asc'}))).rows:(await api('/devices/by-ids',{method:'POST',body:selPayload()})).rows}

function clearInvFilters(){const I=S.inv;Object.assign(I,{page:1,q:'',category:'',status:'',location:'',batch:'',warranty:''});clearSel();route()}
function paintFilters(){
  const I=S.inv,box=$('#fActive');if(!box)return;
  const p=[];
  if(I.q)p.push(['q','Search: '+I.q]);
  if(I.category){const c=S.categories.find(x=>x.id==I.category);p.push(['category','Type: '+(c?c.name:I.category)])}
  if(I.status)p.push(['status','Status: '+I.status]);
  if(I.location)p.push(['location','Location: '+I.location]);
  if(I.warranty)p.push(['warranty','Warranty: '+(I.warranty==='expired'?'Expired':'Expiring soon')]);
  if(I.batch)p.push(['batch','Batch: '+I.batch]);
  box.innerHTML=p.length?`<div class="fbar"><span class="sub" style="font-weight:700">Filtered by</span>${p.map(([k,l])=>`<span class="pill">${esc(l)}<button type="button" data-x="${k}" aria-label="Remove the ${esc(l)} filter">${icon('x',13)}</button></span>`).join('')}<button type="button" class="linkbtn" data-x="__all">Clear all</button></div>`:'';
  $$('[data-x]',box).forEach(b=>b.onclick=()=>{const k=b.dataset.x;if(k==='__all')clearInvFilters();else{I[k]='';I.page=1;clearSel();route()}});
}
async function viewInventory(el){
  const I=S.inv,W=can('write');
  let st={total:0,by_status:{},by_category:[]};
  try{st=await api('/stats')}catch(e){}
  const byS=st.by_status||{},total=st.total||0,ntypes=(st.by_category||[]).length;
  const sbtn=(v,label,n,sc)=>`<button type="button" class="chip-btn${I.status===v?' on':''}" data-st="${esc(v)}"${sc?` style="--sc:${sc}"`:''}>${sc?'<i></i>':''}${esc(label)} <b>${num(n)}</b></button>`;
  el.innerHTML=`<div class="toolbar">
    <div class="searchbox">${icon('search')}<input id="iq" class="inp" type="search" placeholder="Search tag, serial, brand, person or location" aria-label="Search the inventory" value="${esc(I.q)}"></div>
    <span class="spacer"></span>
    ${W?`<button class="btn" id="bImport">${icon('upload')} Import</button>`:''}
    ${W?`<div class="usermenu"><button class="btn" id="bExport">${icon('download')} Export</button><div class="pop" id="expPop" hidden><button id="expX">Excel workbook (whole inventory)</button><button id="expC">CSV (current filters)</button></div></div>`:''}
    ${W?`<button class="btn primary" id="bAdd">${icon('plus')} Add device</button>`:''}</div>
  <p class="sub inv-sub">${num(total)} device${total===1?'':'s'} across ${num(ntypes)} device type${ntypes===1?'':'s'}. Click a status to filter, and use the search box to find a tag, serial number, person or location.</p>
  <div class="inv-filters">
    <div class="inv-chips" role="group" aria-label="Filter by status">${sbtn('','All',total,'')}${STATUSES.map(s=>sbtn(s,s,byS[s]||0,STATUS_COLOR[s])).join('')}</div>
    <div class="inv-sel">
      <select id="icat" class="inp" aria-label="Device type"><option value="">All types</option>${S.categories.map(c=>`<option value="${c.id}" ${I.category==c.id?'selected':''}>${esc(c.name)}</option>`).join('')}</select>
      <select id="iloc" class="inp" aria-label="Location"><option value="">All locations</option>${S.locations.map(l=>`<option ${I.location===l?'selected':''}>${esc(l)}</option>`).join('')}</select>
      <select id="iwarranty" class="inp" aria-label="Warranty"><option value="">Any warranty</option><option value="soon" ${I.warranty==='soon'?'selected':''}>Warranty expiring soon</option><option value="expired" ${I.warranty==='expired'?'selected':''}>Warranty expired</option></select>
    </div>
  </div>
  <div id="fActive"></div>
  ${I.batch?`<div class="notice">Showing one generated batch. <button class="linkbtn" id="clrBatch">Show all devices</button></div>`:''}
  ${W?'<p class="sub hint">Tip: double-click a cell to edit it in place. Click a tag, or the pencil, to open the full editor.</p>':''}
  <div id="bulk"></div>
  <div class="panel flush"><div class="table-wrap"><table class="tbl" style="min-width:1040px"><thead><tr><th class="c-chk"><input type="checkbox" id="chkAll" aria-label="Select all on this page"></th>
    <th data-sort="tag">Tag</th><th data-sort="category">Type</th><th data-sort="brand">Brand</th><th data-sort="model">Model</th><th data-sort="serial">Serial number</th><th data-sort="assigned">Assigned to</th><th data-sort="location">Location</th><th data-sort="status">Status</th><th class="c-act"></th></tr></thead><tbody id="tb"></tbody></table></div><div class="pager" id="pager"></div></div>`;
  let t;const reload=()=>{I.page=1;clearSel();loadRows()};
  $('#iq').oninput=e=>{clearTimeout(t);t=setTimeout(()=>{I.q=e.target.value.trim();reload()},220)};
  $('#icat').onchange=e=>{I.category=e.target.value;reload()};
  $('#iloc').onchange=e=>{I.location=e.target.value;reload()};
  $('#iwarranty').onchange=e=>{I.warranty=e.target.value;reload()};
  $$('[data-st]',el).forEach(b=>b.onclick=()=>{I.status=b.dataset.st===I.status?'':b.dataset.st;
    $$('[data-st]',el).forEach(x=>x.classList.toggle('on',x.dataset.st===I.status));reload()});
  if($('#bAdd'))$('#bAdd').onclick=()=>openDevice(null,I.category||null);
  if($('#bExport'))$('#bExport').onclick=e=>{e.stopPropagation();$('#expPop').hidden=!$('#expPop').hidden};
  if($('#expX'))$('#expX').onclick=()=>{toast('Preparing the Excel workbook...');dl('/api/export.xlsx')};
  if($('#expC'))$('#expC').onclick=()=>dl('/api/export.csv?'+qs(invParams()));
  if($('#bImport'))$('#bImport').onclick=openImport;
  if($('#clrBatch'))$('#clrBatch').onclick=()=>{I.batch='';I.page=1;clearSel();route()};
  $$('th[data-sort]').forEach(th=>th.onclick=()=>{const k=th.dataset.sort;if(I.sort===k)I.dir=I.dir==='asc'?'desc':'asc';else{I.sort=k;I.dir='asc'}I.page=1;loadRows()});
  $('#chkAll').onchange=e=>{I.rows.forEach(r=>e.target.checked?I.sel.add(r.id):I.sel.delete(r.id));if(!e.target.checked)I.allMatching=false;paintRows();paintBulk()};
  $('#tb').onclick=e=>{
    if(e.target.closest('#clrF')){clearInvFilters();return}
    if(e.target.closest('#empAdd')){openDevice(null);return}
    const tr=e.target.closest('tr[data-id]');if(!tr)return;const id=+tr.dataset.id;
    if(e.target.closest('.rowchk')){
      if(I.allMatching){I.allMatching=false;I.sel=new Set(I.rows.map(r=>r.id))}
      e.target.checked?I.sel.add(id):I.sel.delete(id);paintRows();paintBulk();return}
    if(e.target.closest('.lbl')){const r=I.rows.find(x=>x.id===id);if(r)openLabels([r]);else{toast('That device could not be found.','bad');return} return}
    if(e.target.closest('.edt')||e.target.closest('.taglink'))openDevice(id);
  };
  $('#tb').ondblclick=e=>{const td=e.target.closest('td.ed');if(td&&can('write'))startEdit(td)};
  await loadRows();
}
function startEdit(td){
  const I=S.inv;if(td.querySelector('.cell-in'))return;
  const id=+td.closest('tr').dataset.id,f=td.dataset.f,d=I.rows.find(r=>r.id===id),orig=d[f]==null?'':d[f];
  let el;
  if(f==='status'){el=document.createElement('select');el.innerHTML=STATUSES.map(s=>`<option ${s===orig?'selected':''}>${s}</option>`).join('')}
  else if(f==='category_id'){el=document.createElement('select');el.innerHTML=S.categories.map(c=>`<option value="${c.id}" ${c.id==orig?'selected':''}>${esc(c.name)}</option>`).join('')}
  else{el=document.createElement('input');el.type='text';el.value=orig;el.maxLength=f==='serial_number'||f==='brand'||f==='model'||f==='location'?80:120}
  el.className='cell-in inp';td.innerHTML='';td.append(el);el.focus();if(el.select&&el.tagName==='INPUT')el.select();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;
    const val=el.value.trim();
    if(!save||String(val)===String(orig)){paintRows();return}
    try{await api('/devices/'+id,{method:'PUT',body:{[f]:val}});toast('Saved');await loadMeta()}catch(err){toast(err.message,'bad')}
    loadRows();
  };
  el.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();finish(true)}else if(e.key==='Escape'){e.preventDefault();finish(false)}};
  el.onblur=()=>finish(true);
  if(el.tagName==='SELECT')el.onchange=()=>finish(true);
}
async function loadRows(){
  const I=S.inv;
  const tb=$('#tb');if(tb)tb.style.opacity='.45';
  let r;
  try{
    r=await api('/devices?'+qs({...invParams(),page:I.page,size:I.size,sort:I.sort,dir:I.dir}));
  }finally{if(tb)tb.style.opacity=''}
  const pages=Math.max(1,Math.ceil(r.total/I.size));
  if(I.page>pages){I.page=pages;return loadRows()}
  I.rows=r.rows;I.total=r.total;
  paintRows();paintBulk();paintFilters();
  $$('th[data-sort]').forEach(th=>{const a=th.querySelector('.ar');if(a)a.remove();if(th.dataset.sort===I.sort)th.insertAdjacentHTML('beforeend',`<span class="ar">${I.dir==='asc'?'\u25B2':'\u25BC'}</span>`)});
  const from=I.total?(I.page-1)*I.size+1:0,to=Math.min(I.total,I.page*I.size);
  $('#pager').innerHTML=`<span class="sub">${I.total?`Showing ${num(from)}\u2013${num(to)} of ${num(I.total)}`:'No devices'}</span><span class="spacer"></span>
    <label class="sub">Rows <select class="inp" id="psz">${[25,50,100,250].map(n=>`<option ${n===I.size?'selected':''}>${n}</option>`).join('')}</select></label>
    <button class="btn sm" id="pPrev" ${I.page<=1?'disabled':''}>Previous</button><span>Page ${I.page} of ${pages}</span><button class="btn sm" id="pNext" ${I.page>=pages?'disabled':''}>Next</button>`;
  $('#psz').onchange=e=>{I.size=+e.target.value;I.page=1;loadRows()};
  $('#pPrev').onclick=()=>{I.page--;loadRows()};$('#pNext').onclick=()=>{I.page++;loadRows()};
}
function paintRows(){
  const I=S.inv,W=can('write');
  const dash='<span class="dim">-</span>';
  const ed=(d,f,val)=>`<td${W?` class="ed" data-f="${f}"`:''}>${val||dash}</td>`;
  $('#tb').innerHTML=I.rows.length?I.rows.map(d=>{const sel=I.allMatching||I.sel.has(d.id);
    return `<tr data-id="${d.id}" class="${sel?'sel':''}"><td class="c-chk"><input type="checkbox" class="rowchk" ${sel?'checked':''} aria-label="Select ${esc(d.tag)}"></td><td><button class="taglink" title="Open the full editor">${tagParts(d.tag)}</button></td>${ed(d,'category_id',esc(d.category_name))}${ed(d,'brand',esc(d.brand))}${ed(d,'model',esc(d.model))}${ed(d,'serial_number',d.serial_number?`<span class="mono-sm">${esc(d.serial_number)}</span>`:'')}${ed(d,'assigned_to',esc(d.assigned_to))}${ed(d,'location',esc(d.location))}${ed(d,'status',chip(d.status))}<td class="c-act"><button class="icon-btn edt" title="${W?'Open the full editor':'View details'}" aria-label="Open ${esc(d.tag)}">${icon('edit')}</button><button class="icon-btn lbl" title="Print label" aria-label="Print label for ${esc(d.tag)}">${icon('printer')}</button></td></tr>`}).join('')
    :`<tr class="empty-row"><td colspan="10">${(I.q||I.category||I.status||I.location||I.batch||I.warranty)?`<div class="emp"><b>No devices match your filters</b><span class="sub">Try a different search, or clear the filters to see the whole inventory again.</span><button type="button" class="linkbtn" id="clrF">Clear all filters</button></div>`:`<div class="emp"><b>No devices yet</b><span class="sub">Create your first devices with Generate tags, by adding one at a time, or by importing a CSV file.</span>${can('write')?`<div class="emp-act"><a class="btn primary" href="#/generate">${icon('tag')} Generate tags</a><button type="button" class="btn" id="empAdd">${icon('plus')} Add device</button></div>`:''}</div>`}</td></tr>`;
  const all=I.rows.length&&I.rows.every(r=>I.allMatching||I.sel.has(r.id));
  $('#chkAll').checked=!!all;
}
function paintBulk(){
  const I=S.inv,n=selCount(),box=$('#bulk');
  if(!n){box.innerHTML='';return}
  const pageAll=I.rows.length&&I.rows.every(r=>I.sel.has(r.id));
  box.innerHTML=`<div class="bulk"><b>${num(n)} selected</b>
    ${!I.allMatching&&pageAll&&I.total>I.rows.length?`<button class="linkbtn" id="selAll">Select all ${num(I.total)} matching devices</button>`:''}
    <span class="spacer"></span>
    <button class="btn sm" id="bkPrint">${icon('printer',16)} Print or save labels</button>
    <button class="btn sm" id="bkEdit">${icon('edit',16)} Edit fields</button>
    <select id="bkStatus" aria-label="Change status"><option value="">Change status...</option>${STATUSES.filter(s=>s!=='Assigned').map(s=>`<option>${s}</option>`).join('')}</select>
    <button class="btn sm" id="bkDel">${icon('trash',16)} Delete</button>
    <button class="linkbtn" id="bkClear">Clear</button></div>`;
  if(!can('write')){$('#bkStatus').remove();$('#bkEdit').remove()}
  if(!can('admin'))$('#bkDel').remove();
  if($('#selAll'))$('#selAll').onclick=()=>{I.allMatching=true;paintRows();paintBulk()};
  $('#bkClear').onclick=()=>{clearSel();paintRows();paintBulk()};
  $('#bkPrint').onclick=async()=>openLabels(await selRows());
  if($('#bkEdit'))$('#bkEdit').onclick=()=>bulkEditDialog();
  if($('#bkStatus'))$('#bkStatus').onchange=async e=>{
    const val=e.target.value;if(!val)return;
    try{const r=await api('/devices/bulk',{method:'POST',body:{...selPayload(),action:'status',value:val}});toast(num(r.changed)+' of '+num(r.count)+' devices set to '+val);clearSel();loadRows()}catch(err){toast(err.message,'bad')}
  };
  if($('#bkDel'))$('#bkDel').onclick=async()=>{
    const ok=await confirmBox({title:'Delete '+num(n)+' device'+(n>1?'s':'')+'?',body:'They will be removed from the inventory. Their tags are retired and will never be issued again. The deletion is recorded in the audit log.',ok:'Delete '+num(n),danger:true});
    if(!ok)return;
    try{const r=await api('/devices/bulk',{method:'POST',body:{...selPayload(),action:'delete'}});toast(num(r.count)+' devices deleted');clearSel();await loadMeta();loadRows()}catch(err){toast(err.message,'bad')}
  };
}
function bulkEditDialog(){
  const n=selCount();
  const d=openDialog('dlgBulk',`<form class="dlg sm" style="width:min(540px,92vw)" autocomplete="off"><h2>Edit ${num(n)} device${n>1?'s':''}</h2><p>Fill in only what you want to change. Empty fields stay as they are.</p>
    <div class="fgrid" style="margin-top:14px">
      <label class="field span2"><span>Device type</span><select class="inp" name="category_id"><option value="">No change</option>${S.categories.map(c=>`<option value="${c.id}">${esc(c.name)} (${esc(c.code)})</option>`).join('')}</select></label>
      <label class="field"><span>Brand</span><input class="inp" name="brand"></label>
      <label class="field"><span>Model</span><input class="inp" name="model"></label>
      <label class="field"><span>Department</span><input class="inp" name="department" list="dlDepB"></label>
      <label class="field"><span>Location</span><input class="inp" name="location" list="dlLocB"></label>
      <label class="field"><span>Status</span><select class="inp" name="status"><option value="">No change</option>${STATUSES.filter(s=>s!=='Assigned').map(s=>`<option>${s}</option>`).join('')}</select></label>
      <label class="field"><span>Purchase date</span><input class="inp" type="date" name="purchase_date"></label>
      <label class="field"><span>Warranty ends</span><input class="inp" type="date" name="warranty_end"></label>
      <datalist id="dlDepB">${S.departments.map(x=>`<option value="${esc(x)}">`).join('')}</datalist><datalist id="dlLocB">${S.locations.map(x=>`<option value="${esc(x)}">`).join('')}</datalist>
    </div><div class="form-err" id="bErr" hidden style="margin:14px 0 0"></div>
    <div class="dlg-foot"><button type="button" class="btn" data-close>Cancel</button><button class="btn primary" type="submit">Apply to ${num(n)}</button></div></form>`);
  $('form',d).onsubmit=async e=>{
    e.preventDefault();
    const fields=Object.fromEntries([...new FormData(e.target)].filter(([k,v])=>String(v).trim()!==''));
    const x=$('#bErr',d);
    if(!Object.keys(fields).length){x.hidden=false;x.textContent='Fill in at least one field to change.';return}
    try{const r=await api('/devices/bulk',{method:'POST',body:{...selPayload(),action:'edit',fields}});d.close();toast(num(r.changed)+' of '+num(r.count)+' devices updated');clearSel();await loadMeta();loadRows()}
    catch(err){x.hidden=false;x.textContent=err.message}
  };
}

/* ---------- import ---------- */
function parseCSV(text){
  const rows=[];let row=[],cur='',q=false;text=text.replace(/^\uFEFF/,'');
  for(let i=0;i<text.length;i++){const c=text[i];
    if(q){if(c==='"'){if(text[i+1]==='"'){cur+='"';i++}else q=false}else cur+=c}
    else if(c==='"')q=true;
    else if(c===','){row.push(cur);cur=''}
    else if(c==='\n'||c==='\r'){if(c==='\r'&&text[i+1]==='\n')i++;row.push(cur);cur='';rows.push(row);row=[]}
    else cur+=c}
  if(cur!==''||row.length){row.push(cur);rows.push(row)}
  return rows.filter(r=>r.some(x=>x.trim()!==''));
}
const IMPORT_COLS={'tag':'tag','device type':'category','type':'category','category':'category','brand':'brand','model':'model','serial number':'serial_number','serial':'serial_number','mac address':'mac_address','mac':'mac_address','status':'status','assigned to':'assigned_to','department':'department','location':'location','purchase date':'purchase_date','warranty ends':'warranty_end','warranty end':'warranty_end','warranty':'warranty_end','notes':'notes'};
function openImport(){
  const d=openDialog('dlgImport',`<div class="dlg sm" style="width:min(560px,92vw)"><h2>Import devices from CSV</h2><p>Use a file saved from Excel as CSV. The first row must hold column names. Only <b>Device type</b> is required. Leave Tag empty to generate new tags, or fill it in to keep tags you already use.</p>
    <label class="check" style="margin-top:10px"><input type="checkbox" id="impUpdate"> Update existing devices when the tag already exists (instead of skipping)</label>
    <div class="imp-box" style="margin-top:16px"><input type="file" id="impFile" accept=".csv,text/csv"><p id="impInfo" class="sub" style="margin-top:10px"></p></div><div id="impErr"></div>
    <div class="dlg-foot" style="justify-content:space-between"><button class="btn sm" id="impTpl">${icon('download',16)} Download template</button><span><button class="btn" data-close>Close</button> <button class="btn" id="impCheck" disabled>Check file</button> <button class="btn primary" id="impGo" disabled>Import</button></span></div></div>`);
  let rows=[];
  $('#impTpl',d).onclick=()=>{const t='Tag,Device type,Brand,Model,Serial number,MAC address,Status,Assigned to,Department,Location,Purchase date,Notes\r\n,Chromebook,Acer,Spin 311,NXHBVAA001,,In stock,,Grade 7,Computer Lab 1,2025-09-01,\r\n';const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([t],{type:'text/csv'}));a.download='BluePeak-import-template.csv';a.click()};
  const mode=()=>$('#impUpdate',d).checked?'update':'create';
  $('#impFile',d).onchange=async e=>{
    const f=e.target.files[0];if(!f)return;
    const data=parseCSV(await f.text());if(data.length<2){$('#impInfo',d).textContent='That file has no data rows.';return}
    const keys=data[0].map(h=>IMPORT_COLS[h.trim().toLowerCase()]||null);
    if(!keys.includes('category')){$('#impInfo',d).textContent='Could not find a "Device type" column in the first row.';return}
    rows=data.slice(1).map(r=>{const o={};keys.forEach((k,i)=>{if(k)o[k]=(r[i]||'').trim()});return o});
    $('#impInfo',d).textContent=`${num(rows.length)} rows ready from ${f.name}. Check the file before importing.`;
    $('#impGo',d).disabled=false;$('#impCheck',d).disabled=false;$('#impErr',d).innerHTML='';
  };
  $('#impCheck',d).onclick=async()=>{
    try{
      const r=await api('/import',{method:'POST',body:{rows,mode:mode(),dry_run:true}});
      let msg=`Will add ${num(r.would_import)} device${r.would_import===1?'':'s'}`;
      if(r.would_update)msg+=`, update ${num(r.would_update)}`;
      $('#impInfo',d).textContent=msg+'.';
      if(r.skipped_count)$('#impErr',d).innerHTML=`<div class="errlist">${r.skipped.map(esc).join('<br>')}${r.skipped_count>r.skipped.length?'<br>and more...':''}</div>`;
      else $('#impErr',d).innerHTML='<p class="sub" style="color:var(--ok)">No problem rows found.</p>';
    }catch(err){$('#impErr',d).innerHTML=`<div class="errlist">${esc(err.message)}</div>`}
  };
  $('#impGo',d).onclick=async()=>{
    $('#impGo',d).disabled=true;
    try{
      const r=await api('/import',{method:'POST',body:{rows,mode:mode()}});await loadMeta();
      toast(num(r.imported)+' added'+(r.updated?', '+r.updated+' updated':'')+(r.skipped_count?', '+r.skipped_count+' skipped':''));
      if(r.skipped_count){$('#impErr',d).innerHTML=`<div class="errlist">${r.skipped.map(esc).join('<br>')}${r.skipped_count>r.skipped.length?'<br>and more...':''}</div>`;route()}
      else{d.close();route()}
    }catch(err){toast(err.message,'bad');$('#impGo',d).disabled=false}
  };
}

/* ---------- activity log ---------- */
const ACTION_CLASS=a=>({Created:'ok',Generated:'ok',Imported:'ok',Deleted:'bad','Failed sign-in':'bad',Locked:'bad','Blocked sign-in':'bad',Restored:'warn',Printed:'warn',PDF:'warn',Export:'warn',Backup:'warn','Sign in':'mute','Sign out':'mute','Password changed':'mute'})[a]||'info';
const ACTION_GROUPS=[['','Everything',''],['edits','Edits','Updated,Assigned,Returned,Status changed,Tag changed'],['adds','Added','Created,Generated,Imported'],['deletes','Deleted','Deleted'],['restores','Restored','Restored'],['signins','Sign-ins','Sign in,Sign out,Failed sign-in,Locked,Blocked sign-in,Password changed'],['output','Print and export','Printed,PDF,Export,Backup'],['admin','Settings and users','Settings,Users']];
const RANGES=[['','Any time'],['today','Today'],['24h','Last 24 hours'],['7d','Last 7 days'],['30d','Last 30 days'],['month','This month'],['year','This year']];
const MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];
const pad2=n=>String(n).padStart(2,'0');
const toLocalInput=d=>`${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const localToUtc=(v,end)=>{if(!v)return '';const d=new Date(v);if(isNaN(d))return '';if(end)d.setSeconds(59);return d.toISOString().slice(0,19).replace('T',' ')};
function rangeStart(key){const n=new Date();switch(key){case 'today':return new Date(n.getFullYear(),n.getMonth(),n.getDate());case '24h':return new Date(n-864e5);case '7d':return new Date(n-7*864e5);case '30d':return new Date(n-30*864e5);case 'month':return new Date(n.getFullYear(),n.getMonth(),1);case 'year':return new Date(n.getFullYear(),0,1)}return null}

async function viewAudit(el){
  const A={q:'',tag:'',user:'',role:'',action:'',group:'',entity:'',category:'',year:'',month:'',from:'',to:'',range:'',tod_from:'',tod_to:'',has_changes:false,all:false,order:'desc',limit:100,offset:0};
  let filled=false,rowsById={};
  const query=(withLimit)=>qs({q:A.q,tag:A.tag,user:A.user,role:A.role,action:A.action,actions:A.group?ACTION_GROUPS.find(g=>g[0]===A.group)[2]:'',entity:A.entity,category:A.category,year:A.year,month:A.month,from_ts:localToUtc(A.from),to_ts:localToUtc(A.to,true),tod_from:A.tod_from,tod_to:A.tod_to,has_changes:A.has_changes?1:'',all:A.all?1:'',order:A.order,limit:withLimit?A.limit:'',offset:withLimit?A.offset:'',facets:(withLimit&&!filled)?1:0,tz:-new Date().getTimezoneOffset()});
  const opts=(list,label)=>`<option value="">${label}</option>`+list.map(x=>`<option>${esc(x)}</option>`).join('');
  el.innerHTML=`<div class="panel filters">
    <div class="frow"><div class="searchbox grow">${icon('search')}<input id="aq" class="inp" type="search" placeholder="Search a tag, person, action or what changed"></div>
      <label class="field"><span>Tag starts with</span><input id="atag" class="inp tagin" placeholder="GNET-CHB" style="width:170px"></label></div>
    <div class="chiprow"><span class="lab">When</span><span id="aRange">${RANGES.map(([k,l])=>`<button type="button" class="chip-btn" data-r="${k}">${l}</button>`).join(' ')}</span></div>
    <div class="fgrid4">
      <label class="field"><span>From (date and time)</span><input id="af" type="datetime-local" class="inp"></label>
      <label class="field"><span>To (date and time)</span><input id="at" type="datetime-local" class="inp"></label>
      <label class="field"><span>Year</span><select id="ay" class="inp"><option value="">Any year</option></select></label>
      <label class="field"><span>Month</span><select id="am" class="inp"><option value="">Any month</option>${MONTHS.map((m,i)=>`<option value="${i+1}">${m}</option>`).join('')}</select></label>
      <label class="field"><span>User</span><select id="au" class="inp"><option value="">Everyone</option></select></label>
      <label class="field"><span>Role</span><select id="ar" class="inp"><option value="">Any role</option>${Object.entries(ROLE_INFO).map(([k,v])=>`<option value="${k}">${v[0]}</option>`).join('')}</select></label>
      <label class="field"><span>Device type</span><select id="ac" class="inp"><option value="">All device types</option></select></label>
      <label class="field"><span>Area</span><select id="ae" class="inp"><option value="">Everything</option><option value="device">Devices</option><option value="user">Users</option><option value="session">Sign-ins</option><option value="settings">Settings</option><option value="category">Device types</option><option value="labels">Labels</option><option value="system">System</option></select></label>
      <label class="field"><span>Exact action</span><select id="aa" class="inp"><option value="">Any action</option></select></label>
      <label class="field"><span>Time of day from</span><input id="atf" type="time" class="inp"></label>
      <label class="field"><span>Time of day to</span><input id="att" type="time" class="inp"></label>
      <label class="field"><span>Order</span><select id="ao" class="inp"><option value="desc">Newest first</option><option value="asc">Oldest first</option></select></label>
    </div>
    <div class="chiprow"><span class="lab">Kind of action</span><span id="aGroup">${ACTION_GROUPS.map(([k,l])=>`<button type="button" class="chip-btn" data-g="${k}">${l}</button>`).join(' ')}</span></div>
    <div class="frow"><label class="check"><input type="checkbox" id="ach"> Only entries that show before and after values</label><label class="check"><input type="checkbox" id="aall"> List every device row instead of grouping bulk actions</label><span class="spacer"></span><button class="btn sm" id="aReset">Reset filters</button><button class="btn" id="aCsv">${icon('download')} Export CSV</button></div>
  </div>
  <p class="sub hint">Every action is recorded with who did it, when and what changed. Entries cannot be edited or deleted from the app. Click an entry for the before and after values, and to preview or restore an old version of a record.</p>
  <div class="panel flush"><ul class="audit" id="al"></ul><div class="pager" id="apg"></div></div>`;
  const paintChips=()=>{$$('#aRange button').forEach(b=>b.classList.toggle('on',b.dataset.r===A.range));$$('#aGroup button').forEach(b=>b.classList.toggle('on',b.dataset.g===A.group))};
  const detail=a=>{
    let h='';
    if(a.changes&&a.changes.length)h+=`<table class="chtbl"><thead><tr><th>Field</th><th>Before</th><th>After</th></tr></thead><tbody>${a.changes.map(c=>`<tr><td>${esc(c.f)}</td><td>${c.o?esc(c.o):'<span class="dim">empty</span>'}</td><td>${c.n?esc(c.n):'<span class="dim">empty</span>'}</td></tr>`).join('')}</tbody></table>`;
    h+=`<p class="sub meta">Role: ${esc(a.role?(ROLE_INFO[a.role]||[a.role])[0]:'n/a')} &middot; Device type: ${esc(a.category||'n/a')} &middot; Computer: ${esc(a.ip||'n/a')}</p>`;
    if(a.device_id)h+=`<div class="chiprow"><button class="btn sm" data-snap="${a.device_id}:${a.id}:${a.action==='Deleted'?'before':'after'}">View snapshot of this record</button><button class="btn sm" data-opendev="${a.device_id}">Open current record</button></div>`;
    if(a.children)h+=`<div style="margin-top:10px"><button class="btn sm" data-kids="${a.id}">Show affected devices</button></div><div class="kids" data-kidbox="${a.id}"></div>`;
    return h;
  };
  const rowHTML=a=>{const more=(a.changes&&a.changes.length)||a.children||a.device_id;
    return `<li class="arow" data-id="${a.id}"><div class="ahead ${more?'has':''}"><time>${fmtDT(a.ts)}</time><span><b>${esc(a.user||'system')}</b>${a.role?` <small class="dim">${esc((ROLE_INFO[a.role]||[a.role])[0])}</small>`:''}</span><span><span class="chip ${ACTION_CLASS(a.action)}">${esc(a.action)}</span></span><span>${a.tag?tagParts(a.tag):''}</span><span class="sub">${esc(a.details)}${a.children?` <span class="dim">(${num(a.children)} devices)</span>`:''}</span><span class="car">${more?'\u25BE':''}</span></div><div class="adetail" hidden></div></li>`};
  const load=async(append)=>{
    const r=await api('/audit?'+query(true));
    if(!filled){filled=true;$('#au').innerHTML=opts(r.users,'Everyone');$('#aa').innerHTML=opts(r.actions,'Any action');$('#ac').innerHTML=opts(r.categories,'All device types');$('#ay').innerHTML=opts(r.years,'Any year')}
    r.rows.forEach(a=>rowsById[a.id]=a);
    const html=r.rows.length?r.rows.map(rowHTML).join(''):'<li class="arow"><div class="ahead" style="display:block;color:var(--muted)">No activity matches these filters.</div></li>';
    if(append)$('#al').insertAdjacentHTML('beforeend',html);else $('#al').innerHTML=html;
    const shown=Object.keys(rowsById).length;
    $('#apg').innerHTML=`<span class="sub">Showing ${num(shown)} of ${num(r.total)} entr${r.total===1?'y':'ies'}</span>${shown<r.total?'<button class="btn sm" id="aMore">Show more</button>':''}`;
    if($('#aMore'))$('#aMore').onclick=()=>{A.offset+=A.limit;load(true)};
  };
  $('#al').onclick=async e=>{
    const kb=e.target.closest('[data-kids]');
    if(kb){const id=kb.dataset.kids,box=$(`[data-kidbox="${id}"]`);kb.disabled=true;
      try{const r=await api('/audit?'+qs({parent:id,limit:500}));box.innerHTML=r.rows.map(k=>`<div class="kid">${tagParts(k.tag)}<span class="sub">${esc(k.details)}</span>${k.device_id?`<button class="linkbtn" data-snap="${k.device_id}:${k.id}:${k.action==='Deleted'?'before':'after'}">Snapshot</button>`:''}</div>`).join('')+(r.total>r.rows.length?`<div class="sub">Showing ${num(r.rows.length)} of ${num(r.total)}. Export the CSV for the full list.</div>`:'')}catch(err){toast(err.message,'bad')}
      return}
    const sn=e.target.closest('[data-snap]');if(sn){const [dv,au,w]=sn.dataset.snap.split(':');openSnapshot(+dv,+au,w||'after');return}
    const od=e.target.closest('[data-opendev]');if(od){openDevice(+od.dataset.opendev).catch(()=>toast('That device has been deleted. Open its snapshot to bring it back.','bad'));return}
    const head=e.target.closest('.ahead.has');if(!head)return;
    const li=head.closest('.arow'),dt=li.querySelector('.adetail');
    if(dt.hidden){if(!dt.innerHTML)dt.innerHTML=detail(rowsById[li.dataset.id]);dt.hidden=false}else dt.hidden=true;
  };
  const apply=()=>{A.offset=0;load()};
  let t;const later=fn=>{clearTimeout(t);t=setTimeout(fn,240)};
  const bind=(id,key,ev,after)=>{const x=$('#'+id);x.addEventListener(ev,()=>{A[key]=x.value;if(after)after();ev==='input'?later(apply):apply()})};
  bind('aq','q','input');bind('atag','tag','input');bind('au','user','change');bind('ar','role','change');bind('ac','category','change');bind('ae','entity','change');
  bind('aa','action','change',()=>{A.group='';paintChips()});bind('ay','year','change');bind('am','month','change');
  bind('af','from','change',()=>{A.range='';paintChips()});bind('at','to','change',()=>{A.range='';paintChips()});
  bind('atf','tod_from','change');bind('att','tod_to','change');bind('ao','order','change');
  $('#ach').onchange=e=>{A.has_changes=e.target.checked;apply()};$('#aall').onchange=e=>{A.all=e.target.checked;apply()};
  $('#aRange').onclick=e=>{const b=e.target.closest('button');if(!b)return;A.range=b.dataset.r;const s=rangeStart(A.range);A.from=s?toLocalInput(s):'';A.to='';$('#af').value=A.from;$('#at').value='';paintChips();apply()};
  $('#aGroup').onclick=e=>{const b=e.target.closest('button');if(!b)return;A.group=b.dataset.g;A.action='';$('#aa').value='';paintChips();apply()};
  $('#aReset').onclick=()=>viewAudit(el);
  $('#aCsv').onclick=()=>dl('/api/audit.csv?'+query(false));
  paintChips();await load();
}

async function openSnapshot(deviceId,entryId,when){
  let cur=when||'after',lastRestore=null,changed=false;
  const d=openDialog('dlgSnap',`<div class="dlg" style="width:min(980px,96vw)"><div class="dlg-head"><div><h2>Record snapshot</h2><p class="sub">Compare this historical change with the current device record.</p></div><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div><div class="dlg-body"><div id="snBody"><div class="loading">Loading...</div></div></div><div class="dlg-foot" id="snFoot"></div></div>`);
  d.onclose=()=>{if(changed){const dv=$('#dlgDevice');if(dv&&dv.open)dv.close();route()}};
  const foot=html=>{const f=$('#snFoot',d);f.innerHTML=html;$$('[data-close]',f).forEach(b=>b.onclick=()=>d.close())};
  const paint=r=>{
    const e=r.entry;
    const banner=[];
    if(lastRestore)banner.push(`<div class="snban ok"><span>${lastRestore.removed?'The record was removed again.':'Restored. The record now matches the version you chose.'} The version before is kept in the activity log.</span><button class="btn sm" id="snUndo">Undo restore</button></div>`);
    if(!r.available)banner.push(`<div class="snban warn">${esc(r.reason)}</div>`);
    else if(!r.exists)banner.push(`<div class="snban warn">This device has been deleted. You can bring it back with the button below. Its tag and values come from this snapshot.</div>`);
    else if(!r.has_changes)banner.push(`<div class="snban">The current record already matches this version.</div>`);
    else banner.push(`<div class="snban">Review the highlighted fields below. Only changed values are emphasized.</div>`);
    if(r.category_missing)banner.push(`<div class="snban warn">The device type in this snapshot no longer exists. Add it back in Settings before restoring.</div>`);
    const seg=`<div class="seg" id="snSeg"><button type="button" data-w="before" class="${cur==='before'?'on':''}">Before this change</button><button type="button" data-w="after" class="${cur==='after'?'on':''}">After this change</button></div>`;
    const rows=r.available?r.diff.map(x=>`<tr class="${x.changed?'chg':''}"><td class="field">${esc(x.label)}</td><td class="val old">${x.changed? (x.then?esc(x.then):'<span class="dim">empty</span>') : (x.then?esc(x.then):'<span class="dim">empty</span>')}</td><td class="val new">${r.exists?(x.now?esc(x.now):'<span class="dim">empty</span>'):'<span class="dim">Not present</span>'}</td></tr>`).join(''):'';
    const table=r.available?`<div class="snap-card"><div class="snap-card-head"><h3>Field comparison</h3><span class="sub">${r.exists?'Snapshot vs current record':'Snapshot vs deleted record'}</span></div><div class="table-wrap"><table class="snap-table"><thead><tr><th>Field</th><th>${cur==='before'?'Value at this snapshot (before)':'Value at this snapshot (after)'}</th><th>Current record</th></tr></thead><tbody>${rows||`<tr><td colspan="3" class="snap-empty">No field data available.</td></tr>`}</tbody></table></div></div>`:'';
    $('#snBody',d).innerHTML=`<div class="snap-shell">
      <div class="snap-hero">
        <div class="snap-hero-left">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">${tagParts(r.tag)}<span class="snap-pill">${esc(e.action)}</span></div>
          <div class="snap-meta"><span>${esc(e.user||'system')}</span><span>&middot;</span><span>${fmtDT(e.ts)}</span>${cur==='before'?'<span>&middot;</span><span>Showing state before this entry</span>':'<span>&middot;</span><span>Showing state after this entry</span>'}</div>
        </div>
        <div class="snap-actions">${seg}</div>
      </div>
      ${banner.join('')}
      ${table}
    </div>`;
    $$('#snSeg button',d).forEach(b=>b.onclick=()=>{cur=b.dataset.w;load()});
    if($('#snUndo',d))$('#snUndo',d).onclick=undo;
    const canRestore=r.available&&!r.category_missing&&(r.has_changes);
    foot(`<button class="btn" data-close>Back to current record</button>${r.exists?'<button class="btn" id="snOpen">Open current record</button>':''}<span class="spacer"></span><button class="btn primary" id="snRestore" ${canRestore?'':'disabled'}>${r.exists?'Restore this version':'Bring this record back'}</button>`);
    if($('#snOpen',d))$('#snOpen',d).onclick=()=>{d.close();openDevice(r.device_id)};
    $('#snRestore',d).onclick=()=>restore(r);
  };
  const load=async()=>{
    let r;
    try{r=await api('/snapshot?'+qs({device:deviceId,audit:entryId,when:cur}))}
    catch(err){$('#snBody',d).innerHTML=`<div class="form-err" style="margin:0">${esc(err.message)}</div>`;foot('<span class="spacer"></span><button class="btn" data-close>Close</button>');return}
    paint(r);
  };
  const restore=async r=>{
    const list=r.diff.filter(x=>x.changed).map(x=>`<li><b>${esc(x.label)}</b>: ${x.now?esc(x.now):'<i>empty</i>'} &rarr; ${x.then?esc(x.then):'<i>empty</i>'}</li>`).join('');
    const ok=await confirmBox({title:r.exists?'Restore this version?':'Bring this record back?',body:`${r.exists?'These fields will change:':'The deleted device will be created again with these values:'}<ul style="margin:8px 0 0 18px;padding:0">${list}</ul><p style="margin-top:10px">The current version is kept in the activity log, and you can undo this straight away.</p>`,ok:r.exists?'Restore':'Bring back'});
    if(!ok)return;
    try{const res=await api('/snapshot/restore',{method:'POST',body:{device:deviceId,audit:entryId,when:cur}});lastRestore={id:res.audit_id,removed:!!res.removed};changed=true;toast(res.recreated?'Record brought back':'Version restored');await loadMeta();await load()}
    catch(err){toast(err.message,'bad')}
  };
  const undo=async()=>{
    try{const res=await api('/snapshot/restore',{method:'POST',body:{device:deviceId,audit:lastRestore.id,when:'before'}});lastRestore=null;changed=true;toast(res.removed?'Record removed again':'Back to how it was');await loadMeta();await load()}
    catch(err){toast(err.message,'bad')}
  };
  await load();
}

/* ---------- settings ---------- */
async function viewSettings(el){
  const m=await api('/meta');const st=m.settings;S.settings=st;S.categories=m.categories;
  let c={enabled:false,url:'',database:'',has_key:false,auto:0,start:true,pending:false,counts:{},last_push:'',last_pull:'',last_error:''};
  try{c=await api('/cloud')}catch(e){}
  let u={version:'',url:'',auto:true,last_check:'',latest:'',notes:'',error:'',available:false};
  try{u=await api('/update')}catch(e){}
  const cloudReady=!!(c.enabled&&c.url&&c.database&&c.has_key);
  el.innerHTML=`<div class="settings">
    <form class="panel" id="fmtForm" autocomplete="off"><h2>TAG format and label</h2><p class="sub" style="margin:4px 0 18px">Format changes apply to new tags only. Tags that already exist are never rewritten.</p>
      <div class="fgrid">
        <label class="field"><span>Prefix</span><input class="inp" name="prefix" value="${esc(st.prefix)}" maxlength="12" placeholder="GNET"></label>
        <label class="field"><span>Separator</span><select class="inp" name="sep">${[['-','Dash  -'],['_','Underscore  _'],['.','Dot  .'],['/','Slash  /'],['','None']].map(([v,l])=>`<option value="${v}" ${st.sep===v?'selected':''}>${l}</option>`).join('')}</select></label>
        <label class="field"><span>Number length</span><input class="inp" type="number" name="pad" min="1" max="10" value="${st.pad}"></label>
        <label class="field"><span>Default label size</span><select class="inp" name="label_size">${[['sm','Small 50 x 25 mm'],['md','Medium 70 x 35 mm'],['lg','Large 100 x 50 mm']].map(([v,l])=>`<option value="${v}" ${st.label_size===v?'selected':''}>${l}</option>`).join('')}</select></label>
        <label class="field"><span>Default paper</span><select class="inp" name="label_paper"><option value="a4" ${st.label_paper!=='letter'?'selected':''}>A4</option><option value="letter" ${st.label_paper==='letter'?'selected':''}>Letter</option></select></label>
        <label class="check"><input type="checkbox" name="include_year" ${st.include_year?'checked':''}> Include the year in each tag</label>
        <label class="field span2"><span>Code printed on each label</span><select class="inp" name="label_code"><option value="barcode" ${st.label_code!=='qr'?'selected':''}>Barcode (Code 128)</option><option value="qr" ${st.label_code==='qr'?'selected':''}>QR code</option></select><small>Both contain the tag. Any phone camera can read a QR code. A barcode needs a barcode scanner or a scanning app.</small></label>
        <label class="field span2"><span>Name at the top of every label</span><input class="inp" name="label_brand" value="${esc(st.label_brand==null?'BluePeak':st.label_brand)}" maxlength="24" placeholder="BluePeak"><small>Shown on every printed and PDF label. Clear it to leave the name off.</small></label>
        <label class="field span2"><span>Text at the bottom of every label</span><input class="inp" name="label_footer" value="${esc(st.label_footer)}" maxlength="60"></label>
      </div>
      <div class="sample"><span class="sub">Example tag</span><span class="tagcode" id="smp"></span></div>
      <div class="stage" id="smpLabel" style="display:flex;justify-content:center;background:var(--surface-2);border-radius:12px;padding:18px 10px;margin-top:12px;overflow:hidden"></div>
      <div class="form-actions"><button class="btn primary" type="submit">Save changes</button><span id="fmtErr" style="color:var(--bad);font-weight:600"></span></div></form>
    <section class="panel"><div style="display:flex;justify-content:space-between;align-items:center"><h2>Device types</h2><button class="btn sm" id="catAdd">${icon('plus',16)} Add type</button></div>
      <div class="table-wrap" style="margin-top:12px"><table class="tbl compact" style="min-width:0"><thead><tr><th>Name</th><th>Code</th><th>Devices</th><th></th></tr></thead><tbody>${m.categories.map(c=>`<tr style="cursor:default"><td>${esc(c.name)}</td><td><span class="tagcode">${esc(c.code)}</span></td><td>${num(c.devices)}</td><td><div class="row-actions"><button class="icon-btn" data-edit="${c.id}" aria-label="Edit ${esc(c.name)}">${icon('edit')}</button><button class="icon-btn" data-del="${c.id}" aria-label="Delete ${esc(c.name)}">${icon('trash')}</button></div></td></tr>`).join('')}</tbody></table></div></section>
    <section class="panel full"><h2>SQLite Cloud database</h2><p class="sub" style="margin-top:4px">Keep a secure, off-site copy of every device, user and setting. The app always works from the local copy first, then pushes changes up to SQLite Cloud.</p>
      <form id="cloudForm" autocomplete="off">
        <div class="fgrid" style="margin-top:16px">
          <label class="check span2"><input type="checkbox" name="enabled" ${c.enabled?'checked':''}> Use SQLite Cloud for this database</label>
          <label class="field span2"><span>Connection string or Weblite address</span><input class="inp" style="font-family:var(--mono);font-size:13px" name="url" value="${esc(c.url||'')}" maxlength="400" placeholder="sqlitecloud://project.zone.sqlite.cloud:8860/bluepeak_tag_master.sqlite?apikey=..."><small>Paste the connection string from the SQLite Cloud dashboard (bottom left, "Connect"), or the Weblite base URL from the Weblite panel, for example https://project.zone.gateway.sqlite.cloud</small></label>
          <label class="field"><span>API key</span><input class="inp" type="password" name="apikey" value="" maxlength="400" placeholder="${c.has_key?'Saved - type to replace':'Paste your API key'}"><small>Keep it secret. It is stored only in the local database file.</small></label>
          <label class="field"><span>Cloud database name</span><input class="inp" name="database" value="${esc(c.database||'')}" maxlength="80" placeholder="bluepeak_tag_master.sqlite"><small>Create this database in the SQLite Cloud dashboard first.</small></label>
          <label class="field"><span>Automatic push</span><select class="inp" name="auto">${[[0,'Only when I press Push'],[30,'Every 30 seconds'],[60,'Every minute'],[300,'Every 5 minutes'],[900,'Every 15 minutes']].map(([v,l])=>`<option value="${v}" ${String(c.auto)===String(v)?'selected':''}>${l}</option>`).join('')}</select></label>
          <label class="check"><input type="checkbox" name="start" ${c.start?'checked':''}> Pull from the cloud when the app starts</label>
        </div>
        <div class="form-actions"><button class="btn primary" type="submit">Save settings</button><button class="btn" type="button" id="cloudTest">Test connection</button><button class="btn" type="button" id="cloudPush">Push now</button><button class="btn danger" type="button" id="cloudPull">Pull now</button><span id="cloudMsg" style="font-weight:600"></span></div>
      </form>
      <dl class="kv" id="cloudStat" style="margin-top:18px"></dl></section>
    <section class="panel full"><div class="panel-lead"><div class="panel-title"><h2>App updates</h2><p class="sub">Point every copy at one update file. When you publish a newer version, each installation can download and install it from here.</p></div><span class="panel-badge">${icon('refresh')}</span></div>
      <form id="updForm" autocomplete="off">
        <div class="fgrid" style="margin-top:4px">
          <label class="field span2"><span>Update feed address</span><input class="inp" style="font-family:var(--mono);font-size:13px" name="url" value="${esc(u.url||'')}" maxlength="500" placeholder="https://your-site.example/bluepeak-update.json"><small>A small JSON file with the newest <b>version</b>, the installer <b>url</b> and its <b>sha256</b> checksum. HTTPS only. The address syncs through SQLite Cloud so other copies can use it too.</small></label>
          <label class="check span2"><input type="checkbox" name="auto" ${u.auto?'checked':''}> Check for updates automatically (twice a day)</label>
        </div>
        <div class="form-actions"><button class="btn primary" type="submit">Save update settings</button><button class="btn" type="button" id="updCheck">${icon('refresh',16)} Check now</button><button class="btn" type="button" id="updInstall" ${u.available?'':'hidden'}>Install update</button><span id="updMsg" style="font-weight:600"></span></div>
      </form>
      <dl class="kv" id="updStat" style="margin-top:18px"></dl></section>
    <section class="panel full"><h2>Your data</h2><p class="sub" style="margin-top:4px">TAG Master works from the local SQLite file on this computer, and mirrors it to SQLite Cloud when that is switched on. Download a backup before moving to a new PC or making large changes.</p>
      <dl class="kv">
        <dt>Database in use (local)</dt><dd class="mono-sm"><b>${esc(m.db_file)}</b> &mdash; ${esc(m.db_path)}</dd>
        <dt>SQLite Cloud database</dt><dd class="mono-sm">${cloudReady?esc(c.database)+' <span style="color:var(--ok);font-weight:700">&mdash; in use (sync is on)</span>':(c.enabled?esc(c.database||'not named yet')+' <span style="color:var(--warn);font-weight:700">&mdash; switched on, but the connection is incomplete</span>':(c.database?esc(c.database)+' &mdash; cloud sync is off':'No cloud database configured yet'))}</dd>
        <dt>Export scope</dt><dd>${cloudReady?'Backups below cover the local file; the SQLite Cloud copy is downloaded from SQLite Cloud itself.':'SQLite Cloud is off, so the local file above is your only copy.'}</dd>
      </dl>
      <div class="form-actions"><button class="btn primary" id="bkBackup">${icon('db')} Download local backup (.db)</button>${cloudReady?`<button class="btn" id="bkCloud">${icon('db')} Download SQLite Cloud copy (.db)</button>`:''}<button class="btn" id="bkXlsx">${icon('download')} Export Excel workbook</button><button class="btn" id="bkCsv">${icon('download')} Export all devices as CSV</button><input type="file" id="bkFile" accept=".db" hidden><button class="btn" id="bkRestore">${icon('upload')} Restore from backup</button></div>
      <p class="sub" id="bkLast" style="margin-top:10px">${m.backup_last?'Last automatic backup: '+esc(fmtDT(m.backup_last)):'No automatic backup has run yet.'} Restoring always replaces the <b>local</b> database${cloudReady?' &mdash; pull from SQLite Cloud if you want the cloud copy instead':''}.</p></section></div>`;
  pwToggles(el);
  const form=$('#fmtForm');
  const cur=()=>({prefix:form.prefix.value.trim().toUpperCase(),sep:form.sep.value,pad:Math.min(10,Math.max(1,parseInt(form.pad.value)||4)),include_year:form.include_year.checked});
  const smp=()=>{
    const tg=previewTag(cur(),'CHB',S.year,1);$('#smp').textContent=tg;
    const keep=S.settings;S.settings={...keep,label_brand:form.label_brand.value,label_footer:form.label_footer.value,label_code:form.label_code.value};
    $('#smpLabel').innerHTML=labelHTML(tg,'Chromebook',form.label_size.value);S.settings=keep;
  };
  form.addEventListener('input',smp);smp();
  form.onsubmit=async e=>{e.preventDefault();$('#fmtErr').textContent='';
    try{const r=await api('/settings',{method:'PUT',body:{...cur(),label_footer:form.label_footer.value,label_size:form.label_size.value,label_brand:form.label_brand.value,label_paper:form.label_paper.value,label_code:form.label_code.value}});S.settings=r.settings;toast('Settings saved')}catch(err){$('#fmtErr').textContent=err.message}};
  $('#bkBackup').onclick=()=>dl('/api/backup');
  const bc=$('#bkCloud');if(bc)bc.onclick=()=>{toast('Downloading the SQLite Cloud copy...');dl('/api/backup/cloud')};
  $('#bkCsv').onclick=()=>dl('/api/export.csv');$('#bkXlsx').onclick=()=>{toast('Preparing the Excel workbook...');dl('/api/export.xlsx')};
  $('#bkRestore').onclick=()=>$('#bkFile').click();
  $('#bkFile').onchange=async e=>{
    const f=e.target.files[0];if(!f)return;e.target.value='';
    const ok=await confirmBox({title:'Restore from this backup?',body:`Every device, user and setting currently in TAG Master will be replaced with the contents of <b>${esc(f.name)}</b>. A safety copy of the current data is saved in the backups folder first.`,ok:'Restore',danger:true});
    if(!ok)return;
    try{
      toast('Reading backup...');
      const b64=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(String(r.result).split(',')[1]||'');r.onerror=rej;r.readAsDataURL(f)});
      await api('/backup/restore',{method:'POST',body:{data:b64}});
      toast('Backup restored. Reloading...');setTimeout(()=>location.reload(),900);
    }catch(err){fail(err.message)}
  };
  const cf=$('#cloudForm'),cmsg=$('#cloudMsg');
  const cloudReq=()=>{const on=cf.enabled.checked;cf.url.required=on;cf.database.required=on;cf.apikey.required=on&&!c.has_key};
  cf.enabled.onchange=cloudReq;cloudReq();
  const cbody=()=>({enabled:cf.enabled.checked,url:cf.url.value.trim(),apikey:cf.apikey.value.trim(),database:cf.database.value.trim(),auto:cf.auto.value,start:cf.start.checked});
  const say=(t,bad)=>{cmsg.textContent=t||'';cmsg.style.color=bad?'var(--bad)':'var(--ok)'};
  const refresh=async()=>{try{const s=await api('/cloud');$('#cloudStat').innerHTML=cloudStatHTML(s);paintSync(s)}catch(e){}};
  cf.onsubmit=async e=>{e.preventDefault();say('Saving...');try{const r=await api('/cloud',{method:'PUT',body:cbody()});cf.apikey.value='';cf.apikey.placeholder='Saved - type to replace';$('#cloudStat').innerHTML=cloudStatHTML(r);paintSync(r);say('Settings saved')}catch(err){say(err.message,true)}};
  $('#cloudTest').onclick=async()=>{say('Testing the connection...');try{const r=await api('/cloud/test',{method:'POST',body:cbody()});say('Connected - '+r.tables.length+' tables in '+esc(r.database||''))}catch(err){say(err.message,true)}};
  $('#cloudPush').onclick=async()=>{say('Pushing...');try{const r=await api('/cloud/push',{method:'POST',body:cbody()});toast('Pushed '+num(r.rows||0)+' rows to SQLite Cloud');say('Pushed '+num(r.rows||0)+' rows');await loadMeta();await refresh()}catch(err){say(err.message,true);await refresh()}};
  $('#cloudPull').onclick=async()=>{const ok=await confirmBox({title:'Pull from SQLite Cloud?',body:'This replaces the local copy with the copy held in SQLite Cloud. A backup of the local file is saved first.',ok:'Pull now',danger:true});if(!ok)return;say('Pulling...');try{const r=await api('/cloud/pull',{method:'POST',body:cbody()});toast('Pulled '+num(r.rows||0)+' rows from SQLite Cloud');say('Pulled '+num(r.rows||0)+' rows');await loadMeta();await refresh();route()}catch(err){say(err.message,true);await refresh()}};
  $('#cloudStat').innerHTML=cloudStatHTML(c);
  const uf=$('#updForm'),umsg=$('#updMsg');
  const paintU=()=>{
    $('#updStat').innerHTML=`<dt>Current version</dt><dd><b>${esc(u.version||'')}</b></dd>
      <dt>Update feed</dt><dd>${u.url?esc(u.url):'Not set'}</dd>
      <dt>Last check</dt><dd>${u.last_check?fmtDT(u.last_check):'Never'}</dd>
      ${u.available?`<dt>Available</dt><dd><b>${esc(u.latest)}</b>${u.notes?`<div class="sub">${esc(u.notes)}</div>`:''}</dd>`:''}
      ${u.error?`<dt>Last error</dt><dd style="color:var(--bad);font-weight:600">${esc(u.error)}</dd>`:''}`;
    $('#updInstall').hidden=!u.available;
  };
  paintU();
  uf.onsubmit=async e=>{e.preventDefault();umsg.textContent='Saving...';umsg.style.color='var(--muted)';
    try{u=await api('/update',{method:'PUT',body:{url:uf.url.value.trim(),auto:uf.auto.checked}});S.update=u;paintU();paintUpdate(u);umsg.textContent='Update settings saved';umsg.style.color='var(--ok)'}
    catch(err){umsg.textContent=err.message;umsg.style.color='var(--bad)'}};
  $('#updCheck').onclick=async()=>{umsg.textContent='Checking...';umsg.style.color='var(--muted)';
    try{u=await api('/update/check',{method:'POST',body:{url:uf.url.value.trim()}});S.update=u;paintU();paintUpdate(u);umsg.textContent=u.available?('Version '+u.latest+' is available'):'You are up to date';umsg.style.color='var(--ok)'}
    catch(err){umsg.textContent=err.message;umsg.style.color='var(--bad)';try{u=await api('/update');paintU()}catch(e){}}};
  $('#updInstall').onclick=installUpdate;
  $('#catAdd').onclick=()=>catDialog(null);
  $$('[data-edit]',el).forEach(b=>b.onclick=()=>catDialog(m.categories.find(c=>c.id==b.dataset.edit)));
  $$('[data-del]',el).forEach(b=>b.onclick=async()=>{const c=m.categories.find(x=>x.id==b.dataset.del);
    const ok=await confirmBox({title:'Delete '+c.name+'?',body:'This removes the device type. It only works when no devices use it.',ok:'Delete type',danger:true});if(!ok)return;
    try{await api('/categories/'+c.id,{method:'DELETE'});toast('Removed '+c.name);await loadMeta();route()}catch(err){toast(err.message,'bad')}});
}
function catDialog(c){
  const d=openDialog('dlgCat',`<form class="dlg sm" autocomplete="off"><h2>${c?'Edit device type':'Add a device type'}</h2><div class="fgrid" style="margin-top:14px"><label class="field span2"><span>Name</span><input class="inp" name="name" value="${esc(c?c.name:'')}" required maxlength="40" placeholder="Smart Board"></label><label class="field span2"><span>Code used in tags</span><input class="inp" name="code" value="${esc(c?c.code:'')}" required maxlength="5" placeholder="SMB" style="text-transform:uppercase"><small>2 to 5 letters or numbers. Changing it does not change tags that already exist.</small></label></div><div class="form-err" id="cErr" hidden style="margin:14px 0 0"></div><div class="dlg-foot"><button type="button" class="btn" data-close>Cancel</button><button class="btn primary" type="submit">Save</button></div></form>`);
  $('form',d).onsubmit=async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));
    try{await api(c?'/categories/'+c.id:'/categories',{method:c?'PUT':'POST',body});d.close();await loadMeta();route()}catch(err){const x=$('#cErr',d);x.hidden=false;x.textContent=err.message}};
}

/* ---------- accounts ---------- */
const ROLE_INFO={
  admin:['Administrator','info','Full access, including users, settings, deleting devices and backups.'],
  technician:['Technician','ok','Generate tags, add and edit devices, import, print and export. Cannot delete devices or change settings.'],
  viewer:['Viewer','mute','Look up devices, print labels and export. Cannot change anything.']};
const roleChip=r=>{const i=ROLE_INFO[r]||[r,'mute'];return `<span class="chip ${i[1]}">${esc(i[0])}</span>`};
const can=a=>{const r=S.user&&S.user.role;return a==='admin'?r==='admin':a==='write'?(r==='admin'||r==='technician'):!!r};
function genPw(){const a='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';const b=new Uint32Array(12);crypto.getRandomValues(b);return [...b].map(x=>a[x%a.length]).join('')}

function showLogin(msg){
  $$('dialog[open]').forEach(d=>d.close());
  const ub=$('#updbar');if(ub){ub.hidden=true;ub.innerHTML=''}
  $('#login').hidden=false;document.body.style.overflow='hidden';
  const e=$('#loginErr');if(msg){e.hidden=false;e.textContent=msg}else e.hidden=true;
  const f=$('#loginForm');f.password.value='';S.user=null;
  $('#loginLabel').innerHTML=labelHTML('BLUE-LTP-2026-0001','Laptop','md');
  setTimeout(()=>f.username.focus(),50);
}
async function startApp(user){
  S.user=user;$('#login').hidden=true;document.body.style.overflow='';
  Object.assign(S.inv,{page:1,q:'',category:'',status:'',location:'',batch:'',warranty:''});clearSel();
  if(user.must_change){const ok=await passwordDialog(true);if(!ok)return;S.user.must_change=false}
  await loadMeta();buildNav();paintUser();loadUpdate();
  setTimeout(loadUpdate,15000);
  history.replaceState(null,'',location.pathname+'#/dashboard');route();
}
async function doLogout(){
  try{await api('/logout',{method:'POST'})}catch(e){}
  $('#view').innerHTML='';showLogin();
}
function paintUser(){
  const u=S.user,name=u.full_name||u.username;
  $('#userBtn').innerHTML=`<span class="avatar">${esc(name.trim().charAt(0).toUpperCase())}</span><span class="who"><b>${esc(name)}</b><small>${esc((ROLE_INFO[u.role]||[u.role])[0])}</small></span>`;
  $('#popPw').innerHTML=icon('ky')+' Change password';
  $('#popOut').innerHTML=icon('out')+' Sign out';
  const pe=$('#popExit');pe.hidden=u.role!=='admin';pe.innerHTML=icon('out')+' Exit app';
}
function passwordDialog(forced){
  return new Promise(res=>{
    const d=openDialog('dlgPw',`<form class="dlg sm" autocomplete="off"><h2>${forced?'Set a new password':'Change password'}</h2><p>${forced?'You are signed in with a temporary or default password. Choose your own to continue.':'Use at least 8 characters.'}</p>
      <div class="fgrid" style="margin-top:14px">
        <label class="field span2"><span>Current password</span><input class="inp" type="password" name="current" autocomplete="current-password" required></label>
        <label class="field span2"><span>New password</span><input class="inp" type="password" name="new" autocomplete="new-password" minlength="8" required></label>
        <label class="field span2"><span>Confirm new password</span><input class="inp" type="password" name="again" autocomplete="new-password" required></label></div>
      <div class="form-err" id="pwErr" hidden style="margin:14px 0 0"></div>
      <div class="dlg-foot">${forced?'<button type="button" class="btn" id="pwOut">Sign out</button>':'<button type="button" class="btn" data-close>Cancel</button>'}<button class="btn primary" type="submit">Save password</button></div></form>`);
    if(forced){d.oncancel=e=>e.preventDefault();d.onclick=null;$('#pwOut',d).onclick=async()=>{await doLogout();d.close();res(false)}}
    $('form',d).onsubmit=async e=>{
      e.preventDefault();const f=e.target,er=$('#pwErr',d);
      if(f.new.value!==f.again.value){er.hidden=false;er.textContent='The two new passwords do not match.';return}
      try{await api('/account/password',{method:'POST',body:{current:f.current.value,new:f.new.value}});d.close();toast('Password changed');res(true)}
      catch(err){er.hidden=false;er.textContent=err.message}
    };
    setTimeout(()=>$('input[name=current]',d).focus(),50);
  });
}

async function viewUsers(el){
  const r=await api('/users');
  el.innerHTML=`<div class="roles">${Object.entries(ROLE_INFO).map(([k,v])=>`<div class="panel">${roleChip(k)}<p class="sub" style="margin-top:10px">${esc(v[2])}</p></div>`).join('')}</div>
  <div class="toolbar"><h2 style="font-size:17px;font-weight:800">${r.users.length} user${r.users.length>1?'s':''}</h2><span class="spacer"></span><button class="btn primary" id="uAdd">${icon('plus')} Add user</button></div>
  <div class="panel flush"><div class="table-wrap"><table class="tbl" style="min-width:760px"><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead><tbody>${r.users.map(u=>{const me=u.id===S.user.id;return `<tr style="cursor:default"><td><b>${esc(u.full_name||u.username)}</b><div class="dim">@${esc(u.username)}${me?' (you)':''}</div></td><td>${roleChip(u.role)}</td><td>${u.active?'<span class="chip ok">Active</span>':'<span class="chip mute">Disabled</span>'}${u.must_change?' <span class="dim">must set a new password</span>':''}</td><td class="dim">${u.last_login?fmtDT(u.last_login):'Never'}</td><td><div class="row-actions"><button class="icon-btn" data-ed="${u.id}" title="Edit" aria-label="Edit ${esc(u.username)}">${icon('edit')}</button>${me?'':`<button class="icon-btn" data-pw="${u.id}" title="Reset password" aria-label="Reset password for ${esc(u.username)}">${icon('ky')}</button><button class="icon-btn" data-rm="${u.id}" title="Delete" aria-label="Delete ${esc(u.username)}">${icon('trash')}</button>`}</div></td></tr>`}).join('')}</tbody></table></div></div>`;
  const find=id=>r.users.find(u=>u.id==id);
  $('#uAdd').onclick=()=>userDialog(null);
  $$('[data-ed]',el).forEach(b=>b.onclick=()=>userDialog(find(b.dataset.ed)));
  $$('[data-pw]',el).forEach(b=>b.onclick=()=>resetDialog(find(b.dataset.pw)));
  $$('[data-rm]',el).forEach(b=>b.onclick=async()=>{const u=find(b.dataset.rm);
    const ok=await confirmBox({title:'Delete '+(u.full_name||u.username)+'?',body:'This account will be removed and can no longer sign in. Their past activity stays in the log.',ok:'Delete user',danger:true});if(!ok)return;
    try{await api('/users/'+u.id,{method:'DELETE'});toast('User deleted');route()}catch(err){toast(err.message,'bad')}});
}
function userDialog(u){
  const isNew=!u,self=u&&u.id===S.user.id;
  const d=openDialog('dlgUser',`<form class="dlg sm" style="width:min(500px,92vw)" autocomplete="off"><h2>${isNew?'Add a user':'Edit user'}</h2><div class="fgrid" style="margin-top:14px">
    <label class="field span2"><span>Full name</span><input class="inp" name="full_name" value="${esc(u?u.full_name:'')}" maxlength="80" required></label>
    <label class="field span2"><span>Username</span><input class="inp" name="username" value="${esc(u?u.username:'')}" ${isNew?'':'disabled'} maxlength="32" autocapitalize="off" required><small>3 to 32 letters, numbers, dots, dashes or underscores.</small></label>
    <label class="field span2"><span>Role</span><select class="inp" name="role" ${self?'disabled':''}>${Object.entries(ROLE_INFO).map(([k,v])=>`<option value="${k}" ${(u?u.role:'viewer')===k?'selected':''}>${v[0]}</option>`).join('')}</select><small id="roleDesc"></small></label>
    ${isNew?`<label class="field span2"><span>Temporary password</span><div class="pwgen"><input class="inp" name="password" value="${genPw()}" required minlength="8"><button type="button" class="btn" id="pwGen">Generate</button></div><small>Give this to the user. They will be asked to choose their own at first sign-in.</small></label>
    <label class="check span2"><input type="checkbox" name="must_change" checked> Ask for a new password at first sign-in</label>`:''}
    ${isNew||self?'':`<label class="check span2"><input type="checkbox" name="active" ${u.active?'checked':''}> Account is active</label>`}
    </div><div class="form-err" id="uErr" hidden style="margin:14px 0 0"></div>
    <div class="dlg-foot"><button type="button" class="btn" data-close>Cancel</button><button class="btn primary" type="submit">${isNew?'Create user':'Save'}</button></div></form>`);
  const f=$('form',d),desc=()=>{$('#roleDesc',d).textContent=ROLE_INFO[f.role.value][2]};
  f.role.onchange=desc;desc();
  if(isNew)$('#pwGen',d).onclick=()=>{f.password.value=genPw()};
  f.onsubmit=async e=>{e.preventDefault();
    const body={full_name:f.full_name.value.trim(),role:f.role.value};
    if(isNew){body.username=f.username.value.trim();body.password=f.password.value;body.must_change=f.must_change.checked}
    else if(f.active)body.active=f.active.checked;
    if(self)delete body.role;
    try{await api(isNew?'/users':'/users/'+u.id,{method:isNew?'POST':'PUT',body});d.close();toast(isNew?'User created':'User saved');if(self){S.user.full_name=body.full_name;paintUser()}route()}
    catch(err){const x=$('#uErr',d);x.hidden=false;x.textContent=err.message}};
}
function resetDialog(u){
  const d=openDialog('dlgUser',`<form class="dlg sm" style="width:min(480px,92vw)" autocomplete="off"><h2>Reset password</h2><p>Set a temporary password for <b>${esc(u.full_name||u.username)}</b>. They will be signed out everywhere.</p>
    <div class="fgrid" style="margin-top:14px"><label class="field span2"><span>Temporary password</span><div class="pwgen"><input class="inp" name="password" value="${genPw()}" required minlength="8"><button type="button" class="btn" id="pwGen">Generate</button></div></label>
    <label class="check span2"><input type="checkbox" name="must_change" checked> Ask for a new password at next sign-in</label></div>
    <div class="form-err" id="uErr" hidden style="margin:14px 0 0"></div>
    <div class="dlg-foot"><button type="button" class="btn" data-close>Cancel</button><button class="btn primary" type="submit">Reset password</button></div></form>`);
  const f=$('form',d);$('#pwGen',d).onclick=()=>{f.password.value=genPw()};
  f.onsubmit=async e=>{e.preventDefault();
    try{await api('/users/'+u.id+'/password',{method:'POST',body:{password:f.password.value,must_change:f.must_change.checked}});d.close();toast('Password reset for '+u.username);route()}
    catch(err){const x=$('#uErr',d);x.hidden=false;x.textContent=err.message}};
}

/* ---------- shell ---------- */
const VIEWS={
  dashboard:['Overview',viewDashboard,'grid',''],
  generate:['Generate tags',viewGenerate,'tag','write'],
  inventory:['Inventory',viewInventory,'list',''],
  audit:['Activity',viewAudit,'pulse','admin'],
  users:['Users',viewUsers,'users','admin'],
  settings:['Settings',viewSettings,'sliders','admin']};
function buildNav(){
  $('#nav').innerHTML=Object.entries(VIEWS).filter(([k,v])=>!v[3]||can(v[3])).map(([k,v])=>`<a href="#/${k}" data-v="${k}">${icon(v[2],20)}<span>${v[0]}</span></a>`).join('');
}
let routeSeq=0;
async function route(){
  if(!S.user)return;
  const name=(location.hash.replace('#/','')||'dashboard').split('?')[0];
  let key=VIEWS[name]?name:'dashboard';
  if(VIEWS[key][3]&&!can(VIEWS[key][3]))key='dashboard';
  const v=VIEWS[key],my=++routeSeq;
  $$('#nav a').forEach(a=>a.classList.toggle('on',a.dataset.v===key));
  $('#title').textContent=v[0];document.title=v[0]+' | BluePeak TAG Master';
  const el=$('#view');el.innerHTML='<div class="loading">Loading...</div>';
  try{await v[1](el)}catch(e){if(my===routeSeq&&S.user){el.innerHTML=`<div class="panel"><h2>Something went wrong</h2><p class="sub" style="margin-top:6px">${esc(e.message)}</p></div>`;fail(e.message)}}
}
window.addEventListener('unhandledrejection',e=>{const m=(e.reason&&e.reason.message)||String(e.reason||'Unexpected error');if(S.user)fail(m)});
window.addEventListener('hashchange',route);
$('#scanBtn').onclick=()=>scanDialog();
$('#gsearch').addEventListener('keydown',e=>{if(e.key==='Enter'){const v=e.target.value.trim();if(v){e.target.value='';goInventory({q:v})}e.target.blur()}else if(e.key==='Escape'){e.target.value='';e.target.blur()}});
document.addEventListener('keydown',e=>{if((e.key==='s'||e.key==='S')&&$('#login').hidden&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)&&!document.querySelector('dialog[open]')){e.preventDefault();scanDialog()}});
document.addEventListener('keydown',e=>{if(e.key==='/'&&$('#login').hidden&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)&&!document.querySelector('dialog[open]')){e.preventDefault();$('#gsearch').focus()}});
const setTheme=t=>{document.documentElement.dataset.theme=t;try{localStorage.setItem('bp-theme',t)}catch(e){}$('#themeBtn').innerHTML=icon(t==='dark'?'sun':'moon',20)};
let saved=null;try{saved=localStorage.getItem('bp-theme')}catch(e){}
setTheme(saved||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
$('#themeBtn').onclick=()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');
$('#userBtn').onclick=e=>{e.stopPropagation();$('#userPop').hidden=!$('#userPop').hidden};
document.addEventListener('click',()=>{$$('.pop').forEach(p=>p.hidden=true)});
$('#popPw').onclick=()=>passwordDialog(false);
$('#popOut').onclick=doLogout;
$('#popExit').onclick=async()=>{
  const ok=await confirmBox({title:'Stop TAG Master?',body:'The app and its database server stop on this computer. Start it again from the Start menu shortcut or the app folder.',ok:'Exit app',danger:true});
  if(!ok)return;
  try{await api('/exit',{method:'POST'})}catch(e){}
  document.body.innerHTML='<div style="display:grid;place-items:center;min-height:100vh;font-family:system-ui;text-align:center;padding:24px"><div><h1 style="font-size:26px;margin:0 0 8px">TAG Master has stopped</h1><p style="color:var(--muted)">You can close this tab.</p></div></div>';
};
pwToggles(document);
$('#loginForm').onsubmit=async e=>{
  e.preventDefault();const f=e.target,btn=$('#loginBtn');btn.disabled=true;$('#loginErr').hidden=true;
  try{const r=await api('/login',{method:'POST',body:{username:f.username.value.trim(),password:f.password.value}});f.password.value='';await startApp(r.user)}
  catch(err){$('#loginErr').hidden=false;$('#loginErr').textContent=err.message}
  btn.disabled=false;
};
(async()=>{
  let s;
  try{s=await (await fetch('/api/session',{credentials:'same-origin'})).json()}
  catch(e){$('#view').innerHTML='<div class="panel"><h2>Cannot reach the TAG Master server</h2><p class="sub">Make sure the app is still running, then refresh this page.</p></div>';return}
  if(s.user)await startApp(s.user);else showLogin();
})();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    if sys.stdout is None:
        try:
            sys.stdout = open(LOG_PATH, "a", encoding="utf-8")
            sys.stderr = sys.stdout
        except OSError:
            pass
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Stopped. Your data is saved in the database file.")
    except Exception as _e:
        traceback.print_exc()
        fatal_error(_e)
        sys.exit(1)
