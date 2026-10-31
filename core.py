"""core.py - small things every PyBank module needs (kept apart to avoid circular imports)."""

from datetime import datetime


class BankError(Exception):
    """Error that can be shown to the user (wrong PIN, not enough money, ...)."""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
