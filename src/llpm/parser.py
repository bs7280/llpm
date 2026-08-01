"""Frontmatter parsing, validation, and ticket discovery for LLPM."""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

import yaml


# -- Constants --

VALID_STATUSES = {"draft", "planned", "open", "in-progress", "review", "complete", "closed", "deferred"}
RESOLVED_STATUSES = {"complete", "closed"}
VALID_PRIORITIES = {"low", "medium", "high"}
VALID_EFFORTS = {"trivial", "small", "medium", "large", "xlarge"}
VALID_MODEL_TIERS = {"heavy", "standard", "light"}
VALID_ORIGINS = {"human", "agent"}

CORE_FIELDS = {"id", "type", "title", "status", "priority", "parent", "blockers", "created", "updated", "completed", "tags"}

# Ticket types that may carry `serves:` goal references (goal is a frontmatter
# type, not a place -- refs are full vault stems, cross-repo by design)
SERVES_TYPES = {"epic", "feature"}

# Built-in type -> ID prefix mapping
TYPE_PREFIXES = {
    "epic": "EPIC",
    "feature": "FEAT",
    "task": "TASK",
    "research": "RESEARCH",
}

# Regex to match ticket ID patterns like FEAT-001, TASK-012, BUG-001
TICKET_ID_RE = re.compile(r"^([A-Z]+)-(\d{3,})$")


# -- Frontmatter Parsing --

def _normalize_value(value):
    """Normalize PyYAML quirks: datetime.date -> str."""
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def parse_text(text: str, source="<text>") -> tuple[dict, str]:
    """Parse markdown text with YAML frontmatter.

    Returns (frontmatter_dict, body_str). Raises ValueError on malformed input.
    ``source`` appears in error messages only.
    """
    if not text.startswith("---"):
        raise ValueError(f"No frontmatter found in {source}")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Unterminated frontmatter in {source}")

    # parts[0] is empty (before first ---), parts[1] is YAML, parts[2] is body
    raw_yaml = parts[1]
    body = parts[2]
    if body.startswith("\n"):
        body = body[1:]

    data = yaml.safe_load(raw_yaml)
    if not isinstance(data, dict):
        raise ValueError(f"Frontmatter is not a mapping in {source}")

    # Normalize dates
    for key, value in data.items():
        data[key] = _normalize_value(value)

    return data, body


def parse_document(path: Path) -> tuple[dict, str]:
    """Parse a markdown file with YAML frontmatter.

    Returns (frontmatter_dict, body_str). Raises ValueError on malformed input.
    """
    text = path.read_text(encoding="utf-8")
    return parse_text(text, source=path)


def serialize_document(frontmatter: dict, body: str) -> str:
    """Serialize frontmatter + body to markdown text."""
    yaml_str = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{yaml_str}---\n{body}"


def write_document(path: Path, frontmatter: dict, body: str) -> None:
    """Write a markdown file with YAML frontmatter."""
    path.write_text(serialize_document(frontmatter, body), encoding="utf-8")


# -- Validation --

