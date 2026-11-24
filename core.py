"""core.py - small things every PyBank module needs (kept apart to avoid circular imports)."""

from datetime import datetime


class BankError(Exception):
    """Error that can be shown to the user (wrong PIN, not enough money, ...)."""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _CounterPin:
    """Passed instead of a PIN when a bank worker does the operation at the counter (version 5): the
    worker checks the customer's identity document, and the worker's own login and abilities are checked."""

    def __repr__(self):
        return "COUNTER"


COUNTER = _CounterPin()
