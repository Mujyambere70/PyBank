"""Shared test helpers.

The tests run on SQLite by default. To run the same tests on PostgreSQL:
    TEST_DATABASE_URL=postgresql://user@host:5432/dbname python -m unittest discover tests
(the database is emptied before every test - use a database only for tests!)
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
DRIVER = None
if PG_URL:
    try:
        import psycopg as DRIVER                      # the real driver, when installed
    except ImportError:
        import pgmini as DRIVER                       # small built-in client for tests


def target(path):
    """Where a test database lives: the SQLite file `path`, or an emptied PostgreSQL database."""
    if not PG_URL:
        return path
    conn = DRIVER.connect(PG_URL)
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE")
    cur.execute("CREATE SCHEMA public")
    conn.commit()
    conn.close()
    return PG_URL


def cash(bank, cid, amount, pin, note="", kind="Deposit"):
    """Customer asks for a deposit/withdrawal and a bank officer approves it at once. Returns the transaction."""
    ref = bank.request_cash(cid, kind, amount, pin, note)
    rid = [r for r in bank.cash_requests(cid) if r["ref"] == ref][0]["id"]
    return bank.decide_cash_request(rid, True, "Test Officer")[1]
