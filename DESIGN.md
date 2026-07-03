# cambium — design

Why the system is built the way it is, so the decisions survive past the code.

## Problem

Organizations running AI agents accumulate knowledge in conversations and lose
it. Knowledge layers (Supermemory-style) fail for a specific reason: they're a
*side system* — devs' agents never call them. Meanwhile two substrates already
capture pieces of the answer: agentsync records **what happened** (claims,
finishes, notes, changed files, all in git) and context-keeper records **why**
(decisions, constraints, in `.context/` JSON). What's missing is the layer
between: nothing turns events into memory automatically, nothing serves one
recall endpoint to every agent type, and nothing graduates knowledge up scopes
as it earns trust. That lifecycle layer is cambium.

## Decision 1 — a composer, not another store

cambium reads agentsync and context-keeper **in place**, from the formats they
already write:

- agentsync: `git show origin/agentsync:claims.json` — no worktree, no export
  API, no coupling to agentsync's code. The claims format is small and stable.
- context-keeper: `.context/decisions.json` / `constraints.json` — the files
  are documented as human-editable JSON; reading them *is* the integration.

This is deliberate stigmergy over integration API: both tools already put their
state in the shared substrate, so the bridge is file reads, works offline, and
never requires either tool to be running (or even installed).

## Decision 2 — storage per scope, hidden behind recall()

Agents only ever call `recall()`; where items live is cambium's business:

- **local** — `.cambium/knowledge.json` in the project repo. Plain file; no
  ceremony for private notes.
- **team** — `knowledge.json` on a dedicated `cambium` branch of the shared
  repo. This is agentsync's proven pattern reused verbatim: dedicated branch
  (keeps knowledge out of code history, dodges branch protection), private
  worktree under `.git/cambium-wt` (never disturbs the checkout), and **git
  push as compare-and-swap** — a rejected push means a peer wrote first, so
  resync and retry, editing only your own delta. Two agents recalling and
  promoting concurrently cannot clobber each other (tested).
- **org** — `knowledge.json` in a dedicated org knowledge repo. A separate
  repo because org scope has different readership (everyone), different
  access control, and different blast radius than any one project.

Rejected: a database/vector store as the primary. It reintroduces the side
system (infra to run, a place knowledge goes to be forgotten). Git costs
nothing, versions everything, and gives promotion a review mechanism for free.
A vector index can later *serve* `recall()` with git still the source of truth
— the tool contract doesn't change.

## Decision 3 — trust must be earned, and org needs a human-shaped gate

Promotion thresholds encode a simple principle: **usage promotes, endorsement
elevates.**

- local → team: `recalls >= N` (default 3) *or* one endorsement. Recall counts
  are the cheapest honest signal — the item was actually useful to an agent,
  not just written down.
- team → org: an **endorsement is required**; recalls alone can never reach
  org. Bad org knowledge has org-wide blast radius, so crossing that boundary
  demands a deliberate vouch. In PR mode the pull request review *is* the
  gate, and `git revert` is the undo — promotion is reversible by construction.

`force=True` exists on promote for the same reason agentsync's claims are
advisory: the human is in charge; the defaults just make the safe path the
easy path.

## Decision 4 — passive capture is a hook calling distill(), not magic

"Zero burden" capture cannot mean an agent psychically knowing what to save.
It means: the knowledge-bearing artifacts are *already produced by work*
(agentsync notes, context-keeper decisions), and `distill()` is idempotent, so
a session-end / post-commit hook can run it unconditionally. The burden left
is one hook configuration, once. distill deliberately imports each source
record at most once (watermarks in the local store) so hooks can fire freely.

## Decision 5 — recall abstains instead of confabulating

Borrowed from context-keeper v0.10 (which measured 100% confabulation on
no-answer queries before adding abstention): when the top match falls below a
relevance floor, `recall()` returns `no_confident_match: true` plus guidance
not to present results as fact. A knowledge layer that answers confidently
when it knows nothing is worse than none: agents downstream repeat it.

Scoring is lexical (token + substring overlap, tags weighted double). That's a
known limitation, chosen for zero dependencies and deterministic tests;
embeddings are a swap-in behind the same contract.

## What is deliberately NOT here

- **Real-time sync / A2A transport** — same argument as agentsync's DESIGN:
  polling the substrate is the right default; both sides online is not.
- **Automatic org promotion** — the endorsement gate is the feature, not a
  missing automation.
- **Editing context-keeper's store** — the bridge is one-directional (import).
  context-keeper owns its files; cambium never writes `.context/`.
- **Semantic conflict/downgrade detection** — deprecation is manual (set
  `status: "deprecated"` — items are human-editable JSON by design).

## Failure notes

- Team/org unreachable → recall degrades to the scopes it can read; usage
  tracking is best-effort and never fails a recall.
- Duplicate promotion → all store writes are idempotent on item id.
- PR-mode partial failure → the team copy is only annotated after the PR
  exists; a failed PR leaves state unchanged and reports the error.