def validate_frontmatter(data: dict) -> list[str]:
    """Validate frontmatter fields. Returns list of error messages (empty = valid)."""
    errors = []

    # Check required core fields
    for field in CORE_FIELDS:
        if field not in data:
            errors.append(f"Missing required field: '{field}'")

    if errors:
        return errors  # Can't validate further without required fields

    # Validate enum fields
    if data["status"] not in VALID_STATUSES:
        errors.append(f"Invalid status: '{data['status']}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}")

    if data["priority"] not in VALID_PRIORITIES:
        errors.append(f"Invalid priority: '{data['priority']}'. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}")

    # Effort is optional -- validate only if present and non-null
    effort = data.get("effort")
    if effort is not None and effort not in VALID_EFFORTS:
        errors.append(f"Invalid effort: '{effort}'. Must be one of: {', '.join(sorted(VALID_EFFORTS))}")

    # model_tier is optional -- validate only if present and non-null
    model_tier = data.get("model_tier")
    if model_tier is not None and model_tier not in VALID_MODEL_TIERS:
        errors.append(f"Invalid model_tier: '{model_tier}'. Must be one of: {', '.join(sorted(VALID_MODEL_TIERS))}")

    # origin (provenance) is optional -- validate only if present and non-null
    origin = data.get("origin")
    if origin is not None and origin not in VALID_ORIGINS:
        errors.append(f"Invalid origin: '{origin}'. Must be one of: {', '.join(sorted(VALID_ORIGINS))}")

    # Validate ID prefix matches type
    ticket_id = data["id"]
    ticket_type = data["type"]
    expected_prefix = _prefix_for_type(ticket_type)
    if expected_prefix and not ticket_id.startswith(expected_prefix + "-"):
        errors.append(f"ID '{ticket_id}' does not match type '{ticket_type}' (expected prefix '{expected_prefix}-')")

    # Validate list fields are lists
    for field in ("blockers", "tags", "serves", "waits_on", "after", "commits"):
        val = data.get(field)
        if val is not None and not isinstance(val, list):
            errors.append(f"Field '{field}' must be a list, got {type(val).__name__}")

    return errors


def _prefix_for_type(ticket_type: str) -> str:
    """Get the ID prefix for a ticket type. Falls back to uppercase type name."""
    return TYPE_PREFIXES.get(ticket_type, ticket_type.upper())


# -- Ticket Discovery --
#
# These functions accept either a docs_root Path (wrapped in a LocalDirStore)
# or a TicketStore instance. Every read bottoms out in store methods so that
# alternative store implementations see all traffic.

def _as_store(source):
    """Coerce a docs_root path into a LocalDirStore; pass stores through."""
    if isinstance(source, (str, os.PathLike)):
        from .store import LocalDirStore
        return LocalDirStore(Path(source))
    return source


def find_tickets(docs_root: Path, include_archive: bool = True) -> list[Path]:
    """Find all ticket markdown files under docs_root/tickets/."""
    return _as_store(docs_root).list_tickets(include_archive=include_archive)


def find_tickets_active(docs_root: Path) -> list[Path]:
    """Find only non-archived ticket files."""
    return find_tickets(docs_root, include_archive=False)


def find_ticket_by_id(docs_root: Path, ticket_id: str) -> Path | None:
    """Find a ticket file by its ID (case-insensitive prefix match on filename)."""
    upper_id = ticket_id.upper()
    for path in find_tickets(docs_root, include_archive=True):
        if path.name.upper().startswith(upper_id):
            return path
    return None


def load_all_tickets(docs_root: Path, include_archive: bool = True) -> list[tuple[Path, dict, str]]:
    """Parse all tickets, skipping files that fail to parse."""
    store = _as_store(docs_root)
    results = []
    for ref in store.list_tickets(include_archive=include_archive):
        try:
            fm, body = store.read_ref(ref)
            results.append((ref, fm, body))
        except (ValueError, yaml.YAMLError):
            continue
    return results


# -- ID Generation --

def next_id(docs_root: Path, ticket_type: str) -> str:
    """Generate the next ticket ID for the given type.

    Scans all tickets (including archive) to find the highest existing number,
    then returns the next one. Zero-pads to 3 digits.

    Stays client-side (scan-for-max) so it works identically against any store.
    """
    prefix = _prefix_for_type(ticket_type)
    max_num = 0

    for path in find_tickets(docs_root, include_archive=True):
        name = path.stem.upper()
        if name.startswith(prefix + "-"):
            # Extract the number part (between prefix- and first _)
            rest = name[len(prefix) + 1:]
            num_str = rest.split("_")[0]
            try:
                num = int(num_str)
                max_num = max(max_num, num)
            except ValueError:
                continue

    return f"{prefix}-{max_num + 1:03d}"


