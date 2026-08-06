#!/usr/bin/env python3
"""Repair cp1252-misdecoded UTF-8 in distilled knowledge.

    python tools/repair_mojibake.py                 # dry run, reports only
    python tools/repair_mojibake.py --apply         # write the repairs
    python tools/repair_mojibake.py --root D:/code --apply

WHY

context-keeper fixed the CAUSE (it forces UTF-8 on the stdio transport) and
repaired its own stores. cambium distilled from those stores BEFORE the repair
and kept the corrupted copies, so the damage outlived its source: 89 fields
across 71 items on this machine, every one of them recalled as-is with no
indication anything is wrong. `tools/dashboard.py` made the scale visible;
this closes it.

WHY IT IS A SCRIPT AND NOT A TOOL

A one-time migration does not belong in tools/list. Every connected client
loads every tool description at every session start, so a 17th tool would tax
each session forever to fix something once (the same reasoning as
context-keeper's con-004, where repair_mojibake is likewise a hidden handler
rather than a listed tool).

THE REPAIR IS AN EXACT INVERSE, AND IS VERIFIED AS ONE

Mojibake here is UTF-8 bytes decoded as cp1252, so the repair is
`encode("cp1252").decode("utf-8")`. Every candidate is checked by re-applying
the corruption: if that does not reproduce the input byte for byte, the field
is LEFT ALONE. An approximate repair of someone's recorded reasoning is worse
than legible damage, because it looks correct. Ported verbatim from
context-keeper's mojibake.py rather than reimplemented -- the same bug fixed
twice, two different ways, is how the two copies come to disagree.

SCOPE

Local stores only (`.cambium/knowledge.json` per project). Team and org scopes
live on shared git branches; this reports what it can see there and refuses to
rewrite shared history from a maintenance script.
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_ROOT = os.path.dirname(REPO)

# Prose fields on a cambium item. Deliberately excludes id/scope/project/
# status/timestamps: those are machine-facing, and rewriting an id would
# break every source ref and promotion pointer aimed at it.
TEXT_FIELDS = ("content", "why", "example", "deprecated_reason")

# The signatures of UTF-8 read as cp1252. Same list context-keeper uses.
_MARKERS = (
    "\u00c3\u00a9", "\u00c3\u00a8", "\u00c3\u00a0", "\u00c3\u00b4",
    "\u00c3\u00bc", "\u00c3\u00b6", "\u00c3\u00a4", "\u00c3\u00b1",
    "\u00e2\u20ac\u2122", "\u00e2\u20ac\u201c", "\u00e2\u20ac\u201d",
    "\u00e2\u20ac\u0153", "\u00e2\u20ac\u009d", "\u00e2\u20ac\u00a6",
    "\u00e2\u20ac\u02dc", "\u00c3\u2014", "\u00c2\u00a0", "\u00c2\u00b7",
    "\u00c2\u00ab", "\u00c2\u00bb", "\u00e2\u2020\u2019",
)


def _ascii(text):
    """Console-safe. Windows stdout is cp1252, and a SUCCESSFUL repair very
    often produces exactly the characters cp1252 cannot encode -- an arrow, an
    em-dash, a curly quote. Printing the fix would then crash on the same
    encoding boundary the fix exists to heal."""
    return str(text).encode("ascii", "replace").decode("ascii")


def looks_like_mojibake(text):
    return isinstance(text, str) and any(m in text for m in _MARKERS)


def demojibake(text):
    """Repaired text, or None when this is not recoverable cp1252 damage.

    Verified as an exact inverse: re-applying the corruption to the candidate
    must reproduce the input byte for byte.
    """
    if not looks_like_mojibake(text):
        return None
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if repaired == text:
        return None
    try:
        if repaired.encode("utf-8").decode("cp1252") != text:
            return None
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return repaired


def scan_item(item):
    """[(field, before, after)] for every exactly-repairable field."""
    out = []
    for f in TEXT_FIELDS:
        v = item.get(f)
        if not isinstance(v, str):
            continue
        fixed = demojibake(v)
        if fixed is not None:
            out.append((f, v, fixed))
    return out


def scan_store(path):
    """(items, repairs, unrepairable) for one knowledge.json."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        return None, [], [("<file>", str(e))]
    items = (data or {}).get("items", [])
    repairs, unrepairable = [], []
    for item in items:
        fixes = scan_item(item)
        if fixes:
            repairs.append((item, fixes))
        else:
            for f in TEXT_FIELDS:
                v = item.get(f)
                if isinstance(v, str) and looks_like_mojibake(v):
                    unrepairable.append((item.get("id", "?"), f))
    return data, repairs, unrepairable


def repair_store(path, apply_):
    data, repairs, unrepairable = scan_store(path)
    if data is None:
        return 0, 0, unrepairable
    fields = sum(len(f) for _i, f in repairs)
    if apply_ and repairs:
        for item, fixes in repairs:
            for field, _before, after in fixes:
                item[field] = after
        shutil.copy2(path, path + ".pre-repair")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return len(repairs), fields, unrepairable


def find_stores(roots):
    out = []
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name, ".cambium", "knowledge.json")
            if os.path.exists(p):
                out.append((name, p))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="Write the repairs. Without this, reports only.")
    ap.add_argument("--show", type=int, default=3,
                    help="Sample repairs to print per project (default 3).")
    args = ap.parse_args(argv)
    roots = args.root or [DEFAULT_ROOT]

    stores = find_stores(roots)
    if not stores:
        print("no cambium stores found under: " + ", ".join(roots))
        return 0

    tot_items = tot_fields = 0
    all_unrepairable = []
    print("%-24s %8s %8s" % ("project", "items", "fields"))
    print("-" * 42)
    for name, path in stores:
        if args.apply:
            n_items, n_fields, bad = repair_store(path, True)
        else:
            _d, repairs, bad = scan_store(path)
            n_items, n_fields = len(repairs), sum(len(f) for _i, f in repairs)
            if repairs and args.show:
                pass
        all_unrepairable += [(name,) + b for b in bad]
        tot_items += n_items
        tot_fields += n_fields
        if n_items:
            print("%-24s %8d %8d" % (name, n_items, n_fields))
            if not args.apply and args.show:
                _d, repairs, _b = scan_store(path)
                for item, fixes in repairs[:args.show]:
                    field, before, after = fixes[0]
                    i = next((k for k, (a, b) in enumerate(zip(before, after)) if a != b), 0)
                    print("    %s.%s" % (item.get("id", "?"), field))
                    print("      - %s" % _ascii(before[max(0, i - 30):i + 40]).replace("\n", " "))
                    print("      + %s" % _ascii(after[max(0, i - 30):i + 40]).replace("\n", " "))

    print("-" * 42)
    print("%-24s %8d %8d" % ("TOTAL", tot_items, tot_fields))
    if all_unrepairable:
        print("\nNOT repairable (left untouched -- an approximate fix is worse "
              "than legible damage):")
        for row in all_unrepairable[:12]:
            print("   " + _ascii(" / ".join(str(r) for r in row)))
    if args.apply:
        print("\nApplied. Each modified store has a .pre-repair copy beside it.")
        print("Re-run tools/dashboard.py to confirm the counts dropped.")
    elif tot_fields:
        print("\nDRY RUN -- nothing written. Re-run with --apply to fix.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
