#!/usr/bin/env python3
"""
SQLite Cloud link for BluePeak TAG Master
-----------------------------------------
Talks to the SQLite Cloud Weblite REST API (HTTPS + JSON) using nothing but
the Python standard library, so the packaged .exe needs no extra packages.

    POST  <base>/v2/weblite/sql      {"sql": "...", "database": "..."}
    GET   <base>/v2/weblite/<db>     the whole database file (used to pull)
    GET   <base>/v2/health           connectivity check

Push = every local table is copied up to the cloud database (replace).
Pull = the cloud database is copied down into the local file (replace).
A local backup is taken by the caller before a pull.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 45
BATCH = 120
SYNC_TABLES = ["settings", "categories", "counters", "users", "devices", "activity", "sessions"]
PRIVATE_PREFIX = "cloud_"
LOCAL_ONLY_SETTINGS = ("update_last_check", "update_latest", "update_notes", "update_error")
DB_NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
SQLITE_MAGIC = b"SQLite format 3\x00"


class CloudError(Exception):
    pass


def _normalise(cfg):
    url = str(cfg.get("url") or "").strip()
    key = str(cfg.get("apikey") or "").strip()
    db = str(cfg.get("database") or "").strip()
    if not url:
        raise CloudError("No SQLite Cloud address has been set yet.")
    if url.startswith("sqlitecloud://"):
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.split("@")[-1].split(":")[0]
        if not db and parts.path.strip("/"):
            db = parts.path.strip("/").split("/")[0]
        if not key:
            qs = urllib.parse.parse_qs(parts.query)
            key = (qs.get("apikey") or qs.get("token") or [""])[0]
        base = "https://" + host
    elif url.startswith(("http://", "https://")):
        base = url.rstrip("/")
    else:
        base = "https://" + url.strip("/")
    if base.endswith("/v2/weblite/sql"):
        v2 = base[: -len("/weblite/sql")]
    elif base.endswith("/v2"):
        v2 = base
    else:
        v2 = base + "/v2"
    if not key:
        raise CloudError("No SQLite Cloud API key has been set yet.")
    if not db:
        raise CloudError("No cloud database name has been set yet.")
    return {"sql": v2 + "/weblite/sql", "v2": v2, "key": key, "db": db}


def _auth(key):
    if key.lower().startswith("bearer ") or key.startswith("sqlitecloud://"):
        return key
    return "Bearer " + key


def _request(c, method, url, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "Authorization": _auth(c["key"]),
               "User-Agent": "BluePeak-TagMaster/1.3"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise CloudError("Cannot reach SQLite Cloud: %s" % getattr(e, "reason", e))
    except Exception as e:
        raise CloudError("Cannot reach SQLite Cloud: %s" % e)


def _error_text(raw, code):
    try:
        p = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        p = None
    if isinstance(p, dict):
        err = p.get("error")
        if isinstance(err, dict):
            for k in ("detail", "message", "title", "details"):
                v = err.get(k)
                if v and isinstance(v, (str, int, float)):
                    return str(v)[:400]
        for k in ("message", "error_description", "error", "details", "status"):
            v = p.get(k)
            if v and isinstance(v, (str, int, float)):
                return str(v)[:400]
    txt = raw.decode("utf-8", "replace").strip()[:400]
    return txt or ("SQLite Cloud returned HTTP %d." % code)


def call(c, sql):
    payload = {"sql": sql}
    if c["db"]:
        payload["database"] = c["db"]
    status, raw = _request(c, "POST", c["sql"], payload)
    if status >= 400:
        raise CloudError(_error_text(raw, status))
    try:
        out = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        raise CloudError("SQLite Cloud sent a response the app could not read.")
    if isinstance(out, dict) and out.get("error"):
        raise CloudError(str(out.get("message") or out.get("error"))[:400])
    return out


def rows(out):
    meta = out.get("metadata") if isinstance(out, dict) else None
    data = out.get("data", out) if isinstance(out, dict) else out
    if isinstance(data, dict):
        for k in ("rows", "data", "results", "records"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            return [data] if data else []
    if not isinstance(data, list):
        return []
    names = [c.get("name") for c in ((meta or {}).get("columns") or []) if isinstance(c, dict)]
    out_rows = []
    for row in data:
        if isinstance(row, dict):
            out_rows.append(row)
        elif isinstance(row, list) and names and len(row) == len(names):
            out_rows.append(dict(zip(names, row)))
    return out_rows


def table_names(c):
    out = call(c, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return {r.get("name") for r in rows(out) if isinstance(r, dict) and r.get("name")}


def _cloud_columns(c, table):
    out = call(c, 'SELECT * FROM "%s" LIMIT 0' % table)
    meta = out.get("metadata") if isinstance(out, dict) else None
    names = [x.get("name") for x in ((meta or {}).get("columns") or []) if isinstance(x, dict)]
    if names:
        return set(names)
    got = rows(out)
    if got and isinstance(got[0], dict):
        return set(got[0].keys())
    return None


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return "x'%s'" % v.hex()
    s = str(v).replace("\x00", "")
    return "'" + s.replace("'", "''") + "'"


def _open_local(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def push(cfg, db_path):
    """Copy every local table up to the cloud database, replacing its contents."""
    c = _normalise(cfg)
    existing = table_names(c)
    src = _open_local(db_path)
    copied = {}
    snapshot_fp = None
    try:
        src.execute("BEGIN")
        snapshot_fp = _fingerprint_snapshot(src)
        prepared = []
        for table in SYNC_TABLES:
            ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                              (table,)).fetchone()
            if not ddl:
                continue
            info = list(src.execute('PRAGMA table_info("%s")' % table))
            if not info:
                continue
            cols = [r["name"] for r in info]
            data = [tuple(r) for r in src.execute('SELECT * FROM "%s"' % table)]
            if table == "settings" and "key" in cols:
                ki = cols.index("key")
                data = [r for r in data
                        if not str(r[ki] or "").startswith(PRIVATE_PREFIX)
                        and str(r[ki] or "") not in LOCAL_ONLY_SETTINGS]
            prepared.append((table, ddl[0], info, cols, data))
        # Make sure every table and column exists before touching any rows.
        for table, ddl, info, cols, data in prepared:
            if table not in existing:
                call(c, ddl)
                existing.add(table)
            else:
                have = _cloud_columns(c, table)
                if have is not None:
                    for r in info:
                        if r["name"] not in have:
                            call(c, 'ALTER TABLE "%s" ADD COLUMN "%s" %s'
                                 % (table, r["name"], r["type"] or "TEXT"))
        # Clear child tables first: devices references categories and sessions
        # references users, so deleting a parent before its children trips the
        # cloud foreign keys. Inserts below run parents-first again.
        for table, ddl, info, cols, data in reversed(prepared):
            call(c, 'DELETE FROM "%s"' % table)
        for table, ddl, info, cols, data in prepared:
            if not data:
                copied[table] = 0
                continue
            names = ", ".join('"%s"' % x for x in cols)
            sent = 0
            for i in range(0, len(data), BATCH):
                chunk = data[i:i + BATCH]
                values = ",\n".join(
                    "(" + ", ".join(_lit(v) for v in row) + ")" for row in chunk)
                call(c, 'INSERT INTO "%s" (%s) VALUES\n%s' % (table, names, values))
                sent += len(chunk)
            copied[table] = sent
        src.execute("COMMIT")
    except BaseException:
        try:
            src.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        src.close()
    return {"tables": sorted(copied), "rows": sum(copied.values()), "counts": copied,
            "fingerprint": snapshot_fp}


def _download(c):
    url = c["v2"] + "/weblite/" + urllib.parse.quote(c["db"])
    status, raw = _request(c, "GET", url)
    if status >= 400:
        raise CloudError(_error_text(raw, status))
    return raw


def download(cfg):
    """Return the cloud database file as raw bytes, ready to save as a .db file."""
    blob = _download(_normalise(cfg))
    if not blob or not blob.startswith(SQLITE_MAGIC):
        raise CloudError("SQLite Cloud did not return a database file. Use Pull instead.")
    return blob


def pull(cfg, db_path):
    """Replace the local tables with the copy held in SQLite Cloud."""
    c = _normalise(cfg)
    blob = _download(c)
    if not blob or not blob.startswith(SQLITE_MAGIC):
        return _pull_sql(c, db_path)
    tmp = db_path + ".cloudpull"
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
        return _pull_file(tmp, db_path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _pull_file(tmp, db_path):
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("ATTACH DATABASE ? AS cloud", (tmp,))
        cloud_tables = {r[0] for r in con.execute(
            "SELECT name FROM cloud.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        present = [t for t in SYNC_TABLES if t in cloud_tables]
        if not present:
            raise CloudError("The cloud database has no data yet. Push your data first.")
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        keep = {r["key"]: r["value"] for r in con.execute(
            "SELECT key,value FROM main.settings WHERE " + _private_where())}
        for table in reversed(SYNC_TABLES):
            if table in present:
                con.execute('DELETE FROM main."%s"' % table)
        copied = {}
        for table in SYNC_TABLES:
            if table not in present:
                continue
            local = [r["name"] for r in con.execute('PRAGMA main.table_info("%s")' % table)]
            remote = [r["name"] for r in con.execute('PRAGMA cloud.table_info("%s")' % table)]
            use = [x for x in local if x in remote]
            if not use or not local:
                continue
            con.execute('INSERT %s INTO main."%s" (%s) SELECT %s FROM cloud."%s"' % (
                "OR REPLACE" if table == "settings" else "",
                table,
                ", ".join('"%s"' % x for x in use),
                ", ".join('"%s"' % x for x in use),
                table))
            copied[table] = con.execute('SELECT COUNT(*) FROM main."%s"' % table).fetchone()[0]
        for k, v in keep.items():
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        con.execute("COMMIT")
        con.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        try:
            con.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        raise
    finally:
        try:
            con.execute("DETACH DATABASE cloud")
        except Exception:
            pass
        con.close()
    return {"tables": sorted(copied), "counts": copied, "rows": sum(copied.values())}


def _pull_sql(c, db_path):
    con = _open_local(db_path)
    try:
        have = table_names(c)
        present = [t for t in SYNC_TABLES if t in have]
        if not present:
            raise CloudError("The cloud database has no data yet. Push your data first.")
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        keep = {r["key"]: r["value"] for r in con.execute(
            "SELECT key,value FROM settings WHERE " + _private_where())}
        for table in reversed(SYNC_TABLES):
            if table in present:
                con.execute('DELETE FROM "%s"' % table)
        copied = {}
        for table in present:
            got = rows(call(c, 'SELECT * FROM "%s"' % table))
            if not got:
                copied[table] = 0
                continue
            cols = [x for x in got[0].keys() if isinstance(x, str)]
            names = ", ".join('"%s"' % x for x in cols)
            sent = 0
            for i in range(0, len(got), BATCH):
                chunk = got[i:i + BATCH]
                values = ",\n".join(
                    "(" + ", ".join(_lit(row.get(x)) for x in cols) + ")" for row in chunk)
                con.execute('INSERT INTO "%s" (%s) VALUES\n%s' % (table, names, values))
                sent += len(chunk)
            copied[table] = sent
        for k, v in keep.items():
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        con.execute("COMMIT")
        con.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        try:
            con.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        raise
    finally:
        con.close()
    return {"tables": sorted(copied), "counts": copied, "rows": sum(copied.values())}


def test(cfg):
    """Check the address, the key and that we can actually run SQL there."""
    c = _normalise(cfg)
    status, raw = _request(c, "GET", c["v2"] + "/health")
    health = None
    if status < 400:
        try:
            health = json.loads(raw.decode("utf-8")).get("data")
        except Exception:
            health = None
    call(c, "SELECT 1 AS ok")
    names = sorted(table_names(c))
    return {"ok": True, "endpoint": c["sql"], "database": c["db"],
            "tables": names, "health": health, "at": int(time.time())}


def remote_tables(cfg):
    """Names of the tables that already exist in the cloud database."""
    return table_names(_normalise(cfg))


def _private_where(col="key"):
    quoted = ", ".join("'%s'" % k for k in LOCAL_ONLY_SETTINGS)
    return "%s LIKE '%s%%' OR %s IN (%s)" % (col, PRIVATE_PREFIX, col, quoted)


def _fingerprint_table(con, table):
    where = " WHERE NOT (%s)" % _private_where() if table == "settings" else ""
    cols = [r["name"] for r in con.execute('PRAGMA table_info("%s")' % table)]
    count = con.execute('SELECT COUNT(*) FROM "%s"%s' % (table, where)).fetchone()[0]
    if count <= 4000:
        h = hashlib.sha1()
        for row in con.execute('SELECT * FROM "%s"%s' % (table, where)):
            h.update(repr(tuple(row)).encode("utf-8", "replace"))
        return "%d:%s" % (count, h.hexdigest())
    # Large table: hash rowid bounds plus a deterministic sample so that a
    # delete+insert pair, or rows edited within the same second, are still
    # detected (max(updated_at) alone can hide both).
    lo = con.execute('SELECT COALESCE(MIN(rowid),0) FROM "%s"' % table).fetchone()[0]
    hi = con.execute('SELECT COALESCE(MAX(rowid),0) FROM "%s"' % table).fetchone()[0]
    h = hashlib.sha1()
    h.update(("count=%d,lo=%s,hi=%s" % (count, lo, hi)).encode("utf-8"))
    stride = max(1, count // 200)
    for row in con.execute(
            'SELECT * FROM "%s"%s WHERE rowid %% ? = 0 ORDER BY rowid LIMIT 400' % (table, where),
            (stride,)):
        h.update(repr(tuple(row)).encode("utf-8", "replace"))
    return "%d:%s" % (count, h.hexdigest())


def fingerprint(db_path):
    """Cheap signature of everything that syncs, used to skip needless pushes."""
    con = _open_local(db_path)
    try:
        return _fingerprint_snapshot(con)
    finally:
        con.close()


def _fingerprint_snapshot(con):
    """Fingerprint computed over an already-open connection.

    Used both by fingerprint() and by push() so the value recorded as 'synced'
    reflects exactly the snapshot that was uploaded, not the live database.
    """
    parts = []
    for table in SYNC_TABLES:
        try:
            cols = [r["name"] for r in con.execute('PRAGMA table_info("%s")' % table)]
            if not cols:
                continue
            parts.append("%s=%s" % (table, _fingerprint_table(con, table)))
        except sqlite3.Error:
            continue
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def counts(db_path):
    out = {}
    con = _open_local(db_path)
    try:
        for table in SYNC_TABLES:
            try:
                out[table] = con.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
            except sqlite3.Error:
                out[table] = 0
    finally:
        con.close()
    return out
