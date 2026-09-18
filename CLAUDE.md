# LLPM

LLM Project Manager -- a CLI tool for stateless, markdown-based project management designed for LLM multi-agent workflows.

## Quick Start

```bash
uv sync                    # install deps
uv run llpm --help         # see all commands
uv run pytest -x -v        # run tests (928 tests)
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
- **Blockers must be real ticket IDs** -- no free-text blockers
- **Edge vocabulary is deliberately small**: `blockers` (hard, intra-board IDs) · `waits_on` (cross-board vault stems; contributes to derived `blocked`; unknown/offline never blocks) · `after` (soft precedence; never blocks) · `serves` (epic/feature → goal-note stems, cross-repo, soft-validated)
- **Templates resolve store-first, then bundled** -- local-dir stores get copies on `init`; vault stores need no seeding (vault `templates.*` notes act as overrides when present)
- **Atomic file creation** (`os.O_EXCL`) prevents ID collisions across parallel agents
- **`set` cannot modify `status`, `blockers`, `serves`, `waits_on`, or `after`** -- use the dedicated `llpm status` / `blocker` / `serves` / `waits` / `after` commands
- **Provenance is system-written**: `origin: human|agent` + `created_by` resolve at create time (flags > `LLPM_ORIGIN`/`LLPM_CREATED_BY` env > inference); `commits: []` is harvested from ticket-ID mentions in the CWD git log at review/complete (plus explicit `--commit`); every mutation stamps `managed_by: llpm`. None of these are settable via `set`.
- **Goal is a frontmatter type, not a place** -- any note anywhere may carry `type: goal`; `llpm goals` type-scans the vault and rolls up progress from `serves:` chains (own + inherited via `parent`) on the current board. Only `status: stamped` goals bind planning; drafts render as proposals. Cross-repo aggregation across every board is out of scope for the CLI (marginalia's 5000-ft view) -- a single `llpm` invocation only ever sees its own board.
- **Ticket-intake policy (agent origin only)**: `llpm create` forces `status: draft` unless the ticket's `type` is on `.llpm/config.toml`'s `[intake] auto_approve` list. Goal attachment is never enforced at creation -- creation always succeeds; `--serves` (epics/features), a goal-serving `--parent`, and `--triage` (tags `triage`) are optional. `llpm orphans` / `llpm goals` are the pull-based report of agent-created tickets with no goal attachment, gated by `.llpm/config.toml`'s `[intake] require_goal` (`off`|`warn`|`enforce`, default `warn`) -- `off` mutes the report for boards that don't track goals, `warn` is informational only, `enforce` is the per-board opt-in for a *future* dispatcher to refuse orphaned agent tickets as ready work ("reconciler refuses to dispatch orphans," never "create fails" -- llpm has no dispatcher yet, so `enforce` only labels the report today). Human-origin tickets are never gated or flagged.
- **Handoff convention (capture-then-promote, schema enforcement deferred)**: every worker run appends a `## Handoff` section to the ticket body -- `outcome: done|partial|failed`, what shipped, surprises, open questions, proposed follow-ups. Light-tier workers only append (never mutate other sections); heavy tiers may also revise other sections when the work warrants it. Proposed follow-ups are zero-ceremony list entries, not new tickets -- a reconciler/planner later promotes worthy ones to `origin: agent` draft tickets via the ticket-intake policy above. The `llpm-loop` skill (`llpm skills --show llpm-loop`) wraps this plus the full select-claim-work-park cycle for autonomous multi-ticket sessions; interim ready-ticket selection is manual (priority-aware `llpm list --status open --json`, blocked/draft excluded) until `llpm next` (FEAT-009) ships.
- **Notes below a ticket are never tickets**: a ticket sits exactly one segment below its bucket (`repos.<repo>.llpm.tasks.TASK-001`); anything deeper belongs to that ticket -- the natural home for whatever accumulates around it, one child segment per kind: `…TASK-001.agent-workers.<worker_id>[.<child>…]` (a dispatched worker may spam its subtree freely so the ticket body stays short), `…TASK-001.human-review…`, and whatever kind comes next. llpm neither reads nor writes those notes; it only guarantees they don't leak: `list_tickets` skips them (the vault's fnmatch `*` crosses dots, so bucket globs return them, and listings paginate because they count against the page), `archive` carries them along, `delete` removes them (`TicketStore.subnotes`). The rule is structural -- no segment name is special-cased -- and the local-dir spelling is the dotted filename `<ID>.<…>.md` beside the ticket. The note schema + status semantics live with the reader (marginalia `docs/agent-workers.md`).
- **Service layer is the seam, CLI and API are both callers** -- `service.py` holds plain functions over a `TicketStore` that return dicts and raise typed errors (`NotFound`/`Invalid`/`Conflict`); `commands.py` renders them as text + exit codes, `api.py` as 404/422/409. Rules and the JSON shape live in exactly one place, so the two surfaces can't drift. FastAPI/uvicorn are the optional `llpm[api]` extra -- core stays pyyaml-only and nothing on the CLI path imports `api.py`, so every command works without them (`llpm serve` says so in one line if they're missing). `llpm serve` mounts the router standalone; marginalia mounts the same `make_router(store_for)` under `/api/llpm` rather than llpm becoming a third deployed service. `repo` is a path parameter, so one process serves every board. The whole FEAT-015 surface is served: reads (TASK-016), `status` (TASK-017), create + PATCH fields (TASK-018) and the four edge pairs -- blockers/after/waits/serves (TASK-019), each `POST`/`DELETE` answering the updated ticket. Provenance inference (`LLPM_ORIGIN`/`LLPM_CREATED_BY`), commit harvesting and the `[intake] auto_approve` list stay CLI-side: they read a shell and a checkout the server doesn't have, so an HTTP caller names `created_by` and an agent-origin create over HTTP always lands `draft`.
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
llpm todo --add "text" | --rm <id> | -l | -i  # TODO inbox
llpm serve [--host] [--port] [--vault URL]     # HTTP API (needs the llpm[api] extra)
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
