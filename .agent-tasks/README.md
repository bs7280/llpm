# agent-tasks queue

Machine-managed task queue shared by planner and worker agents
(https://github.com/bs7280/simple-subagent-dispatcher).

- `index.json` -- source of truth for task **metadata**: status, assignee,
  blockers, priority, tags. Change these via the `tasks.py` CLI only, never by
  hand-editing this file (the CLI serializes concurrent writers).
- `tasks/TASK-NNN.md` -- one note per task. The note body is free-form and
  agents are meant to edit it directly (description, notes, findings) -- that is
  the point of the system. Keep **Work log** as the last section; the CLI
  appends entries to the end of the file.

- `config.json` -- optional per-project dispatcher defaults (this is where a
  project records its own judgment calls). All keys optional:
  `worktree` (false), `worktree_root` (sibling `<repo>-worktrees/`),
  `runner` (interpreter argv composed into worker prompts, allowlists, and
  the bootstrap invocation; unset = auto-detect -- uv if installed, else the
  best python3 on PATH),
  `lease_minutes` (90 -- claim lease length; expired claims are stealable),
  `model_tiers` (["haiku","sonnet","opus"] -- ordering behind `--tier`),
  `mutex_stale_minutes` (30 -- named-mutex stale-steal timeout),
  `supervisor_ttl_minutes` (20 -- an unrefreshed supervisor lease is abandoned),
  `await_poll_seconds` (5), `await_debounce_seconds` (20 -- a burst of worker
  finishes becomes ONE planner wake), `await_timeout_minutes` (240 -- quiet for
  this long and `await` exits 2, suggesting a handoff),
  `model` (claude CLI default), `permission_mode` ("acceptEdits"),
  `allowed_tools` ([] -- extra permission rules for what your workers may run;
  Bash(...)/PowerShell(...) entries get their other-shell twin added
  automatically unless `expand_shell_rules` is false),
  `bootstrap` (".claude/task-worker-bootstrap.py" -- a Python script),
  `claude_bin` ("claude" -- string or argv list), `extra_args` ([]),
  `seed_hook` / `sync_hook` (null -- argv lists bridging a remote tracker:
  seed runs before a worker spawns and fills its workspace `seed/`; sync
  pushes the worker's outbox + workspace onward at spawn, every
  `sync_interval_seconds` (120) while supervised (`dispatch wait`/`watch`
  on one worker, `tasks await` on all of them), and at fold with the
  outcome; both get a JSON payload on stdin; `hook_timeout_seconds` 120).
  A task's `remote` (`create --remote`, `tasks remote ID [REF]`) is an
  opaque string only those hooks interpret.
  `prices` ({} -- per-model USD-per-million-token overrides for the
  dispatcher's estimated cost, shipped in the sync payload's `activity`
  block and shown by `dispatch status`: {"<model id>": {"input", "output",
  "cache_read", "cache_write_5m", "cache_write_1h"}}; unknown models get a
  null cost and raw usage).
- `config.local.json` -- optional machine-local overlay, merged key-by-key
  over `config.json` (gitignored by init). Any key may be overridden; put
  machine facts here (claude_bin path, runner), project policy in
  config.json.
- `events.jsonl` -- append-only journal: one line per mutation (seq, ts,
  task, kind, agent, msg). Read a delta with `tasks since --cursor N` instead
  of re-reading the board and every note; `tasks await` watches it.
- `handoff.md` -- the current planner handoff: queue state, in-flight workers,
  what needs a decision, and the last planner's intent. Written by
  `tasks handoff --write`; read by the next session with `tasks handoff --show`.
  Overwritten each time (the folder's git history keeps the old ones).
- `runtime/` -- machine-local dispatcher state (worker registry, spawn logs,
  `supervisor.json` -- which session is watching the queue); self-gitignored,
  never committed.

Statuses: open -> in_progress -> review -> done (or cancelled).
A blocker that names a task id auto-resolves when that task is done/cancelled;
free-text blockers stay until removed with `unblock`.

This folder belongs in the repo: commit it and task state travels with the
project (with history for free). Projects that prefer not to can gitignore it.
