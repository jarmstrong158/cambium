#!/usr/bin/env python3
"""
Integration test suite for the cambium MCP server.

Every test builds real git repositories (bare origins + clones) in a temp dir
and drives the actual tool functions. The agentsync substrate is exercised two
ways: fixtures that write the exact claims.json format to a real coordination
branch, and — when the real agentsync repo is present as a sibling — a full
integration test that drives agentsync's own claim/finish tools and then
distills from what they wrote. context-keeper interop uses real .context/
store files in its exact schema. The gh CLI is stubbed only where a test would
otherwise open a real pull request.

Run:  python3 test_cambium.py
(Requires git on PATH and `pip install mcp`.)
"""

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTSYNC_PATH = os.path.join(os.path.dirname(HERE), "agentsync",
                              "agentsync_server.py")

CAMBIUM_ENV = [
    "CAMBIUM_REPO", "CAMBIUM_AGENT_ID", "CAMBIUM_ORG_REPO", "CAMBIUM_ORG_PR",
    "CAMBIUM_PROMOTE_RECALLS", "CAMBIUM_TEAM_BRANCH", "CAMBIUM_AGENTSYNC_BRANCH",
    "CAMBIUM_RELEASE_CAPTURE", "CAMBIUM_CONFIG_FILE",
]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = load_module("cambium_server", os.path.join(HERE, "cambium_server.py"))


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def setup_lab(root, collaborators=("jonny", "stobie")):
    """Bare origin + one clone per collaborator, seeded with a README on main."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.io",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.io"}
    os.environ.update(env)
    origin = os.path.join(root, "origin.git")
    git(["init", "-q", "--bare", "-b", "main", origin], root)
    seed = os.path.join(root, "seed")
    git(["clone", "-q", origin, seed], root)
    with open(os.path.join(seed, "README.md"), "w") as f:
        f.write("# project\n")
    git(["add", "."], seed); git(["commit", "-qm", "init"], seed)
    git(["push", "-q", "origin", "main"], seed)
    clones = {}
    for who in collaborators:
        path = os.path.join(root, who)
        git(["clone", "-q", origin, path], root)
        git(["remote", "set-head", "origin", "main"], path)
        clones[who] = path
    return origin, clones


def setup_org_repo(root):
    """A dedicated org knowledge repo: bare origin + a cambium-managed clone."""
    bare = os.path.join(root, "org.git")
    git(["init", "-q", "--bare", "-b", "main", bare], root)
    seed = os.path.join(root, "org-seed")
    git(["clone", "-q", bare, seed], root)
    with open(os.path.join(seed, "knowledge.json"), "w") as f:
        json.dump({"items": []}, f)
    git(["add", "."], seed); git(["commit", "-qm", "init org knowledge"], seed)
    git(["push", "-q", "origin", "main"], seed)
    clone = os.path.join(root, "org-clone")
    git(["clone", "-q", bare, clone], root)
    git(["remote", "set-head", "origin", "main"], clone)
    return bare, clone


@contextlib.contextmanager
def lab(**kw):
    root = tempfile.mkdtemp(prefix="cambium_test_")
    try:
        origin, clones = setup_lab(root, **kw)
        yield root, origin, clones
    finally:
        shutil.rmtree(root, ignore_errors=True)


def be(clones, who, **extra):
    for k in CAMBIUM_ENV:
        os.environ.pop(k, None)
    os.environ["CAMBIUM_REPO"] = clones[who]
    os.environ["CAMBIUM_AGENT_ID"] = who
    # point the fallback config at a per-lab path that does not exist, so tests
    # never read (or write) the developer's real ~/.cambium/config.json
    os.environ["CAMBIUM_CONFIG_FILE"] = os.path.join(
        os.path.dirname(clones[who]), "cambium_test_config.json")
    for k, v in extra.items():
        os.environ[k] = v


def unconfigured(root, config_name="empty_config.json"):
    """Clear all cambium env and point the fallback config at a nonexistent file
    under `root` — cambium sees a completely cold, unconfigured start."""
    for k in CAMBIUM_ENV:
        os.environ.pop(k, None)
    path = os.path.join(root, config_name)
    os.environ["CAMBIUM_CONFIG_FILE"] = path
    return path


def seed_agentsync_branch(clone, claims):
    """Write claims.json to a real 'agentsync' coordination branch in the
    exact format agentsync's update_status('done') produces."""
    git(["checkout", "-qb", "agentsync", "main"], clone)
    with open(os.path.join(clone, "claims.json"), "w") as f:
        json.dump({"claims": claims}, f, indent=2)
    git(["add", "claims.json"], clone)
    git(["commit", "-qm", "agentsync: seed"], clone)
    git(["push", "-q", "origin", "agentsync"], clone)
    git(["checkout", "-q", "main"], clone)


def rewrite_agentsync_claims(clone, claims):
    """Rewrite claims.json on the existing 'agentsync' branch and push — the
    churn a live agentsync produces as claims are released (pop) or re-claimed
    (overwrite) under an agent id."""
    git(["fetch", "-q", "origin", "agentsync"], clone)
    git(["checkout", "-qB", "agentsync", "origin/agentsync"], clone)
    with open(os.path.join(clone, "claims.json"), "w") as f:
        json.dump({"claims": claims}, f, indent=2)
    git(["add", "claims.json"], clone)
    git(["commit", "-qm", "agentsync: churn"], clone)
    git(["push", "-q", "origin", "agentsync"], clone)
    git(["checkout", "-q", "main"], clone)


DONE_CLAIM = {
    "task": "auth endpoint",
    "touches": ["auth.py"],
    "requires": [],
    "branch": "stobie/auth",
    "status": "done",
    "updated_at": "2026-07-01T00:00:00+00:00",
    "instance": "abc12345",
    "note": "auth uses argon2id; login route is /api/login and returns a JWT",
    "changed_files": [{"status": "A", "path": "auth.py"},
                      {"status": "M", "path": "api/routes.py"}],
}


def seed_context_keeper(clone):
    """Real .context/ store files in context-keeper's exact schema."""
    ctx = os.path.join(clone, ".context")
    os.makedirs(ctx, exist_ok=True)
    decisions = [{
        "id": "dec-001", "schema_version": 1,
        "summary": "Use argon2id for password hashing, not bcrypt",
        "problem": "Password storage needed a KDF choice",
        "why_chosen": "argon2id is memory-hard and the OWASP first choice",
        "what_we_tried": "", "tradeoffs": "slower hashing on login",
        "tags": ["security", "auth"], "related_to": [],
        "alternatives": [], "constraints_created": [], "superseded_by": None,
        "status": "active",
        "created_at": "2026-06-01T00:00:00+00:00",
        "verified_at": "2026-06-01T00:00:00+00:00",
    }, {
        "id": "dec-002", "schema_version": 1,
        "summary": "Deprecated decision that must not import",
        "problem": "x", "why_chosen": "y", "what_we_tried": "", "tradeoffs": "",
        "tags": [], "related_to": [], "alternatives": [],
        "constraints_created": [], "superseded_by": None,
        "status": "deprecated",
        "created_at": "2026-06-01T00:00:00+00:00",
        "verified_at": "2026-06-01T00:00:00+00:00",
    }]
    constraints = [{
        "id": "con-001", "schema_version": 1,
        "rule": "Never log raw passwords or JWTs, even at debug level",
        "reason": "Tokens in logs leaked to the aggregator once already",
        "hardness": "absolute", "scope": "auth", "tags": ["security"],
        "related_to": [], "triggering_incident": "log leak 2026-05",
        "status": "active",
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
        "verified_at": "2026-06-01T00:00:00+00:00",
    }]
    with open(os.path.join(ctx, "decisions.json"), "w") as f:
        json.dump(decisions, f, indent=2)
    with open(os.path.join(ctx, "constraints.json"), "w") as f:
        json.dump(constraints, f, indent=2)


# --------------------------------------------------------------------------- #
# capture + recall
# --------------------------------------------------------------------------- #
def test_capture_and_recall_local():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        r = json.loads(M.capture(
            "Grafana dashboard 'svc-overview' has the latency panels",
            kind="runbook", why="asked every onboarding", tags="grafana,observability"))
        assert r["status"] == "captured", r
        rec = json.loads(M.recall("where are the grafana latency dashboards"))
        assert rec["results"], rec
        assert rec["results"][0]["kind"] == "runbook", rec
        assert "no_confident_match" not in rec, rec
        # usage was tracked
        rec2 = json.loads(M.recall("grafana"))
        assert rec2["results"][0]["trust"]["recalls"] >= 1, rec2


