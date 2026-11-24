"""
roles.py - what each bank worker may do in the back office.

Version 4 had six fixed job roles written in this file. Version 5 keeps them only as the starting
point: the roles now live in the table `job_roles`, and the administrator can

  * change the abilities and the name of every role, or add new roles (Back office > Staff & roles),
  * give one person extra abilities or take some away from him, whatever his role is,
  * give one person his own amount limit for counter work (above it a second person must approve).

An administrator (staff.role = 'admin') can always do everything. Every back-office address is matched
to one ability (ABILITY_FOR_PATH); a person without it gets "403 Forbidden". A test checks that no
back-office address is left without an ability, except the few that every signed-in worker needs.
"""

import fnmatch

ABILITY_GROUPS = [
    ("Customers", [
        ("customers.view", "See customers, accounts and documents"),
        ("customers.edit", "Open accounts at the branch and change KYC data"),
        ("customers.security", "Unlock logins, reset passwords, PINs and two-factor login"),
        ("customers.status", "Freeze or activate accounts, change the account type"),
        ("customers.delete", "Close and delete a customer (balance must be zero)"),
        ("applications", "Approve new accounts and changes of personal data (KYC, MRZ check)"),
        ("requests.answer", "Answer service requests and card disputes"),
    ]),
    ("Counter (cash and payments for customers)", [
        ("counter.deposit", "Cash deposit at the counter"),
        ("counter.withdraw", "Cash withdrawal at the counter"),
        ("counter.transfer", "Transfer to a PyBank account for a customer"),
        ("counter.external", "Payment to another bank for a customer"),
        ("counter.fx", "Exchange between a customer's own currency accounts"),
        ("counter.deposits", "Open and close fixed and recurring deposits for a customer"),
        ("counter.loans", "Take loan EMI payments at the counter"),
        ("till.own", "Open and close one's own cash drawer"),
        ("till.supervise", "See every cash drawer and accept a difference when it is closed"),
    ]),
    ("Money", [
        ("transactions.view", "Search all transactions"),
        ("transactions.reverse", "Reverse a wrong transaction"),
        ("entries.manual", "Manual credit or debit (charges, corrections)"),
        ("cash.approve", "Approve cash requests made in Net Banking"),
        ("checker", "Second approval of large entries (four-eyes rule)"),
        ("loans.decide", "Approve or reject loan applications"),
        ("payments.view", "Payments to other banks and ISO 20022 files"),
    ]),
    ("Risk", [
        ("compliance", "Suspicious-activity flags and dormant accounts"),
        ("screening", "Sanctions screening and customer risk ratings"),
    ]),
    ("Website", [
        ("content.manage", "Website content: contacts, pages, FAQ, products, branches, banners, charges, billers"),
        ("notices.manage", "Notices on the website and in Net Banking"),
    ]),
    ("Administration", [
        ("reports.view", "Reports and the health of the system"),
        ("audit.view", "Read the audit log"),
        ("staff.manage", "Add and change staff members, their details and their abilities"),
        ("roles.manage", "Create and change job roles"),
        ("settings.manage", "Bank settings: limits, interest rates, exchange rates"),
        ("openbanking.manage", "Open banking: register partner apps, see consents"),
        ("data.manage", "Data manager: change records of any table (money records stay read-only)"),
        ("backups", "Backups, restore test and restore"),
        ("database", "Read-only database viewer"),
    ]),
]
ABILITIES = {code: text for _group, items in ABILITY_GROUPS for code, text in items}

# The starting roles (version 4 roles + the counter abilities of version 5). They are copied into the
# table job_roles once; after that the administrator changes them there.
_BRANCH_MANAGER_NOT = ("staff.manage", "roles.manage", "backups", "database", "data.manage", "customers.delete",
                       "openbanking.manage")
DEFAULT_ROLES = {
    "officer": {"label": "Customer service officer",
                "abilities": ["customers.view", "customers.edit", "customers.security", "customers.status",
                              "transactions.view", "transactions.reverse", "entries.manual", "applications",
                              "cash.approve", "loans.decide", "payments.view", "requests.answer",
                              "counter.transfer", "counter.external", "counter.fx", "counter.deposits", "counter.loans"]},
    "teller": {"label": "Teller (cash counter)",
               "abilities": ["customers.view", "transactions.view", "cash.approve", "requests.answer",
                             "counter.deposit", "counter.withdraw", "counter.transfer", "counter.external",
                             "counter.fx", "counter.deposits", "counter.loans", "till.own"]},
    "loan_officer": {"label": "Loan officer",
                     "abilities": ["customers.view", "transactions.view", "loans.decide", "applications",
                                   "counter.loans"]},
    "compliance": {"label": "Compliance officer",
                   "abilities": ["customers.view", "transactions.view", "applications", "payments.view", "compliance",
                                 "screening", "audit.view", "reports.view"]},
    "content_editor": {"label": "Website editor", "abilities": ["content.manage", "notices.manage"]},
    "branch_manager": {"label": "Branch manager",
                       "abilities": [a for a in ABILITIES if a not in _BRANCH_MANAGER_NOT]},
}
ROLES = DEFAULT_ROLES                 # kept for older code; the live roles come from the database
JOB_ROLES = list(DEFAULT_ROLES)

