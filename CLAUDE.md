# LLPM

LLM Project Manager -- a CLI tool for stateless, markdown-based project management designed for LLM multi-agent workflows.

## Quick Start

```bash
uv sync                    # install deps
uv run llpm --help         # see all commands
uv run pytest -x -v        # run tests (101 tests)
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
- **Templates live in the project** (`llpm/templates/`), copied from bundled on `init`
- **Atomic file creation** (`os.O_EXCL`) prevents ID collisions across parallel agents
- **`set` cannot modify `status` or `blockers`** -- use dedicated `llpm status` and `llpm blocker` commands

## Development

- Python 3.12+, UV for dependency management
- Only external dep: PyYAML
- Tests use fixture data copied to tmp_path per test; mock `commands._today()` for deterministic dates
- `uv run llpm` works immediately during dev (entrypoint defined in pyproject.toml)
- `uv tool install --editable .` makes `llpm` globally available

## CLI Reference

```bash
llpm init                                # set up llpm/tickets/ and llpm/templates/
llpm create <type> "title" [options]     # new ticket (epic/feature/task/research/custom)
llpm list [--status X] [--type X]        # list active tickets
llpm board                               # kanban: blocked/open/in-progress/review
llpm backlog                             # planned + draft tickets
llpm show <ID>                           # full ticket details + body
llpm status <ID> <status>                # change status
llpm set <ID> field=value [...]          # set simple fields
llpm blocker add <ID> --blocked-by <ID>  # add dependency
llpm blocker rm <ID> --blocked-by <ID>   # remove dependency
llpm blocker list <ID>                   # show blocker details
llpm archive <ID> | --all [--yes]        # archive closed tickets
llpm delete <ID> [--yes]                 # delete with relationship cleanup
llpm todo --add "text" | --rm <id> | -l | -i  # TODO inbox
llpm skills [--show <name>] [--install <name>] # bundled Claude skills
llpm help [--verbose]                    # full CLI reference
```

## Docs Root Resolution

`--docs-root` flag > `LLPM_DOCS_ROOT` env var > `./llpm/` default

## Claude Skills

LLPM ships with bundled Claude skills for common workflows:

- **llpm-init** -- Guides initial project setup: creates ticket structure, helps create initial tickets, and adds LLPM configuration (including agent role definitions) to the project's CLAUDE.md. Run `llpm skills --show llpm-init` to preview or `llpm skills --install llpm-init` to install as a slash command.
- **llpm-pm** -- Project Manager role: triages TODOs, creates epics/features, sets priorities and dependencies, reviews board state with the user.
- **llpm-planner** -- Planner role: researches the codebase, writes detailed specs for draft tickets, breaks features into tasks, surfaces open questions, marks tickets `planned`/`open` when ready.
- **llpm-worker** -- Worker role: claims open tickets, implements code, runs tests, commits, marks tickets `review` or `complete`. Pushes back unclear specs.
- **llpm-migrate-fd** -- Migrates from the old FD (Feature Design) system to LLPM frontmatter format.

List all available skills with `llpm skills`.