def test_recall_abstains_on_nonsense():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.capture("The deploy script lives in tools/deploy.sh", tags="deploy")
        rec = json.loads(M.recall("zzqx flurbo wumbo"))
        assert rec.get("no_confident_match") is True, rec


def test_capture_validation():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        assert "error" in json.loads(M.capture("", tags="x"))
        assert "error" in json.loads(M.capture("y", type="bogus"))


def test_record_need():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        r = json.loads(M.record_need("We need seeded staging data for auth tests",
                                     why="everyone hand-rolls fixtures"))
        assert r["status"] == "captured" and r["item"]["type"] == "need", r
        rec = json.loads(M.recall("staging data fixtures"))
        assert rec["results"][0]["type"] == "need", rec


# --------------------------------------------------------------------------- #
# distill — the agentsync + context-keeper bridges
# --------------------------------------------------------------------------- #
def test_distill_from_agentsync_done_claim():
    with lab() as (root, origin, clones):
        seed_agentsync_branch(clones["stobie"], {"stobie": DONE_CLAIM})
        be(clones, "jonny")
        r = json.loads(M.distill())
        assert r["sources"]["agentsync"] == "read", r
        outcomes = [i for i in r["items"] if i["kind"] == "outcome"]
        assert len(outcomes) == 1, r
        assert "argon2id" in outcomes[0]["content"], r
        assert "auth.py" in outcomes[0]["content"], r
        # the distilled knowledge is recallable
        rec = json.loads(M.recall("what does the login route return"))
        assert rec["results"] and "JWT" in rec["results"][0]["content"], rec


def test_distill_from_context_keeper():
    with lab() as (root, origin, clones):
        seed_context_keeper(clones["jonny"])
        be(clones, "jonny")
        r = json.loads(M.distill())
        assert r["sources"]["context_keeper"] == "read", r
        kinds = sorted(i["kind"] for i in r["items"])
        assert kinds == ["constraint", "decision"], r  # deprecated dec-002 skipped
        rec = json.loads(M.recall("password hashing choice"))
        assert "argon2id" in rec["results"][0]["content"], rec
        assert rec["results"][0]["source"]["ref"] == "dec-001", rec
        # the constraint carries its why
        rec = json.loads(M.recall("logging JWT rule"))
        assert "aggregator" in rec["results"][0]["why"], rec


def test_distill_reads_legacy_rationale_field():
    """Pre-v0.4 context-keeper decisions carry `rationale`, not `why_chosen`;
    distill must still capture the WHY (context-keeper's whole point)."""
    with lab() as (root, origin, clones):
        ctx = os.path.join(clones["jonny"], ".context")
        os.makedirs(ctx, exist_ok=True)
        decisions = [{
            "id": "dec-legacy", "schema_version": 1,
            "summary": "Use event sourcing for the ledger",
            "rationale": "auditability requires an append-only history",
            "status": "active",
            "created_at": "2026-06-01T00:00:00+00:00",
        }]
        with open(os.path.join(ctx, "decisions.json"), "w",
                  encoding="utf-8") as f:
            json.dump(decisions, f, indent=2)
        be(clones, "jonny")
        M.distill()
        it = M._read_local(M._cfg())["items"][0]
        assert it["kind"] == "decision", it
        assert "auditability" in it["why"], it


def test_distill_is_idempotent():
    with lab() as (root, origin, clones):
        seed_agentsync_branch(clones["stobie"], {"stobie": DONE_CLAIM})
        seed_context_keeper(clones["jonny"])
        be(clones, "jonny")
        first = json.loads(M.distill())
        assert first["new_items"] == 3, first  # 1 outcome + 1 decision + 1 constraint
        second = json.loads(M.distill())
        assert second["new_items"] == 0, second


def test_distill_reports_missing_substrates():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        r = json.loads(M.distill())
        assert r["new_items"] == 0, r
        assert "no coordination branch" in r["sources"]["agentsync"], r
        assert "no .context" in r["sources"]["context_keeper"], r


def test_distill_normalizes_cp1252_mojibake_on_write():
    # A substrate that fed us a cp1252-mangled em-dash (UTF-8 bytes E2 80 94
    # decoded as Windows-1252 -> "â€”") must land CLEAN in the
    # canonical store, so recall() serves clean text — not merely the markdown
    # export. Guards against the store/KNOWLEDGE.md divergence that put "â
    # €”" into the org knowledge.json while the .md rendered fine.
    mojibake = "â€”"        # em-dash gone through cp1252
    clean = "—"                        # the em-dash it must become
    with lab() as (root, origin, clones):
        ctx = os.path.join(clones["jonny"], ".context")
        os.makedirs(ctx, exist_ok=True)
        constraints = [{
            "id": "con-mojibake", "schema_version": 1,
            "rule": f"Verify with git merge-tree {mojibake} do not trust an "
                    f"empty files[] list as a real conflict",
            "reason": f"a missing ref {mojibake} returns a misleading conflict",
            "hardness": "absolute", "scope": "coord", "tags": ["agentsync"],
            "related_to": [], "triggering_incident": "",
            "status": "active",
            "created_at": "2026-06-01T00:00:00+00:00",
            "updated_at": "2026-06-01T00:00:00+00:00",
            "verified_at": "2026-06-01T00:00:00+00:00",
        }]
        with open(os.path.join(ctx, "constraints.json"), "w",
                  encoding="utf-8") as f:
            json.dump(constraints, f, indent=2)
        be(clones, "jonny")
        M.distill()
        stored = M._read_local(M._cfg())["items"][0]
        assert mojibake not in stored["content"], stored["content"]
        assert clean in stored["content"], stored["content"]
        assert mojibake not in stored["why"], stored["why"]
        assert clean in stored["why"], stored["why"]
        # and recall returns the repaired text, not the mangled bytes
        rec = json.loads(M.recall("git merge-tree empty files conflict"))
        assert rec["results"], rec
        assert mojibake not in rec["results"][0]["content"], rec


# --------------------------------------------------------------------------- #
# release-time capture (opt-in) — capture a claim at its done/released
# transition, before agentsync churns it out of live state
# --------------------------------------------------------------------------- #
def test_release_capture_is_off_by_default():
    """A live claim released without a full distill catching it is lost unless
    the flag is on — the default must not silently change."""
    with lab() as (root, origin, clones):
        st = clones["stobie"]
        seed_agentsync_branch(st, {"stobie": DONE_CLAIM})
        be(clones, "jonny")                          # no flag
        first = json.loads(M.distill())
        assert first["release_capture"] is False, first
        assert first["new_items"] == 1, first        # done claim caught live
        # nothing snapshotted when off -> a churn is not reconstructed
        rewrite_agentsync_claims(st, {})             # stobie released
        after = json.loads(M.distill())
        assert after["released_captured"] == 0, after
        assert M._read_local(M._cfg())["imported"]["agentsync_last"] == {}, after


def test_release_capture_survives_reclaim_churn():
    """A done claim that is captured, then re-claimed away before any further
    distill, stays captured exactly once — not lost, not duplicated."""
    with lab() as (root, origin, clones):
        st = clones["stobie"]
        seed_agentsync_branch(st, {"stobie": DONE_CLAIM})
        be(clones, "jonny", CAMBIUM_RELEASE_CAPTURE="1")
        first = json.loads(M.distill())
        assert first["release_capture"] is True, first
        outcomes = [i for i in first["items"] if i["kind"] == "outcome"]
        assert len(outcomes) == 1 and "argon2id" in outcomes[0]["content"], first

        # stobie re-claims brand-new work: the done claim is overwritten out of
        # live state (agentsync keys by agent id) before another distill runs
        reclaim = {"task": "billing refactor", "touches": ["billing.py"],
                   "requires": [], "branch": "stobie/billing",
                   "status": "in-progress",
                   "updated_at": "2026-07-02T00:00:00+00:00",
                   "instance": "def67890", "note": None, "changed_files": []}
        rewrite_agentsync_claims(st, {"stobie": reclaim})
        churned = json.loads(M.distill())
        assert churned["new_items"] == 0, churned          # not re-captured
        auth = [i for i in M._read_local(M._cfg())["items"]
                if i["kind"] == "outcome" and "argon2id" in i["content"]]
        assert len(auth) == 1, "first completion captured exactly once"

        # a later full distill still never duplicates it
        again = json.loads(M.distill())
        assert again["new_items"] == 0, again
        auth = [i for i in M._read_local(M._cfg())["items"]
                if i["kind"] == "outcome" and "argon2id" in i["content"]]
        assert len(auth) == 1, again


