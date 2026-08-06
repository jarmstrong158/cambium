# cambium

<!-- mcp-name: io.github.jarmstrong158/cambium -->

_Part of the [xylem](https://github.com/jarmstrong158/xylem) stack._

The knowledge-lifecycle MCP that turns work your agents already did into
**compound, org-wide knowledge**. cambium bridges two substrates that already
exist — [agentsync](https://github.com/jarmstrong158/agentsync) (what happened:
claims, finishes, notes, changed files) and
[context-keeper](https://github.com/jarmstrong158/context-keeper) (why: decisions,
constraints) — and adds the three things neither has:

1. **`distill()`** — turn events into memory *automatically* (passive capture)
2. **`recall()`** — one federated read endpoint for **every agent type**
   (coding agent, Slack KB bot, SRE bot — same store, same call)
3. **`promote()`** — graduate knowledge **local → team → org** as it earns trust

Named for the cambium layer of a tree: the thin living tissue where all growth
happens.

## Why a third MCP (and not a database)

Knowledge layers fail because they're a *side system* nobody calls. cambium's
bet: capture and recall must be **native tools in the agent's loop**, and state
must live in the **substrate the work already lives in** — git — not a separate
service. Storage is an implementation detail behind `recall()`:

| Scope | Lives in | Trust gate |
|---|---|---|
| `local` | `<repo>/.cambium/knowledge.json` | none — it's yours |
| `team`  | `knowledge.json` on a dedicated `cambium` branch of the shared repo | recalls ≥ N **or** an endorsement |
| `org`   | `knowledge.json` in a dedicated org knowledge repo | an **endorsement required**; optionally lands as a **pull request** — review is the gate, `git revert` is the undo |

Team writes use the agentsync pattern: a private worktree under `.git/` and
`git push` as compare-and-swap, so concurrent agents never clobber each other.

## Install

```bash
pip install -r requirements.txt      # just `mcp`
```

`gh` (GitHub CLI) is only needed for pull-request-mode org promotion.

## Configure

Point an MCP client at the server — no env required to start:

```json
{
  "mcpServers": {
    "cambium": {
      "command": "python3",
      "args": ["/abs/path/to/cambium_server.py"]
    }
  }
}
```

**First contact is helpful, not cold.** MCP servers can't start a conversation,
so cambium teaches you through its own responses. Call **`status()`** (or any
tool) before it's configured and instead of a bare env error you get structured
guidance — what's set, what's missing, what each gap costs in plain terms, and
the exact `setup()` call that fixes it:

```jsonc
{
  "configured": false,
  "gaps": [
    {"setting": "CAMBIUM_REPO",
     "cost": "no project repo → cambium has no substrate; every tool is unavailable",
     "fix": "setup(project_repo=\"/abs/path/to/your/clone\", agent_id=\"your-id\")"},
    {"setting": "CAMBIUM_ORG_REPO",
     "cost": "org scope off → promotions stop at team; org-wide recall unavailable",
     "fix": "setup(project_repo=…, agent_id=…, org_repo=\"owner/knowledge or /abs/path/to/clone\")"}
  ],
  "next_step": "setup(project_repo=\"/abs/path/to/your/clone\", agent_id=\"your-id\")"
}
```

**`setup(project_repo, agent_id, org_repo?, org_pr?, team_branch?)`** finishes
the job: it validates the paths, scaffolds `.cambium/` (and adds it to the
repo's `.gitignore`), and writes a fallback config at `~/.cambium/config.json`
that the server reads when env vars are absent. It takes effect immediately — no
restart. **No secrets are written**: the file holds only paths, ids, and flags,
and lives outside any repo. If `org_repo` is a GitHub `owner/name` you haven't
cloned locally, setup **offers the exact `gh`/`git` commands** to stand it up
and leaves org scope off — it never creates or pushes a repo for you.

**Env still wins.** Any of the variables below, set in the MCP client config,
overrides the config file per-key — the table is the full reference layer:

| env var | required | default | meaning |
|---|---|---|---|
| `CAMBIUM_REPO` | yes | — | your project clone (local scope, agentsync + context-keeper substrates) |
| `CAMBIUM_AGENT_ID` | yes | — | your unique agent id |
| `CAMBIUM_REMOTE` | no | `origin` | git remote |
| `CAMBIUM_GIT_TIMEOUT` | no | `25` | per-invocation git/gh timeout (seconds) so a stuck call fails fast instead of hanging the server |
| `CAMBIUM_TEAM_BRANCH` | no | `cambium` | team-scope branch |
| `CAMBIUM_AGENTSYNC_BRANCH` | no | `agentsync` | coordination branch name distill reads |
| `AGENTSYNC_BOARD_REPO` | no\* | — | **which repo holds the board** — the same setting agentsync reads, so the two servers cannot disagree (see below) |
| `CAMBIUM_ORG_REPO` | no | — | path to the org knowledge repo clone (org scope off without it) |
| `CAMBIUM_ORG_PR` | no | direct push | `1` = org promotion opens a pull request |
| `CAMBIUM_PROMOTE_RECALLS` | no | `3` | recalls needed for local→team |
| `CAMBIUM_RELEASE_CAPTURE` | no | off | `1` = also capture agentsync claims at their done/released transition (see below) |
| `CAMBIUM_CONFIG_FILE` | no | `~/.cambium/config.json` | override the fallback config path (mainly for tests) |

### Where the agentsync board lives (board addressing)

\* `distill()` reads finished agentsync claims off a **coordination board**. That
board is a shared, long-lived team artifact — not a property of whichever
project this session happens to be in — so cambium resolves its address
independently of the session, using **exactly the order agentsync itself uses**:

1. **`AGENTSYNC_BOARD_REPO`** — the explicit board address (env, or the same key
   in `~/.cambium/config.json`). One setting configures both servers.
2. **`AGENTSYNC_REPO`** — agentsync's legacy explicit pin.
3. **`CAMBIUM_REPO`** — but *only if that repo actually holds the coordination
   branch* (real ref lookup: local head → remote-tracking ref → `ls-remote`).
4. Otherwise: **no board**, reported loudly (next section) — never silently.

**Why.** Before this, cambium looked for the coordination branch in
`CAMBIUM_REPO` while agentsync (unpinned) followed
`~/.xylem/active_project.json`. The two could point at different repos, and
whenever the current project had never been provisioned neither found anything.
`distill()` reported that as the bland string `"no coordination branch found"`,
callers treated it as normal, and the result was that distill imported **zero**
agentsync claims across its entire lifetime — a three-legged design silently
running on two legs.

### A skipped source does not look like a completed one

`distill()`'s return now makes a miss impossible to read as a success:

- top-level `status` becomes `"distilled_with_warnings"` (not `"distilled"`);
- top-level `warnings` carries a plain-language line per skipped substrate;
- `sources.agentsync` is an object — `{status, board_repo, board_source, branch,
  claims_seen, done_claims, imported, reason, fix}` — so "there is no board",
  "the board is here and nobody has finished anything", and "imported 3" are
  three visibly different results rather than one empty number.

`status()` reports the same under `substrates.agentsync_board`.

**Org setup**: create one (private) repo, e.g. `github.com/you/knowledge`, with
an empty `{"items": []}` in `knowledge.json`; everyone who should read org
knowledge clones it and points `CAMBIUM_ORG_REPO` (or `setup(org_repo=…)`) at
their clone. cambium manages that clone (it hard-syncs it) — dedicate it, don't
work in it.

## Tools

**`capture(content, type, kind, why, tags, valid_while)`** — save a knowledge
item to local scope (types: `memory` | `need` | `skill`). Manual path.
`valid_while` optionally names the premise the item depends on, so a dead
assumption is spottable later (see *Machine-maintained documentation entropy*).

**`record_need(content, why, tags)`** — first-class needs ("we're missing X"),
promotable like anything else so recurring wants surface at team/org level.

**`distill()`** — the automatic path. Reads agentsync's coordination branch
(every *currently done* claim: task + note + changed files → an `outcome`
memory) and context-keeper's `.context/` (active decisions & constraints,
rationale and `dec-NNN` provenance preserved). Idempotent — wire it to a
session-end or post-commit hook and capture becomes passive.

**Release-time capture (opt-in, `CAMBIUM_RELEASE_CAPTURE=1`).** agentsync keys
claims by agent id and deletes a claim from live state the instant it is
released or re-claimed — it exposes no hook or event, only the rewritten
`claims.json` on the branch. So a claim that completes and then churns before a
full distill runs against it is silently lost. With the flag on, each distill
also remembers the last-seen claim per agent and captures any that has *churned
away* since the previous run — reconstructing it from that snapshot, through the
**same** dedupe watermark, so a claim captured at release time and again in a
later full distill never double-imports. Fire `distill()` on completion events
(a post-commit / session-end hook) and completed work is captured at its
transition instead of only when a distill happens to catch it live.

What this is **not**: it is passive capture *at the moments distill runs*, not
exhaustive reconstruction. The guarantee is precise — *if a distill sweep
observes a claim while it is done (or carries a note), that knowledge is
captured even if the claim later churns.* The residual gap: a done state that is
created **and** churned away entirely between two sweeps (e.g. cambium wasn't
running) is never observed, and only agentsync's git log still holds it.
Walking that log to reconstruct such claims exhaustively is a possible
follow-up (the history survives — agentsync's `history()` reads it), deliberately
left out of this change.

**`import_memory(source, path)`** — ingest an external memory export into
cambium as local-scope, provenance-tagged knowledge items (see **Import**
below). Read-only against the source; imported items are not auto-promoted.

**`recall(query, scope, limit)`** — federated search across local+team+org.
Every hit increments the item's recall counter (the trust signal promotion
feeds on) and records cross-project use. Abstains honestly: below the relevance
floor it returns `no_confident_match: true` instead of confident-looking noise.
Each result carries `endorsed_as` — the item's endorsement notes surfaced as
first-class context, since for a promoted item that is where its cross-project
meaning was written.

**`endorse(item_id, note)`** — vouch for an item. Fast-tracks local→team;
**required** for team→org.

**`promote(item_id, to_scope, force, org_content)`** — no args: scan-and-promote
all eligible local items to team. With `to_scope="org"`: push to the org repo,
or open a PR when `CAMBIUM_ORG_PR=1` (the team copy stays, annotated, until the
PR merges). Promotion stamps `last_verified` — promotion *is* a verification.
**Generalization gate:** org scope is read by every project, so a body that
reads project-specific (names a file, a `test_*` id, a `dec-/con-NNN` ref, or
its own origin project) is refused at the org boundary — restate it as the
cross-project rule via `org_content=` (the concrete body is kept as `example`)
or `force=True` to override. The refusal hands back the endorsement note as a
ready draft. Mirrors the endorsement gate; the safe path is the easy path.

**`generalize(item_id, org_content, note)`** — the remediation counterpart of
the gate: restate an already-promoted item's body as the cross-project rule
*in place*, keeping the concrete version as `example`. For items that reached
org before the gate (or were forced past it) — the ones `review_promotions()`
lists under `org_needs_generalization`. Omit `org_content` to fall back to the
item's latest endorsement note. Writes through the org CAS path (direct, or a
single shared `cambium/generalize` PR branch when `CAMBIUM_ORG_PR=1`, so
repeated calls batch into one reviewable PR); idempotent.

