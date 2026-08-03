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

`llpm next` (FEAT-009, deterministic ready-ticket selection) does **not exist yet**. Until it
ships, select manually:

```bash
llpm list --status open --json    # or: llpm board (OPEN column)
```

- `--status` filters on **effective** status -- a ticket with unresolved blockers reports as
  `blocked`, not `open`, so blocked tickets are already excluded. You don't need to separately
  walk `blockers`.
- `draft` and `planned` are **not workable** -- `draft` is an unspecified stub, `planned` is
  spec'd but not yet approved for work. Only `open` (or effectively-open) tickets are ready.
  `llpm backlog` shows draft+planned -- that's the pre-work pipeline, not your queue.
- `--json` output is already sorted priority high -> low, then ID -- take the first ticket
  you're equipped for. If several share top priority, a ticket's `after:` list is a soft
  ordering hint (never blocks) -- prefer one whose `after` tickets are already done.
- If a ticket carries `model_tier` (`light`/`standard`/`heavy`), prefer ones tagged for your
  own tier, or untagged ones -- don't grab a `heavy`-tagged ticket as a light-tier worker.

When FEAT-009 ships, replace this step with `llpm next` and drop the manual filtering above.

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

The CLI is a frontmatter gateway only -- it has no command to edit a ticket's body. Body
edits go around the CLI:

- **Local-dir store:** `llpm show <ID>` prints `File: <path>` -- edit that markdown file
  directly.
- **Vault store** (default for this homelab): `File:` is a vault stem
  (`repos.<board>.llpm.tasks.TASK-XXX`), not a filesystem path -- there is no local file. Use
  the agent-memory MCP (`append_content`, targeting that stem and a body heading) to add
  notes.

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
- `llpm list --status open --json` (or `llpm board`'s OPEN column) comes back empty -- the
  queue is dry.
- You hit a ticket you can't safely claim or finish -- spec unclear, missing information,
  genuinely out of scope. Push it back per **llpm-worker**'s "Handling Problems" and stop --
  do not guess, and do not spawn subagents to manufacture progress on a small task.
- You're not sure whether something is even a ticket. Tickets come from `llpm`
  (`show`/`list`/`board`) -- don't act on annotations, notes, or other tools' data as if they
  were llpm tickets.

## What This Skill Does NOT Do

- **Does not implement `llpm next`** -- step 1 is a manual stand-in until FEAT-009 ships a
  real deterministic scheduler. When it does, replace step 1 with `llpm next` wholesale.
- **Does not fix the claim race** -- documents it as a tolerable-at-small-N gap, not a bug
  this skill patches.
- **Does not enforce `## Handoff` via schema** -- convention only, unvalidated, on purpose.
- **Does not replace llpm-worker** -- that skill still owns the *how* of implementing a
  single ticket; this skill owns the loop shape around repeated ticket work.

## Workflow Summary

```
1. llpm list --status open --json        # select (manual until FEAT-009 ships llpm next)
2. llpm status TASK-XXX in-progress      # claim
3. [implement per llpm-worker]           # work
4. [edit body / append_content]          # jot progress as you go
5. [append ## Handoff to the ticket]     # outcome/shipped/surprises/questions/follow-ups
6. llpm status TASK-XXX review [--awaiting VALUE]   # park
7. repeat from 1, until queue is dry or you are blocked
```
