"""MCP tools over the service layer -- FEAT-016.

Agents mutate tickets three ways today: the CLI, raw vault writes, and REST.
This is the surface a harness session actually gets handed, and the point of it
is that it is not a fourth set of rules: every tool is exactly one ``service.py``
function, so a tool call applies what a CLI call applies -- intake policy,
provenance, the edge vocabulary, status transitions -- and fails with llpm's own
message when it applies.

Stdlib only, deliberately: ``llpm mcp`` has to run wherever ``llpm`` runs, and
the core install is pyyaml-only (fastapi is the ``llpm[api]`` extra). The
protocol needed for a tools-only server is small -- ``initialize``,
``tools/list``, ``tools/call``, ``ping`` over JSON-RPC 2.0 -- so ``Session``
takes a decoded message and answers one, and the transport is whatever hands it
messages:

- ``llpm mcp`` frames them as newline-delimited JSON on stdin/stdout (the MCP
  stdio transport), serving the one board the usual config discovery finds.
- ``api.make_router`` mounts the same session at ``POST /{repo}/mcp`` (the
  streamable-HTTP transport, JSON responses, no SSE), so ``llpm serve`` already
  serves the remote option and marginalia's ``/api/llpm`` mount gets it free.

What the two transports do NOT share is identity. Over stdio the client
introduces itself in the handshake and the server runs in a checkout, so
``created_by`` falls back to that name and the board's ``[intake] auto_approve``
list applies, exactly as for the CLI. Over HTTP there is no shell, no checkout
and (deliberately) no session state, so the caller names ``created_by`` in the
arguments and an agent-origin create always lands ``draft`` -- the same ruling
the REST surface already makes.

Results are the service's own return values, and the lean ones: a mutation
answers what changed (``{"id", "previous_status", "status", …}``) rather than
the whole re-serialized ticket the REST endpoints answer with. Over the network
that ticket saves a round trip; over MCP the next call is free, while
re-deriving a ticket costs a whole-board load for ``children`` on a vault store.
``get_ticket`` is one call away when the caller does want it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

from . import service
from .store import MdTreeStoreError, TicketStore


# The version this server speaks. A client that asks for an older one it still
# supports gets that one back: nothing here is version-specific (tools only),
# so there is no reason to force a client to renegotiate.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# Sent once in the initialize result: what a model should know about this board
# before it starts calling tools, in the shape the rest of llpm states it.
INSTRUCTIONS = """\
Tickets are markdown notes with YAML frontmatter on one board. These tools own \
the frontmatter; the prose body is edited in the note itself.

Status is derived at read time: 'blocked' is never stored, it is what an \
unresolved blocker or waits_on target makes a ticket look like, so filter on \
status=blocked rather than looking for it in a field. Use set_status for \
status and set_fields for the simple fields -- neither can write the other's, \
and provenance (origin, created_by, commits) is the system's to write.

Four edges, deliberately few: blockers are hard dependencies on ticket IDs on \
THIS board; waits_on holds full vault stems of tickets on other boards; after \
is ordering advice that never blocks; serves points an epic or feature at a \
goal note. Agent-created tickets land in draft unless the board opted their \
type in, and a planner promotes them from there.\
"""

# JSON-RPC 2.0 error codes. Protocol-level failures (bad method, unknown tool)
# are these; a tool that llpm's *rules* refuse is a successful call carrying
# isError -- the model is supposed to read that message and fix its call.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


class _RpcError(Exception):
    """A protocol-level failure, carrying the JSON-RPC code to answer with."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _server_version() -> str:
    try:
        return _pkg_version("llpm")
    except PackageNotFoundError:  # running from a source tree, not installed
        return "0"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    """One MCP tool: a name, what it is for, its argument schema, and the
    ``service`` call it is a face for."""

    name: str
    description: str
    schema: dict
    run: Callable[["Session", dict], Any]

    def spec(self) -> dict:
        """The entry ``tools/list`` publishes."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


def _schema(required: list[str], **properties) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _required(args: dict, name: str):
    """Pull a required argument, in llpm's idiom rather than a schema error.

    The client validates against ``inputSchema`` before it ever gets here; this
    is the backstop for one that doesn't, and it reads like every other rule the
    service states.
    """
    value = args.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise service.Invalid(f"'{name}' is required.")
    return value


_ID_ARG = {"type": "string",
           "description": "Ticket ID on this board, e.g. TASK-001 (case-insensitive)."}


_TYPE_ARTICLES = {"array": "an array", "object": "an object", "integer": "an integer",
                   "string": "a string", "boolean": "a boolean", "number": "a number"}


def _type_name(value: Any) -> str:
    """The JSON Schema type name of a decoded JSON value.

    Checked in this order because ``bool`` is a subclass of ``int`` in Python.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _matches_schema_type(value: Any, expected: str) -> bool:
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True  # a declared type this server never uses: nothing to check


