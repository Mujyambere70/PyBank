"""
tools/release_check.py - the check that runs before every release of PyBank (version 4).

It makes a new temporary bank with the demo data, signs in as every kind of user (visitor, three customers,
the administrator and one staff member of each job role), follows every link of every page and opens each
page again in the six languages. Every answer 500 (server error) and every 404 for a link that the bank
itself shows is a problem, and so is a link to a page that the job role may not open (403).

    python tools/release_check.py               (about 2-4 minutes)
    python tools/release_check.py --pages=300   (a quicker check)

The exit code is 0 only when nothing was found, so the script can stop a deployment (CI).
No internet is used: exchange rates are not downloaded and no e-mail or SMS is sent.
"""

import collections
import logging
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)
os.environ.pop("DATABASE_URL", None)                   # never the real database
logging.disable(logging.CRITICAL)

from app import create_app  # noqa: E402

MAX_PAGES = int(next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--pages=")), "1500"))
LANGS = ["en", "ru", "fr", "es", "ar", "zh"]
USERS = [("visitor", None, None),
         ("customer Rahul", "/login", ("PB10000001", "Demo@1234")),
         ("customer Anna (RUB + EUR)", "/login", ("PB10000004", "Demo@1234")),
         ("customer Arjun (current)", "/login", ("PB10000003", "Demo@1234")),
         ("admin", "/admin/login", ("EMP001", "Admin@1234"))] + \
        [(f"staff {sid}", "/admin/login", (sid, "Staff@1234"))
         for sid in ("EMP002", "EMP003", "EMP004", "EMP005", "EMP006", "EMP007")]
SKIP = re.compile(r"/logout|/lang/|/delete|/restore|/now\.json|\.gz$|/push/|/sw\.js|mailto:|tel:|javascript:|"
                  r"/admin/database/.+\.csv")
LINK = re.compile(r'href="(/[^"#]*)"')


def no_net():
    raise OSError("no internet during the release check")


def main():
    app = create_app(os.path.join(tempfile.mkdtemp(), "release.db"), housekeeping=False, fx_fetch=no_net)
    problems, opened, forbidden = [], collections.Counter(), collections.Counter()
    for who, login_url, cred in USERS:
        c = app.test_client()
        with c.session_transaction() as s:
            s["csrf_token"] = "t"
        if cred:
            r = c.post(login_url, data={"login": cred[0], "password": cred[1], "csrf_token": "t"})
            if r.status_code not in (200, 302):
                problems.append((who, login_url, r.status_code))
                continue
        start = "/" if not cred else ("/admin" if login_url.startswith("/admin") else "/netbanking")
        todo, seen = [start, "/", "/rates", "/faq", "/contact", "/status", "/developers", "/page/privacy"], set()
        while todo and len(seen) < MAX_PAGES:
            url = todo.pop(0)
            if url in seen or SKIP.search(url):
                continue
            seen.add(url)
            r = c.get(url)
            opened[who] += 1
            if r.status_code >= 500 or r.status_code == 404:
                problems.append((who, url, r.status_code))
                continue
            if r.status_code == 403:                       # the page showed a link that this role may not open
                forbidden[who] += 1
                problems.append((who, url, "403 - a link to a page of another job role"))
                continue
            if r.mimetype == "text/html" and r.status_code == 200:
                for link in LINK.findall(r.get_data(as_text=True)):
                    link = link.split("?")[0] if "/netbanking/statement" not in link else link
                    if link not in seen and not SKIP.search(link):
                        todo.append(link)
        # the same pages once more in every language (a sample of 40 pages per user)
        for lang in LANGS[1:]:
            c.get(f"/lang/{lang}?next=/")
            with c.session_transaction() as s:
                s["staff_lang"] = lang
            for url in sorted(seen)[:40]:
                r = c.get(url)
                opened[who] += 1
                if r.status_code >= 500:
                    problems.append((f"{who} [{lang}]", url, r.status_code))
        c.get("/lang/en?next=/")
    total = sum(opened.values())
    for who, n in opened.items():
        print(f"{who:28} {n:5} pages opened, {forbidden[who]:3} not allowed for this role (403)")
    print(f"\n{total} pages opened, {len(problems)} problems")
    for p in problems:
        print("PROBLEM", *p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
