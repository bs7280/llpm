# PLAN_DISCUSSION.md

This document captures design questions, ambiguities, and callouts from the initial review of `build_guide.md`. Each item needs a resolution before we finalize the build guide and start coding.

---

# ALL ITEMS RESOLVED

All 16 design decisions have been finalized. See the updated `build_guide.md` for the canonical spec.

---

# RESOLVED

Items below have clear decisions. Will be incorporated into the updated build guide.

---

## 1. Package & Prefix Naming
**Decision**: Package = `llpm`. Feature prefix = `FEAT-`. Others stay: `EPIC-`, `TASK-`, `RESEARCH-`.

## 2. `children` Field
**Decision**: Drop from frontmatter. Derive at read time by scanning for `parent: X`. Single source of truth.

## 3. `set` Command Scope
**Decision**: `set` cannot modify `status` or `blockers`. Those go through dedicated `llpm status` and `llpm blocker` commands. `set` handles: `priority`, `effort`, `parent`, `tags`, `title`.

## 4. Blockers: Ticket-Only + `requires_human` Flag
**Decision**: Blockers must be valid ticket IDs only -- no free-text. For external dependencies, create a task and block on it. Add `requires_human: true/false` as an optional frontmatter field so agents can identify tasks that need human action. This is a type-specific extended field (see #9), surfaced via `llpm list` filtering and board indicators.

## 5. `FEATURE_INDEX.md`
**Decision**: Drop it. Artifact of the old FD system.

## 6. `archive` Command
**Decision**: Add `llpm archive <ticket-id>`. Only `complete` or `closed` tickets can be archived. Also support `llpm archive --all` to find and archive all closed non-archived tickets (with confirmation prompt). Archived tickets remain on disk and scannable for ID generation / history.

## 7. `draft` vs `open` Semantics
**Decision**: Keep as-is. `draft` = created without body (lazy creation). `open` = body provided at creation. Status can always be changed manually afterward. Clarify in docs that it's a creation-time convenience, not a body-presence check.

## 8. Effort Scale
**Decision**: Make effort optional (nullable). Broaden to `trivial | small | medium | large | xlarge`. Frame as complexity/risk, not time. Epics won't have effort (see #9).

## 9. Type-Specific Frontmatter (Core vs Extended Fields)
**Decision**: Split frontmatter into core (required on all types) and extended (type-specific, optional).

**Core fields** (all types): `id, type, title, status, priority, parent, blockers, created, updated, completed, tags`

**Extended fields** (type-specific):
- `effort` -- on task, feature, research (not epic)
- `requires_human` -- on task (not epic, feature, research by default)

Extra/unknown fields in frontmatter are preserved but not validated by the CLI.

**Templates**: `init` copies bundled templates to `docs/templates/`. CLI always reads from `docs/templates/` -- never from bundled directly. If template is missing, error (no silent fallback). Custom types supported: create a `docs/templates/<type>.md` with core fields, and `llpm create <type> "title"` works. ID prefix derived from type name (uppercase).

## 10. UTF-8 vs ASCII Output
**Decision**: UTF-8 everywhere. Keep `reconfigure` as a safety net. Tool itself won't emit emoji or decorative unicode -- plain text output.

## 11. Windows-Specific Details + Multi-Project
**Decision**: Strip Windows content from build guide. Use pathlib in code. Unix paths in examples.

For docs root resolution: `--docs-root` flag > `LLPM_DOCS_ROOT` env var > `./docs/` default. Internals always work with absolute `Path`. Multi-project management is a future separate tool; this CLI just needs to accept any docs root cleanly.

## 12. Claude Skill & Agent Roles
**Decision**: Focus on `llpm help` and `llpm <cmd> --help` being comprehensive. Add `llpm help --verbose` for full dump. No Claude skill for v1.

## 13. Concurrent Access / ID Collisions
**Decision**: Use atomic file creation (`os.O_CREAT | os.O_EXCL`) in `create` to guarantee unique IDs across parallel agents. If file already exists, rescan and retry (up to 3 times). For ticket updates, accept last-write-wins -- agents typically work on separate tickets. Document as a known limitation; add file locking for updates later if needed.

## 14. `delete` Command
**Decision**: Add `llpm delete <ticket-id>` with confirmation. Must warn about and clean up relationships (remove from other tickets' `blockers` lists, note orphaned children). Primarily for mistakes.

## 15. Blocker ID Validation
**Decision**: Blockers must be valid, existing ticket IDs. No free-text. Error on non-existent ID.

## 16. `todo` Command
**Decision**: Use explicit flags: `--add "text"`, `--rm <id>`, `--list` / `-l`. Bare `llpm todo` (no flags) shows `--help`. Add `--interactive` / `-i` for REPL mode (rapid-fire entry, each line appended, ctrl-d or empty line exits). IDs are stable and never reused.

---

## Summary

| # | Topic | Decision | Status |
|---|-------|----------|--------|
| 1 | Package/prefix naming | `llpm` + `FEAT-` | RESOLVED |
| 2 | `children` field | Drop, derive from `parent` | RESOLVED |
| 3 | `set` scope | No status/blockers, use dedicated cmds | RESOLVED |
| 4 | Blockers + human-required | Ticket-only blockers, `requires_human` flag | RESOLVED |
| 5 | `FEATURE_INDEX.md` | Drop | RESOLVED |
| 6 | `archive` command | Add with `--all` option | RESOLVED |
| 7 | `draft`/`open` semantics | Keep as-is, clarify docs | RESOLVED |
| 8 | Effort scale | Optional, broader scale, complexity not time | RESOLVED |
| 9 | Type-specific frontmatter | Core + extended, templates in docs/ | RESOLVED |
| 10 | UTF-8/ASCII | UTF-8, no decorative unicode in tool output | RESOLVED |
| 11 | Windows + multi-project | Pathlib, env var for docs root | RESOLVED |
| 12 | Claude skill | Focus on `--help`, no skill for v1 | RESOLVED |
| 13 | Concurrent ID safety | Atomic file creation (O_EXCL), last-write-wins for updates | RESOLVED |
| 14 | `delete` command | Add with confirmation + relationship cleanup | RESOLVED |
| 15 | Blocker validation | Must be real ticket IDs, no free-text | RESOLVED |
| 16 | `todo` command | Explicit flags + REPL mode | RESOLVED |
