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

Local stores (`.cambium/knowledge.json`) by default. Team scope lives on a
`cambium` git BRANCH per repo and needs `--team`, because writing it rewrites
state other machines pull -- a maintenance script should not do that because
it happened to be run. With `--team --apply` each repair is committed and
pushed on its own commit, through a throwaway worktree so your checkout is
never touched.

Team scope is where this matters most: the corruption is concentrated on the
branches that get recalled, so those items are returned garbled hundreds of
times while the local copies nobody reads were the clean ones.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_ROOT = os.path.dirname(REPO)

# Prose fields on a cambium item. Deliberately excludes id/scope/project/
# status/timestamps: those are machine-facing, and rewriting an id would
# break every source ref and promotion pointer aimed at it.
TEXT_FIELDS = ("content", "why", "example", "deprecated_reason")

from _mojibake import (  # noqa: E402
    MARKERS as _MARKERS,
    ascii_safe as _ascii,
    demojibake,
    looks_like_mojibake,
)


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


TEAM_BRANCH = "cambium"
TEAM_FILE = "knowledge.json"


def _git(args, cwd, check=True):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, timeout=90)
    if check and r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args),
                                           r.stderr.decode("utf-8", "replace")[:300]))
    return r


def find_team_repos(roots):
    """(name, repo_dir) for every git repo carrying a team knowledge branch."""
    out = []
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            repo = os.path.join(root, name)
            if not os.path.isdir(os.path.join(repo, ".git")):
                continue
            r = _git(["show", "%s:%s" % (TEAM_BRANCH, TEAM_FILE)], repo, check=False)
            if r.returncode != 0:
                r = _git(["show", "origin/%s:%s" % (TEAM_BRANCH, TEAM_FILE)],
                         repo, check=False)
            if r.returncode == 0 and r.stdout:
                out.append((name, repo))
    return out


def scan_team(repo):
    """(data, repairs, unrepairable) for a repo's team branch, read-only.

    origin FIRST. The local branch can lag behind the shared ref -- including
    behind a push this very script made on a previous run -- and scanning the
    stale copy reports work that is already done, or misses work that is not.
    """
    for ref in ("origin/" + TEAM_BRANCH, TEAM_BRANCH):
        r = _git(["show", "%s:%s" % (ref, TEAM_FILE)], repo, check=False)
        if r.returncode == 0 and r.stdout:
            try:
                data = json.loads(r.stdout.decode("utf-8"))
            except Exception as e:
                return None, [], [("<branch>", str(e))]
            repairs = []
            for item in data.get("items", []):
                fixes = scan_item(item)
                if fixes:
                    repairs.append((item, fixes))
            return data, repairs, []
    return None, [], []


def repair_team(name, repo, apply_):
    """Repair the team branch through a throwaway worktree.

    A worktree, not a checkout: your working tree is never touched, so this
    cannot disturb whatever you had open. Fetches first and branches from the
    REMOTE tip so a stale local ref cannot silently revert a peer's push.
    """
    data, repairs, bad = scan_team(repo)
    if data is None:
        return 0, 0, bad
    fields = sum(len(f) for _i, f in repairs)
    if not apply_ or not repairs:
        return len(repairs), fields, bad

    wt = tempfile.mkdtemp(prefix="cambium-repair-")
    try:
        _git(["fetch", "origin", TEAM_BRANCH], repo, check=False)
        base = ("origin/" + TEAM_BRANCH
                if _git(["rev-parse", "--verify", "origin/" + TEAM_BRANCH],
                        repo, check=False).returncode == 0 else TEAM_BRANCH)
        _git(["worktree", "add", "--detach", wt, base], repo)
        path = os.path.join(wt, TEAM_FILE)
        with open(path, "r", encoding="utf-8") as f:
            fresh = json.load(f)
        n = 0
        for item in fresh.get("items", []):
            for field, _before, after in scan_item(item):
                item[field] = after
                n += 1
        if not n:
            return 0, 0, bad          # someone repaired it between scan and write
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fresh, f, ensure_ascii=False, indent=2)
        _git(["add", TEAM_FILE], wt)
        message = "\n".join([
            "cambium: repair cp1252 mojibake in %d field(s)" % n,
            "",
            "Exact-inverse repair (encode cp1252, decode utf-8), verified per",
            "field by re-applying the corruption. Fields that did not",
            "round-trip were left untouched.",
        ])
        _git(["commit", "-m", message], wt)
        push = _git(["push", "origin", "HEAD:%s" % TEAM_BRANCH], wt, check=False)
        if push.returncode != 0:
            # An archived repo, a protected branch, a peer who pushed first --
            # all real and none of them a reason to abandon the other repos.
            # Report it and move on; the commit dies with the worktree, so a
            # failed push leaves the branch exactly as it was.
            why = push.stderr.decode("utf-8", "replace").strip().splitlines()
            detail = next((l for l in why if "remote:" in l or "fatal:" in l),
                          why[-1] if why else "push failed")
            return 0, 0, bad + [("<push>", detail.replace("remote:", "").strip())]
        return len(repairs), n, bad
    finally:
        _git(["worktree", "remove", "--force", wt], repo, check=False)
        shutil.rmtree(wt, ignore_errors=True)


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
    ap.add_argument("--team", action="store_true",
                    help="Also repair team-scope knowledge on each repo's "
                         "`cambium` branch. With --apply this COMMITS AND "
                         "PUSHES to a shared branch.")
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

    if args.team:
        print()
        print("%-24s %8s %8s   (team branch)" % ("project", "items", "fields"))
        print("-" * 56)
        t_i = t_f = 0
        for name, repo in find_team_repos(roots):
            n_i, n_f, bad = repair_team(name, repo, args.apply)
            all_unrepairable += [(name,) + b for b in bad]
            if n_i:
                print("%-24s %8d %8d" % (name, n_i, n_f))
                t_i += n_i
                t_f += n_f
        print("-" * 56)
        print("%-24s %8d %8d" % ("TEAM TOTAL", t_i, t_f))
        if t_f and args.apply:
            print("Committed and pushed to the `cambium` branch of each repo above.")
        tot_items += t_i
        tot_fields += t_f

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