def _validate_args(tool: "Tool", args: dict) -> str | None:
    """Type-check ``args`` against ``tool.schema`` before ``tool.run`` sees them.

    A tool calls straight into ``service.py``, which trusts its caller's types
    the way the CLI and REST both do: ``list(commits)`` on a string silently
    makes six one-character SHAs rather than raising, ``_as_list(tags)`` on an
    int makes a tag out of it. An MCP client is supposed to validate against
    ``inputSchema`` before it ever calls ``tools/call``; this is the backstop
    for one that doesn't, returning the first mismatch as a sentence naming the
    argument and the type it needed to be.
    """
    properties = tool.schema.get("properties", {})
    for key, value in args.items():
        if value is None:
            continue  # absent/null is a "missing" question, not a type one
        prop = properties.get(key)
        expected = prop.get("type") if prop else None
        if expected is None:
            continue
        if not _matches_schema_type(value, expected):
            return (f"'{key}' must be {_TYPE_ARTICLES.get(expected, expected)}, "
                    f"got {_type_name(value)}.")
        if expected == "array":
            item_type = (prop.get("items") or {}).get("type")
            if item_type is None:
                continue
            for item in value:
                if not _matches_schema_type(item, item_type):
                    return (f"'{key}' items must be "
                            f"{_TYPE_ARTICLES.get(item_type, item_type)}, "
                            f"got {_type_name(item)}.")
    return None


def _tool_list_tickets(session: "Session", args: dict):
    return service.list_tickets(
        session.store,
        status=args.get("status"),
        type=args.get("type"),
        parent=args.get("parent"),
        include_archive=bool(args.get("include_archived", False)),
        fields=args.get("fields"),
    )


def _tool_next_tickets(session: "Session", args: dict):
    limit = args.get("limit")
    return service.next_tickets(
        session.store,
        tier=args.get("tier"),
        limit=1 if limit is None else limit,
    )


def _tool_get_ticket(session: "Session", args: dict):
    return service.get_ticket(
        session.store, _required(args, "id"), body=bool(args.get("body", True))
    )


def _tool_create_ticket(session: "Session", args: dict):
    return service.create_ticket(
        session.store,
        _required(args, "type"),
        _required(args, "title"),
        body=args.get("body"),
        parent=args.get("parent"),
        priority=args.get("priority"),
        effort=args.get("effort"),
        tags=args.get("tags"),
        requires_human=bool(args.get("requires_human", False)),
        origin=args.get("origin") or session.origin,
        created_by=session.created_by_for(args),
        serves=args.get("serves"),
        triage=bool(args.get("triage", False)),
        auto_approve=session.auto_approve,
    )


def _tool_set_status(session: "Session", args: dict):
    return service.set_status(
        session.store,
        _required(args, "id"),
        _required(args, "status"),
        awaiting=args.get("awaiting"),
        commits=args.get("commits"),
    )


def _tool_set_fields(session: "Session", args: dict):
    fields = args.get("fields")
    if not isinstance(fields, dict):
        raise service.Invalid(
            "'fields' must be an object of field -> value, e.g. "
            '{"priority": "high", "effort": "small"}.'
        )
    return service.set_fields(session.store, _required(args, "id"), fields)


def _edge(fn, target_arg: str):
    """The four edge pairs are one shape: subject ticket, one target."""
    def run(session: "Session", args: dict):
        return fn(session.store, _required(args, "id"), _required(args, target_arg))
    return run


TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_tickets",
        "List this board's tickets. `status` matches the DERIVED status, so "
        "status='blocked' finds tickets whose stored status is something else. "
        "Bodies are never included; pass `fields` to trim each entry to the "
        "keys you need.",
        _schema(
            [],
            status={"type": "string",
                    "description": "Effective status: draft, planned, open, "
                                   "in-progress, review, blocked, complete, "
                                   "closed or deferred."},
            type={"type": "string",
                  "description": "Ticket type: task, feature, epic or research."},
            parent={"type": "string", "description": "Only children of this ticket ID."},
            include_archived={"type": "boolean",
                              "description": "Include archived tickets (default false)."},
            fields={"type": "array", "items": {"type": "string"},
                    "description": 'Keys to keep per entry, e.g. ["id", "title", "status"].'},
        ),
        _tool_list_tickets,
    ),
    Tool(
        "next_tickets",
        "Pick the ticket(s) to work on next, deterministically. Ready means the "
        "DERIVED status is 'open' (so unresolved blockers and blocking waits_on "
        "stems already exclude it) and nothing about it stops a worker: "
        "acceptance criteria written, effort and model_tier set, not "
        "requires_human. Ordered priority high->low, then an 'after' tie-break "
        "(targets all complete/closed first), then ID. This SELECTS ONLY -- it "
        "changes nothing, so claim what you take with set_status(status="
        "'in-progress').",
        _schema(
            [],
            tier={"type": "string", "enum": ["heavy", "standard", "light"],
                  "description": "Only tickets tagged for this model tier (or untagged)."},
            limit={"type": "integer",
                   "description": "How many tickets to return (default 1)."},
        ),
        _tool_next_tickets,
    ),
    Tool(
        "get_ticket",
        "One ticket in full: frontmatter, derived status, children, resolved "
        "blockers and waits, and the markdown body.",
        _schema(
            ["id"],
            id=_ID_ARG,
            body={"type": "boolean",
                  "description": "Include the markdown body (default true)."},
        ),
        _tool_get_ticket,
    ),
    Tool(
        "create_ticket",
        "File a new ticket from the board's template for its type. Without a "
        "body it lands 'draft' (a stub for a planner to spec); with one it is "
        "workable -- but an agent-created ticket still lands 'draft' unless the "
        "board's intake policy opted its type in. Goal attachment is never "
        "required: `serves` (epics/features), a goal-serving `parent` or "
        "`triage` keep it out of the orphan report, and creation succeeds "
        "either way.",
        _schema(
            ["type", "title"],
            type={"type": "string",
                  "description": "task, feature, epic, research, or any type "
                                 "the board has a template for."},
            title={"type": "string", "description": "Human-readable title."},
            body={"type": "string",
                  "description": "Markdown body replacing the template's. Write "
                                 "the spec here rather than filing an empty stub."},
            parent={"type": "string",
                    "description": "Parent ticket ID; must exist on this board."},
            priority={"type": "string", "enum": ["high", "medium", "low"]},
            effort={"type": "string", "enum": ["small", "medium", "large"]},
            tags={"type": "array", "items": {"type": "string"}},
            requires_human={"type": "boolean",
                            "description": "Mark work only a human can do."},
            serves={"type": "array", "items": {"type": "string"},
                    "description": "Full vault stems of goal notes this serves "
                                   "(epics/features only), e.g. "
                                   '["goals.autonomous-task-loop"].'},
            triage={"type": "boolean",
                    "description": "Tag 'triage': an explicit 'not attached to a "
                                   "goal on purpose'."},
            origin={"type": "string", "enum": ["human", "agent"],
                    "description": "Who is filing this. Defaults to 'agent' -- "
                                   "pass 'human' only when relaying a request a "
                                   "person made."},
            created_by={"type": "string",
                        "description": "Agent/session id to record. Defaults to "
                                       "the identity this server was started "
                                       "with, or the MCP client's own name."},
        ),
        _tool_create_ticket,
    ),
    Tool(
        "set_status",
        "Change a ticket's status -- the only way to write it. 'blocked' is not "
        "a choice (it is derived). `awaiting` is valid only on the way into "
        "'review' and says who unsticks it; every transition clears any "
        "previous one. `commits` records SHAs on the ticket: this server never "
        "runs git, so name them.",
        _schema(
            ["id", "status"],
            id=_ID_ARG,
            status={"type": "string",
                    "enum": ["draft", "planned", "open", "in-progress", "review",
                             "complete", "closed", "deferred"]},
            awaiting={"type": "string",
                      "enum": ["reviewer", "push", "deploy", "human-verify",
                               "human-answer"],
                      "description": "Review-queue discriminator; only with "
                                     "status='review'."},
            commits={"type": "array", "items": {"type": "string"},
                     "description": "Commit SHAs to record on the ticket."},
        ),
        _tool_set_status,
    ),
    Tool(
        "set_fields",
        "Set simple frontmatter fields (title, priority, effort, model_tier, "
        "parent, tags, requires_human, milestone, hours, …). All or nothing: "
        "one bad value changes nothing. Cannot write status, the four edge "
        "lists or awaiting -- each has its own tool -- nor provenance, which "
        "the system writes.",
        _schema(
            ["id", "fields"],
            id=_ID_ARG,
            fields={"type": "object",
                    "description": 'Assignments, e.g. {"priority": "high", '
                                   '"effort": "small"}. null clears a field.'},
        ),
        _tool_set_fields,
    ),
    Tool(
        "blocker_add",
        "Add a hard dependency: this ticket cannot proceed until the blocker "
        "resolves. Blockers are real ticket IDs on this board -- never free "
        "text -- because 'blocked' is derived by resolving them.",
        _schema(["id", "blocker"], id=_ID_ARG,
                blocker={"type": "string", "description": "Ticket ID that blocks it."}),
        _edge(service.blocker_add, "blocker"),
    ),
    Tool(
        "blocker_rm",
        "Remove a hard dependency.",
        _schema(["id", "blocker"], id=_ID_ARG,
                blocker={"type": "string", "description": "Blocker ticket ID to drop."}),
        _edge(service.blocker_rm, "blocker"),
    ),
    Tool(
        "after_add",
        "Add soft precedence: advice that this ticket should come after "
        "another. It never blocks and cycles only warn -- for a real "
        "dependency use blocker_add.",
        _schema(["id", "after"], id=_ID_ARG,
                after={"type": "string", "description": "Ticket ID that should come first."}),
        _edge(service.after_add, "after"),
    ),
    Tool(
        "after_rm",
        "Remove a soft-precedence edge.",
        _schema(["id", "after"], id=_ID_ARG,
                after={"type": "string", "description": "Ticket ID to drop from 'after'."}),
        _edge(service.after_rm, "after"),
    ),
    Tool(
        "waits_add",
        "Add a cross-board dependency by full vault stem (e.g. "
        "repos.marginalia.llpm.features.FEAT-010). It contributes to 'blocked' "
        "while the target is unresolved; a target that cannot be read never "
        "blocks. Bare ticket IDs are refused -- those are blockers.",
        _schema(["id", "stem"], id=_ID_ARG,
                stem={"type": "string", "description": "Full vault stem of the target."}),
        _edge(service.waits_add, "stem"),
    ),
    Tool(
        "waits_rm",
        "Remove a cross-board dependency.",
        _schema(["id", "stem"], id=_ID_ARG,
                stem={"type": "string", "description": "Target stem to drop."}),
        _edge(service.waits_rm, "stem"),
    ),
    Tool(
        "serves_add",
        "Point an epic or feature at a goal note by full vault stem (e.g. "
        "goals.autonomous-task-loop). Tasks serve goals through their parent, "
        "so they cannot carry this.",
        _schema(["id", "stem"], id=_ID_ARG,
                stem={"type": "string", "description": "Full vault stem of the goal note."}),
        _edge(service.serves_add, "stem"),
    ),
    Tool(
        "serves_rm",
        "Remove a goal reference from an epic or feature.",
        _schema(["id", "stem"], id=_ID_ARG,
                stem={"type": "string", "description": "Goal stem to drop."}),
        _edge(service.serves_rm, "stem"),
    ),
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


