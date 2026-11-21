"""
devices.py - signed-in devices, rate limits and the health of the system (version 4).

Devices: every Net Banking login now has a row in `sessions` (a random id kept in the signed cookie).
The Security page lists the phones and browsers that are signed in; the customer can sign out one of
them or all the others. A login from a browser and system never seen before for this customer sends a
"new device" alert. A signed-out session id is refused at the next click, even if somebody copied the
cookie.

Rate limits: a fixed window counter per name and "who" (an IP address, a customer, an API token).
When the limit is passed the request gets "429 Too many requests" and the event is counted for the
admin's report.

Health: /health answers in JSON (database answer time, the last backup, the last daily job) so a
monitoring service can watch the bank; /status shows the same for people.
"""

import hashlib
import re
import secrets
import time
from datetime import date, datetime, timedelta

from core import BankError, now

DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    ip          TEXT NOT NULL DEFAULT '',
    device      TEXT NOT NULL DEFAULT '',
    via         TEXT NOT NULL DEFAULT 'web',
    ended_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions(customer_id, ended_at);
CREATE TABLE IF NOT EXISTS rate_hits (
    bucket TEXT NOT NULL,
    slot   INTEGER NOT NULL,
    n      INTEGER NOT NULL,
    PRIMARY KEY (bucket, slot)
);
CREATE TABLE IF NOT EXISTS rate_blocks (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    who   TEXT NOT NULL,
    at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    path   TEXT NOT NULL,
    error  TEXT NOT NULL
);
"""
# name -> (requests, window in seconds)
LIMITS = {"login": (10, 600), "staff_login": (10, 600), "register": (5, 3600), "api_token": (10, 600),
          "api": (120, 60), "mrz": (20, 600)}


def device_name(user_agent):
    """'Mozilla/5.0 (Windows NT 10.0; ...) Chrome/131...' -> 'Chrome on Windows'."""
    ua = user_agent or ""
    browser = ("Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Chrome" if "Chrome/" in ua else
               "Firefox" if "Firefox/" in ua else "Safari" if "Safari/" in ua else
               "PyBank app" if "PyBankApp" in ua else "Browser")
    system = ("Android" if "Android" in ua else "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else
              "Windows" if "Windows" in ua else "macOS" if "Mac OS" in ua else "Linux" if "Linux" in ua else
              "unknown system")
    return f"{browser} on {system}"


def mask_ip(ip):
    """Show only the first parts of an IP address (privacy)."""
    ip = str(ip or "")
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + ":…"
    parts = ip.split(".")
    return ".".join(parts[:2] + ["x", "x"]) if len(parts) == 4 else ip


class DevicesMixin:

    # ===== signed-in devices =====

    def start_session(self, cid, ip, user_agent, via="web"):
        device = device_name(user_agent)
        sid = secrets.token_urlsafe(24)
        with self._connect() as conn:
            seen = conn.execute("SELECT 1 FROM sessions WHERE customer_id = ? AND device = ? LIMIT 1",
                                (cid, device)).fetchone()
            first = conn.execute("SELECT 1 FROM sessions WHERE customer_id = ? LIMIT 1", (cid,)).fetchone()
            conn.execute("INSERT INTO sessions (id, customer_id, created_at, last_seen, ip, device, via) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, cid, now(), now(), mask_ip(ip), device, via))
            if first and not seen:
                self._alert(conn, cid, f"New sign-in: {device}",
                            f"Your Net Banking was opened from a new device: {device} ({mask_ip(ip)}) on "
                            f"{datetime.now():%d-%m-%Y %H:%M}. If this was not you, sign it out in Net Banking > "
                            f"Security and change your password.")
        return sid

    def touch_session(self, cid, sid):
        """True if the session is still valid. Updates 'last seen' at most once a minute."""
        with self._connect() as conn:
            r = conn.execute("SELECT last_seen, ended_at FROM sessions WHERE id = ? AND customer_id = ?",
                             (sid, cid)).fetchone()
            if r is None or r["ended_at"]:
                return False
            if r["last_seen"] < (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"):
                conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now(), sid))
        return True

    def end_session(self, cid, sid):
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ? AND customer_id = ? AND ended_at = ''",
                         (now(), sid, cid))

    def end_other_sessions(self, cid, keep):
        with self._connect() as conn:
            return conn.execute("UPDATE sessions SET ended_at = ? WHERE customer_id = ? AND id != ? AND ended_at = ''",
                                (now(), cid, keep)).rowcount

    def sessions(self, cid, idle_minutes=10):
        """Devices that are signed in now (a web session ends after `idle_minutes` without a click)."""
        cut = (datetime.now() - timedelta(minutes=idle_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE customer_id = ? AND ended_at = '' "
                                                  "ORDER BY last_seen DESC LIMIT 20", (cid,))]
        return [r for r in rows if r["via"] != "web" or r["last_seen"] >= cut]

    # ===== rate limits =====

    def rate_ok(self, name, who):
        """Count one request; False when the limit of `name` for `who` is passed in this time window."""
        limit, window = LIMITS[name]
        slot = int(time.time() // window)
        bucket = f"{name}:{hashlib.sha256(str(who).encode()).hexdigest()[:16]}"
        with self._connect() as conn:
            conn.execute("INSERT INTO rate_hits (bucket, slot, n) VALUES (?, ?, 1) "
                         "ON CONFLICT(bucket, slot) DO UPDATE SET n = rate_hits.n + 1", (bucket, slot))
            n = conn.execute("SELECT n FROM rate_hits WHERE bucket = ? AND slot = ?", (bucket, slot)).fetchone()[0]
            if n == limit + 1:
                shown = who if re.fullmatch(r"PB\d{8}", str(who)) else mask_ip(who) if "." in str(who) or ":" in str(who) \
                    else str(who)[:6] + "…"
                conn.execute("INSERT INTO rate_blocks (name, who, at) VALUES (?, ?, ?)", (name, shown, now()))
        return n <= limit

    def rate_report(self, days=7):
        since = (date.today() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT name, who, COUNT(*) times, MAX(at) last FROM rate_blocks "
                                                  "WHERE at >= ? GROUP BY name, who ORDER BY last DESC LIMIT 50", (since,))]
        return rows

    def clean_rate_hits(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM rate_hits WHERE slot < ?", (int(time.time() // 3600) - 2,))
            conn.execute("DELETE FROM rate_blocks WHERE at < ?", ((date.today() - timedelta(days=90)).isoformat(),))

    # ===== errors and health =====

    def log_error(self, path, error):
        try:
            with self._connect() as conn:
                conn.execute("INSERT INTO error_log (at, path, error) VALUES (?, ?, ?)", (now(), path[:200], str(error)[:500]))
        except Exception:
            pass

    def errors(self, limit=30):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM error_log ORDER BY id DESC LIMIT ?", (limit,))]

    def health(self):
        t0 = time.perf_counter()
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
                last_backup = conn.execute("SELECT MAX(created_at) FROM backups").fetchone()[0] or ""
                fx = conn.execute("SELECT MAX(updated_at) FROM fx_rates").fetchone()[0] or ""
                waiting = conn.execute("SELECT COUNT(*) FROM external_transfers WHERE status = 'Processing'").fetchone()[0]
                errors = conn.execute("SELECT COUNT(*) FROM error_log WHERE at >= ?",
                                      ((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()[0]
            db_ok = True
        except Exception:
            db_ok, last_backup, fx, waiting, errors = False, "", "", 0, 0
        ms = round((time.perf_counter() - t0) * 1000, 1)
        settings = self.settings() if db_ok else {}
        backup_age = None
        if last_backup:
            backup_age = round((datetime.now() - datetime.strptime(last_backup, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600, 1)
        checks = {
            "database": {"ok": db_ok, "detail": f"{'PostgreSQL' if self.db.is_postgres else 'SQLite'}, answered in {ms} ms"},
            "backup": {"ok": backup_age is not None and backup_age <= 30,
                       "detail": f"last copy {backup_age} hours ago" if backup_age is not None else "no backup yet"},
            "daily_jobs": {"ok": settings.get("last_daily_tasks", "") >= (date.today() - timedelta(days=1)).isoformat(),
                           "detail": f"last run {settings.get('last_daily_tasks') or 'never'}"},
            "exchange_rates": {"ok": bool(fx), "detail": f"updated {fx[:16]}" if fx else "no rates yet"},
            "payments_queue": {"ok": waiting < 200, "detail": f"{waiting} payments to other banks waiting"},
            "errors": {"ok": errors < 20, "detail": f"{errors} server errors in the last 24 hours"},
        }
        return {"status": "ok" if all(c["ok"] for c in checks.values()) else ("down" if not db_ok else "degraded"),
                "db_ms": ms, "checks": checks, "time": now()}
