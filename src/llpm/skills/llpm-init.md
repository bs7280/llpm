# Initialize LLPM in a Project

You are setting up LLPM (LLM Project Manager) in a project for the first time. LLPM is a CLI tool for stateless, markdown-based project management designed for LLM multi-agent workflows.

## Step 1: Initialize

```bash
llpm init
```

This creates:
- `docs/tickets/` -- where all ticket markdown files live
- `docs/tickets/archive/` -- for completed/closed tickets
- `docs/templates/` -- editable templates for each ticket type

## Step 2: Understand the project and create initial tickets

Read the project's README, existing docs, and codebase structure to understand what's being built. Then create an initial set of tickets that capture the current state of work.

### Ticket types and when to use them

- **epic** -- Large initiative spanning multiple features. Use for major milestones or project phases.
  ```bash
  llpm create epic "Authentication System" --priority high --body "## Objective\n\n..."
  ```

- **feature** -- A specific capability to build. Has Problem/Solution/Files/Verification sections.
  ```bash
  llpm create feature "JWT Login Flow" --parent EPIC-001 --body "## Problem\n\n..."
  ```

- **task** -- A concrete unit of work. Has Description/Acceptance Criteria/Notes sections.
  ```bash
  llpm create task "Install JWT library" --parent FEAT-001 --effort small
  ```

- **research** -- Investigation or spike. Has Hypothesis/Methodology/Findings/Conclusion sections.
  ```bash
  llpm create research "Compare auth libraries" --parent FEAT-001
  ```

### Tips for initial ticket creation

1. Start with 1-3 epics that capture the major workstreams
2. Break each epic into features (the "what")
3. Break features into tasks (the "how")
4. Use `--parent` to build the hierarchy
5. Use `llpm blocker add` to capture dependencies between tickets
6. Mark tasks that need human action with `--requires-human`
7. Don't over-plan -- create tickets for known work, add more as you go

## Step 3: Set up the workflow

### For humans (TODO inbox)

Humans capture ideas quickly without structuring them:
```bash
llpm todo --add "rate limiting on API"
llpm todo --add "fix 500 on /users"
llpm todo -i  # REPL mode for rapid entry
```

### For agents (structured commands)

Agents read TODOs and triage into proper tickets:
```bash
llpm todo -l                           # read the inbox
llpm create task "Fix /users 500" ...  # create proper ticket
llpm todo --rm 2                       # clear triaged item
```

Agents pick up work from the board:
```bash
llpm board                             # see what's available
llpm show TASK-001                     # read the spec
llpm status TASK-001 in-progress       # claim it
# ... do the work ...
llpm status TASK-001 review            # mark for review
```

### Status lifecycle

```
draft -> planned -> open -> in-progress -> review -> complete
                                                  -> closed (won't fix)
                                       -> deferred (postponed)
```

- `blocked` is derived -- a ticket with unresolved blockers automatically shows as blocked
- `draft` = created without a body (placeholder/stub)
- `planned` = spec'd but not yet approved for work
- `review` = work done, needs verification

## Step 4: Update CLAUDE.md

Add LLPM conventions to the project's CLAUDE.md so all agents know how to use it:

```markdown
## Project Management

This project uses LLPM for task tracking. All tickets live in `docs/tickets/`.

Key commands:
- `llpm board` -- see active work
- `llpm show <ID>` -- read a ticket's full spec
- `llpm status <ID> <status>` -- update ticket status
- `llpm help --verbose` -- full CLI reference

Before starting work, check `llpm board` and `llpm show` for context.
After completing work, update the ticket status to `review`.
```

## CLI Quick Reference

```bash
llpm init                              # set up project
llpm create <type> "title" [options]   # new ticket
llpm list [--status X] [--type X]      # list tickets
llpm board                             # kanban view
llpm backlog                           # draft/planned tickets
llpm show <ID>                         # full ticket details
llpm status <ID> <status>              # change status
llpm set <ID> field=value              # set fields
llpm blocker add <ID> --blocked-by <ID> # add dependency
llpm blocker list <ID>                 # show blockers
llpm archive <ID>                      # archive closed ticket
llpm delete <ID>                       # delete (with cleanup)
llpm todo --add "text"                 # quick capture
llpm todo -l                           # list todos
llpm help --verbose                    # full reference
```