def test_release_capture_grabs_noted_claim_a_full_distill_would_miss():
    """A claim carrying a reconciliation note but released before ever reaching
    'done' is invisible to a full distill (which reads only done claims).
    Release-time capture keeps it via the last-seen snapshot."""
    with lab() as (root, origin, clones):
        st = clones["stobie"]
        noted = {"task": "cache spike", "touches": ["cache.py"], "requires": [],
                 "branch": "stobie/cache", "status": "in-progress",
                 "updated_at": "2026-07-01T00:00:00+00:00", "instance": "aaa11111",
                 "note": "redis TTL must be >= 300s or the stampede returns",
                 "changed_files": []}
        seed_agentsync_branch(st, {"stobie": noted})

        # flag on: first sweep records the snapshot; nothing captured yet
        # (the claim is live and not done)
        be(clones, "jonny", CAMBIUM_RELEASE_CAPTURE="1")
        seed = json.loads(M.distill())
        assert seed["new_items"] == 0, seed
        assert "stobie" in M._read_local(M._cfg())["imported"]["agentsync_last"]

        # stobie releases the claim — agentsync pops it entirely
        rewrite_agentsync_claims(st, {})
        got = json.loads(M.distill())
        assert got["released_captured"] == 1, got
        caps = [i for i in got["items"] if "redis TTL" in i["content"]]
        assert caps and caps[0]["content"].startswith("[stobie] released"), got

        # recallable, and re-distill does not duplicate
        rec = json.loads(M.recall("redis cache TTL stampede"))
        assert rec["results"] and "redis TTL" in rec["results"][0]["content"], rec
        assert json.loads(M.distill())["new_items"] == 0


# --------------------------------------------------------------------------- #
# import — ingest an external memory export as a source adapter
# --------------------------------------------------------------------------- #
def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def test_import_json_array_maps_to_items_with_provenance():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        src = os.path.join(root, "memories.json")
        _write_json(src, [
            {"id": "m1", "title": "Deploy window",
             "content": "Deploys go out Tuesdays 10am UTC",
             "why": "avoids Friday incidents", "tags": ["deploy", "process"],
             "timestamp": "2026-05-01T00:00:00Z"},
            {"id": "m2", "text": "Postgres connection cap is 90",
             "tags": "db,postgres"},
        ])
        r = json.loads(M.import_memory("json", src))
        assert r["status"] == "imported", r
        assert (r["imported"], r["skipped"], r["duplicates"]) == (2, 0, 0), r
        assert r["scope"] == "local", r

        items = M._read_local(M._cfg())["items"]
        assert len(items) == 2, items
        m1 = next(i for i in items if i["source"]["ref"] == "m1")
        # imported items are local scope, never anything else on import
        assert m1["scope"] == "local", m1
        # provenance: marked imported, origin system + original id + timestamp
        assert m1["source"]["imported"] is True, m1
        assert m1["source"]["system"] == "json", m1
        assert m1["source"]["source_ts"] == "2026-05-01T00:00:00Z", m1
        # field mapping: title folded into body, why + tags carried over
        assert "Deploy window" in m1["content"] and "Tuesdays" in m1["content"], m1
        assert m1["why"] == "avoids Friday incidents", m1
        assert "imported" in m1["tags"] and "deploy" in m1["tags"], m1
        # a record with only a bare 'text' field still maps
        m2 = next(i for i in items if i["source"]["ref"] == "m2")
        assert "Postgres" in m2["content"] and "postgres" in m2["tags"], m2
        # imported knowledge is recallable like any other
        rec = json.loads(M.recall("when do deploys go out"))
        assert rec["results"] and "Tuesdays" in rec["results"][0]["content"], rec


def test_import_jsonl_and_reimport_is_idempotent():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        src = os.path.join(root, "memories.jsonl")
        _write_text(src,
                    '{"id": "a", "content": "use ruff for linting"}\n'
                    '{"id": "b", "content": "prefer pathlib over os.path"}\n')
        first = json.loads(M.import_memory("json", src))
        assert first["imported"] == 2, first
        # re-importing the same source adds nothing — routed through the same
        # watermark path distill uses
        again = json.loads(M.import_memory("json", src))
        assert (again["imported"], again["duplicates"]) == (0, 2), again
        assert len(M._read_local(M._cfg())["items"]) == 2


def test_import_dedupes_by_content_when_no_id():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        src = os.path.join(root, "noids.json")
        _write_json(src, [{"text": "dedupe me by content hash"},
                          {"text": "dedupe me by content hash"}])
        r = json.loads(M.import_memory("json", src))
        assert (r["imported"], r["duplicates"]) == (1, 1), r


def test_import_handles_malformed_and_missing_fields():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        src = os.path.join(root, "messy.jsonl")
        _write_text(src,
                    '{"id": "ok", "content": "keep me"}\n'
                    'not valid json at all\n'          # unparseable line -> skip
                    '{"id": "empty", "content": ""}\n'  # empty body -> skip
                    '"a bare string, not an object"\n'  # not a dict -> skip
                    '{"id": "notext", "tags": ["x"]}\n')  # no body -> skip
        r = json.loads(M.import_memory("json", src))
        assert r["imported"] == 1, r
        assert r["skipped"] == 4, r
        items = M._read_local(M._cfg())["items"]
        assert len(items) == 1 and items[0]["content"] == "keep me", items


def test_import_rejects_unknown_source_and_missing_file():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        assert "error" in json.loads(M.import_memory("notes-app", "/whatever")), \
            "unknown adapter must error, not guess"
        assert "error" in json.loads(
            M.import_memory("json", os.path.join(root, "nope.json")))


def test_import_is_read_only_against_source():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        src = os.path.join(root, "immutable.json")
        _write_json(src, [{"id": "z", "content": "source stays untouched"}])
        before = open(src, encoding="utf-8").read()
        M.import_memory("json", src)
        after = open(src, encoding="utf-8").read()
        assert before == after, "import must never mutate the source"


def test_import_items_are_not_auto_promoted():
    with lab() as (root, origin, clones):
        # threshold of 1 recall would promote easily — but import grants none
        be(clones, "jonny", CAMBIUM_PROMOTE_RECALLS="1")
        src = os.path.join(root, "mem.json")
        _write_json(src, [{"id": "p", "content": "imported wisdom stays local"}])
        M.import_memory("json", src)
        rp = json.loads(M.review_promotions())
        assert rp["eligible_for_team"] == [], rp   # 0 recalls, 0 endorsements
        assert all(i["scope"] == "local"
                   for i in M._read_local(M._cfg())["items"])
        assert M._read_team(M._cfg()) == []


# --------------------------------------------------------------------------- #
# post-promotion staleness — verification events + premise linkage
# --------------------------------------------------------------------------- #
def mk_item(item_id, content, scope="team", last_verified=None, valid_while=None,
            project="proj", tags=None, status="active"):
    """A minimal promoted-shaped item for seeding the team store directly."""
    it = {"id": item_id, "type": "memory", "kind": "note", "content": content,
          "why": "", "tags": tags or [], "scope": scope, "project": project,
          "source": {"system": "manual", "ref": ""}, "created_by": "t",
          "created_at": "2025-01-01T00:00:00+00:00",
          "updated_at": "2025-01-01T00:00:00+00:00", "status": status,
          "trust": {"recalls": 0, "endorsements": [], "projects": [project]}}
    if last_verified is not None:
        it["last_verified"] = last_verified
    if valid_while is not None:
        it["valid_while"] = valid_while
    return it


def seed_team_items(items):
    """Push crafted items onto the team branch via the real CAS write path."""
    def add(data):
        data["items"].extend(items)
        return None
    assert M._team_mutate(M._cfg(), add, "test: seed team items")


def org_item(iid, content, project, kind="note", system="manual", ref="",
             recalls=0, last_verified="2026-06-01T00:00:00+00:00", why=""):
    """An org-scope knowledge item shaped exactly like a promoted one."""
    return {"id": iid, "type": "memory", "kind": kind, "content": content,
            "why": why, "tags": [], "scope": "org", "project": project,
            "source": {"system": system, "ref": ref}, "created_by": "t",
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": last_verified,
            "status": "active", "last_verified": last_verified,
            "trust": {"recalls": recalls, "endorsements": [],
                      "projects": [project]}}


