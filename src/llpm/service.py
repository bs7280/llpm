"""Library surface for llpm: plain functions over a ``TicketStore``.

Everything llpm does to ticket data that isn't *printing* lives here, so the
CLI (``commands.py``) and the HTTP API (``api.py``) cannot drift -- both are
thin callers of the same functions. Service functions never print and never
``sys.exit``: they return plain dicts and raise the typed errors below, which
each caller renders in its own idiom (``Error: …`` + exit 1 for the CLI,
404/422/409 for the API).

This is FEAT-015's first slice (TASK-016) and covers the read side only. The
writes -- ``set_status``, ``create_ticket``, ``set_fields`` and the edge pairs
-- belong here too and land in the slices that follow.
"""

from __future__ import annotations

from pathlib import Path

from . import parser
from .store import TicketStore


# -- Typed errors ------------------------------------------------------------
#
# One class per HTTP status the router needs, so api.py maps outcomes without
# knowing anything about llpm's rules.

class ServiceError(Exception):
    """Base class for every failure the service reports to its callers."""


class NotFound(ServiceError):
    """No ticket (or board) with that id. -> 404."""


class Invalid(ServiceError):
    """A well-formed request that llpm's own rules reject. -> 422.

    The message is llpm's existing wording, so the CLI and the API say the
    same thing about the same mistake.
    """


class Conflict(ServiceError):
    """The store refused a write because the note moved underneath us. -> 409.

    Unraised by the read slice; the write slices raise it.
    """


# -- Ticket serialization ----------------------------------------------------

# Plan-structure fields (contract: vault stem
# repos.coaching_platfrom_saas.llpm.milestones) are free-form frontmatter, so
# the JSON shape normalizes them the way the marginalia board does: `resource`
# is stored comma-separated (set only splits tags) and `hours` may predate
# numeric coercion in `set`.
def _split_keys(value) -> list[str]:
    if value is None:
        return []
    parts = value if isinstance(value, list) else str(value).split(",")
    return [s for s in (str(p).strip() for p in parts) if s]