**`verify_entry(item_id, note)`** — confirm an entry still holds; stamps its
`last_verified` to now (optional note). The event that keeps promoted knowledge
from silently going stale (see *Machine-maintained documentation entropy*).

**`stale_report(project, older_than_days)`** — promoted (team + org) entries
sorted oldest-verified-first, never-reverified ones flagged, each entry's
`valid_while` premise surfaced. Reports staleness; never auto-downgrades.

**`review_promotions()`** — what's eligible for team, what's endorsed for org,
which org PRs are pending, and `org_needs_generalization` — org items whose body
still reads project-specific (crossed before the gate, or forced), each with the
tells found and the endorsement note as a suggested restatement.

**`export_markdown(scope)`** — render knowledge to a human-readable
`KNOWLEDGE.md`, grouped by scope then project (each item: summary, kind,
provenance `dec-NNN`/claim origin, recall count, promoted date; cp1252 mojibake
normalized). `scope="org"` (default) re-renders and pushes the org repo's
`KNOWLEDGE.md` beside its `knowledge.json`; `local`/`team`/`all` return the
markdown without publishing. It also runs **automatically after any org
promotion** — direct-push commits both files together, PR mode puts both on the
same PR branch — so the org repo's docs are always current.

**`setup(project_repo, agent_id, org_repo?, org_pr?, team_branch?)`** — finish
configuration from a cold start (see *Configure*). Validates paths, scaffolds
`.cambium/`, writes the fallback config; offers `gh` commands for an org repo
rather than creating one. No secrets written.

