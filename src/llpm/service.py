"""Library surface for llpm: plain functions over a ``TicketStore``.

Everything llpm does to ticket data that isn't *printing* lives here, so the
CLI (``commands.py``) and the HTTP API (``api.py``) cannot drift -- both are
thin callers of the same functions. Service functions never print and never
``sys.exit``: they return plain dicts and raise the typed errors below, which
each caller renders in its own idiom (``Error: …`` + exit 1 for the CLI,
404/422/409 for the API).

FEAT-015 landed this in slices: TASK-016 the read side, TASK-017 ``set_status``,
TASK-018 ``create_ticket``/``set_fields``, TASK-019 the four edge pairs. Every
``cmd_*`` that touches ticket data is now a printer over one of these.
"""

from __future__ import annotations

import re
from datetime import date
from importlib import resources as importlib_resources
from pathlib import Path

import yaml

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

    Nothing raises this yet: no store has a write precondition. ``LocalDirStore``
    writes the file and ``MdTreeStore`` PUTs the note, both unconditionally, so
    concurrent writes to one ticket are last-writer-wins. The class and the 409
    mapping exist so that when a precondition (an ETag / ``If-Match`` on the
    vault's PUT) arrives, it becomes a store-level change and callers need no
    edit. See TASK-017's handoff.
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
    for path, fm in tickets:
        parent = fm.get("parent")
        if parent and "archive" not in path.parts:
            index.setdefault(str(parent).upper(), []).append(fm.get("id"))
    return index


def board_index(tickets) -> dict[str, dict]:
    """ID -> frontmatter, from an already-loaded ticket list.

    The blocker half of what ``children_index`` does for parents: every blocker
    is an intra-board ID, so a board that is already in memory can resolve them
    without going back to the store. On the vault that is the difference
    between one request per blocker and none.
    """
    return {
        str(fm["id"]).upper(): fm for _, fm in tickets if fm.get("id")
    }


def _derive(
    store: TicketStore, fm: dict, by_id: dict[str, dict] | None = None
) -> tuple[str, list[dict], list[dict]]:
    """``(effective_status, blocker_details, waits_details)`` in one pass.

    ``parser.effective_status`` resolves every blocker to answer one yes/no
    question, and the caller then resolves them again for their details -- two
    ``store.read`` round trips per blocker, which on the vault store is two HTTP
    requests. Resolving once and deriving the status from the details it already
    has gives identical answers for half the traffic: a dangling or unparseable
    blocker comes back ``resolved: False``, which is exactly what made
    ``is_blocked`` say True.
    """
    blocker_details = (
        parser.get_blocker_details(store, fm, by_id) if fm.get("blockers") else []
    )
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
    board_by_id: dict[str, dict] | None = None,
) -> dict:
    """Serialize a ticket to the JSON output schema.

    If body is None, it is omitted (list mode). If provided, it is included (show mode).
    ``children_by_parent`` (from ``children_index``) lets listings resolve children
    without reloading the board per ticket; ``show`` passes nothing and pays one load.
    ``board_by_id`` (from ``board_index``) does the same for blockers.
    """
    eff_status, blocker_details, waits_details = _derive(store, fm, board_by_id)
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

def _begin_read_scope(store: TicketStore) -> None:
    """Open a read scope on the store -- one service call is one scope.

    A store is allowed to cache reads inside a scope (``MdTreeStore`` keeps
    resolved ``waits_on`` stems, so a board listing resolves each distinct one
    once), and must not carry that cache across scopes: ``llpm serve`` and
    marginalia's ``/api/llpm`` mount keep one store per board alive for the life
    of the process, where a per-process cache froze a cross-board target's
    status until restart (TASK-021).

    Every path that derives blockers or waits starts at ``load_board`` (whole
    board) or ``read_ticket`` (one ticket), so opening the scope in those two
    covers the surface without a decorator on every function -- and without
    reopening it mid-board, which would cost one foreign read per ticket
    instead of one per distinct stem.

    Stores without the method no-op, like ``read_foreign``/``list_boards``.
    """
    fn = getattr(store, "begin_read_scope", None)
    if fn is not None:
        fn()


