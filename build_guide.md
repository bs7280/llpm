# LLPM Build Guide

This document describes how to build LLPM from zero. It captures the design decisions, implementation order, and rationale so the project can be faithfully reconstructed.

## What is LLPM?

LLPM (LLM Project Manager) is a CLI tool for stateless, markdown-based project management designed for LLM multi-agent workflows. All state lives in markdown files with YAML frontmatter. The CLI is the structured gateway for frontmatter -- agents and humans edit markdown bodies directly.

**Core principle**: The CLI handles ID generation, status validation, blocker resolution, and date tracking so that agents don't have to manually parse/write YAML frontmatter.

## Prerequisites

- Python 3.12+
- UV for dependency management
- PyYAML (only external dependency)
- pytest (dev dependency)

## Architecture Overview

```
src/llpm/
    __init__.py
    __main__.py          # CLI entry point -- argparse setup, dispatch
    parser.py            # Frontmatter read/write/validate, ticket discovery
    commands.py          # All command implementations
    templates/           # Bundled defaults, copied to project on init
        __init__.py
        epic.md
        feature.md
        task.md
        research.md
tests/
    conftest.py          # docs_root fixture (copies fixtures to tmp_path)
    test_parser.py
    test_commands.py
    fixtures/docs/
        tickets/
            EPIC-001_CLI_TOOLING.md
            FEAT-001_EXPANDED_FRONTMATTER.md
            FEAT-002_DOC_PARSING.md
            TASK-001_ADD_PYYAML.md
            RESEARCH-001_YAML_LIBRARIES.md
            archive/
                FEAT-000_INITIAL_SETUP.md
        templates/
            epic.md
            feature.md
            task.md
            research.md
```

## Phase 1: Project Setup

### 1.1 Scaffold with UV

```bash
mkdir llpm && cd llpm
uv init
```

### 1.2 pyproject.toml

```toml
[project]
name = "llpm"
version = "0.1.0"
description = "Markdown-based project management CLI for LLM multi-agent workflows"
requires-python = ">=3.12"
dependencies = ["pyyaml>=6.0"]

[project.scripts]
llpm = "llpm.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/llpm"]

[dependency-groups]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

### 1.3 Install dependencies

```bash
uv sync
```

## Phase 2: Design the Frontmatter Schema

All doc types share a set of **core fields**. Some types have additional **extended fields**. Unknown/extra fields in frontmatter are preserved but not validated by the CLI.

### Core fields (required on ALL ticket types, including custom ones):

```yaml
---
id: "FEAT-001"
type: feature          # epic | feature | task | research (or custom)
title: "Human-readable title"
status: draft          # draft | planned | open | in-progress | review | complete | closed | deferred
priority: medium       # low | medium | high
parent: null           # ticket ID or null
blockers: []           # list of ticket IDs (must be valid, existing tickets)
created: "2026-03-18"  # YYYY-MM-DD
updated: "2026-03-18"
completed: null        # set automatically when status -> complete
tags: []               # list of strings
---
```

### Extended fields (type-specific):

```yaml
# On task, feature, research (NOT epic):
effort: null           # trivial | small | medium | large | xlarge (optional, nullable)