def seed_org_items(org_clone, items):
    """Append items to the org repo's knowledge.json on its origin (no
    KNOWLEDGE.md yet), so export can be exercised against a real org repo."""
    git(["fetch", "-q", "origin"], org_clone)
    git(["checkout", "-q", "main"], org_clone)
    git(["reset", "--hard", "origin/main"], org_clone)
    path = os.path.join(org_clone, "knowledge.json")
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) \
        else {"items": []}
    data["items"].extend(items)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    git(["add", "knowledge.json"], org_clone)
    git(["commit", "-qm", "seed org items"], org_clone)
    git(["push", "-q", "origin", "main"], org_clone)


def test_optional_staleness_fields_absent_are_handled():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        # a legacy-shaped item carries neither new field
        item = json.loads(M.capture("legacy fact", tags="legacy"))["item"]
        assert "last_verified" not in item and "valid_while" not in item, item
        # recall still works, and verify_entry adds the field cleanly
        assert json.loads(M.recall("legacy fact"))["results"], "recall broke"
        v = json.loads(M.verify_entry(item["id"]))
        assert v["status"] == "verified" and v["item"]["last_verified"], v


def test_capture_records_valid_while_premise():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        r = json.loads(M.capture("Sync inventory nightly via the REST bridge",
                                 valid_while="while we're on NetSuite",
                                 tags="inventory"))
        assert r["item"]["valid_while"] == "while we're on NetSuite", r
        stored = M._read_local(M._cfg())["items"][0]
        assert stored["valid_while"] == "while we're on NetSuite", stored


def test_verify_entry_local_roundtrip():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        iid = json.loads(M.capture("cron runs at 0300 UTC", tags="cron"))["item"]["id"]
        assert "last_verified" not in M._read_local(M._cfg())["items"][0]
        v = json.loads(M.verify_entry(iid, note="confirmed with ops"))
        assert v["status"] == "verified" and v["scope"] == "local", v
        stored = M._read_local(M._cfg())["items"][0]
        assert stored["last_verified"] == v["last_verified"], stored
        assert stored["last_verified_note"] == "confirmed with ops", stored


def test_promotion_stamps_last_verified():
    with lab() as (root, origin, clones):
        be(clones, "jonny", CAMBIUM_PROMOTE_RECALLS="1")
        iid = json.loads(M.capture("promote me", tags="promo"))["item"]["id"]
        assert "last_verified" not in M._read_local(M._cfg())["items"][0]
        M.recall("promote me")  # 1 recall -> eligible
        assert json.loads(M.promote())["status"] == "promoted"
        team_item = next(i for i in M._read_team(M._cfg()) if i["id"] == iid)
        assert team_item["last_verified"], "promotion must stamp last_verified"


def test_verify_entry_reaches_team_scope():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        iid = json.loads(M.capture("shared team truth", tags="shared"))["item"]["id"]
        M.endorse(iid)
        M.promote()  # -> team, stamped at promotion
        promo_ts = next(i for i in M._read_team(M._cfg())
                        if i["id"] == iid)["last_verified"]
        v = json.loads(M.verify_entry(iid, note="re-checked"))
        assert v["status"] == "verified" and v["scope"] == "team", v
        after = next(i for i in M._read_team(M._cfg()) if i["id"] == iid)
        assert after["last_verified"] == v["last_verified"] >= promo_ts, after
        assert after["last_verified_note"] == "re-checked", after


def test_stale_report_orders_oldest_first_and_flags_never_verified():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        seed_team_items([
            mk_item("k-recent", "recently verified",
                    last_verified="2026-07-01T00:00:00+00:00"),
            mk_item("k-old", "verified long ago",
                    last_verified="2025-01-01T00:00:00+00:00"),
            mk_item("k-never", "never reverified since a legacy promotion",
                    valid_while="while we're on NetSuite"),
        ])
        rep = json.loads(M.stale_report())
        assert [e["id"] for e in rep["entries"]] == ["k-never", "k-old",
                                                     "k-recent"], rep
        assert rep["never_verified"] == 1, rep
        never = rep["entries"][0]
        assert never["never_verified"] is True and never["last_verified"] is None
        assert never["valid_while"] == "while we're on NetSuite", never


def test_stale_report_older_than_days_and_project_filters():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        seed_team_items([
            mk_item("k-fresh", "verified just now", project="alpha",
                    last_verified=M._now()),
            mk_item("k-stale", "verified ages ago", project="alpha",
                    last_verified="2025-01-01T00:00:00+00:00"),
            mk_item("k-never", "never verified", project="alpha"),
            mk_item("k-beta", "other project", project="beta"),
        ])
        rep = json.loads(M.stale_report(project="alpha", older_than_days=30))
        ids = {e["id"] for e in rep["entries"]}
        assert "k-fresh" not in ids, rep       # recently verified -> filtered
        assert "k-beta" not in ids, rep        # wrong project -> filtered
        assert ids == {"k-stale", "k-never"}, rep  # old + never kept


def test_distill_release_includes_verification_prompt():
    with lab() as (root, origin, clones):
        be(clones, "jonny", CAMBIUM_RELEASE_CAPTURE="1")
        # a promoted, never-reverified entry relevant to the auth work about to
        # land, plus an unrelated one that should NOT surface
        seed_team_items([
            mk_item("k-authold", "auth service hashes passwords with argon2id",
                    tags=["auth", "argon2id"], valid_while="while argon2 is our KDF"),
            mk_item("k-billing", "invoices are net-30", tags=["billing"],
                    last_verified="2026-07-01T00:00:00+00:00"),
        ])
        seed_agentsync_branch(clones["stobie"], {"stobie": DONE_CLAIM})
        d = json.loads(M.distill())
        vp = d["verification_prompt"]
        assert vp, "release distill should surface a verification prompt"
        ids = [e["id"] for e in vp]
        assert "k-authold" in ids, vp          # relevant to the auth release
        assert "k-billing" not in ids, vp      # unrelated -> not surfaced
        top = next(e for e in vp if e["id"] == "k-authold")
        assert top["never_verified"] is True, top
        assert top["valid_while"] == "while argon2 is our KDF", top


# --------------------------------------------------------------------------- #
# promotion lifecycle
# --------------------------------------------------------------------------- #
def test_promote_local_to_team_by_recalls():
    with lab() as (root, origin, clones):
        be(clones, "jonny", CAMBIUM_PROMOTE_RECALLS="2")
        M.capture("CI needs FORCE_COLOR=0 or the log parser chokes", tags="ci")
        # not eligible yet
        r = json.loads(M.promote())
        assert r["status"] == "none_eligible", r
        M.recall("ci log parser")
        M.recall("FORCE_COLOR ci")
        r = json.loads(M.promote())
        assert r["status"] == "promoted" and r["to"] == "team", r
        # gone from local, visible to ANOTHER collaborator from team scope
        be(clones, "stobie", CAMBIUM_PROMOTE_RECALLS="2")
        rec = json.loads(M.recall("why does the ci log parser choke"))
        assert rec["results"], rec
        assert rec["results"][0]["scope"] == "team", rec


def test_endorse_fast_tracks_promotion():
    with lab() as (root, origin, clones):
        be(clones, "jonny")  # default threshold 3 recalls — endorse skips it
        r = json.loads(M.capture("Rotate the staging TLS cert every 60 days",
                                 tags="tls staging"))
        item_id = r["item"]["id"]
        assert json.loads(M.promote())["status"] == "none_eligible"
        e = json.loads(M.endorse(item_id, note="confirmed with infra"))
        assert e["status"] == "endorsed" and e["scope"] == "local", e
        r = json.loads(M.promote())
        assert r["status"] == "promoted", r


def test_promote_explicit_id_respects_threshold_and_force():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        item_id = json.loads(M.capture("niche fact", tags="niche"))["item"]["id"]
        r = json.loads(M.promote(item_id=item_id))
        assert r["status"] == "not_eligible", r
        r = json.loads(M.promote(item_id=item_id, force=True))
        assert r["status"] == "promoted", r


