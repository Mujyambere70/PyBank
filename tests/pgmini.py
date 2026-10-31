"""
pgmini.py - a tiny PostgreSQL client used ONLY by the tests, when psycopg is not installed.

It speaks the PostgreSQL "simple query" protocol and offers the few DB-API calls that db.py uses
(connect, cursor.execute with %s, fetchone, fetchall, rowcount, description, commit, rollback, close).
Parameters are inserted as safely quoted literals. The real application uses psycopg.
"""

import hashlib
import socket
import struct
from decimal import Decimal
from urllib.parse import unquote, urlparse

INT_OIDS = {20, 21, 23, 26}
FLOAT_OIDS = {700, 701}
BOOL_OID, NUMERIC_OID = 16, 1700


class Error(Exception):
    pass


def _literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, Decimal)):
        return repr(v) if isinstance(v, float) else str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _interpolate(sql, params):
    out, i, k = [], 0, 0
    while i < len(sql):
        if sql.startswith("%%", i):
            out.append("%"); i += 2
        elif sql.startswith("%s", i):
            out.append(_literal(params[k])); k += 1; i += 2
        else:
            out.append(sql[i]); i += 1
    if k != len(params):
        raise Error(f"{len(params)} parameters for {k} placeholders")
    return "".join(out)


class Cursor:
    def __init__(self, conn):
        self.conn, self.rows, self.description, self.rowcount = conn, [], None, -1

    def execute(self, sql, params=()):
        if not self.conn.in_tx:
            self.conn._simple("BEGIN")
            self.conn.in_tx = True
        cols, rows, tag = self.conn._simple(_interpolate(sql, list(params)))
        self.description = [(c[0], c[1]) for c in cols] if cols else None
        self.rows = rows
        parts = tag.split()
        self.rowcount = int(parts[-1]) if parts and parts[-1].isdigit() else -1

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class Connection:
    def __init__(self, url):
        u = urlparse(url)
        self.user, self.password = unquote(u.username or ""), unquote(u.password or "")
        self.sock = socket.create_connection((u.hostname or "localhost", u.port or 5432))
        self.in_tx = False
        body = b"user\0" + self.user.encode() + b"\0database\0" + (u.path.lstrip("/") or self.user).encode() + b"\0\0"
        self.sock.sendall(struct.pack("!ii", len(body) + 8, 196608) + body)
        while True:
            t, data = self._read()
            if t == b"R":
                code = struct.unpack("!i", data[:4])[0]
                if code == 3:
                    self._send(b"p", self.password.encode() + b"\0")
                elif code == 5:
                    inner = hashlib.md5((self.password + self.user).encode()).hexdigest()
                    self._send(b"p", b"md5" + hashlib.md5(inner.encode() + data[4:8]).hexdigest().encode() + b"\0")
                elif code != 0:
                    raise Error(f"authentication method {code} not supported by pgmini")
            elif t == b"E":
                raise Error(self._err(data))
            elif t == b"Z":
                break

    def _send(self, t, body):
        self.sock.sendall(t + struct.pack("!i", len(body) + 4) + body)

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise Error("connection closed")
            buf += chunk
        return buf

    def _read(self):
        t = self._recv(1)
        n = struct.unpack("!i", self._recv(4))[0]
        return t, self._recv(n - 4)

    @staticmethod
    def _err(data):
        fields = {f[:1]: f[1:].decode(errors="replace") for f in data.split(b"\0") if f}
        return f"{fields.get(b'C', '')}: {fields.get(b'M', 'error')}"

    def _simple(self, sql):
        self._send(b"Q", sql.encode() + b"\0")
        cols, rows, tag, error = [], [], "", None
        while True:
            t, data = self._read()
            if t == b"T":
                n, pos = struct.unpack("!h", data[:2])[0], 2
                for _ in range(n):
                    end = data.index(b"\0", pos)
                    name = data[pos:end].decode()
                    oid = struct.unpack("!i", data[end + 7:end + 11])[0]
                    cols.append((name, oid))
                    pos = end + 19
            elif t == b"D":
                n, pos, row = struct.unpack("!h", data[:2])[0], 2, []
                for i in range(n):
                    size = struct.unpack("!i", data[pos:pos + 4])[0]
                    pos += 4
                    if size == -1:
                        row.append(None)
                        continue
                    raw = data[pos:pos + size].decode()
                    pos += size
                    oid = cols[i][1]
                    row.append(int(raw) if oid in INT_OIDS else float(raw) if oid in FLOAT_OIDS
                               else Decimal(raw) if oid == NUMERIC_OID else raw == "t" if oid == BOOL_OID else raw)
                rows.append(tuple(row))
            elif t == b"C":
                tag = data.rstrip(b"\0").decode()
            elif t == b"E":
                error = self._err(data)
            elif t == b"Z":
                if error:
                    raise Error(error)
                return cols, rows, tag

    def cursor(self):
        return Cursor(self)

    def commit(self):
        if self.in_tx:
            self._simple("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            try:
                self._simple("ROLLBACK")
            finally:
                self.in_tx = False

    def close(self):
        try:
            self._send(b"X", b"")
        finally:
            self.sock.close()


def connect(url):
    return Connection(url)
