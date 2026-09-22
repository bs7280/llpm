---
id: TASK-004
title: TASK-024: MCP tools/call rejects unknown argument names
status: done
created: 2026-09-19T22:25:25Z
remote: TASK-024
---

# TASK-004 -- TASK-024: MCP tools/call rejects unknown argument names

## Description

Execute the linked llpm ticket — full text in workspace/seed/ticket.md. Every acceptance criterion is a checkbox; satisfy all of them.

The defect, already located for you: src/llpm/mcp.py _validate_args() does
    prop = properties.get(key)
    expected = prop.get("type") if prop else None
    if expected is None:
        continue
so an argument name absent from the tool's inputSchema falls through and is silently dropped. list_tickets {"n": 5} returns the DEFAULT answer, successfully. Fix is the unknown-key check, before the type checks, returning through the existing _tool_error path.

DUTIES:
- Built-in tools (Read/Edit/Glob/Grep) over shell for file work.
- Commit, NEVER push. Work on your agent-tasks/<worker-id> branch.
- Claim is already done (ticket is in-progress) — do not touch llpm ticket status.
- Jot to the Worklog as you go, at the moment things happen (hypotheses, dead ends, surprises), with this exact call, changing only text:
    mcp__agent-memory__append_content(stem="repos.llpm.llpm.tasks.TASK-024", heading="Worklog", text="**2026-09-19 <you>** — <one-line jot>")
- NEVER wait on Monitor, background notifications, or a backgrounded command and end your turn — a headless run is never re-invoked, that is death not patience. Run long commands in the foreground.
- Finish with a ## Summary line then the STATUS sentinel, and a ## Handoff (outcome / shipped / surprises / open questions / follow-ups).

## Acceptance criteria

- every acceptance criterion in seed/ticket.md is met
- uv run pytest passes
- committed, not pushed

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-09-19T22:25:25Z [agent] created
- 2026-09-19T22:26:06Z [task-004-83ac5ea7] claimed
- 2026-09-19T22:26:08Z [dispatcher] dispatched worker task-004-83ac5ea7 (session 83ac5ea7-9395-451f-b83f-376870a91e79, pid 59307, model sonnet, cwd /Users/benshaughnessy/code/llpm-worktrees/task-004-83ac5ea7)
- 2026-09-19T22:31:32Z [dispatcher] outbox of task-004-83ac5ea7 folded:
    Starting TASK-024: fix `_validate_args` in src/llpm/mcp.py to reject unknown argument names (before type checks), with a near-miss suggestion, via the existing `_tool_error` isError path; then add tests and run the suite.
    Checked inbox/0000-ticket-updated.md: it's an echo of the current ticket text (no new instructions, no diff from seed/ticket.md aside from my own worklog jot appearing in it). Nothing to act on.
    Added `TestUnknownArguments` to tests/test_mcp.py covering all 8 acceptance criteria (unknown-key naming + tool name, near-miss suggestion, no-suggestion case, unknown-before-type ordering, isError-not-protocol-error, every tool in TOOLS, null-unknown-key still rejected, null-known-key not a type error, full-argument regression for list_tickets and create_ticket).
    Full suite: uv run pytest -x -q -> 1249 passed. ruff check src/llpm/mcp.py tests/test_mcp.py -> clean. Committed as 65c5646 on agent-tasks/task-004-83ac5ea7 (not pushed). Final inbox check: no new items beyond the ticket-echo already acknowledged.
    
    ## Handoff
    - outcome: done
    - shipped: `_unknown_arg_message` + an unknown-key pass in `_validate_args` (src/llpm/mcp.py), running before the type-check loop, shared by both the stdio and HTTP MCP transports since both call `_call_tool`. `difflib.get_close_matches` supplies the near-miss hint (stdlib, no new dependency). Renamed `_call_tool`'s local `type_error` -> `arg_error` since the check is no longer type-only. Added `TestUnknownArguments` (10 test methods x 2 store params = 20 cases) to `tests/test_mcp.py`.
    - surprises: `get_ticket`'s existing `bool(args.get("body", True))` already turns an explicit `body=None` into `False` (pre-existing, unrelated to this fix) -- my first draft of the null-known-key test wrongly assumed `None` meant "use the default", had to correct the assertion to "not a type error" instead of "body key still present".
    - open questions: none -- all 8 acceptance criteria in seed/ticket.md are met and directly tested.
    - follow-ups: none identified; this was a scoped, self-contained fix.
    
    ## Summary
    Fixed `_validate_args` in `src/llpm/mcp.py` so `tools/call` rejects any argument name absent from a tool's `inputSchema` (with a `difflib` near-miss suggestion) before the existing type checks, returned via the existing `_tool_error` isError path -- added 20 tests (`TestUnknownArguments` in `tests/test_mcp.py`) covering every acceptance criterion; full suite passes (1249) and the change is committed, not pushed.
    
    STATUS: review
- 2026-09-19T22:31:32Z [task-004-83ac5ea7] status: in_progress -> review (outbox sentinel)
- 2026-09-22T00:12:44Z [mission-control] llpm ticket verified and flipped complete in the 2026-09-19 mission-control cycle; wrapper closed in the 2026-09-21 hygiene pass
- 2026-09-22T00:12:44Z [mission-control] status: review -> done