**`status()`** — config state first: what's set, what's missing, each gap's cost
and the `setup()` that fixes it (never raises when unconfigured). Once
configured, also counts per scope/type, import watermarks, and wired substrates.

## The compound-growth loop

```
stobie's agent finishes work ──agentsync──▶ done claim + note + files
                                                 │
jonny's cambium: distill()  ◀────────────────────┘
        │ outcome memory (local)
jonny + teammates: recall() ×N  ──▶ trust grows ──▶ promote() → team
        │ visible to every collaborator's agent
someone: endorse()  ──▶ promote(to_scope="org")  ──▶ org repo / PR
        │
ANY agent, ANY project, ANY type (SRE bot, KB bot): recall(scope="org")
```

Passive capture: add a Claude Code hook that runs `distill` at session end —
capture then costs zero per-note effort. Run it on completion events too (a
post-commit hook) with `CAMBIUM_RELEASE_CAPTURE=1` and finished agentsync work
is captured at the moment it completes, before a release or re-claim can erase
it.

## Import

`import_memory(source, path)` ingests knowledge from an **external** memory
system into cambium. It is import/ingest **only** — it reads the external store
and writes cambium items; it never writes back to the source (export is a
separate, riskier feature and is deliberately out of scope). Import is modelled
as a **source adapter**, the same shape as distill's substrate readers: an
adapter reads records from a source location and yields normalized cambium
knowledge items, which land through the *same* normalize-and-write/dedupe path
distill uses — no second mechanism.

