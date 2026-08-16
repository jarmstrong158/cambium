# Spec: a model-written page tier

**Status: designed, not built.** Nothing in the codebase implements this. It is
written down so the choice can be made deliberately rather than by drift.

## What it is for

The current page tier is deterministic. It can *assemble* — resolve a reference
to the rule it names, render supersession as change history, group by role — but
it cannot do the three things that need judgement:

1. **Summarise.** "These six decisions are one idea, arrived at in stages."
2. **Reconcile.** "`con-004` and `con-009` both bound the same budget and they
   disagree about the number."
3. **Answer.** "What do we believe about silent fallbacks?" — a question whose
   answer is spread across four projects and stated nowhere.

Deterministic synthesis will never do these. That is the ceiling, and it is the
only reason to build this tier.

## Why it cannot just be added to the existing tier

Three properties of the current tier break if a model writes the body, and each
one is load-bearing today:

- **Regenerable from entries alone.** `pages.json` can be deleted and rebuilt
  byte-identically. A model's output is not a function of its inputs, so a
  rebuild produces different prose from the same entries.
- **Computed staleness.** Today a page is stale iff a source moved. If the body
  is generated, a rebuild that differs could mean the sources moved *or* that
  the model phrased it differently, and the two are indistinguishable — which
  is precisely the "cannot tell reported as a conflict" failure this project has
  already written a constraint about.
- **Never a trust-tier read.** A derived summary that reads as authoritative is
  exactly the thing kept out of `recall()`. A model-written page is *more*
  confident-sounding than a compiled one, so the isolation matters more, not
  less.

So it is a **separate tier with its own contract**, not a flag on `compile_page`.

## The contract

Store: `.cambium/essays.json`, a third file alongside `knowledge.json` and
`pages.json`. Same isolation rule — never returned by `recall()`,
`session_primer()`, `export_markdown()`, or any trust-tier read.

An essay record carries:

| field | meaning |
|---|---|
| `id`, `project`, `question` | what it was asked to answer |
| `body` | the model's prose |
| `sources[]` | entry ids + content hashes, exactly as pages do |
| `input_digest` | hash of the *rendered deterministic page(s)* fed to the model |
| `model`, `prompt_version` | what produced it |
| `written_at`, `written_by` | provenance |
| `reviewed_by`, `reviewed_at` | **required before it can be read by anything** |

### Staleness, made decidable again

The trick is that the model never reads `.context/` directly. It reads the
**deterministic page** as its input. That makes the input reproducible, so:

- `input_digest` changes ⇒ the material changed ⇒ the essay is stale.
- `input_digest` unchanged ⇒ the essay is current, regardless of what prose a
  re-run would produce.

Staleness stays a comparison, never a judgement about text. Re-running the model
on unchanged input is explicitly **not** a way to detect drift.

### Review is the gate, mirroring org promotion

cambium already has this shape: crossing into wider readership is a hard gate
(`promote(to_scope="org")` requires an endorsement). Same principle here — an
unreviewed essay is stored but marked `draft` and excluded from every export.
The human vouches that the prose is true to its sources. `force=True` exists for
the same reason it does on `promote`: the human is in charge, the default just
makes the safe path the easy one.

### Refusal, not confabulation

`recall()` abstains below a relevance floor rather than answering confidently
from nothing. An essay generator must inherit that: if the deterministic input
has fewer than N sources, or the question matches nothing, it **refuses to write
an essay** rather than producing fluent text with no basis. A knowledge layer
that writes confidently when it knows nothing is worse than none — that is
already recorded as Decision 5.

## Tools it would add

- `write_essay(project, question, force=False)` — compile the deterministic
  input, refuse if the basis is too thin, generate, store as `draft`.
- `review_essay(id, note)` — the gate. Marks reviewed; only reviewed essays
  export.
- `list_essays(project, stale_only)` — staleness by `input_digest`, same shape
  as `list_pages`.

## Open questions to settle before building

1. **Where does the model call happen?** cambium is an MCP server with no model
   access. Either the calling agent generates and passes the prose in (cambium
   stays model-free, which preserves its zero-dependency property), or cambium
   gains an API client and a key — a real change to what this process is.
   *The first is strongly preferred and would keep every existing property.*
2. **Cross-project essays** need an identity and a staleness rule spanning 20
   stores. The per-project case should ship first.
3. **What happens to a reviewed essay when it goes stale?** Probably: keep it,
   mark it, never silently regenerate — a reviewed artifact that quietly changes
   under the reviewer is the failure mode this whole gate exists to prevent.

## Do not build this until

The deterministic tier is actually being read and its limits are felt in
practice. As of writing, the mesh has 423 entries and 2 supersession links, so
the *inputs* to synthesis are thin — a model would be writing essays about a
graph that barely exists. Fix the linking first (see the Links view); the
ceiling only matters once you are up against it.
