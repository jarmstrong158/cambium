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
    "CAMBIUM_RELEASE_CAPTURE",
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
    for k, v in extra.items():
        os.environ[k] = v


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
        r = json.loads(M.promote(item_id=item_id, to_scope="org"))
        assert r["status"] == "promoted", r
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
    test_promote_local_to_team_by_recalls,
    test_endorse_fast_tracks_promotion,
    test_promote_explicit_id_respects_threshold_and_force,
    test_team_recall_tracks_cross_project_usage,
    test_promote_team_to_org_requires_endorsement,
    test_promote_to_org_via_pull_request,
    test_review_promotions_lists_eligible,
    test_full_compound_growth_loop,
    test_team_cas_survives_concurrent_push,
    test_status_overview,
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
