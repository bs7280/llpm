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

This slice serves the read side. The write endpoints (status, create, PATCH,
edges) land in the slices that follow and hang off the same ``_mapped()``
error mapping, which is the whole point of the typed service errors:
``NotFound`` -> 404, ``Invalid`` -> 422, ``Conflict`` -> 409.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager

from fastapi import APIRouter, FastAPI, HTTPException

from . import service
from .store import TicketStore

StoreFor = Callable[[str], TicketStore]
Boards = Callable[[], list[str]]


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

    @router.get("/{repo}/tickets/{ticket_id}")
    def get_ticket(repo: str, ticket_id: str, body: bool = True):
        with _mapped():
            return service.get_ticket(store_for(repo), ticket_id, body=body)

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