def _as_hours(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def children_index(tickets) -> dict[str, list[str]]:
    """parent id -> child ids, built once from an already-loaded ticket list.

    TASK-012: ``get_children`` reloads the whole board per call, which made every
    JSON listing O(n^2) in vault requests (153 tickets ~ 24k requests, ~16 min).
    Archived tickets are excluded, matching ``get_children(include_archive=False)``.
    """
    index: dict[str, list[str]] = {}
    for path, fm, *_ in tickets:
        parent = fm.get("parent")
        if parent and "archive" not in path.parts:
            index.setdefault(str(parent).upper(), []).append(fm.get("id"))
    return index


def _derive(store: TicketStore, fm: dict) -> tuple[str, list[dict], list[dict]]:
    """``(effective_status, blocker_details, waits_details)`` in one pass.

    ``parser.effective_status`` resolves every blocker to answer one yes/no
    question, and the caller then resolves them again for their details -- two
    ``store.read`` round trips per blocker, which on the vault store is two HTTP
    requests. Resolving once and deriving the status from the details it already
    has gives identical answers for half the traffic: a dangling or unparseable
    blocker comes back ``resolved: False``, which is exactly what made
    ``is_blocked`` say True.
    """
    blocker_details = parser.get_blocker_details(store, fm) if fm.get("blockers") else []
    waits_details = parser.get_waits_on_details(store, fm)

    stored = fm.get("status", "draft")
    if stored in parser.TERMINAL_STATUSES:
        return stored, blocker_details, waits_details

    blocked = (
        any(not d["resolved"] for d in blocker_details)
        or any(d["blocking"] for d in waits_details)
    )
    return ("blocked" if blocked else stored), blocker_details, waits_details


def ticket_dict(
    store: TicketStore,
    path: Path,
    fm: dict,
    body: str | None = None,
    children_by_parent: dict[str, list[str]] | None = None,
) -> dict:
    """Serialize a ticket to the JSON output schema.

    If body is None, it is omitted (list mode). If provided, it is included (show mode).
    ``children_by_parent`` (from ``children_index``) lets listings resolve children
    without reloading the board per ticket; ``show`` passes nothing and pays one load.
    """
    eff_status, blocker_details, waits_details = _derive(store, fm)
    is_blocked = eff_status == "blocked"

    if children_by_parent is not None:
        child_ids = list(children_by_parent.get(str(fm["id"]).upper(), []))
    else:
        child_ids = [c["id"] for c in parser.get_children(store, fm["id"])]

    archived = "archive" in path.parts

    result = {
        "id": fm["id"],
        "type": fm["type"],
        "title": fm["title"],
        "status": fm["status"],
        "effective_status": eff_status,
        "is_blocked": is_blocked,
        "awaiting": fm.get("awaiting"),
        "priority": fm["priority"],
        "effort": fm.get("effort"),
        "model_tier": fm.get("model_tier"),
        "parent": fm.get("parent"),
        "children": child_ids,
        "blockers": [
            {"id": d["id"], "resolved": d["resolved"]}
            for d in blocker_details
        ],
        "serves": fm.get("serves") or [],
        "waits_on": waits_details,
        "after": fm.get("after") or [],
        "tags": fm.get("tags") or [],
        "requires_human": fm.get("requires_human", False),
        "milestone": fm.get("milestone"),
        "batch": fm.get("batch"),
        "resource": _split_keys(fm.get("resource")),
        "hours": _as_hours(fm.get("hours")),
        "origin": fm.get("origin"),
        "created_by": fm.get("created_by"),
        "commits": fm.get("commits") or [],
        "managed_by": fm.get("managed_by"),
        "created": fm.get("created"),
        "updated": fm.get("updated"),
        "completed": fm.get("completed"),
        "archived": archived,
        "path": str(path),
    }

    if body is not None:
        result["body"] = body
        result["body_html"] = None

    return result


# Every key ``ticket_dict`` puts in a listing entry -- what ``fields=`` may ask
# for. ``body``/``body_html`` are deliberately absent: listings carry no bodies.
# Pinned against ``ticket_dict`` by a test, so the two can't drift apart.
TICKET_FIELDS = frozenset({
    "id", "type", "title", "status", "effective_status", "is_blocked",
    "awaiting", "priority", "effort", "model_tier", "parent", "children",
    "blockers", "serves", "waits_on", "after", "tags", "requires_human",
    "milestone", "batch", "resource", "hours", "origin", "created_by",
    "commits", "managed_by", "created", "updated", "completed", "archived",
    "path",
})


_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _priority_key(fm: dict) -> tuple[int, str]:
    """Sort key for list/board output: priority high->low, then ID.

    Works on a raw frontmatter dict or on a ``ticket_dict`` result -- both
    carry ``priority`` and ``id``.
    """
    return (_PRIORITY_RANK.get(fm.get("priority"), 1), fm.get("id") or "")


# -- Read operations ---------------------------------------------------------

def load_board(store: TicketStore, *, include_archive: bool = False) -> list[dict]:
    """Every ticket on the board, serialized, sorted priority high->low then ID.

    The children index is built once for the whole board (TASK-012), so this is
    one pass over the store however many tickets there are. Callers that want a
    subset filter the result -- ``filter_tickets`` is pure, and a filtered
    listing still resolves children against the *whole* board.
    """
    tickets = parser.load_all_tickets(store, include_archive=include_archive)
    index = children_index(tickets)
    dicts = [ticket_dict(store, ref, fm, children_by_parent=index) for ref, fm, _ in tickets]
    dicts.sort(key=_priority_key)
    return dicts


def check_fields(fields: list[str] | None) -> None:
    """Reject a ``fields=`` selection naming something a listing doesn't carry.

    Separate from the filtering so callers can fail a typo *before* loading a
    board -- against the vault that load is hundreds of HTTP requests, and a
    misspelled field name is worth naming rather than silently dropping.
    """
    if fields is None:
        return
    unknown = [f for f in fields if f not in TICKET_FIELDS]
    if unknown:
        raise Invalid(
            f"Unknown field(s): {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(TICKET_FIELDS))}"
        )


def filter_tickets(
    tickets: list[dict],
    *,
    status: str | None = None,
    type: str | None = None,
    parent: str | None = None,
    fields: list[str] | None = None,
) -> list[dict]:
    """Filter and trim an already-loaded board. Pure -- no store access.

    ``status`` matches the *effective* status, so ``status=blocked`` finds
    tickets whose stored status is something else. ``fields`` trims each entry
    to the named keys (token cost is the point: a listing is often read by an
    agent that only wants ids and titles).
    """
    check_fields(fields)

    results = []
    for t in tickets:
        if status and t["effective_status"] != status:
            continue
        if type and t["type"] != type:
            continue
        if parent and str(t["parent"] or "").upper() != parent.upper():
            continue
        results.append(t)

    if fields is not None:
        results = [{f: t[f] for f in fields if f in t} for t in results]
    return results


def list_tickets(
    store: TicketStore,
    *,
    status: str | None = None,
    type: str | None = None,
    parent: str | None = None,
    include_archive: bool = False,
    fields: list[str] | None = None,
) -> list[dict]:
    """The listing behind ``llpm list --json`` and ``GET /{repo}/tickets``."""
    check_fields(fields)  # before the board load, so a typo costs nothing
    board = load_board(store, include_archive=include_archive)
    return filter_tickets(board, status=status, type=type, parent=parent, fields=fields)


def read_ticket(store: TicketStore, ticket_id: str) -> tuple[Path, dict, str]:
    """Locate and parse one ticket, or raise ``NotFound``.

    The raw ``(ref, frontmatter, body)`` triple: what every mutation starts
    from, and what the CLI's text renderers need (they print blocker titles and
    statuses, which the compact ticket dict deliberately drops).
    """
    found = store.read(ticket_id)
    if found is None:
        raise NotFound(f"Ticket '{ticket_id}' not found.")
    return found


def get_ticket(store: TicketStore, ticket_id: str, *, body: bool = True) -> dict:
    """One serialized ticket -- ``llpm show --json`` / ``GET /{repo}/tickets/{id}``."""
    ref, fm, text = read_ticket(store, ticket_id)
    return ticket_dict(store, ref, fm, body=text if body else None)


def list_boards(store: TicketStore) -> list[str]:
    """Repo names that have an llpm board, for a store that can see the whole
    vault. Stores that only know their own board (LocalDirStore, and anything
    predating ``list_boards``) answer ``[]`` rather than raising -- the same
    degrade rule ``read_foreign`` and ``subnotes`` follow.
    """
    fn = getattr(store, "list_boards", None)
    if fn is None:
        return []
    return fn()
