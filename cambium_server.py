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
    CAMBIUM_RELEASE_CAPTURE  "1" = capture agentsync claims at the
                             done/released transition, not only when a
                             full distill happens to catch them live
                                                                    (default: off)
"""

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cambium")

KNOWLEDGE_FILE = "knowledge.json"
KNOWLEDGE_MD = "KNOWLEDGE.md"   # human-readable render of a knowledge store
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
#
# MCP servers can't initiate a conversation, so cambium can't pop a setup
# wizard. Instead the server teaches whoever touches it how to finish setup:
# every tool that hits missing config returns structured guidance (what's set,
# what's missing, what each gap costs, and the exact setup() call that fixes it)
# rather than a bare env error. Config resolves per-key from the environment
# first, then a local fallback file setup() writes — env always wins.
# --------------------------------------------------------------------------- #
class ConfigError(RuntimeError):
    pass


# The settings cambium understands, with the plain-terms cost of leaving each
# unset. Ordered required-first. gap-fixing always routes back through setup().
_CONFIG_KEYS = ("CAMBIUM_REPO", "CAMBIUM_AGENT_ID", "CAMBIUM_ORG_REPO",
                "CAMBIUM_TEAM_BRANCH", "CAMBIUM_ORG_PR", "CAMBIUM_RELEASE_CAPTURE",
                "CAMBIUM_PROMOTE_RECALLS", "CAMBIUM_REMOTE",
                "CAMBIUM_AGENTSYNC_BRANCH")


def _config_file():
    """Path to the local fallback config. Lives in the user's home (outside any
    repo, so it is never committed); CAMBIUM_CONFIG_FILE overrides it."""
    override = os.environ.get("CAMBIUM_CONFIG_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".cambium", "config.json")


def _load_config_file():
    """The fallback config as a dict, or {} if absent/unreadable. Never raises."""
    try:
        with open(_config_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_config_file(conf):
    path = _config_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)


def _resolve(name, file_cfg, default=None):
    """Env wins, then the fallback file, then the default. Empty string counts
    as unset, matching the original get(..., '') behaviour."""
    v = os.environ.get(name)
    if v is not None and v != "":
        return v
    fv = file_cfg.get(name)
    if fv is not None and fv != "":
        return fv if isinstance(fv, str) else str(fv)
    return default


def _config_source(name, file_cfg):
    if os.environ.get(name):
        return "env"
    if file_cfg.get(name) not in (None, ""):
        return "config-file"
    return "unset"


def _abspath(p):
    return os.path.abspath(os.path.expanduser(p)) if p else ""


def _cfg():
    """Resolved config dict, or ConfigError if a required setting is missing or
    the repo isn't a git clone. Tools call this via _require_cfg() so the error
    becomes helpful guidance instead of a raised exception."""
    file_cfg = _load_config_file()
    repo = _resolve("CAMBIUM_REPO", file_cfg)
    agent = _resolve("CAMBIUM_AGENT_ID", file_cfg)
    if not repo or not agent:
        missing = [n for n, v in (("CAMBIUM_REPO", repo),
                                  ("CAMBIUM_AGENT_ID", agent)) if not v]
        raise ConfigError("cambium is not configured: missing "
                          + ", ".join(missing))
    repo = _abspath(repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise ConfigError(f"{repo} is not a git repository (no .git directory).")
    org = _resolve("CAMBIUM_ORG_REPO", file_cfg, "")
    return {
        "repo": repo,
        "agent": agent,
        "project": os.path.basename(repo.rstrip("/\\")),
        "remote": _resolve("CAMBIUM_REMOTE", file_cfg, "origin"),
        "team_branch": _resolve("CAMBIUM_TEAM_BRANCH", file_cfg, "cambium"),
        "agentsync_branch": _resolve("CAMBIUM_AGENTSYNC_BRANCH", file_cfg,
                                     "agentsync"),
        "org_repo": _abspath(org),
        "org_pr": _resolve("CAMBIUM_ORG_PR", file_cfg, "") == "1",
        "release_capture": _resolve("CAMBIUM_RELEASE_CAPTURE", file_cfg, "") == "1",
        "promote_recalls": int(_resolve("CAMBIUM_PROMOTE_RECALLS", file_cfg, "3")
                               or "3"),
        "worktree": os.path.join(repo, ".git", "cambium-wt"),
        "local_store": os.path.join(repo, LOCAL_DIR, KNOWLEDGE_FILE),
        "context_dir": os.path.join(repo, ".context"),
    }


def _config_state():
    """Structured config state that NEVER raises: what's set, what's missing,
    what each gap costs in plain terms, and the exact setup() call that fixes it.
    Powers status() and the fail-helpful path so any agent that touches an
    unconfigured cambium can offer setup conversationally from the response."""
    file_cfg = _load_config_file()
    repo = _resolve("CAMBIUM_REPO", file_cfg)
    agent = _resolve("CAMBIUM_AGENT_ID", file_cfg)
    org = _resolve("CAMBIUM_ORG_REPO", file_cfg, "")
    repo_is_git = bool(repo) and os.path.isdir(os.path.join(_abspath(repo), ".git"))

    setup_hint = ('setup(project_repo="/abs/path/to/your/clone", '
                  'agent_id="your-id")')
    gaps = []
    if not repo:
        gaps.append({"setting": "CAMBIUM_REPO",
                     "cost": "no project repo → cambium has no substrate to read "
                             "or write; every tool is unavailable",
                     "fix": setup_hint})
    elif not repo_is_git:
        gaps.append({"setting": "CAMBIUM_REPO",
                     "cost": f"{_abspath(repo)} is not a git repository → cambium "
                             "stores knowledge in git and needs a real clone",
                     "fix": 'setup(project_repo="/abs/path/to/a/git/clone", '
                            'agent_id="your-id")'})
    if not agent:
        gaps.append({"setting": "CAMBIUM_AGENT_ID",
                     "cost": "no agent identity → captures, endorsements and "
                             "promotions can't be attributed to anyone",
                     "fix": setup_hint})
    if not org:
        gaps.append({"setting": "CAMBIUM_ORG_REPO",
                     "cost": "org scope off → promotions stop at team; org-wide "
                             "recall is unavailable",
                     "fix": 'setup(project_repo="…", agent_id="…", '
                            'org_repo="owner/knowledge or /abs/path/to/clone")'})

    configured = bool(repo and agent and repo_is_git)
    state = {
        "configured": configured,
        "me": agent or None,
        "project": os.path.basename(_abspath(repo).rstrip("/\\")) if repo else None,
        "config_source": {n: _config_source(n, file_cfg) for n in _CONFIG_KEYS},
        "config_file": _config_file(),
        "config_file_exists": os.path.exists(_config_file()),
        "gaps": gaps,
    }
    if not configured:
        state["needs_setup"] = True
        state["next_step"] = setup_hint
        state["guidance"] = (
            "cambium isn't configured yet. Offer the user setup: call setup() "
            "with the absolute path to their project git clone and an agent id. "
            "Environment variables override the config file when both are set.")
    return state


def _require_cfg():
    """(_cfg(), None) when configured, else (None, helpful-guidance-JSON). Lets a
    tool fail helpful in two lines instead of raising a bare env error."""
    try:
        return _cfg(), None
    except ConfigError:
        return None, json.dumps(_config_state(), indent=2)


# --------------------------------------------------------------------------- #
# git / gh plumbing (agentsync's hardened pattern: timeout, DEVNULL stdin)
# --------------------------------------------------------------------------- #
def _git(args, cwd, check=True):
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            # Decode git output as UTF-8 regardless of the host locale — on a
            # cp1252 (Windows) locale, text=True would mis-decode UTF-8 bytes and
            # turn em dashes / curly quotes into mojibake on read.
            encoding="utf-8", errors="replace",
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
    # imported.agentsync        : sha1 watermarks — the single dedupe path
    # imported.agentsync_last   : last-seen claim per agent, for release-time
    #                             transition detection (see distill())
    # imported.import           : watermarks for external memory imports
    return {"items": [],
            "imported": {"context_keeper": [], "agentsync": [],
                         "agentsync_last": {}, "import": []}}


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
    data["imported"].setdefault("agentsync_last", {})
    data["imported"].setdefault("import", [])
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
        # keep the human-readable render current, committed alongside the JSON
        _write_knowledge_md(repo, data["items"])
        _git(["add", KNOWLEDGE_FILE, KNOWLEDGE_MD], repo)
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
    # render KNOWLEDGE.md onto the same PR branch so review sees both together
    _write_knowledge_md(repo, data["items"])
    _git(["add", KNOWLEDGE_FILE, KNOWLEDGE_MD], repo)
    if _git(["status", "--porcelain"], repo).stdout.strip():
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


_ORG_GENERALIZE_BRANCH = "cambium/generalize"


def _org_mutate_direct(cfg, fn, message):
    """CAS in-place edit of the org store on its default branch: sync, apply
    fn(data) (return False to abort as no-op), re-render md, commit both, push;
    retry on a rejected push. The edit counterpart of _org_add_direct."""
    repo = cfg["org_repo"]
    for attempt in range(PUSH_RETRIES):
        branch = _org_sync(cfg)
        data = _org_read_wt(cfg)
        if fn(data) is False:
            return True, "no change"
        with open(os.path.join(repo, KNOWLEDGE_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _write_knowledge_md(repo, data["items"])
        _git(["add", KNOWLEDGE_FILE, KNOWLEDGE_MD], repo)
        if not _git(["status", "--porcelain"], repo).stdout.strip():
            return True, "already current"
        _git(["commit", "-m", message], repo)
        if _git(["push", "origin", branch], repo, check=False).returncode == 0:
            return True, "pushed"
        time.sleep(0.4 * (attempt + 1))
    return False, "org push kept losing the race"


def _org_mutate_pr(cfg, fn, message):
    """In-place org edit landed on a single shared PR branch (not the default
    branch), so repeated edits accumulate into ONE reviewable PR — the same
    'review is the gate' contract as promote's PR mode, without a PR per item."""
    repo = cfg["org_repo"]
    base = _org_sync(cfg)
    br = _ORG_GENERALIZE_BRANCH
    # start the branch from the PR tip if it exists, else from base
    if _git(["ls-remote", "--heads", "origin", br], repo,
            check=False).stdout.strip():
        _git(["fetch", "origin", br], repo, check=False)
        _git(["checkout", "-qB", br, f"origin/{br}"], repo)
    else:
        _git(["checkout", "-qB", br, f"origin/{base}"], repo)
    data = _org_read_wt(cfg)
    if fn(data) is False:
        _git(["checkout", "-q", base], repo, check=False)
        return True, "no change", None
    with open(os.path.join(repo, KNOWLEDGE_FILE), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    _write_knowledge_md(repo, data["items"])
    _git(["add", KNOWLEDGE_FILE, KNOWLEDGE_MD], repo)
    if _git(["status", "--porcelain"], repo).stdout.strip():
        _git(["commit", "-m", message], repo)
    push = _git(["push", "origin", br], repo, check=False)
    if push.returncode != 0:
        _git(["checkout", "-q", base], repo, check=False)
        return False, f"could not push PR branch: {push.stderr.strip()[:200]}", None
    created = _gh(["pr", "create", "--head", br, "--base", base,
                   "--title", "cambium: generalize org items for org readership",
                   "--body", "Restates project-specific org bodies as the "
                   "cross-project rule (concrete body kept as `example`). "
                   "Opened/updated by cambium generalize()."],
                  cwd=repo, check=False)
    url = ""
    if created.returncode == 0 and created.stdout.strip():
        url = created.stdout.strip().splitlines()[-1]
    else:
        view = _gh(["pr", "view", br, "--json", "url", "--jq", ".url"],
                   cwd=repo, check=False)
        if view.returncode == 0:
            url = view.stdout.strip()
    _git(["checkout", "-q", base], repo, check=False)
    return True, "pr", url


# --------------------------------------------------------------------------- #
# human-readable export — render a knowledge store to KNOWLEDGE.md
# --------------------------------------------------------------------------- #
def _demojibake(s):
    """Repair the classic cp1252 mojibake where UTF-8 bytes were decoded as
    Windows-1252 (em dashes / en dashes / curly quotes / ellipses come back as
    'â€"', 'â€™', …). Guarded: only re-decodes when a tell-tale lead byte is
    present AND the round-trip is clean, so correct text is never corrupted."""
    if not isinstance(s, str) or not s:
        return s
    if "Ã" not in s and "â" not in s:  # Ã / â — the mojibake tell
        return s
    try:
        repaired = s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s  # not this mojibake (chars outside cp1252, or not valid UTF-8)
    return repaired


def _oneline(s):
    """Clean, single-line text for a markdown cell: demojibake + collapse space."""
    return " ".join(_demojibake(s or "").split())


def _provenance(item):
    """Where this knowledge came from, in human terms: context-keeper dec-NNN,
    an agentsync claim, a manual capture, or an import."""
    src = item.get("source", {}) or {}
    system = src.get("system") or "unknown"
    ref = _oneline(src.get("ref") or "")
    if src.get("imported"):
        return f"imported from {system}" + (f" ({ref})" if ref else "")
    if system == "context-keeper":
        return f"context-keeper {ref}" if ref else "context-keeper"
    if system == "agentsync":
        return f"agentsync claim {ref}" if ref else "agentsync"
    if system == "manual":
        return "manual capture"
    return (f"{system} {ref}").strip()


def _promoted_date(item):
    """The date this item was promoted, YYYY-MM-DD. Prefers the PR-promotion
    stamp, then last_verified (promotion sets it), then updated_at."""
    raw = ((item.get("promotion") or {}).get("at")
           or item.get("last_verified") or item.get("updated_at") or "")
    return raw[:10] if isinstance(raw, str) and raw else ""


_SCOPE_ORDER = {"local": 0, "team": 1, "org": 2}


def _render_markdown(items, title="Knowledge"):
    """Render knowledge items to KNOWLEDGE.md text: grouped by scope then
    project, each item showing summary, kind, provenance, recall count and
    promoted date. Deterministic ordering (most-recalled first) so re-exports
    produce stable diffs. All text is demojibake-cleaned."""
    lines = [f"# {title}", "",
             "_Generated by cambium from `knowledge.json` — do not edit by hand; "
             "it is overwritten on the next export._", ""]
    active = [i for i in items if i.get("status", "active") == "active"]
    if not active:
        lines += ["_No active knowledge items yet._", ""]
        return "\n".join(lines)

    by_scope = {}
    for i in active:
        by_scope.setdefault(i.get("scope", "local"), {}).setdefault(
            i.get("project") or "—", []).append(i)

    for scope in sorted(by_scope, key=lambda s: (_SCOPE_ORDER.get(s, 9), s)):
        lines += [f"## {scope} scope", ""]
        if scope == "org":
            lines += ["_Project headings below mark where each item was learned "
                      "(its provenance); org-scope knowledge applies across "
                      "projects, not only to its origin._", ""]
        projects = by_scope[scope]
        for project in sorted(projects):
            lines += [f"### {project}", ""]
            entries = sorted(
                projects[project],
                key=lambda i: (-i.get("trust", {}).get("recalls", 0),
                               _oneline(i.get("content", ""))))
            for i in entries:
                summary = _oneline(i.get("content", "")) or "(no summary)"
                lines.append(f"- **{summary}**")
                lines.append(f"  - kind: `{i.get('kind', 'note')}`")
                lines.append(f"  - provenance: {_provenance(i)}")
                lines.append(f"  - recalls: "
                             f"{i.get('trust', {}).get('recalls', 0)}")
                lines.append(f"  - promoted: {_promoted_date(i) or '—'}")
                why = _oneline(i.get("why", ""))
                if why:
                    lines.append(f"  - why: {why}")
                vw = _oneline(i.get("valid_while", ""))
                if vw:
                    lines.append(f"  - valid while: {vw}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_knowledge_md(repo_dir, items, title="Knowledge"):
    """Render items and write KNOWLEDGE.md into repo_dir. Returns the markdown."""
    md = _render_markdown(items, title)
    with open(os.path.join(repo_dir, KNOWLEDGE_MD), "w", encoding="utf-8") as f:
        f.write(md)
    return md


def _org_publish_markdown(cfg):
    """(Re)render the org repo's KNOWLEDGE.md from its knowledge.json and push
    it. CAS like _org_add_direct. Returns (ok, detail, markdown)."""
    repo, md = cfg["org_repo"], ""
    for attempt in range(PUSH_RETRIES):
        branch = _org_sync(cfg)
        md = _write_knowledge_md(repo, _org_read_wt(cfg)["items"])
        _git(["add", KNOWLEDGE_MD], repo)
        if not _git(["status", "--porcelain"], repo).stdout.strip():
            return True, "already current", md
        _git(["commit", "-m", "cambium: refresh KNOWLEDGE.md"], repo)
        if _git(["push", "origin", branch], repo, check=False).returncode == 0:
            return True, "pushed", md
        time.sleep(0.4 * (attempt + 1))
    return False, "org push kept losing the race", md


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #
VALID_TYPES = ("memory", "need", "skill")


def _new_item(cfg, content, type_, kind, why, tags, source):
    # Normalize cp1252 mojibake at the single write chokepoint every source
    # (distill, import, manual capture) routes through, so the canonical store
    # — and therefore recall() — is clean, not just the markdown export. A
    # substrate that fed us mangled em-dashes (context-keeper .context/, an
    # import export, an agentsync note) is repaired once, on the way in.
    src = dict(source or {})
    if src.get("ref"):
        src["ref"] = _demojibake(src["ref"])
    return {
        "id": f"k-{uuid.uuid4().hex[:8]}",
        "type": type_,
        "kind": kind,
        "content": _demojibake(content),
        "why": _demojibake(why),
        "tags": [_demojibake(t) for t in tags],
        "scope": "local",
        "project": cfg["project"],
        "source": src,
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
# org-scope framing — a body that is right in ONE repo is not automatically
# right for EVERY repo. Promotion to org is a change of readership (everyone),
# so a project-specific runbook ("append to dashboard.py REGIMES", "back up as
# clark_foundation.pt") must be restated as the cross-project rule before it
# crosses. cambium does not rewrite prose (it is deterministic, model-free); it
# DETECTS the smell and makes the human resolve it at the boundary, exactly as
# the endorsement gate already does. The generalization usually already exists,
# in the endorsement note — offered back as a ready draft.
# --------------------------------------------------------------------------- #
# A concrete filename is the reliable "this is one repo's runbook" signal. We
# deliberately do NOT match bare word/word "paths": prose uses slashes for
# lists ("survey/claim/update_status", "read/write"), and every real path of
# concern in practice ends in a filename this already catches.
_FILE_TELL = re.compile(
    r"\b[\w-]+\.(?:py|md|json|gd|tscn|ts|js|jsx|tsx|sh|toml|ya?ml|cfg|ini|txt"
    r"|rs|go|c|cpp|h|pt|ckpt|sql)\b")
_TEST_TELL = re.compile(r"\b(?:test_[a-z]\w+|Test[A-Z]\w+)\b")
_PROV_TELL = re.compile(r"\b(?:dec|con)-\d+\b")       # a real provenance ref


def _org_body_smells_local(item):
    """Deterministic lint: does this item's BODY read like a single-repo runbook
    rather than a cross-project rule? Returns the list of concrete tells found
    (empty == looks org-ready). Used to gate team->org promotion so a specific
    body cannot silently acquire org-wide blast radius — the human either
    restates it (org_content=) or overrides (force=True)."""
    content = item.get("content", "") or ""
    tells = []
    project = (item.get("project") or "").strip()
    # 1. the origin project's name appearing in the body (underscores count as
    #    a boundary so "clark_foundation" trips on project "clark", but
    #    "start" does not trip on "art").
    if len(project) >= 4:
        if re.search(r"(?<![a-z0-9])" + re.escape(project) + r"(?![a-z0-9])",
                     content, re.IGNORECASE):
            tells.append(f"names its origin project ('{project}')")
    # 2. a concrete filename, 3. a test id, 4. a provenance ref
    for m in _FILE_TELL.findall(content):
        tells.append(f"names a file ('{m}')")
    for m in _TEST_TELL.findall(content):
        tells.append(f"names a test ('{m}')")
    for m in _PROV_TELL.findall(content):
        tells.append(f"cites a provenance ref ('{m}')")
    # de-dup, preserve order
    seen, out = set(), []
    for t in tells:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _endorsement_notes(item):
    """The non-empty endorsement notes on an item, newest last — where the
    cross-project restatement usually already lives."""
    return [e.get("note", "").strip()
            for e in item.get("trust", {}).get("endorsements", [])
            if e.get("note", "").strip()]


# --------------------------------------------------------------------------- #
# post-promotion staleness — verification events + premise linkage.
#
# Trust-gated promotion defends knowledge on the way IN; nothing marked a
# promoted entry going stale AFTERWARD. These helpers add that, deliberately
# event-driven: last_verified is a timestamp set by an explicit verification
# (promotion counts as one), never a decaying confidence score, and valid_while
# is the free-text premise an entry depends on. Absent/old last_verified is a
# signal to a human, not an automatic downgrade — no clock-driven decay.
# --------------------------------------------------------------------------- #
def _stamp_verified(item, when, note=""):
    """Record a verification event on an entry (in place)."""
    item["last_verified"] = when
    if note:
        item["last_verified_note"] = note
    item["updated_at"] = when


def _verified_key(item):
    """Oldest-verified-first sort key: never-verified sorts before everything
    (maximally stale), then ascending ISO timestamp (lexical == chronological)."""
    lv = item.get("last_verified")
    return (lv is not None, lv or "")


def _days_since(ts):
    """Whole days since an ISO timestamp, or None if absent/unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def _stale_entry_view(scope, item):
    lv = item.get("last_verified")
    return {
        "id": item.get("id"),
        "scope": scope,
        "project": item.get("project"),
        "kind": item.get("kind"),
        "content": (item.get("content") or "")[:120],
        "last_verified": lv,
        "never_verified": lv is None,
        "days_since_verified": _days_since(lv),
        "valid_while": item.get("valid_while", ""),
    }


def _verification_prompt(cfg, basis_items, limit=3):
    """The oldest-verified promoted (team/org) entries relevant to what just
    completed — surfaced at the release moment so re-verification piggybacks on an
    existing workflow beat instead of needing a new habit. Best-effort; never
    fails the distill."""
    q = set()
    for it in basis_items:
        q |= _tokens(it.get("content", ""))
        q |= {t.lower() for t in it.get("tags", [])}
    if not q:
        return []
    try:
        promoted = [("team", i) for i in _read_team(cfg)]
        if cfg["org_repo"]:
            promoted += [("org", i) for i in _read_org(cfg)]
    except Exception:
        return []  # a nudge is best-effort — never break capture over it
    relevant = [(s, i) for s, i in promoted
                if i.get("status") == "active" and _score(i, q) > 0]
    relevant.sort(key=lambda si: _verified_key(si[1]))
    return [_stale_entry_view(s, i) for s, i in relevant[:limit]]


# --------------------------------------------------------------------------- #
# agentsync distillation (shared by the full-distill pass and release-time
# capture so a claim caught either way carries the identical dedupe key)
# --------------------------------------------------------------------------- #
def _agentsync_key(agent, task, branch, note):
    """The idempotency watermark for one agentsync claim — unchanged from the
    original inline computation so old watermarks stay valid."""
    return hashlib.sha1(
        f"{agent}|{task}|{branch}|{note}".encode()
    ).hexdigest()[:12]


def _agentsync_item(cfg, agent, claim):
    """Build the outcome memory + its dedupe key for one agentsync claim."""
    note = claim.get("note") or ""
    task = claim.get("task") or "task"
    branch = claim.get("branch", "") or ""
    key = _agentsync_key(agent, task, branch, note)
    files = [c.get("path") for c in (claim.get("changed_files") or [])
             if isinstance(c, dict) and c.get("path")]
    verb = "finished" if claim.get("status") == "done" else "released"
    content = f"[{agent}] {verb} '{task}'"
    if note:
        content += f": {note}"
    if files:
        content += f" (files: {', '.join(files[:8])}" + \
                   (", …)" if len(files) > 8 else ")")
    item = _new_item(
        cfg, content, "memory", "outcome",
        note, ["agentsync", agent] + _parse_tags(task)[:4],
        {"system": "agentsync", "ref": f"{agent}:{branch}"},
    )
    return item, key


def _ingest(data, bucket, seen, new_items, item, key):
    """The single normalize-and-write step every source shares: dedupe `key`
    against the per-source watermark list, append the item to the store, record
    the watermark. Returns False if the key was already seen (a duplicate).
    distill's passes and import_memory all route through here — one write/dedupe
    mechanism, one place items enter the store."""
    if key in seen:
        return False
    seen.add(key)
    data["items"].append(item)
    data["imported"][bucket].append(key)
    new_items.append(item)
    return True


def _capture_claim(data, imported_as, new_items, item, key):
    """Import an agentsync outcome once, through the shared ingest path, so the
    same claim caught at release time and again in a later full distill never
    double-imports."""
    _ingest(data, "agentsync", imported_as, new_items, item, key)


def _claim_ident(claim):
    """Logical identity of a claim, stable across in-progress -> done but
    distinct across a re-claim (new task/branch under the same agent id)."""
    if not isinstance(claim, dict):
        return None
    return (claim.get("task"), claim.get("branch"))


def _claim_snapshot(claim):
    """The slice of a claim we remember between sweeps to reconstruct its
    outcome after it churns out of live state."""
    return {k: claim.get(k) for k in
            ("task", "branch", "status", "note", "changed_files")}


# --------------------------------------------------------------------------- #
# import — external memory stores, ingested as a source adapter
#
# An import source adapter is a generator:  adapter(cfg, path) -> yields
#   * a normalized cambium item dict for each usable record, or
#   * None for a record it cannot map (counted as skipped)
# It reads the source READ-ONLY and never writes; dedupe and persistence belong
# to import_memory via the shared _ingest path. One adapter = one external
# format. Register new formats in IMPORT_ADAPTERS; core logic never changes.
# --------------------------------------------------------------------------- #
def _first_str(rec, keys):
    """First non-empty value among `keys`, coerced to a trimmed string. Numbers
    are accepted (e.g. epoch timestamps); everything else must be a real str."""
    for k in keys:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
    return ""


def _imported_item(cfg, content, kind, why, tags, system, source_id, source_ts):
    """Build a cambium item from an external record with provenance stamped:
    source.imported=True plus the origin system, original id, and original
    timestamp — so imported knowledge is never mistaken for native capture and
    stays auditable back to where it came from."""
    src = {"system": system, "ref": source_id or "", "imported": True}
    if source_ts:
        src["source_ts"] = source_ts
    all_tags = _parse_tags(tags) + ["imported", system]
    # scope is local by construction (_new_item) — imported items have not
    # earned promotion in cambium; that must still be earned the normal way.
    return _new_item(cfg, content, "memory", kind or "note", why or "",
                     all_tags, src)


def _import_key(item):
    """Stable dedupe watermark for an imported item: the source system + its
    original id when present, else a content hash. Re-importing the same record
    is therefore a no-op."""
    src = item.get("source", {})
    system = src.get("system", "?")
    ref = src.get("ref") or ""
    if ref:
        return f"{system}:{ref}"
    digest = hashlib.sha1(item.get("content", "").encode()).hexdigest()[:12]
    return f"{system}:h:{digest}"


def _read_json_records(path):
    """Read a JSON or JSONL memory export into a list of raw records, read-only.
    Accepts a top-level array, an object wrapping a list under a common key, a
    single record object, or JSONL (one JSON value per line). A malformed JSONL
    line becomes a None record (skipped downstream) rather than aborting the
    whole import."""
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        doc = None
    if doc is not None:
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict):
            for k in ("memories", "items", "records", "data", "entries"):
                if isinstance(doc.get(k), list):
                    return doc[k]
            return [doc]
        return []
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append(None)
    return records


def _adapter_json(cfg, path):
    """Reference adapter: a generic JSON / JSONL export of memory records. Each
    record is an object with a text body under content/text/body/memory/note
    (required — records without it are skipped) plus optional title, why, kind,
    tags, id, and timestamp. Maps those onto cambium fields; missing fields fall
    back to sensible defaults rather than being guessed."""
    for rec in _read_json_records(path):
        if not isinstance(rec, dict):
            yield None
            continue
        content = _first_str(rec, ("content", "text", "body", "memory", "note"))
        if not content:
            yield None  # no usable body — nothing to distill
            continue
        title = _first_str(rec, ("title", "name", "summary"))
        if title and title not in content:
            content = f"{title} — {content}"
        why = _first_str(rec, ("why", "reason", "rationale", "context"))
        kind = _first_str(rec, ("kind", "type", "category")) or "note"
        source_id = _first_str(rec, ("id", "uuid", "_id", "key"))
        source_ts = _first_str(rec, ("timestamp", "created_at", "ts", "time",
                                     "date"))
        yield _imported_item(cfg, content, kind, why, rec.get("tags", []),
                             "json", source_id, source_ts)


IMPORT_ADAPTERS = {
    "json": _adapter_json,   # generic JSON / JSONL memory export (local file)
}


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def capture(content: str, type: str = "memory", kind: str = "note",
            why: str = "", tags: str = "", valid_while: str = "") -> str:
    """Save a knowledge item to your LOCAL scope: a fact, design note, gotcha,
    or troubleshooting step worth remembering. This is the manual capture path;
    distill() is the automatic one.

    type        : memory | need | skill
    kind        : freeform subtype (note, decision, constraint, runbook, ...)
    why         : the rationale — makes the item far more useful at recall time
    tags        : comma/space-separated keywords (boost recall matching)
    valid_while : optional premise this knowledge depends on, e.g. "while we're
                  on NetSuite" — surfaced later so a dead assumption is spottable"""
    cfg, err = _require_cfg()
    if err:
        return err
    if type not in VALID_TYPES:
        return json.dumps({"error": f"type must be one of {', '.join(VALID_TYPES)}"})
    if not content.strip():
        return json.dumps({"error": "content must not be empty"})
    data = _read_local(cfg)
    item = _new_item(cfg, content.strip(), type, kind, why.strip(),
                     _parse_tags(tags), {"system": "manual", "ref": ""})
    if valid_while.strip():
        item["valid_while"] = _demojibake(valid_while.strip())
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
    from a session-end or post-commit hook for passive capture).

    Release-time capture (opt-in, CAMBIUM_RELEASE_CAPTURE=1): agentsync erases a
    claim from live state the moment it is released or re-claimed, so a claim
    that completes and churns before the next full distill is lost. With the flag
    on, distill also remembers the last-seen claim per agent and captures any
    that has churned away since the previous run — from that snapshot, via the
    same watermark, so nothing double-imports. Wire distill to fire on
    completion events and captured-once-at-completion is the result. The residual
    gap: a done state that lives and dies entirely between two runs is never
    observed (only the agentsync git log holds it)."""
    cfg, err = _require_cfg()
    if err:
        return err
    data = _read_local(cfg)
    imported_as = set(data["imported"]["agentsync"])
    imported_ck = set(data["imported"]["context_keeper"])
    new_items = []

    # --- source 1: agentsync coordination branch -------------------------- #
    repo, remote = cfg["repo"], cfg["remote"]
    as_branch = cfg["agentsync_branch"]
    agentsync_seen = False
    released_captured = 0
    verification_prompt = []
    _git(["fetch", remote, as_branch], repo, check=False)
    claims_doc = _show_file(repo, f"{remote}/{as_branch}", "claims.json")
    if isinstance(claims_doc, dict):
        agentsync_seen = True
        claims_now = claims_doc.get("claims", {})
        if not isinstance(claims_now, dict):
            claims_now = {}

        # (1) live done claims — the classic full-distill pass.
        for agent, claim in claims_now.items():
            if not isinstance(claim, dict) or claim.get("status") != "done":
                continue
            item, key = _agentsync_item(cfg, agent, claim)
            _capture_claim(data, imported_as, new_items, item, key)

        # (2) release-time capture (opt-in). agentsync exposes no hook or event:
        # a claim marked done then released/re-claimed before a distill runs is
        # silently erased from live state. So we diff the live claims against the
        # last-seen snapshot and capture any claim that has churned away — from
        # the snapshot, before the churn erases it — reusing the same watermark.
        if cfg["release_capture"]:
            last = data["imported"].setdefault("agentsync_last", {})
            for agent, prev in list(last.items()):
                if not isinstance(prev, dict):
                    continue
                if _claim_ident(claims_now.get(agent)) == _claim_ident(prev):
                    continue  # same claim still live (may have progressed) — wait
                # Only knowledge-bearing completions become memory: a done claim,
                # or one carrying a reconciliation note. A never-noted abandoned
                # claim holds nothing to distill.
                if prev.get("status") != "done" and not (prev.get("note") or ""):
                    continue
                item, key = _agentsync_item(cfg, agent, prev)
                before = len(new_items)
                _capture_claim(data, imported_as, new_items, item, key)
                released_captured += len(new_items) - before
            # Advance the snapshot to the live claims for the next sweep.
            data["imported"]["agentsync_last"] = {
                a: _claim_snapshot(c) for a, c in claims_now.items()
                if isinstance(c, dict)
            }

    # Lifecycle hook: at the release moment, nudge re-verification of the
    # oldest-verified promoted entries relevant to what just completed. new_items
    # so far are the agentsync outcomes (context-keeper runs below), so they are
    # exactly the "what just happened" basis. Only in the release-capture path.
    if cfg["release_capture"]:
        verification_prompt = _verification_prompt(cfg, list(new_items))

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
                # context-keeper renamed rationale->why_chosen at v0.4 but still
                # reads both; a pre-v0.4 decision carries only `rationale`, so
                # fall back to it or the whole WHY (context-keeper's entire
                # point) is silently dropped while the summary is kept.
                e.get(why_f) or e.get("rationale") or "",
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
            "released_captured": released_captured,
            "release_capture": cfg["release_capture"],
            "verification_prompt": verification_prompt,
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
def import_memory(source: str, path: str) -> str:
    """Ingest an external memory export into cambium as LOCAL-scope, provenance-
    tagged knowledge items — a source adapter alongside distill's substrate
    readers. Import/ingest only: it reads the source READ-ONLY and never writes
    back to it.

    source : adapter name. 'json' = a generic JSON/JSONL export — a list of
             records (or an object wrapping one under memories/items/records),
             each with a text body (content/text/body/memory/note) plus optional
             title, why, kind, tags, id, timestamp. It's the extension point:
             new formats are new adapters, no core changes.
    path   : local file path to read (no network, no external auth).

    Every item is stamped with provenance (source.imported=True, the origin
    system, original id + timestamp) so imports never masquerade as native
    capture. Idempotent — re-importing the same records adds nothing (dedupe by
    source id, or content hash when no id). Imported items are NOT auto-promoted;
    they earn team/org the normal way, through recall usage and endorsement.

    Returns a summary: imported / skipped / duplicates."""
    cfg, err = _require_cfg()
    if err:
        return err
    adapter = IMPORT_ADAPTERS.get(source)
    if adapter is None:
        return json.dumps({"error": f"unknown source '{source}'. available: "
                           f"{', '.join(sorted(IMPORT_ADAPTERS))}"})
    if not path or not os.path.isfile(path):
        return json.dumps({"error": f"no readable file at path: {path!r}"})

    data = _read_local(cfg)
    seen = set(data["imported"]["import"])
    new_items = []
    imported = skipped = duplicates = 0
    try:
        for item in adapter(cfg, path):
            if item is None:
                skipped += 1
                continue
            if _ingest(data, "import", seen, new_items, item, _import_key(item)):
                imported += 1
            else:
                duplicates += 1
    except (OSError, ValueError) as e:
        return json.dumps({"error": f"could not read source {path!r}: {e}"})

    _write_local(cfg, data)
    return json.dumps(
        {
            "status": "imported",
            "source": source,
            "scope": "local",
            "imported": imported,
            "skipped": skipped,
            "duplicates": duplicates,
            "items": [{"id": i["id"], "kind": i["kind"],
                       "content": i["content"][:80], "source": i["source"]}
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
    cfg, err = _require_cfg()
    if err:
        return err
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
            k: i[k] for k in ("id", "type", "kind", "content", "example", "why",
                              "tags", "project", "trust", "source") if k in i},
         # Surface the endorsement notes as first-class context: for an item
         # promoted from one project, this is where its cross-project meaning
         # was written — not buried in the trust blob.
         **({"endorsed_as": _endorsement_notes(i)} if _endorsement_notes(i)
            else {})}
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
    cfg, err = _require_cfg()
    if err:
        return err
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
def verify_entry(item_id: str, note: str = "") -> str:
    """Confirm a knowledge entry still holds — stamp its last_verified to now.
    This is the event that keeps promoted knowledge honest: promotion's trust gate
    defends what comes IN, verification keeps an entry from silently going stale
    after. An optional note records what was confirmed. Absent/old last_verified
    is a signal (see stale_report), never an automatic downgrade. Works on local
    and team entries; find stale ones with stale_report()."""
    cfg, err = _require_cfg()
    if err:
        return err
    when = _now()

    data = _read_local(cfg)
    for i in data["items"]:
        if i["id"] == item_id:
            _stamp_verified(i, when, note)
            _write_local(cfg, data)
            return json.dumps({"status": "verified", "scope": "local",
                               "last_verified": when, "item": i}, indent=2)

    found = {}
    def mark(team_data):
        for i in team_data["items"]:
            if i["id"] == item_id:
                _stamp_verified(i, when, note)
                found["item"] = i
                return None
        return False
    ok = _team_mutate(cfg, mark, f"cambium: {cfg['agent']} verifies {item_id}")
    if found.get("item"):
        return json.dumps({"status": "verified", "scope": "team",
                           "last_verified": when, "item": found["item"]}, indent=2)
    if not ok:
        return json.dumps({"status": "retry_exhausted"})
    return json.dumps({"error": f"No item '{item_id}' in local or team scope."})


@mcp.tool()
def promote(item_id: str = "", to_scope: str = "", force: bool = False,
            org_content: str = "") -> str:
    """Graduate knowledge up a scope as it earns trust — the compound-growth
    step. With no arguments, scans your local items and promotes every one
    that qualifies to team. With an item_id, promotes that item one level
    (local->team, or team->org with to_scope="org").

    Thresholds: local->team needs recalls >= CAMBIUM_PROMOTE_RECALLS or one
    endorsement; team->org always needs an endorsement (force=True overrides,
    use deliberately). Org promotion lands as a direct push, or as a pull
    request when CAMBIUM_ORG_PR=1 — the PR review is the org trust gate.

    org_content : the cross-project restatement of a body that is specific to
    one repo. Promotion to org changes the readership to everyone, so a
    project-local runbook ("append to dashboard.py REGIMES") must become the
    general rule ("annotate a regime boundary when a metric's computation
    changes"). If the body reads project-specific and no org_content is given,
    promotion is refused (with the tells and a suggested draft) unless
    force=True. When supplied, org_content becomes the org body and the original
    is preserved as `example`."""
    cfg, err = _require_cfg()
    if err:
        return err

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
        stamped = _now()
        # Generalization gate: org is a wider readership than any one repo, so a
        # project-specific body must be restated before it crosses (or forced).
        if org_content.strip():
            item["example"] = item["content"]  # keep the concrete runbook
            item["content"] = _demojibake(org_content.strip())
        elif not force:
            tells = _org_body_smells_local(item)
            if tells:
                notes = _endorsement_notes(item)
                return json.dumps({
                    "status": "not_generalized",
                    "message": "This body reads project-specific, but org scope "
                               "is read by every project. Restate it as the "
                               "cross-project rule via org_content=\"...\" (the "
                               "concrete version is kept as `example`), or "
                               "force=True to promote as-is.",
                    "project_local_signals": tells,
                    "suggested_org_statement": notes[-1] if notes else None,
                }, indent=2)
        item["scope"] = "org"
        item["updated_at"] = stamped
        item["last_verified"] = stamped  # promotion IS a verification
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
    stamped = _now()
    for c in candidates:
        c["scope"] = "team"
        c["updated_at"] = stamped
        c["last_verified"] = stamped  # promotion IS a verification
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
def generalize(item_id: str, org_content: str = "", note: str = "") -> str:
    """Restate an ALREADY-PROMOTED item's body as the cross-project rule, in
    place, keeping the concrete version as `example`. The remediation
    counterpart of the org generalization gate: items that reached org (or team)
    before the gate — or were forced past it — are listed by review_promotions()
    under `org_needs_generalization`; this rewrites one to its general form.

    org_content : the cross-project rule to become the body. If omitted, the
                  item's latest endorsement note is used (that is where the
                  generalization was usually already written).
    note        : optional note recorded as a verification stamp.

    Writes through the org store's CAS path (direct push, or the shared
    `cambium/generalize` PR branch when CAMBIUM_ORG_PR=1 — repeated calls batch
    into one reviewable PR), re-rendering KNOWLEDGE.md alongside. Team-scope
    items are edited via the team CAS path."""
    cfg, err = _require_cfg()
    if err:
        return err
    src = next((i for i in _read_org(cfg) if i["id"] == item_id), None)
    scope = "org" if src else None
    if not src:
        src = next((i for i in _read_team(cfg) if i["id"] == item_id), None)
        scope = "team" if src else None
    if not src:
        return json.dumps({"error": f"No item '{item_id}' in org or team scope. "
                           "generalize() edits promoted items; for local ones "
                           "just recapture."})
    notes = _endorsement_notes(src)
    new_body = _demojibake((org_content.strip() or (notes[-1] if notes else "")))
    if not new_body:
        return json.dumps({"error": "No org_content given and the item has no "
                           "endorsement note to fall back on — pass "
                           "org_content=\"<the cross-project rule>\"."})
    stamped = _now()

    def mut(data):
        for i in data["items"]:
            if i["id"] == item_id:
                if i["content"] == new_body:
                    return False  # already generalized — no-op
                i.setdefault("example", i["content"])  # keep the concrete body
                i["content"] = new_body
                i["updated_at"] = stamped
                i["last_verified"] = stamped
                if note.strip():
                    i.setdefault("trust", {}).setdefault(
                        "endorsements", []).append(
                        {"by": cfg["agent"], "at": stamped,
                         "note": _demojibake(note.strip())})
                return None
        return False

    msg = f"cambium: generalize {item_id} for org readership"
    if scope == "org":
        if not cfg["org_repo"]:
            return json.dumps({"error": "CAMBIUM_ORG_REPO is not configured."})
        if cfg["org_pr"]:
            ok, detail, url = _org_mutate_pr(cfg, mut, msg)
            if not ok:
                return json.dumps({"status": "failed", "detail": detail})
            return json.dumps({"status": "generalized", "scope": "org",
                               "via": "pr", "pr_url": url, "detail": detail,
                               "content": new_body, "example": src["content"]},
                              indent=2)
        ok, detail = _org_mutate_direct(cfg, mut, msg)
        if not ok:
            return json.dumps({"status": "failed", "detail": detail})
        return json.dumps({"status": "generalized", "scope": "org",
                           "via": "direct", "detail": detail,
                           "content": new_body,
                           "example": src["content"]}, indent=2)
    if not _team_mutate(cfg, mut, msg):
        return json.dumps({"status": "retry_exhausted"})
    return json.dumps({"status": "generalized", "scope": "team",
                       "content": new_body, "example": src["content"]},
                      indent=2)


@mcp.tool()
def review_promotions() -> str:
    """What's ready to move up? Lists local items eligible for team, team items
    eligible for org (endorsed), and org PRs already opened. The human-readable
    checkpoint before running promote()."""
    cfg, err = _require_cfg()
    if err:
        return err
    local = _read_local(cfg)["items"]
    team = _read_team(cfg)
    def brief(i):
        t = i.get("trust", {})
        return {"id": i["id"], "content": i["content"][:100],
                "recalls": t.get("recalls", 0),
                "endorsements": len(t.get("endorsements", [])),
                "projects": t.get("projects", [])}
    # Self-diagnosis: org items whose body still reads like a single-repo
    # runbook. These crossed before the generalization gate existed (or were
    # forced); each should be restated as the cross-project rule.
    org_smells = []
    if cfg["org_repo"]:
        for i in _read_org(cfg):
            if i.get("status", "active") != "active":
                continue
            tells = _org_body_smells_local(i)
            if tells:
                notes = _endorsement_notes(i)
                org_smells.append({
                    "id": i["id"], "project": i.get("project"),
                    "content": i["content"][:100],
                    "project_local_signals": tells,
                    "suggested_org_statement": notes[-1] if notes else None,
                })
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
            "org_needs_generalization": org_smells,
            "org_configured": bool(cfg["org_repo"]),
        },
        indent=2,
    )


@mcp.tool()
def stale_report(project: str = "", older_than_days: int = 0) -> str:
    """Which promoted knowledge might be going stale? Lists team + org entries
    (the ones that cleared the trust gate) sorted OLDEST-VERIFIED-FIRST, with
    never-reverified entries flagged at the top and each entry's valid_while
    premise surfaced so a reader can spot dead assumptions ("while we're on
    NetSuite" long after the NetSuite migration).

    project         : limit to one project's entries (default: all)
    older_than_days : only entries last verified more than N days ago (plus every
                      never-verified one); 0 = no age filter.

    Event-driven, not clock-driven: this reports absent/old verification events,
    it does NOT compute a decaying confidence score. Re-confirm with
    verify_entry(); promotion also counts as a verification."""
    cfg, err = _require_cfg()
    if err:
        return err
    older_than_days = max(0, int(older_than_days))
    pool = [("team", i) for i in _read_team(cfg)]
    if cfg["org_repo"]:
        pool += [("org", i) for i in _read_org(cfg)]

    rows = []
    for scope, i in pool:
        if i.get("status") != "active":
            continue
        if project and i.get("project") != project:
            continue
        if older_than_days:
            age = _days_since(i.get("last_verified"))
            # never-verified (age is None) is maximally stale — always kept
            if age is not None and age < older_than_days:
                continue
        rows.append((scope, i))
    rows.sort(key=lambda si: _verified_key(si[1]))
    views = [_stale_entry_view(s, i) for s, i in rows]
    return json.dumps(
        {
            "scope": "team+org" if cfg["org_repo"] else "team",
            "project": project or "all",
            "older_than_days": older_than_days,
            "count": len(views),
            "never_verified": sum(1 for v in views if v["never_verified"]),
            "entries": views,
        },
        indent=2,
    )


@mcp.tool()
def export_markdown(scope: str = "org") -> str:
    """Render knowledge to a human-readable KNOWLEDGE.md — grouped by scope then
    project, each item showing summary, kind, provenance (dec-NNN / claim
    origin), recall count and promoted date. cp1252 mojibake (em dashes, curly
    quotes) is normalized so the text is clean.

    scope='org' (default): re-render the org knowledge repo's KNOWLEDGE.md from
    its knowledge.json and commit + push it beside the JSON, so the org repo's
    docs are always current. (This also runs automatically after any org
    promotion — direct-push commits both files together; PR mode puts both on
    the same PR branch.)

    scope='local'|'team'|'all': render those scope(s) and RETURN the markdown
    without publishing — there is no repo to publish local/team docs to."""
    cfg, err = _require_cfg()
    if err:
        return err
    if scope in ("local", "team", "all"):
        items = []
        if scope in ("local", "all"):
            items += _read_local(cfg)["items"]
        if scope in ("team", "all"):
            items += _read_team(cfg)
        if scope == "all" and cfg["org_repo"]:
            items += _read_org(cfg)
        return json.dumps({"status": "rendered", "scope": scope,
                           "published": False,
                           "markdown": _render_markdown(items)}, indent=2)
    if scope != "org":
        return json.dumps({"error": "scope must be org | local | team | all"})
    if not cfg["org_repo"]:
        return json.dumps({"error": "CAMBIUM_ORG_REPO is not configured — no org "
                           "repo to publish KNOWLEDGE.md to. Configure org scope "
                           "with setup(org_repo=…)."})
    ok, detail, md = _org_publish_markdown(cfg)
    return json.dumps({"status": "published" if ok else "failed", "scope": "org",
                       "published": ok, "detail": detail, "file": KNOWLEDGE_MD,
                       "markdown": md}, indent=2)


@mcp.tool()
def status() -> str:
    """First thing to call — especially when cambium looks broken. Returns
    structured config state: what's set, what's missing, what each gap costs in
    plain terms, and the exact setup() call that fixes it. NEVER raises on
    missing config. When fully configured it also reports item counts per
    scope/type, distill watermarks, and which substrates are actually wired."""
    state = _config_state()
    if not state["configured"]:
        return json.dumps(state, indent=2)  # pure guidance — touches no git

    cfg = _cfg()
    local = _read_local(cfg)
    team = _read_team(cfg)
    org = _read_org(cfg)

    def count(items):
        by_type = {}
        for i in items:
            by_type[i.get("type", "?")] = by_type.get(i.get("type", "?"), 0) + 1
        return {"total": len(items), "by_type": by_type}

    state.update({
        "scopes": {"local": count(local["items"]), "team": count(team),
                   "org": count(org) if cfg["org_repo"] else "not configured"},
        "imported": {"context_keeper": len(local["imported"]["context_keeper"]),
                     "agentsync": len(local["imported"]["agentsync"]),
                     "import": len(local["imported"]["import"])},
        "substrates": {
            "agentsync_branch": cfg["agentsync_branch"],
            "context_dir": os.path.isdir(cfg["context_dir"]),
            "team_branch": cfg["team_branch"],
            "org_repo": cfg["org_repo"] or None,
            "org_mode": "pull-request" if cfg["org_pr"] else "direct-push",
        },
        "release_capture": cfg["release_capture"],
        "promote_threshold_recalls": cfg["promote_recalls"],
    })
    return json.dumps(state, indent=2)


# --------------------------------------------------------------------------- #
# setup — the one tool that works BEFORE cambium is configured. It validates,
# scaffolds .cambium/, and writes the fallback config the server reads when env
# vars are absent (env still wins). It never runs org-repo creation unprompted.
#
# TODO(follow-up): fold in friction notes from the first real cycle
# (context-keeper, cambium project). A parallel session is generating those now;
# they get integrated in a follow-up pass — tune the gap costs, setup prompts,
# and org guidance here against what actually tripped up the first onboarding.
# --------------------------------------------------------------------------- #
def _gh_available():
    from shutil import which
    return which("gh") is not None


def _org_setup_advice(name):
    """Exact commands to stand up an org knowledge repo — to RETURN, not run.
    cambium never creates or pushes someone's repo unprompted."""
    slug = name.rstrip("/").split("/")[-1] or "knowledge"
    clone = f"/abs/path/to/{slug}"
    return [
        f"gh repo create {name} --private        # or create it in the GitHub UI",
        f"git clone https://github.com/{name}.git {clone}",
        f"printf '{{\"items\": []}}' > {clone}/knowledge.json",
        f"git -C {clone} add knowledge.json && "
        f"git -C {clone} commit -m 'init org knowledge' && git -C {clone} push",
        f'then re-run: setup(project_repo="…", agent_id="…", org_repo="{clone}")',
    ]


def _ensure_gitignored(repo, entry):
    """Append `entry` to the repo's .gitignore if absent. Returns True if added.
    Keeps the local knowledge store (and anything else under .cambium/) out of
    version control — no secrets, no per-machine paths committed."""
    gi = os.path.join(repo, ".gitignore")
    lines = []
    if os.path.exists(gi):
        with open(gi, encoding="utf-8") as f:
            lines = f.read().splitlines()
    if entry in lines or entry.rstrip("/") in lines:
        return False
    with open(gi, "a", encoding="utf-8") as f:
        if lines and lines[-1].strip():
            f.write("\n")
        f.write(entry + "\n")
    return True


@mcp.tool()
def setup(project_repo: str, agent_id: str, org_repo: str = "",
          org_pr: bool = False, team_branch: str = "") -> str:
    """Finish cambium's setup in one call — the tool status() and every
    unconfigured error point you to. Validates paths, scaffolds .cambium/ (and
    gitignores it), and writes a local fallback config the server reads when env
    vars are absent (env still wins when set, and it takes effect immediately —
    no restart).

    project_repo : absolute path to your project's git clone (required)
    agent_id     : your unique agent id (required)
    org_repo     : optional — a local clone path, OR a GitHub 'owner/name'. If a
                   name isn't cloned locally, setup OFFERS the exact gh/git
                   commands to stand it up and leaves org scope off; it never
                   creates or pushes a repo for you.
    org_pr       : optional — org promotion opens a pull request instead of a
                   direct push.
    team_branch  : optional — override the team-scope branch (default 'cambium').

    No secrets are written anywhere; the config file holds only paths, ids, and
    flags, and lives outside any repo."""
    repo = _abspath(project_repo)
    if not project_repo.strip() or not os.path.isdir(repo):
        return json.dumps({"error": f"project_repo not found: {repo!r}. Pass the "
                           "absolute path to an existing git clone."})
    if not os.path.isdir(os.path.join(repo, ".git")):
        return json.dumps({"error": f"{repo} is not a git repository (no .git). "
                           "Point setup() at a git clone — cambium stores "
                           "knowledge in git."})
    if not agent_id.strip():
        return json.dumps({"error": "agent_id must not be empty."})
    agent_id = agent_id.strip()

    # scaffold the local store dir and keep it out of version control
    os.makedirs(os.path.join(repo, LOCAL_DIR), exist_ok=True)
    gitignored = _ensure_gitignored(repo, LOCAL_DIR + "/")

    # org: offer-but-don't-assume
    org_result = None
    org_value = ""
    if org_repo.strip():
        given = org_repo.strip()
        cand = _abspath(given)
        if os.path.isdir(os.path.join(cand, ".git")):
            org_value = cand  # a real local clone — wire it up
        else:
            org_result = {
                "status": "not_created",
                "given": given,
                "note": "org scope stays OFF until this resolves to a local git "
                        "clone; cambium will not create or push it for you",
                "gh_available": _gh_available(),
                "run_these_yourself": _org_setup_advice(given),
            }

    # write the fallback config (paths / ids / flags only — never secrets)
    conf = {"CAMBIUM_REPO": repo, "CAMBIUM_AGENT_ID": agent_id}
    if org_value:
        conf["CAMBIUM_ORG_REPO"] = org_value
    if org_pr:
        conf["CAMBIUM_ORG_PR"] = "1"
    if team_branch.strip():
        conf["CAMBIUM_TEAM_BRANCH"] = team_branch.strip()
    _write_config_file(conf)

    result = {
        "status": "configured",
        "config_file": _config_file(),
        "wrote": sorted(conf.keys()),
        "scaffolded": os.path.join(repo, LOCAL_DIR),
        "gitignored": (LOCAL_DIR + "/") if gitignored else "already ignored",
        "note": "env vars override this file when set; it takes effect "
                "immediately — no restart needed",
    }
    if org_result:
        result["org_repo"] = org_result
    result["state"] = _config_state()  # reflects the just-written config
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    mcp.run()
