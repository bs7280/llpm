---
id: FEAT-001
type: feature
title: TicketStore protocol + LocalDirStore extraction
status: in-progress
priority: high
effort: medium
parent: EPIC-001
blockers: []
created: '2026-07-04'
updated: '2026-07-04'
completed: null
tags:
- task-fabric
- tier-standard
---
# Problem
All ticket I/O is raw filesystem access spread across `parser.py` (core: `parse_document` :47, `write_document` :76, `find_tickets` :134, `find_ticket_by_id` :148, `load_all_tickets` :160, `next_id` :180) and `commands.py` (~25-30 call sites + direct ops: archive `rename` :675/:686, delete `unlink` :759, TODO.md :797/:807, template read :361, `O_EXCL` create :442-449). This blocks any non-local storage backend (vault-over-API, gitea-backed).

# Solution
Extract a `TicketStore` protocol and a behavior-preserving `LocalDirStore` implementation.

New module `src/llpm/store.py`:
- `TicketStore` (Protocol): `list_tickets(include_archive: bool) -> list[Path-like refs or parsed docs]` · `read(id)` · `write(id, doc)` · `create_exclusive(filename, content)` (raises on exists) · `archive(id)` · `delete(id)` · `read_blob(name)` / `write_blob(name, text)` (TODO.md, project templates) · `exists(id)`
- `LocalDirStore(docs_root)`: wraps the EXACT current operations, including the `O_CREAT|O_EXCL` atomic create.
- `next_id(type)` logic stays client-side (scan-for-max + create-exclusive retry loop, currently 3 attempts) — it must work identically against any store.

Rewire `parser.py` functions and `commands.py` call sites to go through the store. Store construction happens once at dispatch (from `--docs-root` / `LLPM_DOCS_ROOT` / `./llpm` — resolution order unchanged, `commands.py:51-58`).

# Design Decisions
- **Zero behavior change.** This is an extraction, not a redesign. File format, filename convention (`<TYPE>-NNN_SLUG.md`), CLI surface, output, and error messages all unchanged.
- Derived-at-read invariants (`blocked` via `effective_status`, `children` via `get_children`) stay ABOVE the store seam — they compose store reads, they are not store methods.
- Directory/skills/init/template-copy operations MAY stay filesystem-local (`cmd_init`, `cmd_skills`) — they are project scaffolding, not ticket data. Judgment call: keep the seam minimal.
- No new dependencies. PyYAML remains the only runtime dep.

# Out of Scope
- Any remote/HTTP store implementation (that's the MdTreeStore ticket, blocked on markdown-tree-service write REST).
- The in-repo pointer file / store-kind discovery (separate ticket).
- `model_tier` field (separate ticket).

# Acceptance Criteria
- Full existing test suite passes UNCHANGED (no test edits beyond import paths if strictly unavoidable — prefer zero).
- New unit tests for `LocalDirStore` and the protocol seam (a `FakeStore`/in-memory store exercising one command end-to-end proves the seam is real).
- `llpm board`, `llpm create task "x"`, `llpm status <id> in-progress`, `llpm archive`, `llpm delete`, `llpm todo --list` all behave identically on a sample project.
- Ruff/format clean if the repo uses it; match existing code style either way.

# References
- Vault: `area.homelab.agent-platform.task-fabric.llpm-plan` (code-verified scoping) and `.ticketstore` (design).
- NOTE: main has uncommitted work touching `commands.py`/`__main__.py` (JSON output feature). Work from committed main on a branch; the merge is a known follow-up.
