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
    CAMBIUM_PROJECTS         explicit project -> repo path map for the page tier
                             and the snapshot exporter, as a JSON object or
                             "name=/abs/path" pairs. Explicit rather than a
                             filesystem scan: a store can only be read because
                             someone named it.                      (optional)
    CAMBIUM_CONTEXT_KEEPER   path to context-keeper's server.py (or its console
                             script) so export_snapshot can run its
                             verify_quality. Absent, the snapshot reports the
                             quality scan as not-checked rather than clean.
                                                                    (optional)

Pages
-----
A fourth thing lives here alongside the three scopes: compiled synthesis pages
(compile_page / compile_project / list_pages / recompile). Pages are BUILD
ARTIFACTS, not knowledge — they live in .cambium/pages.json, never in
knowledge.json, so they cannot be recalled, promoted, or cited as a source, and
deleting the file loses nothing that .context/ cannot rebuild.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("cambium")
except Exception:
    __version__ = "0.0.0+local"

# The tool list is static code, identical for every caller with no auth-scoped
# variation, so a shared intermediary may cache it. Tool *results* are
# caller-specific (recall is scoped to the caller's project and promotion
# tier), but tools/call is not a cacheable method, so none of that is cached.
mcp = MCPServer(
    "cambium",
    version=__version__,
    cache_hints={
        "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
        "server/discover": CacheHint(ttl_ms=300_000, scope="public"),
    },
)

KNOWLEDGE_FILE = "knowledge.json"
KNOWLEDGE_MD = "KNOWLEDGE.md"   # human-readable render of a knowledge store
LOCAL_DIR = ".cambium"
# Pages live in their OWN file, never in knowledge.json. A page is a build
# artifact compiled from context-keeper entries: deletable, regenerable, and —
# critically — outside every trust-tier read. recall(), session_primer(),
# export_markdown(), stale_report() and review_promotions() all iterate
# data["items"], so a page stored there would be recallable AND promotable, i.e.
# a derived summary could climb to org scope and be cited as a source. Keeping
# pages in a separate file makes that impossible by construction rather than by
# a filter every future read has to remember to apply.
PAGES_FILE = "pages.json"
# Stamped into every exported markdown page's frontmatter. export_pages reaps
# stale files from its output directory, so it needs a way to tell its own
# output from a note a human put there — the marker is that proof.
PAGES_MARKER = "cambium:export_pages"
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


def _log(msg):
    """Append a timestamped line to <repo>/.git/cambium.log. Best-effort: a
    logging failure must never break a tool call. This is the breadcrumb that
    turns 'the promotion vanished' into 'the push to the team branch failed at
    this timestamp with this stderr'."""
    try:
        repo = os.environ.get("CAMBIUM_REPO") or _git_root()
        if not repo or not os.path.isdir(os.path.join(repo, ".git")):
            return
        line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
        with open(os.path.join(repo, ".git", "cambium.log"), "a",
                  encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


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
                "CAMBIUM_AGENTSYNC_BRANCH", "AGENTSYNC_BOARD_REPO")


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
    if name == "CAMBIUM_REPO" and _git_root():
        return "cwd"          # resolved from the current project, not a gap
    return "unset"


def _abspath(p):
    return os.path.abspath(os.path.expanduser(p)) if p else ""


def _git_root(start=None):
    """The git working-tree root at or above `start` (default: cwd), or "".
    Lets a solo operator who IS the org configure once (org_repo + agent_id) and
    have cambium operate on whichever project the session is in — CAMBIUM_REPO
    defaults to the current repo instead of being pinned to a single project."""
    d = _abspath(start or os.getcwd())
    while d:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent
    return ""


def _projects_map(file_cfg):
    """The explicit project -> repo-path map used by the page tier and the
    snapshot exporter.

    cambium is otherwise configured one repo at a time (cfg["project"] is just
    the basename of CAMBIUM_REPO), so anything that spans projects — a page
    compiled for another repo, a snapshot listing every project — has no way to
    resolve a project NAME to a store. This map is that resolution, and it is
    deliberately EXPLICIT rather than a filesystem scan: the alternative is
    walking a root and reading every .context/ it finds, which silently pulls in
    private repos and local-only projects. An operator who has to name a project
    to include it cannot be surprised by what got read (see con-015-12da in
    context-keeper, and the dashboard.html gitignore rule here).

    Accepts a JSON object (from the config file, or as an env string) or the
    compact "name=path,name=path" form. Bad shapes are dropped, never raised —
    a typo in one entry must not take down every tool that reads config."""
    raw = _resolve("CAMBIUM_PROJECTS", file_cfg, "")
    if not raw:
        return {}
    if isinstance(raw, dict):
        pairs = raw.items()
    else:
        text = str(raw).strip()
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                return {}
            pairs = loaded.items() if isinstance(loaded, dict) else []
        else:
            # "name=path" separated by commas or newlines. Paths hold ':' and
            # ';' on Windows, so neither is usable as the separator here.
            pairs = []
            for chunk in text.replace("\n", ",").split(","):
                if "=" in chunk:
                    name, _, path = chunk.partition("=")
                    pairs.append((name, path))
    out = {}
    for name, path in pairs:
        name, path = str(name).strip(), str(path).strip()
        if name and path:
            out[name] = _abspath(path)
    return out


def _cfg():
    """Resolved config dict, or ConfigError if a required setting is missing or
    the repo isn't a git clone. Tools call this via _require_cfg() so the error
    becomes helpful guidance instead of a raised exception."""
    file_cfg = _load_config_file()
    # CAMBIUM_REPO falls back to the current repo (cwd's git root) when unset, so
    # one org_repo + agent_id config serves every project the operator works in.
    repo = _resolve("CAMBIUM_REPO", file_cfg) or _git_root()
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
        "mode": (_resolve("CAMBIUM_MODE", file_cfg, "auto") or "auto").lower(),
        "worktree": os.path.join(repo, ".git", "cambium-wt"),
        "local_store": os.path.join(repo, LOCAL_DIR, KNOWLEDGE_FILE),
        "pages_store": os.path.join(repo, LOCAL_DIR, PAGES_FILE),
        "context_dir": os.path.join(repo, ".context"),
        "projects": _projects_map(file_cfg),
    }


def _config_state():
    """Structured config state that NEVER raises: what's set, what's missing,
    what each gap costs in plain terms, and the exact setup() call that fixes it.
    Powers status() and the fail-helpful path so any agent that touches an
    unconfigured cambium can offer setup conversationally from the response."""
    file_cfg = _load_config_file()
    repo = _resolve("CAMBIUM_REPO", file_cfg) or _git_root()
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


def _repo_has_board(repo, remote, branch):
    """True if `repo` actually holds an agentsync coordination branch — as a
    local head, a remote-tracking ref, or on the remote itself. Cheap local ref
    checks first; the network ls-remote is a last resort.

    MIRRORS agentsync.repo_has_board. If you change one, change both."""
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return False
    for ref in (f"refs/heads/{branch}", f"refs/remotes/{remote}/{branch}"):
        p = _git(["rev-parse", "--verify", "--quiet", ref], repo, check=False)
        if p.returncode == 0 and p.stdout.strip():
            return True
    try:
        p = _git(["ls-remote", "--heads", remote, branch], repo, check=False)
    except RuntimeError:
        return False
    return p.returncode == 0 and bool(p.stdout.strip())


def _resolve_board_repo(cfg):
    """Where the agentsync coordination board lives, resolved INDEPENDENTLY of
    the session pointer and identically to agentsync itself.

    Returns (repo, source, problem). `problem` is None when a board was found,
    otherwise a plain-language explanation that callers must surface — never
    swallow. Unlike agentsync this does not raise: distill has a second
    substrate (context-keeper) and must still do that half of its job.

    Order (mirrors agentsync._resolve_board_repo):
      1. AGENTSYNC_BOARD_REPO  — the explicit, shared board address. One
         setting configures both servers, which is the point: cambium and
         agentsync can no longer disagree about where the board is.
      2. AGENTSYNC_REPO        — agentsync's legacy explicit pin.
      3. cfg["repo"], but ONLY if it actually holds the coordination branch.
      4. no board — reported loudly."""
    file_cfg = _load_config_file()
    remote, branch = cfg["remote"], cfg["agentsync_branch"]
    for name in ("AGENTSYNC_BOARD_REPO", "AGENTSYNC_REPO"):
        v = _resolve(name, file_cfg)
        if v:
            p = _abspath(v)
            if not os.path.isdir(os.path.join(p, ".git")):
                return p, name, (
                    f"{name} points at {p}, which is not a git repository — "
                    "no agentsync claims can be read from it")
            if not _repo_has_board(p, remote, branch):
                return p, name, (
                    f"{name} points at {p}, which has no '{branch}' branch on "
                    f"'{remote}' — it is not a board")
            return p, name, None

    repo = cfg["repo"]
    if _repo_has_board(repo, remote, branch):
        return repo, "current-repo", None
    return repo, "current-repo", (
        "AGENTSYNC_BOARD_REPO is not set, and the current project "
        f"({repo}) has no '{branch}' coordination branch on '{remote}'. The "
        "board is a shared, long-lived artifact — it does not follow whichever "
        "project this session happens to be in.")


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


def _atomic_write_json(path, data):
    """Write JSON to `path` atomically: dump to a temp file in the same dir,
    fsync, then os.replace (an atomic rename on every platform). A crash mid-
    write leaves the original intact instead of a half-written, unparseable file
    — which, combined with the quarantine in _read_local, is what keeps the
    local store from silently vanishing."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".knowledge-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _quarantine_corrupt(path):
    """Move an unparseable local store aside (…knowledge.json.corrupt-<epoch>)
    instead of letting the next write silently overwrite it with an empty store.
    The local store is the one tier NOT backed by git, so a corrupt file is the
    only copy — preserve it for hand-recovery. Best-effort; never raises."""
    try:
        os.replace(path, "%s.corrupt-%d" % (path, int(time.time())))
    except OSError:
        pass


def _read_local(cfg):
    path = cfg["local_store"]
    if not os.path.exists(path):
        return _empty_local()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return _empty_local()
    except json.JSONDecodeError:
        # Corrupt/partial store. Returning empty here (the old behaviour) meant
        # the next write overwrote the only copy — total silent loss. Quarantine
        # it first so it is recoverable, THEN start clean.
        _quarantine_corrupt(path)
        return _empty_local()
    if not isinstance(data, dict):
        _quarantine_corrupt(path)
        return _empty_local()
    data.setdefault("items", [])
    data.setdefault("imported", {})
    data["imported"].setdefault("context_keeper", [])
    data["imported"].setdefault("agentsync", [])
    data["imported"].setdefault("agentsync_last", {})
    data["imported"].setdefault("import", [])
    return data


def _write_local(cfg, data):
    _atomic_write_json(cfg["local_store"], data)


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


def _push_landed(wt, remote, branch):
    """True only if the REMOTE branch actually contains this worktree's HEAD.

    A zero exit from `git push` is not proof on its own once retries are in
    play, and — more importantly — a clean working tree is not proof at all: a
    commit that failed to push leaves the tree clean and the work local. This
    asks the remote directly (ls-remote, no cached ref), then falls back to an
    ancestry test so a peer pushing on top of us still counts as landed."""
    head = _git(["rev-parse", "HEAD"], wt, check=False)
    if head.returncode != 0 or not head.stdout.strip():
        return False
    local = head.stdout.strip()
    ls = _git(["ls-remote", "--heads", remote, branch], wt, check=False)
    if ls.returncode != 0 or not ls.stdout.strip():
        return False                      # remote branch does not exist at all
    if ls.stdout.split()[0] == local:
        return True
    _git(["fetch", remote, branch], wt, check=False)
    return _git(["merge-base", "--is-ancestor", local, f"{remote}/{branch}"],
                wt, check=False).returncode == 0


def _team_mutate(cfg, fn, message):
    """CAS write to the team store: fetch+reset, apply fn(data) (return False
    to abort as a no-op), commit, push; on rejected push resync and retry so a
    peer's concurrent write is observed, never clobbered.

    Returns True ONLY when the change is verified present on the remote. It used
    to report success from a clean working tree alone: a failed push left the
    commit local, the `reset --hard <remote>/<branch>` meant to undo it ALSO
    failed whenever the remote branch didn't exist yet, and the next attempt
    then saw an empty `git status --porcelain` and returned True. promote()
    trusts that True and deletes the local copies — which is exactly how
    clark-mcp ended up with nine knowledge items reachable only from an
    unpushed branch inside .git."""
    wt, remote, branch = cfg["worktree"], cfg["remote"], cfg["team_branch"]
    last_err = ""
    for attempt in range(PUSH_RETRIES):
        _ensure_team_worktree(cfg)
        data = _read_team_wt(cfg)
        if fn(data) is False:
            return True  # nothing to do
        with open(os.path.join(wt, KNOWLEDGE_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _git(["add", KNOWLEDGE_FILE], wt)
        st = _git(["status", "--porcelain"], wt)
        if st.stdout.strip():
            _git(["commit", "-m", message], wt)
        elif _push_landed(wt, remote, branch):
            return True      # genuinely nothing to do; the state is on the remote
        # else: tree is clean but HEAD is not on the remote — an earlier attempt
        # committed and failed to push. Do NOT mistake that for success; push it.
        push = _git(["push", remote, branch], wt, check=False)
        if push.returncode == 0 and _push_landed(wt, remote, branch):
            return True
        last_err = (push.stderr or push.stdout or "").strip()[:300]
        _log(f"team push did not land (attempt {attempt + 1}): {last_err}")
        # Resync only when there is something to resync TO. Resetting to a
        # nonexistent remote branch is itself an error, and doing it blindly is
        # what silently discarded the retry's starting point.
        if _remote_has_branch(cfg["repo"], remote, branch):
            _git(["reset", "--hard", f"{remote}/{branch}"], wt, check=False)
        time.sleep(0.4 * (attempt + 1))
    _log(f"team push FAILED after {PUSH_RETRIES} attempts: {last_err} — the "
         f"commit is local-only on '{branch}' in {cfg['repo']}")
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
    and tokens >=3 chars match on a shared PREFIX (jwt~jwts, hash~hashing) so a
    stem matches its inflections. Prefix, not bare infix: an earlier version used
    `tok in w or w in tok`, which let a short token match anywhere inside a longer
    word — "art" scored a hit on "start", "cat" on "locate" — inflating unrelated
    items and (since recall counts feed promotion) their trust. Requiring one to
    be a prefix of the other keeps the intended stem/plural matches while dropping
    those infix/suffix false positives. Deterministic, dependency-free — the
    semantic upgrade is a later swap."""
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
            (w.startswith(tok) or tok.startswith(w)) for w in body if len(w) >= 3
        ):
            hits += 1.0
    return min(1.0, hits / len(q_tokens))


def _eligible_team(cfg, item):
    t = item.get("trust", {})
    return (t.get("recalls", 0) >= cfg["promote_recalls"]
            or len(t.get("endorsements", [])) >= 1)


def _eligible_org(item):
    return len(item.get("trust", {}).get("endorsements", [])) >= 1


def _recall_mark(cfg):
    """The dedup key for one recall credit: this agent, this UTC day. Recall
    counts feed promotion (3 recalls auto-qualifies local->team), so crediting
    every raw recall let one agent loop the same query and promote its own item.
    Crediting at most once per (agent, day) keeps 'usage promotes' honest — it
    now means sustained use across days, or use by distinct teammates."""
    return "%s|%s" % (cfg["agent"], _now()[:10])


def _retire_synced(item, ref, source_status):
    """Deprecate a cambium item in place because the source entry it was
    distilled from (context-keeper `ref`) is now superseded/deprecated. Same
    status field deprecate() sets, so it drops out of recall/export/promotion,
    with a reason that traces back to the source."""
    now = _now()
    item["status"] = "deprecated"
    item["deprecated_at"] = now
    item["deprecated_reason"] = ("source context-keeper %s is now %s"
                                 % (ref, source_status))
    item["updated_at"] = now


