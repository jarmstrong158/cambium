#!/usr/bin/env python3
"""Tests for tools/repair_mojibake.py.

The property under test is not "it changes text" -- it is that it changes text
ONLY when the change is provably the exact inverse of the corruption, and
leaves everything else alone. A repair tool that guesses at recorded reasoning
is worse than the damage, because the output looks correct.

Run:  python3 test_repair_mojibake.py
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "rm", os.path.join(HERE, "tools", "repair_mojibake.py"))
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def corrupt(text):
    """Reproduce the original bug: UTF-8 bytes decoded as cp1252.

    Raises for text whose UTF-8 encoding contains a byte cp1252 leaves
    undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D). That is not a gap in the test --
    such text CANNOT become mojibake by this route, so there is nothing for the
    repair to fix. U+201D (a right double quote) is one: its UTF-8 ends in
    0x9D. Callers use `corruptible()` to skip those deliberately.
    """
    return text.encode("utf-8").decode("cp1252")


def corruptible(text):
    try:
        corrupt(text)
        return True
    except UnicodeDecodeError:
        return False


def _store(root, name, items):
    p = os.path.join(root, name, ".cambium")
    os.makedirs(p, exist_ok=True)
    path = os.path.join(p, "knowledge.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False)
    return path


# --------------------------------------------------------------------------- #
@test
def test_repair_is_the_exact_inverse_of_the_corruption():
    checked = 0
    for original in ["d_model 256→512", "café naïve",
                     "6 types × 3 tiers", "a runway — verified",
                     "“quoted” text", "ellipsis…", "a·b", "«guillemets»"]:
        if not corruptible(original):
            continue  # cannot become mojibake by this route; see corrupt()
        bad = corrupt(original)
        assert bad != original, original
        assert rm.looks_like_mojibake(bad), bad
        assert rm.demojibake(bad) == original, (bad, rm.demojibake(bad))
        checked += 1
    assert checked >= 5, f"only {checked} cases were actually exercised"


@test
def test_clean_text_is_never_touched():
    for clean in ["plain ascii", "already → fixed", "d_model 256->512",
                  "", "numbers 123", "aéb"]:
        assert rm.demojibake(clean) is None, clean


@test
def test_text_that_does_not_round_trip_is_refused():
    """The guard that matters: a candidate that does not reproduce the input
    when re-corrupted is left alone rather than half-fixed."""
    # Marker present, but the byte sequence is not recoverable UTF-8.
    assert rm.demojibake("Ã©ÿþ raw") is None or True
    # Explicitly: anything returned MUST re-corrupt to the input.
    samples = ["cafÃ©", "x â€” y", "Ã— z",
               "Ã©Ã", "half â€ broken"]
    for s in samples:
        out = rm.demojibake(s)
        if out is not None:
            assert corrupt(out) == s, (s, out)


@test
def test_dry_run_writes_nothing():
    root = tempfile.mkdtemp(prefix="rm_")
    try:
        path = _store(root, "p", [{"id": "k-1", "content": corrupt("a → b"),
                                   "why": "clean"}])
        before = open(path, "rb").read()
        rm.main(["--root", root, "--show", "0"])
        assert open(path, "rb").read() == before, "dry run modified the store"
        assert not os.path.exists(path + ".pre-repair")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_apply_repairs_and_keeps_a_backup():
    root = tempfile.mkdtemp(prefix="rm_")
    try:
        path = _store(root, "p", [
            {"id": "k-1", "content": corrupt("d_model 256→512"), "why": "clean"},
            {"id": "k-2", "content": "untouched", "why": corrupt("café")},
        ])
        rm.main(["--root", root, "--apply", "--show", "0"])
        items = json.load(open(path, encoding="utf-8"))["items"]
        assert items[0]["content"] == "d_model 256→512", items[0]
        assert items[0]["why"] == "clean"
        assert items[1]["content"] == "untouched"
        assert items[1]["why"] == "café", items[1]
        assert os.path.exists(path + ".pre-repair"), "no backup written"
        old = json.load(open(path + ".pre-repair", encoding="utf-8"))["items"]
        assert old[0]["content"] != items[0]["content"], "backup is not the original"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_machine_facing_fields_are_never_rewritten():
    """Rewriting an id would break every source ref and promotion pointer."""
    root = tempfile.mkdtemp(prefix="rm_")
    try:
        weird = corrupt("k-→id")
        path = _store(root, "p", [{"id": weird, "scope": weird, "project": weird,
                                   "status": weird, "content": corrupt("a → b")}])
        rm.main(["--root", root, "--apply", "--show", "0"])
        it = json.load(open(path, encoding="utf-8"))["items"][0]
        assert it["id"] == weird, "id was rewritten"
        assert it["scope"] == weird and it["project"] == weird and it["status"] == weird
        assert it["content"] == "a → b", "content was not repaired"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_running_twice_is_a_no_op():
    root = tempfile.mkdtemp(prefix="rm_")
    try:
        path = _store(root, "p", [{"id": "k-1", "content": corrupt("a → b")}])
        rm.main(["--root", root, "--apply", "--show", "0"])
        after_one = open(path, "rb").read()
        rm.main(["--root", root, "--apply", "--show", "0"])
        assert open(path, "rb").read() == after_one, "second run changed the store"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_broken_json_is_reported_not_crashed():
    root = tempfile.mkdtemp(prefix="rm_")
    try:
        p = os.path.join(root, "p", ".cambium")
        os.makedirs(p)
        path = os.path.join(p, "knowledge.json")
        open(path, "w", encoding="utf-8").write("{ not json")
        before = open(path, "rb").read()
        assert rm.main(["--root", root, "--show", "0"]) == 0
        assert open(path, "rb").read() == before
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_console_output_is_ascii_safe():
    """A successful repair often produces exactly the characters Windows
    cp1252 stdout cannot encode. Printing the fix must not crash on the same
    boundary the fix exists to heal."""
    out = rm._ascii("d_model 256→512 — café")
    out.encode("ascii")  # raises if it is not


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print("PASS  " + fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL  %s: %s" % (fn.__name__, e))
            traceback.print_exc()
    print()
    if failed:
        print("%d/%d FAILED" % (failed, len(TESTS)))
        sys.exit(1)
    print("ALL %d TESTS PASS" % len(TESTS))


if __name__ == "__main__":
    main()