def test_team_recall_tracks_cross_project_usage():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        item_id = json.loads(M.capture("shared wisdom", tags="wisdom"))["item"]["id"]
        M.endorse(item_id)
        M.promote()
        # a collaborator whose clone dir (project name) differs recalls it
        be(clones, "stobie")
        json.loads(M.recall("shared wisdom"))
        team = M._read_team(M._cfg())
        item = next(i for i in team if i["id"] == item_id)
        assert item["trust"]["recalls"] >= 1, item
        assert "stobie" in item["trust"]["projects"], item


def test_promote_team_to_org_requires_endorsement():
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone, CAMBIUM_PROMOTE_RECALLS="1")
        item_id = json.loads(M.capture("org-worthy: all services use UTC "
                                       "everywhere, never local time",
                                       tags="time utc"))["item"]["id"]
        M.recall("utc time")            # 1 recall -> team-eligible
        assert json.loads(M.promote())["status"] == "promoted"
        # no endorsement yet -> org refuses
        r = json.loads(M.promote(item_id=item_id, to_scope="org"))
        assert r["status"] == "not_eligible", r
        M.endorse(item_id, note="org-wide truth")
        r = json.loads(M.promote(item_id=item_id, to_scope="org"))
        assert r["status"] == "promoted" and r["to"] == "org", r
        # landed on the org repo's origin, removed from team
        p = git(["show", "origin/main:knowledge.json"],
                os.path.join(root, "org-clone"))
        org_items = json.loads(p.stdout)["items"]
        assert any(i["id"] == item_id for i in org_items), org_items
        assert all(i["id"] != item_id for i in M._read_team(M._cfg()))
        # and org scope is recallable by anyone configured with the org repo
        rec = json.loads(M.recall("what timezone do services use", scope="org"))
        assert rec["results"] and rec["results"][0]["scope"] == "org", rec


# --------------------------------------------------------------------------- #
# org generalization gate — a project-specific body cannot silently acquire
# org-wide readership; it must be restated (or forced) at the boundary.
# --------------------------------------------------------------------------- #
def _seed_team_endorsed(clones, org_clone, content, note):
    """Capture -> team -> endorse, returning the item_id ready for org promotion."""
    be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone, CAMBIUM_PROMOTE_RECALLS="1")
    item_id = json.loads(M.capture(content, tags="x"))["item"]["id"]
    M.recall(content[:30])
    M.promote()
    M.endorse(item_id, note=note)
    return item_id


def test_org_promotion_blocks_project_specific_body():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        item_id = _seed_team_endorsed(
            clones, org_clone,
            "Append every regime boundary to dashboard.py REGIMES so trend "
            "lines are never misread across a discontinuity",
            "Universal metrics practice: annotate a regime boundary whenever a "
            "metric's computation changes")
        r = json.loads(M.promote(item_id=item_id, to_scope="org"))
        assert r["status"] == "not_generalized", r
        assert any("dashboard.py" in s for s in r["project_local_signals"]), r
        assert "regime boundary" in (r["suggested_org_statement"] or ""), r
        # nothing crossed into org
        assert all(i["id"] != item_id for i in M._read_org(M._cfg())), "leaked"


def test_org_promotion_with_org_content_generalizes_and_preserves_example():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        specific = ("Append every regime boundary to dashboard.py REGIMES so "
                    "trends are never misread across a discontinuity")
        item_id = _seed_team_endorsed(clones, org_clone, specific,
                                      "annotate regime boundaries")
        general = ("Annotate a regime boundary whenever a metric's computation "
                   "or accounting changes, so trends are not misread across it")
        r = json.loads(M.promote(item_id=item_id, to_scope="org",
                                 org_content=general))
        assert r["status"] == "promoted", r
        assert r["item"]["content"] == general, r
        assert r["item"]["example"] == specific, r     # concrete kept
        # a collaborator recalls the GENERAL rule at org scope; concrete = example
        be(clones, "stobie", CAMBIUM_ORG_REPO=org_clone)
        rec = json.loads(M.recall("metric computation changed trend", scope="org"))
        top = rec["results"][0]
        assert top["content"] == general, rec
        assert "dashboard.py" in top["example"], rec


def test_org_promotion_force_ships_specific_body():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        item_id = _seed_team_endorsed(
            clones, org_clone,
            "Back up the checkpoint as clark_foundation.pt.bak first",
            "training safety")
        r = json.loads(M.promote(item_id=item_id, to_scope="org", force=True))
        assert r["status"] == "promoted", r
        assert "clark_foundation" in r["item"]["content"], r
        assert "example" not in r["item"], r           # forced, not restated


def test_org_promotion_allows_clean_universal_body():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        item_id = _seed_team_endorsed(
            clones, org_clone,
            "Every subprocess or network call must have an explicit timeout",
            "universal reliability")
        r = json.loads(M.promote(item_id=item_id, to_scope="org"))
        assert r["status"] == "promoted", r            # gate must not over-block


def test_recall_surfaces_endorsed_as():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        item_id = json.loads(M.capture("Keep hook print() output ASCII-only",
                                       tags="hooks"))["item"]["id"]
        M.endorse(item_id, note="universal Windows cp1252 hazard")
        rec = json.loads(M.recall("hook ascii output"))
        assert rec["results"][0]["endorsed_as"] == \
            ["universal Windows cp1252 hazard"], rec


def test_review_promotions_flags_org_needs_generalization():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        item_id = _seed_team_endorsed(
            clones, org_clone,
            "Mirror each decision into DECISIONS.md in the same commit",
            "keep machine + human logs in lockstep")
        # force a specific body into org (as pre-gate promotions did), then the
        # tool self-diagnoses it as needing generalization
        M.promote(item_id=item_id, to_scope="org", force=True)
        rv = json.loads(M.review_promotions())
        row = next((x for x in rv["org_needs_generalization"]
                    if x["id"] == item_id), None)
        assert row, rv
        assert any("DECISIONS.md" in s for s in row["project_local_signals"]), row
        assert "lockstep" in (row["suggested_org_statement"] or ""), row


def test_generalize_org_item_in_place_and_clears_flag():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        item_id = _seed_team_endorsed(
            clones, org_clone,
            "Append every regime boundary to dashboard.py REGIMES",
            "annotate regime boundaries when a metric's computation changes")
        M.promote(item_id=item_id, to_scope="org", force=True)  # pre-gate style
        general = ("Annotate a regime boundary whenever a metric's computation "
                   "changes, so trends are not misread across it")
        r = json.loads(M.generalize(item_id, org_content=general))
        assert r["status"] == "generalized" and r["scope"] == "org", r
        # the org store now serves the general rule; the concrete stays as example
        org = M._read_org(M._cfg())
        it = next(i for i in org if i["id"] == item_id)
        assert it["content"] == general, it
        assert "dashboard.py" in it["example"], it
        # and the tool no longer flags it as needing generalization
        rv = json.loads(M.review_promotions())
        assert all(x["id"] != item_id
                   for x in rv["org_needs_generalization"]), rv


def test_generalize_falls_back_to_endorsement_note():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        note = "Back up any model checkpoint before a destructive training change"
        item_id = _seed_team_endorsed(
            clones, org_clone,
            "Back up the checkpoint as clark_foundation.pt.bak first", note)
        M.promote(item_id=item_id, to_scope="org", force=True)
        r = json.loads(M.generalize(item_id))          # no org_content -> use note
        assert r["status"] == "generalized", r
        assert r["content"] == note, r
        it = next(i for i in M._read_org(M._cfg()) if i["id"] == item_id)
        assert "clark_foundation" in it["example"], it


def test_generalize_is_idempotent():
    with lab() as (root, origin, clones):
        _, org_clone = setup_org_repo(root)
        item_id = _seed_team_endorsed(
            clones, org_clone,
            "Keep the schema under 2500 tokens per TestToolSchemaBudget",
            "Budget an MCP tools/list payload; lazy-load rich guidance")
        M.promote(item_id=item_id, to_scope="org", force=True)
        g = "Budget an MCP tools/list payload; lazy-load rich guidance"
        assert json.loads(M.generalize(item_id, org_content=g))["status"] == \
            "generalized"
        # second call is a no-op (content already general), example not clobbered
        r2 = json.loads(M.generalize(item_id, org_content=g))
        assert r2.get("detail") in ("no change", "already current"), r2
        it = next(i for i in M._read_org(M._cfg()) if i["id"] == item_id)
        assert "TestToolSchemaBudget" in it["example"], it


