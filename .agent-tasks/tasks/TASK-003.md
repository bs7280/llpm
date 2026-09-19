---
id: TASK-003
title: FEAT-009: llpm next -- deterministic ready-ticket selection
status: done
created: 2026-09-18T21:09:58Z
remote: FEAT-009 +area.homelab.agent-platform.task-fabric.goals-layer +area.homelab.agent-platform.task-fabric.dispatch
---

# TASK-003 -- FEAT-009: llpm next -- deterministic ready-ticket selection

## Description

Execute the linked feature. It has no Verification section: write one first (workspace/notes/verification.md), then implement to it. Read the seeded design notes (goals-layer, heading "Autonomous-loop contract"; dispatch) and `uv run llpm skills --show llpm-loop` step 1, which `llpm next` replaces. Shape: `llpm next [--tier heavy|standard|light] [--json] [-n N]` (default 1, or "No ready tickets."). Ready = effective status open (blocked already excluded), not requires_human, `parser.dispatch_problems` empty (TASK-014, on main by the time you start), and with --tier: model_tier equal or unset. Order: priority high->low, then after: tie-break (all targets complete/closed ranks first), then ID; deterministic. Implement as service.next_tickets so api.py gets GET /{repo}/next and mcp.py gets a next_tickets tool, each exactly one service call, tests over both stores like the existing ones. Update the llpm-loop skill step 1 and `llpm help`. Commit as `feat(FEAT-009): ...`. Never push.

## Acceptance criteria

- your Verification section met, including ordering/filtering tests (blocked, requires_human, no-AC, tier)
- CLI + REST + MCP all answer next; `uv run pytest -q` green
- outbox ends with a ## Summary line and STATUS: review

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-09-18T21:09:58Z [mission-control] created
- 2026-09-18T21:20:40Z [task-003-d68fd9d4] claimed
- 2026-09-18T21:20:41Z [dispatcher] dispatched worker task-003-d68fd9d4 (session d68fd9d4-f12e-456a-afeb-a1b88cece50f, pid 90360, model opus, cwd /Users/benshaughnessy/code/llpm-worktrees/task-003-d68fd9d4)
- 2026-09-18T21:32:25Z [dispatcher] outbox of task-003-d68fd9d4 folded:
    # TASK-003 -- FEAT-009: `llpm next` deterministic ready-ticket selection
    
    - Starting: reading seed (ticket.md, goals-layer/dispatch design notes), then writing a Verification section to `notes/verification.md` before implementing `service.next_tickets` + CLI/REST/MCP surfaces.
    - Read the seed: FEAT-009 already carries a `## Verification` written by mission control 2026-09-18, so my job was to refine rather than invent it. Also read the goals-layer "Autonomous-loop contract" heading, the dispatch note, and `llpm-loop` step 1.
    - Wrote `notes/verification.md` — the Verification section I implement to: V1 surface, V2 ready set, V3 ordering, V4 one service call behind three surfaces, V5 test list, V6 docs. Two things it settles that the spec left open: `after` targets resolve from the in-memory board with a MISS falling through to `store.read` (so an *archived* target counts as complete, mirroring blockers), and a dangling/unreadable target counts as NOT satisfied.
    - **Finding for the planner (in notes/verification.md, "Finding 1"):** the spec's `--tier` clause ("model_tier equal *or unset*") is unreachable under the spec's own readiness gate — `no-tier` is one of `parser.dispatch_problems`' codes, so a tier-less ticket is never in the ready set with or without `--tier`. Implementing the filter literally as written and pinning the interaction with a test rather than silently dropping either clause.
    - Implemented `service.next_tickets(store, *, tier=None, limit=1)` + `service._after_satisfied` (the `after` tie-break, resolving from the in-memory board with a `store.read` fallthrough for archived targets). One board load, then one body read per candidate that survives the cheap status/tier filters — the same cost rule `lint_tickets` follows.
    - Wired the three surfaces, each exactly one service call: `llpm next [--tier] [-n N] [--json]` (`commands.cmd_next` + subparser, so `llpm help` picks it up automatically), `GET /{repo}/next?tier=&limit=`, MCP tool `next_tickets`.
    - Tests: 38 new service tests over BOTH stores (ready set, tier, ordering incl. the `after`/archived/dangling cases, determinism, limit, cost), 9 CLI, 7 REST, 6 MCP. Full suite: **1227 passed** (was 1167).
    - Also flagged, not changed: `llpm orphans`' text promises "NOT dispatch-eligible once llpm next enforces this (FEAT-009)" for `[intake] require_goal = enforce`. Making `next` drop orphans is a policy change outside this note's scope (and needs a board-config read the service layer doesn't take) — left alone.
    - Docs: rewrote `llpm-loop` step 1 as `llpm next` **wholesale** (the manual `llpm list`+`llpm lint` recipe is gone, not just annotated), updated the loop's dry-queue stop condition, the "does NOT do" list and the workflow summary; fixed `llpm-worker`'s stale "until `llpm next` ships"; `llpm help` picks the command up automatically from the subparser (asserted by a test); CLAUDE.md got a CLI-reference line, a new architecture bullet, and two stale phrasings corrected (lint's "will filter" → "filters", MCP's "thirteen tools" → fourteen).
    - Verified beyond the suite — evidence in `notes/test-report.md`: against **this repo's real vault board** `llpm lint` flags all 3 open tickets and `llpm next` correspondingly says `No ready tickets.` (the two agree on live data); on a throwaway local-dir board, priority ordering and the `after` tie-break both behave (a ticket that sorts FIRST by ID ranks second when its `after` target is unfinished); `llpm mcp` driven over a real stdio subprocess returns the service's listing for `next_tickets` and llpm's own sentence for a bad tier. HTTP is exercised through `TestClient` over the same ASGI app `llpm serve` runs, as every other endpoint in `test_api.py` is — no live port was bound.
    - Committed as `48881ba feat(FEAT-009): llpm next -- deterministic ready-ticket selection` (12 files, +660/-45), staged by name, not pushed. Working tree clean.
    
    ## Summary
    
    `llpm next` ships: one `service.next_tickets` behind CLI/REST/MCP that deterministically picks the next ready ticket (effectively-open, lint-clean, tier-filtered; priority → `after` tie-break → ID), with the llpm-loop skill's step 1 rewritten to use it — 60 new tests, full suite 1227 green.
    
    STATUS: review
- 2026-09-18T21:32:25Z [task-003-d68fd9d4] status: in_progress -> review (outbox sentinel)
- 2026-09-18T21:34:20Z [mission-control] status: review -> done
