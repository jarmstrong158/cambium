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

A wrinkle the substrate forces: agentsync keys claims by agent id and deletes a
claim from live state the moment it is released or re-claimed, and it exposes no
hook or event — only the rewritten `claims.json`. So the classic "read the
current done claims" distill loses any claim that completes and churns before it
runs. `CAMBIUM_RELEASE_CAPTURE=1` (opt-in — default behaviour must not change
silently) adds a *last-seen snapshot* of each agent's claim to the local store;
each sweep diffs live claims against it and captures anything that has churned
away, from the snapshot, through the **same** watermark path — no second dedupe.
The design choice is deliberate about what it does *not* promise: this is
capture at the moments distill runs, not exhaustive history reconstruction. The
honest residual gap — a done state born and gone between two sweeps — is
recoverable only from agentsync's git log (which `history()` proves survives);
walking it is a viable follow-up, not smuggled into this change.

## Decision 5 — recall abstains instead of confabulating

Borrowed from context-keeper v0.10 (which measured 100% confabulation on
no-answer queries before adding abstention): when the top match falls below a
relevance floor, `recall()` returns `no_confident_match: true` plus guidance
not to present results as fact. A knowledge layer that answers confidently
when it knows nothing is worse than none: agents downstream repeat it.

Scoring is lexical (token + substring overlap, tags weighted double). That's a
known limitation, chosen for zero dependencies and deterministic tests;
embeddings are a swap-in behind the same contract.

## Decision 6 — org is a wider readership, so its body must be generalized

A subtle mismanagement hid in the original `promote()`: team→org was a pure
scope-flag flip (`item["scope"] = "org"`), copying the body verbatim. But the
three scopes differ precisely in **readership** — local is you, org is *every*
project. A body that is correct in one repo ("append to `dashboard.py`
REGIMES", "back up as `clark_foundation.pt`") is not correct for a reader who
has no `dashboard.py`; promoted unchanged it is either useless or misleading,
and — worse — it now carries org-wide blast radius and gets *recalled, cited,
institutionalized*. Meanwhile the generalization almost always already existed,
written in the endorsement note ("Universal metrics practice: annotate a regime
boundary when a metric's computation changes") — and `recall()` buried it inside
the `trust` blob, so the one right statement was captured and hidden while the
wrong one was served.

The fix follows cambium's own split (Decision 3): **crossing into org is a
hard gate; drift within a scope is a soft signal.** This is a crossing, so it
gets a gate, mirroring the endorsement gate exactly — same `not_*` refusal,
same `force=True` override. `promote(to_scope="org")` runs a deterministic lint
(`_org_body_smells_local`: origin-project name, filenames, `test_*`/`Test*`
ids, `dec-/con-NNN` refs) and, if the body reads project-specific, **refuses**
unless the caller supplies `org_content=` (the cross-project restatement — the
concrete body is kept as `example`) or forces it. cambium does not rewrite
prose (it stays deterministic and model-free); it detects the smell, hands back
the endorsement note as a ready draft, and lets the human decide at the
boundary. Two companion moves close the loop: `recall()` surfaces endorsement
notes as first-class `endorsed_as` context (un-burying the generalization for
items already promoted), and `review_promotions()` reports
`org_needs_generalization` — org items whose body still trips the lint — so the
tool self-diagnoses the runbooks that crossed before the gate existed, instead
of a human having to eyeball them.

The lint is precision-biased on purpose: a bare `word/word` "path" is *not* a
tell, because prose uses slashes for lists (`survey/claim/update_status`), and
every real path of concern already ends in a filename the file-tell catches. A
false block is more expensive than a false pass here — `force=True` and the
`review_promotions` backstop both catch what the lint lets through.

## What is deliberately NOT here

- **Real-time sync / A2A transport** — same argument as agentsync's DESIGN:
  polling the substrate is the right default; both sides online is not.
- **Automatic org promotion** — the endorsement gate is the feature, not a
  missing automation.
- **Editing context-keeper's store** — the bridge is one-directional (import).
  context-keeper owns its files; cambium never writes `.context/`.
- **Semantic conflict/downgrade detection** — deprecation is manual (set
  `status: "deprecated"` — items are human-editable JSON by design).
- **Export to external memory systems** — `import_memory` ingests *from* an
  external store *into* cambium (a source adapter, same shape as distill's
  substrate readers, routed through the same normalize-and-write/dedupe path).
  The reverse — cambium writing into external systems — is a separate, riskier
  feature (it mutates someone else's store) and is deliberately out of scope.
  Imported items are provenance-stamped (`source.imported`) and land at local
  scope; they earn promotion the normal way, never automatically.

## Failure notes

- Team/org unreachable → recall degrades to the scopes it can read; usage
  tracking is best-effort and never fails a recall.
- Duplicate promotion → all store writes are idempotent on item id.
- PR-mode partial failure → the team copy is only annotated after the PR
  exists; a failed PR leaves state unchanged and reports the error.
