---
id: FEAT-002
type: feature
title: MdTreeStore — vault-backed store over markdown-tree-service
status: planned
priority: medium
effort: large
parent: EPIC-001
blockers:
- FEAT-001
created: '2026-07-04'
updated: '2026-07-04'
completed: null
tags:
- task-fabric
- tier-standard
- blocked-external
---
# Problem
Vault-native repos keep their tickets in the agent-memory vault at `repos.<name>.llpm.*` instead of an in-repo `llpm/` dir. llpm needs a `TicketStore` implementation that talks to markdown-tree-service over HTTP.

# Solution
`MdTreeStore(base_url, repo_stem)` implementing the `TicketStore` protocol:
- Stem mapping: `TASK-012` ↔ `repos.<name>.llpm.tasks.TASK-012` (type-plural sub-stems: epics/features/tasks/research). Title lives in frontmatter only (filename slug is already non-canonical).
- Archive: move to `repos.<name>.llpm.archive.<ID>` (preserves `include_archive` list semantics).
- Blobs: TODO.md ↔ `repos.<name>.llpm.todo`; templates ↔ `repos.<name>.llpm.templates.<type>`.
- `create_exclusive` → the service's create-iff-absent endpoint; keep the same next_id scan+retry loop.
- Config: stdlib HTTP (urllib) or httpx as an optional extra — decide against repo's dependency philosophy (PyYAML-only today; lean stdlib).

# Design Decisions
- Read the resolution/pointer from the store-discovery ticket's mechanism (`.llpm/config.toml` → `store = "mdtree"`, `url`, `stem`).
- Errors must be loud and specific (connection refused, 404 stem, create conflict) — never fall back silently to local.

# Out of Scope
- Auth (deferred to the homelab agent-scoped-auth pass; endpoints are LAN-trust for now).
- Mirror/gitea write path (separate ticket).

# Acceptance Criteria
- Same command matrix as the extraction ticket passes against a live or faked markdown-tree-service.
- Integration test with a mock HTTP server (no network in CI).

# References
- BLOCKED EXTERNALLY on markdown-tree-service "write REST + create-exclusive" (that repo's llpm EPIC/FEAT filed 2026-07-04). Unblock signal: write endpoints live on https://agent-memory.home.lab (or dev instance).
- Vault: `area.homelab.agent-platform.task-fabric.llpm-plan`, `.schema` (stem layout + frontmatter superset).
