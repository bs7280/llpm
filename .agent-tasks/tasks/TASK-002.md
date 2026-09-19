---
id: TASK-002
title: FEAT-016 fix: validate MCP tools/call arguments
status: done
created: 2026-09-18T21:09:58Z
remote: FEAT-016
---

# TASK-002 -- FEAT-016 fix: validate MCP tools/call arguments

## Description

The linked feature was reviewed and sent back -- read the mission-control review entries at the end of its ## Worklog in workspace/seed/ticket.md. Fix in src/llpm/mcp.py: validate every tools/call argument against the tool inputSchema types (array / string / object / integer / boolean) before tool.run, answering an isError result whose text names the argument and the expected type; answer -32602 with a plain sentence (never -32603 with Python exception text) for non-object params, non-string name, non-string id. Turn the repro cases into tests over both stores: set_status(commits="abc123") must not write six one-char SHAs; list_tickets(fields="id,title") must be refused; create_ticket(tags=5) must be refused. Do not change the tool surface or the transports. Commit as `fix(FEAT-016): ...`. Never push.

## Acceptance criteria

- the three repro cases are tests and pass; existing 171 mcp/api tests still pass; `uv run pytest -q` green
- malformed envelopes answer -32602 with a sentence
- outbox ends with a ## Summary line and STATUS: review

## Notes

_(worker scratch space -- findings, decisions, open questions)_

## Work log

- 2026-09-18T21:09:58Z [mission-control] created
- 2026-09-18T21:09:59Z [task-002-c47e6348] claimed
- 2026-09-18T21:10:00Z [dispatcher] dispatched worker task-002-c47e6348 (session c47e6348-c927-4a54-a3cc-08fac8afc6c4, pid 63941, model sonnet, cwd /Users/benshaughnessy/code/llpm-worktrees/task-002-c47e6348)
- 2026-09-18T21:16:40Z [dispatcher] outbox of task-002-c47e6348 folded:
    Starting: validating MCP tools/call arguments against each tool's inputSchema before tool.run in src/llpm/mcp.py, per FEAT-016 review defect (mission-control session e84197f0) -- fixing the set_status(commits="abc123") data-corruption case plus list_tickets(fields=...) and create_ticket(tags=5), and fixing malformed-envelope error codes (-32602 with a sentence, not -32603 with raw exception text).
    Implemented in src/llpm/mcp.py: `_validate_args` type-checks every tools/call argument (including array item types) against the tool's inputSchema before `tool.run`, returning isError naming the argument and expected type -- this alone catches all three repro cases (set_status commits=str, list_tickets fields=str, create_ticket tags=int) since each is declared `type: array` in its schema. Also hardened `Session.handle`: top-level `params` must be an object if present, and a non-string/unhashable tools/call `name` and a non-string/number `id` now answer -32602 with a sentence instead of falling through to the generic exception handler's -32603 raw-exception text.
    Next: writing tests for the three repro cases (both stores) and the malformed-envelope cases, then running the full suite.
    Ran `uv run pytest -q tests/test_mcp.py` (111 passed, up from 95 -- the 16 new tests) and the full `uv run pytest -q` suite (1066 passed, up from 1050, no other regressions). Verified by hand that git diff is scoped to only src/llpm/mcp.py and tests/test_mcp.py. Committed as 64f5819 (`fix(FEAT-016): validate MCP tools/call arguments against inputSchema`); not pushed.
    Note for the reviewer: the review's "non-string id" line I read as the JSON-RPC envelope's top-level id needing to be a valid id type (string/number/null, not object/array) rather than literally string-only -- literal string-only would have broken every existing test, which all use integer ids. Flagging the interpretation call in case a different reading was intended.
    
    ## Summary
    Fixed FEAT-016's MCP argument-validation gap: `tools/call` arguments are now type-checked against each tool's inputSchema before `tool.run`, closing the three data-corruption repro cases (`set_status(commits="abc123")`, `list_tickets(fields="id,title")`, `create_ticket(tags=5)`) with an `isError` naming the bad argument and expected type, and malformed JSON-RPC envelopes (non-object params, non-string tool name, non-scalar id) now answer -32602 with a sentence instead of -32603 with raw Python exception text. 16 new tests added, full suite green (1066 passed).
    
    STATUS: review
- 2026-09-18T21:16:40Z [task-002-c47e6348] status: in_progress -> review (outbox sentinel)
- 2026-09-18T21:18:15Z [mission-control] status: review -> done
