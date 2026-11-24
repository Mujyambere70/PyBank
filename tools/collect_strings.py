"""
tools/collect_strings.py - find English texts that have no translation yet.

It opens every page of a fresh demo bank (as a visitor, a customer and the administrator), cuts each
page into texts exactly like i18n.py does, and also reads the messages in the Python code
(BankError("..."), flash("...")). Every text that i18n cannot translate into the chosen language is
printed, one per line - ready to be translated and added with tools/build_catalogs.py.

    python tools/collect_strings.py --lang=ru > missing_ru.txt
"""

import html
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import i18n  # noqa: E402

SKIP = re.compile(r"^[\s\d.,:;%₹₽$€£¥/+\-–—()&#*·|@×→←▾✓✗★]*$|^PB\d|^5021|^EX\d|^CR\d|^FD\d|^LN|^[A-Z]{2,5}\d|"
                  r"^https?://|^/|^[\w.+-]+@[\w.-]+$")


def texts_of_page(page):
    out = []
    page = i18n._CODE.sub("", page)
    skip = None
    for part in i18n._TOKEN.split(page):
        if not part:
            continue
        if part.startswith("<"):
            if part.startswith("<!--"):
                continue
            tag = re.match(r"</?\s*([a-zA-Z0-9]+)", part)
            name = tag.group(1).lower() if tag else ""
            if skip:
                if part.startswith("</") and name == skip:
                    skip = None
                continue
            if name in i18n.SKIP_TAGS and not part.startswith("</") and not part.endswith("/>"):
                skip = name
            if 'translate="no"' in part and not part.startswith("</") and name not in ("input", "img", "br"):
                skip = name
            for m in i18n._ATTR.finditer(part):
                out.append(html.unescape(m.group(2)))
            for m in i18n._BUTTON_VALUE.finditer(part):
                out.append(html.unescape(m.group(2)))
        elif not skip:
            out.append(html.unescape(part))
    return [" ".join(t.split()) for t in out if t.strip()]


def code_messages():
    out = []
    rx = re.compile(r'(?:BankError|flash)\(\s*(f?)"((?:[^"\\]|\\.)+)"')
    for name in os.listdir(HERE):
        if name.endswith(".py"):
            src = open(os.path.join(HERE, name), encoding="utf-8").read()
            for f, text in rx.findall(src):
                if f:
                    text = re.sub(r"\{[^}]+\}", "{0}", text)
                    n = 0

                    def num(m):
                        nonlocal n
                        n += 1
                        return "{%d}" % (n - 1)
                    text = re.sub(r"\{0\}", num, text)
                out.append(text)
    return out


def crawl(lang):
    from app import create_app
    tmp = tempfile.mkdtemp()
    app = create_app(os.path.join(tmp, "c.db"), housekeeping=False)
    seen, found = set(), []

    def walk(client, start, prefix, limit=400):
        todo = list(start)
        while todo and len(seen) < 100000 and limit:
            url = todo.pop(0)
            if url in seen:
                continue
            seen.add(url)
            limit -= 1
            r = client.get(url)
            if r.status_code != 200 or r.mimetype != "text/html":
                continue
            page = r.get_data(as_text=True)
            found.extend(texts_of_page(page))
            for link in re.findall(r'href="(/[^"#?]*)', page):
                if link.startswith(prefix) and not link.startswith(("/admin/logout", "/logout", "/lang/")) \
                        and not re.search(r"\.(csv|pdf|json|gz|xml|png|jpg)$", link):
                    todo.append(link)

    c = app.test_client()
    walk(c, ["/", "/about", "/rates", "/faq", "/contact", "/status", "/developers", "/login", "/open-account",
              "/search?q=fixd+deposit"], "/", 80)
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf_token"] = "t"
    c.post("/login", data={"login": "PB10000001", "password": "Demo@1234", "csrf_token": "t"})
    walk(c, ["/netbanking", "/netbanking/savings", "/netbanking/connected-apps", "/netbanking/assistant",
             "/netbanking/search?q=swigy"], "/netbanking", 140)
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf_token"] = "t"
    c.post("/admin/login", data={"login": "admin@pybank.demo", "password": "Admin@1234", "csrf_token": "t"})
    walk(c, ["/admin", "/admin/counter", "/admin/counter/PB10000001", "/admin/till", "/admin/tills", "/admin/roles",
             "/admin/staff/EMP003/access", "/admin/screening", "/admin/openbanking", "/admin/data",
             "/admin/data/customers", "/admin/data/customers/PB10000001", "/admin/search?q=rahul+varma",
             "/admin/assistant"], "/admin", 340)
    return found


def main():
    lang = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--lang=")), "ru")
    texts = crawl(lang) + code_messages()
    missing, done = [], set()
    for t in texts:
        if t in done or SKIP.match(t) or len(t) < 2 or not re.search(r"[A-Za-z]{2}", t):
            continue
        done.add(t)
        if i18n.translate_text(t, lang) == t:
            missing.append(t)
    for t in missing:
        print(t)
    print(f"# {len(missing)} texts without a {lang} translation", file=sys.stderr)


if __name__ == "__main__":
    main()
