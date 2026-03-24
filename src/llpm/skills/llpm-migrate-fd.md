# Migrate from FD System to LLPM

You are migrating a project from the old Feature Design (FD) system to LLPM. The old system used bold-text key-value pairs in markdown files. The new system uses YAML frontmatter.

## Step 1: Check current state

First, determine what exists:

```bash
llpm init  # safe to re-run, won't clobber existing docs/
```

Look for old FD documents. They typically live in `docs/` or `docs/tickets/` and have this format:

```markdown
**ID**: FD-001
**Title**: Some Feature
**Status**: in-progress
**Priority**: high
...

## Problem
...
```

Search for them:
```bash
# Look for old bold-text frontmatter files
grep -rl '^\*\*ID\*\*:' docs/ 2>/dev/null
# Also check for FD- prefixed files
find docs/ -name 'FD-*.md' 2>/dev/null
```

## Step 2: Convert each document

For each old FD document found:

1. **Read the old file** and extract the bold-text fields
2. **Map the fields** to LLPM YAML frontmatter:
   - `**ID**: FD-001` -> `id: "FEAT-001"` (note: FD- becomes FEAT-)
   - `**Title**: ...` -> `title: "..."`
   - `**Status**: ...` -> `status: ...` (normalize to valid LLPM statuses)
   - `**Priority**: ...` -> `priority: ...`
   - `**Effort**: ...` -> `effort: ...` (map to: trivial | small | medium | large | xlarge)
   - `**Parent**: ...` -> `parent: ...` (update FD- references to FEAT-)
   - `**Blockers**: ...` -> `blockers: [...]` (update FD- references to FEAT-)
   - `**Tags**: ...` -> `tags: [...]`
   - `**Created**: ...` -> `created: "..."`
   - `**Updated**: ...` -> `updated: "..."`
   - `**Completed**: ...` -> `completed: "..."` or `null`
3. **Preserve the body** -- everything after the bold-text fields is the markdown body
4. **Write as new LLPM format** with YAML frontmatter

### Status mapping

Old FD statuses may not match LLPM exactly. Map them:
- `draft` -> `draft`
- `planned` / `specced` -> `planned`
- `open` / `ready` -> `open`
- `in-progress` / `active` / `wip` -> `in-progress`
- `review` / `in-review` -> `review`
- `complete` / `done` / `finished` -> `complete`
- `closed` -> `closed`
- `deferred` / `postponed` / `blocked` -> `deferred` (note: `blocked` is now derived in LLPM)

### ID renaming

The old FD system used `FD-` as the feature prefix. LLPM uses `FEAT-`. When converting:
- Rename `FD-NNN` to `FEAT-NNN` in the ID field
- Rename `FD-NNN` to `FEAT-NNN` in ALL references (parent, blockers, body text)
- Rename the file from `FD-NNN_SLUG.md` to `FEAT-NNN_SLUG.md`

Other prefixes (EPIC-, TASK-, RESEARCH-) stay the same.

## Step 3: Verify migration

After converting all documents:

```bash
llpm list                    # all tickets should appear with correct statuses
llpm board                   # active work should show correctly
llpm show FEAT-001           # spot-check a converted ticket
```

Check for:
- All IDs are valid and unique
- Parent/blocker references point to real tickets
- Statuses are valid LLPM statuses
- No orphaned references to old FD- IDs

## Step 4: Clean up

- Remove old FEATURE_INDEX.md if it exists (LLPM doesn't use it)
- Remove any old FD system config files
- Update CLAUDE.md if it references the old FD system commands

## Important notes

- Do NOT use `llpm create` for migration -- that would assign new IDs. Instead, write the files directly with the correct converted IDs to preserve the existing numbering.
- The `docs/templates/` directory should already exist from `llpm init`. Don't overwrite customized templates.
- If the old system had a `children` field, drop it. LLPM derives children from the `parent` field.
- If the old system had a `blocked` status stored explicitly, convert it: set the actual status (e.g., `open` or `in-progress`) and ensure the blocking tickets are in the `blockers` list. LLPM will derive the blocked state.
- Run `llpm list --status blocked` after migration to verify blocker relationships are correct.
