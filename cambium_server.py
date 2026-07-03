#!/usr/bin/env python3
"""
cambium — the knowledge-lifecycle and federation MCP that bridges agentsync
(coordination events) and context-keeper (project memory) into compound,
org-wide knowledge growth.

The gap it closes
-----------------
agentsync knows WHAT happened (claims, finishes, notes, changed files).
context-keeper knows WHY (decisions, constraints, rationale). Neither:
  a) turns events into memory automatically         -> distill()
  b) lets ANY agent recall across projects/scopes   -> recall()
  c) graduates knowledge local -> team -> org as it
     earns trust                                    -> promote()

cambium is a composer, not another store to forget about. It reads agentsync's
coordination branch and context-keeper's .context/ files directly from the
substrate they already live in (git / the repo), and keeps its own items in the
same style: human-editable JSON, versioned in git.

Scopes and where they live
--------------------------
    local  <repo>/.cambium/knowledge.json      (yours; not shared)
    team   knowledge.json on a dedicated git branch of the shared repo
           (default branch name "cambium" — the agentsync pattern: CAS via
           push, private worktree under .git/, never touches your checkout)
    org    knowledge.json in a dedicated org knowledge repo (a separate clone)
           promotion lands there either directly or as a pull request

Trust model (what "earning promotion" means)
--------------------------------------------
Every item counts recalls (it was actually useful to an agent), endorsements
(a person/agent vouched), and the set of projects it was recalled from.
local -> team : recalls >= CAMBIUM_PROMOTE_RECALLS (default 3) OR an endorsement
team  -> org  : an endorsement is REQUIRED (recalls alone can't reach org) —
                the blast radius of bad org knowledge demands a deliberate vouch.
Promotion is reversible: items carry provenance and can be deprecated.

Config (environment, set in the MCP client config)
--------------------------------------------------
    CAMBIUM_REPO             absolute path to the project clone     (required)
    CAMBIUM_AGENT_ID         this agent's id, e.g. "jonny"          (required)
    CAMBIUM_REMOTE           git remote name                        (default: origin)
    CAMBIUM_TEAM_BRANCH      team-scope branch                      (default: cambium)
    CAMBIUM_AGENTSYNC_BRANCH agentsync coordination branch          (default: agentsync)
    CAMBIUM_ORG_REPO         path to the org knowledge repo clone   (optional)
    CAMBIUM_ORG_PR           "1" = promote to org via pull request  (default: direct push)
    CAMBIUM_PROMOTE_RECALLS  recalls needed for local->team         (default: 3)
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cambium")

KNOWLEDGE_FILE = "knowledge.json"
LOCAL_DIR = ".cambium"
PUSH_RETRIES = 5
RELEVANCE_FLOOR = 0.2  # below this, recall says "no confident match"

# Any single git/gh invocation is bounded so a stuck network call or an
# un-answerable credential prompt fails fast instead of hanging the MCP server.
GIT_TIMEOUT = int(os.environ.get("CAMBIUM_GIT_TIMEOUT", "25"))


def _noninteractive_env():
    """git env that refuses to block on a credential/login prompt (an MCP
    subprocess has no terminal to answer one)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
class ConfigError(RuntimeError):
    pass