def _credit_recall(item, mark):
    """Record one recall credit against an item unless this (agent, day) mark is
    already counted. Returns True if it was newly credited. Legacy items have a
    `recalls` count but no `recall_marks`; the first new credit starts the ledger
    without discarding the prior count."""
    trust = item.setdefault("trust", {})
    marks = trust.setdefault("recall_marks", [])
    if mark in marks:
        return False
    marks.append(mark)
    trust["recalls"] = trust.get("recalls", 0) + 1
    return True


def _distinct_identities(cfg):
    """Every identity that has ever acted in this coordination space: the
    configured agent, agentsync claim owners, and everyone who has endorsed an
    item at any scope. This is the raw signal for solo-vs-team auto-detection --
    one identity means nobody else is here. Best-effort: any unreadable source is
    skipped, never fatal."""
    ids = set()
    if cfg.get("agent"):
        ids.add(cfg["agent"])
    try:
        # Same board resolution distill uses — a solo/team judgement made from
        # the wrong (or a missing) board is the same failure in a new costume.
        remote, br = cfg["remote"], cfg["agentsync_branch"]
        repo, _src, problem = _resolve_board_repo(cfg)
        if problem:
            raise RuntimeError(problem)
        _git(["fetch", remote, br], repo, check=False)
        claims = _show_file(repo, f"{remote}/{br}", "claims.json")
        if isinstance(claims, dict):
            for owner in (claims.get("claims", {}) or {}):
                ids.add(owner)
    except Exception:
        pass

    def _endorsers(items):
        for it in items or []:
            for e in it.get("trust", {}).get("endorsements", []):
                who = e.get("by")
                if who:
                    ids.add(who)
    for src in (lambda: _read_local(cfg)["items"],
                lambda: _read_team(cfg),
                lambda: _read_org(cfg)):
        try:
            _endorsers(src())
        except Exception:
            pass
    return ids


def _detect_mode(cfg):
    """'solo' or 'team'. CAMBIUM_MODE forces it; 'auto' (default) infers from the
    number of distinct identities ever seen -- <=1 is solo. Reversible: the day a
    second collaborator claims or endorses anything, this flips to 'team' and the
    full peer-endorsement ladder re-engages, with no migration."""
    m = (cfg.get("mode") or "auto").lower()
    if m in ("solo", "team"):
        return m
    return "solo" if len(_distinct_identities(cfg)) <= 1 else "team"


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
    verb = "finished" if _claim_is_done(claim) else "released"
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


_DONE_STATUS = re.compile(r"^done\b", re.IGNORECASE)