def test_promote_to_org_via_pull_request():
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone, CAMBIUM_ORG_PR="1",
           CAMBIUM_PROMOTE_RECALLS="1")
        item_id = json.loads(M.capture("API errors follow RFC 7807",
                                       tags="api errors"))["item"]["id"]
        M.endorse(item_id)
        M.promote()

        pr_calls = []
        orig = M._gh
        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["pr", "create"]:
                pr_calls.append(args)
                return SimpleNamespace(returncode=0,
                                       stdout="https://github.com/org/knowledge/pull/12\n",
                                       stderr="")
            raise AssertionError(f"unexpected gh call: {args}")
        M._gh = fake_gh
        try:
            r = json.loads(M.promote(item_id=item_id, to_scope="org"))
        finally:
            M._gh = orig
        assert r["status"] == "pr_opened", r
        assert r["pr_url"].endswith("/pull/12"), r
        assert pr_calls and "--head" in pr_calls[0], pr_calls
        # PR branch was pushed to the org origin
        p = git(["ls-remote", "--heads", "origin", f"cambium/promote-{item_id}"],
                org_clone)
        assert p.stdout.strip(), "PR branch not on org origin"
        # team copy stays, annotated with the PR
        team = M._read_team(M._cfg())
        item = next(i for i in team if i["id"] == item_id)
        assert item["promotion"]["pr"].endswith("/pull/12"), item
        # review_promotions surfaces the pending PR and stops re-listing it
        rp = json.loads(M.review_promotions())
        assert any(x["id"] == item_id for x in rp["org_prs_pending"]), rp
        assert all(x["id"] != item_id for x in rp["eligible_for_org"]), rp


# --------------------------------------------------------------------------- #
# export_markdown — human-readable KNOWLEDGE.md
# --------------------------------------------------------------------------- #
def test_export_markdown_publishes_grouped_with_provenance():
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone)
        seed_org_items(org_clone, [
            org_item("k-dec", "Use argon2id for password hashing", "auth-svc",
                     kind="decision", system="context-keeper", ref="dec-001",
                     recalls=5, why="OWASP first choice"),
            org_item("k-out", "Stripe webhooks verified via signature header",
                     "pay-svc", kind="outcome", system="agentsync",
                     ref="stobie:stobie/pay", recalls=2),
        ])
        r = json.loads(M.export_markdown())
        assert r["published"] is True and r["detail"] == "pushed", r
        md = r["markdown"]
        # grouped by scope then project
        assert "## org scope" in md, md
        assert "### auth-svc" in md and "### pay-svc" in md, md
        # each item: summary, kind, provenance (dec-NNN / claim origin), recalls,
        # promoted date
        assert "Use argon2id for password hashing" in md, md
        assert "kind: `decision`" in md, md
        assert "context-keeper dec-001" in md, md
        assert "agentsync claim stobie:stobie/pay" in md, md
        assert "recalls: 5" in md and "promoted: 2026-06-01" in md, md
        # actually published to the org origin
        p = git(["show", "origin/main:KNOWLEDGE.md"], org_clone)
        assert p.returncode == 0 and "argon2id" in p.stdout, p.stdout


def test_export_normalizes_cp1252_mojibake():
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone)
        # the classic UTF-8-as-cp1252 em-dash mojibake: "—" -> "â€”"
        bad = "Use Postgres â€” never MySQL for new services"
        seed_org_items(org_clone, [org_item("k-moji", bad, "db-svc")])
        md = json.loads(M.export_markdown())["markdown"]
        assert "Use Postgres — never MySQL" in md, repr(md)   # em dash restored
        assert "â€”" not in md, repr(md)            # mojibake gone


def test_org_promotion_writes_knowledge_md_alongside_json():
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone, CAMBIUM_PROMOTE_RECALLS="1")
        iid = json.loads(M.capture("All services log in UTC, never local time",
                                   kind="constraint", tags="time"))["item"]["id"]
        M.recall("utc log time")           # 1 recall -> team-eligible
        M.promote()
        M.endorse(iid, note="org-wide")
        r = json.loads(M.promote(item_id=iid, to_scope="org"))
        assert r["status"] == "promoted", r
        # KNOWLEDGE.md exists on the org origin and reflects the promotion
        md = git(["show", "origin/main:KNOWLEDGE.md"], org_clone)
        assert md.returncode == 0, md.stderr
        assert "## org scope" in md.stdout and "UTC" in md.stdout, md.stdout
        assert "kind: `constraint`" in md.stdout, md.stdout
        # the same commit carried BOTH files
        show = git(["show", "--name-only", "--pretty=format:", "origin/main"],
                   org_clone)
        assert "KNOWLEDGE.md" in show.stdout and "knowledge.json" in show.stdout, \
            show.stdout


def test_org_pr_mode_puts_knowledge_md_on_the_pr_branch():
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone, CAMBIUM_ORG_PR="1",
           CAMBIUM_PROMOTE_RECALLS="1")
        iid = json.loads(M.capture("API errors follow RFC 7807",
                                   tags="api errors"))["item"]["id"]
        M.endorse(iid)
        M.promote()
        orig = M._gh
        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["pr", "create"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="https://github.com/org/knowledge/pull/7\n", stderr="")
            raise AssertionError(f"unexpected gh call: {args}")
        M._gh = fake_gh
        try:
            r = json.loads(M.promote(item_id=iid, to_scope="org"))
        finally:
            M._gh = orig
        assert r["status"] == "pr_opened", r
        pr_branch = f"cambium/promote-{iid}"
        md = git(["show", f"origin/{pr_branch}:KNOWLEDGE.md"], org_clone)
        assert md.returncode == 0 and "RFC 7807" in md.stdout, md.stdout
        # both files on the same PR branch
        show = git(["show", "--name-only", "--pretty=format:",
                    f"origin/{pr_branch}"], org_clone)
        assert "KNOWLEDGE.md" in show.stdout and "knowledge.json" in show.stdout, \
            show.stdout


def test_export_markdown_local_and_team_render_without_publishing():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.capture("local-only note about the build", tags="build")
        r = json.loads(M.export_markdown(scope="local"))
        assert r["published"] is False and r["status"] == "rendered", r
        assert "## local scope" in r["markdown"], r
        assert "local-only note about the build" in r["markdown"], r


def test_review_promotions_lists_eligible():
    with lab() as (root, origin, clones):
        be(clones, "jonny", CAMBIUM_PROMOTE_RECALLS="1")
        a = json.loads(M.capture("alpha wisdom", tags="alpha"))["item"]["id"]
        M.capture("beta trivia", tags="beta")
        M.recall("alpha wisdom")
        rp = json.loads(M.review_promotions())
        ids = [x["id"] for x in rp["eligible_for_team"]]
        assert a in ids and len(ids) == 1, rp


def test_full_compound_growth_loop():
    """The Yarharel story end-to-end: work happens (agentsync) -> distilled to
    memory -> recalled by a teammate's agent -> promoted to team -> endorsed ->
    promoted to org -> recallable org-wide."""
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        seed_agentsync_branch(clones["stobie"], {"stobie": DONE_CLAIM})
        be(clones, "jonny", CAMBIUM_ORG_REPO=org_clone, CAMBIUM_PROMOTE_RECALLS="2")
        M.distill()
        M.recall("login JWT")
        M.recall("argon2 auth")
        assert json.loads(M.promote())["status"] == "promoted"
        team = M._read_team(M._cfg())
        item_id = next(i["id"] for i in team if "argon2id" in i["content"])
        M.endorse(item_id, note="canonical auth interface")
        # the distilled body names auth.py/routes.py, so org promotion requires
        # the cross-project restatement (the generalization gate).
        r = json.loads(M.promote(item_id=item_id, to_scope="org",
                                 org_content="Auth hashes passwords with "
                                 "argon2id and the login route returns a JWT"))
        assert r["status"] == "promoted", r
        assert "auth.py" in r["item"]["example"], r  # concrete body preserved
        # a different collaborator, org-configured, recalls it at org scope
        be(clones, "stobie", CAMBIUM_ORG_REPO=org_clone)
        rec = json.loads(M.recall("how does auth hash passwords with argon2",
                                  scope="org"))
        assert rec["results"] and "argon2id" in rec["results"][0]["content"], rec


