# LLPM

LLM Project Manager -- a CLI tool for stateless, markdown-based project management designed for LLM multi-agent workflows.

## Quick Start

```bash
uv sync                    # install deps
uv run llpm --help         # see all commands
uv run pytest -x -v        # run tests (1050 tests)
uv sync --extra api        # + fastapi/uvicorn, only needed for `llpm serve`
```

## Project Structure

```
src/llpm/
    __main__.py      # CLI entry point (argparse + dispatch)
    parser.py        # Frontmatter parsing, validation, ticket discovery
    service.py       # Library seam: plain functions over a store, typed errors
    commands.py      # All command implementations (printers over service.py)
    api.py           # FastAPI router over service.py (optional llpm[api] extra)
    mcp.py           # MCP tools over service.py (stdlib; stdio + the api.py mount)
    templates/       # Bundled defaults (copied to project on init)
    skills/          # Bundled Claude skills (installable via `llpm skills`)
tests/
    conftest.py      # docs_root fixture (copies fixtures to tmp_path)
    test_parser.py   # Parser unit tests
    test_commands.py # Command integration tests
    fixtures/docs/   # Realistic interconnected ticket tree for tests
```

## Key Architecture Decisions

- **YAML frontmatter** is the structured data layer; markdown bodies are freeform
- **CLI is the frontmatter gateway** -- use CLI for structured ops, edit bodies directly
- **Derived fields**: `blocked` status and `children` are computed at read time, never stored
- **A board is read ONCE, in one request**: `parser.load_all_tickets` carries `(ref, frontmatter)` and NO bodies -- nothing that walks a whole board ever used them, and on a remote store fetching them was the entire cost. `TicketStore.load_frontmatter` lets a store answer the whole board its cheapest way: `MdTreeStore` uses one `?pattern=<ns>.*&include=frontmatter` listing (the vault's own index -- the endpoint marginalia's board.py has always used), `LocalDirStore` reads the files it would have read anyway. Blockers then resolve from `service.board_index` (`{ID: frontmatter}` of that same load) exactly as children resolve from `children_index`, with a MISS still falling through to the store so an archived blocker doesn't read as 'not found'. A 40-ticket board went 63 requests -> 7 (the rest are cross-board `waits_on` stems), ~25 s -> ~3 s from inside a container where every request pays a fresh DNS + TCP + TLS handshake. Anything that wants a BODY reads that one ticket (`read_ticket` / `store.read`).
- **A store's cache lives inside one service call** (TASK-021) -- `MdTreeStore` caches resolved `waits_on` stems so a board listing reads each distinct target once instead of once per ticket, and `service._begin_read_scope` empties it at the top of `load_board` / `read_ticket` (the two roots every derive path starts from, so the scope never reopens mid-board). Without that boundary the cache was per *process*: `llpm serve` and marginalia's `/api/llpm` mount keep one store per board alive, so a cross-board target's status froze at whatever it was when first read and only a restart unstuck it. The store also drops a cached stem it writes itself (either spelling -- archiving moves the note). `load_frontmatter` and `board_index` never had the problem: both are rebuilt from a fresh read every call.
- **One keep-alive connection per thread** (TASK-015) -- `MdTreeStore` sends every request through `_send` over a `threading.local` `http.client` connection instead of a `urlopen` handshake per call (urllib always sends `Connection: close`). Per *thread*, not per store: `llpm serve` shares one store per board across FastAPI's threadpool. An idle connection the server closed is detected before sending; a request dropped unanswered on a *reused* connection is resent once, on a fresh one never (a create or move could apply twice). `_send` is urlopen-shaped (URL or `Request` in, body out, `HTTPError` on non-2xx) and is the seam the store tests stub; the keep-alive tests run a real localhost server. No proxy support: urllib's `HTTPS_PROXY` handling went with it.
- **Blockers must be real ticket IDs** -- no free-text blockers
- **Edge vocabulary is deliberately small**: `blockers` (hard, intra-board IDs) · `waits_on` (cross-board vault stems; contributes to derived `blocked`; unknown/offline never blocks) · `after` (soft precedence; never blocks) · `serves` (epic/feature → goal-note stems, cross-repo, soft-validated)
- **Templates resolve store-first, then bundled** -- local-dir stores get copies on `init`; vault stores need no seeding (vault `templates.*` notes act as overrides when present)
- **Atomic file creation** (`os.O_EXCL`) prevents ID collisions across parallel agents
- **`set` cannot modify `status`, `blockers`, `serves`, `waits_on`, or `after`** -- use the dedicated `llpm status` / `blocker` / `serves` / `waits` / `after` commands
- **Provenance is system-written**: `origin: human|agent` + `created_by` resolve at create time (flags > `LLPM_ORIGIN`/`LLPM_CREATED_BY` env > inference); `commits: []` is harvested from ticket-ID mentions in the CWD git log at review/complete (plus explicit `--commit`); every mutation stamps `managed_by: llpm`. None of these are settable via `set`.
- **Goal is a frontmatter type, not a place** -- any note anywhere may carry `type: goal`; `llpm goals` type-scans the vault and rolls up progress from `serves:` chains (own + inherited via `parent`) on the current board. Only `status: stamped` goals bind planning; drafts render as proposals. Cross-repo aggregation across every board is out of scope for the CLI (marginalia's 5000-ft view) -- a single `llpm` invocation only ever sees its own board.
- **Ticket-intake policy (agent origin only)**: `llpm create` forces `status: draft` unless the ticket's `type` is on `.llpm/config.toml`'s `[intake] auto_approve` list. Goal attachment is never enforced at creation -- creation always succeeds; `--serves` (epics/features), a goal-serving `--parent`, and `--triage` (tags `triage`) are optional. `llpm orphans` / `llpm goals` are the pull-based report of agent-created tickets with no goal attachment, gated by `.llpm/config.toml`'s `[intake] require_goal` (`off`|`warn`|`enforce`, default `warn`) -- `off` mutes the report for boards that don't track goals, `warn` is informational only, `enforce` is the per-board opt-in for a *future* dispatcher to refuse orphaned agent tickets as ready work ("reconciler refuses to dispatch orphans," never "create fails" -- llpm has no dispatcher yet, so `enforce` only labels the report today). Human-origin tickets are never gated or flagged.
- **Dispatch readiness is a report, never a gate** (TASK-014) -- `parser.dispatch_problems(fm, body)` is the one predicate for "can a worker be handed this?": `no-ac` (`## Acceptance Criteria` for a task / `## Verification` for a feature is missing or still the template's `_placeholder_` hint), `no-effort`, `requires-human` (a finding, not a defect -- a human owns it), `no-tier`. Pure, per-ticket, no store: `llpm lint` reports it and `llpm next` (FEAT-009) filters the ready set on it being empty, so the two can't drift. `service.lint_tickets` is the board-level half (it needs `load_board` + effective status, which parser can't reach) -- default scope is the effectively-`open` ready set, named IDs override it, `--status all` is the whole board. Exit 0 even when it reports, `--strict` for scripts. Like `llpm orphans` it refuses nothing: `llpm status <ID> open` still succeeds on a flagged ticket, and a hard gate (if ever) belongs on `planned -> open` behind a `[dispatch]` switch. Cost: a board listing carries no bodies, so lint reads the body of each *candidate* and of nothing else.
- **Handoff convention (capture-then-promote, schema enforcement deferred)**: every worker run appends a `## Handoff` section to the ticket body -- `outcome: done|partial|failed`, what shipped, surprises, open questions, proposed follow-ups. Light-tier workers only append (never mutate other sections); heavy tiers may also revise other sections when the work warrants it. Proposed follow-ups are zero-ceremony list entries, not new tickets -- a reconciler/planner later promotes worthy ones to `origin: agent` draft tickets via the ticket-intake policy above. The `llpm-loop` skill (`llpm skills --show llpm-loop`) wraps this plus the full select-claim-work-park cycle for autonomous multi-ticket sessions; its step 1 is `llpm next` (FEAT-009).
- **`llpm next` selects, it never claims** (FEAT-009) -- `service.next_tickets(store, tier=, limit=)` is the scheduler primitive the autonomous loop (and a future dispatcher) calls: the effectively-`open` set minus anything `parser.dispatch_problems` flags, optionally narrowed to one `model_tier`, ordered priority high->low, then an `after:` tie-break (targets all complete/closed first), then ID. A total order, so the same board always answers the same way. The hard edges are honoured by *exclusion*, not by sorting -- an unresolved `blockers` entry or a blocking `waits_on` stem derives to `blocked`, which is not `open` -- which is what the design note's "topological over blockers + waits_on" amounts to once status is derived. It writes nothing: two callers get the same answer and claiming is still `llpm status <ID> in-progress`, still not CAS. `requires_human` is excluded because `requires-human` is one of lint's codes, not by a second rule; for the same reason the `--tier` filter's "or unset" half is unreachable while `no-tier` is a dispatch problem (kept, so the filter stays right if that relaxes). Cost mirrors `lint`: one board load, then one body read per candidate that survives the cheap status/tier filters. CLI `llpm next`, `GET /{repo}/next`, MCP `next_tickets` -- each exactly one service call.
- **Notes below a ticket are never tickets**: a ticket sits exactly one segment below its bucket (`repos.<repo>.llpm.tasks.TASK-001`); anything deeper belongs to that ticket -- the natural home for whatever accumulates around it, one child segment per kind: `…TASK-001.agent-workers.<worker_id>[.<child>…]` (a dispatched worker may spam its subtree freely so the ticket body stays short), `…TASK-001.human-review…`, and whatever kind comes next. llpm neither reads nor writes those notes; it only guarantees they don't leak: `list_tickets` skips them (the vault's fnmatch `*` crosses dots, so bucket globs return them, and listings paginate because they count against the page), `archive` carries them along, `delete` removes them (`TicketStore.subnotes`). The rule is structural -- no segment name is special-cased -- and the local-dir spelling is the dotted filename `<ID>.<…>.md` beside the ticket. The note schema + status semantics live with the reader (marginalia `docs/agent-workers.md`).
- **Service layer is the seam; the CLI, the HTTP API and MCP are all callers** -- `service.py` holds plain functions over a `TicketStore` that return dicts and raise typed errors (`NotFound`/`Invalid`/`Conflict`); `commands.py` renders them as text + exit codes, `api.py` as 404/422/409. Rules and the JSON shape live in exactly one place, so the two surfaces can't drift. FastAPI/uvicorn are the optional `llpm[api]` extra -- core stays pyyaml-only and nothing on the CLI path imports `api.py`, so every command works without them (`llpm serve` says so in one line if they're missing). `llpm serve` mounts the router standalone; marginalia mounts the same `make_router(store_for)` under `/api/llpm` rather than llpm becoming a third deployed service. `repo` is a path parameter, so one process serves every board. The whole FEAT-015 surface is served: reads (TASK-016), `status` (TASK-017), create + PATCH fields (TASK-018) and the four edge pairs -- blockers/after/waits/serves (TASK-019), each `POST`/`DELETE` answering the updated ticket. Provenance inference (`LLPM_ORIGIN`/`LLPM_CREATED_BY`), commit harvesting and the `[intake] auto_approve` list stay CLI-side: they read a shell and a checkout the server doesn't have, so an HTTP caller names `created_by` and an agent-origin create over HTTP always lands `draft`.
- **MCP is the agent-facing chokepoint** (FEAT-016) -- `mcp.py` publishes fourteen tools, one per service function (`list_tickets`, `next_tickets`, `get_ticket`, `create_ticket`, `set_status`, `set_fields`, and the four edge pairs), so a harness session that would otherwise write vault notes by hand goes through llpm's rules. Stdlib only (JSON-RPC 2.0 by hand: initialize / tools/list / tools/call / ping), because `llpm mcp` must run on the pyyaml-only install -- `api.py` mounts the same `Session` at `POST /{repo}/mcp` (streamable HTTP, JSON only, no SSE, no server-side session), so `llpm serve` is the remote option and no second server exists. The board is addressed by the transport (CWD for stdio, the path parameter for HTTP), never by a tool argument. A rule llpm refuses comes back as a tool result with `isError` carrying llpm's own sentence, not a JSON-RPC error -- protocol codes are for unknown methods/tools. Results are the service's lean returns, NOT the re-serialized ticket the REST endpoints answer with: over the network that ticket saves a round trip, over MCP it costs a whole-board load for `children` and the next call is free. Identity: stdio takes `--created-by`/`LLPM_CREATED_BY` and falls back to the client's handshake name, and `origin` defaults to `agent` (an MCP client is one); HTTP is stateless, so the caller names `created_by` in the arguments. Commit harvesting stays CLI-only -- this server never runs git, `set_status` takes the SHAs it is given.
- **Worklog convention**: every ticket body carries a `## Worklog` section -- append-only, format `**<date> <agent/session>** -- <text>` -- where working agents jot train-of-thought (hypotheses, dead ends, discovered constraints) as it happens, distinct from the summarizing `## Handoff` appended at park time. See `llpm skills --show llpm-loop` step 4.

## Development

- Python 3.12+, UV for dependency management
- Only external dep: PyYAML
- Tests use fixture data copied to tmp_path per test; mock `commands._today()` for deterministic dates
- `uv run llpm` works immediately during dev (entrypoint defined in pyproject.toml)
- `uv tool install --editable .` makes `llpm` globally available

## CLI Reference

```bash
llpm init                                # set up llpm/tickets/ and llpm/templates/
llpm create <type> "title" [options]     # new ticket (--origin/--created-by/--serves/--triage)
llpm list [--status X] [--type X]        # list active tickets
llpm board                               # kanban: blocked/open/in-progress/review
llpm backlog                             # planned + draft tickets
llpm show <ID>                           # full ticket details + body
llpm status <ID> <status> [--commit SHA] # change status (review/complete harvest commits[])
llpm set <ID> field=value [...]          # set simple fields
llpm blocker add <ID> --blocked-by <ID>  # add dependency
llpm blocker rm <ID> --blocked-by <ID>   # remove dependency
llpm blocker list <ID>                   # show blocker details
llpm waits add <ID> --on <stem>          # cross-board dep (full vault stem)
llpm waits rm <ID> --on <stem>           # remove cross-board dep
llpm waits list <ID>                     # cross-board deps + resolution state
llpm after add <ID> --after <ID>         # soft precedence (never blocks)
llpm after rm <ID> --after <ID>          # remove soft precedence
llpm serves add <ID> <goal-stem>         # goal ref on epic/feature
llpm serves rm <ID> <goal-stem>          # remove goal ref
llpm archive <ID> | --all [--yes]        # archive closed tickets
llpm delete <ID> [--yes]                 # delete with relationship cleanup
llpm goals [--json]                      # per-goal rollup from serves: chains; unplanned gaps
llpm orphans [--json]                    # agent-created tickets with no goal attachment
llpm lint [ID ...] [--status X] [--json] [--strict]  # dispatch-readiness: AC, effort, tier
llpm next [--tier X] [-n N] [--json]     # select the next ready ticket(s), deterministically
llpm todo --add "text" | --rm <id> | -l | -i  # TODO inbox
llpm mcp [--created-by ID] [--origin X]        # serve the board to an MCP client (stdio)
llpm serve [--host] [--port] [--vault URL]     # HTTP API + POST /<board>/mcp (llpm[api] extra)
llpm skills [--show <name>] [--install <name>] # bundled Claude skills
llpm help [--verbose]                    # full CLI reference
```

## Docs Root Resolution

`--docs-root` flag > `LLPM_DOCS_ROOT` env var > `.llpm/config.toml` pointer > `./llpm/` default

**This repo dogfoods the vault store**: its own board lives at `repos.llpm.llpm.*` in the
agent-memory vault (`.llpm/config.toml` → kind=mdtree). There is no local `llpm/` dir; run
`llpm board` from the repo root to see it.

## Claude Skills

LLPM ships with bundled Claude skills for common workflows:

- **llpm-init** -- Guides initial project setup: creates ticket structure, helps create initial tickets, and adds LLPM configuration (including agent role definitions) to the project's CLAUDE.md. Run `llpm skills --show llpm-init` to preview or `llpm skills --install llpm-init` to install as a slash command.
- **llpm-pm** -- Project Manager role: triages TODOs, creates epics/features, sets priorities and dependencies, reviews board state with the user.
- **llpm-planner** -- Planner role: researches the codebase, writes detailed specs for draft tickets, breaks features into tasks, surfaces open questions, marks tickets `planned`/`open` when ready.
- **llpm-worker** -- Worker role: claims open tickets, implements code, runs tests, commits, marks tickets `review` or `complete`. Pushes back unclear specs.
- **llpm-loop** -- Autonomous multi-ticket worker cycle: select next ready ticket -> claim -> work -> jot progress -> append `## Handoff` -> park (`review`, optionally `--awaiting`) -> repeat until the queue is dry or blocked. Wraps llpm-worker's single-ticket discipline in the rails needed for unattended looping; documents (doesn't fix) the claim-isn't-CAS race.
- **llpm-migrate-fd** -- Migrates from the old FD (Feature Design) system to LLPM frontmatter format.

List all available skills with `llpm skills`.
