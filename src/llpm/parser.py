"""Frontmatter parsing, validation, and ticket discovery for LLPM."""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import yaml


# -- Constants --

VALID_STATUSES = {"draft", "planned", "open", "in-progress", "review", "complete", "closed", "deferred"}
RESOLVED_STATUSES = {"complete", "closed"}
VALID_PRIORITIES = {"low", "medium", "high"}
VALID_EFFORTS = {"trivial", "small", "medium", "large", "xlarge"}

CORE_FIELDS = {"id", "type", "title", "status", "priority", "parent", "blockers", "created", "updated", "completed", "tags"}

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


def parse_document(path: Path) -> tuple[dict, str]:
    """Parse a markdown file with YAML frontmatter.

    Returns (frontmatter_dict, body_str). Raises ValueError on malformed input.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"No frontmatter found in {path}")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Unterminated frontmatter in {path}")

    # parts[0] is empty (before first ---), parts[1] is YAML, parts[2] is body
    raw_yaml = parts[1]
    body = parts[2]
    if body.startswith("\n"):
        body = body[1:]

    data = yaml.safe_load(raw_yaml)
    if not isinstance(data, dict):
        raise ValueError(f"Frontmatter is not a mapping in {path}")

    # Normalize dates
    for key, value in data.items():
        data[key] = _normalize_value(value)

    return data, body


def write_document(path: Path, frontmatter: dict, body: str) -> None:
    """Write a markdown file with YAML frontmatter."""
    yaml_str = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False, allow_unicode=True)
    content = f"---\n{yaml_str}---\n{body}"
    path.write_text(content, encoding="utf-8")


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

    # Validate ID prefix matches type
    ticket_id = data["id"]
    ticket_type = data["type"]
    expected_prefix = _prefix_for_type(ticket_type)
    if expected_prefix and not ticket_id.startswith(expected_prefix + "-"):
        errors.append(f"ID '{ticket_id}' does not match type '{ticket_type}' (expected prefix '{expected_prefix}-')")

    # Validate list fields are lists
    for field in ("blockers", "tags"):
        val = data.get(field)
        if val is not None and not isinstance(val, list):
            errors.append(f"Field '{field}' must be a list, got {type(val).__name__}")

    return errors


def _prefix_for_type(ticket_type: str) -> str:
    """Get the ID prefix for a ticket type. Falls back to uppercase type name."""
    return TYPE_PREFIXES.get(ticket_type, ticket_type.upper())


# -- Ticket Discovery --

def find_tickets(docs_root: Path, include_archive: bool = True) -> list[Path]:
    """Find all ticket markdown files under docs_root/tickets/."""
    tickets_dir = docs_root / "tickets"
    if not tickets_dir.exists():
        return []

    results = list(tickets_dir.glob("*.md"))
    if include_archive:
        archive_dir = tickets_dir / "archive"
        if archive_dir.exists():
            results.extend(archive_dir.glob("*.md"))

    return sorted(results)


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
    results = []
    for path in find_tickets(docs_root, include_archive=include_archive):
        try:
            fm, body = parse_document(path)
            results.append((path, fm, body))
        except (ValueError, yaml.YAMLError):
            continue
    return results


# -- ID Generation --

def next_id(docs_root: Path, ticket_type: str) -> str:
    """Generate the next ticket ID for the given type.

    Scans all tickets (including archive) to find the highest existing number,
    then returns the next one. Zero-pads to 3 digits.
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
    """Check if a ticket has any unresolved blockers."""
    blockers = frontmatter.get("blockers") or []
    if not blockers:
        return False

    for blocker_id in blockers:
        path = find_ticket_by_id(docs_root, blocker_id)
        if path is None:
            # Dangling reference -- treat as blocking (shouldn't happen with validation)
            return True
        try:
            fm, _ = parse_document(path)
            if fm.get("status") not in RESOLVED_STATUSES:
                return True
        except (ValueError, yaml.YAMLError):
            return True

    return False


def get_blocker_details(docs_root: Path, frontmatter: dict) -> list[dict]:
    """Get detailed info about each blocker on a ticket."""
    blockers = frontmatter.get("blockers") or []
    details = []

    for blocker_id in blockers:
        path = find_ticket_by_id(docs_root, blocker_id)
        if path is None:
            details.append({
                "id": blocker_id,
                "status": "not found",
                "title": "???",
                "resolved": False,
            })
            continue

        try:
            fm, _ = parse_document(path)
            details.append({
                "id": fm.get("id", blocker_id),
                "status": fm.get("status", "unknown"),
                "title": fm.get("title", "???"),
                "resolved": fm.get("status") in RESOLVED_STATUSES,
            })
        except (ValueError, yaml.YAMLError):
            details.append({
                "id": blocker_id,
                "status": "parse error",
                "title": "???",
                "resolved": False,
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
