# Initialize LLPM in a Project

You are setting up LLPM (LLM Project Manager) in a project for the first time. LLPM is a CLI tool for stateless, markdown-based project management designed for LLM multi-agent workflows.

## Step 1: Initialize

```bash
llpm init
```

This creates:
- `llpm/tickets/` -- where all ticket markdown files live
- `llpm/tickets/archive/` -- for completed/closed tickets
- `llpm/templates/` -- editable templates for each ticket type

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

**This step is critical.** Add an LLPM section to the project's `CLAUDE.md` so that every agent session knows how to use LLPM, what role it should play, and how to interact with the ticket system. Without this, agents will not know LLPM exists.

Add the following to `CLAUDE.md`, adapting it to the project's specifics:

```markdown
## Project Management (LLPM)

This project uses LLPM for markdown-based task tracking. All tickets live in `llpm/tickets/`.

### Quick Reference

- `llpm board` -- see active work (kanban view)
- `llpm backlog` -- see planned/draft tickets
- `llpm show <ID>` -- read a ticket's full spec and body
- `llpm status <ID> <status>` -- update ticket status
- `llpm blocker add <ID> --blocked-by <ID>` -- add a dependency
- `llpm help --verbose` -- full CLI reference

### Agent Roles

Different agent sessions serve different roles. Follow the guidelines for your role:

#### Worker
You implement tickets. Your workflow:
1. Run `llpm board` to find open/unblocked tickets ready for work
2. Run `llpm show <ID>` to read the full spec
3. Run `llpm status <ID> in-progress` to claim the ticket
4. Implement the work according to the ticket spec
5. Run `llpm status <ID> review` when done (or `complete` if no review is needed)

Do not create or modify ticket specs. If the spec is unclear or incomplete, set the ticket back to `planned` and add a note in the body explaining what's missing.

#### Planner
You research, design, and write ticket specs. Your workflow:
1. Review `llpm backlog` for draft/planned tickets that need fleshing out
2. Research the codebase, ask the user questions, and iterate on the design
3. Write detailed specs in ticket bodies (Problem, Solution, Files, Acceptance Criteria)
4. Break large tickets into subtasks with `llpm create task --parent <ID>`
5. Set up dependencies with `llpm blocker add`
6. Set tickets to `open` when the spec is ready for a worker to pick up

You may also create new tickets from research or user conversations.

#### Grooming / PM
You manage the backlog and help the user prioritize. Your workflow:
1. Review `llpm list` and `llpm board` for the current state of work
2. Discuss priorities, scope, and feature ideas with the user
3. Create and refine epics and features at a high level
4. Triage `llpm todo -l` items into proper tickets or discard them
5. Ensure ticket hierarchy makes sense (epics -> features -> tasks)
6. Flag blocked or stale tickets and help resolve dependencies

You focus on *what* to build and *why*, not implementation details.

#### Tester
You verify completed work. Your workflow:
1. Run `llpm list --status review` to find tickets awaiting verification
2. Run `llpm show <ID>` to read the acceptance criteria
3. Run tests, check the implementation, verify the criteria are met
4. Run `llpm status <ID> complete` if it passes, or `llpm status <ID> in-progress` with a body note explaining what failed
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
