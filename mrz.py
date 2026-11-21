"""
mrz.py - checking the machine-readable zone (MRZ) of a passport or ID card (version 4).

The MRZ is the block of letters at the bottom of a passport photo page (ICAO Document 9303):

    TD3 (passport, 2 lines of 44):  P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<
                                    L898902C36UTO7408122F1204159ZE184226B<<<<<10
    TD1 (ID card, 3 lines of 30)

Most fields carry a check digit: each character gets a value (0-9 -> 0-9, A-Z -> 10-35, < -> 0), the
values are multiplied by 7, 3, 1, 7, 3, 1 ... and the check digit is the sum modulo 10. A typing or
reading mistake almost always breaks a check digit, and a made-up document often does too.

PyBank does not read the photo itself (there is no OCR here): the customer or the bank officer types
the MRZ lines, and this module checks the digits and compares the name, date of birth and expiry
date with the application. The officer still looks at the photo before approving.
Only the last 3 characters of the document number are stored.
"""

import re
from datetime import date

from core import BankError, now

MRZ_SCHEMA = """
CREATE TABLE IF NOT EXISTS mrz_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    doc_type    TEXT NOT NULL,
    doc_end     TEXT NOT NULL,
    nationality TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    problems    TEXT NOT NULL DEFAULT '',
    checked_by  TEXT NOT NULL,
    checked_at  TEXT NOT NULL
);
"""
WEIGHTS = (7, 3, 1)


def char_value(ch):
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - 55
    if ch == "<":
        return 0
    raise ValueError(ch)


def check_digit(text):
    return str(sum(char_value(c) * WEIGHTS[i % 3] for i, c in enumerate(text)) % 10)


def _date(yymmdd, future=False):
    """MRZ dates have 2-digit years: a birth date is in the past, an expiry date may be in the future."""
    if not re.fullmatch(r"\d{6}", yymmdd):
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    this = date.today().year % 100
    century = 2000 if (yy <= this + (20 if future else 0)) else 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        return None


def clean_lines(text):
    lines = [re.sub(r"\s", "", ln).upper().replace("«", "<") for ln in str(text or "").splitlines() if ln.strip()]
    return lines


def parse(text):
    """Read MRZ lines. Returns a dict with the fields and `errors` (a list of what is wrong)."""
    lines = clean_lines(text)
    if len(lines) == 2 and all(len(ln) == 44 for ln in lines):
        kind = "TD3"
    elif len(lines) == 3 and all(len(ln) == 30 for ln in lines):
        kind = "TD1"
    else:
        raise BankError("Please type the 2 lines of 44 characters of a passport, or the 3 lines of 30 characters "
                        "of an ID card, exactly as printed (use < for the arrows).")
    if any(not re.fullmatch(r"[A-Z0-9<]+", ln) for ln in lines):
        raise BankError("The MRZ may contain only capital letters, digits and the sign <.")
    errors = []
    if kind == "TD3":
        l1, l2 = lines
        doc_type, country, names = l1[0:2], l1[2:5], l1[5:44]
        number, n_cd, nat, birth, b_cd, sex, expiry, e_cd = (l2[0:9], l2[9], l2[10:13], l2[13:19], l2[19], l2[20],
                                                             l2[21:27], l2[27])
        optional, o_cd, final = l2[28:42], l2[42], l2[43]
        composite = l2[0:10] + l2[13:20] + l2[21:43]
        if optional.strip("<"):
            if check_digit(optional) != o_cd:
                errors.append("check digit of the personal number")
        elif o_cd not in "<0":
            errors.append("check digit of the personal number")
    else:
        l1, l2, l3 = lines
        doc_type, country, number, n_cd = l1[0:2], l1[2:5], l1[5:14], l1[14]
        birth, b_cd, sex, expiry, e_cd, nat = l2[0:6], l2[6], l2[7], l2[8:14], l2[14], l2[15:18]
        names, final = l3, l2[29]
        composite = l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29]
    if check_digit(number) != n_cd:
        errors.append("check digit of the document number")
    if check_digit(birth) != b_cd:
        errors.append("check digit of the date of birth")
    if check_digit(expiry) != e_cd:
        errors.append("check digit of the expiry date")
    if check_digit(composite) != final:
        errors.append("final check digit")
    surname, _, given = names.partition("<<")
    born, expires = _date(birth), _date(expiry, future=True)
    if born is None:
        errors.append("date of birth is not a real date")
    if expires is None:
        errors.append("expiry date is not a real date")
    if sex not in "MF<":
        errors.append("sex must be M, F or <")
    return {"kind": kind, "doc_type": doc_type.strip("<"), "country": country.strip("<"),
            "number": number.strip("<"), "nationality": nat.strip("<"),
            "surname": surname.replace("<", " ").strip(), "given": given.replace("<", " ").strip(),
            "birth": born, "expiry": expires, "sex": sex.replace("<", "X"), "errors": errors}


def _words(name):
    return {w for w in re.sub(r"[^A-Z ]", " ", name.upper()).split() if w}


def compare(m, full_name, dob, today=None):
    """Problems found when the MRZ is compared with the application (empty list = all good)."""
    today = today or date.today()
    problems = list(m["errors"])
    mrz_words = _words(m["surname"] + " " + m["given"])
    app_words = _words(full_name)
    if not mrz_words or not app_words or len(mrz_words & app_words) < min(2, len(app_words)):
        problems.append("the name does not match the application")
    if m["birth"] and str(m["birth"]) != str(dob):
        problems.append("the date of birth does not match the application")
    if m["expiry"] and m["expiry"] < today:
        problems.append("the document has expired")
    return problems


class MrzMixin:

    def check_mrz(self, cid, text, checked_by):
        """Check the MRZ of a customer's document, save the result (without the full number) and return it."""
        c = self.get_customer(cid)
        m = parse(text)
        problems = compare(m, c.full_name, c.dob)
        with self._connect() as conn:
            conn.execute("INSERT INTO mrz_checks (customer_id, doc_type, doc_end, nationality, ok, problems, checked_by, "
                         "checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (cid, f"{m['kind']} {m['doc_type']}", m["number"][-3:], m["nationality"], int(not problems),
                          "; ".join(problems), checked_by, now()))
        return {**m, "problems": problems, "ok": not problems}

    def mrz_checks(self, cid):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM mrz_checks WHERE customer_id = ? ORDER BY id DESC",
                                                  (cid,))]
