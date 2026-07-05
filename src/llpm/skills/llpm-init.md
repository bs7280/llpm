# Onboard LLPM in a Project

You are setting up LLPM (LLM Project Manager) in a project. LLPM is a CLI tool for
stateless, markdown-based project management designed for LLM multi-agent workflows.
Follow every step — the permissions step (Step 2) and the CLAUDE.md step (Step 5) are what
make future agent sessions run without friction; skipping them means permission prompts
every few commands and agents that don't know the board exists.

## Step 0: Pick the store

LLPM boards live in one of two stores:

- **Vault store (default for this homelab):** tickets are notes in the agent-memory vault
  at `repos.<stem>.llpm.*`, readable/writable by the `llpm` CLI over HTTPS, the
  agent-memory MCP, and the marginalia board UI (`https://marginalia.home.lab/board/<stem>`).
  No local ticket files in the repo at all.
- **Local-dir store:** classic `llpm/tickets/` markdown files in the repo. Use only for
  repos that must stay self-contained offline (or non-homelab machines).

For the vault store, machine preconditions (one-time, usually already done):
- `llpm` on PATH (`uv tool install --editable <llpm repo>` or equivalent)
- The mkcert root CA trusted by Python: `SSL_CERT_FILE=".../mkcert/rootCA.pem"` exported in
  **`~/.zshenv`** — NOT `.zshrc`, which is interactive-only and silently misses agent
  harnesses, cron, and `zsh -c` shells. See vault note
  `area.homelab.knowledge.claude-code-harness` § "TLS — the one gotcha".

## Step 1: Wire the store

**Vault store:** create `.llpm/config.toml` in the repo root (commit it):

```toml
[store]
kind = "mdtree"
url  = "https://agent-memory.home.lab"
stem = "<repo-name>"   # MUST match the gitea repo name / repos.<name> vault note
```

No `llpm init` needed — fresh vault boards work immediately (`create` falls back to
bundled templates). Verify with `llpm project` (should print the vault namespace, not a
local path).

Also wire the vault side (via the agent-memory MCP if available):
- Ensure the `repos.<stem>` note exists; set frontmatter `llpm: repos.<stem>.llpm`
  (the store-pointer convention — how agents discover "where are this repo's tickets").

**Local-dir store:** run `llpm init` (creates `llpm/tickets/`, `llpm/tickets/archive/`,
`llpm/templates/`).

**Migrating an existing local board to the vault:** PUT each ticket file to
`https://agent-memory.home.lab/api/v1/notes/repos.<stem>.llpm.<bucket>.<ID>?if_absent=true`
(JSON body `{"content": "<file text>"}`; bucket = tasks/features/epics/research from the ID
prefix; TODO.md → `repos.<stem>.llpm.todo`), then delete the local `llpm/` dir and add the
config. `if_absent` makes re-runs safe — an existing note is an error, never an overwrite.

## Step 2: Claude Code permissions (kills the prompt spam)

Two things cause permission prompts around llpm, and they need different fixes:

**(a) Allowlist the CLI.** Add to the project's `.claude/settings.json` (committed — so
every machine, box, and worktree gets it) or `.claude/settings.local.json` (this machine
only). Merge into the existing file if one exists:

```json
{
  "permissions": {
    "allow": [
      "Bash(llpm:*)"
    ]
  }
}
```

**(b) Run llpm as SINGLE, PLAIN commands.** Compound shell commands bypass the allowlist
entirely — no allow rule can ever match them, so they always prompt. This prompts every
time, even with the rule above:

```bash
# BAD — chaining, redirects, and pipes defeat the allowlist:
ls -la llpm/tickets/ 2>/dev/null; echo "==="; llpm project 2>&1 | head -30
```

```bash
# GOOD — each is one bare command, matched by Bash(llpm:*):
llpm project
llpm board
llpm list --json
```

Related rules that keep sessions prompt-free:
- Never `ls`/`find`/`cat` ticket files — vault boards have no local files anyway. The CLI
  is the interface: `llpm list`, `llpm show <ID>`.
- Need machine-readable output? Every read command takes `--json`. Don't pipe to `head`.
- llpm output is short; you never need to truncate or filter it in the shell.

## Step 3: Understand the project and create initial tickets

Read the project's README, existing docs, and codebase structure to understand what's
being built. Then create an initial set of tickets that capture the current state of work.

### Ticket types and when to use them

- **epic** — Large initiative spanning multiple features. Use for major milestones or
  project phases.
  ```bash
  llpm create epic "Authentication System" --priority high
  ```
- **feature** — A specific capability to build. Has Problem/Solution/Files/Verification
  sections.
  ```bash
  llpm create feature "JWT Login Flow" --parent EPIC-001
  ```
- **task** — A concrete unit of work. Has Description/Acceptance Criteria/Notes sections.
  ```bash
  llpm create task "Install JWT library" --parent FEAT-001 --effort small
  ```
