# cambium

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

```json
{
  "mcpServers": {
    "cambium": {
      "command": "python3",
      "args": ["/abs/path/to/cambium_server.py"],
      "env": {
        "CAMBIUM_REPO": "/abs/path/to/your/project/clone",
        "CAMBIUM_AGENT_ID": "jonny"
      }
    }
  }
}
```

| env var | required | default | meaning |
|---|---|---|---|
| `CAMBIUM_REPO` | yes | — | your project clone (local scope, agentsync + context-keeper substrates) |
| `CAMBIUM_AGENT_ID` | yes | — | your unique agent id |
| `CAMBIUM_REMOTE` | no | `origin` | git remote |
| `CAMBIUM_TEAM_BRANCH` | no | `cambium` | team-scope branch |
| `CAMBIUM_AGENTSYNC_BRANCH` | no | `agentsync` | where distill reads coordination events |
| `CAMBIUM_ORG_REPO` | no | — | path to the org knowledge repo clone (org scope off without it) |
| `CAMBIUM_ORG_PR` | no | direct push | `1` = org promotion opens a pull request |
| `CAMBIUM_PROMOTE_RECALLS` | no | `3` | recalls needed for local→team |

**Org setup**: create one (private) repo, e.g. `github.com/you/knowledge`, with
an empty `{"items": []}` in `knowledge.json`; everyone who should read org
knowledge clones it and points `CAMBIUM_ORG_REPO` at their clone. cambium
manages that clone (it hard-syncs it) — dedicate it, don't work in it.

## Tools

**`capture(content, type, kind, why, tags)`** — save a knowledge item to local
scope (types: `memory` | `need` | `skill`). Manual path.

**`record_need(content, why, tags)`** — first-class needs ("we're missing X"),
promotable like anything else so recurring wants surface at team/org level.

**`distill()`** — the automatic path. Reads agentsync's coordination branch
(every *done* claim: task + note + changed files → an `outcome` memory) and
context-keeper's `.context/` (active decisions & constraints, rationale and
`dec-NNN` provenance preserved). Idempotent — wire it to a session-end or
post-commit hook and capture becomes passive.

**`recall(query, scope, limit)`** — federated search across local+team+org.
Every hit increments the item's recall counter (the trust signal promotion
feeds on) and records cross-project use. Abstains honestly: below the relevance
floor it returns `no_confident_match: true` instead of confident-looking noise.

**`endorse(item_id, note)`** — vouch for an item. Fast-tracks local→team;
**required** for team→org.

**`promote(item_id, to_scope, force)`** — no args: scan-and-promote all
eligible local items to team. With `to_scope="org"`: push to the org repo, or
open a PR when `CAMBIUM_ORG_PR=1` (the team copy stays, annotated, until the
PR merges).

**`review_promotions()`** — what's eligible for team, what's endorsed for org,
which org PRs are pending.

**`status()`** — counts per scope/type, import watermarks, which substrates
are actually wired.

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
capture then costs zero per-note effort.

## Test

```bash
python3 test_cambium.py
```

20 cases against real git repos: capture/recall (+ honest abstention),
distill from both substrates (exact agentsync claims format; exact
context-keeper `.context/` schema) with idempotency, the full promotion
lifecycle (recall-threshold, endorsement fast-track, org-requires-endorsement,
PR-mode with `gh` stubbed), cross-project trust tracking, team-write CAS under
a concurrent peer push, a **real-agentsync integration test** (drives the
actual agentsync tools when the sibling repo is present), and a **real MCP
stdio transport test**. CI runs it on every push.

## Limitations (honest ones)

- **Lexical recall, not semantic.** The scorer is token/substring overlap —
  deterministic and dependency-free. Swappable for embeddings later; the tool
  contract doesn't change.
- **Org usage isn't tracked** (recalls at org scope don't increment counters) —
  org items have already finished climbing.
- **PR-mode promotion isn't transactional** — the team copy stays (annotated
  with the PR URL) until a human merges. That's the point: review is the gate.
- **Distill imports agentsync's *current* done claims** — a claim released or
  re-claimed before a distill run is not reconstructed from history.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE.md) — free for any
noncommercial use.
