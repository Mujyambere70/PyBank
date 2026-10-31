"""
db.py - one small database layer for PyBank that works with two databases:

    SQLite      - a file on your computer (default, nothing to install)
    PostgreSQL  - a database server, used online (Render / Neon / Supabase ...)

The rest of the code writes normal SQL with "?" placeholders, exactly as for SQLite.
For PostgreSQL this module translates it (placeholders, AUTOINCREMENT, LIKE, RETURNING id ...).

Choose the database with an environment variable:
    DATABASE_URL=postgresql://user:password@host/dbname     -> PostgreSQL
    (not set)                                               -> SQLite file bank.db

Why: free web hosts (e.g. Render free plan) delete the local disk at every restart,
so an SQLite file there is reset again and again. A PostgreSQL database keeps the data.
"""

import queue
import re
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal


def is_postgres_url(url):
    return str(url or "").startswith(("postgres://", "postgresql://"))


# ------------------------------------------------------------------ rows

class Row:
    """A result row that works like sqlite3.Row: row["name"], row[0], dict(row), row.keys()."""

    __slots__ = ("_keys", "_values", "_index")

    def __init__(self, keys, values):
        self._keys = keys
        self._values = tuple(_clean(v) for v in values)
        self._index = {k: i for i, k in enumerate(keys)}

    def keys(self):
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._keys, self._values))})"


def _clean(v):
    """PostgreSQL returns SUM() of integers as Decimal - give back a plain int like SQLite does."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


# ------------------------------------------------------------------ SQL translation (SQLite dialect -> PostgreSQL)

def schema_for_postgres(script):
    s = re.sub(r"--[^\n]*", "", script)
    s = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bINTEGER\b", "BIGINT", s, flags=re.I)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s, flags=re.I)
    return [x.strip() for x in s.split(";") if x.strip()]


def to_postgres(sql):
    sql = sql.replace("%", "%%")
    out, quote = [], False
    for ch in sql:                       # "?" outside quotes -> "%s"
        if ch == "'":
            quote = not quote
        out.append("%s" if ch == "?" and not quote else ch)
    sql = "".join(out)
    sql = re.sub(r"\bLIKE\b", "ILIKE", sql)                # SQLite LIKE ignores case, like ILIKE
    return sql


# ------------------------------------------------------------------ PostgreSQL connection wrapper

class PgCursor:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid
        self.rowcount = cur.rowcount
        self.description = cur.description
        self._keys = [d[0] for d in cur.description] if cur.description else []

    def fetchone(self):
        r = self._cur.fetchone()
        return None if r is None else Row(self._keys, r)

    def fetchall(self):
        return [Row(self._keys, r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    is_postgres = True

    def __init__(self, raw, serial_tables):
        self.raw = raw
        self.serial_tables = serial_tables
        self.after_commit = []

    def execute(self, sql, params=()):
        lastrowid = None
        m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
        returning = bool(m and m.group(1).lower() in self.serial_tables and "RETURNING" not in sql.upper())
        if returning:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self.raw.cursor()
        cur.execute(to_postgres(sql), tuple(params))
        if returning:
            row = cur.fetchone()
            lastrowid = row[0] if row else None
        return PgCursor(cur, lastrowid)

    def executescript(self, script):
        cur = self.raw.cursor()
        for statement in schema_for_postgres(script):
            cur.execute(statement)

    def columns(self, table):
        cur = self.raw.cursor()
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
        return [r[0] for r in cur.fetchall()]

    def tables(self):
        cur = self.raw.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
        return {r[0] for r in cur.fetchall()}

    def reset_sequence(self, table):
        cur = self.raw.cursor()
        cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")


class SqliteConnection:
    """Thin wrapper so both databases have the same helper methods."""
    is_postgres = False

    def __init__(self, raw):
        self.raw = raw
        self.after_commit = []

    def execute(self, sql, params=()):
        return self.raw.execute(sql, params)

    def executescript(self, script):
        self.raw.executescript(script)

    def columns(self, table):
        return [r[1] for r in self.raw.execute(f"PRAGMA table_info({table})")]

    def tables(self):
        return {r[0] for r in self.raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def reset_sequence(self, table):
        pass                              # SQLite continues after MAX(id) by itself


# ------------------------------------------------------------------ the database object used by the app

class Database:
    def __init__(self, target, driver=None):
        """target = path of an SQLite file, or a postgresql:// URL."""
        self.target = str(target)
        self.is_postgres = is_postgres_url(self.target)
        self.serial_tables = set()
        self._driver = driver
        self._pool = queue.LifoQueue(maxsize=self.POOL_SIZE)
        if self.is_postgres and self._driver is None:
            try:
                import psycopg                              # pip install "psycopg[binary]"
            except ImportError as e:                        # pragma: no cover
                raise RuntimeError("DATABASE_URL is a PostgreSQL URL: install psycopg "
                                   "(pip install \"psycopg[binary]\")") from e
            self._driver = psycopg

    def init_schema(self, script):
        self.serial_tables = {m.lower() for m in re.findall(
            r"CREATE TABLE IF NOT EXISTS (\w+) \(\s*id\s+INTEGER PRIMARY KEY AUTOINCREMENT", script, re.I)}
        with self.connect() as conn:
            conn.executescript(script)

    POOL_SIZE = 4            # PostgreSQL connections kept open for reuse (a new one over the internet is slow)
    CHECK_AFTER = 30         # seconds: a connection unused longer than this is tested before reuse

    def _new_pg(self):
        url = self.target.replace("postgres://", "postgresql://", 1)
        return self._driver.connect(url)

    def _open(self):
        if self.is_postgres:
            raw = None
            while raw is None:
                try:
                    raw, last_used = self._pool.get_nowait()
                except queue.Empty:
                    raw = self._new_pg()
                    break
                if time.time() - last_used > self.CHECK_AFTER:     # the server may have closed it (e.g. Neon sleeps)
                    try:
                        raw.cursor().execute("SELECT 1")
                        raw.rollback()
                    except Exception:
                        self._discard(raw)
                        raw = None
            return PgConnection(raw, self.serial_tables)
        raw = sqlite3.connect(self.target, timeout=15)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        return SqliteConnection(raw)

    @staticmethod
    def _discard(raw):
        try:
            raw.close()
        except Exception:
            pass

    def _release(self, conn, healthy):
        if not self.is_postgres or not healthy:
            self._discard(conn.raw)
            return
        try:
            self._pool.put_nowait((conn.raw, time.time()))
        except queue.Full:
            self._discard(conn.raw)

    @contextmanager
    def connect(self):
        """Commit if everything worked, roll back on any error, always close.
        Functions in conn.after_commit run only after a successful commit (e.g. sending alerts)."""
        conn = self._open()
        healthy = True
        try:
            yield conn
            conn.raw.commit()
        except BaseException:
            try:
                conn.raw.rollback()
            except Exception:
                healthy = False
            raise
        finally:
            self._release(conn, healthy)
        for fn in conn.after_commit:
            try:
                fn()
            except Exception:                           # an alert must never break a payment
                pass

    def describe(self):
        if not self.is_postgres:
            return f"SQLite file ({self.target.rsplit('/', 1)[-1]})"
        host = re.sub(r"^.*@", "", self.target).split("/")[0].split("?")[0]
        return f"PostgreSQL ({host})"
