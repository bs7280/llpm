# LLPM

LLM Project Manager -- a CLI tool for stateless, markdown-based project management designed for LLM multi-agent workflows.

## Quick Start

```bash
uv sync                    # install deps
uv run llpm --help         # see all commands
uv run pytest -x -v        # run tests (412 tests)
```

## Project Structure

```
src/llpm/
    __main__.py      # CLI entry point (argparse + dispatch)
    parser.py        # Frontmatter parsing, validation, ticket discovery
    commands.py      # All command implementations
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
- **Blockers must be real ticket IDs** -- no free-text blockers
- **Edge vocabulary is deliberately small**: `blockers` (hard, intra-board IDs) · `waits_on` (cross-board vault stems; contributes to derived `blocked`; unknown/offline never blocks) · `after` (soft precedence; never blocks) · `serves` (epic/feature → goal-note stems, cross-repo, soft-validated)
- **Templates resolve store-first, then bundled** -- local-dir stores get copies on `init`; vault stores need no seeding (vault `templates.*` notes act as overrides when present)
- **Atomic file creation** (`os.O_EXCL`) prevents ID collisions across parallel agents
- **`set` cannot modify `status`, `blockers`, `serves`, `waits_on`, or `after`** -- use the dedicated `llpm status` / `blocker` / `serves` / `waits` / `after` commands
- **Provenance is system-written**: `origin: human|agent` + `created_by` resolve at create time (flags > `LLPM_ORIGIN`/`LLPM_CREATED_BY` env > inference); `commits: []` is harvested from ticket-ID mentions in the CWD git log at review/complete (plus explicit `--commit`); every mutation stamps `managed_by: llpm`. None of these are settable via `set`.
- **Goal is a frontmatter type, not a place** -- any note anywhere may carry `type: goal`; `llpm goals` type-scans the vault and rolls up progress from `serves:` chains (own + inherited via `parent`) on the current board. Only `status: stamped` goals bind planning; drafts render as proposals. Cross-repo aggregation across every board is out of scope for the CLI (marginalia's 5000-ft view) -- a single `llpm` invocation only ever sees its own board.
- **Ticket-intake policy (agent origin only)**: `llpm create` forces `status: draft` unless the ticket's `type` is on `.llpm/config.toml`'s `[intake] auto_approve` list. Goal attachment is never enforced at creation -- creation always succeeds; `--serves` (epics/features), a goal-serving `--parent`, and `--triage` (tags `triage`) are optional. `llpm orphans` / `llpm goals` are the pull-based report of agent-created tickets with no goal attachment, gated by `.llpm/config.toml`'s `[intake] require_goal` (`off`|`warn`|`enforce`, default `warn`) -- `off` mutes the report for boards that don't track goals, `warn` is informational only, `enforce` is the per-board opt-in for a *future* dispatcher to refuse orphaned agent tickets as ready work ("reconciler refuses to dispatch orphans," never "create fails" -- llpm has no dispatcher yet, so `enforce` only labels the report today). Human-origin tickets are never gated or flagged.
- **Handoff convention (capture-then-promote, schema enforcement deferred)**: every worker run appends a `## Handoff` section to the ticket body -- `outcome: done|partial|failed`, what shipped, surprises, open questions, proposed follow-ups. Light-tier workers only append (never mutate other sections); heavy tiers may also revise other sections when the work warrants it. Proposed follow-ups are zero-ceremony list entries, not new tickets -- a reconciler/planner later promotes worthy ones to `origin: agent` draft tickets via the ticket-intake policy above. The `llpm-loop` skill (`llpm skills --show llpm-loop`) wraps this plus the full select-claim-work-park cycle for autonomous multi-ticket sessions; interim ready-ticket selection is manual (priority-aware `llpm list --status open --json`, blocked/draft excluded) until `llpm next` (FEAT-009) ships.

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