# -- Blocker Resolution --

def is_blocked(docs_root: Path, frontmatter: dict) -> bool:
    """Check if a ticket has any unresolved blockers or cross-board waits."""
    store = _as_store(docs_root)

    for blocker_id in frontmatter.get("blockers") or []:
        try:
            found = store.read(blocker_id)
        except (ValueError, yaml.YAMLError):
            return True
        if found is None:
            # Dangling reference -- treat as blocking (shouldn't happen with validation)
            return True
        _, fm, _ = found
        if fm.get("status") not in RESOLVED_STATUSES:
            return True

    if any(d["blocking"] for d in get_waits_on_details(store, frontmatter)):
        return True

    return False


def get_blocker_details(docs_root: Path, frontmatter: dict) -> list[dict]:
    """Get detailed info about each blocker on a ticket."""
    blockers = frontmatter.get("blockers") or []
    details = []

    store = _as_store(docs_root)
    for blocker_id in blockers:
        try:
            found = store.read(blocker_id)
        except (ValueError, yaml.YAMLError):
            details.append({
                "id": blocker_id,
                "status": "parse error",
                "title": "???",
                "resolved": False,
            })
            continue

        if found is None:
            details.append({
                "id": blocker_id,
                "status": "not found",
                "title": "???",
                "resolved": False,
            })
            continue

        _, fm, _ = found
        details.append({
            "id": fm.get("id", blocker_id),
            "status": fm.get("status", "unknown"),
            "title": fm.get("title", "???"),
            "resolved": fm.get("status") in RESOLVED_STATUSES,
        })

    return details


def _read_foreign(store, stem: str) -> tuple[str, dict | None]:
    """Resolve a foreign vault stem via the store. Stores that predate the
    ``read_foreign`` protocol method degrade to 'unavailable'."""
    fn = getattr(store, "read_foreign", None)
    if fn is None:
        return ("unavailable", None)
    return fn(stem)


def get_waits_on_details(docs_root: Path, frontmatter: dict) -> list[dict]:
    """Resolve each cross-board ``waits_on`` stem to its current state.

    States: 'ok' (target read; status/resolved meaningful), 'missing'
    (definitive miss -> blocking, mirrors dangling intra-board blockers),
    'error' (target unparseable -> blocking), 'unavailable' (store cannot
    resolve foreign stems or vault unreachable -> NOT blocking, so local-dir
    and offline use degrades gracefully instead of freezing the board).
    """
    waits = frontmatter.get("waits_on") or []
    if not waits:
        return []

    store = _as_store(docs_root)
    details = []
    for stem in waits:
        state, fm = _read_foreign(store, stem)
        status = fm.get("status") if fm else None
        resolved = state == "ok" and status in RESOLVED_STATUSES
        details.append({
            "stem": stem,
            "state": state,
            "status": status,
            "resolved": resolved,
            "blocking": state in ("missing", "error") or (state == "ok" and not resolved),
        })
    return details


def effective_status(docs_root: Path, frontmatter: dict) -> str:
    """Return the effective status, accounting for blockers.

    If the ticket has unresolved blockers and isn't in a terminal state,
    returns 'blocked' instead of the stored status.
    """
    stored = frontmatter.get("status", "draft")
    # Don't override terminal/deferred states
    if stored in ("complete", "closed", "deferred"):
        return stored
    if is_blocked(docs_root, frontmatter):
        return "blocked"
    return stored


# -- Derived Relationships --

def get_children(docs_root: Path, ticket_id: str) -> list[dict]:
    """Find all tickets that have parent == ticket_id. Derived at read time."""
    upper_id = ticket_id.upper()
    children = []
    for path, fm, _ in load_all_tickets(docs_root, include_archive=False):
        parent = fm.get("parent")
        if parent and parent.upper() == upper_id:
            children.append({
                "id": fm.get("id"),
                "type": fm.get("type"),
                "title": fm.get("title"),
                "status": fm.get("status"),
            })
    return children


