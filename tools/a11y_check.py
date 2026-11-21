"""
tools/a11y_check.py - accessibility audit of PyBank with axe-core (version 4).

    npm pack axe-core@4.10.2 && tar xzf axe-core-4.10.2.tgz         (once, outside the project)
    python tools/a11y_check.py path/to/package/axe.min.js [report.json]

It starts the bank on a free local port with the demo data, opens the pages below in Chromium (Playwright) as a
visitor, a customer and the administrator, runs axe with the WCAG 2.1 A and AA rules and prints the problems of
every page. The exit code is 0 only when no page has a problem.
"""

import json
import logging
import os
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)
os.environ.pop("DATABASE_URL", None)
logging.disable(logging.CRITICAL)

PAGES = [("guest", u) for u in ("/", "/about", "/rates", "/faq", "/contact", "/status", "/developers",
                                "/page/privacy", "/page/terms", "/login", "/open-account")] + \
        [("customer", u) for u in ("/netbanking", "/netbanking/accounts", "/netbanking/card", "/netbanking/bills",
                                   "/netbanking/insights", "/netbanking/security", "/netbanking/e-statements",
                                   "/netbanking/transfer", "/netbanking/transfer/other-bank",
                                   "/netbanking/fixed-deposits", "/netbanking/statement", "/netbanking/profile")] + \
        [("admin", u) for u in ("/admin", "/admin/customers", "/admin/customers/PB10000001", "/admin/applications",
                                "/admin/content", "/admin/content/pages/about", "/admin/content/items/faq",
                                "/admin/content/items/branch", "/admin/content/items/banner",
                                "/admin/content/items/product", "/admin/content/items/charge",
                                "/admin/content/translate/faq/1", "/admin/media", "/admin/content/billers",
                                "/admin/payments/iso", "/admin/compliance", "/admin/compliance/1", "/admin/dormant",
                                "/admin/checker", "/admin/health", "/admin/staff", "/admin/settings",
                                "/admin/fx/history", "/admin/notices", "/admin/backups", "/admin/audit")]
USERS = {"customer": ("/login", "PB10000001", "Demo@1234"), "admin": ("/admin/login", "EMP001", "Admin@1234")}


def no_net():
    raise OSError("no internet")


def main(axe_path, out=None):
    from werkzeug.serving import make_server
    from playwright.sync_api import sync_playwright
    from app import create_app
    app = create_app(os.path.join(tempfile.mkdtemp(), "a11y.db"), housekeeping=False, fx_fetch=no_net)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    axe = open(axe_path, encoding="utf-8").read()
    report, bad = {}, 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pages = {}
        for who in ("guest", "customer", "admin"):
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            if who in USERS:
                url, login, password = USERS[who]
                page.goto(base + url)
                page.fill("#login", login)
                page.fill("#password", password)
                with page.expect_navigation():
                    page.press("#password", "Enter")
            pages[who] = page
        for who, url in PAGES:
            page = pages[who]
            page.goto(base + url)
            page.wait_for_load_state("networkidle")
            page.add_script_tag(content=axe)
            res = page.evaluate("""async () => (await axe.run(document, {runOnly: {type: 'tag',
                values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}})).violations.map(v => ({id: v.id,
                impact: v.impact, help: v.help, nodes: v.nodes.length,
                where: v.nodes.slice(0, 3).map(n => n.target.join(' '))}))""")
            report[url] = res
            bad += bool(res)
            print(f"{url:40} {len(res)} problems", "; ".join(f"{v['id']}({v['nodes']}) {v['where']}" for v in res))
        browser.close()
    srv.shutdown()
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    print(f"\n{len(PAGES)} pages checked, {bad} pages with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
