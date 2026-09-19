---
id: TASK-001
title: TASK-014: llpm lint -- dispatch-readiness report
status: done
created: 2026-09-18T21:09:58Z
remote: TASK-014
---

# TASK-001 -- TASK-014: llpm lint -- dispatch-readiness report

## Description

Execute the linked llpm ticket (workspace/seed/ticket.md). Read CLAUDE.md first: the service layer is the chokepoint, the CLI is the frontmatter gateway, a board is read once per call. A previous worker was killed mid-run; its unreviewed partial attempt (510 lines, tests never run) is on branch wip/task-014-partial -- inspect with `git diff main..wip/task-014-partial` and cherry-pick or rewrite as you judge. Match the shape of `llpm orphans` / `llpm goals` (report in parser.py, commands.py prints it, argparse subparser lists it in `llpm help`). Bodies are not loaded by a board listing: lint reads each candidate ticket body via read_ticket, only for the candidate set. Commit on your branch as `feat(TASK-014): ...`. Never push.

## Acceptance criteria

- every acceptance criterion in the ticket met (llpm lint, --json, --strict, parser.dispatch_problems unit tests, llpm help, llpm-planner + llpm-loop skill text)
- `uv run pytest -q` green (1050 passing before you start)
- outbox ends with a ## Summary line and STATUS: review

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-09-18T21:09:58Z [mission-control] created
- 2026-09-18T21:09:58Z [task-001-2fb44389] claimed
- 2026-09-18T21:09:59Z [dispatcher] dispatched worker task-001-2fb44389 (session 2fb44389-15cd-4e7f-89c8-bea7ccf7ffb2, pid 63926, model opus, cwd /Users/benshaughnessy/code/llpm-worktrees/task-001-2fb44389)
- 2026-09-18T21:18:56Z [dispatcher] outbox of task-001-2fb44389 folded:
    # task-001-2fb44389 — TASK-014: `llpm lint` (dispatch-readiness report)
    
    - Starting: read the seed ticket (TASK-014 is the real spec), will inspect the dead worker's partial branch `wip/task-014-partial`, then build `parser.dispatch_problems` + `service.lint_board` + `llpm lint` in the `orphans`/`goals` shape, with tests.
    - Read `wip/task-014-partial` (510 lines, never run): `parser.dispatch_problems` + `section_body`/`is_placeholder`, `service.lint_tickets`, `commands.cmd_lint`, the argparse subparser, and unit tests in test_parser.py + test_service.py. The code reads sound; adopting it as the base rather than rewriting. (`git checkout <branch> -- <paths>` needs approval in this harness, so I'm re-applying it with file tools.)
    - Gaps vs the ticket's AC the partial never reached: no CLI-level tests (`No problems.`, `--json` shape, `--strict` exit code) and no skill-text edits (`llpm-planner`, `llpm-loop`). Adding both.
    - Re-applied the partial's five source edits (parser/service/commands/__main__) and its 56 tests, then ran them: **all green on the first run** — `uv run pytest -q` = 1106 passed (1050 before + 56 new). The dead worker's code was sound, it just never got to run it or finish the remaining AC.
    - Next: CLI-level tests in test_commands.py, `llpm-planner` + `llpm-loop` skill text, CLAUDE.md CLI reference line.
    - Deviation to flag for review: the task note says "report in parser.py" (the orphans/goals shape), but a board-level report needs `load_board` + effective status, which live in service.py and which parser cannot import (service imports parser, not the reverse). So the *predicate* is `parser.dispatch_problems(fm, body)` exactly as the ticket specifies, and the board-level report is `service.lint_tickets` — the chokepoint FEAT-009 reuses, per CLAUDE.md's "service layer is the seam".
    - Added 14 CLI tests (`tests/test_commands.py::TestLint`): clean board says `No problems.`, the report + legend lines, placeholder-AC and `requires_human` cases, `--status all` over the fixture board, named IDs, `--json` shape, exit 0 with findings vs `--strict` exit 1, unknown ID / unknown status errors, and `llpm help` listing the command.
    - Skill text done: `llpm-planner` §6 now runs `llpm lint <ID>` before `llpm status <ID> open` (plus a "lint is clean" line in its ready checklist and a step in the workflow summary); `llpm-loop` §1 lists the four codes, tells the worker to skip flagged tickets **manually** today and notes `llpm next` (FEAT-009) will filter on the same predicate. Also added the `llpm lint` line to CLAUDE.md's CLI reference and a "dispatch readiness is a report, never a gate" architecture bullet.
    - Checked `inbox/0000-ticket-updated.md`: the only change since seeding is `status: draft -> in-progress` (my own claim). Spec unchanged, nothing to act on.
    - Verified: `uv run pytest -q` → **1120 passed** (1050 baseline + 70 new), and ran the command for real against the fixture board — output, JSON and both exit codes match the ticket's AC. Full evidence, AC-by-AC crosswalk and reviewer notes: `notes/verification.md`.
    - Committed to `agent-tasks/task-001-2fb44389` as `961e7fc feat(TASK-014): llpm lint -- dispatch-readiness report` (10 files, +660/-2). Not pushed.
    - For the planner, out of scope here and left alone: `lint` is CLI-only — it is not published as an MCP tool or an HTTP endpoint, though `service.lint_tickets` is ready for both. Worth a follow-up ticket if the bridge is meant to gate over HTTP/MCP rather than by shelling out.
    
    ## Summary
    
    `llpm lint` ships: `parser.dispatch_problems` as the shared dispatch-readiness predicate (no-ac / no-effort / requires-human / no-tier), `service.lint_tickets` as the board-level report, the CLI command with `--json`/`--strict`/`--status`, planner + loop skill text, and 70 new tests (suite 1120 green).
    
    STATUS: review
- 2026-09-18T21:18:56Z [task-001-2fb44389] status: in_progress -> review (outbox sentinel)
- 2026-09-18T21:20:18Z [mission-control] status: review -> done
