"""
abilities.py - the administrator manages every ability of every staff member (version 6).

In version 5 a person's abilities came from his job role; single abilities could be changed only on a
"role / yes / no" page that was hard to find. Version 6 makes abilities the main tool:

  * Person > Abilities: every ability is a tick box. Tick = the person has it, no tick = he has not,
    whatever his role says. PyBank remembers the difference from the role (added / taken away), so a
    later change of the role still works.
  * Ability matrix: all staff members against all abilities on one page, tick boxes everywhere.
  * Copy the abilities of one person to another (a new teller gets what the best teller has).
  * Temporary ability: given until a date, then it ends by itself (cover for a colleague on leave).
  * Own abilities: the administrator creates a new ability (for example "Night shift approvals"),
    gives it a group and a description, and decides which back-office pages need it.
  * Page rules: for any back-office page the administrator can choose the ability it needs.
    His rules are checked before the bank's own list. A page can never be opened to everybody by a
    rule - a rule always names an ability.

Every change is written in the audit log. Several web workers stay in step through the setting
"abilities_version": a worker that sees a newer number loads the abilities again.
"""

import re
from datetime import date

import roles
from core import BankError, now

ABILITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_abilities (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    grp         TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ability_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern    TEXT NOT NULL,
    method     TEXT NOT NULL DEFAULT 'ANY',
    ability    TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ability_grants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id   TEXT NOT NULL,
    ability    TEXT NOT NULL,
    until      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    granted_by TEXT NOT NULL DEFAULT '',
    granted_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);