# --------------------------------------------------------------------------- #
# concurrency + status
# --------------------------------------------------------------------------- #
def test_team_cas_survives_concurrent_push():
    """A peer commit landing between our fetch and push must be observed and
    preserved by the retry — the same guarantee agentsync proves."""
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        item_id = json.loads(M.capture("mine", tags="mine"))["item"]["id"]
        M.endorse(item_id)
        # peer lands their own team item out-of-band mid-flight
        scratch = os.path.join(root, "scratch")
        git(["clone", "-q", origin, scratch], root)

        real = M._git
        fired = {"done": False}
        def wrapper(args, cwd, check=True):
            # fire on the DATA push only — the branch-init push uses `push -u`
            if (args[0] == "push" and "-u" not in args and not fired["done"]
                    and cwd == M._cfg()["worktree"]):
                fired["done"] = True
                git(["fetch", "-q", "origin", "cambium"], scratch)
                git(["checkout", "-qB", "cambium", "origin/cambium"], scratch)
                path = os.path.join(scratch, "knowledge.json")
                data = json.load(open(path)) if os.path.exists(path) else {"items": []}
                data["items"].append({"id": "k-peer", "type": "memory",
                                      "kind": "note", "content": "peer item",
                                      "why": "", "tags": [], "scope": "team",
                                      "project": "x", "source": {}, "status":
                                      "active", "created_by": "peer",
                                      "created_at": "t", "updated_at": "t",
                                      "trust": {"recalls": 0, "endorsements": [],
                                                "projects": []}})
                json.dump(data, open(path, "w"), indent=2)
                git(["add", "knowledge.json"], scratch)
                git(["commit", "-qm", "peer"], scratch)
                git(["push", "-q", "origin", "cambium"], scratch)
            return real(args, cwd, check)

        M._git = wrapper
        try:
            r = json.loads(M.promote())
        finally:
            M._git = real
        assert r["status"] == "promoted", r
        team = M._read_team(M._cfg())
        ids = {i["id"] for i in team}
        assert "k-peer" in ids and item_id in ids, ids  # both survived


def test_status_overview():
    with lab() as (root, origin, clones):
        seed_context_keeper(clones["jonny"])
        be(clones, "jonny")
        M.capture("a fact", tags="x")
        M.distill()
        s = json.loads(M.status())
        assert s["scopes"]["local"]["total"] == 3, s
        assert s["imported"]["context_keeper"] == 2, s
        assert s["substrates"]["context_dir"] is True, s
        assert s["scopes"]["org"] == "not configured", s


# --------------------------------------------------------------------------- #
# integration with the REAL agentsync (skipped if the sibling repo is absent)
# --------------------------------------------------------------------------- #
def test_real_agentsync_integration():
    if not os.path.exists(AGENTSYNC_PATH):
        print("SKIP  test_real_agentsync_integration (agentsync repo not found)")
        return
    A = load_module("agentsync_server", AGENTSYNC_PATH)
    with lab() as (root, origin, clones):
        # stobie's agent does real work through real agentsync tools
        os.environ["AGENTSYNC_REPO"] = clones["stobie"]
        os.environ["AGENTSYNC_AGENT_ID"] = "stobie"
        r = json.loads(A.claim("payments", ["pay.py"], branch="stobie/pay"))
        assert r["status"] == "claimed", r
        r = json.loads(A.update_status(
            "done", note="stripe webhooks verified via signature header"))
        assert r["status"] == "updated", r
        # jonny's cambium distills stobie's finished work into knowledge
        be(clones, "jonny")
        d = json.loads(M.distill())
        assert d["new_items"] == 1, d
        rec = json.loads(M.recall("how are stripe webhooks verified"))
        assert rec["results"], rec
        assert "signature" in rec["results"][0]["content"], rec
        os.environ.pop("AGENTSYNC_REPO", None)
        os.environ.pop("AGENTSYNC_AGENT_ID", None)


def test_real_agentsync_release_capture():
    """Release-time capture over the REAL agentsync seam: stobie completes work,
    then releases and re-claims through agentsync's own tools; the completion is
    captured once at its transition and never lost or duplicated."""
    if not os.path.exists(AGENTSYNC_PATH):
        print("SKIP  test_real_agentsync_release_capture (agentsync repo not found)")
        return
    A = load_module("agentsync_server", AGENTSYNC_PATH)
    with lab() as (root, origin, clones):
        def as_stobie():
            os.environ["AGENTSYNC_REPO"] = clones["stobie"]
            os.environ["AGENTSYNC_AGENT_ID"] = "stobie"

        # stobie finishes real work through real agentsync tools
        as_stobie()
        assert json.loads(A.claim("payments", ["pay.py"],
                                  branch="stobie/pay"))["status"] == "claimed"
        assert json.loads(A.update_status(
            "done", note="stripe webhooks verified via signature header"
        ))["status"] == "updated"

        # jonny's cambium, release-capture on, observes the done claim live
        be(clones, "jonny", CAMBIUM_RELEASE_CAPTURE="1")
        first = json.loads(M.distill())
        assert first["new_items"] == 1, first

        # stobie churns: real release (pops the claim), then re-claims new work
        as_stobie()
        assert json.loads(A.release())["status"] in ("released", "updated"), "release"
        assert json.loads(A.claim("refunds", ["refunds.py"],
                                  branch="stobie/refunds"))["status"] == "claimed"

        # the completed payments work is captured exactly once — not lost to the
        # churn, not duplicated by the re-run
        be(clones, "jonny", CAMBIUM_RELEASE_CAPTURE="1")
        churned = json.loads(M.distill())
        assert churned["new_items"] == 0, churned
        pays = [i for i in M._read_local(M._cfg())["items"]
                if i["kind"] == "outcome" and "signature" in i["content"]]
        assert len(pays) == 1, "stripe outcome captured exactly once"
        rec = json.loads(M.recall("how are stripe webhooks verified"))
        assert rec["results"] and "signature" in rec["results"][0]["content"], rec

        os.environ.pop("AGENTSYNC_REPO", None)
        os.environ.pop("AGENTSYNC_AGENT_ID", None)


# --------------------------------------------------------------------------- #
# onboarding — helpful first contact when unconfigured (status / fail-helpful /
# setup)
# --------------------------------------------------------------------------- #
def test_status_reports_gaps_when_unconfigured():
    with lab() as (root, origin, clones):
        unconfigured(root)
        s = json.loads(M.status())  # must not raise
        assert s["configured"] is False, s
        settings = {g["setting"] for g in s["gaps"]}
        assert {"CAMBIUM_REPO", "CAMBIUM_AGENT_ID"} <= settings, s
        # every gap states a plain-terms cost and the exact setup() fix
        assert all(g.get("cost") and "setup(" in g.get("fix", "")
                   for g in s["gaps"]), s
        assert "setup(" in s["next_step"], s


def test_every_tool_fails_helpful_when_unconfigured():
    with lab() as (root, origin, clones):
        unconfigured(root)
        tools = [
            ("capture", lambda: M.capture("x")),
            ("record_need", lambda: M.record_need("x")),
            ("distill", lambda: M.distill()),
            ("import_memory", lambda: M.import_memory("json", "/no/file")),
            ("recall", lambda: M.recall("x")),
            ("endorse", lambda: M.endorse("k-1")),
            ("verify_entry", lambda: M.verify_entry("k-1")),
            ("promote", lambda: M.promote()),
            ("review_promotions", lambda: M.review_promotions()),
            ("stale_report", lambda: M.stale_report()),
            ("export_markdown", lambda: M.export_markdown()),
            ("status", lambda: M.status()),
        ]
        for name, call in tools:
            r = json.loads(call())  # none may raise
            assert r.get("configured") is False, f"{name}: {r}"
            assert r.get("needs_setup") is True, f"{name}: {r}"
            assert "setup(" in r.get("next_step", ""), f"{name}: {r}"


def test_setup_configures_from_cold_start():
    with lab() as (root, origin, clones):
        cfgfile = unconfigured(root, "home_config.json")
        r = json.loads(M.setup(project_repo=clones["jonny"], agent_id="jonny"))
        assert r["status"] == "configured", r
        assert os.path.exists(cfgfile), "fallback config written"
        # scaffolding + gitignore in the project repo
        assert os.path.isdir(os.path.join(clones["jonny"], ".cambium"))
        gi = open(os.path.join(clones["jonny"], ".gitignore"),
                  encoding="utf-8").read()
        assert ".cambium" in gi, gi
        # no secrets in the written config — only paths/ids/flags
        conf = json.load(open(cfgfile, encoding="utf-8"))
        assert conf["CAMBIUM_AGENT_ID"] == "jonny" and conf["CAMBIUM_REPO"], conf
        assert not any(bad in k.upper() for k in conf
                       for bad in ("TOKEN", "SECRET", "PASSWORD", "KEY")), conf
        # server now resolves from the file with NO env repo/agent set
        assert "CAMBIUM_REPO" not in os.environ
        s = json.loads(M.status())
        assert s["configured"] is True and s["me"] == "jonny", s
        # and a real tool works end to end, immediately (no restart)
        assert json.loads(M.capture("first note after setup"))["status"] == "captured"