# -- Goals rollup (FEAT-008) --

def get_goal_notes(docs_root: Path) -> list[tuple[str, dict]]:
    """Vault-wide scan for ``type: goal`` notes -- goal is a type, not a
    place. Degrades to ``[]`` for stores that can't scan (a local dir with
    no vault, or a store predating the ``scan_by_type`` protocol method)."""
    store = _as_store(docs_root)
    fn = getattr(store, "scan_by_type", None)
    if fn is None:
        return []
    return fn("goal")


def _goal_stems_served(frontmatter: dict, by_id: dict[str, dict]) -> set[str]:
    """Every goal stem a ticket serves: its own ``serves`` plus every
    ancestor's (only epics/features carry `serves`; tasks/research inherit
    it by walking the `parent` chain up)."""
    stems = set(frontmatter.get("serves") or [])
    seen: set[str] = set()
    parent_id = frontmatter.get("parent")
    while parent_id:
        key = parent_id.upper()
        if key in seen:
            break  # guard against a parent cycle
        seen.add(key)
        parent_fm = by_id.get(key)
        if parent_fm is None:
            break
        stems.update(parent_fm.get("serves") or [])
        parent_id = parent_fm.get("parent")
    return stems


def get_goal_rollup(docs_root: Path) -> list[dict]:
    """Roll up per-goal progress from ``serves:`` chains, on this board.

    Scans the vault for ``type: goal`` notes, then for each gathers every
    ticket on *this* board that serves it -- directly (`serves:`) or via its
    parent chain (a task inherits the goals its epic/feature ancestor
    serves). Cross-repo aggregation across every board is marginalia's job
    (the 5000-ft rollup view); a single `llpm` invocation only sees its own
    board, so this stays board-scoped by construction.

    Only ``status: stamped`` goals bind planning (``goal["stamped"]``);
    drafts render as proposals and are never flagged as a gap.
    ``unplanned_gap`` is true for a stamped goal with no serving tickets, or
    with serving tickets but none open/in-progress/review and not all of
    them done -- i.e. nothing left for an agent to pick up and the goal
    isn't actually achieved either, the trigger for a planning session with
    Ben rather than agents freelancing. A goal whose serving tickets are all
    complete/closed is NOT a gap -- that's the goal achieved, not stuck.
    """
    store = _as_store(docs_root)
    goal_notes = get_goal_notes(store)
    tickets = load_all_tickets(store, include_archive=True)
    by_id = {fm["id"].upper(): fm for _, fm, _ in tickets if fm.get("id")}

    rollup = []
    for goal_stem, goal_fm in goal_notes:
        serving = [
            (fm, effective_status(store, fm))
            for _, fm, _ in tickets
            if goal_stem in _goal_stems_served(fm, by_id)
        ]
        serving.sort(key=lambda item: item[0].get("id") or "")

        counts: dict[str, int] = {}
        for _, status in serving:
            counts[status] = counts.get(status, 0) + 1

        total = len(serving)
        done = counts.get("complete", 0) + counts.get("closed", 0)
        workable = counts.get("open", 0) + counts.get("in-progress", 0) + counts.get("review", 0)
        stamped = goal_fm.get("status") == "stamped"

        rollup.append({
            "stem": goal_stem,
            "title": goal_fm.get("title"),
            "status": goal_fm.get("status"),
            "stamped": stamped,
            "serving": [
                {"id": fm["id"], "type": fm.get("type"), "status": status}
                for fm, status in serving
            ],
            "counts": counts,
            "total": total,
            "done": done,
            "pct_done": round(100 * done / total) if total else 0,
            "unplanned_gap": stamped and (total == 0 or (workable == 0 and done < total)),
        })

    rollup.sort(key=lambda g: (not g["stamped"], g["stem"]))
    return rollup