- **research** — Investigation or spike. Has Hypothesis/Methodology/Findings/Conclusion
  sections.
  ```bash
  llpm create research "Compare auth libraries" --parent FEAT-001
  ```

### Tips for initial ticket creation

1. Start with 1-3 epics that capture the major workstreams
2. Break each epic into features (the "what"), features into tasks (the "how")
3. Use `--parent` to build the hierarchy; `llpm blocker add` for dependencies
4. Mark tasks that need human action with `--requires-human`
5. Set `model_tier` per ticket (`llpm set <ID> model_tier=light|standard|heavy`) — heavy
   plans/reviews, cheaper tiers execute well-scoped tickets
6. Don't over-plan — create tickets for known work, add more as you go

## Step 4: Set up the workflow

### For humans (TODO inbox)

```bash
llpm todo --add "rate limiting on API"
llpm todo -i    # REPL mode for rapid entry
```

### For agents (structured commands)

Triage the inbox into tickets:
```bash
llpm todo -l
llpm create task "Fix /users 500" --parent FEAT-002
llpm todo --rm 2
```

Pick up work from the board:
```bash
llpm board
llpm show TASK-001
llpm status TASK-001 in-progress
# ... do the work ...
llpm status TASK-001 review
```

### Status lifecycle

```
draft -> planned -> open -> in-progress -> review -> complete
                                                  -> closed (won't fix)
                                       -> deferred (postponed)
```

- `blocked` is derived — unresolved blockers automatically show a ticket as blocked
- `draft` = stub · `planned` = spec'd, not approved · `review` = done, needs verification

## Step 5: Update CLAUDE.md

**This step is critical.** Without it, agents will not know LLPM exists. Add the following
to the project's `CLAUDE.md`, adapting the specifics:

```markdown
## Project Management (LLPM)

This project uses LLPM for markdown-based task tracking. Tickets live in the agent-memory
vault at `repos.<stem>.llpm.*` — the CLI reads/writes them via `.llpm/config.toml`; there
are no local ticket files. Board UI: https://marginalia.home.lab/board/<stem>

### Running LLPM

`llpm` is a CLI tool installed globally on PATH. Run it directly — do NOT use `npx`,
`pnpm exec`, `bunx`, or any package runner.

**Always run llpm as a single, bare command** (`llpm board`, `llpm list --json`). Never
chain it with `;`/`&&`, pipe it, or redirect it — compound commands bypass the permission
allowlist and stall the session on a prompt. Use `--json` instead of piping to other tools,
and use `llpm list`/`llpm show` instead of ls/cat on ticket paths.

### Quick Reference

- `llpm board` — active work (kanban) · `llpm backlog` — planned/draft
- `llpm show <ID>` — full spec · `llpm status <ID> <status>` — update status
- `llpm blocker add <ID> --blocked-by <ID>` — dependency
- `llpm help --verbose` — full CLI reference

### Agent Roles

Different agent sessions serve different roles:

#### Worker
1. `llpm board` — find open/unblocked tickets
2. `llpm show <ID>` — read the spec; `llpm status <ID> in-progress` — claim it
3. Implement per spec; `llpm status <ID> review` when done (`complete` if no review needed)

Do not create or modify ticket specs. If a spec is unclear, set the ticket back to
`planned` and add a body note explaining what's missing.

#### Planner
1. Review `llpm backlog` for tickets needing specs
2. Research the codebase; write detailed specs into ticket bodies
3. Break large tickets down with `llpm create task --parent <ID>`; wire `llpm blocker add`
4. Set tickets to `open` when ready for a worker

#### Grooming / PM
1. Review `llpm list` / `llpm board`; discuss priorities with the user
2. Create/refine epics and features; triage `llpm todo -l` into tickets
3. Keep the hierarchy sane; flag blocked or stale tickets

#### Tester
1. `llpm list --status review` — find tickets awaiting verification
2. Verify acceptance criteria; `llpm status <ID> complete` on pass, or back to
   `in-progress` with a body note on failure
```

## Step 6: Verify

```bash
llpm project
llpm board
```

`llpm project` must show the intended store (vault namespace or local path). For vault
boards, also check `https://marginalia.home.lab/board/<stem>` — the repo appears on the
multi-repo board as soon as it has one ticket.

## CLI Quick Reference

```bash
llpm create <type> "title" [options]   # new ticket
llpm list [--status X] [--type X]      # list tickets (--json for machine-readable)
llpm board                             # kanban view
llpm backlog                           # draft/planned tickets
llpm show <ID>                         # full ticket details
llpm status <ID> <status>              # change status
llpm set <ID> field=value              # set fields (incl. model_tier)
llpm blocker add <ID> --blocked-by <ID> # add dependency
llpm archive <ID>                      # archive closed ticket
llpm delete <ID>                       # delete (with cleanup)
llpm todo --add "text" | -l | --rm <id> # TODO inbox
llpm help --verbose                    # full reference
```