def test_setup_env_overrides_config_file():
    with lab() as (root, origin, clones):
        unconfigured(root, "home_config.json")
        M.setup(project_repo=clones["jonny"], agent_id="jonny")
        # env names a different agent — env must win over the file
        os.environ["CAMBIUM_AGENT_ID"] = "override-agent"
        s = json.loads(M.status())
        assert s["me"] == "override-agent", s
        assert s["config_source"]["CAMBIUM_AGENT_ID"] == "env", s
        assert s["config_source"]["CAMBIUM_REPO"] == "config-file", s


def test_setup_offers_org_commands_without_creating():
    with lab() as (root, origin, clones):
        unconfigured(root, "home_config.json")
        # a GitHub-style name that isn't cloned locally
        r = json.loads(M.setup(project_repo=clones["jonny"], agent_id="jonny",
                               org_repo="myorg/knowledge"))
        assert r["status"] == "configured", r
        org = r.get("org_repo", {})
        assert org.get("status") == "not_created", r
        cmds = " ".join(org.get("run_these_yourself", []))
        assert "gh repo create myorg/knowledge" in cmds, org
        # org scope stayed OFF (not recorded) → the org gap persists
        s = json.loads(M.status())
        assert any(g["setting"] == "CAMBIUM_ORG_REPO" for g in s["gaps"]), s
        assert "CAMBIUM_ORG_REPO" not in json.load(
            open(os.environ["CAMBIUM_CONFIG_FILE"], encoding="utf-8"))


def test_setup_wires_a_local_org_clone():
    with lab() as (root, origin, clones):
        org_bare, org_clone = setup_org_repo(root)
        unconfigured(root, "home_config.json")
        r = json.loads(M.setup(project_repo=clones["jonny"], agent_id="jonny",
                               org_repo=org_clone))
        assert r["status"] == "configured" and "org_repo" not in r, r
        s = json.loads(M.status())
        # org now configured -> no org gap, org scope reported
        assert not any(g["setting"] == "CAMBIUM_ORG_REPO" for g in s["gaps"]), s


def test_setup_rejects_non_git_and_missing_paths():
    with lab() as (root, origin, clones):
        unconfigured(root)
        assert "error" in json.loads(M.setup(project_repo="/no/such/dir",
                                             agent_id="jonny"))
        plain = os.path.join(root, "plain")          # a dir but not a git clone
        os.makedirs(plain, exist_ok=True)
        assert "error" in json.loads(M.setup(project_repo=plain, agent_id="jonny"))
        assert "error" in json.loads(M.setup(project_repo=clones["jonny"],
                                             agent_id="   "))


# --------------------------------------------------------------------------- #
# real MCP stdio transport — the server as a client actually runs it
# --------------------------------------------------------------------------- #
def _mcp_drive(server_env, calls, timeout=90):
    """Launch cambium as a real MCP stdio subprocess and make tool calls over
    JSON-RPC — exactly what an MCP client does. Bounded so a stdin-inheritance
    hang fails loudly instead of wedging CI."""
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(HERE, "cambium_server.py")],
        env=server_env,
    )

    async def run():
        results = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, args in calls:
                    res = await session.call_tool(name, arguments=args)
                    results.append(res.content[0].text if res.content else "")
        return results

    return asyncio.run(asyncio.wait_for(run(), timeout=timeout))


def test_mcp_transport_capture_distill_recall():
    with lab() as (root, origin, clones):
        seed_agentsync_branch(clones["stobie"], {"stobie": DONE_CLAIM})
        env = {k: v for k, v in os.environ.items() if v is not None}
        for k in CAMBIUM_ENV:
            env.pop(k, None)
        env["CAMBIUM_REPO"] = clones["jonny"]
        env["CAMBIUM_AGENT_ID"] = "jonny"
        env["CAMBIUM_CONFIG_FILE"] = os.path.join(root, "transport_config.json")
        out = _mcp_drive(env, [
            ("capture", {"content": "release train leaves fridays at noon",
                         "tags": "release process"}),
            ("distill", {}),
            ("recall", {"query": "when does the release train leave"}),
            ("status", {}),
        ])
        assert json.loads(out[0])["status"] == "captured", out[0]
        assert json.loads(out[1])["new_items"] == 1, out[1]
        rec = json.loads(out[2])
        assert rec["results"] and "fridays" in rec["results"][0]["content"], out[2]
        st = json.loads(out[3])
        assert st["scopes"]["local"]["total"] == 2, out[3]


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
TESTS = [
    test_capture_and_recall_local,
    test_recall_abstains_on_nonsense,
    test_capture_validation,
    test_record_need,
    test_distill_from_agentsync_done_claim,
    test_distill_from_context_keeper,
    test_distill_is_idempotent,
    test_distill_reports_missing_substrates,
    test_distill_normalizes_cp1252_mojibake_on_write,
    test_distill_reads_legacy_rationale_field,
    test_release_capture_is_off_by_default,
    test_release_capture_survives_reclaim_churn,
    test_release_capture_grabs_noted_claim_a_full_distill_would_miss,
    test_import_json_array_maps_to_items_with_provenance,
    test_import_jsonl_and_reimport_is_idempotent,
    test_import_dedupes_by_content_when_no_id,
    test_import_handles_malformed_and_missing_fields,
    test_import_rejects_unknown_source_and_missing_file,
    test_import_is_read_only_against_source,
    test_import_items_are_not_auto_promoted,
    test_optional_staleness_fields_absent_are_handled,
    test_capture_records_valid_while_premise,
    test_verify_entry_local_roundtrip,
    test_promotion_stamps_last_verified,
    test_verify_entry_reaches_team_scope,
    test_stale_report_orders_oldest_first_and_flags_never_verified,
    test_stale_report_older_than_days_and_project_filters,
    test_distill_release_includes_verification_prompt,
    test_promote_local_to_team_by_recalls,
    test_endorse_fast_tracks_promotion,
    test_promote_explicit_id_respects_threshold_and_force,
    test_team_recall_tracks_cross_project_usage,
    test_promote_team_to_org_requires_endorsement,
    test_org_promotion_blocks_project_specific_body,
    test_org_promotion_with_org_content_generalizes_and_preserves_example,
    test_org_promotion_force_ships_specific_body,
    test_org_promotion_allows_clean_universal_body,
    test_recall_surfaces_endorsed_as,
    test_review_promotions_flags_org_needs_generalization,
    test_generalize_org_item_in_place_and_clears_flag,
    test_generalize_falls_back_to_endorsement_note,
    test_generalize_is_idempotent,
    test_promote_to_org_via_pull_request,
    test_review_promotions_lists_eligible,
    test_export_markdown_publishes_grouped_with_provenance,
    test_export_normalizes_cp1252_mojibake,
    test_org_promotion_writes_knowledge_md_alongside_json,
    test_org_pr_mode_puts_knowledge_md_on_the_pr_branch,
    test_export_markdown_local_and_team_render_without_publishing,
    test_full_compound_growth_loop,
    test_team_cas_survives_concurrent_push,
    test_status_overview,
    test_status_reports_gaps_when_unconfigured,
    test_every_tool_fails_helpful_when_unconfigured,
    test_setup_configures_from_cold_start,
    test_setup_env_overrides_config_file,
    test_setup_offers_org_commands_without_creating,
    test_setup_wires_a_local_org_clone,
    test_setup_rejects_non_git_and_missing_paths,
    test_real_agentsync_integration,
    test_real_agentsync_release_capture,
    test_mcp_transport_capture_distill_recall,
]


def main():
    failures = 0
    for t in TESTS:
        name = t.__name__
        try:
            t()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001 - test runner
            failures += 1
            traceback.print_exc()
            print(f"FAIL  {name}: {e}")
    print()
    if failures:
        print(f"{failures}/{len(TESTS)} FAILED")
        sys.exit(1)
    print(f"ALL {len(TESTS)} TESTS PASS")


if __name__ == "__main__":
    main()
