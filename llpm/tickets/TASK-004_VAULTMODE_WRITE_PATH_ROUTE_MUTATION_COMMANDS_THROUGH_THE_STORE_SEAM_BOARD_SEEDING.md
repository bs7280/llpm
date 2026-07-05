---
id: "TASK-004"
type: task
title: "Vault-mode write path: route mutation commands through the store seam + board seeding"
status: planned  # draft | planned | open | in-progress | review | complete | closed | deferred (blocked is derived)
priority: high  # low | medium | high
effort: medium  # trivial | small | medium | large | xlarge
requires_human: false
parent: FEAT-002
blockers: []
created: "2026-07-05"
updated: "2026-07-05"
completed: null
tags:
- store
- infra
model_tier: light  # heavy | standard | light
---

## Description

Second consumer-repo e2e pass (2026-07-05, after TASK-003's TLS fix): the read path and
`create` work over the vault, but the rest of the CLI does not. From claude-tools
(`.llpm/config.toml` → mdtree, `SSL_CERT_FILE` set):

- `llpm list` / `board` / `backlog` / `show` — **work** (use `_resolve_store_and_root`).
- `llpm create` — **works** once templates exist in the vault (`store.read_blob` →
  `repos.<repo>.llpm.templates.<type>`). First successful consumer-repo write:
  `repos.claude-tools.llpm.tasks.TASK-001`. But on a fresh board it fails with a
  local-path-flavored error (`No template found ... /dev/null/mdtree-sentinel/templates/task.md`)
  because nothing seeds the vault templates — claude-tools was seeded by hand via the
  agent-memory MCP (bundled template copies).
- `llpm status` / `set` / `blocker add|rm|list` / `archive` / `delete` / `todo` / `project` —
  **all fail with `Error: Not initialized. Run 'llpm init' first.`** They still use the legacy
  `_resolve_docs_root(args)` + `_require_initialized(docs_root)` (no store arg) +
  `_make_store(docs_root)` pattern (commands.py ~593-1027) instead of
  `_resolve_store_and_root(args)`, so vault mode never even constructs the MdTreeStore.
  (`_require_initialized` already special-cases MdTreeStore — it just never receives it.)

Consequence today: a vault board can be created and read but not mutated by llpm — status
flips have to go out-of-band through the agent-memory MCP (that's how claude-tools TASK-001
got its status/fields set).

## Scope

1. **Mechanical swap:** point `cmd_status`, `cmd_set`, `cmd_blocker_add/rm/list`,
   `cmd_archive`, `cmd_delete`, `cmd_todo`, `cmd_project` at `_resolve_store_and_root(args)`.
   Audit each command body for remaining direct-filesystem assumptions (archive moves,
   delete cleanup) — the store protocol already has `_move`/`_delete` seams.
2. **Board seeding:** make fresh vault boards usable without hand-seeding. Preferred:
   `cmd_create` falls back to the bundled template (`_templates_source()`) when
   `store.read_blob` returns `None` — avoids per-board template copies and the staleness
   class of bug (llpm's own `llpm/templates/` predated model_tier until 2026-07-05).
   Vault-side `templates.*` notes stay as an override layer. Optionally: vault-aware
   `llpm init` that seeds `templates.*` + empty `todo` via `write_blob` for explicitness.
3. **Tests + e2e:** vault-mode tests for each rerouted command (FakeStore/MdTreeStore);
   full consumer-repo round trip (create → status → set → blocker → archive → delete →
   todo) — this is the CLI half of markdown-tree-service TASK-002's acceptance.

## Acceptance Criteria

- [ ] Every llpm command that works in local mode works against an mdtree store (or fails
      with an actionable vault-specific message), from a consumer repo over TLS.
- [ ] Fresh vault board: `llpm create` works with no hand-seeding (bundled fallback).
- [ ] Full round-trip e2e documented/scripted; mts TASK-002 can point at it.
- [ ] The hand-seeded `repos.claude-tools.llpm.templates.*` notes either removed (fallback
      covers them) or kept deliberately as overrides — decided and noted.

## Notes

Found while standing up the first consumer-repo board (claude-tools). Context and dispatch
ordering: vault `area.homelab.agent-platform.task-fabric.rollout` § Status snapshot
(2026-07-05 evening addendum). Cross-links: llpm TASK-003 (TLS trust, merged, `review`),
mts TASK-002 (llpm-over-vault e2e), claude-tools board TASK-001 (TLS convention adoption).