What import guarantees:

- **Local scope, always.** Imported items enter at `local` scope. They have not
  earned promotion inside cambium and are **not** auto-promoted — team/org is
  still earned the normal way, through `recall()` usage and `endorse()`.
- **Provenance, always.** Every imported item is stamped
  `source: {system, ref, imported: true, source_ts}` and tagged `imported`, so
  imported knowledge is distinguishable from natively-distilled capture and
  auditable back to its origin (system + original id + original timestamp).
- **Idempotent.** Re-importing the same records adds nothing — dedupe is by the
  source record's stable id when present, else by a content hash, routed through
  the shared watermark path.
- **Read-only against the source**, and dependency-free (stdlib, local files
  only — no network, no external auth).

### The `json` adapter (reference)

The one bundled adapter reads a **generic JSON / JSONL memory export from a
local file** — no service-specific coupling. It accepts a top-level array, an
object wrapping a list under `memories`/`items`/`records`/`data`/`entries`, or
JSONL (one JSON object per line). Each record maps as:

| cambium field | source keys (first present wins) | if absent |
|---|---|---|
| `content` (body) | `content`, `text`, `body`, `memory`, `note` | **record skipped** (no body) |
| — folded into body | `title`, `name`, `summary` | omitted |
| `why` | `why`, `reason`, `rationale`, `context` | empty |
| `kind` | `kind`, `type`, `category` | `"note"` |
| `tags` | `tags` (list or comma/space string) | just `imported`, `json` |
| `source.ref` | `id`, `uuid`, `_id`, `key` | content-hash dedupe instead |
| `source.source_ts` | `timestamp`, `created_at`, `ts`, `time`, `date` | omitted |

`type` is always `memory`; malformed lines and records with no usable body are
counted as **skipped**, never crash the import. The return value summarizes
`imported` / `skipped` / `duplicates`.

```bash
# import_memory(source="json", path="/abs/path/to/export.jsonl")
```

