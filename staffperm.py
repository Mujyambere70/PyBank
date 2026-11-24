"""
staffperm.py - job roles in the database and the abilities of each staff member (version 5).

    effective abilities = abilities of the role  +  extra abilities of the person  -  taken away

The administrator (role 'admin') always has every ability, and nobody can take the last
administrator's rights away. Each change is written in the audit log.

A staff member can also have his own amount limit for counter work ("max_amount", in rupees;
other currencies are compared at the middle rate). Above his limit - or above the bank's
four-eyes amount - the operation waits for a second person.
"""

import re

import roles
from core import BankError, now

ROLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_roles (
    code       TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    abilities  TEXT NOT NULL DEFAULT '',
    builtin    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);
"""
STAFF_COLUMNS = [("extra_abilities", "TEXT NOT NULL DEFAULT ''"), ("denied_abilities", "TEXT NOT NULL DEFAULT ''"),
                 ("max_amount", "INTEGER NOT NULL DEFAULT 0"), ("photo", "TEXT NOT NULL DEFAULT ''"),
                 ("address", "TEXT NOT NULL DEFAULT ''"), ("dob", "TEXT NOT NULL DEFAULT ''"),
                 ("emergency_contact", "TEXT NOT NULL DEFAULT ''"), ("branch", "TEXT NOT NULL DEFAULT ''")]


def join_codes(codes):
    return ",".join(sorted(c for c in set(codes) if c in roles.ABILITIES))


class StaffPermMixin:
    """Mixed into the back-office class (Office)."""

    # ===== roles =====

    def ensure_roles(self):
        with self._connect() as conn:
            have = {r[0] for r in conn.execute("SELECT code FROM job_roles")}
            for code, r in roles.DEFAULT_ROLES.items():
                if code not in have:
                    conn.execute("INSERT INTO job_roles (code, label, abilities, builtin, updated_at) VALUES (?, ?, ?, 1, ?)",
                                 (code, r["label"], join_codes(r["abilities"]), now()))

    def job_roles(self):
        """{code: {"label", "abilities" (set), "builtin", "people"}} in a stable order."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM job_roles ORDER BY builtin DESC, label").fetchall()
            people = {r[0]: r[1] for r in conn.execute(
                "SELECT job_role, COUNT(*) FROM staff WHERE role = 'staff' GROUP BY job_role")}
        return {r["code"]: {"label": r["label"], "abilities": roles.split_codes(r["abilities"]),
                            "builtin": r["builtin"], "people": people.get(r["code"], 0),
                            "updated_at": r["updated_at"], "updated_by": r["updated_by"]} for r in rows}

    def _attach(self, conn, s):
        """Add `_abilities` and `_job_label` to a staff row."""
        if s is None:
            return None
        s = dict(s)
        if s.get("role") == "admin":
            s["_abilities"] = set(roles.ABILITIES)
            s["_job_label"] = "Administrator"
            return s
        r = conn.execute("SELECT label, abilities FROM job_roles WHERE code = ?", (s.get("job_role") or "officer",)).fetchone()
        base = roles.split_codes(r[1]) if r else roles.default_abilities(s.get("job_role"))
        s["_abilities"] = (base | roles.split_codes(s.get("extra_abilities"))) - roles.split_codes(s.get("denied_abilities"))
        s["_job_label"] = r[0] if r else roles.job_label({"job_role": s.get("job_role")})
        return s

    def get_staff(self, sid):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM staff WHERE staff_id = ?", (sid,)).fetchone()
            return self._attach(conn, r)

    def staff_list(self):
        with self._connect() as conn:
            return [self._attach(conn, r) for r in conn.execute("SELECT * FROM staff ORDER BY role, full_name").fetchall()]

    def _clean_role(self, label, abilities):
        label = " ".join((label or "").split())[:60]
        if len(label) < 3:
            raise BankError("Please give the role a name (at least 3 letters).")
        unknown = [a for a in abilities if a not in roles.ABILITIES]
        if unknown:
            raise BankError("Unknown ability: " + ", ".join(unknown[:3]))
        codes = list(dict.fromkeys(abilities))
        if not codes:
            raise BankError("A role needs at least one ability.")
        return label, join_codes(codes)

    def save_role(self, by, code, label, abilities):
        """Create a role (code empty) or change one. Returns the code."""
        label, codes = self._clean_role(label, abilities)
        with self._connect() as conn:
            if not code:
                base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30] or "role"
                code, n = base, 2
                while conn.execute("SELECT 1 FROM job_roles WHERE code = ?", (code,)).fetchone():
                    code, n = f"{base}_{n}", n + 1
                if conn.execute("SELECT 1 FROM job_roles WHERE LOWER(label) = ?", (label.lower(),)).fetchone():
                    raise BankError("A role with this name already exists.")
                conn.execute("INSERT INTO job_roles (code, label, abilities, builtin, updated_at, updated_by) "
                             "VALUES (?, ?, ?, 0, ?, ?)", (code, label, codes, now(), by["staff_id"]))
                self.audit(conn, by, "Job role created", code, f"{label}: {codes}")
            else:
                if conn.execute("SELECT 1 FROM job_roles WHERE LOWER(label) = ? AND code <> ?",
                                (label.lower(), code)).fetchone():
                    raise BankError("A role with this name already exists.")
                if conn.execute("UPDATE job_roles SET label = ?, abilities = ?, updated_at = ?, updated_by = ? "
                                "WHERE code = ?", (label, codes, now(), by["staff_id"], code)).rowcount == 0:
                    raise BankError("Role not found.")
                self.audit(conn, by, "Job role changed", code, f"{label}: {codes}")
        return code

    def reset_role(self, by, code):
        if code not in roles.DEFAULT_ROLES:
            raise BankError("Only the starting roles can be reset.")
        r = roles.DEFAULT_ROLES[code]
        with self._connect() as conn:
            conn.execute("UPDATE job_roles SET label = ?, abilities = ?, updated_at = ?, updated_by = ? WHERE code = ?",
                         (r["label"], join_codes(r["abilities"]), now(), by["staff_id"], code))
            self.audit(conn, by, "Job role reset", code, r["label"])

    def delete_role(self, by, code):
        with self._connect() as conn:
            row = conn.execute("SELECT builtin FROM job_roles WHERE code = ?", (code,)).fetchone()
            if row is None:
                raise BankError("Role not found.")
            if row[0]:
                raise BankError("The starting roles cannot be deleted (you can change them).")
            if conn.execute("SELECT 1 FROM staff WHERE job_role = ? AND role = 'staff'", (code,)).fetchone():
                raise BankError("Some staff members still have this role. Give them another role first.")
            conn.execute("DELETE FROM job_roles WHERE code = ?", (code,))
            self.audit(conn, by, "Job role deleted", code)

    # ===== one person's abilities =====

    def set_job_role(self, by, sid, job):
        with self._connect() as conn:
            r = conn.execute("SELECT label FROM job_roles WHERE code = ?", (job or "",)).fetchone()
            if r is None:
                raise BankError("Unknown job role.")
            if sid == by["staff_id"]:
                raise BankError("You cannot change your own role.")
            if conn.execute("UPDATE staff SET job_role = ? WHERE staff_id = ?", (job, sid)).rowcount == 0:
                raise BankError("Staff member not found.")
            self.audit(conn, by, "Job role changed", sid, r[0])

    def set_staff_access(self, by, sid, form):
        """Job role, extra / taken-away abilities and the amount limit of one person."""
        target = self.get_staff(sid)
        if target is None:
            raise BankError("Staff member not found.")
        if sid == by["staff_id"]:
            raise BankError("You cannot change your own abilities. Ask another administrator.")
        job = form.get("job_role") or target.get("job_role") or "officer"
        extra, denied = set(), set()
        for code in roles.ABILITIES:
            choice = form.get("ab_" + code, "role")
            if choice == "yes":
                extra.add(code)
            elif choice == "no":
                denied.add(code)
        try:
            max_amount = int(round(float((form.get("max_amount") or "0").replace(",", "")) * 100))
        except ValueError:
            raise BankError("The amount limit must be a number (0 = only the bank's four-eyes amount).")
        if not 0 <= max_amount <= 10 ** 13:
            raise BankError("The amount limit is too big.")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM job_roles WHERE code = ?", (job,)).fetchone() is None:
                raise BankError("Unknown job role.")
            conn.execute("UPDATE staff SET job_role = ?, extra_abilities = ?, denied_abilities = ?, max_amount = ? "
                         "WHERE staff_id = ?", (job, join_codes(extra), join_codes(denied), max_amount, sid))
            self.audit(conn, by, "Abilities changed", sid,
                       f"role {job}; extra {join_codes(extra) or '-'}; taken away {join_codes(denied) or '-'}; "
                       f"limit {max_amount // 100 if max_amount else 'bank'}")

    def amount_in_inr(self, amount, currency):
        """Amount (minor units) in rupee paise at the middle rate; the same number when there is no rate."""
        if currency == "INR":
            return amount
        try:
            return int(amount * self.bank.middle_rate(currency, "INR"))
        except BankError:
            return amount

    def over_own_limit(self, me, amount, currency):
        """True when the amount is above the person's own counter limit (admins have none)."""
        if me.get("role") == "admin" or not me.get("max_amount"):
            return False
        return self.amount_in_inr(amount, currency) > me["max_amount"]

    # ===== the person's own details (also the administrator) =====

    def update_own_details(self, me, form, photo=None):
        name, email, phone = self._check_staff_fields(form.get("full_name"), form.get("email"), form.get("phone"))
        designation = " ".join((form.get("designation") or "").split())[:60]
        address = " ".join((form.get("address") or "").split())[:200]
        emergency = " ".join((form.get("emergency_contact") or "").split())[:100]
        dob = (form.get("dob") or "").strip()
        if dob and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            raise BankError("Please give the date of birth as YYYY-MM-DD.")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM staff WHERE LOWER(email) = ? AND staff_id <> ?",
                            (email.lower(), me["staff_id"])).fetchone():
                raise BankError("Another staff member already uses this email.")
            conn.execute("UPDATE staff SET full_name = ?, email = ?, phone = ?, designation = ?, address = ?, dob = ?, "
                         "emergency_contact = ? WHERE staff_id = ?",
                         (name, email, phone, designation, address, dob, emergency, me["staff_id"]))
            if photo is not None:
                conn.execute("UPDATE staff SET photo = ? WHERE staff_id = ?", (photo, me["staff_id"]))
            self.audit(conn, me, "Own details changed", me["staff_id"])

    def set_own_photo(self, me, photo_b64):
        with self._connect() as conn:
            conn.execute("UPDATE staff SET photo = ? WHERE staff_id = ?", (photo_b64 or "", me["staff_id"]))
            self.audit(conn, me, "Own photo changed" if photo_b64 else "Own photo removed", me["staff_id"])

    def update_staff_details(self, by, sid, form):
        """An administrator (staff.manage) changes another person's details, also access and the extra fields."""
        target = self.get_staff(sid)
        if target is None:
            raise BankError("Staff member not found.")
        role = form.get("role") or target["role"]
        if role not in ("admin", "staff"):
            raise BankError("Unknown access level.")
        if sid == by["staff_id"] and role != by["role"]:
            raise BankError("You cannot change your own access level.")
        if role == "admin" and by.get("role") != "admin":
            raise BankError("Only an administrator can make another administrator.")
        if target["role"] == "admin" and role != "admin":
            with self._connect() as conn:
                if conn.execute("SELECT COUNT(*) FROM staff WHERE role = 'admin' AND active = 1").fetchone()[0] <= 1:
                    raise BankError("The bank needs at least one administrator.")
        self.update_staff(by, sid, form.get("full_name"), form.get("email"), form.get("phone"), form.get("designation"), role)
        dob = (form.get("dob") or "").strip()
        if dob and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            raise BankError("Please give the date of birth as YYYY-MM-DD.")
        with self._connect() as conn:
            conn.execute("UPDATE staff SET address = ?, dob = ?, emergency_contact = ?, branch = ? WHERE staff_id = ?",
                         (" ".join((form.get("address") or "").split())[:200], dob,
                          " ".join((form.get("emergency_contact") or "").split())[:100],
                          " ".join((form.get("branch") or "").split())[:80], sid))