def load_board(store: TicketStore, *, include_archive: bool = False) -> list[dict]:
    """Every ticket on the board, serialized, sorted priority high->low then ID.

    The children index is built once for the whole board (TASK-012), so this is
    one pass over the store however many tickets there are. Callers that want a
    subset filter the result -- ``filter_tickets`` is pure, and a filtered
    listing still resolves children against the *whole* board.
    """
    _begin_read_scope(store)
    tickets = parser.load_all_tickets(store, include_archive=include_archive)
    index = children_index(tickets)
    by_id = board_index(tickets)
    dicts = [
        ticket_dict(store, ref, fm, children_by_parent=index, board_by_id=by_id)
        for ref, fm in tickets
    ]
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
    _begin_read_scope(store)
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


# -- Write operations --------------------------------------------------------

def _today() -> str:
    """Today as YYYY-MM-DD -- the fallback when a caller supplies no date.

    The CLI has its own mockable ``commands._today`` that the whole CLI test
    suite patches, and it passes the result in as ``today=``. A write that
    arrives over HTTP has no such clock and stamps the server's day.
    """
    return date.today().isoformat()


def write_ticket(store: TicketStore, ref: Path, fm: dict, body: str) -> None:
    """Chokepoint for every ticket mutation: stamps the ownership key
    (``managed_by: llpm``, decision 6 -- the k8s kind/managed-by split) so
    tickets self-describe their write path, then writes through the store."""
    fm.setdefault("managed_by", "llpm")
    store.write(ref, fm, body)


def _merge_commits(existing: list[str], new_shas: list[str]) -> list[str]:
    """Append new SHAs, prefix-aware so short and full forms of the same
    commit don't both land in the list. Never removes entries."""
    merged = list(existing)
    for sha in new_shas:
        sha = sha.strip()
        if not sha:
            continue
        if not any(m.startswith(sha) or sha.startswith(m) for m in merged):
            merged.append(sha)
    return merged