**Adapters are the extension point.** `json` is the only format shipped — this
is not a claim of support for any particular memory product. To ingest another
system, add one adapter (a generator yielding normalized items) to
`IMPORT_ADAPTERS`; core logic doesn't change. Adapters that require network
access or credentials are intentionally not included here.

## Machine-maintained documentation entropy

Trust-gated promotion defends knowledge on the way *in*: an entry only reaches
team or org after it earns recalls or an endorsement. But nothing marked it
going stale *afterward*. A fact that was true when it cleared the gate — "billing
runs on NetSuite", "the staging DB caps at 90 connections" — stays trusted long
after the premise dies. Worse, agents *recall* it, act on it, and cite it, so a
wrong assumption doesn't just persist; it gets institutionalized, and the more
it's used the more authoritative it looks. Promotion raises the stakes of being
wrong without adding any way to notice you've become wrong.

cambium closes this with **verification events and premise linkage**, not
confidence scores or time decay — both of which manufacture false precision. A
`0.62`-confidence memory implies a measurement nobody took, and "trust halves
every 90 days" would quietly demote knowledge that is simply stable and correct.
Instead every entry carries an optional `last_verified` timestamp (promotion
counts as the first verification; `verify_entry` records later ones) and an
optional `valid_while` premise naming the condition it depends on. Staleness is
**event-driven**: `stale_report` sorts promoted entries oldest-verified-first
and flags the never-reverified, and `distill`'s release-time path surfaces the
oldest-verified relevant entries right when work completes — so re-checking rides
an existing workflow beat. Absent or old `last_verified` is a *signal to a human*,
never an automatic downgrade. cambium reports the smell; a person decides.

## Seeing it: `tools/dashboard.py`

```bash
python tools/dashboard.py --open
```

Builds a single self-contained HTML view of the whole mesh — every project's
context-keeper log **and** the cambium layer distilled from them. Four views:

- **Overview** — the distillation funnel: recorded → active → distilled →
  *recalled* → promoted. Each stage a subset of the one above.
- **Projects** — sortable table of every store; click through to one project's
  full log, its supersession chains, its constraint scopes and its health flags.
- **Knowledge** — what distillation produced, what actually gets recalled, and
  where things sit on the local → team → org ladder.
- **Health** — mojibake, thin rationale, untagged, stale and never-recalled,
  each as a share of the population it is drawn from so a big store does not
  look unhealthy merely for being big.

It exists because counting was already possible and *understanding* was not.
The first run answered a question no single tool could:

| scope | items | recalled | recalls |
|---|---|---|---|
| local | 229 | 15 (7%) | 117 |
| team | 136 | **134 (99%)** | **774** |

**Promotion is what makes knowledge get read.** Local is a staging area and its
low recall rate is the system working, not failing — a conclusion only visible
with both scopes in one view. The first version of this tool read
`.cambium/knowledge.json` alone (local scope only) and reported *"nothing has
left local"*; team knowledge lives on a git branch, and 136 promoted items were
being recalled 774 times.

It also surfaced 84 items carrying cp1252 mojibake distilled before
context-keeper fixed the transport — concentrated on the team branches that are
recalled most. `tools/repair_mojibake.py` fixes the local stores; the team
branches are shared git state and are left to a deliberate push.

**Local by design.** The output is gitignored. The mesh spans private repos and
ones with no remote at all, so a published version could honestly show
aggregates and nothing more; this one shows everything, because it is yours.
Read-only, and `test_dashboard.py` asserts that byte-for-byte.

## Test

```bash
python3 test_cambium.py      # server integration suite
python3 test_dashboard.py    # the mesh dashboard
```