def _claim_is_done(claim):
    """True if an agentsync claim reports itself finished.

    agentsync's own update_status() only writes the exact literal "done", but
    claims.json is a foreign substrate cambium does not own: agentsync-remote,
    a hand edit, or a human writing a closing note in the status field all
    produce things like "DONE - shipped to origin/main as 56052d8". An exact ==
    "done" test silently skipped those, which looks identical to "nothing has
    finished yet". Anchored at the start and word-bounded, so "not done" and
    "done-ish"-style continuations of another word are not matched."""
    if not isinstance(claim, dict):
        return False
    return bool(_DONE_STATUS.match(str(claim.get("status") or "").strip()))


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
    """Record a NEED — something missing, wanted, or blocking. Needs are
    first-class alongside memories: "we need staging seeds", "docs for X are
    missing", "no way to replay a failed job".

    When to use this instead of the alternatives:
      record_need : a GAP. Something that does not exist yet and should.
      capture     : a FACT you learned. Use it when the thing is already true.
      distill     : never called by hand for this — it imports work that
                    already happened, and cannot invent a need.

    Side effects: appends one item to the LOCAL knowledge store on this machine
    (personal scope) and writes it to disk immediately. Nothing is shared with
    the team or org until promote() is called on it, and nothing is sent over
    the network. Recording the same need twice creates two items — this does
    not deduplicate, so recall() first if you may already have logged it.

    Parameters:
      content : the need itself, stated concretely. "docs for the retry policy
                are missing" beats "docs are bad".
      why     : optional but strongly worth filling. The rationale — what is
                blocked, or what it costs to leave unfixed. This is what makes
                the item useful months later when the urgency is forgotten,
                and it is indexed for recall alongside the content.
      tags    : optional comma- or space-separated keywords ("ci, flaky,
                staging"). They boost recall matching, so a future session
                searching different words can still find this.

    Returns JSON: {"status": "captured", "item": {...}} where item includes the
    generated id needed by promote(), endorse() and deprecate(). On failure
    returns {"error": "..."} — an empty content is refused."""
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
    remote = cfg["remote"]
    as_branch = cfg["agentsync_branch"]
    released_captured = 0
    verification_prompt = []
    warnings = []
    # The board is addressed independently of the session pointer, by the same
    # rules agentsync uses. Previously this read cfg["repo"] and reported a
    # miss as the bland, ignorable string "no coordination branch found".
    repo, board_source, board_problem = _resolve_board_repo(cfg)
    agentsync_report = {
        "status": "skipped",
        "board_repo": repo,
        "board_source": board_source,
        "branch": as_branch,
        "imported": 0,
    }
    claims_doc = None
    if board_problem is None:
        _git(["fetch", remote, as_branch], repo, check=False)
        claims_doc = _show_file(repo, f"{remote}/{as_branch}", "claims.json")
        if not isinstance(claims_doc, dict):
            board_problem = (
                f"the '{as_branch}' branch exists in {repo} but its claims.json "
                "is missing or unparseable, so no claims could be read")
            claims_doc = None

    if board_problem is not None:
        agentsync_report["reason"] = board_problem
        agentsync_report["fix"] = (
            "Set AGENTSYNC_BOARD_REPO (env, or via setup()) to the absolute "
            "path of the clone holding the agentsync coordination branch. "
            "agentsync resolves the board the same way, so one setting fixes "
            "both.")
        # A SKIPPED STEP MUST NOT LOOK LIKE A COMPLETED ONE. This is the whole
        # lesson of this codebase: the old return said
        # "agentsync": "no coordination branch found" and every caller read it
        # as normal, so distill imported zero agentsync claims for its entire
        # lifetime without anyone noticing.
        warnings.append(
            "agentsync source SKIPPED — no claims were distilled. "
            + board_problem + " " + agentsync_report["fix"])

    claims_now = {}
    if claims_doc is not None:
        agentsync_report["status"] = "read"
        claims_now = claims_doc.get("claims", {})
        if not isinstance(claims_now, dict):
            claims_now = {}
        agentsync_report["claims_seen"] = len(claims_now)
        agentsync_report["done_claims"] = sum(
            1 for c in claims_now.values() if _claim_is_done(c))

    if claims_doc is not None:
        # (1) live done claims — the classic full-distill pass.
        for agent, claim in claims_now.items():
            if not isinstance(claim, dict) or not _claim_is_done(claim):
                continue
            item, key = _agentsync_item(cfg, agent, claim)
            _capture_claim(data, imported_as, new_items, item, key)
        if agentsync_report.get("claims_seen") and not agentsync_report[
                "done_claims"]:
            # Read successfully, but nothing was finished. Say so — an empty
            # import from a live board and an import from no board at all are
            # very different facts.
            warnings.append(
                f"agentsync board read ({agentsync_report['claims_seen']} "
                "claim(s)) but none are done, so nothing was distilled from it. "
                "Claims become knowledge when they reach status 'done'.")

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
                if not _claim_is_done(prev) and not (prev.get("note") or ""):
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
        agentsync_report["imported"] = len(new_items)

    # Lifecycle hook: at the release moment, nudge re-verification of the
    # oldest-verified promoted entries relevant to what just completed. new_items
    # so far are the agentsync outcomes (context-keeper runs below), so they are
    # exactly the "what just happened" basis. Only in the release-capture path.
    if cfg["release_capture"]:
        verification_prompt = _verification_prompt(cfg, list(new_items))

    # --- source 2: context-keeper .context/ ------------------------------- #
    # Read the FULL entry set (not only active) so distill can do two jobs:
    # import new active entries, AND re-sync the lifecycle of ones imported
    # earlier. Trust-gated promotion defends knowledge on the way IN; without a
    # re-sync, a decision context-keeper later supersedes/deprecates keeps living
    # as active cambium knowledge — recalled, and free to climb to org.
    ck_seen = False
    ck_status = {}   # eid -> current context-keeper status
    for fname, kind, text_f, why_f in (
        ("decisions.json", "decision", "summary", "why_chosen"),
        ("constraints.json", "constraint", "rule", "reason"),
    ):
        path = os.path.join(cfg["context_dir"], fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, json.JSONDecodeError):
            # A corrupt/unreadable store must NOT be reported as read: the old
            # code set ck_seen before this load, so a corrupt file looked
            # captured. Leaving ck_seen alone here keeps "read" honest.
            continue
        ck_seen = True
        for e in entries if isinstance(entries, list) else []:
            eid = e.get("id", "")
            if not eid:
                continue
            status = e.get("status") or "active"
            ck_status[eid] = status
            if eid in imported_ck or status != "active":
                continue
            content = e.get(text_f) or ""
            if not content:
                continue
            # context-keeper renamed rationale->why_chosen at v0.4 but still reads
            # both; a pre-v0.4 decision carries only `rationale`.
            why = e.get(why_f) or e.get("rationale") or ""
            # Keep the decision's `problem` (what forced it). Dropping it left
            # cambium — and org readers at the promotion gate — unable to judge
            # whether a decision was context-specific or a general rule.
            problem = (e.get("problem") or "").strip()
            if problem:
                why = ("%s  Problem: %s" % (why, problem)).strip() if why \
                    else "Problem: %s" % problem
            item = _new_item(
                cfg, content, "memory", kind, why,
                _parse_tags(e.get("tags", [])) + ["context-keeper"],
                {"system": "context-keeper", "ref": eid},
            )
            data["items"].append(item)
            data["imported"]["context_keeper"].append(eid)
            new_items.append(item)

    # Lifecycle re-sync. Deprecate any LOCAL cambium item whose source is now
    # superseded/deprecated, so a retired decision stops being served by recall()
    # and can't keep climbing. Promoted copies (team/org) are only REPORTED for a
    # deliberate deprecate() — auto-pushing deprecations to shared scopes from an
    # automatic hook would violate cambium's "org is a hard gate" rule.
    resynced = []
    stale_promoted = []
    retired = {eid for eid, st in ck_status.items()
               if st in ("superseded", "deprecated")}
    if retired:
        for i in data["items"]:
            src = i.get("source") or {}
            if (src.get("system") == "context-keeper"
                    and src.get("ref") in retired
                    and i.get("status") == "active"):
                st = ck_status[src["ref"]]
                _retire_synced(i, src["ref"], st)
                resynced.append({"id": i["id"], "ref": src["ref"],
                                 "source_status": st})
        try:
            promoted = [("team", i) for i in _read_team(cfg)]
            if cfg["org_repo"]:
                promoted += [("org", i) for i in _read_org(cfg)]
            for scope, i in promoted:
                src = i.get("source") or {}
                if (src.get("system") == "context-keeper"
                        and src.get("ref") in retired
                        and i.get("status") == "active"):
                    stale_promoted.append({
                        "scope": scope, "id": i["id"], "ref": src["ref"],
                        "source_status": ck_status[src["ref"]],
                        "action": "deprecate(%r)" % i["id"]})
        except Exception:
            pass  # best-effort; a missing remote scope never fails a distill

    if not ck_seen:
        warnings.append(
            "context-keeper source SKIPPED — no readable .context/ store at "
            f"{cfg['context_dir']}, so no decisions or constraints were "
            "distilled.")

    _write_local(cfg, data)
    ck_report = {
        "status": "read" if ck_seen else "skipped",
        "context_dir": cfg["context_dir"],
    }
    if not ck_seen:
        ck_report["reason"] = "no readable decisions.json/constraints.json found"
    return json.dumps(
        {
            # A skipped substrate must not be reportable as a completed one.
            # Callers that only look at `status` still see the difference.
            "status": "distilled_with_warnings" if warnings else "distilled",
            "warnings": warnings,
            "new_items": len(new_items),
            "released_captured": released_captured,
            "release_capture": cfg["release_capture"],
            "resynced": resynced,
            "stale_promoted": stale_promoted,
            "verification_prompt": verification_prompt,
            "sources": {
                "agentsync": agentsync_report,
                "context_keeper": ck_report,
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
    team_by_id = {}
    if "local" in scopes:
        local_data = _read_local(cfg)
        pool += [("local", i) for i in local_data["items"]]
    if "team" in scopes:
        for i in _read_team(cfg):
            team_by_id[i["id"]] = i
            pool.append(("team", i))
    if "org" in scopes:
        pool += [("org", i) for i in _read_org(cfg)]

    scored = [
        (s, _score(i, q), i) for s, i in pool if i.get("status") == "active"
    ]
    scored.sort(key=lambda t: t[1], reverse=True)
    top = [t for t in scored[:limit] if t[1] > 0]

    # usage tracking — best-effort, never fails the recall. Credit is deduped
    # per (agent, day) so a repeat recall neither inflates trust nor triggers a
    # write; that dedup is also what lets us skip the team git-push entirely when
    # nothing new would be recorded (see _recall_mark / _credit_recall).
    mark = _recall_mark(cfg)
    hit_local = {i["id"] for s, _, i in top if s == "local"}
    hit_team = {i["id"] for s, _, i in top if s == "team"}
    if hit_local and local_data is not None:
        changed = False
        for i in local_data["items"]:
            if i["id"] in hit_local and _credit_recall(i, mark):
                i["updated_at"] = _now()
                changed = True
        if changed:
            _write_local(cfg, local_data)
    if hit_team:
        # Pre-check against the copies already read into the pool: only touch the
        # shared branch (a network fetch + commit + push) if some team hit has a
        # new credit or a new project to record. An already-credited recall is a
        # pure read.
        def _needs_write(i):
            t = i.get("trust", {})
            return (mark not in t.get("recall_marks", [])
                    or cfg["project"] not in t.get("projects", []))
        if any(_needs_write(team_by_id[iid]) for iid in hit_team
               if iid in team_by_id):
            def bump(data):
                changed = False
                for i in data["items"]:
                    if i["id"] not in hit_team:
                        continue
                    credited = _credit_recall(i, mark)
                    projs = i.setdefault("trust", {}).setdefault("projects", [])
                    new_proj = cfg["project"] not in projs
                    if new_proj:
                        projs.append(cfg["project"])  # cross-project signal
                    if credited or new_proj:
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
    """Vouch for an item — the strong trust signal, and a human judgement that
    nothing else in this server can substitute for.

    Why it matters: one endorsement fast-tracks local->team promotion, and it
    is REQUIRED for team->org. Recall counts alone never reach org scope, by
    design — popularity is not correctness, so somebody has to deliberately say
    "this is right" before knowledge becomes org-wide.

    When to use this instead of the alternatives:
      endorse       : you have CONFIRMED the item is correct and want it to
                      carry more weight and become promotable.
      verify_entry  : you re-checked it and want to refresh its freshness date
                      without adding a trust signal.
      promote       : you want to move it up a scope now. Endorse first if it
                      has not cleared the bar.
      deprecate     : it turned out to be wrong.

    Side effects: appends an endorsement stamp (your agent id, a timestamp, and
    your note) to the item's trust record and writes it immediately. Endorsing
    the same item twice appends a SECOND stamp rather than replacing the first
    — this is deliberate, since two people vouching is stronger than one, but
    it does mean re-running this inflates the count. Searches local scope
    first, then team and org.

    Parameters:
      item_id : the id of the item to vouch for, as returned by recall(),
                capture(), record_need() or status(). Not the content text —
                an id that matches nothing in any scope returns an error rather
                than creating anything.
      note    : optional free text recording WHY you are vouching — e.g. "hit
                this in prod on 2026-07-14, the workaround is exact". Stored on
                the stamp and shown to whoever reviews the promotion later, so
                it is the difference between a countable vote and a reviewable
                one.

    Returns JSON: {"status": "endorsed", "scope": "local"|"team"|"org",
    "item": {...}} naming the scope the item was found in. On failure returns
    {"error": "..."}."""
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
def deprecate(item_id: str, reason: str = "") -> str:
    """Retire a knowledge item so recall stops serving it — the counterpart to
    verify_entry (which re-affirms). This is the action stale_report points at:
    when an entry's premise has died ("we've left NetSuite"), deprecate it.

    Sets status='deprecated' wherever the item lives — local, team, or org —
    through the same write/CAS path promotion uses (org honours CAMBIUM_ORG_PR,
    landing the change as a pull request). Deprecated items drop out of recall(),
    KNOWLEDGE.md, and promotion, but stay in the store with a `deprecated_at`
    stamp and optional reason for provenance/audit; reactivating one is a manual
    JSON edit by design (retiring is the common, safe direction)."""
    cfg, err = _require_cfg()
    if err:
        return err
    when = _now()

    def _retire(i):
        i["status"] = "deprecated"
        i["deprecated_at"] = when
        i["deprecated_by"] = cfg["agent"]
        if reason.strip():
            i["deprecated_reason"] = _demojibake(reason.strip())
        i["updated_at"] = when

    # local
    data = _read_local(cfg)
    for i in data["items"]:
        if i["id"] == item_id:
            _retire(i)
            _write_local(cfg, data)
            return json.dumps({"status": "deprecated", "scope": "local",
                               "item": i}, indent=2)

    # team
    found = {}
    def mark_team(team_data):
        for i in team_data["items"]:
            if i["id"] == item_id:
                _retire(i)
                found["item"] = i
                return None
        return False
    ok = _team_mutate(cfg, mark_team,
                      f"cambium: {cfg['agent']} deprecates {item_id}")
    if found.get("item"):
        return json.dumps({"status": "deprecated", "scope": "team",
                           "item": found["item"]}, indent=2)
    if not ok:
        return json.dumps({"status": "retry_exhausted"})

    # org
    if cfg["org_repo"] and any(i["id"] == item_id for i in _read_org(cfg)):
        def mark_org(org_data):
            for i in org_data["items"]:
                if i["id"] == item_id:
                    _retire(i)
                    return None
            return False
        msg = f"cambium: deprecate {item_id}"
        if cfg["org_pr"]:
            ok2, detail, url = _org_mutate_pr(cfg, mark_org, msg)
            if not ok2:
                return json.dumps({"status": "failed", "detail": detail})
            return json.dumps({"status": "deprecated", "scope": "org",
                               "via": "pr", "pr_url": url, "detail": detail},
                              indent=2)
        ok2, detail = _org_mutate_direct(cfg, mark_org, msg)
        if not ok2:
            return json.dumps({"status": "failed", "detail": detail})
        return json.dumps({"status": "deprecated", "scope": "org",
                           "via": "direct", "detail": detail}, indent=2)

    return json.dumps({"error": f"No item '{item_id}' in local, team, or org "
                       "scope."})


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

    # ---- team -> org, plus the solo local -> org fast lane -------------- #
    if item_id and to_scope == "org":
        if not cfg["org_repo"]:
            return json.dumps({"error": "CAMBIUM_ORG_REPO is not configured."})
        mode = _detect_mode(cfg)
        # Team is the normal origin for an org promotion. A solo builder can also
        # promote straight from local -- the team hop only earns its keep when
        # there is actually a team to stage for.
        src = next((i for i in _read_team(cfg) if i["id"] == item_id), None)
        src_scope = "team" if src else None
        if not src and mode == "solo":
            src = next((i for i in _read_local(cfg)["items"]
                        if i["id"] == item_id), None)
            if src:
                src_scope = "local"
        if not src:
            hint = ("Promote local items to team first." if mode == "team"
                    else "No such item in team or local scope.")
            return json.dumps({"error": f"No item '{item_id}' in team scope. {hint}"})

        item = dict(src)
        stamped = _now()
        # Endorsement gate. Team scope needs a peer's deliberate vouch. In solo
        # mode this promote-to-org call IS that deliberate act, so it counts as
        # the endorsement (auto-stamped for the audit trail). The generalization
        # gate below is NOT relaxed -- it is the quality bar that survives solo.
        if mode == "solo":
            item.setdefault("trust", {}).setdefault("endorsements", [])
            if not item["trust"]["endorsements"]:
                item["trust"]["endorsements"] = [{
                    "by": cfg["agent"], "at": stamped,
                    "note": "solo fast-lane: promote to org is the vouch",
                }]
        elif not force and not _eligible_org(src):
            return json.dumps({
                "status": "not_eligible",
                "message": "team->org requires at least one endorsement "
                           "(endorse() it, or force=True).",
            })

        # Generalization gate: org is a wider readership than any one repo, so a
        # project-specific body must be restated before it crosses (or forced).
        # Enforced in EVERY mode -- solo does not get to skip this.
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

        # Drop or stamp the promoted copy in its source scope (team branch, or
        # the local store for the solo fast lane).
        def _drop_source():
            if src_scope == "team":
                def remove(data):
                    before = len(data["items"])
                    data["items"] = [i for i in data["items"]
                                     if i["id"] != item_id]
                    return None if len(data["items"]) != before else False
                _team_mutate(cfg, remove, f"cambium: {item_id} promoted to org")
            else:
                ld = _read_local(cfg)
                ld["items"] = [i for i in ld["items"] if i["id"] != item_id]
                _write_local(cfg, ld)

        def _stamp_source_pr(url):
            if src_scope == "team":
                def mark(data):
                    for i in data["items"]:
                        if i["id"] == item_id:
                            i["promotion"] = {"pr": url, "at": _now()}
                            return None
                    return False
                _team_mutate(cfg, mark, f"cambium: org PR opened for {item_id}")
            else:
                ld = _read_local(cfg)
                for i in ld["items"]:
                    if i["id"] == item_id:
                        i["promotion"] = {"pr": url, "at": _now()}
                _write_local(cfg, ld)

        if cfg["org_pr"]:
            ok, url = _org_add_pr(cfg, item)
            if not ok:
                return json.dumps({"status": "failed", "detail": url})
            _stamp_source_pr(url)
            return json.dumps({"status": "pr_opened", "pr_url": url, "mode": mode,
                               "from": src_scope,
                               "note": f"{src_scope} copy stays until the PR merges"},
                              indent=2)
        ok, err = _org_add_direct(cfg, item)
        if not ok:
            return json.dumps({"status": "failed", "detail": err})
        _drop_source()
        return json.dumps({"status": "promoted", "to": "org", "mode": mode,
                           "from": src_scope, "item": item}, indent=2)

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
        # The local copies are deliberately NOT removed here — this is the only
        # remaining copy until the team branch is verifiably on the remote.
        return json.dumps({
            "status": "retry_exhausted",
            "promoted": 0,
            "warning": "The team branch could not be published, so nothing was "
                       "promoted and your local copies were left untouched. A "
                       "commit may exist locally on "
                       f"'{cfg['team_branch']}' in {cfg['repo']}; check "
                       f"`git -C \"{cfg['repo']}\" log {cfg['team_branch']}` and "
                       f"`git -C \"{cfg['repo']}\" push {cfg['remote']} "
                       f"{cfg['team_branch']}`. See .git/cambium.log for the "
                       "push error.",
            "items": [{"id": c["id"], "content": c["content"]}
                      for c in candidates],
        }, indent=2)
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

    board_repo, board_source, board_problem = _resolve_board_repo(cfg)
    board_state = {"repo": board_repo, "source": board_source,
                   "found": board_problem is None}
    if board_problem:
        board_state["problem"] = board_problem

    state.update({
        "mode": _detect_mode(cfg),
        "scopes": {"local": count(local["items"]), "team": count(team),
                   "org": count(org) if cfg["org_repo"] else "not configured"},
        "imported": {"context_keeper": len(local["imported"]["context_keeper"]),
                     "agentsync": len(local["imported"]["agentsync"]),
                     "import": len(local["imported"]["import"])},
        "substrates": {
            "agentsync_branch": cfg["agentsync_branch"],
            # Which board distill will actually read, and whether it exists.
            # Reporting only the branch NAME hid the fact that no repo in
            # scope had that branch at all.
            "agentsync_board": board_state,
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
# pages — a synthesis tier compiled FROM context-keeper entries
#
# A page is a build artifact, not a source. Three properties define it, and each
# one is enforced somewhere rather than merely intended:
#
#   deletable/regenerable : pages.json can be deleted and every page rebuilt
#                           from .context/ alone. Nothing is authored here, so
#                           nothing can be lost here.
#   never a trust-tier read : pages live outside knowledge.json (see PAGES_FILE)
#                           and cambium never writes .context/, so a page can
#                           reach neither recall() nor reload_constraints().
#   computed staleness    : a page carries the exact entry ids it compiled from
#                           plus each one's status and CONTENT HASH, so "is this
#                           page still true" is answered by comparison, never by
#                           a heuristic or an age threshold.
#
# Why a content hash and not just updated_at: context-keeper's stores are
# documented as human-editable JSON and are edited by hand in practice, so a
# body can change with no timestamp bump — timestamp-only staleness would report
# a page green while its sources had moved. The reverse also happens:
# _backfill_updated_at stamps updated_at onto entries that never changed. The
# hash is authoritative; updated_at is kept for display and corroboration only.
# --------------------------------------------------------------------------- #

# (filename, type name, title field). Pages read all THREE entry kinds —
# distill() reads only decisions and constraints, so pages are deliberately a
# superset of what cambium imports as knowledge.
ENTRY_FILES = (("decisions.json", "decisions", "summary"),
               ("constraints.json", "constraints", "rule"),
               ("pipelines.json", "pipelines", "name"))

# Fields excluded from an entry's content hash: lifecycle and bookkeeping that
# either has its own staleness cause (status, superseded_by) or moves without
# the entry's meaning changing (verified_at refreshes, mirror touches, the
# updated_at backfill). Everything else is hashed — including fields added by
# future schema versions, so a new field makes pages stale and asks a human to
# look, which is the safe direction for a staleness detector to fail in.
_HASH_EXCLUDED = frozenset((
    "created_at", "updated_at", "verified_at", "verified_sha",
    "status", "superseded_by", "schema_version",
))

PAGE_TOPIC_FLOOR = 0.34   # min token overlap for a topic selector to take an entry
PAGE_MIN_CLUSTER = 2      # tags with fewer active entries than this get no page


def _pages_context_dir(cfg, project):
    """Resolve a project name to its .context/ directory, or raise ConfigError
    with the list of names that WOULD work. Falls back to the configured repo
    when the caller names it (or names nothing), so single-project use needs no
    CAMBIUM_PROJECTS map at all."""
    if not project or project == cfg["project"]:
        return cfg["project"], cfg["context_dir"]
    path = cfg["projects"].get(project)
    if not path:
        known = sorted(set(list(cfg["projects"]) + [cfg["project"]]))
        raise ConfigError(
            "unknown project %r. Known projects: %s. Add it with "
            "CAMBIUM_PROJECTS (\"name=/abs/path\" pairs, or a JSON object) so "
            "cambium can resolve the name to a store." % (project, ", ".join(known)))
    return project, os.path.join(path, ".context")


def _entry_content_hash(entry):
    """Stable hash of an entry's MEANING. Sorted keys so dict order in the file
    can't change it; ensure_ascii=False so an em-dash hashes as itself rather
    than as its escape."""
    body = {k: v for k, v in entry.items() if k not in _HASH_EXCLUDED}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _read_entries(context_dir):
    """All entries in a .context/ store as {entry_id: (type_name, title_field,
    entry)}. Reads only the three entry files by name — .context/ also holds
    usage.json, embeddings.json and .mirror_conflicts.json, which are
    per-machine telemetry, not entries."""
    out = {}
    for fname, tname, title_f in ENTRY_FILES:
        path = os.path.join(context_dir, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and e.get("id"):
                out[e["id"]] = (tname, title_f, e)
    return out


def _entry_title(record):
    tname, title_f, e = record
    return _demojibake(e.get(title_f) or e.get("summary") or e.get("id") or "?")


def _entry_text(record):
    """Everything about an entry a topic query could reasonably match."""
    _tname, _title_f, e = record
    parts = [str(e.get(k, "")) for k in (
        "summary", "rule", "name", "problem", "why_chosen", "reason",
        "purpose", "what_we_tried", "tradeoffs", "triggering_incident",
        "when_to_invoke")]
    parts += [str(t) for t in e.get("tags", [])]
    parts += [str(h) for h in e.get("retrieval_hints", [])]
    return " ".join(p for p in parts if p)


def _entry_is_active(record):
    return (record[2].get("status") or "active") == "active"


def _select_entries(entries, tag="", topic=""):
    """Pick the ACTIVE entries a page compiles from, plus the candidate count
    (every active entry in the store). The denominator is recorded because a
    tag-selected page is silently partial by nature — tags are free-form and
    context-keeper's own verify_quality flags a no_tags population — so a page
    that says "7 of 31" is honest where a bare list of 7 is not."""
    active = {eid: rec for eid, rec in entries.items() if _entry_is_active(rec)}
    if tag:
        want = tag.strip().lower()
        chosen = {eid: rec for eid, rec in active.items()
                  if want in {str(t).lower() for t in rec[2].get("tags", [])}}
    elif topic:
        q = _tokens(topic)
        chosen = {}
        for eid, rec in active.items():
            hay = _tokens(_entry_text(rec))
            if not q:
                continue
            hits = sum(1 for t in q if t in hay
                       or any(w.startswith(t) or t.startswith(w)
                              for w in hay if len(w) >= 3 and len(t) >= 3))
            if hits / len(q) >= PAGE_TOPIC_FLOOR:
                chosen[eid] = rec
        # An untagged entry can still match a topic query, which is exactly why
        # topic pages exist alongside tag pages.
    else:
        chosen = dict(active)
    return chosen, len(active)


def _page_slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return s or "page"


def _page_key(project, selector):
    """The page's slug, injective over (kind, raw value).

    _page_slug collapses every run of non-alphanumerics to one dash, so `ci/cd`,
    `ci-cd` and `ci cd` all produced the SAME slug -- and _upsert_page replaces
    by id, so the second cluster silently overwrote the first while
    compile_project counted both as covered. A tag named `all` collided with the
    all-entries page for the same reason.

    The kind is in the key, and a short digest of the raw value is appended
    whenever slugging was lossy, so two distinct tags can never land on one
    page. A slug that survives untouched keeps its clean name."""
    kind = selector.get("kind") or "all"
    raw = str(selector.get("value") or "")
    slug = _page_slug(f"{project}-{raw or kind}")
    lossy = raw and _page_slug(raw) != raw.strip().lower()
    if kind in ("topic", "index", "unfiled", "all") or lossy:
        stamp = hashlib.sha1(f"{kind}\x00{raw}".encode("utf-8")).hexdigest()[:6]
        if kind in ("index", "unfiled", "all") and not lossy:
            return _page_slug(f"{project}-{kind}")   # stable, no rival possible
        return f"{slug}-{stamp}"
    return slug


def _related_slugs(own_slug, own_ids, others, index_slug=None, limit=12):
    """Pages genuinely related to this one: they share at least one entry, or an
    entry here names an entry there.

    Not "every other page in the project". Linking to all siblings produced
    17,488 wikilinks across 221 pages — about 79 per page — which in a vault is
    a hairball and in a backlinks panel is noise. A link that is always present
    says nothing; these say "the same decision appears on both of these".

    `others` is {slug: set(entry_ids)}. The index is always included, because
    that link is navigation rather than a claim about relatedness."""
    scored = []
    for slug, ids in others.items():
        if slug == own_slug or not ids:
            continue
        shared = own_ids & ids
        if shared:
            scored.append((len(shared), slug))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = [slug for _n, slug in scored[:limit]]
    if index_slug and index_slug != own_slug:
        out.append(index_slug)
    return out


def _cluster_tally(active, floor=None):
    """tag -> {entry ids}, keeping only tags at or above the cluster floor."""
    floor = PAGE_MIN_CLUSTER if floor is None else floor
    tally = {}
    for eid, rec in active.items():
        for t in rec[2].get("tags", []):
            tally.setdefault(str(t).lower(), set()).add(eid)
    return {t: ids for t, ids in tally.items() if len(ids) >= floor}


def _unfiled_ids(entries, floor=None):
    """Active entries no cluster page covers — the population an `unfiled` page
    exists to make visible.

    The floor is passed in rather than assumed, and compile_project records it
    on the selector: a project built with min_cluster=3 has a different uncovered
    set than the default, and a staleness check using the wrong floor would
    report drift that never happened."""
    active = {eid: rec for eid, rec in entries.items() if _entry_is_active(rec)}
    covered = {eid for ids in _cluster_tally(active, floor).values() for eid in ids}
    return set(active) - covered


def _selector_matches(selector, entries):
    """The entry ids a selector SHOULD cover right now, or None when the
    selector is not a function of entries at all.

    None is the important return: an `index` page summarises other pages and has
    no entry sources, so re-running a selector over it would report every entry
    in the project as newly matching. That is exactly what happened the first
    time this shipped — every index and unfiled page came out stale the instant
    it was compiled, because both fell through to the "no tag, no topic, so
    match everything" branch of _select_entries."""
    kind = selector.get("kind")
    if kind == "tag":
        chosen, _ = _select_entries(entries, tag=selector.get("value", ""))
        return set(chosen)
    if kind == "topic":
        chosen, _ = _select_entries(entries, topic=selector.get("value", ""))
        return set(chosen)
    if kind == "unfiled":
        return _unfiled_ids(entries, selector.get("floor"))
    if kind == "all":
        chosen, _ = _select_entries(entries)
        return set(chosen)
    return None   # index, and any future selector with no entry basis


def _source_record(eid, record, role="primary"):
    """A page's record of one source entry.

    `role` distinguishes the entries the selector CHOSE (primary) from the ones
    pulled in to build an arc — a superseded predecessor quoted in the "was X,
    changed because Y" line (context). The distinction is load-bearing for
    staleness: a context source is superseded BY DEFINITION, so treating its
    status as a staleness cause would make every page with any history on it
    permanently stale. Its content is still hashed, because if the predecessor's
    text is edited the page's rendered body changes and the page really is out
    of date."""
    tname, _title_f, e = record
    return {
        "entry_id": eid,
        "type": tname,
        "role": role,
        "status": e.get("status") or "active",
        "updated_at": e.get("updated_at") or e.get("created_at") or "",
        "content_hash": _entry_content_hash(e),
    }


def _ref(eid, entries, entries_on_page=frozenset()):
    """Render a cross-reference as a STATEMENT, not a bare id.

    The whole difference between a log and a synthesis lives here: the stores
    carry a graph — related_to, constraints_created, superseded_by — and every
    edge is an id sitting inert in the JSON. Resolving one to its subject is how
    a page says something no single entry says."""
    rec = entries.get(eid)
    if rec is None:
        return f"`{eid}` *(not in this store)*", False
    title = _oneline(_entry_title(rec))
    if len(title) > 120:
        title = title[:119].rstrip() + "…"
    here = " (on this page)" if eid in entries_on_page else ""
    return f"`{eid}` — {title}{here}", True


def _predecessors(eid, entries):
    """Entries this one replaced: anything whose superseded_by points at it.

    Reads the edge from the OLD entry, which is where context-keeper writes it,
    rather than expecting a `supersedes` list on the new one."""
    out = []
    for other_id, rec in entries.items():
        if rec[2].get("superseded_by") == eid and other_id != eid:
            out.append(other_id)
    return sorted(out)


def _predecessor_line(old_id, old_rec, new_rec):
    """One compact line of change history, in context-keeper's own format
    (server.py::_predecessor_line) so the two surfaces read identically."""
    was = _oneline(_demojibake(_entry_title(old_rec)))
    if len(was) > 140:
        was = was[:139].rstrip() + "…"
    why = (old_rec[2].get("deprecated_reason")
           or new_rec[2].get("problem") or "").strip()
    why = _oneline(_demojibake(why))
    if len(why) > 200:
        why = why[:199].rstrip() + "…"
    line = f'supersedes `{old_id}`: was "{was}"'
    return line + (f" — changed because: {why}" if why else "")


def _rendered_refs(chosen, entries):
    """Every entry whose TEXT the body renders but the selector did not choose.

    The invariant this exists to keep: a page tracks every entry it quotes. The
    body resolves references to titles, so an entry can reach the page through
    `related_to`, `constraints_created`, or a supersession edge without ever
    being selected — and an untracked quote is drift the staleness check cannot
    see. One level deep only; a resolved reference's own references are that
    page's business, not this one's."""
    extra = set()
    for eid in chosen:
        extra.update(_predecessors(eid, entries))
        e = chosen[eid][2]
        for field in ("related_to", "constraints_created"):
            for ref in (e.get(field) or []):
                if ref in entries:
                    extra.add(ref)
        # a constraint names the decision that created it, wherever that lives
        if chosen[eid][0] == "constraints":
            for other_id, rec in entries.items():
                if eid in (rec[2].get("constraints_created") or []):
                    extra.add(other_id)
    return extra - set(chosen)


def _constraint_origins(eid, entries):
    """Decisions whose constraints_created names this constraint — searched
    across the whole store, because the decision that produced a rule is worth
    naming even once it has itself been superseded."""
    return sorted(d for d, rec in entries.items()
                  if eid in (rec[2].get("constraints_created") or []))


def _page_tensions(chosen, entries, selector=None):
    """Cross-entry problems a page can assert deterministically.

    Deliberately only two checks, both cheap to justify:
      * a reference pointing at an id that is not in the store — always a real
        defect, never a matter of taste;
      * entries on this page sharing two or more tags with no link either way —
        a heuristic, so it is phrased as something to look at rather than a
        finding, and it is capped so a big cluster cannot bury the page.

    Deliberately NOT here: anything verify_quality already owns (thin reasons,
    missing tags, code drift, enforced_by resolution). Two tools computing the
    same judgement is how they come to disagree."""
    out = []
    for eid in sorted(chosen):
        e = chosen[eid][2]
        for field in ("related_to", "constraints_created"):
            for ref in (e.get(field) or []):
                # constraints_created legitimately holds prose descriptions of a
                # rule as well as ids; only id-shaped values are checked.
                if not re.match(r"^(dec|con|pipe)-", str(ref)):
                    continue
                if ref not in entries:
                    out.append(f"`{eid}` names {field} `{ref}`, which is not in "
                               "this store — the link is dead")
    # Every entry on a tag page shares the selector's tag BY CONSTRUCTION, so
    # counting it toward "shared tags" turns a >=2 threshold into >=1 and floods
    # the page. On the real stores that flagged 143 of 221 pages — a signal that
    # fires almost everywhere is not a signal.
    free = {(selector or {}).get("value", "").lower()} if (
        selector or {}).get("kind") == "tag" else set()
    pairs = []
    ids = sorted(chosen)
    for i, a in enumerate(ids):
        ta = {str(t).lower() for t in chosen[a][2].get("tags", [])} - free
        la = set(chosen[a][2].get("related_to") or [])
        for b in ids[i + 1:]:
            tb = {str(t).lower() for t in chosen[b][2].get("tags", [])} - free
            lb = set(chosen[b][2].get("related_to") or [])
            shared = ta & tb
            if len(shared) >= 2 and a not in lb and b not in la:
                pairs.append((a, b, sorted(shared)))
    for a, b, shared in pairs[:5]:
        out.append(f"`{a}` and `{b}` share {', '.join(shared)} but neither links "
                   "the other — possibly one arc recorded as two entries")
    if len(pairs) > 5:
        out.append(f"…and {len(pairs) - 5} more unlinked pairs on this page")
    return out


def _entry_created(rec):
    return rec[2].get("created_at") or ""


def _ordered_steps(steps):
    """Pipeline steps as ordered strings, tolerating both shapes found in real
    stores: {order, action} objects and bare strings. Objects sort by `order`;
    strings keep their list position, since that IS their order."""
    if not isinstance(steps, list):
        return []
    dicts = [s for s in steps if isinstance(s, dict)]
    if dicts:
        dicts.sort(key=lambda s: s.get("order") or 0)
        return [_demojibake(str(s.get("action") or s.get("output") or "")).strip()
                for s in dicts]
    return [_demojibake(str(s)).strip() for s in steps if str(s).strip()]


def _render_page_body(project, title, selector, chosen, candidate_count,
                      related_slugs, entries):
    """Deterministic markdown, structured BY ROLE rather than by entry order.

    The page answers three questions in the order someone actually asks them —
    what is in force, how it came to be decided, what changed — instead of
    listing entries as they happen to sit in the file. Everything that makes it
    a synthesis rather than a reformat comes from the graph BETWEEN entries:
    references resolved to their subject, supersession rendered as a change
    line, and cross-entry problems asserted at the end. No sentence here is
    invented; the assembly is what is new.

    Carries NO timestamp: compiled_at lives in the page record, so an unchanged
    store recompiles to a byte-identical body (context-keeper's export_snapshot
    made the same choice for the same reason)."""
    on_page = frozenset(chosen)
    lines = [f"# {_demojibake(title)}", ""]
    sel = (f"tag `{selector['value']}`" if selector["kind"] == "tag"
           else f"topic “{selector['value']}”" if selector["kind"] == "topic"
           else "entries no cluster covers" if selector["kind"] == "unfiled"
           else "all active entries")
    lines += [
        f"*Compiled from {len(chosen)} of {candidate_count} active entries in "
        f"`{project}`, selected by {sel}.*", "",
        "> This page is a build artifact. Edit the entries, not this file — it "
        "is regenerated from `.context/` and any edit here is lost on recompile.",
        "",
    ]

    by_type = {}
    for eid, rec in chosen.items():
        by_type.setdefault(rec[0], []).append((eid, rec))

    def field_block(e, pairs):
        out = []
        for field, heading in pairs:
            val = (e.get(field) or "").strip()
            if val:
                out += [f"**{heading}:** {_demojibake(val)}", ""]
        return out

    # --- rules in force ---------------------------------------------------- #
    # Constraints lead, because they are the part that governs what you may do
    # next. Buried in entry order they read as trivia; at the top they read as
    # the operative rules they are.
    cons = sorted(by_type.get("constraints", []), key=lambda p: p[0])
    if cons:
        lines += ["## Rules in force", ""]
        for eid, rec in cons:
            e = rec[2]
            bits = [b for b in (e.get("hardness") or "",
                                f"scope `{e['scope']}`" if e.get("scope") else "")
                    if b]
            head = f"### {_entry_title(rec)}  `{eid}`"
            lines += [head, ""]
            if bits:
                lines += ["*" + " · ".join(bits) + "*", ""]
            if e.get("enforced_by"):
                lines += [f"Enforced by `{_demojibake(e['enforced_by'])}`.", ""]
            lines += field_block(e, (("reason", "Why"),
                                     ("triggering_incident", "What happened")))
            # The decision that produced this rule, named — the single most
            # useful edge in the store and the one nothing was following.
            for d in _constraint_origins(eid, entries):
                text, _ok = _ref(d, entries, on_page)
                lines += [f"Created by {text}", ""]

    # --- how it got decided ------------------------------------------------ #
    decs = sorted(by_type.get("decisions", []),
                  key=lambda p: (_entry_created(p[1]), p[0]))
    if decs:
        lines += ["## How this got decided", ""]
        for eid, rec in decs:
            e = rec[2]
            lines += [f"### {_entry_title(rec)}  `{eid}`", ""]
            # Change history inline: what this replaced and why it moved.
            for old in _predecessors(eid, entries):
                lines += [_predecessor_line(old, entries[old], rec), ""]
            lines += field_block(e, (("problem", "Problem"),
                                     ("why_chosen", "Why"),
                                     ("what_we_tried", "What we tried"),
                                     ("tradeoffs", "Tradeoffs")))
            for field, heading in (("constraints_created", "Rules this created"),
                                   ("related_to", "Related")):
                refs = [r for r in (e.get(field) or []) if r]
                if not refs:
                    continue
                lines.append(f"**{heading}:**")
                for r in sorted(refs):
                    text, _ok = _ref(r, entries, on_page)
                    lines.append(f"- {text}")
                lines.append("")

    # --- pipelines --------------------------------------------------------- #
    pipes = sorted(by_type.get("pipelines", []), key=lambda p: p[0])
    if pipes:
        lines += ["## Pipelines", ""]
        for eid, rec in pipes:
            e = rec[2]
            lines += [f"### {_entry_title(rec)}  `{eid}`", ""]
            lines += field_block(e, (("purpose", "Purpose"),
                                     ("when_to_invoke", "When to invoke")))
            # steps are documented as {order, action, output} objects, but real
            # stores also carry plain strings (older entries, hand-edited ones).
            # A synthesis layer reads what is THERE, not what the schema says.
            for n, s in enumerate(_ordered_steps(e.get("steps")), start=1):
                lines.append(f"{n}. {s}")
            lines.append("")

    # --- what changed ------------------------------------------------------ #
    # The superseded entries behind everything above, gathered in one place so
    # the arc is legible without the retired text competing with what is current.
    history = []
    for eid, _rec in decs + cons:
        for old in _predecessors(eid, entries):
            history.append((old, eid))
    if history:
        lines += ["## What changed", ""]
        for old, new in sorted(set(history)):
            old_text, _ = _ref(old, entries, on_page)
            new_text, _ = _ref(new, entries, on_page)
            lines.append(f"- {old_text} → replaced by {new_text}")
        lines.append("")

    # --- worth a look ------------------------------------------------------ #
    tensions = _page_tensions(chosen, entries, selector)
    if tensions:
        lines += ["## Worth a look", "",
                  "*Detected across entries, not asserted by any one of them.*",
                  ""]
        lines += [f"- {t}" for t in tensions]
        lines.append("")

    if related_slugs:
        lines += ["## Related pages", ""]
        lines += [f"- [[{s}]]" for s in sorted(related_slugs)]
        lines.append("")
    # `"Sources: " + x or "none"` binds as `("Sources: " + x) or "none"`, and a
    # non-empty prefix is always truthy — the fallback could never fire.
    refs = ", ".join(f"`{eid}`" for eid in sorted(chosen))
    lines += ["---", "", "Sources: " + (refs if refs else "none"), ""]
    return "\n".join(lines)


def _empty_pages():
    return {"pages": []}


def _read_pages(cfg):
    path = cfg["pages_store"]
    if not os.path.exists(path):
        return _empty_pages()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return _empty_pages()
    except json.JSONDecodeError:
        # QUARANTINE, exactly as _read_local does. The old comment claimed there
        # was "nothing to lose because pages are wholly derived" -- but a page's
        # TITLE and a topic selector's query string are typed by a human into
        # compile_page and exist nowhere in .context/, and the set of projects
        # that had been compiled is not recoverable either. Worse, returning
        # empty here meant the next compile wrote that empty store back plus one
        # page, silently destroying every other compiled page while reporting
        # success -- the precise failure _read_local was fixed for.
        _quarantine_corrupt(path)
        return _empty_pages()
    if not isinstance(data, dict):
        _quarantine_corrupt(path)
        return _empty_pages()
    data.setdefault("pages", [])
    return data


def _write_pages(cfg, data):
    _atomic_write_json(cfg["pages_store"], data)


def _build_page(project, title, selector, chosen, candidate_count,
                related_slugs, entries=None):
    """Assemble a page record. `identity` is the idempotence key: body plus the
    exact source fingerprints, deliberately EXCLUDING compiled_at. Two compiles
    of an unchanged store produce equal identities and unequal compiled_at, so
    "recompile changed nothing" is a testable claim rather than a hope."""
    entries = chosen if entries is None else entries
    body = _render_page_body(project, title, selector, chosen,
                             candidate_count, related_slugs, entries)
    sources = [_source_record(eid, rec) for eid, rec in chosen.items()]
    # Everything the body quotes but the selector did not choose — predecessors
    # in the change lines, and entries a resolved reference names. The page's
    # body contains their text, so a page that did not track them could render
    # stale quotes and still report itself clean. Marked `context` so a
    # predecessor's (inevitable) superseded status is not itself a cause.
    for eid in sorted(_rendered_refs(chosen, entries)):
        sources.append(_source_record(eid, entries[eid], role="context"))
    seen, deduped = set(), []
    for s in sorted(sources, key=lambda s: (s["entry_id"], s["role"])):
        if s["entry_id"] in seen:
            continue
        seen.add(s["entry_id"])
        deduped.append(s)
    sources = deduped
    body_hash = hashlib.sha1(body.encode("utf-8")).hexdigest()
    ident = hashlib.sha1(
        (body_hash + json.dumps(sources, sort_keys=True)).encode("utf-8")
    ).hexdigest()
    return {
        "id": "page-" + _page_key(project, selector),
        "project": project,
        "title": title,
        "slug": _page_key(project, selector),
        "selector": selector,
        "sources": sources,
        "candidate_count": candidate_count,
        "body": body,
        "body_hash": body_hash,
        "identity": ident,
        "compiled_at": _now(),
    }


def _page_staleness(page, entries):
    """Compare a page's recorded sources against the store as it is NOW.

    Returns (stale, causes). Every cause names the entry that caused it, because
    "this page is stale" without the offending id just moves the search to the
    human. Causes are ordered by entry id so the report is stable."""
    causes = []
    for src in page.get("sources", []):
        eid = src["entry_id"]
        rec = entries.get(eid)
        if rec is None:
            causes.append({"entry_id": eid, "cause": "orphaned",
                           "detail": "source entry no longer exists in the store"})
            continue
        e = rec[2]
        status = e.get("status") or "active"
        if src.get("role") == "context":
            # Quoted history. It is superseded because that is why it is on the
            # page; only an edit to its text (or its disappearance, handled
            # above) changes what the page says.
            if _entry_content_hash(e) != src.get("content_hash"):
                causes.append({"entry_id": eid, "cause": "changed",
                               "detail": "quoted predecessor's content changed"})
            continue
        if status == "superseded":
            causes.append({"entry_id": eid, "cause": "superseded",
                           "detail": "superseded by %s" % (e.get("superseded_by") or "?")})
        elif status == "deprecated":
            causes.append({"entry_id": eid, "cause": "deprecated",
                           "detail": "entry was deprecated"
                                     + (" in favour of %s" % e["superseded_by"]
                                        if e.get("superseded_by") else "")})
        elif _entry_content_hash(e) != src.get("content_hash"):
            # Body moved. Note this fires whether or not updated_at moved with
            # it — a hand edit that never bumped the timestamp still lands here.
            causes.append({"entry_id": eid, "cause": "changed",
                           "detail": "entry content changed since compile"})
    # A page can also go stale by OMISSION: an entry recorded after the compile
    # that the selector now matches belongs on the page and isn't there. Pure
    # source-diffing never sees this, and it is the common case for a tag page
    # on an active project.
    expected = _selector_matches(page.get("selector") or {}, entries)
    if expected is not None:
        # Compared against the PRIMARY sources only: context sources are quoted
        # history, not things the selector chose.
        known = {s["entry_id"] for s in page.get("sources", [])
                 if s.get("role", "primary") == "primary"}
        for eid in sorted(expected - known):
            causes.append({"entry_id": eid, "cause": "new_match",
                           "detail": "entry now matches this page's selector"})
        # The symmetric case, which nothing was checking. On a tag page a
        # departing entry is caught incidentally because its own hash moves, but
        # UNFILED membership is a function of OTHER entries: a second entry
        # pushing a tag over the cluster floor takes the first one out of the
        # uncovered set without touching its bytes. The page kept listing it,
        # reported clean, and the newly-formed cluster had no page at all.
        for eid in sorted(known - expected):
            if eid in entries:    # a missing entry is already `orphaned` above
                causes.append({"entry_id": eid, "cause": "no_longer_matches",
                               "detail": "entry no longer matches this page's "
                                         "selector"})
    causes.sort(key=lambda c: (c["entry_id"], c["cause"]))
    return (bool(causes), causes)


def _compile_one(cfg, project, context_dir, title, selector, related_slugs=()):
    entries = _read_entries(context_dir)
    chosen, candidates = _select_entries(
        entries,
        tag=selector["value"] if selector["kind"] == "tag" else "",
        topic=selector["value"] if selector["kind"] == "topic" else "")
    return _build_page(project, title, selector, chosen, candidates,
                       related_slugs, entries)


def _upsert_page(data, page):
    """Replace by id, preserving list order so pages.json diffs stay readable."""
    for i, existing in enumerate(data["pages"]):
        if existing.get("id") == page["id"]:
            data["pages"][i] = page
            return "recompiled"
    data["pages"].append(page)
    return "compiled"


def _page_view(page, stale, causes):
    """The list/report shape: everything but the body, which is large and is
    what the markdown file is for."""
    return {
        "id": page["id"],
        "project": page["project"],
        "title": page["title"],
        "selector": page["selector"],
        "sources": [s["entry_id"] for s in page.get("sources", [])],
        "source_count": len(page.get("sources", [])),
        "candidate_count": page.get("candidate_count"),
        "compiled_at": page.get("compiled_at"),
        "stale": stale,
        "stale_causes": causes,
    }


@mcp.tool()
def compile_page(project: str = "", tag: str = "", topic: str = "",
                 title: str = "") -> str:
    """Compile ONE synthesis page from a project's context-keeper entries,
    selected by tag or by topic.

    The page is a build artifact: it is stored apart from cambium's knowledge
    items, is never returned by recall() or any trust-tier read, and can be
    deleted and rebuilt from .context/ at any time. It records the exact entry
    ids it compiled from together with each entry's status and content hash, so
    staleness is computed by comparison rather than guessed from age.

    Pass `tag` for an exact tag match or `topic` for a free-text match; with
    neither, the page covers every active entry in the project. `project`
    defaults to the configured repo — naming another one requires it to be in
    the CAMBIUM_PROJECTS map."""
    cfg, err = _require_cfg()
    if err:
        return err
    if tag and topic:
        return json.dumps({"error": "pass tag OR topic, not both"}, indent=2)
    try:
        project, context_dir = _pages_context_dir(cfg, project)
    except ConfigError as e:
        return json.dumps({"error": str(e)}, indent=2)
    if not os.path.isdir(context_dir):
        return json.dumps({
            "error": f"no context-keeper store at {context_dir}",
            "fix": "pages compile FROM context-keeper entries; record some "
                   "first, or point CAMBIUM_PROJECTS at the right clone.",
        }, indent=2)

    selector = ({"kind": "tag", "value": tag.strip()} if tag
                else {"kind": "topic", "value": topic.strip()} if topic
                else {"kind": "all", "value": ""})
    label = title.strip() or tag.strip() or topic.strip() or f"{project} — all entries"
    data = _read_pages(cfg)
    # A page never links to itself, so its own slug is excluded BEFORE compiling
    # — the links are part of the body, and a body that linked to itself would
    # differ from the same page compiled fresh.
    own_slug = _page_key(project, selector)
    entries_now = _read_entries(context_dir)
    own_ids = set(_select_entries(
        entries_now,
        tag=selector["value"] if selector["kind"] == "tag" else "",
        topic=selector["value"] if selector["kind"] == "topic" else "")[0])
    others = {p["slug"]: {s["entry_id"] for s in p.get("sources", [])}
              for p in data["pages"]
              if p.get("project") == project and p.get("slug")}
    index_slug = _page_key(project, {"kind": "index", "value": ""})
    siblings = _related_slugs(own_slug, own_ids, others,
                              index_slug if index_slug in others else None)
    page = _compile_one(cfg, project, context_dir, label, selector, siblings)
    action = _upsert_page(data, page)
    _write_pages(cfg, data)
    entries = _read_entries(context_dir)
    stale, causes = _page_staleness(page, entries)
    return json.dumps({
        "status": action,
        "page": _page_view(page, stale, causes),
        "body_preview": page["body"][:600],
        "store": cfg["pages_store"],
    }, indent=2)


@mcp.tool()
def compile_project(project: str = "", min_cluster: int = 0) -> str:
    """Compile a whole project: one page per tag cluster, plus an index page
    that links them with wikilinks.

    Clusters are tags carrying at least `min_cluster` active entries (default
    2). Entries that no cluster covers are compiled into an explicit "unfiled"
    page rather than dropped — an untagged entry is invisible to tag selection,
    and a synthesis layer that silently omits part of the store is worse than
    one that shows the gap."""
    cfg, err = _require_cfg()
    if err:
        return err
    try:
        project, context_dir = _pages_context_dir(cfg, project)
    except ConfigError as e:
        return json.dumps({"error": str(e)}, indent=2)
    if not os.path.isdir(context_dir):
        return json.dumps({"error": f"no context-keeper store at {context_dir}"},
                          indent=2)
    floor = min_cluster if min_cluster > 0 else PAGE_MIN_CLUSTER
    entries = _read_entries(context_dir)
    active = {eid: rec for eid, rec in entries.items() if _entry_is_active(rec)}

    tally = _cluster_tally(active, floor)
    clusters = sorted(tally)
    covered = {eid for ids in tally.values() for eid in ids}
    unfiled = sorted(set(active) - covered)

    index_slug = _page_key(project, {"kind": "index", "value": ""})
    # Entry set per page, known up front, so cross-links can be computed from
    # actual overlap instead of "everything else in this project".
    page_ids = {_page_key(project, {"kind": "tag", "value": t}): set(tally[t])
                for t in clusters}
    if unfiled:
        page_ids[_page_key(project, {"kind": "unfiled", "value": "unfiled"})] = set(unfiled)
    slugs = sorted(page_ids)

    data = _read_pages(cfg)
    # A rebuild REPLACES this project's pages: a cluster that no longer exists
    # must not linger as a page nothing can make stale.
    data["pages"] = [p for p in data["pages"] if p.get("project") != project]

    built = []
    for t in clusters:
        own = _page_key(project, {"kind": "tag", "value": t})
        page = _compile_one(cfg, project, context_dir, t,
                            {"kind": "tag", "value": t},
                            _related_slugs(own, page_ids[own], page_ids,
                                           index_slug))
        _upsert_page(data, page)
        built.append(page)
    if unfiled:
        chosen = {eid: active[eid] for eid in unfiled}
        own = _page_key(project, {"kind": "unfiled", "value": "unfiled"})
        page = _build_page(project, "unfiled",
                           {"kind": "unfiled", "value": "unfiled", "floor": floor},
                           chosen, len(active),
                           _related_slugs(own, page_ids[own], page_ids,
                                          index_slug),
                           entries)
        _upsert_page(data, page)
        built.append(page)

    index_body = ["# %s — index" % project, "",
                  "*%d active entries across %d cluster pages.*"
                  % (len(active), len(clusters)), ""]
    for t in clusters:
        index_body.append("- [[%s]] — %d entries" % (_page_key(project, {"kind": "tag", "value": t}),
                                                     len(tally[t])))
    if unfiled:
        index_body.append("- [[%s]] — %d entries matching no cluster"
                          % (_page_key(project, {"kind": "unfiled", "value": "unfiled"}), len(unfiled)))
    index = {
        "id": "page-" + index_slug, "project": project,
        "title": f"{project} — index", "slug": index_slug,
        "selector": {"kind": "index", "value": ""},
        # The index summarises pages, not entries, so it has no entry sources
        # and cannot go stale by source-diffing. It is rebuilt whenever the
        # project is, which is the only thing that can change it.
        "sources": [], "candidate_count": len(active),
        "body": "\n".join(index_body) + "\n",
        "body_hash": hashlib.sha1(("\n".join(index_body) + "\n").encode()).hexdigest(),
        "identity": hashlib.sha1(("index" + "|".join(slugs)).encode()).hexdigest(),
        "compiled_at": _now(),
    }
    _upsert_page(data, index)
    _write_pages(cfg, data)
    return json.dumps({
        "status": "compiled",
        "project": project,
        "index": index["id"],
        "pages": [p["id"] for p in built],
        "clusters": len(clusters),
        "unfiled_entries": len(unfiled),
        "active_entries": len(active),
        "coverage": {"on_a_cluster_page": len(covered), "unfiled": len(unfiled)},
        "store": cfg["pages_store"],
    }, indent=2)


@mcp.tool()
def list_pages(project: str = "", stale_only: bool = False) -> str:
    """List compiled pages with their computed staleness and, when stale, the
    entry that caused it.

    Staleness is recomputed against the live .context/ store on every call —
    never cached, never inferred from age. Causes are: `superseded` (a source
    was replaced), `deprecated`, `changed` (content hash moved, with or without
    a timestamp bump), `orphaned` (the source entry is gone), and `new_match`
    (an entry the selector now matches that the page never compiled)."""
    cfg, err = _require_cfg()
    if err:
        return err
    data = _read_pages(cfg)
    by_project = {}
    out = []
    for page in data["pages"]:
        proj = page.get("project") or cfg["project"]
        if project and proj != project:
            continue
        if proj not in by_project:
            try:
                _, ctx = _pages_context_dir(cfg, proj)
                by_project[proj] = _read_entries(ctx)
            except ConfigError:
                # The project left the map. Report the page as unresolvable
                # rather than silently clean — a page whose store cannot be
                # read is precisely the state a stale check must not call green.
                by_project[proj] = None
        entries = by_project[proj]
        if entries is None:
            out.append(dict(_page_view(page, True, [{
                "entry_id": "*", "cause": "unresolvable_project",
                "detail": "no store for project %r; add it to CAMBIUM_PROJECTS"
                          % proj}])))
            continue
        stale, causes = _page_staleness(page, entries)
        if stale_only and not stale:
            continue
        out.append(_page_view(page, stale, causes))
    return json.dumps({
        "pages": out,
        "count": len(out),
        "stale_count": sum(1 for p in out if p["stale"]),
        "store": cfg["pages_store"],
        "note": "Pages are build artifacts: delete the store and recompile to "
                "rebuild them. They are never returned by recall().",
    }, indent=2)


@mcp.tool()
def recompile(page_id: str = "", all_stale: bool = False) -> str:
    """Rebuild a page (or every stale page) from the current entries.

    Recompiling an unchanged store is a no-op in substance: the page's
    `identity` — its body plus its source fingerprints — is unchanged, and only
    `compiled_at` moves. The response reports `changed` per page so a caller can
    tell a real rebuild from a refresh."""
    cfg, err = _require_cfg()
    if err:
        return err
    if not page_id and not all_stale:
        return json.dumps(
            {"error": "pass page_id, or all_stale=True"}, indent=2)
    data = _read_pages(cfg)
    targets, results = [], []
    ctx_cache = {}

    def ctx_for(proj):
        if proj not in ctx_cache:
            try:
                ctx_cache[proj] = _pages_context_dir(cfg, proj)[1]
            except ConfigError:
                ctx_cache[proj] = None
        return ctx_cache[proj]

    for page in data["pages"]:
        proj = page.get("project") or cfg["project"]
        if page_id and page.get("id") != page_id:
            continue
        if all_stale and not page_id:
            ctx = ctx_for(proj)
            if ctx is None:
                continue
            stale, _ = _page_staleness(page, _read_entries(ctx))
            if not stale:
                continue
        targets.append(page)

    if page_id and not targets:
        return json.dumps({"error": f"no page with id {page_id!r}"}, indent=2)

    for page in targets:
        proj = page.get("project") or cfg["project"]
        ctx = ctx_for(proj)
        if ctx is None:
            results.append({"id": page["id"], "status": "skipped",
                            "reason": "project %r is not resolvable" % proj})
            continue
        if page["selector"].get("kind") == "index":
            results.append({"id": page["id"], "status": "skipped",
                            "reason": "index pages rebuild via compile_project"})
            continue
        others = {p["slug"]: {s["entry_id"] for s in p.get("sources", [])}
                  for p in data["pages"]
                  if p.get("project") == proj and p.get("slug")}
        idx = _page_key(proj, {"kind": "index", "value": ""})
        siblings = _related_slugs(
            page.get("slug"), {s["entry_id"] for s in page.get("sources", [])},
            others, idx if idx in others else None)
        if page["selector"].get("kind") == "unfiled":
            entries = _read_entries(ctx)
            active = {eid: rec for eid, rec in entries.items()
                      if _entry_is_active(rec)}
            chosen = {eid: active[eid] for eid in
                      sorted(_unfiled_ids(entries, page["selector"].get("floor")))}
            fresh = _build_page(proj, page["title"], page["selector"], chosen,
                                len(active), siblings, entries)
        else:
            fresh = _compile_one(cfg, proj, ctx, page["title"],
                                 page["selector"], siblings)
        changed = fresh["identity"] != page.get("identity")
        _upsert_page(data, fresh)
        results.append({"id": fresh["id"], "status": "recompiled",
                        "changed": changed,
                        "sources": len(fresh["sources"])})
    _write_pages(cfg, data)
    return json.dumps({
        "status": "ok",
        "recompiled": sum(1 for r in results if r["status"] == "recompiled"),
        "changed": sum(1 for r in results if r.get("changed")),
        "results": results,
    }, indent=2)


@mcp.tool()
def export_pages(out_dir: str = "", project: str = "") -> str:
    """Write every compiled page to disk as a markdown file — a readable wiki.

    One file per page, named `<slug>.md`, which is exactly what the `[[slug]]`
    links in the bodies already point at. That is Obsidian's link format, so
    opening the output directory as a vault gives working navigation and
    backlinks with no further setup.

    The directory is REGENERATED, not merged: files from a previous export whose
    pages no longer exist are removed, because a page tier that leaves orphans
    behind stops being a build artifact. Only files carrying this tool's own
    marker are ever deleted — anything else in the directory is left alone."""
    cfg, err = _require_cfg()
    if err:
        return err
    out_dir = _abspath(out_dir) if out_dir else os.path.join(
        cfg["repo"], LOCAL_DIR, "pages")
    data = _read_pages(cfg)
    pages = [p for p in data["pages"]
             if not project or p.get("project") == project]
    if not pages:
        return json.dumps({
            "error": "no compiled pages" + (f" for project {project!r}" if project
                                            else ""),
            "fix": "run compile_project first",
        }, indent=2)

    os.makedirs(out_dir, exist_ok=True)
    entries_cache = {}
    written, wanted = [], set()
    for page in sorted(pages, key=lambda p: p["id"]):
        proj = page.get("project") or cfg["project"]
        if proj not in entries_cache:
            try:
                entries_cache[proj] = _read_entries(_pages_context_dir(cfg, proj)[1])
            except ConfigError:
                entries_cache[proj] = {}
        stale, causes = _page_staleness(page, entries_cache[proj])
        name = f"{page.get('slug') or page['id']}.md"
        wanted.add(name)
        # Frontmatter is Obsidian Properties: it makes the vault queryable
        # (Dataview/Bases) without the page body having to carry the metadata.
        # No compiled_at — an unchanged store must export byte-identically.
        head = [
            "---",
            f"project: {proj}",
            f"page_id: {page['id']}",
            f"selector: {page['selector'].get('kind')}"
            + (f" / {page['selector']['value']}" if page["selector"].get("value") else ""),
            f"sources: {len(page.get('sources', []))}",
            f"stale: {'true' if stale else 'false'}",
            f"generated_by: {PAGES_MARKER}",
            "---",
            "",
        ]
        if stale:
            head += ["> [!warning] This page is stale",
                     "> " + "; ".join(f"`{c['entry_id']}` {c['cause']}"
                                      for c in causes[:6]), ""]
        with open(os.path.join(out_dir, name), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("\n".join(head) + page["body"])
        written.append(name)

    # The concept tier as one readable page, GENERATED. It was hand-maintained
    # for exactly one session before becoming a second copy of facts the
    # knowledge store already owned — which is this corpus's own rule about not
    # stating a fact in two places, and about prose drifting silently because
    # nothing executes it. The store is the authority; this is a projection.
    laws = _lessons_block(cfg, True, _mesh_index(cfg))
    if laws["laws"]:
        name = "_LESSONS.md"
        wanted.add(name)
        out = ["---", "title: What we've learned", f"laws: {laws['count']}",
               f"behind: {laws['needs_update']}",
               f"generated_by: {PAGES_MARKER}", "---", "",
               "# What we've learned", "",
               f"*{laws['count']} cross-project laws, drawn from the whole mesh. "
               "Generated from cambium's knowledge store — edit the laws there, "
               "not this file.*", ""]
        for law in laws["laws"]:
            out += [f"## {law['law']}", ""]
            if law.get("evidence"):
                out += [law["evidence"], ""]
            bits = []
            if law["evidence_projects"]:
                bits.append("Seen in: " + ", ".join(law["evidence_projects"]))
            if law["cites"]:
                bits.append("Cites: " + ", ".join(f"`{c}`" for c in law["cites"]))
            if bits:
                out += ["*" + " · ".join(bits) + "*", ""]
            if law["unincorporated"]:
                out += ["> [!note] Candidate evidence not yet cited: "
                        + ", ".join(f"`{e}`" for e in law["unincorporated"][:12]),
                        ""]
        with open(os.path.join(out_dir, name), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write("\n".join(out))
        written.append(name)

    # Reap only our own leftovers, identified by the marker in the frontmatter —
    # and, when a project filter is in effect, only that project's files. The
    # reaper previously deleted every page it had not just written, so exporting
    # one project into a shared vault removed every OTHER project's pages while
    # reporting success.
    removed = []
    for existing in sorted(os.listdir(out_dir)):
        if not existing.endswith(".md") or existing in wanted:
            continue
        path = os.path.join(out_dir, existing)
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(400)
            if PAGES_MARKER not in head:
                continue              # not ours — leave it alone
            if project:
                m = re.search(r"^project:\s*(.+)$", head, re.M)
                if not m or m.group(1).strip() != project:
                    continue          # another project's page; not this run's business
            os.remove(path)
            removed.append(existing)
        except OSError:
            continue
    return json.dumps({
        "status": "exported",
        "out_dir": out_dir,
        "written": len(written),
        "removed_orphans": removed,
        "stale_pages": sum(1 for p in pages
                           if _page_staleness(
                               p, entries_cache.get(p.get("project")
                                                    or cfg["project"], {}))[0]),
        "note": "Open this directory as an Obsidian vault — the [[links]] in "
                "the page bodies resolve to these filenames.",
    }, indent=2)


# --------------------------------------------------------------------------- #
# snapshot export — one JSON file describing the whole mesh, for a static
# dashboard to render with no server and no live store access.
#
# Two rules shape what goes in it:
#
#   1. No bodies by default. Entry text (summary/rule/name, and the `summary`
#      verify_quality echoes into every flagged item) is omitted unless the
#      caller asks for it. Counts, statuses, ids and edges are enough to render
#      the whole dashboard; the prose is opt-in.
#   2. Nothing that was not checked may render as checked. verify_quality lives
#      in context-keeper, so cambium shells out to it. When that call cannot be
#      made the snapshot says checked=false with the reason — it never emits an
#      empty gap list, which a dashboard would draw as a clean bill of health.
#      (Same lesson as distill's "a skipped step must not look like a completed
#      one" and context-keeper's own drift_checked flag.)
#
# Like context-keeper's export_snapshot, the payload carries NO generated-at
# timestamp: an unchanged mesh exports byte-identically, so committing it does
# not churn git and a diff means something actually moved.
# --------------------------------------------------------------------------- #
SNAPSHOT_SCHEMA = 1


def _ck_runner():
    """How to invoke context-keeper's CLI, or None. Prefers an explicit path
    (CAMBIUM_CONTEXT_KEEPER — either the console script or server.py) and falls
    back to the console script on PATH."""
    from shutil import which
    explicit = os.environ.get("CAMBIUM_CONTEXT_KEEPER", "").strip()
    if explicit:
        p = _abspath(explicit)
        if os.path.isfile(p):
            return [sys.executable, p] if p.endswith(".py") else [p]
        return None
    found = which("context-keeper")
    return [found] if found else None


def _quality_gaps(project_dir, include_bodies):
    """context-keeper's verify_quality for one project, via its CLI.

    A subprocess rather than an import: cambium's whole integration model is to
    read substrates in place and never couple to the other tool's code, and
    importing context-keeper's server would pull its mirror/urllib/usage stack
    into this process for one call."""
    runner = _ck_runner()
    if not runner:
        return {"checked": False,
                "reason": "context-keeper CLI not found; set "
                          "CAMBIUM_CONTEXT_KEEPER to its server.py or install "
                          "the console script",
                "gaps": None}
    try:
        p = subprocess.run(
            runner + ["verify_quality", json.dumps({"project_dir": project_dir})],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
            env=_noninteractive_env())
        # THE EXIT CODE IS THE ANSWER. Without this check a crashed
        # verify_quality -- an import traceback, an unknown-tool exit 2 from an
        # older build, a wrong CAMBIUM_CONTEXT_KEEPER target -- writes its error
        # to stderr, leaves stdout empty, decodes as {} and sails through every
        # guard below as "checked: true, zero gaps". A scan that never ran would
        # render as a clean bill of health for every project in the snapshot.
        if p.returncode != 0 or not (p.stdout or "").strip():
            reason = (p.stderr or "").strip() or (
                "verify_quality exited %d with no output" % p.returncode)
            return {"checked": False, "reason": reason[:300], "gaps": None}
        data = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"checked": False, "reason": f"verify_quality failed: {e}",
                "gaps": None}
    if not isinstance(data, dict) or "flagged" in data and not isinstance(
            data.get("flagged"), list):
        return {"checked": False, "reason": "unparseable verify_quality output",
                "gaps": None}
    if data.get("error"):
        return {"checked": False, "reason": str(data["error"]), "gaps": None}
    gaps = []
    for f in data.get("flagged", []):
        # An issue is {"type": "isolated", "detail": "..."} — the LABEL is
        # `type`, and `detail` can quote the entry (the mojibake check echoes
        # the damaged text), so detail travels only when bodies are allowed.
        issues = []
        for i in f.get("issues", []):
            if isinstance(i, dict):
                issues.append({"type": i.get("type") or "?",
                               **({"detail": _demojibake(i.get("detail") or "")}
                                  if include_bodies else {})})
            else:
                issues.append({"type": str(i)})
        row = {"id": f.get("id"), "type": f.get("type"), "issues": issues}
        if include_bodies:
            row["summary"] = _demojibake(f.get("summary") or "")
        gaps.append(row)
    return {
        "checked": True,
        "gaps": gaps,
        "count": len(gaps),
        "total_active": data.get("total_active"),
        # Carried through verbatim: "we could not look" is not "nothing drifted".
        "drift_checked": data.get("drift_checked"),
    }


LAW_TAG = "xylem-law"      # marks a cross-project concept page in the knowledge store
DECISIONS_FILE = "decisions.json"   # judgements made on the dashboard


def _empty_decisions():
    """One place for the shape, because three copies of a literal drift.

    `link_evals` holds an agent's AUDIT of a proposal, keyed the same
    project:newer:older as a dismissal. It is advisory by construction: a
    verdict here changes what the reviewer reads, never what the store says.
    The tap is still the gate."""
    return {"dismissed_links": [], "law_citations": {}, "law_dismissed": {},
            "link_evals": {}, "law_evals": {}, "eval_pending": [],
            "repair_proposals": {}, "repair_dismissed": []}


def _read_decisions(cfg):
    """Judgements the operator has already made — dismissed link proposals,
    per-law rulings on candidate evidence, and agent audits awaiting a ruling.

    A review surface that re-proposes what you already rejected is worse than
    one that proposes nothing: it trains you to stop reading it. These are a
    record of judgement, not a cache, so a dismissal stands until it is
    explicitly reversed."""
    path = os.path.join(os.path.dirname(cfg["local_store"]), DECISIONS_FILE)
    if not os.path.exists(path):
        return _empty_decisions()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_decisions()
    if not isinstance(d, dict):
        return _empty_decisions()
    for key, default in _empty_decisions().items():
        d.setdefault(key, default)
    return d


def _cited_ids(text):
    return set(re.findall(r"\b((?:dec|con|pipe)-[0-9a-z]+(?:-[0-9a-z]+)?)\b",
                          (text or "").lower()))


def _mesh_index(cfg):
    """{entry_id: (project, type, entry)} for every active entry in every named
    project. The concept tier is checked against the WHOLE corpus — a law that
    only knew about one project would not be a cross-project law."""
    targets = {cfg["project"]: cfg["context_dir"]}
    for name, path in cfg["projects"].items():
        targets[name] = os.path.join(path, ".context")
    mesh = {}
    for name, ctx in targets.items():
        if not os.path.isdir(ctx):
            continue
        for eid, rec in _read_entries(ctx).items():
            if (rec[2].get("status") or "active") == "active":
                mesh[eid] = (name, rec[0], rec[2])
    return mesh


def _lessons_block(cfg, include_bodies, mesh):
    """The concept tier: cross-project laws, each with its evidence and — the
    part that makes it self-iterating — the entries that MATCH it but are not
    yet cited in it.

    This is the LLM-wiki compile trigger expressed deterministically. A concept
    page is written by an agent (judgement: "these six entries are one idea"),
    but whether it has fallen behind the corpus is a comparison anyone can run:
    take the law's topic tags, find every entry in the mesh carrying them, and
    subtract the ids the law already cites. What is left is unincorporated
    evidence, and it is the work list for the next compile — nobody has to
    notice or ask.

    `mesh` is {entry_id: (project, type, entry)} across every named project."""
    decided = _read_decisions(cfg)
    scopes = [("local", _read_local(cfg)["items"])]
    try:
        scopes.append(("team", _read_team(cfg)))
    except Exception:
        pass
    try:
        if cfg["org_repo"]:
            scopes.append(("org", _read_org(cfg)))
    except Exception:
        pass

    rows = []
    for scope, items in scopes:
        for item in items:
            tags = {str(t).lower() for t in (item.get("tags") or [])}
            if LAW_TAG not in tags or item.get("status") != "active":
                continue
            cited = _cited_ids(item.get("content", "") + " " + item.get("why", ""))
            # A candidate you have ruled on is settled either way: accepted
            # means the law accounts for it, rejected means it never belonged.
            # Both remove it from the work list; only the reason differs.
            lid = item.get("id")
            cited |= {e.lower() for e in decided["law_citations"].get(lid, [])}
            cited |= {e.lower() for e in decided["law_dismissed"].get(lid, [])}
            topic = tags - {LAW_TAG, "cross-project"}
            # Score by how many of the law's topics an entry carries. A single
            # shared tag is weak — broad tags like `testing` or `architecture`
            # match most of the corpus — so one hit is a lead and two is a
            # candidate. Without this the work list fired on everything, which
            # by this store's own rule is not a signal at all.
            scored, projects = {}, set()
            for eid, (proj, _t, e) in mesh.items():
                etags = {str(x).lower() for x in (e.get("tags") or [])}
                overlap = topic & etags
                if overlap:
                    scored[eid] = len(overlap)
                    projects.add(proj)
            leads = sorted(set(scored) - cited,
                           key=lambda e: (-scored[e], e))
            unincorporated = [e for e in leads if scored[e] >= 2]
            weak = [e for e in leads if scored[e] < 2]
            row = {
                "id": item.get("id"),
                "scope": scope,
                "law": _demojibake(item.get("content", "")),
                "topics": sorted(topic),
                "cites": sorted(cited),
                "evidence_projects": sorted(projects),
                # The work list: entries sharing TWO OR MORE of the law's
                # topics that it does not cite. Non-empty means the corpus has
                # moved past the page and it is due a recompile.
                "unincorporated": unincorporated[:40],
                "unincorporated_count": len(unincorporated),
                # An agent's read on a candidate, keyed law:entry. Advisory in
                # exactly the way a link verdict is: it says whether the law
                # SHOULD account for the entry and what would change, and the
                # citation is still only written by a tap.
                "unincorporated_evals": {
                    e: v for e, v in (
                        (e, decided["law_evals"].get("%s:%s" % (item.get("id"), e)))
                        for e in unincorporated[:40]) if v},
                # Same visibility problem as a link: filed, waiting on a reader.
                "unincorporated_awaiting": [
                    e for e in unincorporated[:40]
                    if "%s:%s" % (item.get("id"), e) in set(decided["eval_pending"])],
                # Single-tag matches, counted but not listed. Reported so the
                # narrowing is visible rather than looking like there was
                # nothing else there.
                "weak_leads": len(weak),
                "recalls": (item.get("trust") or {}).get("recalls", 0),
            }
            if include_bodies:
                row["evidence"] = _demojibake(item.get("why", ""))
            rows.append(row)
    rows.sort(key=lambda r: (-r["unincorporated_count"], r["id"] or ""))
    return {
        "count": len(rows),
        "laws": rows,
        "needs_update": sum(1 for r in rows if r["unincorporated_count"]),
        "note": "A law is written by judgement; whether it has fallen behind "
                "the corpus is computed. `unincorporated` is the next compile's "
                "work list.",
    }


def _survey_script():
    """context-keeper's supersession survey, or None. Derived from
    CAMBIUM_CONTEXT_KEEPER (the script is `scripts/` beside `server.py`) so one
    setting wires both it and verify_quality; overridable outright."""
    explicit = os.environ.get("CAMBIUM_SUPERSESSION_SURVEY", "").strip()
    if explicit:
        p = _abspath(explicit)
        return p if os.path.isfile(p) else None
    ck = os.environ.get("CAMBIUM_CONTEXT_KEEPER", "").strip()
    if not ck:
        return None
    root = os.path.dirname(_abspath(ck))
    guess = os.path.join(root, "scripts", "survey_supersessions.py")
    return guess if os.path.isfile(guess) else None


def _link_proposals(cfg):
    """Missing supersession links, proposed automatically for every named
    project — so the backfill is something you LOOK AT rather than something you
    remember to run.

    Delegates to context-keeper's survey script rather than reimplementing its
    heuristic, for the same reason verify_quality is shelled out to: it scores
    pairs with the very function the write-time advisory uses, so a backfilled
    link matches what the advisory would have suggested at the time. A second
    implementation here would drift from that and the two would disagree about
    what "same subject" means.

    NOTHING IS EVER WRITTEN TO A STORE. The script is read-only by design, and
    that restraint is deliberate upstream: "these two entries look related" is
    not the same claim as "this one replaced that one", and only the second
    justifies an edge. An edge written from a heuristic silently demotes a rule
    that may still be in force. So this surfaces proposals where you already
    look, and you decide."""
    script = _survey_script()
    if not script:
        return {"checked": False, "proposals": None,
                "reason": "context-keeper's survey_supersessions.py not found; "
                          "set CAMBIUM_CONTEXT_KEEPER (or "
                          "CAMBIUM_SUPERSESSION_SURVEY)"}
    roots = {os.path.dirname(p) for p in cfg["projects"].values()}
    roots.add(os.path.dirname(cfg["repo"]))
    # The script writes its payload to --out and only a summary to stdout, so
    # the JSON is collected from a temp file rather than off the wire.
    fd, tmp = tempfile.mkstemp(prefix="cambium-survey-", suffix=".json")
    os.close(fd)
    argv = [sys.executable, script, "--json", "--out", tmp]
    for r in sorted(roots):
        argv += ["--root", r]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=max(GIT_TIMEOUT, 60),
                           env=_noninteractive_env())
        if p.returncode != 0:
            return {"checked": False, "proposals": None,
                    "reason": (p.stderr or "survey exited %d" % p.returncode).strip()[:300]}
        with open(tmp, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"checked": False, "proposals": None,
                "reason": f"survey output unreadable: {e}"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    # The script discovers stores by scanning a root, so it reads more than the
    # named projects. Filtering here means only named projects are ever
    # SURFACED, keeping the explicit-map promise at the output boundary.
    named = set(cfg["projects"]) | {cfg["project"]}
    out = {}
    for prop in data.get("proposals", []):
        if prop.get("project") in named:
            out.setdefault(prop["project"], []).append(prop)
    unpaired = {}
    for u in data.get("unpaired_markers", []):
        if u.get("project") in named:
            unpaired.setdefault(u["project"], []).append(u)
    decisions = _read_decisions(cfg)
    return {"checked": True, "proposals": out, "unpaired": unpaired,
            "threshold": data.get("threshold"),
            "dismissed": decisions["dismissed_links"],
            "evals": decisions["link_evals"],
            "awaiting": set(decisions["eval_pending"]),
            "repairs": {k: v for k, v in decisions["repair_proposals"].items()
                        if k not in set(decisions["repair_dismissed"])}}


def _synthesis_gaps(name, entries, lessons):
    """What this project knows that no law has taken up yet.

    _lessons_block asks the law-centric question -- "what does THIS law fail to
    cite?" -- and that is the right question for keeping a law current. It is
    the wrong question for a project, because a project whose knowledge never
    became a law appears in no law's work list at all: having no law to fall
    behind reads exactly like having nothing to say.

    So ask it from the other end. Split this project's active entries three
    ways: CITED by some law, a CANDIDATE the law tier already surfaced, and
    everything else -- entries no law cites and no law is even reaching for.
    That last bucket is the project's unsynthesized knowledge, and it is the
    only one of the three that nothing else in the mesh reports.

    Arithmetic, all of it. Writing the law is judgement and stays with a person.
    """
    cited, candidate = {}, {}
    laws_here = set()
    for law in lessons.get("laws") or []:
        lid = law.get("id")
        for eid in law.get("cites") or []:
            e = str(eid).lower()
            cited.setdefault(e, []).append(lid)
            laws_here.add(lid)
        for eid in law.get("unincorporated") or []:
            candidate.setdefault(str(eid).lower(), []).append(lid)

    active = [e for e, r in entries.items()
              if (r[2].get("status") or "active") == "active"]
    feeding = sorted(e for e in active if e.lower() in cited)
    pending = sorted(e for e in active
                     if e.lower() not in cited and e.lower() in candidate)
    orphan = sorted(e for e in active
                    if e.lower() not in cited and e.lower() not in candidate)
    laws_from_here = sorted({l for e in feeding for l in cited[e.lower()]})
    return {
        "active": len(active),
        "feeding_laws": feeding,
        "awaiting_incorporation": pending,
        "unsynthesized": orphan,
        "laws_drawing_on_this": laws_from_here,
        "law_count": len(laws_from_here),
        # The headline: of everything this project has settled, how much has
        # reached the tier that other projects can read?
        "generalized_pct": (round(100.0 * len(feeding) / len(active))
                            if active else 0),
    }


def _project_snapshot(cfg, name, context_dir, pages, include_bodies,
                      links=None, lessons=None):
    entries = _read_entries(context_dir)
    by_kind, by_status, by_kind_status, nodes, edges = {}, {}, {}, [], []
    for eid, rec in sorted(entries.items()):
        tname, _title_f, e = rec
        status = e.get("status") or "active"
        by_kind[tname] = by_kind.get(tname, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_kind_status.setdefault(tname, {})
        by_kind_status[tname][status] = by_kind_status[tname].get(status, 0) + 1
        node = {"id": eid, "kind": tname, "status": status,
                "updated_at": e.get("updated_at") or e.get("created_at") or ""}
        if include_bodies:
            node["title"] = _entry_title(rec)
            node["tags"] = sorted({str(t).lower() for t in (e.get("tags") or [])})
            # Enough of the entry to JUDGE it. A reviewer approving a candidate
            # by id alone is approving something they cannot read, and the whole
            # point of the review gate is that a human looked.
            for field in ("problem", "why_chosen", "reason", "purpose"):
                val = (e.get(field) or "").strip()
                if val:
                    node["excerpt"] = _demojibake(val)[:600]
                    break
        nodes.append(node)
        target = e.get("superseded_by")
        if target:
            # A dangling edge is real and must be drawn as such: record_entry
            # skips unknown supersedes ids silently, and prune_stale can remove
            # a target, so "superseded by something that isn't there" is a state
            # the store reaches on its own.
            edges.append({"from": eid, "to": target, "kind": tname,
                          "dangling": target not in entries})

    page_rows = []
    for page in pages:
        stale, causes = _page_staleness(page, entries)
        row = _page_view(page, stale, causes)
        if not include_bodies:
            row.pop("title", None)
        page_rows.append(row)

    return {
        "name": name,
        "counts": {"total": len(entries), "by_kind": by_kind,
                   "by_status": by_status, "by_kind_status": by_kind_status},
        "entries": nodes,
        "supersession_edges": edges,
        "pages": page_rows,
        "stale_page_count": sum(1 for p in page_rows if p["stale"]),
        "quality": _project_quality(cfg, name, context_dir, include_bodies, links),
        "synthesis": _synthesis_gaps(name, entries, lessons or {}),
        "links": _project_links(links, name, include_bodies),
    }


# What can actually be DONE about each issue, which is the only question a
# reviewer is asking. A single total treats "one tap fixes this" and "this
# clears itself in a week" and "somebody must read code" as the same
# outstanding item, so the number never moves and the surface reads as broken.
GAP_CLASSES = {
    "auto":    "one tap; the drain applies it with no judgement",
    "review":  "needs authored text, then your approval",
    "waiting": "already remediated; clears itself once retrieval catches up",
    "blocked": "needs the code re-read before anything may be written",
}


def _classify_gap(issue_type, entry):
    """Which bucket one issue on one entry falls in.

    Mirrors what apply_queue's drain will actually do, deliberately: a card that
    promises a one-tap fix the drain then declines is worse than no card."""
    if issue_type == "code_drift":
        return "blocked"
    drifted = False  # caller passes entry already known non-drifted for isolated
    if issue_type == "isolated":
        return "blocked" if drifted else "auto"
    if issue_type == "legacy":
        return ("auto" if (entry.get("rationale") or "").strip()
                and not (entry.get("why_chosen") or "").strip() else "review")
    if issue_type == "unused":
        # Hints are written; the counter only moves when the entry is actually
        # returned by a query, which no edit can force.
        return "waiting" if (entry.get("retrieval_hints") or []) else "auto"
    if issue_type == "no_tags":
        return "auto"
    return "review"


def _project_quality(cfg, name, context_dir, include_bodies, links):
    """verify_quality's gaps, plus whether a repair has already been asked for.

    The awaiting set is read off `links` rather than re-reading decisions.json
    per project: it is the same eval_pending list for every kind, and reading it
    twenty times to answer one boolean would make the snapshot slower for no
    additional truth."""
    q = _quality_gaps(os.path.dirname(context_dir), include_bodies)
    # Any quality request for this project, whatever bucket filed it. The
    # bucket buttons file quality-review:<project> and quality-blocked:<project>
    # while the per-project button files quality:<project>, so matching only the
    # bare form left the mesh-wide sends invisible -- tap, refresh, nothing.
    awaiting = (links or {}).get("awaiting") or set()
    pending_cls = sorted({
        (k.split(":", 1)[0].split("-", 1) + [""])[1] or "any"
        for k in awaiting
        if k.split(":", 1)[0].split("-", 1)[0] == "quality"
        and k.split(":", 1)[-1] == name})
    if pending_cls:
        q["awaiting_repair"] = True
        q["awaiting_classes"] = pending_cls

    # Classify every issue so the card can say what a tap will DO, rather than
    # showing one total that never moves.
    if q.get("checked"):
        entries = _read_entries(context_dir)
        counts = dict.fromkeys(GAP_CLASSES, 0)
        for g in (q.get("gaps") or []):
            types = [i.get("type") for i in g.get("issues", [])]
            rec = entries.get(g.get("id"))
            entry = rec[2] if rec else {}
            has_drift = "code_drift" in types
            for t in types:
                # isolated on a drifted entry is unreachable until the drift is
                # resolved, because writing to it would clear the drift flag.
                cls = "blocked" if (t == "isolated" and has_drift) \
                    else _classify_gap(t, entry)
                counts[cls] += 1
        q["gap_classes"] = counts
        q["fixable_now"] = counts["auto"]

    # Proposed edits awaiting a ruling. A repair that changes an entry's TEXT is
    # judgement, so it belongs on the same footing as a supersession: proposed
    # with its reasoning, applied only by a tap. Repairs used to be written
    # directly, which made this the one surface with no approval step at all.
    props = (links or {}).get("repairs") or {}
    mine = [dict(v, key=k) for k, v in sorted(props.items())
            if v.get("project") == name]
    if mine:
        q["repair_proposals"] = mine if include_bodies else [
            {k2: v2 for k2, v2 in m.items() if k2 not in ("current", "proposed")}
            for m in mine]
        q["repair_proposal_count"] = len(mine)
    return q


def _project_links(links, name, include_bodies):
    """One project's slice of the survey. Same honesty rule as quality: a survey
    that could not run reports `checked: false`, never an empty proposal list,
    which a dashboard would draw as "nothing to link"."""
    if not links or not links.get("checked"):
        return {"checked": False, "proposals": None,
                "reason": (links or {}).get("reason", "not run")}
    dismissed = set(links.get("dismissed") or [])
    evals = links.get("evals") or {}
    rows = []
    for prop in links.get("proposals", {}).get(name, []):
        key = "%s:%s:%s" % (name, prop.get("newer_id"), prop.get("older_id"))
        if key in dismissed:
            continue          # you already said no; stop asking
        ev = prop.get("evidence", {})
        row = {
            "older_id": prop.get("older_id"),
            "newer_id": prop.get("newer_id"),
            "kind": prop.get("kind"),
            # `tier` is replacement evidence; `both_signals` was topic overlap
            # being reported as evidence and measured 21.9% precision against
            # 80% for `likely`. Kept only for snapshots written before the fix.
            "tier": prop.get("tier", "lead"),
            "both_signals": bool(prop.get("both_signals")),
            "overlap_score": ev.get("overlap_score"),
            "shared_tags": ev.get("shared_tags") or [],
            "replacement_signals": ev.get("replacement_signals") or [],
        }
        if include_bodies:
            row["older_summary"] = _demojibake(prop.get("older_summary") or "")
            row["newer_summary"] = _demojibake(prop.get("newer_summary") or "")
        if key in (links.get("awaiting") or set()):
            # Filed and waiting on a reader. Without this the card reverts to
            # offering "Send for eval" again the moment the queue drains, and a
            # successful tap is indistinguishable from one that did nothing.
            row["awaiting_eval"] = True
        verdict = evals.get(key)
        if verdict:
            # Carried WITH the proposal rather than as a separate list, so the
            # reviewer reads the audit and the evidence in one place and cannot
            # rule on one while looking at the other.
            row["eval"] = {
                "verdict": verdict.get("verdict"),
                "confidence": verdict.get("confidence"),
                "at": verdict.get("at"),
                "model": verdict.get("model"),
            }
            if include_bodies:
                row["eval"]["reasoning"] = _demojibake(
                    verdict.get("reasoning") or "")[:1200]
        rows.append(row)
    # Audited first, then evidence: a pair someone has already done the reading
    # on is the one worth a tap, and a `likely` with no audit still beats a
    # `lead` with one.
    rows.sort(key=lambda r: (not r.get("eval"), r["tier"] != "likely",
                             -(r["overlap_score"] or 0), r["older_id"] or ""))
    return {
        "checked": True,
        "proposals": rows,
        "count": len(rows),
        "likely": sum(1 for r in rows if r["tier"] == "likely"),
        "audited": sum(1 for r in rows if r.get("eval")),
        "both_signals": sum(1 for r in rows if r["both_signals"]),
        "unpaired_markers": len(links.get("unpaired", {}).get(name, [])),
        "note": "Proposals only. Nothing was written to any store — an edge "
                "written from a heuristic silently demotes a rule that may "
                "still be in force.",
    }


def _build_snapshot(cfg, include_bodies=False):
    """The whole mesh as one JSON-able dict. Reads only the projects named in
    CAMBIUM_PROJECTS plus the configured repo — never a filesystem scan, so a
    store can only be in here because someone named it."""
    targets = {cfg["project"]: cfg["context_dir"]}
    for name, path in cfg["projects"].items():
        targets[name] = os.path.join(path, ".context")
    pages_by_project = {}
    for page in _read_pages(cfg)["pages"]:
        pages_by_project.setdefault(page.get("project") or cfg["project"],
                                    []).append(page)

    # One survey for the whole mesh, not one per project: the script scans by
    # root, so running it per project would rescan everything N times.
    links = _link_proposals(cfg)

    mesh = _mesh_index(cfg)

    # Laws are computed BEFORE the projects that get measured against them: a
    # project's synthesis gap is defined by what the law tier already covers, so
    # the law tier has to exist first.
    lessons = _lessons_block(cfg, include_bodies, mesh)

    projects, skipped = [], []
    for name in sorted(targets):
        ctx = targets[name]
        if not os.path.isdir(ctx):
            skipped.append({"name": name, "reason": "no .context/ store at %s" % ctx})
            continue
        projects.append(_project_snapshot(cfg, name, ctx,
                                          pages_by_project.get(name, []),
                                          include_bodies, links, lessons))
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generator": "cambium",
        "includes_bodies": include_bodies,
        "lessons": lessons,
        "projects": projects,
        # Named and reported, so a project silently missing from the dashboard
        # is visible as a skip rather than as an absence.
        "skipped_projects": skipped,
        "totals": {
            "projects": len(projects),
            "entries": sum(p["counts"]["total"] for p in projects),
            "pages": sum(len(p["pages"]) for p in projects),
            "stale_pages": sum(p["stale_page_count"] for p in projects),
            "quality_gaps": sum(p["quality"].get("count") or 0 for p in projects),
            "projects_without_quality_check": sum(
                1 for p in projects if not p["quality"]["checked"]),
            "laws": lessons["count"],
            "laws_behind": lessons["needs_update"],
            "link_proposals": sum(p["links"].get("count") or 0 for p in projects),
            "link_proposals_likely": sum(
                p["links"].get("likely") or 0 for p in projects),
            "projects_without_link_survey": sum(
                1 for p in projects if not p["links"]["checked"]),
            # The synthesis headline: projects that have settled real knowledge
            # and generalized none of it. Counted only above a floor, because a
            # project with three entries has not earned a law yet and flagging
            # it would drown the ones that have.
            "projects_generalizing_nothing": sum(
                1 for p in projects
                if p["synthesis"]["law_count"] == 0
                and p["synthesis"]["active"] >= 10),
            "unsynthesized_entries": sum(
                len(p["synthesis"]["unsynthesized"]) for p in projects),
        },
    }


@mcp.tool()
def refresh(out: str = "", vault: str = "", include_bodies: bool = False) -> str:
    """Bring every derived surface up to date in one call: recompile each named
    project's pages, rewrite the markdown vault, and re-export the snapshot.

    This exists to be wired to a hook. The whole point of the page and concept
    tiers is that they stay current without anyone remembering to run anything —
    a synthesis you have to ask for is one you will find stale at exactly the
    moment you needed it. distill() already runs from SessionEnd for the same
    reason; this belongs on the same path.

    Everything it does is idempotent, so firing it unconditionally is safe: an
    unchanged store recompiles to identical pages and exports a byte-identical
    snapshot."""
    cfg, err = _require_cfg()
    if err:
        return err
    projects = sorted(set(list(cfg["projects"]) + [cfg["project"]]))
    compiled, failed = [], []
    for name in projects:
        try:
            _, ctx = _pages_context_dir(cfg, name)
        except ConfigError as e:
            failed.append({"project": name, "reason": str(e)})
            continue
        if not os.path.isdir(ctx):
            failed.append({"project": name, "reason": "no .context/ store"})
            continue
        r = json.loads(compile_project(project=name))
        if r.get("error"):
            failed.append({"project": name, "reason": r["error"]})
        else:
            compiled.append({"project": name, "pages": len(r["pages"]) + 1,
                             "unfiled": r["unfiled_entries"]})
    vault_out = None
    if vault:
        v = json.loads(export_pages(out_dir=vault))
        vault_out = None if v.get("error") else {
            "dir": v["out_dir"], "written": v["written"],
            "reaped": len(v["removed_orphans"])}
    snap = json.loads(export_snapshot(out=out, include_bodies=include_bodies))
    # Read the law counts from the snapshot that was just written. An earlier
    # version recomputed them against an EMPTY mesh to save a pass and reported
    # laws_behind: 0 — a check that could only ever return clean, which is the
    # exact failure this codebase keeps writing rules about.
    totals = snap.get("totals") or {}
    return json.dumps({
        "status": "refreshed",
        "projects_compiled": len(compiled),
        "compiled": compiled,
        "failed": failed,
        "vault": vault_out,
        "snapshot": {"path": snap.get("path"), "totals": totals},
        # Surfaced here so a hook's output alone tells you whether the concept
        # tier has fallen behind, without opening the dashboard.
        "laws": totals.get("laws"),
        "laws_behind": totals.get("laws_behind"),
    }, indent=2)


@mcp.tool()
def export_snapshot(out: str = "", include_bodies: bool = False) -> str:
    """Write the whole mesh to one JSON file for a static dashboard to read.

    Contains, per project: entry counts by kind and status, every entry as a
    graph node, supersession edges (including dangling ones), compiled pages
    with their computed staleness and cause, and context-keeper's verify_quality
    gaps. Entry prose is EXCLUDED unless include_bodies is set.

    The payload carries no generated-at timestamp, so re-exporting an unchanged
    mesh produces a byte-identical file and committing it never churns git."""
    cfg, err = _require_cfg()
    if err:
        return err
    snap = _build_snapshot(cfg, include_bodies)
    path = _abspath(out) if out else os.path.join(
        cfg["repo"], LOCAL_DIR, "snapshot.json")
    _atomic_write_json(path, snap)
    return json.dumps({
        "status": "exported",
        "path": path,
        "includes_bodies": include_bodies,
        "totals": snap["totals"],
        "skipped_projects": snap["skipped_projects"],
    }, indent=2)


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


# Words that appear in every repo and so separate nothing.
_STOP = frozenset("""
this that with from have been will your they them then than into more most some
such only other same each about after before which while where when what
python java script test tests main src lib code file files json yaml init
update updates fix fixes add adds remove removes refactor docs doc readme
""".split())


def _work_terms(repo, limit=60):
    """What this session is actually touching: paths in the working tree and the
    subjects of recent commits.

    This is the missing input. Ranking a knowledge digest by recall count alone
    is self-sealing: an item that has never been recalled sorts last, gets cut by
    the budget, is therefore never seen, and so is never recalled. The best-
    written law in a store can be structurally invisible from the day it is
    written. Relevance needs something to be relevant TO."""
    terms = set()
    try:
        for args in (["status", "--porcelain"],
                     ["log", "-8", "--format=%s"],
                     ["diff", "--name-only", "HEAD~3..HEAD"]):
            r = subprocess.run(["git", "-C", repo] + args, capture_output=True,
                               text=True, timeout=GIT_TIMEOUT,
                               env=_noninteractive_env())
            if r.returncode != 0:
                continue
            for tok in re.split(r"[^A-Za-z0-9_]+", r.stdout.lower()):
                if len(tok) >= 4 and tok not in _STOP and not tok.isdigit():
                    terms.add(tok)
            if len(terms) >= limit:
                break
    except Exception:
        pass
    return terms


def _relevance(item, terms):
    """How many distinct work terms this item mentions. Tags count double --
    a tag is a deliberate index, a word in prose may be incidental."""
    if not terms:
        return 0
    text = (_oneline(item.get("content", "")) + " "
            + _oneline(item.get("why", ""))).lower()
    tags = " ".join(str(t) for t in (item.get("tags") or [])).lower()
    score = sum(1 for t in terms if t in text)
    score += 2 * sum(1 for t in terms if t in tags)
    return score


@mcp.tool()
def session_primer(limit: int = 8) -> str:
    """A compact digest of what's ALREADY known for this project — built to be
    injected automatically at session start so recall is passive, not a step an
    agent has to remember. Returns the highest-value active knowledge (most-
    recalled, then most-recently-updated) across local+team+org, plus any promoted
    assumptions (valid_while premises) that look stale and worth re-checking.

    READ-ONLY: unlike recall(), it does NOT increment recall counters — a passive
    session-start surfacing must never inflate trust or nudge promotion. It is the
    session-start counterpart to distill() at session end: together they close the
    capture/recall loop without either one depending on the agent remembering to
    call it. Use recall(query) for a real, query-scoped search."""
    cfg, err = _require_cfg()
    if err:
        return err
    limit = max(1, min(int(limit), 25))

    local_items = _read_local(cfg)["items"]
    pool = [("local", i) for i in local_items]
    try:
        pool += [("team", i) for i in _read_team(cfg)]
        if cfg["org_repo"]:
            pool += [("org", i) for i in _read_org(cfg)]
    except Exception:
        pass  # a primer is best-effort; a missing remote scope never fails it
    active = [(s, i) for s, i in pool if i.get("status") == "active"]

    # Two rankings, and the budget is SPLIT between them.
    #
    # Recalls alone is self-sealing: never-recalled items sort last, get cut, are
    # never seen, and so are never recalled. Measured on this machine, 16 of 19
    # org-scope laws sat at recalls=0 -- structurally invisible -- while the same
    # session committed five mistakes those exact laws describe.
    #
    # So half the seats go to what has PROVEN useful (recalls), and half to what
    # is relevant to the work in front of you right now (terms from the working
    # tree and recent commits). Proven knowledge keeps its place; a law written
    # yesterday for exactly today's problem can still get in.
    by_recall = sorted(
        active,
        key=lambda si: (si[1].get("trust", {}).get("recalls", 0),
                        si[1].get("updated_at", "")),
        reverse=True)

    terms = _work_terms(cfg["repo"])
    scored = [(si, _relevance(si[1], terms)) for si in active]
    by_relevance = [si for si, sc in
                    sorted(scored, key=lambda x: (x[1],
                           x[0][1].get("trust", {}).get("recalls", 0)),
                           reverse=True) if sc > 0]

    reserved = max(1, limit // 2)
    top, seen = [], set()
    for si in by_relevance[:reserved]:
        top.append(si)
        seen.add(id(si[1]))
    for si in by_recall:
        if len(top) >= limit:
            break
        if id(si[1]) not in seen:
            top.append(si)
            seen.add(id(si[1]))

    # promoted assumptions whose premise may have died, oldest-verified first
    premises = [(s, i) for s, i in active if (i.get("valid_while") or "").strip()]
    premises.sort(key=lambda si: _verified_key(si[1]))

    digest = {
        "project": cfg["project"],
        "known_items": len(active),
        "known": [{"scope": s, "kind": i.get("kind", "note"),
                   "content": _oneline(i.get("content", ""))[:160],
                   "recalls": i.get("trust", {}).get("recalls", 0)}
                  for s, i in top],
        "check_assumptions": [
            {"content": _oneline(i.get("content", ""))[:100],
             "valid_while": _oneline(i.get("valid_while", "")),
             "last_verified": i.get("last_verified")}
            for s, i in premises[:3]],
        "how_to_use": ("recall(<query>) to search deeper; this primer is "
                       "read-only and did not count as a recall."),
    }
    if not active:
        digest["note"] = ("No cambium knowledge for this project yet — it will "
                          "accumulate as distill() runs at session end.")
    return json.dumps(digest, indent=2)


def _run_cli(argv):
    """CLI parity for the commands that make sense outside an MCP session.

    Mirrors context-keeper's CLI shape deliberately (`<command> [flags]`,
    dispatching to the SAME function the MCP tool calls — no duplicated logic)
    so one habit works across the suite. Exit codes: 2 usage error, 1 if the
    command reported an error, 0 otherwise."""
    commands = {
        "export-snapshot": (
            "write the mesh snapshot a dashboard reads "
            "[--out PATH] [--bodies]"),
        "export-pages": (
            "write compiled pages as markdown (an Obsidian vault) "
            "[--out DIR] [--project NAME]"),
        "refresh": (
            "recompile pages + vault + snapshot in one call; wire this to a "
            "hook [--out PATH] [--vault DIR]"),
    }
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stderr.write(
            "Usage: cambium-mcp <command> [flags]   (no args = stdio MCP server)\n"
            + "".join(f"  {n:<18}{d}\n" for n, d in sorted(commands.items())))
        return 0 if argv else 2

    name = argv[0]
    if name not in commands:
        sys.stderr.write(f"Unknown command: {name}\n"
                         "Run 'cambium-mcp --help' for the list.\n")
        return 2

    rest, out, bodies, project, vault = argv[1:], "", False, "", ""
    i = 0
    while i < len(rest):
        if rest[i] == "--bodies":
            bodies = True
        elif rest[i] == "--out" and i + 1 < len(rest):
            out = rest[i + 1]
            i += 1
        elif rest[i] == "--project" and i + 1 < len(rest):
            project = rest[i + 1]
            i += 1
        elif rest[i] == "--vault" and i + 1 < len(rest):
            vault = rest[i + 1]
            i += 1
        else:
            sys.stderr.write(f"Unknown or incomplete flag: {rest[i]}\n")
            return 2
        i += 1

    if name == "export-pages":
        result = export_pages(out_dir=out, project=project)
    elif name == "refresh":
        # --bodies was parsed and then thrown away here, so the one path
        # documented "to be wired to a hook" wrote full entry prose
        # unconditionally while export-snapshot required the flag. The same CLI
        # had opposite defaults for the same privacy switch, and the automated
        # path had the unsafe one.
        result = refresh(out=out, vault=vault, include_bodies=bodies)
    else:
        result = export_snapshot(out=out, include_bodies=bodies)
    sys.stdout.write(result + "\n")
    # A hook's ONLY channel is the exit code. Keying it on an "error" key alone
    # meant an unconfigured machine -- where _require_cfg returns guidance with
    # configured:false and no "error" key -- ran refresh, did nothing at all,
    # and exited 0. The hook this command exists for could not tell a full
    # refresh from a total no-op.
    try:
        payload = json.loads(result)
    except (ValueError, AttributeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("error") or payload.get("configured") is False:
        return 1
    if payload.get("failed"):
        return 1
    return 0


def main():
    """Console entry point (pip install cambium-mcp -> `cambium-mcp`).

    Args present = CLI; no args = the stdio MCP server, unchanged. Same
    convention as context-keeper's entry point, so adding a command can never
    change what an MCP client launching the bare executable gets."""
    argv = sys.argv[1:]
    if argv:
        sys.exit(_run_cli(argv))
    mcp.run()


if __name__ == "__main__":
    main()