def set_status(
    store: TicketStore,
    ticket_id: str,
    status: str,
    *,
    awaiting: str | None = None,
    commits: list[str] | None = None,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    """Change a ticket's status -- ``llpm status`` and ``POST …/status``.

    One read, one write. ``commits`` are SHAs to record, already gathered by
    the caller: the CLI harvests them from its CWD git repo (``commands.
    _harvest_commits``) and the API takes them in the request body, because the
    server has no checkout and must never run git.

    Returns the transition, not a ticket::

        {"id", "previous_status", "status", "commits_captured"}

    plus ``"ticket"`` (the serialized ticket dict) when ``include_ticket`` is
    set. It is opt-in because serializing re-derives blockers and waits and
    loads the whole board for ``children`` -- dozens of vault round trips that
    the CLI, which only prints ``ID: old -> new``, has no use for.

    Raises ``NotFound`` for an unknown id and ``Invalid`` for a status or
    ``awaiting`` value llpm's rules reject, with the message text the CLI has
    always printed.
    """
    if status not in parser.VALID_STATUSES:
        raise Invalid(
            f"Invalid status: '{status}'. Must be one of: "
            f"{', '.join(sorted(parser.VALID_STATUSES))}"
        )

    # awaiting: -- review-queue discriminator (FEAT-012). Only meaningful
    # alongside a transition INTO review -- error loudly otherwise rather
    # than silently accepting a value that would have no effect.
    if awaiting is not None:
        if status != "review":
            raise Invalid(
                f"--awaiting is only valid when the target status is "
                f"'review' (got '{status}')."
            )
        if awaiting not in parser.VALID_AWAITING:
            raise Invalid(
                f"Invalid awaiting '{awaiting}'. Must be one of: "
                f"{', '.join(sorted(parser.VALID_AWAITING))}"
            )

    ref, fm, body = read_ticket(store, ticket_id)
    previous_status = fm["status"]
    stamp = today or _today()

    fm["status"] = status
    fm["updated"] = stamp

    # Self-clear: every transition drops any prior 'awaiting', then re-adds
    # it only if one was passed on THIS call (already constrained to
    # status == "review" above).
    fm.pop("awaiting", None)
    if awaiting is not None:
        fm["awaiting"] = awaiting

    if status == "complete" and not fm.get("completed"):
        fm["completed"] = stamp

    # Commit capture (FEAT-007). Which SHAs reach here is the caller's call;
    # merging them onto commits[] is the rule.
    captured = 0
    if commits:
        existing = list(fm.get("commits") or [])
        merged = _merge_commits(existing, list(commits))
        captured = len(merged) - len(existing)
        if merged:
            fm["commits"] = merged

    write_ticket(store, ref, fm, body)

    result = {
        "id": fm["id"],
        "previous_status": previous_status,
        "status": status,
        "commits_captured": captured,
    }
    if include_ticket:
        result["ticket"] = ticket_dict(store, ref, fm, body=body)
    return result


# -- Create (TASK-018) -------------------------------------------------------

_MAX_CREATE_RETRIES = 3


def _slugify(title: str) -> str:
    """Convert title to UPPER_SNAKE_CASE for filenames."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", title)
    return "_".join(cleaned.upper().split())


def _as_list(value) -> list[str]:
    """``"a,b"`` or ``["a", "b"]`` -> ``["a", "b"]``.

    The two callers spell a list differently: argparse collects one
    comma-joined string, JSON carries an array. Both mean the same thing, so
    the service takes either rather than making one caller pre-convert.
    """
    if value is None:
        return []
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [s for s in (str(p).strip() for p in parts) if s]


def resolve_template(store: TicketStore, ticket_type: str) -> str:
    """Template text for a ticket type: store first, then bundled.

    Store-first is what lets a board override a template (a vault
    ``templates.*`` note, or the copies ``llpm init`` puts in a local docs
    root) without every board needing to be seeded.
    """
    text = store.read_blob(f"templates/{ticket_type}.md")
    if text is not None:
        return text
    bundled = Path(str(importlib_resources.files("llpm") / "templates" / f"{ticket_type}.md"))
    if bundled.exists():
        return bundled.read_text(encoding="utf-8")
    raise Invalid(f"No template found for type '{ticket_type}'.")


def inject_provenance(content: str, origin: str, created_by: str | None) -> str:
    """Append provenance + ownership keys to rendered template frontmatter.

    Textual insertion (not re-serialization) so template enum-hint comments
    survive into the created ticket. Keys the template already carries are
    left alone.
    """
    try:
        fm, _ = parser.parse_text(content, source="<template>")
    except (ValueError, yaml.YAMLError):
        return content  # malformed template: let create fail/succeed as before

    lines = []
    if "managed_by" not in fm:
        lines.append("managed_by: llpm")
    if "origin" not in fm:
        lines.append(f"origin: {origin}")
    if created_by and "created_by" not in fm:
        # created_by is caller input -- serialize the one line properly
        lines.append(yaml.safe_dump({"created_by": created_by}, default_flow_style=False).strip())
    if "commits" not in fm:
        lines.append("commits: []")
    if not lines:
        return content

    parts = content.split("---", 2)
    fm_block = parts[1].rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    return "---" + fm_block + "---" + parts[2]


def _render_template(
    template_text: str,
    *,
    ticket_id: str,
    title: str,
    today: str,
    status: str,
    priority: str,
    effort: str | None,
    parent: str | None,
    serves: list[str],
    tags: list[str],
    requires_human: bool,
    body: str | None,
    origin: str,
    created_by: str | None,
) -> str:
    """Fill a template by string substitution, not by re-serializing YAML.

    The templates carry enum-hint comments (``status: draft  # draft|open|…``)
    that a parse/dump round trip would eat, so each optional field is a
    targeted replacement of the default the template ships with.
    """
    content = template_text
    content = content.replace("__ID__", ticket_id)
    content = content.replace("__TITLE__", title)
    content = content.replace("__DATE__", today)

    # Replace status/priority in the template line (preserve comment)
    content = re.sub(
        r"^(status:\s*)draft(\s*#.*)$",
        rf"\g<1>{status}\2",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r"^(priority:\s*)medium(\s*#.*)$",
        rf"\g<1>{priority}\2",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if effort:
        content = re.sub(
            r"^(effort:\s*)null(\s*#.*)$",
            rf"\g<1>{effort}\2",
            content,
            count=1,
            flags=re.MULTILINE,
        )
    if parent:
        content = content.replace("parent: null", f"parent: {parent}", 1)
    if serves:
        content = content.replace("serves: []", "serves: [" + ", ".join(serves) + "]", 1)
    if tags:
        content = content.replace("tags: []", "tags: [" + ", ".join(tags) + "]", 1)
    if requires_human:
        content = content.replace("requires_human: false", "requires_human: true", 1)

    if body:
        # Split on the second --- to get frontmatter vs body
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[0] + "---" + parts[1] + "---\n" + body

    return inject_provenance(content, origin, created_by)


def create_ticket(
    store: TicketStore,
    ticket_type: str,
    title: str,
    *,
    body: str | None = None,
    parent: str | None = None,
    priority: str | None = None,
    effort: str | None = None,
    tags=None,
    requires_human: bool = False,
    origin: str = "agent",
    created_by: str | None = None,
    serves=None,
    triage: bool = False,
    auto_approve=(),
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    """File a new ticket -- ``llpm create`` and ``POST /{repo}/tickets``.

    ``origin`` defaults to ``agent`` because that is the safe assumption for a
    caller that didn't say: agent-origin tickets land ``draft`` unless their
    type is on ``auto_approve``. Resolving provenance from the *environment*
    (``LLPM_ORIGIN``, ``LLPM_CREATED_BY``, a git user) is deliberately CLI-only
    -- over HTTP the caller must say who it is, so ``commands`` resolves it and
    passes the answer in.

    ``auto_approve`` is the board's ``[intake] auto_approve`` list (FEAT-011,
    policy-as-data in ``.llpm/config.toml``). "Kind" for that policy is the
    ticket ``type`` -- there is no separate kind taxonomy in the schema. The
    list is a parameter rather than something read from the store because the
    config lives in the repo checkout, which a server need not have; a caller
    that passes nothing gets the conservative answer (agent -> draft).

    Goal attachment is never enforced here: creation always succeeds whatever
    ``serves``/``parent``/``triage`` say (ruling from Ben+fable 2026-08-01).
    ``llpm orphans`` is the pull-based report that surfaces the gap.

    Returns ``{"id", "type", "title", "status", "path"}`` -- plus ``"ticket"``
    (the full serialized dict) when ``include_ticket`` is set, which the router
    answers with and the CLI printer has no use for.
    """
    title = (title or "").strip()
    if not title:
        raise Invalid("Title is required.")

    if origin not in parser.VALID_ORIGINS:
        raise Invalid(
            f"Invalid origin '{origin}'. Must be one of: "
            f"{', '.join(sorted(parser.VALID_ORIGINS))}"
        )
    if priority is not None and priority not in parser.VALID_PRIORITIES:
        raise Invalid(
            f"Invalid priority '{priority}'. Must be one of: "
            f"{', '.join(sorted(parser.VALID_PRIORITIES))}"
        )
    if effort is not None and effort not in parser.VALID_EFFORTS:
        raise Invalid(
            f"Invalid effort '{effort}'. Must be one of: "
            f"{', '.join(sorted(parser.VALID_EFFORTS))}"
        )

    serve_list = _as_list(serves)
    if serve_list and ticket_type not in parser.SERVES_TYPES:
        raise Invalid(
            f"'serves' is only valid on epics/features ('{ticket_type}' "
            f"cannot carry it). Tasks serve goals via their parent."
        )

    tag_list = _as_list(tags)
    if triage and "triage" not in tag_list:
        tag_list.append("triage")

    template_text = resolve_template(store, ticket_type)

    if parent and not store.exists(parent):
        raise Invalid(f"Parent ticket '{parent}' not found.")

    # A body means someone already wrote the spec, so the ticket is workable;
    # a bare template body is a placeholder, hence draft. The intake policy
    # then overrides that for unapproved agent-origin kinds (FEAT-011).
    status = "open" if body else "draft"
    if origin == "agent" and ticket_type not in (auto_approve or ()):
        status = "draft"

    stamp = today or _today()

    for _ in range(_MAX_CREATE_RETRIES):
        ticket_id = parser.next_id(store, ticket_type)
        filename = f"{ticket_id}_{_slugify(title)}.md"
        content = _render_template(
            template_text,
            ticket_id=ticket_id,
            title=title,
            today=stamp,
            status=status,
            priority=priority or "medium",
            effort=effort,
            parent=parent,
            serves=serve_list,
            tags=tag_list,
            requires_human=requires_human,
            body=body,
            origin=origin,
            created_by=created_by,
        )
        try:
            ref = store.create_exclusive(filename, content)
        except FileExistsError:
            continue  # another agent took this id between next_id and create

        result = {
            "id": ticket_id,
            "type": ticket_type,
            "title": title,
            "status": status,
            "path": str(ref),
        }
        if include_ticket:
            # Serialize what we just wrote rather than reading it back: a
            # brand-new ticket has no children, so the board load `ticket_dict`
            # would otherwise pay for is pure waste.
            fm, text = parser.parse_text(content, source=filename)
            result["ticket"] = ticket_dict(store, ref, fm, body=text,
                                           children_by_parent={})
        return result

    raise Conflict(
        f"Could not create ticket after {_MAX_CREATE_RETRIES} retries (ID collision)."
    )


# -- set_fields (TASK-018) ---------------------------------------------------

# Written by the system, never by a caller: identity, the dates llpm stamps,
# and the FEAT-007 provenance keys.
FORBIDDEN_FIELDS = frozenset({
    "id", "type", "created", "updated", "completed",
    "origin", "created_by", "commits", "managed_by",
})

# Settable, but only through the command that owns the rule. The hints name
# CLI commands because that is llpm's own wording for the mistake; an HTTP
# caller reads them as "this field has a dedicated endpoint".
FIELD_REDIRECTS = {
    "status": "Use 'llpm status'.",
    "blockers": "Use 'llpm blocker'.",
    "serves": "Use 'llpm serves'.",
    "waits_on": "Use 'llpm waits'.",
    "after": "Use 'llpm after'.",
    "awaiting": "Use 'llpm status <id> review --awaiting <value>'.",
}

# `set` values arrive as strings from the CLI. Numeric-looking ones become
# numbers (so `hours=9` stays numeric across edits) except in known text
# fields -- and a leading zero ("007") is text, not a number.
_TEXT_FIELDS = {"title", "parent", "branch", "origin_request", "milestone",
                "batch", "resource"}
_INT_RE = re.compile(r"^-?(?:0|[1-9]\d*)$")
_FLOAT_RE = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")


def _coerce_number(field: str, value):
    if not isinstance(value, str) or field in _TEXT_FIELDS:
        return value
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        return float(value)
    return value


def _is_null(value) -> bool:
    """JSON ``null`` and the CLI's spelling of it ("null"/"none")."""
    return value is None or (isinstance(value, str) and value.lower() in ("null", "none"))


def _coerce_field(store: TicketStore, field: str, value):
    """Validate and normalize one ``field=value`` assignment.

    Handles both spellings of a value: the CLI's strings and JSON's own types
    (a real bool for ``requires_human``, an array for ``tags``, a number for
    ``hours``), so neither caller has to pre-convert.
    """
    if field == "priority" and value not in parser.VALID_PRIORITIES:
        raise Invalid(
            f"Invalid priority '{value}'. Must be one of: "
            f"{', '.join(sorted(parser.VALID_PRIORITIES))}"
        )

    if field == "effort":
        if _is_null(value):
            return None
        if value not in parser.VALID_EFFORTS:
            raise Invalid(
                f"Invalid effort '{value}'. Must be one of: "
                f"{', '.join(sorted(parser.VALID_EFFORTS))}"
            )

    if field == "model_tier":
        if _is_null(value):
            return None
        if value not in parser.VALID_MODEL_TIERS:
            raise Invalid(
                f"Invalid model_tier '{value}'. Must be one of: "
                f"{', '.join(sorted(parser.VALID_MODEL_TIERS))}"
            )

    if field == "tags":
        return _as_list(None if _is_null(value) else value)

    if field == "requires_human":
        if isinstance(value, bool):
            return value
        if _is_null(value):
            return False
        return str(value).lower() in ("true", "yes", "1")

    if _is_null(value):
        return None

    value = _coerce_number(field, value)

    if field == "parent" and not store.exists(str(value)):
        raise Invalid(f"Parent ticket '{value}' not found.")

    return value


def set_fields(
    store: TicketStore,
    ticket_id: str,
    fields: dict,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    """Set simple frontmatter fields -- ``llpm set`` and ``PATCH …/{id}``.

    Every field is validated before any is applied, so a request with one bad
    value changes nothing. Fields that belong to another command are refused
    rather than quietly redirected: ``status``, the four edge lists and
    ``awaiting`` each have their own entry point, and the provenance keys are
    the system's to write.

    Returns ``{"id", "changes": [{"field", "value", "previous"}, …]}`` -- the
    CLI prints one line per change -- plus ``"ticket"`` when asked.
    """
    if not fields:
        raise Invalid("No fields to set.")

    for field in fields:
        if field in FORBIDDEN_FIELDS:
            raise Invalid(f"Cannot set '{field}' -- managed automatically.")
        if field in FIELD_REDIRECTS:
            raise Invalid(f"Cannot set '{field}' via 'set'. {FIELD_REDIRECTS[field]}")

    ref, fm, body = read_ticket(store, ticket_id)

    changes = [
        {"field": field, "value": _coerce_field(store, field, value),
         "previous": fm.get(field)}
        for field, value in fields.items()
    ]

    for change in changes:
        fm[change["field"]] = change["value"]
    fm["updated"] = today or _today()
    write_ticket(store, ref, fm, body)

    result = {"id": fm["id"], "changes": changes}
    if include_ticket:
        result["ticket"] = ticket_dict(store, ref, fm, body=body)
    return result


# -- Edges (TASK-019) --------------------------------------------------------
#
# Four pairs over one shape: read the ticket, validate the target, append to (or
# drop from) one frontmatter list, write. Each returns
# ``{"id", "target", "changed"}`` -- ``changed: False`` is an add that was
# already there, which is a no-op and not an error -- plus whatever extra the
# CLI needs to narrate it, plus ``"ticket"`` when the router asks.
#
# The vocabulary stays deliberately small: `blockers` (hard, intra-board IDs),
# `waits_on` (cross-board vault stems), `after` (soft precedence, never blocks),
# `serves` (epic/feature -> goal-note stems).


def _edge_result(
    store: TicketStore,
    ref: Path,
    fm: dict,
    body: str,
    target: str,
    changed: bool,
    include_ticket: bool,
    **extra,
) -> dict:
    result = {"id": fm["id"], "target": target, "changed": changed, **extra}
    if include_ticket:
        result["ticket"] = ticket_dict(store, ref, fm, body=body)
    return result


def _write_edge(store: TicketStore, ref: Path, fm: dict, body: str, today: str | None) -> None:
    fm["updated"] = today or _today()
    write_ticket(store, ref, fm, body)


def blocker_add(
    store: TicketStore,
    ticket_id: str,
    blocker_id: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    """Add a hard dependency. The blocker must be a real ticket on this board:
    ``blockers`` never holds free text, because ``blocked`` is derived from
    resolving each one."""
    ref, fm, body = read_ticket(store, ticket_id)
    upper = blocker_id.upper()

    if upper == fm["id"].upper():
        raise Invalid(f"Ticket {fm['id']} cannot block itself.")
    if not store.exists(blocker_id):
        raise Invalid(f"Ticket '{blocker_id}' not found.")

    blockers = fm.get("blockers") or []
    changed = not any(b.upper() == upper for b in blockers)
    if changed:
        fm["blockers"] = blockers + [upper]
        _write_edge(store, ref, fm, body, today)

    return _edge_result(store, ref, fm, body, upper, changed, include_ticket)


def blocker_rm(
    store: TicketStore,
    ticket_id: str,
    blocker_id: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    ref, fm, body = read_ticket(store, ticket_id)
    upper = blocker_id.upper()

    blockers = fm.get("blockers") or []
    if not any(b.upper() == upper for b in blockers):
        raise NotFound(f"'{blocker_id}' is not a blocker on {fm['id']}.")

    fm["blockers"] = [b for b in blockers if b.upper() != upper]
    _write_edge(store, ref, fm, body, today)
    return _edge_result(store, ref, fm, body, upper, True, include_ticket)


def _after_reaches(store: TicketStore, start_id: str, target_id: str) -> bool:
    """True if following ``after`` edges from start reaches target -- the
    soft-cycle probe for ``after_add``."""
    target = target_id.upper()
    seen: set[str] = set()
    frontier = [start_id.upper()]
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        found = store.read(current)
        if found is None:
            continue
        _, fm, _ = found
        frontier.extend(a.upper() for a in fm.get("after") or [])
    return False


def after_add(
    store: TicketStore,
    ticket_id: str,
    other_id: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    """Add soft precedence -- advice to a scheduler, never a block.

    Cycles are reported (``cycle_warning``), not refused: an edge that only
    advises can't deadlock anything, and refusing would make ordering two
    tickets depend on which one you touched first.
    """
    ref, fm, body = read_ticket(store, ticket_id)
    upper = other_id.upper()

    # Soft edge, but still a real ticket reference -- same rule as blockers.
    # A self-edge is NOT refused the way a self-blocker is: it is a cycle, and
    # cycles on this edge warn (see below). Only the hard edge can deadlock.
    if not store.exists(other_id):
        raise Invalid(f"Ticket '{other_id}' not found.")

    after = fm.get("after") or []
    changed = not any(a.upper() == upper for a in after)
    cycle = False
    if changed:
        cycle = _after_reaches(store, other_id, fm["id"])
        fm["after"] = after + [upper]
        _write_edge(store, ref, fm, body, today)

    return _edge_result(store, ref, fm, body, upper, changed, include_ticket,
                        cycle_warning=cycle)


def after_rm(
    store: TicketStore,
    ticket_id: str,
    other_id: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    ref, fm, body = read_ticket(store, ticket_id)
    upper = other_id.upper()

    after = fm.get("after") or []
    if not any(a.upper() == upper for a in after):
        raise NotFound(f"{fm['id']} is not ordered after '{other_id}'.")

    fm["after"] = [a for a in after if a.upper() != upper]
    _write_edge(store, ref, fm, body, today)
    return _edge_result(store, ref, fm, body, upper, True, include_ticket)


def waits_add(
    store: TicketStore,
    ticket_id: str,
    stem: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    """Add a cross-board dependency, addressed by full vault stem.

    The target's state is probed *after* the write and reported
    (``state``/``target_status``) as feedback, never as a gate: a stem that
    doesn't resolve yet is exactly the case this edge exists for.
    """
    ref, fm, body = read_ticket(store, ticket_id)
    target = stem.strip()

    # Bare ticket IDs are intra-board dependencies -- that's what blockers are.
    if parser.TICKET_ID_RE.match(target.upper()):
        raise Invalid(
            "'waits_on' holds full vault stems "
            "(e.g. repos.marginalia.llpm.features.FEAT-010). "
            "For same-board dependencies use 'llpm blocker'."
        )

    waits = fm.get("waits_on") or []
    changed = target not in waits
    state = target_status = None
    if changed:
        fm["waits_on"] = waits + [target]
        _write_edge(store, ref, fm, body, today)
        state, target_fm = parser._read_foreign(store, target)
        target_status = target_fm.get("status") if target_fm else None

    return _edge_result(store, ref, fm, body, target, changed, include_ticket,
                        state=state, target_status=target_status)


def waits_rm(
    store: TicketStore,
    ticket_id: str,
    stem: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    ref, fm, body = read_ticket(store, ticket_id)
    target = stem.strip()

    waits = fm.get("waits_on") or []
    if target not in waits:
        raise NotFound(f"{fm['id']} does not wait on '{target}'.")

    fm["waits_on"] = [w for w in waits if w != target]
    _write_edge(store, ref, fm, body, today)
    return _edge_result(store, ref, fm, body, target, True, include_ticket)


def serves_add(
    store: TicketStore,
    ticket_id: str,
    goal_stem: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    """Point an epic/feature at a goal note. Soft-validated: the stem is a
    cross-repo vault address llpm can't always resolve, so only the shape is
    checked (a ticket ID here is the common mistake)."""
    ref, fm, body = read_ticket(store, ticket_id)
    target = goal_stem.strip()

    if fm.get("type") not in parser.SERVES_TYPES:
        raise Invalid(
            f"'serves' is only valid on epics/features "
            f"({fm['id']} is a {fm.get('type')}). Tasks serve goals via their parent."
        )
    if parser.TICKET_ID_RE.match(target.upper()):
        raise Invalid(
            "'serves' holds full vault stems to goal notes "
            "(e.g. goals.unified-agent-platform), not ticket IDs. "
            "For ticket dependencies use 'llpm blocker'."
        )

    serves = fm.get("serves") or []
    changed = target not in serves
    if changed:
        fm["serves"] = serves + [target]
        _write_edge(store, ref, fm, body, today)

    return _edge_result(store, ref, fm, body, target, changed, include_ticket)


def serves_rm(
    store: TicketStore,
    ticket_id: str,
    goal_stem: str,
    *,
    today: str | None = None,
    include_ticket: bool = False,
) -> dict:
    ref, fm, body = read_ticket(store, ticket_id)
    target = goal_stem.strip()

    serves = fm.get("serves") or []
    if target not in serves:
        raise NotFound(f"{fm['id']} does not serve '{target}'.")

    fm["serves"] = [s for s in serves if s != target]
    _write_edge(store, ref, fm, body, today)
    return _edge_result(store, ref, fm, body, target, True, include_ticket)