62 cases against real git repos: **markdown export** (`KNOWLEDGE.md` grouped by
scope then project with provenance/recalls/promoted-date, cp1252 mojibake
normalized, auto-written alongside `knowledge.json` on org promotion in both
direct-push and PR modes), **write-time normalization** (a substrate that feeds
distill a cp1252-mangled em-dash lands clean in the canonical store, so
`recall()` serves repaired text, not just the rendered `.md`), **onboarding**
(unconfigured `status()` reports
gaps with costs and fixes, every tool fails helpful when unconfigured, `setup()`
configures from a cold start and its config takes effect in-process, env
overrides the file, org names are offered as `gh` commands not created, non-git
and missing paths rejected), capture/recall (+ honest abstention),
distill from both substrates (exact agentsync claims format; exact
context-keeper `.context/` schema) with idempotency, **post-promotion
staleness** (optional `last_verified`/`valid_while` fields, absent-field
back-compat, `verify_entry` local + team round-trips, promotion stamps
verification, `stale_report` oldest-first ordering + never-verified flag + age
and project filters, release distill surfaces the verification prompt),
**release-time capture**
(off by default; a done claim survives a re-claim churn captured exactly once;
a noted claim released before it reaches *done* is kept where a full distill
would miss it), **import** (JSON + JSONL export → provenance-tagged local items,
re-import dedupes, content-hash fallback without ids, malformed/missing fields
skipped not crashed, imported items stay local and unpromoted, source left
untouched), the full promotion lifecycle (recall-threshold, endorsement
fast-track, org-requires-endorsement, PR-mode with `gh` stubbed), the
**org generalization gate** (a project-specific body is refused at the org
boundary with its tells and a suggested restatement; `org_content=` generalizes
and preserves the concrete body as `example`; `force=True` overrides; a clean
universal body is not over-blocked; `recall` surfaces `endorsed_as`;
`review_promotions` self-reports `org_needs_generalization`;
`generalize()` restates an already-promoted body in place, keeps the concrete
as `example`, clears the flag, and is idempotent), a distill **legacy-field
fallback** (a pre-v0.4 context-keeper decision carrying only `rationale` still
distills its WHY), cross-project trust tracking, team-write CAS under a
concurrent peer push, **two
real-agentsync integration tests** (drive the actual agentsync claim / finish /
release tools when the sibling repo is present — including the release-capture
seam), and a **real MCP stdio transport test**. CI runs it on every push.

## Limitations (honest ones)

- **Lexical recall, not semantic.** The scorer is token/substring overlap —
  deterministic and dependency-free. Swappable for embeddings later; the tool
  contract doesn't change.
- **Org usage isn't tracked** (recalls at org scope don't increment counters) —
  org items have already finished climbing.
- **PR-mode promotion isn't transactional** — the team copy stays (annotated
  with the PR URL) until a human merges. That's the point: review is the gate.
  A direct consequence: **promote to org one item at a time in PR mode.** Every
  `promote(item_id, to_scope="org")` branches off `origin/main` and appends to
  the same `items` array in `knowledge.json`, so two concurrently-open promotion
  PRs edit the same region of that file and the second can't merge cleanly until
  the first lands. (Observed: two knowledge PRs opened back-to-back on
  2026-07-10; the second needed manual conflict resolution against
  `knowledge.json`.) Open a PR, merge it, *then* promote the next — serializing
  avoids the conflict entirely.
- **Distill reads exactly ONE board per run.** `distill()` `git fetch`es
  `claims.json` from `<CAMBIUM_REMOTE>/<CAMBIUM_AGENTSYNC_BRANCH>` of the repo
  resolved by [board addressing](#where-the-agentsync-board-lives-board-addressing)
  — `AGENTSYNC_BOARD_REPO` if set, else `CAMBIUM_REPO` when it genuinely holds
  the branch. It is no longer pinned to the project repo, but it still does not
  merge several boards: work coordinated on a *different* board, or through the
  agentsync-remote transport against a different backing store, is invisible to
  that run. Distill once per board.
- **Distill captures at the moments it runs, not exhaustively.** By default it
  imports agentsync's *currently* done claims; a claim released or re-claimed
  before any distill catches it live is lost. `CAMBIUM_RELEASE_CAPTURE=1` closes
  most of that gap by capturing claims at their done/released transition (via a
  last-seen snapshot), but a done state created and churned away entirely
  between two sweeps is still only recoverable from agentsync's git log — a
  history walk that is not built here.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE.md) — free for any
noncommercial use.

## Related

Part of the [xylem](https://github.com/jarmstrong158/xylem) stack.