# ---------------------------------------------------------------------------
# Session -- transport-independent protocol handling
# ---------------------------------------------------------------------------

class Session:
    """One MCP conversation with one board.

    Holds the store, the identity tickets it files are stamped with, and the
    board's intake policy. ``handle`` takes a decoded JSON-RPC message and
    returns the message to send back, or None when there is nothing to answer
    (a notification).

    The store is long-lived here -- a stdio server holds it for the life of the
    process -- which is exactly the case ``service._begin_read_scope`` exists
    for: each tool call is a service call, and each service call is a fresh read
    scope, so nothing a store cached for one call is served to the next.
    """

    def __init__(
        self,
        store: TicketStore,
        *,
        auto_approve=(),
        created_by: str | None = None,
        origin: str = "agent",
    ) -> None:
        self.store = store
        self.auto_approve = tuple(auto_approve or ())
        self.created_by = created_by
        self.origin = origin
        self.client: dict = {}

    # -- identity ---------------------------------------------------------

    def created_by_for(self, args: dict) -> str:
        """Who to record as the author of a ticket filed through this session.

        The call may name it; otherwise the identity the server started with
        (``--created-by``/``LLPM_CREATED_BY``); otherwise whoever the client
        said it was in the handshake. An HTTP session has none of those unless
        the call names one, which is the point: a server with no shell must not
        invent provenance.
        """
        named = args.get("created_by") or self.created_by
        if not named:
            raise service.Invalid(
                "'created_by' is required: this server cannot infer who you "
                "are. Pass created_by with the call (the stdio server also "
                "takes it from --created-by, LLPM_CREATED_BY, or the MCP "
                "handshake)."
            )
        return named

    # -- protocol ---------------------------------------------------------

    def handle(self, message) -> dict | None:
        """Answer one JSON-RPC message, or None if it wants no answer."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST,
                          "Expected a JSON-RPC 2.0 message object.")

        method = message.get("method")
        message_id = message.get("id")

        if method is None:
            # A response to a request this server never sent: nothing to do,
            # and answering an answer would be a protocol error of its own.
            return None
        if message_id is None:
            self._notification(method)
            return None
        if isinstance(message_id, (dict, list)):
            # Not identifiable as a request id (JSON-RPC: string, number, or
            # null) -- nothing sane to echo back, so answer with id: null.
            return _error(None, _INVALID_PARAMS,
                          "'id' must be a string or number, not an object or array.")

        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            return _error(message_id, _INVALID_PARAMS, "'params' must be an object.")

        try:
            return _result(message_id, self._invoke(method, params or {}))
        except _RpcError as e:
            return _error(message_id, e.code, str(e))
        except Exception as e:  # noqa: BLE001 -- a bad call must not end the session
            return _error(message_id, _INTERNAL_ERROR, f"{type(e).__name__}: {e}")

    def _notification(self, method: str) -> None:
        """Notifications are one-way. ``notifications/initialized`` is the only
        one this server expects, and it needs no bookkeeping -- the handshake
        already told us everything we use."""

    def _invoke(self, method: str, params: dict):
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.spec() for tool in TOOLS]}
        if method == "tools/call":
            return self._call_tool(params)
        raise _RpcError(_METHOD_NOT_FOUND, f"Unknown method: {method!r}")

    def _initialize(self, params: dict) -> dict:
        self.client = params.get("clientInfo") or {}
        if self.created_by is None:
            self.created_by = _client_id(self.client)

        asked = params.get("protocolVersion")
        return {
            "protocolVersion": (
                asked if asked in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            ),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "llpm", "version": _server_version()},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str):
            raise _RpcError(_INVALID_PARAMS, "'name' must be a string.")
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise _RpcError(_INVALID_PARAMS, f"Unknown tool: {name!r}")

        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _RpcError(_INVALID_PARAMS, "'arguments' must be an object.")

        type_error = _validate_args(tool, args)
        if type_error is not None:
            return _tool_error(type_error)

        try:
            return _tool_result(tool.run(self, args))
        except (service.ServiceError, MdTreeStoreError) as e:
            # A rule llpm refuses (or a vault it cannot reach) is a successful
            # call carrying isError, not a protocol failure: the message is
            # llpm's own, and it is written to be read and acted on -- which a
            # model only gets to do when it comes back as tool output.
            return _tool_error(str(e))


def _client_id(client_info: dict) -> str | None:
    """``created_by`` from an MCP handshake: ``claude-code/2.1.4``."""
    name = client_info.get("name")
    if not name:
        return None
    client_version = client_info.get("version")
    return f"{name}/{client_version}" if client_version else str(name)


# ---------------------------------------------------------------------------
# Message shapes
# ---------------------------------------------------------------------------

def _result(message_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id,
            "error": {"code": code, "message": message}}


def _tool_result(result) -> dict:
    """Tool output: the JSON text a model reads, plus the same thing structured.

    The structured half is parsed back out of the text so the two can't say
    different things -- and so a ``datetime.date`` that came out of YAML
    frontmatter is a string in both, whichever transport serializes it.
    """
    text = json.dumps(result, indent=2, default=str)
    payload = {"content": [{"type": "text", "text": text}]}
    if isinstance(result, dict):
        payload["structuredContent"] = json.loads(text)
    return payload


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "isError": True}


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------

def serve_stdio(session: Session, stdin=None, stdout=None) -> None:
    """Run the MCP stdio transport until the client closes stdin.

    One JSON-RPC message per line, UTF-8, and NOTHING on stdout but responses --
    a stray print corrupts the stream, which is why ``llpm mcp`` puts its banner
    on stderr. Batched (array) messages are not accepted: JSON-RPC batching was
    dropped from MCP in 2025-06-18.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            _write(stdout, _error(None, _PARSE_ERROR, f"Invalid JSON: {e}"))
            continue
        response = session.handle(message)
        if response is not None:
            _write(stdout, response)


def _write(stdout, message: dict) -> None:
    stdout.write(json.dumps(message, default=str) + "\n")
    stdout.flush()
