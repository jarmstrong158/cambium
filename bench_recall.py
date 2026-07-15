#!/usr/bin/env python3
"""Recall quality benchmark for cambium's lexical scorer.

Purpose: answer "is it time to add semantic (embedding) recall yet?" with data
instead of vibes. The current scorer (_score in cambium_server.py) is pure token
overlap: deterministic, dependency-free. Semantic recall would cost a dependency
and the "zero-dependency" promise, so we only pay that price once the data proves
lexical is actually dropping right answers.

This script runs a fixed query set (control queries that share vocabulary with an
item, plus paraphrase queries that deliberately use DIFFERENT words) and reports:
  - control top-1 accuracy
  - paraphrase top-1 accuracy   <- the synonym-gap signal
  - min gold score              <- how close the correct item gets to the floor
  - floor-abstention failures   <- gold item pushed BELOW RELEVANCE_FLOOR (the
                                    real "lexical is failing" trigger)

TRIGGER TO REVISIT SEMANTIC RECALL (recorded in context-keeper 2026-07-12):
  Re-run this when the knowledge base crosses ~100 items. Build hybrid embeddings
  only if EITHER paraphrase top-1 drops below ~0.70 OR any gold item falls below
  the floor (a true recall miss, not a rank-2 near-miss). Homonym collisions
  (e.g. two items both about "sync") are NOT a reason to add embeddings -- they
  are disambiguation failures embeddings don't reliably fix; fix the tags instead.

Usage:
  PYTHONIOENCODING=utf-8 python bench_recall.py [path/to/knowledge.json]
Exit code is non-zero if any gold item falls below the floor (CI-friendly).
"""
import json
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_KB = os.path.join(HERE, "..", "knowledge", "knowledge.json")

# (query, correct item index by CONTENT match, is_paraphrase)
# Paraphrase queries intentionally avoid the item's own vocabulary -- they are the
# semantic-gap probes. Control queries reuse item vocabulary as a sanity baseline.
# NOTE: indices assume the current knowledge.json ordering. If items are added or
# reordered, re-map `gold` by matching on a distinctive content phrase, or set
# gold to the item id and look it up. Kept index-based here for a stable snapshot.
TESTS = [
    ("keep the decision log and the machine store in sync", 0, False),
    ("why is my changelog out of sync with the code", 0, True),
    ("mark a regime boundary when metric math changes", 1, False),
    ("my chart trend line looks wrong after i changed how i count", 1, True),
    ("agentsync unicode encoding error charmap codec", 2, False),
    ("coordination tool crashed on a special character", 2, True),
    ("check_conflicts false positive empty files", 3, False),
    ("the merge collision it reported might not be real", 3, True),
    ("back up a checkpoint before a destructive change", 4, False),
    ("how do i avoid losing model progress on a crash", 4, True),
    ("subprocess scripts need explicit timeouts", 5, False),
    ("my script hangs forever spawning a child process", 5, True),
    ("hook scripts ascii only no unicode in print", 6, False),
    ("windows hook fails on a fancy dash in output", 6, True),
    ("mcp schema verbosity token tax per session", 7, False),
    ("my server eats too many tokens at startup", 7, True),
    ("dual write local canonical mirror offline first", 8, False),
    ("sync state to a remote but keep working when offline", 8, True),
]


def load_scorer():
    spec = importlib.util.spec_from_file_location(
        "cs", os.path.join(HERE, "cambium_server.py"))
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    return cs


def main():
    kb_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KB
    cs = load_scorer()
    floor = cs.RELEVANCE_FLOOR
    items = json.load(open(kb_path, encoding="utf-8"))["items"]

    def rank(q):
        qt = cs._tokens(q)
        return sorted(((cs._score(it, qt), i) for i, it in enumerate(items)),
                      reverse=True)

    ctrl_hit = ctrl_n = syn_hit = syn_n = 0
    below_floor = []
    min_gold = 1.0
    print("mark  top  gold  kind  query")
    for q, gold, is_syn in TESTS:
        scored = rank(q)
        top_s, top_i = scored[0]
        gold_s = dict((i, s) for s, i in scored).get(gold, 0.0)
        min_gold = min(min_gold, gold_s)
        ok = (top_i == gold and top_s >= floor)
        if gold_s < floor:
            below_floor.append((q, gold_s))
        if is_syn:
            syn_n += 1
            syn_hit += ok
        else:
            ctrl_n += 1
            ctrl_hit += ok
        print(f"{'OK ' if ok else 'MISS'} {top_s:5.2f} {gold_s:5.2f} "
              f"{'syn ' if is_syn else 'ctrl'} {q}")

    syn_rate = syn_hit / syn_n if syn_n else 0.0
    print()
    print(f"items in KB:            {len(items)}")
    print(f"control top-1:          {ctrl_hit}/{ctrl_n}")
    print(f"paraphrase top-1:       {syn_hit}/{syn_n}  ({syn_rate:.0%})")
    print(f"min gold score:         {min_gold:.2f}  (floor={floor})")
    print(f"gold below floor:       {len(below_floor)}  <- real recall misses")

    trigger = []
    if len(items) >= 100:
        trigger.append(f"KB has {len(items)} items (>=100 re-eval threshold)")
    if syn_rate < 0.70:
        trigger.append(f"paraphrase top-1 {syn_rate:.0%} < 70%")
    if below_floor:
        trigger.append(f"{len(below_floor)} gold item(s) below floor")

    print()
    if trigger:
        print("VERDICT: consider building hybrid embedding recall --")
        for t in trigger:
            print(f"  - {t}")
    else:
        print("VERDICT: lexical is sufficient. Do NOT add semantic recall yet.")

    # non-zero exit only on a true recall miss (gold below floor)
    sys.exit(1 if below_floor else 0)


if __name__ == "__main__":
    main()