"""
PATTERN_RE = re.compile(r"^/admin(/[A-Za-z0-9_.*<>-]+)*$")
# pages that must keep their own ability, so nobody can lock the administrator's tools away by mistake
PROTECTED = ("/admin/abilities", "/admin/staff", "/admin/roles", "/admin/login", "/admin/logout")
NEW_IN_V6 = {"officer": ["counter.cheque", "cheques.clear"], "teller": ["counter.cheque"],
             "loan_officer": ["credit.decide"], "content_editor": ["assistant.train"]}


def join_codes(codes):
    return ",".join(sorted(c for c in set(codes) if c in roles.ABILITIES))


class AbilityAdminMixin:
    """Mixed into the back-office class (Office), next to StaffPermMixin."""

    _abilities_version = None

    # ===== loading =====

    def load_abilities(self):
        with self._connect() as conn:
            customs = {r["code"]: {"label": r["label"], "group": r["grp"], "description": r["description"]}
                       for r in conn.execute("SELECT * FROM custom_abilities")}
            rules = [(("POST:" if r["method"] == "POST" else "") + r["pattern"], r["ability"])
                     for r in conn.execute("SELECT * FROM ability_rules ORDER BY LENGTH(pattern) DESC, id")]
            v = conn.execute("SELECT value FROM settings WHERE key = 'abilities_version'").fetchone()
        roles.load_custom(customs, rules)
        self._abilities_version = v[0] if v else "0"

    def refresh_abilities(self):
        """Called before back-office requests: load again when another worker changed something."""
        with self._connect() as conn:
            v = conn.execute("SELECT value FROM settings WHERE key = 'abilities_version'").fetchone()
        if (v[0] if v else "0") != self._abilities_version:
            self.load_abilities()

    def _bump(self, conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('abilities_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now() + "." + str(id(conn) % 997),))

    def add_v6_role_abilities(self):
        """The starting roles get the new abilities of version 6 once (an older database had v5 roles)."""
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM settings WHERE key = 'roles_v6'").fetchone():
                return
            for code, extra in NEW_IN_V6.items():
                r = conn.execute("SELECT abilities FROM job_roles WHERE code = ?", (code,)).fetchone()
                if r is not None:
                    conn.execute("UPDATE job_roles SET abilities = ? WHERE code = ?",
                                 (join_codes(roles.split_codes(r[0]) | set(extra)), code))
            r = conn.execute("SELECT abilities FROM job_roles WHERE code = 'branch_manager'").fetchone()
            if r is not None:
                new = {a for a in roles.BUILTIN if a not in roles._BRANCH_MANAGER_NOT}
                conn.execute("UPDATE job_roles SET abilities = ? WHERE code = 'branch_manager'",
                             (join_codes(roles.split_codes(r[0]) | new),))
            conn.execute("INSERT INTO settings (key, value) VALUES ('roles_v6', ?)", (now(),))

    # ===== the administrator's own abilities =====

    def custom_abilities(self):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM custom_abilities ORDER BY grp, label")]
            rules = [dict(r) for r in conn.execute("SELECT * FROM ability_rules")]
        for r in rows:
            r["pages"] = [x for x in rules if x["ability"] == r["code"]]
        return rows

    def _clean_ability(self, label, group, description):
        label = " ".join((label or "").split())[:60]
        if len(label) < 3:
            raise BankError("Please give the ability a name (at least 3 letters).")
        return label, " ".join((group or "").split())[:40], " ".join((description or "").split())[:200]

    def create_ability(self, by, label, group="", description="", pages=()):
        label, group, description = self._clean_ability(label, group, description)
        base = roles.ABILITY_PREFIX + (re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30] or "ability")
        with self._connect() as conn:
            if any(info.lower() == label.lower() for info in roles.ABILITIES.values()) or conn.execute(
                    "SELECT 1 FROM custom_abilities WHERE LOWER(label) = ?", (label.lower(),)).fetchone():
                raise BankError("An ability with this name already exists.")
            code, n = base, 2
            while conn.execute("SELECT 1 FROM custom_abilities WHERE code = ?", (code,)).fetchone():
                code, n = f"{base}_{n}", n + 1
            conn.execute("INSERT INTO custom_abilities (code, label, grp, description, created_by, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (code, label, group, description, by["staff_id"], now()))
            self.audit(conn, by, "Ability created", code, f"{label} ({group or 'Own abilities'})")
            self._bump(conn)
        self.load_abilities()
        for p in pages:
            if p.strip():
                self.add_rule(by, p, code)
        return code

    def update_ability(self, by, code, label, group="", description=""):
        label, group, description = self._clean_ability(label, group, description)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM custom_abilities WHERE LOWER(label) = ? AND code <> ?",
                            (label.lower(), code)).fetchone():
                raise BankError("An ability with this name already exists.")
            if conn.execute("UPDATE custom_abilities SET label = ?, grp = ?, description = ? WHERE code = ?",
                            (label, group, description, code)).rowcount == 0:
                raise BankError("Only the abilities you created can be changed here.")
            self.audit(conn, by, "Ability changed", code, label)
            self._bump(conn)
        self.load_abilities()

    def delete_ability(self, by, code):
        """Remove an own ability everywhere: roles, people, temporary grants and page rules."""
        if code in roles.BUILTIN:
            raise BankError("The bank's own abilities cannot be deleted.")
        with self._connect() as conn:
            if conn.execute("DELETE FROM custom_abilities WHERE code = ?", (code,)).rowcount == 0:
                raise BankError("Ability not found.")
            conn.execute("DELETE FROM ability_rules WHERE ability = ?", (code,))
            conn.execute("DELETE FROM ability_grants WHERE ability = ?", (code,))
            for table, key, cols in (("job_roles", "code", ("abilities",)),
                                     ("staff", "staff_id", ("extra_abilities", "denied_abilities"))):
                for row in conn.execute(f"SELECT {key}, {', '.join(cols)} FROM {table}").fetchall():
                    for i, col in enumerate(cols, 1):
                        codes = [c for c in (row[i] or "").split(",") if c and c != code]
                        if len(codes) != len([c for c in (row[i] or "").split(",") if c]):
                            conn.execute(f"UPDATE {table} SET {col} = ? WHERE {key} = ?", (",".join(sorted(codes)), row[0]))
            self.audit(conn, by, "Ability deleted", code)
            self._bump(conn)
        self.load_abilities()

    # ===== page rules =====

    def rules(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM ability_rules ORDER BY pattern")]

    def add_rule(self, by, pattern, ability, method="ANY", note=""):
        pattern = (pattern or "").strip().rstrip("/")
        method = "POST" if (method or "").upper() == "POST" else "ANY"
        if not PATTERN_RE.match(pattern) or pattern == "/admin" or "/." in pattern:
            raise BankError("Write a back-office address, for example /admin/loans or /admin/customers/*/freeze.")
        if any(pattern == p or pattern.startswith(p + "/") for p in PROTECTED):
            raise BankError("This page keeps its own ability, so that nobody locks the staff tools away.")
        if ability not in roles.ABILITIES:
            raise BankError("Please choose an ability for this page.")
        with self._connect() as conn:
            conn.execute("DELETE FROM ability_rules WHERE pattern = ? AND method = ?", (pattern, method))
            rid = conn.execute("INSERT INTO ability_rules (pattern, method, ability, note, created_by, created_at) "
                               "VALUES (?, ?, ?, ?, ?, ?)", (pattern, method, ability, (note or "")[:120],
                                                             by["staff_id"], now())).lastrowid
            self.audit(conn, by, "Page rule set", pattern, f"{method} -> {ability}")
            self._bump(conn)
        self.load_abilities()
        return rid

    def delete_rule(self, by, rule_id):
        with self._connect() as conn:
            r = conn.execute("SELECT pattern, ability FROM ability_rules WHERE id = ?", (rule_id,)).fetchone()
            if r is None:
                raise BankError("Rule not found.")
            conn.execute("DELETE FROM ability_rules WHERE id = ?", (rule_id,))
            self.audit(conn, by, "Page rule removed", r[0], r[1])
            self._bump(conn)
        self.load_abilities()

    # ===== one person: tick boxes =====

    def role_base(self, job):
        with self._connect() as conn:
            r = conn.execute("SELECT abilities FROM job_roles WHERE code = ?", (job or "officer",)).fetchone()
        return roles.split_codes(r[0]) if r else roles.default_abilities(job)

    def _check_target(self, by, sid):
        target = self.get_staff(sid)
        if target is None:
            raise BankError("Staff member not found.")
        if sid == by["staff_id"]:
            raise BankError("You cannot change your own abilities. Ask another administrator.")
        return target

    def set_person_abilities(self, by, sid, checked, job=None):
        """The person gets exactly the ticked abilities (and the role, if given)."""
        target = self._check_target(by, sid)
        job = job or target.get("job_role") or "officer"
        unknown = [c for c in checked if c not in roles.ABILITIES]
        if unknown:
            raise BankError("Unknown ability: " + ", ".join(unknown[:3]))
        base, want = self.role_base(job), set(checked)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM job_roles WHERE code = ?", (job,)).fetchone() is None:
                raise BankError("Unknown job role.")
            conn.execute("UPDATE staff SET job_role = ?, extra_abilities = ?, denied_abilities = ? WHERE staff_id = ?",
                         (job, join_codes(want - base), join_codes(base - want), sid))
            added = sorted(want - (target.get("_abilities") or set()))
            removed = sorted((target.get("_abilities") or set()) - want - self._grants_of(conn, sid))
            self.audit(conn, by, "Abilities changed", sid,
                       f"role {job}; added {', '.join(added) or '-'}; removed {', '.join(removed) or '-'}")
        return want

    def give_ability(self, by, sid, code):
        target = self._check_target(by, sid)
        return self.set_person_abilities(by, sid, (target["_abilities"] - self.active_grants(sid)) | {code})

    def take_ability(self, by, sid, code):
        target = self._check_target(by, sid)
        with self._connect() as conn:
            conn.execute("UPDATE ability_grants SET revoked = 1 WHERE staff_id = ? AND ability = ?", (sid, code))
        return self.set_person_abilities(by, sid, (target["_abilities"] - self.active_grants(sid)) - {code})

    def reset_to_role(self, by, sid):
        target = self._check_target(by, sid)
        with self._connect() as conn:
            conn.execute("UPDATE staff SET extra_abilities = '', denied_abilities = '' WHERE staff_id = ?", (sid,))
            self.audit(conn, by, "Abilities reset to the role", sid, target.get("job_role") or "")

    def copy_abilities(self, by, from_sid, to_sid):
        src = self.get_staff(from_sid)
        if src is None:
            raise BankError("Staff member not found.")
        if src.get("role") == "admin":
            raise BankError("An administrator has every ability; choose a staff member to copy from.")
        target = self._check_target(by, to_sid)
        if target.get("role") == "admin":
            raise BankError("An administrator already has every ability.")
        want = src["_abilities"] - self.active_grants(from_sid)
        self.set_person_abilities(by, to_sid, want, src.get("job_role"))
        with self._connect() as conn:
            self.audit(conn, by, "Abilities copied", to_sid, f"from {from_sid}")
        return want

    def save_matrix(self, by, form):
        """Ability matrix: fields m:<sid>:<code> for ticked boxes; 'rows' lists the people on the page."""
        rows = form.getlist("rows") if hasattr(form, "getlist") else form.get("rows", [])
        changed = 0
        for sid in rows:
            s = self.get_staff(sid)
            if s is None or s.get("role") == "admin" or sid == by["staff_id"]:
                continue
            want = {c for c in roles.ABILITIES if form.get(f"m:{sid}:{c}")}
            if want != s["_abilities"] - self.active_grants(sid):
                self.set_person_abilities(by, sid, want)
                changed += 1
        return changed

    def who_has(self, code):
        return [s for s in self.staff_list() if code in (s.get("_abilities") or set()) and s.get("active", 1)]

    # ===== temporary abilities =====

    def _grants_of(self, conn, sid, today=None):
        today = (today or date.today()).isoformat()
        return {r[0] for r in conn.execute("SELECT ability FROM ability_grants WHERE staff_id = ? AND revoked = 0 "
                                           "AND until >= ?", (sid, today))}

    def active_grants(self, sid):
        with self._connect() as conn:
            return self._grants_of(conn, sid)

    def grants(self, sid=None):
        sql, args = "SELECT * FROM ability_grants WHERE revoked = 0", []
        if sid:
            sql += " AND staff_id = ?"
            args.append(sid)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql + " ORDER BY until", args)]
        for r in rows:
            r["label"] = roles.ABILITIES.get(r["ability"], r["ability"])
            r["expired"] = r["until"] < date.today().isoformat()
        return rows

    def grant_temporary(self, by, sid, code, until, reason=""):
        self._check_target(by, sid)
        if code not in roles.ABILITIES:
            raise BankError("Please choose an ability.")
        try:
            end = date.fromisoformat((until or "").strip())
        except ValueError:
            raise BankError("Please choose the last day (YYYY-MM-DD).")
        if end < date.today():
            raise BankError("The last day must be today or later.")
        if (end - date.today()).days > 366:
            raise BankError("A temporary ability can last at most one year.")
        reason = " ".join((reason or "").split())[:120]
        if len(reason) < 3:
            raise BankError("Please write why (for example: cover for Ravi on leave).")
        with self._connect() as conn:
            gid = conn.execute("INSERT INTO ability_grants (staff_id, ability, until, reason, granted_by, granted_at) "
                               "VALUES (?, ?, ?, ?, ?, ?)", (sid, code, end.isoformat(), reason, by["staff_id"],
                                                             now())).lastrowid
            self.audit(conn, by, "Temporary ability given", sid, f"{code} until {end}: {reason}")
        return gid

    def revoke_grant(self, by, gid):
        with self._connect() as conn:
            r = conn.execute("SELECT staff_id, ability FROM ability_grants WHERE id = ?", (gid,)).fetchone()
            if r is None:
                raise BankError("Not found.")
            conn.execute("UPDATE ability_grants SET revoked = 1 WHERE id = ?", (gid,))
            self.audit(conn, by, "Temporary ability ended", r[0], r[1])

    def ability_history(self, sid=None, limit=60):
        sql = ("SELECT * FROM audit_log WHERE (action LIKE 'Abilit%' OR action LIKE 'Temporary ability%' "
               "OR action LIKE 'Job role%' OR action LIKE 'Page rule%')")
        args = []
        if sid:
            sql += " AND target = ?"
            args.append(sid)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + f" ORDER BY id DESC LIMIT {int(limit)}", args)]
