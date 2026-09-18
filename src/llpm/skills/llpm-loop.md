# LLPM Loop: Autonomous Worker Cycle

You are running the **autonomous worker loop** -- an unattended, multi-ticket session that
repeatedly selects a ready ticket, claims it, does the work, and parks it, without checking
in between tickets. This skill is the *cycle* as rails: a fixed sequence of mechanical steps,
not prose to interpret. For the single-ticket implementation discipline (reading specs,
following acceptance criteria, running tests, committing, handling ambiguity), see
**llpm-worker** -- this skill wraps that workflow in the loop, it doesn't replace it.

## Why Rails, Not Prose

On 2026-08-01 a light-tier (haiku) worker running a loose "keep working" loop conflated
marginalia annotations with llpm tickets, spawned four subagents to do a five-call task, and
reported completion that hadn't happened. None of that required more intelligence to avoid --
it required a fixed sequence with unambiguous stop conditions. That's what this skill is: do
these seven steps, in this order, and stop when the queue is dry or you're blocked. Don't
improvise the loop shape, and don't reach for subagents to pad out small tasks.

## The Loop

```
1. select next ready ticket
2. claim it          -- llpm status <ID> in-progress
3. work it            -- see llpm-worker
4. jot progress        -- as you go, into the ticket body
5. append Handoff      -- ## Handoff section, every run, no exceptions
6. park it             -- llpm status <ID> review [--awaiting <value>]
7. repeat              -- until the queue is dry or you are blocked
```

### 1. Select the Next Ready Ticket

```bash
llpm next                          # the one ticket to work on next
llpm next --tier light --json      # ...for your own model tier, as JSON
```

That is the whole step. `llpm next` (FEAT-009) is the deterministic selector -- don't
reimplement it out of `llpm list` + your own judgement, and don't take a ticket it didn't
offer you. It prints `No ready tickets.` when the queue is dry, which is stop condition 1 in
step 7.

What it already did for you, so you don't have to:

- **Blocked work is gone.** Readiness is the **effective** status -- a ticket with an
  unresolved `blockers` entry or a blocking `waits_on` stem reports as `blocked`, never
  `open`, so you never walk edges yourself.
- **Pre-work and claimed work are gone.** `draft` is an unspecified stub and `planned` is
  spec'd but not approved; `in-progress` is someone's claim and `review` is parked.
  `llpm backlog` shows the draft+planned pipeline -- that is the planner's queue, not yours.
- **Undispatchable work is gone.** The same predicate `llpm lint` (TASK-014) reports gates
  the ready set: acceptance criteria missing or still the template placeholder (`no-ac`), no
  `effort` (`no-effort`), no `model_tier` (`no-tier`), `requires_human: true`
  (`requires-human` -- a human owns it). A `no-ac` ticket you'd otherwise have to spec
  yourself belongs back with the planner: leave it, don't write its criteria and then grade
  yourself against them. `llpm lint` is still worth running when you want to *see* what was
  skipped and why.
- **Ordering is settled and reproducible**: priority high -> low, then an `after:` tie-break
  (a ticket whose `after` targets are all complete/closed comes first -- `after` is soft
  precedence and never blocks), then ID. The same board answers the same way every time.

`--tier heavy|standard|light` restricts the answer to tickets tagged for your own tier --
pass it if you know which tier you are running as, so a light worker never picks up a
`heavy` ticket. `-n N` returns the top N (default 1) when you want to see what's behind the
one you're taking; take them one at a time regardless.

`llpm next` **selects, it does not claim**: it writes nothing and changes no status. Step 2
is still yours.

### 2. Claim It

```bash
llpm status <ID> in-progress
```

**Known gap, documented not fixed here:** claiming is not compare-and-swap. Two workers can
both read the same ready ticket and both claim it before either sees the other's write --
there is no lock. This is tolerable at small scale (N<=2 concurrent workers); the real fix is
a run-model with CAS semantics (see `goals.autonomous-task-loop`), not something this skill
patches. If `llpm show <ID>` shows the ticket already `in-progress` with recent activity you
didn't make, back off and select a different ticket rather than contesting it.

### 3. Work It

Follow **llpm-worker** for the actual implementation: read the spec, respect acceptance
criteria, stay in scope, run tests, commit referencing the ticket ID. This skill doesn't
duplicate that discipline -- it only adds the loop shape around it.

### 4. Jot Progress as You Go

Every ticket body carries a `## Worklog` section -- the designated home for train-of-thought
notes you want to survive the session: hypotheses, dead ends, discovered constraints,
workarounds, missing-capability moments ("needed an API for X, did Y instead"), commit links.
Append-only -- never edit a prior entry -- one entry per jot, in the format:

```
**<date> <agent/session>** -- <text>
```

Jot AT THE MOMENT something happens, not retroactively when you park the ticket. A worklog
reconstructed from memory at Handoff time is a summary; the trail is what mission control's
liveness probe ("no jot past TTL = stuck") and a resuming agent after a dead run actually need.

