"""
tools/build_catalogs.py - add the translations of tools/tr/*.tsv to translations/<lang>.json.

Each line of a .tsv file: English <TAB> Russian <TAB> French <TAB> Spanish <TAB> Arabic <TAB> Chinese.
A text with {0}, {1} ... is a pattern (see i18n.py). Existing entries are kept; a line in a .tsv
file replaces an older translation of the same English text.

    python tools/build_catalogs.py
"""

import glob
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGS = ["ru", "fr", "es", "ar", "zh"]


def main():
    cats = {}
    for lang in LANGS:
        with open(os.path.join(HERE, "translations", f"{lang}.json"), encoding="utf-8") as f:
            cats[lang] = json.load(f)
    added = 0
    for path in sorted(glob.glob(os.path.join(HERE, "tools", "tr", "*.tsv"))):
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 6:
                raise SystemExit(f"{os.path.basename(path)} line {n}: {len(parts)} columns instead of 6")
            en, *tr = [" ".join(p.split()) for p in parts]
            holes = sorted(set(re.findall(r"\{\d\}", en)))
            for lang, text in zip(LANGS, tr):
                if sorted(set(re.findall(r"\{\d\}", text))) != holes:
                    raise SystemExit(f"{os.path.basename(path)} line {n} ({lang}): the {{0}} gaps differ")
                if cats[lang].get(en) != text:
                    added += lang == "ru"
                cats[lang][en] = text
    for lang in LANGS:
        with open(os.path.join(HERE, "translations", f"{lang}.json"), "w", encoding="utf-8") as f:
            json.dump(dict(sorted(cats[lang].items())), f, ensure_ascii=False, indent=1)
    print(f"{added} texts added or changed; {len(cats['ru'])} texts in each language")


if __name__ == "__main__":
    main()
