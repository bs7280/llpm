"""FastAPI surface over ``service.py`` -- the HTTP half of FEAT-015.

Importing this module requires the optional ``llpm[api]`` extra (fastapi +
uvicorn); nothing in the core CLI imports it, so ``llpm --help`` keeps working
on a pyyaml-only install.

Two entry points:

- ``make_router(store_for, boards=)`` -- an ``APIRouter`` another app mounts.
  marginalia mounts it under ``/api/llpm`` rather than llpm becoming a third
  deployed service.
- ``make_app(store_for, boards=)`` -- that router plus ``/healthz`` as a
  standalone app, which is what ``llpm serve`` runs.

``repo`` is a path parameter, so one process serves every board: ``store_for``
maps a repo name to its ``TicketStore`` (and caches it -- building a store per
request would throw away the vault connection).

TASK-016 served the read side, TASK-017 the status write, TASK-018 create and
PATCH, TASK-019 the four edge pairs -- the whole FEAT-015 surface -- and
FEAT-009 added ``GET /{repo}/next``, the selection a dispatcher polls. Every handler
hangs off the same ``_mapped()`` error mapping, which is the point of the typed
service errors: ``NotFound`` -> 404, ``Invalid`` -> 422, ``Conflict`` -> 409.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Response
from pydantic import BaseModel

from . import mcp, service
from .store import TicketStore

StoreFor = Callable[[str], TicketStore]
Boards = Callable[[], list[str]]


class TicketCreate(BaseModel):
    """Body of ``POST /{repo}/tickets``.

    ``origin``/``created_by`` are deliberately explicit: the CLI infers them
    from a shell (``LLPM_ORIGIN``, ``LLPM_CREATED_BY``, human-by-default), and
    a server has no shell to infer from, so an HTTP caller says who it is.
    ``created_by`` is required for that reason; ``origin`` defaults to the
    conservative answer, which is what puts an unapproved kind in ``draft``.

    Enums and every other rule are checked by ``service.create_ticket``, not
    here, so a bad value produces llpm's own message rather than pydantic's.
    """

    type: str
    title: str
    body: str | None = None
    parent: str | None = None
    priority: str | None = None
    effort: str | None = None
    tags: list[str] | None = None
    requires_human: bool = False
    origin: str = "agent"
    created_by: str
    serves: list[str] | None = None
    triage: bool = False


class TicketRef(BaseModel):
    """``{"id": "FEAT-002"}`` -- the body of the intra-board edge endpoints."""

    id: str


class StemRef(BaseModel):
    """``{"stem": "repos.marginalia.llpm.features.FEAT-010"}`` -- the body of
    the cross-board (``waits``) and goal (``serves``) edge endpoints, which
    address vault notes rather than tickets on this board."""

    stem: str


class StatusChange(BaseModel):
    """Body of ``POST /{repo}/tickets/{id}/status``.

    ``commits`` is a list because the server has no checkout and must never run
    git: the CLI harvests SHAs from its CWD repo, an HTTP caller names them.
    The values are checked by ``service.set_status`` against llpm's own rules,
    not here -- a status typo has to produce llpm's message, not pydantic's.
    """

    status: str
    awaiting: str | None = None
    commits: list[str] | None = None


@contextmanager
def _mapped():
    """Turn llpm's typed service errors into HTTP responses.

    Handlers wrap their service calls in this instead of the app registering
    exception handlers, because a mounted router is not an app -- marginalia's
    app must not have to know llpm's exception classes to get right status codes.
    """
    try:
        yield
    except service.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except service.Invalid as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except service.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _split_fields(fields: str | None) -> list[str] | None:
    """``"id,title"`` -> ``["id", "title"]``; nothing asked for -> None."""
    if not fields:
        return None
    return [f.strip() for f in fields.split(",") if f.strip()]


def make_router(store_for: StoreFor, boards: Boards | None = None) -> APIRouter:
    """Build the llpm router.

    Args:
        store_for: repo name -> ``TicketStore``. Called per request, so it
            should cache: one live store per board, not one per request. Raise
            ``service.NotFound`` for a repo this deployment doesn't serve.
        boards: optional enumerator for ``GET /boards``. A deployment that
            can't enumerate them (a single mounted board) omits it, and the
            endpoint answers with an empty list.
    """
    router = APIRouter()

    # Responses are deliberately un-modelled: they are exactly what
    # `llpm list --json` / `llpm show --json` print, and a response_model would
    # be a second place for that shape to live.

    @router.get("/boards")
    def get_boards():
        if boards is None:
            return []
        with _mapped():
            return boards()

    @router.get("/{repo}/tickets")
    def get_tickets(
        repo: str,
        status: str | None = None,
        type: str | None = None,
        parent: str | None = None,
        include_archived: bool = False,
        fields: str | None = None,
    ):
        with _mapped():
            return service.list_tickets(
                store_for(repo),
                status=status,
                type=type,
                parent=parent,
                include_archive=include_archived,
                fields=_split_fields(fields),
            )

    @router.get("/{repo}/next")
    def get_next(repo: str, tier: str | None = None, limit: int = 1):
        """Ready-ticket selection (FEAT-009) -- the scheduler primitive a
        dispatcher polls. Declared before ``/{repo}/tickets/{ticket_id}``'s
        sibling routes for readability only; ``next`` is a distinct path
        segment, so no route shadows another."""
        with _mapped():
            return service.next_tickets(store_for(repo), tier=tier, limit=limit)

    @router.get("/{repo}/tickets/{ticket_id}")
    def get_ticket(repo: str, ticket_id: str, body: bool = True):
        with _mapped():
            return service.get_ticket(store_for(repo), ticket_id, body=body)

    @router.post("/{repo}/tickets", status_code=201)
    def post_ticket(repo: str, new: TicketCreate):
        """File a ticket; answer 201 with it as ``GET`` would render it.

        No ``[intake] auto_approve`` list is passed: that policy lives in the
        board's ``.llpm/config.toml``, which is a repo checkout the server need
        not have. So an agent-origin create over HTTP always lands ``draft`` --
        the conservative half of FEAT-011 -- and a caller that wants it open
        flips the status in a second call. Wiring the config through
        ``store_for``'s deployment is the follow-up.
        """
        with _mapped():
            result = service.create_ticket(
                store_for(repo),
                new.type,
                new.title,
                body=new.body,
                parent=new.parent,
                priority=new.priority,
                effort=new.effort,
                tags=new.tags,
                requires_human=new.requires_human,
                origin=new.origin,
                created_by=new.created_by,
                serves=new.serves,
                triage=new.triage,
                include_ticket=True,
            )
        return result["ticket"]

    @router.patch("/{repo}/tickets/{ticket_id}")
    def patch_ticket(repo: str, ticket_id: str, fields: dict):
        """Set simple fields. The body is the assignments themselves --
        ``{"priority": "high", "hours": 3}`` -- so the field names are llpm's
        own, not a wrapper schema that would have to track them."""
        with _mapped():
            result = service.set_fields(
                store_for(repo), ticket_id, fields, include_ticket=True
            )
        return result["ticket"]

    @router.post("/{repo}/tickets/{ticket_id}/status")
    def post_status(repo: str, ticket_id: str, change: StatusChange):
        """Change a status; answer with the ticket as ``GET`` would render it."""
        with _mapped():
            result = service.set_status(
                store_for(repo),
                ticket_id,
                change.status,
                awaiting=change.awaiting,
                commits=change.commits,
                include_ticket=True,
            )
        return result["ticket"]

    # -- Edges (TASK-019) ---------------------------------------------------
    #
    # Four pairs, one shape: POST adds, DELETE removes, both answer with the
    # updated ticket so the caller sees the effect (a new blocker shows up as
    # `is_blocked` at once) without a follow-up GET. Adds are idempotent -- an
    # edge that was already there is 200, not an error -- while removing one
    # that isn't there is a 404, because it names something that doesn't exist.
    # The target travels in the body rather than the path: `waits`/`serves`
    # stems are dotted vault addresses, and a body keeps all four pairs the
    # same shape.

    def _edge(fn, repo: str, ticket_id: str, target: str):
        with _mapped():
            return fn(store_for(repo), ticket_id, target, include_ticket=True)["ticket"]

    @router.post("/{repo}/tickets/{ticket_id}/blockers")
    def post_blocker(repo: str, ticket_id: str, target: TicketRef):
        return _edge(service.blocker_add, repo, ticket_id, target.id)

    @router.delete("/{repo}/tickets/{ticket_id}/blockers")
    def delete_blocker(repo: str, ticket_id: str, target: TicketRef):
        return _edge(service.blocker_rm, repo, ticket_id, target.id)

    @router.post("/{repo}/tickets/{ticket_id}/after")
    def post_after(repo: str, ticket_id: str, target: TicketRef):
        return _edge(service.after_add, repo, ticket_id, target.id)

    @router.delete("/{repo}/tickets/{ticket_id}/after")
    def delete_after(repo: str, ticket_id: str, target: TicketRef):
        return _edge(service.after_rm, repo, ticket_id, target.id)

    @router.post("/{repo}/tickets/{ticket_id}/waits")
    def post_waits(repo: str, ticket_id: str, target: StemRef):
        return _edge(service.waits_add, repo, ticket_id, target.stem)

    @router.delete("/{repo}/tickets/{ticket_id}/waits")
    def delete_waits(repo: str, ticket_id: str, target: StemRef):
        return _edge(service.waits_rm, repo, ticket_id, target.stem)

    @router.post("/{repo}/tickets/{ticket_id}/serves")
    def post_serves(repo: str, ticket_id: str, target: StemRef):
        return _edge(service.serves_add, repo, ticket_id, target.stem)

    @router.delete("/{repo}/tickets/{ticket_id}/serves")
    def delete_serves(repo: str, ticket_id: str, target: StemRef):
        return _edge(service.serves_rm, repo, ticket_id, target.stem)

    # -- MCP (FEAT-016) -----------------------------------------------------
    #
    # The remote half of `llpm mcp`: the same `mcp.Session` over the streamable-
    # HTTP transport, so an agent that can reach this mount gets the tool
    # surface without a checkout. JSON in, JSON out, one message per request --
    # no SSE and no server-side session, which is what lets any instance answer
    # any request. The board is the path parameter, exactly as for the REST
    # endpoints, so no tool needs a `repo` argument.
    #
    # Statelessness has one visible consequence: the `initialize` handshake that
    # names the client doesn't outlive its request, so an HTTP caller names
    # `created_by` in the tool arguments. That is the same ruling `TicketCreate`
    # makes, for the same reason -- a server has no shell to infer identity from.

    @router.post("/{repo}/mcp")
    def post_mcp(repo: str, message: dict):
        with _mapped():
            session = mcp.Session(store_for(repo))
        reply = session.handle(message)
        if reply is None:
            return Response(status_code=202)  # a notification: nothing to answer
        return reply

    @router.get("/{repo}/mcp")
    def get_mcp(repo: str):
        """The transport allows a server to offer no SSE stream, and this one
        doesn't; saying so is a 405 rather than letting a client hang."""
        raise HTTPException(
            status_code=405,
            detail="No SSE stream here -- POST JSON-RPC messages to this URL.",
        )

    return router


def make_app(store_for: StoreFor, boards: Boards | None = None) -> FastAPI:
    """The standalone app behind ``llpm serve``: the router plus ``/healthz``."""
    app = FastAPI(
        title="llpm",
        description="Read and change llpm tickets on any board over HTTP.",
    )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    app.include_router(make_router(store_for, boards=boards))
    return app
