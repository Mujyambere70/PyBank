"""
roles.py - what each kind of bank worker may do in the back office (version 4).

Until version 3 there were only two roles: "admin" (everything) and "staff" (everything except the
administration pages). A real branch splits the work: a teller handles cash, a loan officer decides
loans, a compliance officer looks at suspicious payments, a web editor changes the website.

The `staff.role` column keeps its two values (admin / staff). The new column `staff.job_role` says
which job a "staff" person has; its abilities come from ROLES below. An admin can do everything.
Every back-office address is matched to one ability (ABILITY_FOR_PATH); a person without that
ability gets "403 Forbidden".
"""

ABILITIES = {
    "customers.view": "See customers, accounts and documents",
    "customers.edit": "Open accounts at the branch, change KYC data, unlock, reset, freeze, manual entries",
    "transactions.view": "Search all transactions",
    "transactions.reverse": "Reverse a wrong transaction",
    "applications": "Approve new accounts and changes of personal data (KYC, MRZ check)",
    "cash.approve": "Approve cash deposits and withdrawals",
    "loans.decide": "Approve or reject loan applications",
    "payments.view": "Payments to other banks and ISO 20022 files",
    "requests.answer": "Answer service requests and card disputes",
    "checker": "Second approval of large manual entries and reversals (four-eyes rule)",
    "compliance": "Suspicious-activity flags and dormant accounts",
    "content.manage": "Website content: contacts, pages, FAQ, products, branches, banners, charges, billers",
    "notices.manage": "Notices on the website and in Net Banking",
    "reports.view": "Reports and the health of the system",
    "audit.view": "Read the audit log",
    "staff.manage": "Add and change staff members and their roles",
    "settings.manage": "Bank settings: limits, interest rates, exchange rates",
    "backups": "Backups, restore test and restore",
    "database": "Read-only database viewer",
}

ROLES = {
    "officer": {"label": "Customer service officer",
                "abilities": ["customers.view", "customers.edit", "transactions.view", "transactions.reverse",
                              "applications", "cash.approve", "loans.decide", "payments.view", "requests.answer"]},
    "teller": {"label": "Teller (cash counter)",
               "abilities": ["customers.view", "transactions.view", "cash.approve", "requests.answer"]},
    "loan_officer": {"label": "Loan officer",
                     "abilities": ["customers.view", "transactions.view", "loans.decide", "applications"]},
    "compliance": {"label": "Compliance officer",
                   "abilities": ["customers.view", "transactions.view", "applications", "payments.view", "compliance",
                                 "audit.view", "reports.view"]},
    "content_editor": {"label": "Website editor", "abilities": ["content.manage", "notices.manage"]},
    "branch_manager": {"label": "Branch manager",
                       "abilities": [a for a in ABILITIES if a not in ("staff.manage", "backups", "database")]},
}
JOB_ROLES = list(ROLES)

# address of a back-office page -> the ability it needs (the first matching line wins).
# "POST:" lines are used only for form posts; other lines for every method.
ABILITY_FOR_PATH = [
    ("/admin/login", None), ("/admin/logout", None), ("/admin/profile", None), ("/admin/lang", None),
    ("/admin/applications", "applications"), ("/admin/profile-changes", "applications"),
    ("/admin/mrz", "applications"),
    ("/admin/documents", "customers.view"),
    ("/admin/payments", "payments.view"),
    ("POST:/admin/customers", "customers.edit"), ("/admin/customers/new", "customers.edit"),
    ("/admin/customers", "customers.view"),
    ("POST:/admin/transactions", "transactions.reverse"), ("/admin/transactions", "transactions.view"),
    ("/admin/requests", "requests.answer"), ("/admin/disputes", "requests.answer"),
    ("/admin/approvals", "cash.approve"),
    ("/admin/loans", "loans.decide"),
    ("/admin/checker", "checker"),
    ("/admin/compliance", "compliance"), ("/admin/dormant", "compliance"),
    ("/admin/content", "content.manage"), ("/admin/media", "content.manage"),
    ("/admin/notices", "notices.manage"),
    ("/admin/reports", "reports.view"), ("/admin/health", "reports.view"),
    ("/admin/audit", "audit.view"),
    ("/admin/staff", "staff.manage"),
    ("/admin/settings", "settings.manage"), ("/admin/interest", "settings.manage"), ("/admin/fx", "settings.manage"),
    ("/admin/backups", "backups"),
    ("/admin/database", "database"),
    ("/admin", None),                       # the dashboard: everybody who is signed in
]


def abilities_of(staff):
    """Set of abilities of a staff row (dict)."""
    if not staff:
        return set()
    if staff.get("role") == "admin":
        return set(ABILITIES)
    job = ROLES.get(staff.get("job_role") or "officer", ROLES["officer"])
    return set(job["abilities"])


def can(staff, ability):
    return ability is None or ability in abilities_of(staff)


def ability_for(path, method="GET"):
    """The ability needed for a back-office address; None = any signed-in staff member."""
    for prefix, ability in ABILITY_FOR_PATH:
        post_only = prefix.startswith("POST:")
        p = prefix[5:] if post_only else prefix
        if post_only and method != "POST":
            continue
        if path == p or path.startswith(p.rstrip("/") + "/") or path.startswith(p + "."):
            return ability
    return None


def job_label(staff):
    if staff.get("role") == "admin":
        return "Administrator"
    return ROLES.get(staff.get("job_role") or "officer", ROLES["officer"])["label"]