# address of a back-office page -> the ability it needs (the first matching line wins).
# "POST:" lines are used only for form posts; "*" matches one part of the address.
ABILITY_FOR_PATH = [
    ("/admin/login", None), ("/admin/logout", None), ("/admin/profile", None), ("/admin/lang", None),
    ("/admin/search", None), ("/admin/assistant", None), ("/admin/suggest", None),   # results follow the abilities
    ("/admin/name-enquiry", "customers.view"),
    ("/admin/applications", "applications"), ("/admin/profile-changes", "applications"),
    ("/admin/mrz", "applications"),
    ("/admin/documents", "customers.view"),
    ("/admin/payments", "payments.view"),
    ("POST:/admin/customers/*/delete", "customers.delete"),
    ("POST:/admin/customers/*/unlock", "customers.security"), ("POST:/admin/customers/*/reset2fa", "customers.security"),
    ("POST:/admin/customers/*/password", "customers.security"), ("POST:/admin/customers/*/pin", "customers.security"),
    ("POST:/admin/customers/*/freeze", "customers.status"), ("POST:/admin/customers/*/activate", "customers.status"),
    ("POST:/admin/customers/*/type", "customers.status"),
    ("POST:/admin/customers/*/entry", "entries.manual"),
    ("POST:/admin/customers/*/screen", "screening"), ("POST:/admin/customers/*/risk", "screening"),
    ("POST:/admin/customers", "customers.edit"), ("/admin/customers/new", "customers.edit"),
    ("/admin/customers", "customers.view"),
    ("POST:/admin/transactions", "transactions.reverse"), ("/admin/transactions", "transactions.view"),
    ("/admin/requests", "requests.answer"), ("/admin/disputes", "requests.answer"),
    ("/admin/approvals", "cash.approve"),
    ("/admin/loans", "loans.decide"),
    ("/admin/checker", "checker"),
    ("POST:/admin/counter/*/deposit", "counter.deposit"), ("POST:/admin/counter/*/withdraw", "counter.withdraw"),
    ("POST:/admin/counter/*/transfer", "counter.transfer"), ("POST:/admin/counter/*/external", "counter.external"),
    ("POST:/admin/counter/*/fx", "counter.fx"), ("POST:/admin/counter/*/fd", "counter.deposits"),
    ("POST:/admin/counter/*/rd", "counter.deposits"), ("POST:/admin/counter/*/emi", "counter.loans"),
    ("/admin/counter", "customers.view"),
    ("/admin/receipt", "transactions.view"),
    ("/admin/tills", "till.supervise"),
    ("/admin/till", "till.own"),
    ("/admin/compliance", "compliance"), ("/admin/dormant", "compliance"),
    ("/admin/screening", "screening"),
    ("/admin/content", "content.manage"), ("/admin/media", "content.manage"),
    ("/admin/notices", "notices.manage"),
    ("/admin/reports", "reports.view"), ("/admin/health", "reports.view"),
    ("/admin/audit", "audit.view"),
    ("/admin/roles", "roles.manage"),
    ("/admin/staff", "staff.manage"),
    ("/admin/settings", "settings.manage"), ("/admin/interest", "settings.manage"), ("/admin/fx", "settings.manage"),
    ("/admin/openbanking", "openbanking.manage"),
    ("/admin/data", "data.manage"),
    ("/admin/backups", "backups"),
    ("/admin/database", "database"),
    ("/admin", None),                       # the dashboard: everybody who is signed in
]
OPEN_PATHS = {"/admin", "/admin/login", "/admin/login/verify", "/admin/logout", "/admin/profile", "/admin/lang/<code>",
              "/admin/search", "/admin/assistant", "/admin/suggest",
              "/admin/staff-photo/<sid>.jpg", "/admin/profile/photo"}     # own photo and colleagues' pictures


def default_abilities(job):
    return set(DEFAULT_ROLES.get(job or "officer", DEFAULT_ROLES["officer"])["abilities"])


def split_codes(text):
    return {c for c in (text or "").replace(" ", "").split(",") if c in ABILITIES}


def abilities_of(staff):
    """Set of abilities of a staff row (dict). Rows loaded by the back office carry `_abilities`
    (role from the database + extra - taken away); without it the starting roles are used."""
    if not staff:
        return set()
    if staff.get("role") == "admin":
        return set(ABILITIES)
    if "_abilities" in staff:
        return set(staff["_abilities"])
    return (default_abilities(staff.get("job_role")) | split_codes(staff.get("extra_abilities"))) \
        - split_codes(staff.get("denied_abilities"))


def can(staff, ability):
    return ability is None or ability in abilities_of(staff)


def _match(pattern, path):
    if "*" in pattern:
        return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern + "/*")
    return path == pattern or path.startswith(pattern.rstrip("/") + "/") or path.startswith(pattern + ".")


def ability_for(path, method="GET"):
    """The ability needed for a back-office address; None = any signed-in staff member."""
    for prefix, ability in ABILITY_FOR_PATH:
        post_only = prefix.startswith("POST:")
        p = prefix[5:] if post_only else prefix
        if post_only and method != "POST":
            continue
        if _match(p, path):
            return ability
    return None


def job_label(staff):
    if staff.get("role") == "admin":
        return "Administrator"
    if staff.get("_job_label"):
        return staff["_job_label"]
    return DEFAULT_ROLES.get(staff.get("job_role") or "officer", DEFAULT_ROLES["officer"])["label"]
