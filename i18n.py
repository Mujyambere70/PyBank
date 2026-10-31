"""
i18n.py - the interface in several languages.

The pages are written in English. After a page is made, every piece of visible text (and the
placeholder / title / alt texts) is looked up in translations/<lang>.json and replaced.
Text that is not in the catalog (names, amounts, account numbers) stays as it is.

A catalog entry is either exact            "Fund Transfer": "Перевод средств"
or a pattern with numbered gaps            "Request {0} for {1} was sent ...": "Заявка {0} на {1} отправлена ..."
The text that fills a gap is translated too when the catalog knows it (e.g. "{0} | PyBank").
"""

import html
import json
import os
import re
from functools import lru_cache

import world

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_DIR = os.path.join(HERE, "translations")
SKIP_TAGS = {"script", "style", "textarea", "code", "pre"}
TEXT_ATTRS = ("placeholder", "title", "alt", "aria-label", "data-tr")
_TOKEN = re.compile(r"(<!--.*?-->|<[^>]+>)", re.S)
_CODE = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>)", re.S | re.I)
_ATTR = re.compile(r'\b(placeholder|title|alt|aria-label|data-tr)="([^"]*)"')
_BUTTON_VALUE = re.compile(r'(<input\b[^>]*\btype="(?:submit|button)"[^>]*\bvalue=")([^"]*)(")', re.I)


@lru_cache(maxsize=None)
def catalog(lang):
    """(exact translations, [(regex, translation)]) of one language."""
    path = os.path.join(CATALOG_DIR, f"{lang}.json")
    if lang == "en" or not os.path.exists(path):
        return {}, []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    exact, patterns = {}, []
    for key, value in data.items():
        if not value:
            continue
        if re.search(r"\{\d\}", key):
            rx = "^" + re.sub(r"\\\{(\d)\\\}", r"(?P<g\1>.+?)", re.escape(key)) + "$"
            patterns.append((re.compile(rx, re.S), value))
        else:
            exact[key] = value
    patterns.sort(key=lambda p: -len(p[0].pattern))          # longest (most specific) first
    return exact, patterns


def translate_text(text, lang=None):
    """Translate one piece of plain text (also used for alerts, emails and PDF statements)."""
    lang = lang or world.current_lang()
    if lang == "en" or not text or not text.strip():
        return text
    exact, patterns = catalog(lang)
    lead = text[:len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    key = " ".join(text.split())
    if key in exact:
        return lead + exact[key] + tail
    for rx, value in patterns:
        m = rx.match(key)
        if m:
            out = value
            for name, part in m.groupdict().items():
                out = out.replace("{" + name[1:] + "}", exact.get(part, part))
            return lead + out + tail
    return text


def _translate_attr(m, lang):
    value = html.unescape(m.group(2))
    return f'{m.group(1)}="{html.escape(translate_text(value, lang), quote=True)}"'


def translate_html(page, lang):
    """Translate the visible text of a whole HTML page."""
    if lang == "en":
        return page
    exact, patterns = catalog(lang)
    if not exact and not patterns:
        return page
    # scripts and styles are copied as they are (a "<" inside JavaScript must not look like a tag)
    pieces = _CODE.split(page)
    return "".join(p if _CODE.fullmatch(p) else _translate_markup(p, lang) for p in pieces)


def _translate_markup(page, lang):
    out, skip = [], None
    for part in _TOKEN.split(page):
        if not part:
            continue
        if part.startswith("<"):
            if part.startswith("<!--"):
                out.append(part)
                continue
            tag = re.match(r"</?\s*([a-zA-Z0-9]+)", part)
            name = tag.group(1).lower() if tag else ""
            if skip:
                if part.startswith("</") and name == skip:
                    skip = None
                out.append(part)
                continue
            if name in SKIP_TAGS and not part.startswith("</") and not part.endswith("/>"):
                skip = name
            if 'translate="no"' in part and not part.startswith("</") and name not in ("input", "img", "br"):
                skip = name                                         # e.g. <span translate="no">
            part = _ATTR.sub(lambda m: _translate_attr(m, lang), part)
            part = _BUTTON_VALUE.sub(lambda m: m.group(1) + html.escape(
                translate_text(html.unescape(m.group(2)), lang), quote=True) + m.group(3), part)
            out.append(part)
        elif skip:
            out.append(part)
        else:
            plain = html.unescape(part)
            done = translate_text(plain, lang)
            out.append(part if done == plain else html.escape(done, quote=False))
    return "".join(out)