# On task:
requires_human: false  # true if a human must perform this action (not an agent)
```

### Key decisions:

- **YAML frontmatter** (not bold-text key-value pairs) -- machine-readable, parseable by PyYAML
- **`blocked` is derived, not stored** -- computed by checking if any blockers are in a non-resolved state. Completing a blocker automatically unblocks downstream tickets.
- **`children` is derived, not stored** -- computed at read time by scanning all tickets for `parent: X`. Single source of truth via the `parent` field.
- **`draft` vs `open`** -- creating without a body gives `draft` (lazy creation), with a body gives `open` (spec'd). This is a creation-time convenience, not a body-presence check. Status can always be changed manually.
- **`planned` status** -- for tickets that are spec'd but not yet approved for work.
- **`review` status** -- worker thinks it's done, awaiting verification before `complete`.
- **`requires_human`** -- agents see this flag and know to surface the task to the user rather than attempting it themselves. Use case: getting API keys, setting up accounts, manual approvals.
- **Each type has its own ID counter** -- FEAT-003 and TASK-003 coexist. IDs never reuse.
- **ID prefix derived from type** -- built-in: `EPIC`, `FEAT`, `TASK`, `RESEARCH`. Custom types: uppercase of type name (e.g., type `bug` -> `BUG-001`).
- **`RESOLVED_STATUSES`** = {complete, closed}. Only these resolve blockers.
- **Blockers must be valid ticket IDs** -- no free-text. For external dependencies, create a task (with `requires_human: true` if needed) and block on that.
- **Effort is complexity/risk, not time** -- `trivial | small | medium | large | xlarge`. Optional, nullable. Not applicable to epics (their effort is the aggregate of children).

## Phase 3: Templates

Templates are bundled with the package but **copied to the project's `docs/templates/` on `init`**. The CLI always reads templates from `docs/templates/`, never from the bundled copy directly. If a template is missing, the CLI errors (no silent fallback).

This means:
- Users can edit templates per-project (customize body sections, add fields)
- Custom types are supported: drop a `docs/templates/<type>.md` with core fields
- A future `repair` command could re-copy bundled templates if needed

### Template body sections by type:

| Type | Sections |
|------|----------|
| Feature (FEAT) | Problem, Solution, Files to Create/Modify, Verification, Related |
| Epic (EPIC) | Objective, Scope, Success Criteria, Breakdown, Dependencies, Related |
| Task (TASK) | Description, Acceptance Criteria, Notes |
| Research (RESEARCH) | Hypothesis, Methodology, Findings, Conclusion, Related |

### Template status line includes inline comments:

```yaml
status: draft  # draft | planned | open | in-progress | review | complete | closed | deferred (blocked is derived)
```

This is why `create` uses **string substitution** on the raw template text (not YAML parse/write) -- to preserve these helpful comments in newly created tickets.

## Phase 4: Build parser.py

This is the foundation module. Build and test it before anything else.

### Functions to implement:

1. **`parse_document(path) -> (dict, str)`** -- Split on `---` delimiters, `yaml.safe_load` the YAML portion, return (frontmatter, body). Normalize `datetime.date` objects to strings (PyYAML parses quoted dates as date objects).

2. **`write_document(path, frontmatter, body)`** -- `yaml.safe_dump` with `sort_keys=False`, reassemble as `---\n{yaml}---\n{body}`.

3. **`validate_frontmatter(data) -> list[str]`** -- Check required core fields exist, validate enum values, check ID prefix matches type. Extended fields validated only if present.

4. **`find_tickets(docs_root, include_archive=True) -> list[Path]`** -- Glob `tickets/*.md` and optionally `tickets/archive/*.md`.

5. **`find_ticket_by_id(docs_root, ticket_id) -> Path | None`** -- Scan filenames for prefix match (case-insensitive).

6. **`load_all_tickets(docs_root) -> list[(Path, dict, str)]`** -- Parse all tickets, skip invalid files.

7. **`next_id(docs_root, ticket_type) -> str`** -- Scan all tickets (including archive), find highest number for the type prefix, return next. Zero-pad to 3 digits.

8. **`is_blocked(docs_root, frontmatter) -> bool`** -- Check if any blockers reference tickets in non-resolved status.

9. **`get_blocker_details(docs_root, frontmatter) -> list[dict]`** -- For each blocker, return its resolution status and title.

10. **`effective_status(docs_root, frontmatter) -> str`** -- Return stored status unless the ticket has unresolved blockers (then return "blocked"). Don't override complete/closed/deferred.

11. **`get_children(docs_root, ticket_id) -> list[dict]`** -- Scan all tickets for `parent: ticket_id`, return their frontmatter. Derived at read time.

### PyYAML pitfalls to handle:

- Dates: `yaml.safe_load` parses `"2026-03-18"` as `datetime.date` -- normalize to string
- null/None: roundtrips correctly
- Comments: stripped on parse/write -- this is why `create` uses string substitution
- Flow-style lists: `yaml.dump` with `default_flow_style=False` outputs block lists -- accept the style change

### Test this module thoroughly before moving on. Cover:

- Parse/roundtrip feature, epic, task, research
- Date normalization (string vs datetime.date)
- Null dates
- Body preservation
- Error cases (no frontmatter, unterminated frontmatter)
- Validation (missing fields, invalid enums, ID prefix mismatch)
- Ticket discovery (find all, exclude archive, empty dir)
- Find by ID (existing, case-insensitive, archived, not found)
- Next ID (each type, first-of-type)
- Load all tickets
- Derived children

## Phase 5: Build commands.py

Each command is a function that takes the argparse `args` namespace.

### Commands to implement (in order):

1. **`cmd_init`** -- Create `docs/tickets/`, `docs/tickets/archive/`, copy bundled templates to `docs/templates/`. Check if already initialized. Does NOT create FEATURE_INDEX.md (dropped).

2. **`cmd_list`** -- Load all tickets, compute `effective_status` for each, filter by --status/--type/--parent, print table. Status filter uses derived status (so `--status blocked` works).

3. **`cmd_board`** -- Kanban view: group tickets by effective status into columns (BLOCKED, OPEN, IN-PROGRESS, REVIEW). Show priority indicators (`!!!` high, ` ! ` medium). Exclude draft/planned/complete/closed/deferred.

4. **`cmd_backlog`** -- Show PLANNED and DRAFT sections. This is where planning agents look for work.

5. **`cmd_show`** -- Print all frontmatter fields with effective status. Show derived children. If ticket has blockers, show each with [RESOLVED] or [BLOCKING] tag. Print full markdown body.

6. **`cmd_create`** -- The most complex command:
   - Get next ID via `next_id()`
   - Slugify title to UPPER_SNAKE_CASE
   - Read template from `docs/templates/<type>.md` (error if missing)
   - **String substitution** on frontmatter fields (preserves YAML comments)
   - Handle body input: `--body` (inline string), `--body-file` (path), stdin pipe, or none (draft)
   - If body provided: replace template body, set status to `open`
   - If no body: keep template body, status stays `draft`
   - Handle `--parent`, `--priority`, `--effort`, `--tags`, `--requires-human`
   - **Atomic file creation** using `os.O_CREAT | os.O_EXCL` to prevent ID collisions across parallel agents. Retry with next ID on failure (up to 3 times).

7. **`cmd_status`** -- Parse ticket, update status field, set `updated` date. If completing, set `completed` date.

8. **`cmd_blocker_add`** -- Add a blocker to a ticket's blockers list. Uses `--blocked-by` flag for explicit direction. **Validates that the blocker ticket ID exists** -- errors on non-existent IDs.

9. **`cmd_blocker_rm`** -- Remove a blocker. Used for correcting mistakes.

10. **`cmd_blocker_list`** -- Show each blocker with resolution status and overall blocked/unblocked summary.

11. **`cmd_set`** -- Frontmatter field setter for "simple" fields only. Supports `field=value` pairs for batch updates. Validates enums, handles list fields (comma-separated), handles null/none. **Cannot set**: `id`, `type`, `created`, `completed`, `updated` (managed automatically), `status` (use `llpm status`), `blockers` (use `llpm blocker`).

12. **`cmd_archive`** -- Move ticket to `tickets/archive/`. Only allows `complete` or `closed` tickets. Supports `--all` to find and archive all closed non-archived tickets (with confirmation prompt unless `--yes`).

13. **`cmd_delete`** -- Delete a ticket file. Requires confirmation (unless `--yes`). Before deleting: warns about and cleans up relationships -- removes the ticket from other tickets' `blockers` lists, warns about orphaned children.

14. **`cmd_todo`** -- TODO inbox with stable IDs. Uses explicit flags:
    - `--add "text"` -- append a todo
    - `--rm <id>` -- remove by stable ID
    - `--list` / `-l` -- show all todos
    - `--interactive` / `-i` -- REPL mode for rapid-fire entry (each line appended, ctrl-d or empty line exits)
    - Bare `llpm todo` (no flags) shows help
    - IDs never reuse (max existing + 1)
    - Format: `- (1) text` per line in TODO.md

### Key design choices in commands:

- **`_read_body(args)`** helper handles all body input modes (--body, --body-file, stdin, none). Wraps `sys.stdin.isatty()` in try/except for pytest compatibility.
- **`_today()`** helper for mockable date. Tests mock this for deterministic output.
- **`_slugify(title)`** -- strip non-alphanumeric, UPPER_SNAKE_CASE.
- **Atomic file creation** in `cmd_create` via `os.O_CREAT | os.O_EXCL` -- prevents ID collisions across parallel agents without file locking.

## Phase 6: Build __main__.py

Thin argparse setup and dispatch.

### UTF-8 output safety

```python
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

Safety net for non-UTF-8 terminals. The tool itself does not emit emoji or decorative unicode.

### Docs root resolution

Priority order: `--docs-root` flag > `LLPM_DOCS_ROOT` env var > `./docs/` default. Always resolved to an absolute `Path` internally.

```python
def resolve_docs_root(args):
    if args.docs_root:
        return Path(args.docs_root).resolve()
    env = os.environ.get("LLPM_DOCS_ROOT")
    if env:
        return Path(env).resolve()
    return Path("docs").resolve()
```

### Help messages must be LLM-readable

Every command and flag has a detailed `description` and `help` string. An LLM running `llpm help` (or `llpm <command> --help`) should understand the full system without needing external docs.

### The `help` command

`llpm help` iterates all subparsers and prints every command's help in one shot. `llpm help --verbose` includes full descriptions and examples. This gives an LLM complete context in a single CLI call.

### The `blocker` command

Uses proper subparsers (add/rm/list) since the semantics are different enough. The `--blocked-by` flag makes dependency direction explicit -- critical for LLMs that might otherwise confuse "A blocks B" with "A is blocked by B".

## Phase 7: Test Fixtures

Create `tests/fixtures/docs/` with a realistic interconnected ticket tree:

- EPIC-001 (in-progress) -- parent of FEAT-001, FEAT-002, TASK-001
- FEAT-001 (complete) -- child of EPIC-001
- FEAT-002 (in-progress) -- child of EPIC-001, parent of TASK-001 and RESEARCH-001
- TASK-001 (open, blockers: [FEAT-001, FEAT-002]) -- has one resolved and one unresolved blocker
- RESEARCH-001 (complete) -- child of FEAT-002
- FEAT-000 (complete, in archive/) -- tests archive handling

Also include `tests/fixtures/docs/templates/` with copies of all built-in templates.

The `conftest.py` fixture copies this tree to `tmp_path` for each test so writes are non-destructive.

## Phase 8: Write Tests

### test_parser.py
- Parse/roundtrip, validation, discovery, next_id, load_all, derived children

### test_commands.py
- TestList: all, filter by status/type/parent, derived blocked, empty
- TestShow: normal, blocked ticket with blocker details, derived children, not found
- TestCreate: draft (no body), with body, body-file, parent, priority, tags, effort, requires-human, template comments, first-of-type, atomic ID collision
- TestStatus: update, complete sets date, not found, cannot set blocked
- TestBlocker: add, duplicate, rm, not found, list, list empty, resolved when complete, non-existent ID rejected
- TestSet: equals syntax, multiple fields, tags, parent, null, invalid field/enum, cannot set status/blockers/id, title
- TestArchive: single ticket, --all, non-closed rejected, already archived
- TestDelete: with confirmation, relationship cleanup, not found
- TestInit: fresh (copies templates), already exists
- TestTodo: --add, IDs increment, IDs never reuse, --list, -l, empty, --rm by ID, --rm not found, --interactive, bare shows help

### Testing patterns:
- Mock `_today()` for deterministic dates
- Use `capsys` for output assertions
- `pytest.raises(SystemExit)` for error cases
- The `docs_root` fixture provides a fresh copy of fixtures per test

## Phase 9: Distribution

### Editable install for development:

```bash
uv tool install --editable /path/to/this/repo
```

This makes `llpm` globally available while source changes take effect immediately.

### For other users (future):

```bash
uv tool install llpm    # after publishing to PyPI
pipx install llpm       # alternative
```

### How it works across projects:

`llpm` uses `--docs-root` (default: `docs/`) relative to cwd, overridable via `LLPM_DOCS_ROOT` env var. Each project gets its own `docs/` directory. No global config, no project registration. You `cd` to the project and `llpm` just works.

## CLI Usage Reference and Examples

### llpm init

```bash
llpm init
# -> Initialized llpm in /path/to/docs
# ->   /path/to/docs/tickets
# ->   /path/to/docs/tickets/archive
# ->   /path/to/docs/templates/epic.md
# ->   /path/to/docs/templates/feature.md
# ->   /path/to/docs/templates/task.md
# ->   /path/to/docs/templates/research.md

# Safe to re-run:
llpm init
# -> Already initialized: /path/to/docs. Run 'llpm list' to see tickets.

# Custom docs location:
llpm init --docs-root pm/
```

**Nuance**: Only checks for `docs/tickets/` existence. Copies bundled templates to `docs/templates/` if they don't already exist (won't overwrite customized templates).

### llpm create

```bash
# Draft (no body) -- status is "draft", template body preserved as placeholder
llpm create feature "User authentication"
# -> Created FEAT-001: User authentication
# -> File: /path/to/docs/tickets/FEAT-001_USER_AUTHENTICATION.md

# With body (inline) -- status is "open"
llpm create feature "Auth system" --body "## Problem\n\nUsers cannot log in."

# With body from file
llpm create feature "Auth system" --body-file specs/auth_spec.md

# Piped body
cat specs/auth_spec.md | llpm create feature "Auth system"

# All frontmatter options
llpm create task "Add JWT library" \
    --parent FEAT-001 \
    --priority high \
    --effort small \
    --tags "auth,deps" \
    --requires-human

# Each type uses its template from docs/templates/:
llpm create epic "Auth overhaul"
llpm create feature "Login flow"
llpm create task "Install jsonwebtoken"
llpm create research "Compare JWT libs"

# Custom types work if template exists:
llpm create bug "Login 500 error"  # requires docs/templates/bug.md
```

**Nuances**:
- Title is slugified to UPPER_SNAKE_CASE for filename
- Each type has its own counter: FEAT-001 and TASK-001 coexist
- IDs scan ALL tickets including archive -- if FEAT-005 was archived, next feature is FEAT-006
- Body replaces the template body; frontmatter comments are preserved (string substitution)
- When body is provided, status is `open`. Without body, stays `draft`.
- `--tags` takes comma-separated values
- `--parent` validates that the parent ticket exists
- File creation is atomic (`O_EXCL`) to prevent ID collisions across parallel agents

### llpm list

```bash
# All active tickets (excludes archived)
llpm list

# Filter by effective status (including derived "blocked")
llpm list --status blocked
llpm list --status open

# Filter by type
llpm list --type task

# Filter by parent (show children of a ticket)
llpm list --parent EPIC-001

# Combine filters
llpm list --status open --type task
```

**Nuances**:
- Status shown is the **effective** status -- a ticket stored as `open` but with unresolved blockers shows as `blocked`
- `--status blocked` dynamically matches tickets with unresolved blockers
- Archived tickets are excluded
- `--parent` is case-insensitive

### llpm board

```bash
llpm board
# -> -- BLOCKED (1) --
# ->    !  TASK-001       Add PyYAML dependency
# ->
# -> -- OPEN (0) --
# ->   (empty)
# ->
# -> -- IN-PROGRESS (2) --
# ->   !!! EPIC-001       CLI Tooling
# ->   !!! FEAT-002       Doc Parsing Library
# ->
# -> -- REVIEW (0) --
# ->   (empty)
```

**Nuances**:
- Only 4 columns: BLOCKED, OPEN, IN-PROGRESS, REVIEW
- Draft, planned, complete, closed, deferred excluded -- use `llpm backlog`
- Priority indicators: `!!!` = high, ` ! ` = medium, `   ` = low
- Uses effective (derived) status

### llpm backlog

```bash
llpm backlog
# -> -- PLANNED (2) --
# ->   ID             Type       Priority   Title
# ->   --------------------------------------------------
# ->   FEAT-003       feature    high       Rate Limiting
# ->   TASK-005       task       medium     Update Deps
# ->
# -> -- DRAFT (1) --
# ->   ID             Type       Priority   Title
# ->   --------------------------------------------------
# ->   FEAT-004       feature    medium     Caching Layer
```

### llpm show

```bash
llpm show FEAT-001
# -> ID:        FEAT-001
# -> Type:      feature
# -> Title:     Expanded Frontmatter
# -> Status:    complete
# -> Priority:  high
# -> Effort:    medium
# -> Parent:    EPIC-001
# -> Children:  TASK-002, TASK-003 (derived)
# -> Blockers:  -
# -> Created:   2026-03-15
# -> Updated:   2026-03-18
# -> Completed: 2026-03-18
# -> Tags:      templates, core
# -> File:      /path/to/docs/tickets/FEAT-001_EXPANDED_FRONTMATTER.md
# ->
# -> ## Problem
# -> ...

# Blocked ticket shows blocker details:
llpm show TASK-001
# -> ...
# -> Status:    blocked
# -> Blockers:  FEAT-001 (complete) [RESOLVED], FEAT-002 (in-progress) [BLOCKING]
```

**Nuances**:
- Status is effective (derived)
- Children are derived by scanning for `parent: <this-id>`
- Blockers show `[RESOLVED]` or `[BLOCKING]`
- File path is absolute
- Ticket ID lookup is case-insensitive

### llpm status

```bash
llpm status FEAT-001 in-progress
# -> FEAT-001: open -> in-progress

llpm status FEAT-001 complete
# -> FEAT-001: in-progress -> complete
# (sets completed and updated dates)

# Cannot set blocked (it's derived)
llpm status FEAT-001 blocked
# -> error: argument new_status: invalid choice: 'blocked'
```

### llpm set

```bash
# Single field
llpm set FEAT-001 priority=high
# -> FEAT-001: priority = high (was medium)

# Multiple fields
llpm set FEAT-001 priority=high effort=large tags=auth,core
# -> FEAT-001: priority = high (was medium)
# -> FEAT-001: effort = large (was medium)
# -> FEAT-001: tags = ['auth', 'core'] (was ['templates'])

# Clear a field
llpm set FEAT-001 parent=null

# Cannot set status or blockers (use dedicated commands)
llpm set FEAT-001 status=open
# -> Error: Cannot set 'status' via 'set'. Use 'llpm status'.

llpm set FEAT-001 blockers=FEAT-002
# -> Error: Cannot set 'blockers' via 'set'. Use 'llpm blocker'.
```

**Nuances**:
- Settable fields: `priority`, `effort`, `parent`, `tags`, `title`, `requires_human`
- NOT settable: `id`, `type`, `created`, `updated`, `completed` (automatic), `status` (use `llpm status`), `blockers` (use `llpm blocker`)
- List fields: comma-separated input
- `null` or `none` clears a field
- All validations run before any writes (atomic)
- Shows old value for confirmation

### llpm blocker

```bash
# Add -- direction is explicit
llpm blocker add TASK-001 --blocked-by FEAT-002
# -> TASK-001: now blocked by 'FEAT-002'

# Non-existent blocker is rejected
llpm blocker add TASK-001 --blocked-by FAKE-999
# -> Error: ticket 'FAKE-999' not found.

# Duplicate detection
llpm blocker add TASK-001 --blocked-by FEAT-002
# -> TASK-001: already blocked by 'FEAT-002'.

# Show blocker details
llpm blocker list TASK-001
# -> TASK-001: Add PyYAML dependency
# ->
# -> Blockers:
# ->   FEAT-001       complete       Expanded Frontmatter         [RESOLVED]
# ->   FEAT-002       in-progress    Doc Parsing Library           [BLOCKING]
# ->
# -> Status: BLOCKED (1 unresolved)

# Remove -- for mistakes only
llpm blocker rm TASK-001 --blocked-by FEAT-002
# -> TASK-001: removed blocker 'FEAT-002'
```

**Nuances**:
- `--blocked-by` makes direction unambiguous
- Blockers must be valid, existing ticket IDs -- no free-text
- When a blocker is completed, it auto-resolves (stays in the list as [RESOLVED])
- `blocker rm` is for correcting mistakes, not resolving dependencies

### llpm archive

```bash
# Archive a single completed ticket
llpm archive FEAT-001
# -> Archived FEAT-001 -> tickets/archive/FEAT-001_EXPANDED_FRONTMATTER.md

# Only complete/closed tickets can be archived
llpm archive TASK-001
# -> Error: TASK-001 is 'in-progress'. Only complete or closed tickets can be archived.

# Archive all closed tickets at once
llpm archive --all
# -> Found 3 closed tickets to archive:
# ->   FEAT-001  Expanded Frontmatter
# ->   FEAT-003  Rate Limiting
# ->   TASK-002  Fix Login Bug
# -> Archive all? [y/N]: y
# -> Archived 3 tickets.

# Skip confirmation
llpm archive --all --yes
```

### llpm delete

```bash
llpm delete TASK-001
# -> TASK-001: Add PyYAML dependency
# ->
# -> WARNING: This ticket is referenced by:
# ->   - FEAT-002 blockers list
# ->   - TASK-003 has this as parent
# ->
# -> Deleting will:
# ->   - Remove TASK-001 from FEAT-002's blockers
# ->   - Orphan TASK-003 (parent will become null)
# ->
# -> Delete? [y/N]: y
# -> Deleted TASK-001.

# Skip confirmation
llpm delete TASK-001 --yes
```

### llpm todo

```bash
# Add a todo
llpm todo --add "auth endpoint returns 500 when token expired"
# -> (1) auth endpoint returns 500 when token expired

llpm todo --add "maybe use redis for session cache?"
# -> (2) maybe use redis for session cache?

# List all
llpm todo --list
llpm todo -l
# -> TODO (2 items):
# ->   (1) auth endpoint returns 500 when token expired
# ->   (2) maybe use redis for session cache?

# Remove by stable ID
llpm todo --rm 1
# -> Removed (1): auth endpoint returns 500 when token expired

# IDs never reuse
llpm todo --add "rate limiting requirements"
# -> (3) rate limiting requirements

# REPL mode for rapid entry
llpm todo --interactive
llpm todo -i
# -> TODO REPL (empty line or ctrl-d to exit):
# -> > fix navbar z-index
# ->   (4) fix navbar z-index
# -> > button hover state missing
# ->   (5) button hover state missing
# -> >
# -> Added 2 items.

# No flags shows help
llpm todo
# -> (shows --help)
```

**Nuances**:
- Stable IDs, never reused
- `TODO.md` format: `- (1) text here` per line
- REPL mode is for human rapid-fire entry during testing sessions
- Agents should read TODO, triage into tickets, then `--rm`

### llpm help

```bash
llpm help
# -> (prints main help + summary of all commands)

llpm help --verbose
# -> (prints every command's full help, descriptions, examples)
```

### --docs-root (global flag)

```bash
# Default: ./docs/ relative to cwd (or LLPM_DOCS_ROOT env var)
llpm list

# Explicit path
llpm --docs-root tests/fixtures/docs list
llpm --docs-root /path/to/other/project/docs board

# Must come BEFORE the subcommand
llpm --docs-root pm/ init    # correct
llpm init --docs-root pm/    # WRONG -- argparse won't parse it

# Environment variable
export LLPM_DOCS_ROOT=~/code/myapp/docs
llpm list  # uses ~/code/myapp/docs
```

## Full Workflow Example

```bash
# Human initializes project
llpm init
llpm todo --add "need user auth with JWT"
llpm todo --add "rate limiting on API endpoints"
llpm todo --add "fix 500 error on /api/users"

# PM agent triages TODO
llpm todo -l                                     # read inbox
llpm create epic "Authentication System" --priority high --body "## Objective..."
llpm create feature "JWT Auth Flow" --parent EPIC-001 --priority high --body "## Problem..."
llpm create feature "Rate Limiting" --priority medium   # draft -- needs spec
llpm create task "Fix /api/users 500" --priority high --body "## Description..."
llpm todo --rm 1                                 # triaged
llpm todo --rm 2
llpm todo --rm 3
llpm blocker add FEAT-001 --blocked-by TASK-001  # auth needs the bug fix first

# Planning agent specs out the draft
llpm backlog                                     # sees FEAT-002 in DRAFT
llpm show FEAT-002                               # reads the stub
# (agent edits docs/tickets/FEAT-002_RATE_LIMITING.md body directly)
llpm status FEAT-002 planned                     # spec done

# Human reviews planned tickets
llpm backlog                                     # sees FEAT-002 in PLANNED
llpm show FEAT-002                               # reads the spec
llpm status FEAT-002 open                        # approved for work

# Worker agent picks up work
llpm board                                       # sees TASK-001 in OPEN
llpm show TASK-001                               # reads the spec
llpm status TASK-001 in-progress                 # claiming it
# (agent implements the fix)
llpm status TASK-001 review                      # done, needs verification

# Review agent checks the work
llpm list --status review                        # sees TASK-001
llpm show TASK-001                               # reads spec + checks implementation
llpm status TASK-001 complete                    # verified

# FEAT-001 auto-unblocks now that TASK-001 is complete
llpm board                                       # FEAT-001 now in OPEN, not BLOCKED
```

## Design Principles

1. **Stateless** -- All state is in markdown files. No database, no server, no config files beyond templates.
2. **CLI is the frontmatter gateway** -- Agents use the CLI for structured operations. Bodies are edited directly.
3. **Verbose flags for LLMs** -- `--blocked-by`, detailed help strings, `llpm help --verbose`. LLMs make most CLI calls, so clarity beats brevity.
4. **Derived over manual** -- `blocked` status and `children` are computed, not stored. Completing blockers auto-unblocks.
5. **Stable IDs** -- Ticket IDs and TODO IDs never reuse. Atomic file creation prevents collisions across parallel agents.
6. **Templates in the project** -- Copied on init, read from `docs/templates/`. Editable per-project, extensible with custom types.
7. **Core + extended schema** -- All types share core fields. Type-specific fields are optional. Unknown fields are preserved.
8. **Cross-platform** -- pathlib for paths, UTF-8 with reconfigure safety net. macOS/Linux first, Windows works via Python's path handling.
9. **Test everything against fixtures** -- Fresh copy per test, deterministic dates, real ticket hierarchies.
10. **Parallel-agent safe** -- Atomic file creation for IDs, last-write-wins for updates. No lock files.