The CLI is a frontmatter gateway only -- it has no command to edit a ticket's body. Body
edits go around the CLI:

- **Local-dir store:** `llpm show <ID>` prints `File: <path>` -- edit that markdown file
  directly, appending your entry under `## Worklog`.
- **Vault store** (default for this homelab): `File:` is a vault stem
  (`repos.<board>.llpm.tasks.TASK-XXX`), not a filesystem path -- there is no local file.
  **Bind the jot call ONCE at claim time and reuse it verbatim** -- fill `stem` from
  `llpm show <ID>`'s `File:` line, then every jot for the rest of the run is the exact same
  call with only `text` changed:

  ```
  mcp__agent-memory__append_content(
      stem    = "repos.<board>.llpm.tasks.TASK-XXX",   # bound once, at claim
      heading = "Worklog",                              # never changes
      text    = "**<date> <agent/session>** -- <one-line jot>")
  ```

  No per-jot decisions about where things go -- if you're thinking about the destination,
  you're doing it wrong. If the heading doesn't exist yet (older ticket, predates TASK-010),
  `ensure_heading` once, then the same bound call.

Keep jots short -- a line or two noting what you just did or found, not a running narration.
This is what makes a `review` handoff legible later without replaying the whole session.

### 5. Append `## Handoff`

Every worker run ends by appending a `## Handoff` section to the ticket body. **Light tiers
only append** -- never rewrite frontmatter or existing prose. Heavy tiers may also revise
other sections if the work uncovered something the spec got wrong. Schema enforcement of this
heading is explicitly deferred -- there is no validator; this is convention held by
discipline, not tooling.

```markdown
## Handoff (2026-08-02)

- outcome: done
- what shipped: <one or two lines>
- surprises: <anything that didn't match the spec, or "none">
- open questions: <anything still unresolved, or "none">
- proposed follow-ups:
  - <zero-ceremony list item -- not a new ticket>
  - <another, if any>
```

`outcome` is one of `done | partial | failed`. Proposed follow-ups are **capture, not
create** -- do not spin them into tickets yourself. A reconciler/planner reviews Handoffs
later and promotes worthy ones to `origin: agent` draft tickets via the ticket-intake policy
(draft-by-default, `llpm orphans` reporting -- see the project's CLAUDE.md).

### 6. Park It

```bash
llpm status <ID> review                        # default: awaiting reviewer
llpm status <ID> review --awaiting <value>      # or: push | deploy | human-verify | human-answer
```

`--awaiting` is only valid when parking into `review`; absent means `reviewer` (FEAT-012).
Pick whichever actually describes what unsticks the ticket next:

- **reviewer** (default) -- a human or another agent needs to review the diff.
- **push** -- work is committed locally but needs a human's go-ahead before it's pushed.
- **deploy** -- code has landed but needs a deploy step before it's really done.
- **human-verify** -- needs manual/real-world verification beyond reading the diff.
- **human-answer** -- blocked on a question only a human can answer.

If the ticket genuinely isn't done and isn't ready for review, don't park it into `review` to
look finished -- see Handling Problems in **llpm-worker**, and stop the loop instead.

### 7. Repeat, Until the Queue Is Dry or You Are Blocked

Go back to step 1. Reselect -- don't assume the queue you saw at the start of the session is
still accurate; a blocker may have cleared, or a new ticket may have gone `open` mid-session.

Stop the loop when:
- `llpm next` says `No ready tickets.` (`[]` with `--json`) -- the queue is dry.
- You hit a ticket you can't safely claim or finish -- spec unclear, missing information,
  genuinely out of scope. Push it back per **llpm-worker**'s "Handling Problems" and stop --
  do not guess, and do not spawn subagents to manufacture progress on a small task.
- You're not sure whether something is even a ticket. Tickets come from `llpm`
  (`show`/`list`/`board`) -- don't act on annotations, notes, or other tools' data as if they
  were llpm tickets.

## What This Skill Does NOT Do

- **Does not re-implement selection** -- step 1 is `llpm next` (FEAT-009) and nothing else.
  Don't rebuild it out of `llpm list` plus your own filtering; if `llpm next` offers nothing,
  the queue is dry, not wrong.
- **Does not fix the claim race** -- documents it as a tolerable-at-small-N gap, not a bug
  this skill patches.
- **Does not enforce `## Handoff` via schema** -- convention only, unvalidated, on purpose.
- **Does not replace llpm-worker** -- that skill still owns the *how* of implementing a
  single ticket; this skill owns the loop shape around repeated ticket work.

## Workflow Summary

```
1. llpm next [--tier <tier>]             # select -- blocked/undispatchable already excluded
2. llpm status TASK-XXX in-progress      # claim
3. [implement per llpm-worker]           # work
4. [append_content -> ## Worklog]        # jot progress as you go, at the moment it happens
5. [append ## Handoff to the ticket]     # outcome/shipped/surprises/questions/follow-ups
6. llpm status TASK-XXX review [--awaiting VALUE]   # park
7. repeat from 1, until queue is dry or you are blocked
```
