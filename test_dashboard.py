#!/usr/bin/env python3
"""Tests for tools/dashboard.py.

Runs against synthetic stores in a temp dir -- never the real ones, because a
tool whose whole job is reading your memory must be provably read-only.

Run:  python3 test_dashboard.py
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
    "dash", os.path.join(HERE, "tools", "dashboard.py"))
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _project(root, name, decisions=(), constraints=(), items=()):
    p = os.path.join(root, name)
    ctx = os.path.join(p, ".context")
    os.makedirs(ctx, exist_ok=True)
    json.dump(list(decisions), open(os.path.join(ctx, "decisions.json"), "w",
                                    encoding="utf-8"))
    json.dump(list(constraints), open(os.path.join(ctx, "constraints.json"), "w",
                                      encoding="utf-8"))
    if items:
        cam = os.path.join(p, ".cambium")
        os.makedirs(cam, exist_ok=True)
        json.dump({"items": list(items)},
                  open(os.path.join(cam, "knowledge.json"), "w", encoding="utf-8"))
    return p


def _dec(i, **kw):
    d = {"id": f"dec-{i:03d}", "summary": f"decision {i}", "why_chosen": "x" * 200,
         "tags": ["t"], "status": "active", "created_at": "2026-05-01T00:00:00+00:00",
         "verified_at": "2026-05-01T00:00:00+00:00"}
    d.update(kw)
    return d


# --------------------------------------------------------------------------- #
@test
def test_reads_both_stores_and_counts_them():
    root = tempfile.mkdtemp(prefix="dash_")
    try:
        _project(root, "alpha",
                 decisions=[_dec(1), _dec(2)],
                 constraints=[{"id": "con-001", "rule": "never x", "reason": "y" * 200,
                               "scope": "src/", "tags": ["t"], "status": "active"}],
                 items=[{"id": "k-1", "kind": "decision", "scope": "local",
                         "content": "c", "trust": {"recalls": 3}},
                        {"id": "k-2", "kind": "constraint", "scope": "local",
                         "content": "c", "trust": {"recalls": 0}}])
        projects = dash.discover([root])
        assert len(projects) == 1, projects
        s = projects[0]["stats"]
        assert s["entries"] == 3, s
        assert s["constraints"] == 1, s
        assert s["scoped"] == 1, s
        assert s["distilled"] == 2, s
        assert s["recalled"] == 1, s
        assert s["recalls"] == 3, s
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_never_writes_to_a_store():
    """The one property that matters most: reading your memory cannot change it."""
    root = tempfile.mkdtemp(prefix="dash_")
    try:
        p = _project(root, "alpha", decisions=[_dec(1)],
                     items=[{"id": "k-1", "kind": "decision", "content": "c",
                             "trust": {"recalls": 1}}])
        before = {}
        for dirpath, _d, files in os.walk(p):
            for f in files:
                fp = os.path.join(dirpath, f)
                before[fp] = (os.path.getmtime(fp), open(fp, "rb").read())
        out = os.path.join(root, "out.html")
        dash.build([root], out)
        after = {}
        for dirpath, _d, files in os.walk(p):
            for f in files:
                fp = os.path.join(dirpath, f)
                after[fp] = (os.path.getmtime(fp), open(fp, "rb").read())
        assert before == after, "the dashboard modified a store"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_supersession_links_are_counted():
    root = tempfile.mkdtemp(prefix="dash_")
    try:
        _project(root, "alpha", decisions=[
            _dec(1, status="superseded", superseded_by="dec-002"), _dec(2)])
        s = dash.discover([root])[0]["stats"]
        assert s["links"] == 1, s
        assert s["superseded"] == 1, s
        assert s["active"] == 1, s
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_flags_thin_untagged_and_garbled():
    root = tempfile.mkdtemp(prefix="dash_")
    try:
        _project(root, "alpha", decisions=[
            _dec(1, why_chosen="short"),                       # thin
            _dec(2, tags=[]),                                  # untagged
            _dec(3, summary="cafÃ© â€” x"),  # mojibake
        ])
        s = dash.discover([root])[0]["stats"]
        assert s["thin"] == 1, s
        assert s["notags"] == 1, s
        assert s["moji_ck"] == 1, s
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_empty_and_broken_stores_do_not_crash():
    """A half-written JSON file is a normal state for a live store."""
    root = tempfile.mkdtemp(prefix="dash_")
    try:
        p = _project(root, "alpha", decisions=[_dec(1)])
        open(os.path.join(p, ".context", "constraints.json"), "w",
             encoding="utf-8").write("{ this is not json")
        _project(root, "empty")
        os.makedirs(os.path.join(root, "not-a-project"), exist_ok=True)
        projects = dash.discover([root])
        names = [x["name"] for x in projects]
        assert names == ["alpha"], names          # empty + non-project excluded
        assert projects[0]["stats"]["entries"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_output_is_one_self_contained_file():
    """No CDN, no sibling assets -- it has to open from a file:// path."""
    root = tempfile.mkdtemp(prefix="dash_")
    try:
        _project(root, "alpha", decisions=[_dec(1)],
                 items=[{"id": "k-1", "kind": "decision", "content": "c",
                         "trust": {"recalls": 2}}])
        out = os.path.join(root, "d.html")
        payload, path = dash.build([root], out)
        doc = open(path, encoding="utf-8").read()
        assert doc.startswith("<!doctype html>")
        assert "/*DATA*/" not in doc, "data placeholder was not substituted"
        for bad in ("http://", "https://", "src=", "<link"):
            assert bad not in doc, f"external reference in output: {bad}"
        assert payload["totals"]["entries"] == 1
        assert payload["totals"]["recalls"] == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_embedded_json_survives_a_closing_script_tag():
    """Entry text is arbitrary prose and can contain </script>."""
    root = tempfile.mkdtemp(prefix="dash_")
    try:
        _project(root, "alpha",
                 decisions=[_dec(1, summary="beware </script> in text")])
        out = os.path.join(root, "d.html")
        dash.build([root], out)
        doc = open(out, encoding="utf-8").read()
        head, _sep, tail = doc.partition("const D = ")
        payload = tail[:tail.index(";\nconst $")]
        data = json.loads(payload.replace("<\\/", "</"))
        assert data["projects"][0]["entries"][0]["t"] == "beware </script> in text"
        # and the raw sequence never appears unescaped inside the script block
        assert "</script> in text" not in tail.split("</script>")[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
