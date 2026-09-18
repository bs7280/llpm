"""Router tests for llpm's HTTP surface (TASK-016).

The router is the thin half: these tests check wiring, query-param plumbing and
error mapping. What the JSON *says* is the service's contract, pinned in
test_service.py -- with one assertion here that an endpoint answers exactly what
the corresponding service call returns, so the router can't quietly reshape it.

Skipped entirely without the optional ``llpm[api]`` extra installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="needs the llpm[api] extra")

from fastapi.testclient import TestClient  # noqa: E402

from conftest import load_fake_store  # noqa: E402
from llpm import api, service  # noqa: E402
from llpm.store import LocalDirStore  # noqa: E402


@pytest.fixture
def client(docs_root):
    """One app serving one board, named 'demo'."""
    store = LocalDirStore(docs_root)

    def store_for(repo):
        if repo != "demo":
            raise service.NotFound(f"No board {repo!r} on this server.")
        return store

    return TestClient(api.make_app(store_for, boards=lambda: ["demo"]))


def ids(payload) -> list[str]:
    return [t["id"] for t in payload]


class TestHealthAndBoards:
    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_boards(self, client):
        assert client.get("/boards").json() == ["demo"]

    def test_boards_without_an_enumerator(self, docs_root):
        """A deployment that can't enumerate boards still answers, emptily."""
        app = api.make_app(lambda repo: LocalDirStore(docs_root))
        assert TestClient(app).get("/boards").json() == []


class TestListEndpoint:
    def test_lists_without_bodies(self, client):
        payload = client.get("/demo/tickets").json()
        assert ids(payload) == [
            "EPIC-001", "FEAT-001", "FEAT-002", "RESEARCH-001", "TASK-001",
        ]
        assert all("body" not in t for t in payload)

    def test_matches_the_service_exactly(self, client, docs_root):
        """The endpoint is `service.list_tickets` over HTTP and nothing else."""
        assert client.get("/demo/tickets?status=blocked").json() == \
            service.list_tickets(LocalDirStore(docs_root), status="blocked")

    def test_filters(self, client):
        assert ids(client.get("/demo/tickets?type=feature").json()) == ["FEAT-001", "FEAT-002"]
        assert ids(client.get("/demo/tickets?parent=FEAT-002").json()) == \
            ["RESEARCH-001", "TASK-001"]
        assert ids(client.get("/demo/tickets?status=blocked").json()) == ["TASK-001"]

    def test_include_archived(self, client):
        assert "FEAT-000" not in ids(client.get("/demo/tickets").json())
        assert "FEAT-000" in ids(client.get("/demo/tickets?include_archived=1").json())

    def test_fields_trims_the_payload(self, client):
        payload = client.get("/demo/tickets?fields=id,title").json()
        assert all(set(t) == {"id", "title"} for t in payload)

    def test_unknown_field_is_422(self, client):
        r = client.get("/demo/tickets?fields=id,bogus")
        assert r.status_code == 422
        assert "bogus" in r.json()["detail"]

    def test_unknown_board_is_404(self, client):
        r = client.get("/nosuch/tickets")
        assert r.status_code == 404
        assert "nosuch" in r.json()["detail"]


class TestTicketEndpoint:
    def test_single_ticket_with_body(self, client):
        ticket = client.get("/demo/tickets/FEAT-001").json()
        assert ticket["id"] == "FEAT-001"
        assert "## Problem" in ticket["body"]

    def test_body_can_be_switched_off(self, client):
        ticket = client.get("/demo/tickets/FEAT-001?body=0").json()
        assert "body" not in ticket
        assert ticket["id"] == "FEAT-001"

    def test_derived_fields_travel(self, client):
        ticket = client.get("/demo/tickets/TASK-001").json()
        assert ticket["effective_status"] == "blocked"
        assert ticket["is_blocked"] is True
        assert ticket["blockers"] == [
            {"id": "FEAT-001", "resolved": True},
            {"id": "FEAT-002", "resolved": False},
        ]

    def test_unknown_id_is_404(self, client):
        r = client.get("/demo/tickets/NOPE-999")
        assert r.status_code == 404
        assert r.json()["detail"] == "Ticket 'NOPE-999' not found."


class TestManyBoardsOneApp:
    """`repo` is a path parameter precisely so one process serves every board."""

    @pytest.fixture
    def two_boards(self, docs_root, tmp_path):
        other = load_fake_store(docs_root)
        # Give the second board a ticket the first one doesn't have, so a
        # mix-up can't pass unnoticed.
        other.active["TASK-900_OTHER_BOARD.md"] = (
            {
                "id": "TASK-900", "type": "task", "title": "Only on beta",
                "status": "open", "priority": "high", "parent": None,
                "blockers": [], "created": "2026-01-01", "updated": "2026-01-01",
                "completed": None, "tags": [],
            },
            "## Description\n",
        )
        stores = {"alpha": LocalDirStore(docs_root), "beta": other}
        calls: list[str] = []

        def store_for(repo):
            calls.append(repo)
            if repo not in stores:
                raise service.NotFound(f"No board {repo!r} on this server.")
            return stores[repo]

        return TestClient(api.make_app(store_for, boards=lambda: sorted(stores))), calls

    def test_each_repo_gets_its_own_store(self, two_boards):
        client, calls = two_boards

        assert "TASK-900" not in ids(client.get("/alpha/tickets").json())
        assert "TASK-900" in ids(client.get("/beta/tickets").json())
        assert client.get("/alpha/tickets/TASK-900").status_code == 404
        assert client.get("/beta/tickets/TASK-900").json()["title"] == "Only on beta"

        assert client.get("/boards").json() == ["alpha", "beta"]
        assert calls == ["alpha", "beta", "alpha", "beta"]

    def test_store_for_is_asked_per_request(self, two_boards):
        """Caching is the caller's job (llpm serve caches per repo); the router
        must not hold a store of its own, or a board would go stale."""
        client, calls = two_boards
        client.get("/alpha/tickets")
        client.get("/alpha/tickets")
        assert calls == ["alpha", "alpha"]