def _cfg():
    repo = os.environ.get("CAMBIUM_REPO")
    agent = os.environ.get("CAMBIUM_AGENT_ID")
    if not repo or not agent:
        raise ConfigError(
            "CAMBIUM_REPO and CAMBIUM_AGENT_ID must be set in the MCP config."
        )
    repo = os.path.abspath(repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise ConfigError(f"{repo} is not a git repository (no .git directory).")
    org = os.environ.get("CAMBIUM_ORG_REPO", "")
    return {
        "repo": repo,
        "agent": agent,
        "project": os.path.basename(repo.rstrip("/\\")),
        "remote": os.environ.get("CAMBIUM_REMOTE", "origin"),
        "team_branch": os.environ.get("CAMBIUM_TEAM_BRANCH", "cambium"),
        "agentsync_branch": os.environ.get("CAMBIUM_AGENTSYNC_BRANCH", "agentsync"),
        "org_repo": os.path.abspath(org) if org else "",
        "org_pr": os.environ.get("CAMBIUM_ORG_PR", "") == "1",
        "promote_recalls": int(os.environ.get("CAMBIUM_PROMOTE_RECALLS", "3")),
        "worktree": os.path.join(repo, ".git", "cambium-wt"),
        "local_store": os.path.join(repo, LOCAL_DIR, KNOWLEDGE_FILE),
        "context_dir": os.path.join(repo, ".context"),
    }


# --------------------------------------------------------------------------- #
# git / gh plumbing (agentsync's hardened pattern: timeout, DEVNULL stdin)
# --------------------------------------------------------------------------- #
def _git(args, cwd, check=True):
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            env=_noninteractive_env(), timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,  # never inherit the MCP stdio pipe
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s — likely a "
            "stuck network call or an unanswerable credential prompt."
        )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}"
        )
    return p


def _gh(args, cwd=None, check=True):
    try:
        p = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "The GitHub CLI ('gh') is not installed or not on PATH. "
            "Install from https://cli.github.com and run `gh auth login`."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh {' '.join(args)} timed out after {GIT_TIMEOUT}s.")
    if check and p.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}"
        )
    return p


def _remote_has_branch(repo, remote, branch):
    p = _git(["ls-remote", "--heads", remote, branch], repo, check=False)
    return bool(p.stdout.strip())


def _default_remote_head(repo, remote):
    p = _git(
        ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"],
        repo, check=False,
    )
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    return f"{remote}/main"


def _show_file(repo, ref, path):
    """Read a file from a git ref without touching any working tree. Returns
    parsed JSON or None if the ref/file doesn't exist."""
    p = _git(["show", f"{ref}:{path}"], repo, check=False)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# local store
# --------------------------------------------------------------------------- #
def _empty_local():
    return {"items": [], "imported": {"context_keeper": [], "agentsync": []}}


def _read_local(cfg):
    path = cfg["local_store"]
    if not os.path.exists(path):
        return _empty_local()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_local()
    data.setdefault("items", [])
    data.setdefault("imported", {})
    data["imported"].setdefault("context_keeper", [])
    data["imported"].setdefault("agentsync", [])
    return data


def _write_local(cfg, data):
    os.makedirs(os.path.dirname(cfg["local_store"]), exist_ok=True)
    with open(cfg["local_store"], "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# --------------------------------------------------------------------------- #
# team store (knowledge.json on a dedicated branch — CAS via push)
# --------------------------------------------------------------------------- #
def _ensure_team_worktree(cfg):
    """Worktree at .git/cambium-wt checked out to the team branch, synced to
    the remote tip. Creates the branch on first use."""
    repo, wt = cfg["repo"], cfg["worktree"]
    remote, branch = cfg["remote"], cfg["team_branch"]
    _git(["fetch", remote, "--prune"], repo, check=False)

    if not os.path.isdir(wt):
        if _remote_has_branch(repo, remote, branch):
            _git(["worktree", "add", "-B", branch, wt, f"{remote}/{branch}"], repo)
        else:
            base = _default_remote_head(repo, remote)
            _git(["worktree", "add", "-b", branch, wt, base], repo)
            with open(os.path.join(wt, KNOWLEDGE_FILE), "w", encoding="utf-8") as f:
                json.dump({"items": []}, f, indent=2)
            _git(["add", KNOWLEDGE_FILE], wt)
            _git(["commit", "-m", "cambium: initialize team knowledge"], wt)
            _git(["push", "-u", remote, branch], wt)
        return

    if _remote_has_branch(repo, remote, branch):
        _git(["fetch", remote, branch], wt, check=False)
        _git(["reset", "--hard", f"{remote}/{branch}"], wt, check=False)


def _read_team_wt(cfg):
    path = os.path.join(cfg["worktree"], KNOWLEDGE_FILE)
    if not os.path.exists(path):
        return {"items": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"items": []}
    data.setdefault("items", [])
    return data


def _team_mutate(cfg, fn, message):
    """CAS write to the team store: fetch+reset, apply fn(data) (return False
    to abort as a no-op), commit, push; on rejected push resync and retry so a
    peer's concurrent write is observed, never clobbered."""
    wt, remote, branch = cfg["worktree"], cfg["remote"], cfg["team_branch"]
    for attempt in range(PUSH_RETRIES):
        _ensure_team_worktree(cfg)
        data = _read_team_wt(cfg)
        if fn(data) is False:
            return True  # nothing to do
        with open(os.path.join(wt, KNOWLEDGE_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _git(["add", KNOWLEDGE_FILE], wt)
        st = _git(["status", "--porcelain"], wt)
        if not st.stdout.strip():
            return True
        _git(["commit", "-m", message], wt)
        push = _git(["push", remote, branch], wt, check=False)
        if push.returncode == 0:
            return True
        _git(["reset", "--hard", f"{remote}/{branch}"], wt, check=False)
        time.sleep(0.4 * (attempt + 1))
    return False


def _read_team(cfg):
    """Fresh team items straight from the remote tip (read-only, no worktree
    mutation)."""
    repo, remote, branch = cfg["repo"], cfg["remote"], cfg["team_branch"]
    _git(["fetch", remote, branch], repo, check=False)
    data = _show_file(repo, f"{remote}/{branch}", KNOWLEDGE_FILE)
    return data.get("items", []) if isinstance(data, dict) else []


# --------------------------------------------------------------------------- #
# org store (a dedicated knowledge repo clone; direct push or PR)
# --------------------------------------------------------------------------- #
def _org_default_branch(cfg):
    head = _default_remote_head(cfg["org_repo"], "origin")
    return head.rsplit("/", 1)[-1]


def _read_org(cfg):
    if not cfg["org_repo"]:
        return []
    repo = cfg["org_repo"]
    _git(["fetch", "origin", "--prune"], repo, check=False)
    data = _show_file(repo, _default_remote_head(repo, "origin"), KNOWLEDGE_FILE)
    return data.get("items", []) if isinstance(data, dict) else []


def _org_sync(cfg):
    """Hard-sync the org clone's default branch to the remote tip. The org repo
    is a cambium-managed clone (document this!) — resetting it is deliberate."""
    repo = cfg["org_repo"]
    branch = _org_default_branch(cfg)
    _git(["fetch", "origin", "--prune"], repo, check=False)
    _git(["checkout", "-q", branch], repo, check=False)
    _git(["reset", "--hard", f"origin/{branch}"], repo, check=False)
    return branch


def _org_read_wt(cfg):
    path = os.path.join(cfg["org_repo"], KNOWLEDGE_FILE)
    if not os.path.exists(path):
        return {"items": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"items": []}
    data.setdefault("items", [])
    return data


def _org_add_direct(cfg, item):
    """CAS-append an item to the org store on its default branch."""
    repo = cfg["org_repo"]
    for attempt in range(PUSH_RETRIES):
        branch = _org_sync(cfg)
        data = _org_read_wt(cfg)
        if any(i["id"] == item["id"] for i in data["items"]):
            return True, None
        data["items"].append(item)
        with open(os.path.join(repo, KNOWLEDGE_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _git(["add", KNOWLEDGE_FILE], repo)
        _git(["commit", "-m", f"cambium: promote {item['id']} to org "
              f"({item['content'][:50]!r})"], repo)
        push = _git(["push", "origin", branch], repo, check=False)
        if push.returncode == 0:
            return True, None
        time.sleep(0.4 * (attempt + 1))
    return False, "org push kept losing the race"


def _org_add_pr(cfg, item):
    """Open a pull request adding the item to the org store. The PR review IS
    the org-level trust gate."""
    repo = cfg["org_repo"]
    base = _org_sync(cfg)
    pr_branch = f"cambium/promote-{item['id']}"
    _git(["checkout", "-qB", pr_branch, f"origin/{base}"], repo)
    data = _org_read_wt(cfg)
    if not any(i["id"] == item["id"] for i in data["items"]):
        data["items"].append(item)
        with open(os.path.join(repo, KNOWLEDGE_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    _git(["add", KNOWLEDGE_FILE], repo)
    _git(["commit", "-m", f"cambium: promote {item['id']} to org"], repo)
    push = _git(["push", "-f", "origin", pr_branch], repo, check=False)
    _git(["checkout", "-q", base], repo, check=False)
    if push.returncode != 0:
        return False, f"could not push PR branch: {push.stderr.strip()[:200]}"
    created = _gh(
        ["pr", "create", "--head", pr_branch, "--base", base,
         "--title", f"Promote knowledge: {item['content'][:60]}",
         "--body", f"cambium promotion of `{item['id']}`\n\n"
                   f"> {item['content']}\n\n"
                   f"why: {item.get('why') or '—'}\n"
                   f"trust: {json.dumps(item.get('trust', {}))}\n"
                   f"source: {json.dumps(item.get('source', {}))}"],
        cwd=repo, check=False,
    )
    if created.returncode == 0:
        url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
        return True, url
    view = _gh(["pr", "view", pr_branch, "--json", "url", "--jq", ".url"],
               cwd=repo, check=False)
    if view.returncode == 0 and view.stdout.strip():
        return True, view.stdout.strip()
    return False, f"PR creation failed: {created.stderr.strip()[:200]}"


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #
VALID_TYPES = ("memory", "need", "skill")


def _new_item(cfg, content, type_, kind, why, tags, source):
    return {
        "id": f"k-{uuid.uuid4().hex[:8]}",
        "type": type_,
        "kind": kind,
        "content": content,
        "why": why,
        "tags": tags,
        "scope": "local",
        "project": cfg["project"],
        "source": source,
        "created_by": cfg["agent"],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "active",
        "trust": {"recalls": 0, "endorsements": [], "projects": [cfg["project"]]},
    }


def _parse_tags(tags):
    if not tags:
        return []
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return [t.strip() for t in str(tags).replace(",", " ").split() if t.strip()]


def _tokens(text):
    return {w for w in "".join(
        c.lower() if c.isalnum() else " " for c in (text or "")
    ).split() if len(w) > 1}


def _score(item, q_tokens):
    """Fraction of query tokens the item matches; tags and kind count double,
    and tokens >=3 chars match by substring either way (jwt~jwts, hash~hashing).
    Deterministic, dependency-free — the semantic upgrade is a later swap."""
    if not q_tokens:
        return 0.0
    body = _tokens(item.get("content", "")) | _tokens(item.get("why", ""))
    tagset = {t.lower() for t in item.get("tags", [])} | {item.get("kind", "").lower()}
    hits = 0.0
    for tok in q_tokens:
        if tok in tagset:
            hits += 2.0
        elif tok in body or any(tok in t for t in tagset):
            hits += 1.0
        elif len(tok) >= 3 and any(
            (tok in w or w in tok) for w in body if len(w) >= 3
        ):
            hits += 1.0
    return min(1.0, hits / len(q_tokens))


def _eligible_team(cfg, item):
    t = item.get("trust", {})
    return (t.get("recalls", 0) >= cfg["promote_recalls"]
            or len(t.get("endorsements", [])) >= 1)


def _eligible_org(item):
    return len(item.get("trust", {}).get("endorsements", [])) >= 1


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def capture(content: str, type: str = "memory", kind: str = "note",
            why: str = "", tags: str = "") -> str:
    """Save a knowledge item to your LOCAL scope: a fact, design note, gotcha,
    or troubleshooting step worth remembering. This is the manual capture path;
    distill() is the automatic one.

    type : memory | need | skill
    kind : freeform subtype (note, decision, constraint, runbook, ...)
    why  : the rationale — makes the item far more useful at recall time
    tags : comma/space-separated keywords (boost recall matching)"""
    if type not in VALID_TYPES:
        return json.dumps({"error": f"type must be one of {', '.join(VALID_TYPES)}"})
    if not content.strip():
        return json.dumps({"error": "content must not be empty"})
    cfg = _cfg()
    data = _read_local(cfg)
    item = _new_item(cfg, content.strip(), type, kind, why.strip(),
                     _parse_tags(tags), {"system": "manual", "ref": ""})
    data["items"].append(item)
    _write_local(cfg, data)
    return json.dumps({"status": "captured", "item": item}, indent=2)


@mcp.tool()
def record_need(content: str, why: str = "", tags: str = "") -> str:
    """Record a NEED — something missing, wanted, or blocking (a first-class
    citizen alongside memories: 'we need staging seeds', 'docs for X are
    missing'). Needs surface in recall like any knowledge and can be promoted
    so the team/org sees recurring wants."""
    return capture(content, type="need", kind="need", why=why, tags=tags)


@mcp.tool()
def distill() -> str:
    """Automatically turn work that already happened into knowledge. Reads two
    substrates natively — no export step, no copy-paste:

    1. agentsync: every DONE claim on the coordination branch (task + partner
       note + changed files) becomes an 'outcome' memory. The note your partner
       left for reconciliation is exactly the knowledge worth keeping.
    2. context-keeper: every active decision and constraint in .context/
       becomes a memory with its rationale, preserving the dec-NNN/con-NNN
       provenance.

    Idempotent — each source record imports at most once; re-run freely (e.g.
    from a session-end or post-commit hook for passive capture)."""
    cfg = _cfg()
    data = _read_local(cfg)
    imported_as = set(data["imported"]["agentsync"])
    imported_ck = set(data["imported"]["context_keeper"])
    new_items = []

    # --- source 1: agentsync coordination branch -------------------------- #
    repo, remote = cfg["repo"], cfg["remote"]
    as_branch = cfg["agentsync_branch"]
    agentsync_seen = False
    _git(["fetch", remote, as_branch], repo, check=False)
    claims_doc = _show_file(repo, f"{remote}/{as_branch}", "claims.json")
    if isinstance(claims_doc, dict):
        agentsync_seen = True
        for agent, claim in claims_doc.get("claims", {}).items():
            if claim.get("status") != "done":
                continue
            note = claim.get("note") or ""
            task = claim.get("task") or "task"
            key = hashlib.sha1(
                f"{agent}|{task}|{claim.get('branch','')}|{note}".encode()
            ).hexdigest()[:12]
            if key in imported_as:
                continue
            files = [c.get("path") for c in (claim.get("changed_files") or [])
                     if isinstance(c, dict) and c.get("path")]
            content = f"[{agent}] finished '{task}'"
            if note:
                content += f": {note}"
            if files:
                content += f" (files: {', '.join(files[:8])}" + \
                           (", …)" if len(files) > 8 else ")")
            item = _new_item(
                cfg, content, "memory", "outcome",
                note, ["agentsync", agent] + [t for t in _parse_tags(task)][:4],
                {"system": "agentsync", "ref": f"{agent}:{claim.get('branch','')}"},
            )
            data["items"].append(item)
            data["imported"]["agentsync"].append(key)
            new_items.append(item)

    # --- source 2: context-keeper .context/ ------------------------------- #
    ck_seen = False
    for fname, kind, text_f, why_f in (
        ("decisions.json", "decision", "summary", "why_chosen"),
        ("constraints.json", "constraint", "rule", "reason"),
    ):
        path = os.path.join(cfg["context_dir"], fname)
        if not os.path.exists(path):
            continue
        ck_seen = True
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for e in entries if isinstance(entries, list) else []:
            eid = e.get("id", "")
            if not eid or eid in imported_ck or e.get("status") not in (None, "active"):
                continue
            content = e.get(text_f) or ""
            if not content:
                continue
            item = _new_item(
                cfg, content, "memory", kind,
                e.get(why_f) or "",
                _parse_tags(e.get("tags", [])) + ["context-keeper"],
                {"system": "context-keeper", "ref": eid},
            )
            data["items"].append(item)
            data["imported"]["context_keeper"].append(eid)
            new_items.append(item)

    _write_local(cfg, data)
    return json.dumps(
        {
            "status": "distilled",
            "new_items": len(new_items),
            "sources": {
                "agentsync": "read" if agentsync_seen else "no coordination branch found",
                "context_keeper": "read" if ck_seen else "no .context/ store found",
            },
            "items": [{"id": i["id"], "kind": i["kind"], "content": i["content"]}
                      for i in new_items],
        },
        indent=2,
    )


@mcp.tool()
def recall(query: str, scope: str = "auto", limit: int = 5) -> str:
    """Search knowledge across scopes and return the best matches. THE read
    endpoint for every agent type — a coding agent, a Slack KB bot, an SRE bot
    — they all ask here, so knowledge captured once serves them all.

    scope : auto (local+team+org, the default) | local | team | org
    limit : max results

    Every returned item's recall counter is incremented (local directly, team
    best-effort via the shared branch) — usage is the trust signal promotion
    feeds on. If nothing clears the relevance floor the response says
    no_confident_match: true — don't present weak matches as established fact."""
    cfg = _cfg()
    limit = max(1, min(int(limit), 25))
    q = _tokens(query)
    scopes = ["local", "team", "org"] if scope == "auto" else [scope]
    if scope not in ("auto", "local", "team", "org"):
        return json.dumps({"error": "scope must be auto | local | team | org"})

    pool = []
    local_data = None
    if "local" in scopes:
        local_data = _read_local(cfg)
        pool += [("local", i) for i in local_data["items"]]
    if "team" in scopes:
        pool += [("team", i) for i in _read_team(cfg)]
    if "org" in scopes:
        pool += [("org", i) for i in _read_org(cfg)]

    scored = [
        (s, _score(i, q), i) for s, i in pool if i.get("status") == "active"
    ]
    scored.sort(key=lambda t: t[1], reverse=True)
    top = [t for t in scored[:limit] if t[1] > 0]

    # usage tracking — best-effort, never fails the recall
    hit_local = {i["id"] for s, _, i in top if s == "local"}
    hit_team = {i["id"] for s, _, i in top if s == "team"}
    if hit_local and local_data is not None:
        for i in local_data["items"]:
            if i["id"] in hit_local:
                i["trust"]["recalls"] += 1
                i["updated_at"] = _now()
        _write_local(cfg, local_data)
    if hit_team:
        def bump(data):
            changed = False
            for i in data["items"]:
                if i["id"] in hit_team:
                    i.setdefault("trust", {}).setdefault("recalls", 0)
                    i["trust"]["recalls"] += 1
                    projs = i["trust"].setdefault("projects", [])
                    if cfg["project"] not in projs:
                        projs.append(cfg["project"])  # cross-project signal
                    i["updated_at"] = _now()
                    changed = True
            return None if changed else False
        try:
            _team_mutate(cfg, bump, f"cambium: usage by {cfg['agent']}")
        except Exception:
            pass  # tracking must never break recall

    results = [
        {"scope": s, "relevance": round(sc, 3), **{
            k: i[k] for k in ("id", "type", "kind", "content", "why", "tags",
                              "project", "trust", "source") if k in i}}
        for s, sc, i in top
    ]
    out = {
        "query": query,
        "results": results,
        "top_relevance": round(top[0][1], 3) if top else 0.0,
    }
    if not top or top[0][1] < RELEVANCE_FLOOR:
        out["no_confident_match"] = True
        out["guidance"] = (
            "No stored knowledge confidently matches this query. Do not present "
            "these results as established fact."
        )
    return json.dumps(out, indent=2)


@mcp.tool()
def endorse(item_id: str, note: str = "") -> str:
    """Vouch for an item — the strong trust signal. One endorsement fast-tracks
    local->team promotion and is REQUIRED for team->org (usage alone never
    reaches org; someone has to deliberately say 'this is right')."""
    cfg = _cfg()
    stamp = {"by": cfg["agent"], "at": _now(), "note": note}

    data = _read_local(cfg)
    for i in data["items"]:
        if i["id"] == item_id:
            i["trust"]["endorsements"].append(stamp)
            i["updated_at"] = _now()
            _write_local(cfg, data)
            return json.dumps({"status": "endorsed", "scope": "local",
                               "item": i}, indent=2)

    found = {}
    def add(team_data):
        for i in team_data["items"]:
            if i["id"] == item_id:
                i.setdefault("trust", {}).setdefault("endorsements", []).append(stamp)
                i["updated_at"] = _now()
                found["item"] = i
                return None
        return False
    ok = _team_mutate(cfg, add, f"cambium: {cfg['agent']} endorses {item_id}")
    if found.get("item"):
        return json.dumps({"status": "endorsed", "scope": "team",
                           "item": found["item"]}, indent=2)
    if not ok:
        return json.dumps({"status": "retry_exhausted"})
    return json.dumps({"error": f"No item '{item_id}' in local or team scope."})


@mcp.tool()
def promote(item_id: str = "", to_scope: str = "", force: bool = False) -> str:
    """Graduate knowledge up a scope as it earns trust — the compound-growth
    step. With no arguments, scans your local items and promotes every one
    that qualifies to team. With an item_id, promotes that item one level
    (local->team, or team->org with to_scope="org").

    Thresholds: local->team needs recalls >= CAMBIUM_PROMOTE_RECALLS or one
    endorsement; team->org always needs an endorsement (force=True overrides,
    use deliberately). Org promotion lands as a direct push, or as a pull
    request when CAMBIUM_ORG_PR=1 — the PR review is the org trust gate."""
    cfg = _cfg()

    # ---- explicit team -> org ------------------------------------------- #
    if item_id and to_scope == "org":
        if not cfg["org_repo"]:
            return json.dumps({"error": "CAMBIUM_ORG_REPO is not configured."})
        team_items = _read_team(cfg)
        src = next((i for i in team_items if i["id"] == item_id), None)
        if not src:
            return json.dumps({"error": f"No item '{item_id}' in team scope. "
                               "Promote local items to team first."})
        if not force and not _eligible_org(src):
            return json.dumps({
                "status": "not_eligible",
                "message": "team->org requires at least one endorsement "
                           "(endorse() it, or force=True).",
            })
        item = dict(src)
        item["scope"] = "org"
        item["updated_at"] = _now()
        if cfg["org_pr"]:
            ok, url = _org_add_pr(cfg, item)
            if not ok:
                return json.dumps({"status": "failed", "detail": url})
            def mark(data):
                for i in data["items"]:
                    if i["id"] == item_id:
                        i["promotion"] = {"pr": url, "at": _now()}
                        return None
                return False
            _team_mutate(cfg, mark, f"cambium: org PR opened for {item_id}")
            return json.dumps({"status": "pr_opened", "pr_url": url,
                               "note": "team copy stays until the PR merges"},
                              indent=2)
        ok, err = _org_add_direct(cfg, item)
        if not ok:
            return json.dumps({"status": "failed", "detail": err})
        def remove(data):
            before = len(data["items"])
            data["items"] = [i for i in data["items"] if i["id"] != item_id]
            return None if len(data["items"]) != before else False
        _team_mutate(cfg, remove, f"cambium: {item_id} promoted to org")
        return json.dumps({"status": "promoted", "to": "org",
                           "item": item}, indent=2)

    # ---- local -> team (single or scan) ----------------------------------- #
    data = _read_local(cfg)
    if item_id:
        candidates = [i for i in data["items"] if i["id"] == item_id]
        if not candidates:
            return json.dumps({"error": f"No local item '{item_id}'."})
        if not force and not _eligible_team(cfg, candidates[0]):
            t = candidates[0]["trust"]
            return json.dumps({
                "status": "not_eligible",
                "message": f"needs recalls >= {cfg['promote_recalls']} "
                           f"(has {t.get('recalls', 0)}) or an endorsement "
                           f"(has {len(t.get('endorsements', []))}). "
                           "Use force=True to override.",
            })
    else:
        candidates = [i for i in data["items"]
                      if i.get("status") == "active" and _eligible_team(cfg, i)]
        if not candidates:
            return json.dumps({"status": "none_eligible",
                               "message": "No local items meet the promotion "
                               "threshold yet. See review_promotions()."})

    moved = []
    for c in candidates:
        c["scope"] = "team"
        c["updated_at"] = _now()
    ids = {c["id"] for c in candidates}

    def add(team_data):
        have = {i["id"] for i in team_data["items"]}
        for c in candidates:
            if c["id"] not in have:
                team_data["items"].append(c)
        return None
    if not _team_mutate(cfg, add,
                        f"cambium: {cfg['agent']} promotes {len(candidates)} "
                        "item(s) to team"):
        return json.dumps({"status": "retry_exhausted"})
    data["items"] = [i for i in data["items"] if i["id"] not in ids]
    _write_local(cfg, data)
    moved = [{"id": c["id"], "content": c["content"]} for c in candidates]
    return json.dumps({"status": "promoted", "to": "team", "items": moved},
                      indent=2)


@mcp.tool()
def review_promotions() -> str:
    """What's ready to move up? Lists local items eligible for team, team items
    eligible for org (endorsed), and org PRs already opened. The human-readable
    checkpoint before running promote()."""
    cfg = _cfg()
    local = _read_local(cfg)["items"]
    team = _read_team(cfg)
    def brief(i):
        t = i.get("trust", {})
        return {"id": i["id"], "content": i["content"][:100],
                "recalls": t.get("recalls", 0),
                "endorsements": len(t.get("endorsements", [])),
                "projects": t.get("projects", [])}
    return json.dumps(
        {
            "threshold": {"team_recalls": cfg["promote_recalls"],
                          "org": "1+ endorsement"},
            "eligible_for_team": [brief(i) for i in local
                                  if i.get("status") == "active"
                                  and _eligible_team(cfg, i)],
            "eligible_for_org": [brief(i) for i in team
                                 if i.get("status") == "active"
                                 and _eligible_org(i)
                                 and "promotion" not in i],
            "org_prs_pending": [{"id": i["id"], "pr": i["promotion"]["pr"]}
                                for i in team if i.get("promotion")],
            "org_configured": bool(cfg["org_repo"]),
        },
        indent=2,
    )


@mcp.tool()
def status() -> str:
    """Overview: item counts per scope and type, distill watermarks, and which
    substrates (agentsync branch, .context/, org repo) are actually wired up.
    Run this first when something looks off."""
    cfg = _cfg()
    local = _read_local(cfg)
    team = _read_team(cfg)
    org = _read_org(cfg)

    def count(items):
        by_type = {}
        for i in items:
            by_type[i.get("type", "?")] = by_type.get(i.get("type", "?"), 0) + 1
        return {"total": len(items), "by_type": by_type}

    return json.dumps(
        {
            "me": cfg["agent"],
            "project": cfg["project"],
            "scopes": {"local": count(local["items"]), "team": count(team),
                       "org": count(org) if cfg["org_repo"] else "not configured"},
            "imported": {"context_keeper": len(local["imported"]["context_keeper"]),
                         "agentsync": len(local["imported"]["agentsync"])},
            "substrates": {
                "agentsync_branch": cfg["agentsync_branch"],
                "context_dir": os.path.isdir(cfg["context_dir"]),
                "team_branch": cfg["team_branch"],
                "org_repo": cfg["org_repo"] or None,
                "org_mode": "pull-request" if cfg["org_pr"] else "direct-push",
            },
            "promote_threshold_recalls": cfg["promote_recalls"],
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
