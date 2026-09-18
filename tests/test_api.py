"""Router tests for llpm's HTTP surface (TASK-016 reads, TASK-017 status,
TASK-018 create + PATCH, TASK-019 the edge pairs).

The router is the thin half: these tests check wiring, query-param plumbing and
error mapping. What the JSON *says* is the service's contract, pinned in
test_service.py -- with one assertion here that an endpoint answers exactly what
the corresponding service call returns, so the router can't quietly reshape it.

Skipped entirely without the optional ``llpm[api]`` extra installed.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("fastapi", reason="needs the llpm[api] extra")

from fastapi.testclient import TestClient  # noqa: E402

from conftest import load_fake_store  # noqa: E402
from llpm import api, commands, mcp, service  # noqa: E402
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


class TestNextEndpoint:
    """FEAT-009 over HTTP: the endpoint a dispatcher polls. Which tickets are
    ready and in what order is pinned in test_service.py -- these check the
    wiring, the query params and the error mapping."""

    @pytest.fixture
    def ready_board(self, docs_root):
        """The fixture board plus one ticket nothing stops a worker taking."""
        store = LocalDirStore(docs_root)
        result = service.create_ticket(store, "task", "Fresh work",
                                       body="## Acceptance Criteria\n\n- [ ] Works\n",
                                       origin="human", created_by="test",
                                       today="2026-03-20")
        service.set_fields(store, result["id"], {"effort": "small"}, today="2026-03-20")
        service.set_status(store, result["id"], "open", today="2026-03-20")
        return TestClient(api.make_app(lambda repo: store)), result["id"]

    def test_an_empty_ready_set_is_an_empty_array(self, client):
        # The fixture board's only `open` ticket is blocked.
        assert client.get("/demo/next").json() == []

    def test_matches_the_service_exactly(self, ready_board, docs_root):
        client, _ticket_id = ready_board
        assert client.get("/demo/next").json() == \
            service.next_tickets(LocalDirStore(docs_root))

    def test_limit_and_tier_plumb_through(self, ready_board):
        client, ticket_id = ready_board
        assert ids(client.get("/demo/next").json()) == [ticket_id]
        assert ids(client.get("/demo/next?limit=5&tier=standard").json()) == [ticket_id]
        assert client.get("/demo/next?tier=heavy").json() == []

    def test_an_unknown_tier_is_422(self, client):
        r = client.get("/demo/next?tier=gigantic")
        assert r.status_code == 422
        assert "gigantic" in r.json()["detail"]

    def test_a_limit_below_one_is_422(self, client):
        r = client.get("/demo/next?limit=0")
        assert r.status_code == 422
        assert "positive integer" in r.json()["detail"]

    def test_an_unknown_board_is_404(self, client):
        assert client.get("/nosuch/next").status_code == 404

    def test_next_is_not_read_as_a_ticket_id(self, client):
        """`/{repo}/next` and `/{repo}/tickets/{id}` are different segments --
        no route shadows the other."""
        assert client.get("/demo/tickets/next").status_code == 404
        assert client.get("/demo/next").status_code == 200


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


class TestStatusEndpoint:
    """TASK-017: the first write. Rules live in `service.set_status` (pinned in
    test_service.py); these tests are about the HTTP skin over it."""

    def test_happy_path_with_awaiting(self, client):
        r = client.post("/demo/tickets/FEAT-002/status",
                        json={"status": "review", "awaiting": "deploy"})
        assert r.status_code == 200
        ticket = r.json()
        assert ticket["status"] == "review"
        assert ticket["awaiting"] == "deploy"

    def test_answers_what_a_get_would_say(self, client):
        """The response is the updated ticket dict and nothing reshaped."""
        posted = client.post("/demo/tickets/FEAT-002/status", json={"status": "review"}).json()
        assert posted == client.get("/demo/tickets/FEAT-002").json()

    def test_the_change_is_persisted(self, client):
        client.post("/demo/tickets/FEAT-002/status", json={"status": "complete"})
        assert client.get("/demo/tickets/FEAT-002").json()["status"] == "complete"

    def test_commits_come_from_the_body(self, client):
        sha = "e" * 40
        ticket = client.post("/demo/tickets/FEAT-002/status",
                             json={"status": "review", "commits": [sha]}).json()
        assert ticket["commits"] == [sha]

    def test_the_server_never_runs_git(self, client, monkeypatch):
        """Harvesting is the CLI's job -- it has a CWD and a checkout. A request
        that names no commits records none, whatever the server's CWD holds."""
        monkeypatch.setattr(commands, "_harvest_commits", lambda tid: ["f" * 40])
        ticket = client.post("/demo/tickets/FEAT-002/status", json={"status": "review"}).json()
        assert ticket["commits"] == []

    def test_awaiting_on_a_non_review_target_is_422(self, client):
        r = client.post("/demo/tickets/FEAT-002/status",
                        json={"status": "open", "awaiting": "deploy"})
        assert r.status_code == 422
        # The same sentence `llpm status` prints.
        assert r.json()["detail"] == (
            "--awaiting is only valid when the target status is 'review' (got 'open')."
        )
        assert client.get("/demo/tickets/FEAT-002").json()["status"] == "in-progress"

    def test_invalid_awaiting_is_422(self, client):
        r = client.post("/demo/tickets/FEAT-002/status",
                        json={"status": "review", "awaiting": "bogus"})
        assert r.status_code == 422
        assert "Invalid awaiting 'bogus'" in r.json()["detail"]

    def test_invalid_status_is_422(self, client):
        r = client.post("/demo/tickets/FEAT-002/status", json={"status": "shipped"})
        assert r.status_code == 422
        assert "Invalid status: 'shipped'" in r.json()["detail"]

    def test_derived_blocked_is_not_settable(self, client):
        r = client.post("/demo/tickets/FEAT-002/status", json={"status": "blocked"})
        assert r.status_code == 422

    def test_unknown_id_is_404(self, client):
        r = client.post("/demo/tickets/NOPE-999/status", json={"status": "open"})
        assert r.status_code == 404
        assert r.json()["detail"] == "Ticket 'NOPE-999' not found."

    def test_unknown_board_is_404(self, client):
        r = client.post("/nosuch/tickets/FEAT-002/status", json={"status": "open"})
        assert r.status_code == 404

    def test_missing_status_is_422(self, client):
        assert client.post("/demo/tickets/FEAT-002/status", json={}).status_code == 422

    def test_a_store_write_conflict_is_409(self, client, monkeypatch):
        """Nothing raises Conflict today (no store has a write precondition, so
        concurrent writes are last-writer-wins) -- the mapping is wired ahead of
        the ETag that would."""
        def boom(*a, **kw):
            raise service.Conflict("Ticket 'FEAT-002' changed underneath this write.")

        monkeypatch.setattr(service, "set_status", boom)
        r = client.post("/demo/tickets/FEAT-002/status", json={"status": "review"})
        assert r.status_code == 409
        assert "changed underneath" in r.json()["detail"]


class TestCreateEndpoint:
    """TASK-018: `POST /{repo}/tickets`. The intake and provenance rules are
    pinned in test_service.py; these are about the HTTP skin."""

    BODY = {"type": "task", "title": "Filed over HTTP", "created_by": "claude/session-1"}

    def test_201_with_the_ticket(self, client):
        r = client.post("/demo/tickets", json=self.BODY)
        assert r.status_code == 201
        ticket = r.json()
        assert ticket["id"] == "TASK-002"
        assert ticket["title"] == "Filed over HTTP"
        assert ticket["origin"] == "agent"
        assert ticket["created_by"] == "claude/session-1"
        assert ticket["managed_by"] == "llpm"

    def test_answers_what_a_get_would_say(self, client):
        posted = client.post("/demo/tickets", json=self.BODY).json()
        assert posted == client.get(f"/demo/tickets/{posted['id']}").json()

    def test_it_lands_on_the_board(self, client):
        client.post("/demo/tickets", json=self.BODY)
        assert "TASK-002" in ids(client.get("/demo/tickets").json())

    def test_an_unsigned_create_is_422(self, client):
        """`created_by` is required over HTTP: the server has no shell to infer
        a caller from, so a ticket can't arrive anonymous."""
        r = client.post("/demo/tickets", json={"type": "task", "title": "Anonymous"})
        assert r.status_code == 422

    def test_agent_origin_lands_draft(self, client):
        assert client.post("/demo/tickets", json={**self.BODY, "body": "## Description\n\nGo.\n"}
                           ).json()["status"] == "draft"

    def test_human_origin_with_a_body_is_open(self, client):
        r = client.post("/demo/tickets", json={**self.BODY, "origin": "human",
                                               "body": "## Description\n\nGo.\n"})
        assert r.json()["status"] == "open"

    def test_optional_fields(self, client):
        ticket = client.post("/demo/tickets", json={
            **self.BODY, "priority": "high", "effort": "small", "parent": "FEAT-001",
            "tags": ["auth", "security"], "requires_human": True,
        }).json()
        assert ticket["priority"] == "high"
        assert ticket["effort"] == "small"
        assert ticket["parent"] == "FEAT-001"
        assert ticket["tags"] == ["auth", "security"]
        assert ticket["requires_human"] is True

    def test_serves_on_a_feature(self, client):
        ticket = client.post("/demo/tickets", json={
            "type": "feature", "title": "Goal-serving", "created_by": "claude/1",
            "serves": ["goals.unified-agent-platform"],
        }).json()
        assert ticket["serves"] == ["goals.unified-agent-platform"]

    def test_serves_on_a_task_is_422(self, client):
        r = client.post("/demo/tickets", json={**self.BODY, "serves": ["goals.x"]})
        assert r.status_code == 422
        assert "only valid on epics/features" in r.json()["detail"]

    def test_unknown_parent_is_422(self, client):
        r = client.post("/demo/tickets", json={**self.BODY, "parent": "NOPE-999"})
        assert r.status_code == 422
        assert r.json()["detail"] == "Parent ticket 'NOPE-999' not found."

    def test_bad_enum_is_422_with_llpms_message(self, client):
        r = client.post("/demo/tickets", json={**self.BODY, "priority": "urgent"})
        assert r.status_code == 422
        assert "Invalid priority 'urgent'" in r.json()["detail"]

    def test_unknown_type_is_422(self, client):
        r = client.post("/demo/tickets", json={**self.BODY, "type": "banana"})
        assert r.status_code == 422
        assert r.json()["detail"] == "No template found for type 'banana'."

    def test_unknown_board_is_404(self, client):
        assert client.post("/nosuch/tickets", json=self.BODY).status_code == 404


class TestConcurrentCreates:
    """Atomic create is what lets parallel agents share one board: two requests
    that race must never be handed the same ID."""

    @pytest.fixture(params=["local", "vault"])
    def app_for(self, request, docs_root):
        store = LocalDirStore(docs_root) if request.param == "local" else load_fake_store(docs_root)
        return api.make_app(lambda repo: store), store

    def test_two_racing_posts_get_two_ids(self, app_for):
        app, store = app_for
        body = {"type": "task", "title": "Raced", "created_by": "claude/1"}

        def post():
            # A client per thread: TestClient's portal is not the thing under
            # test, the store's exclusive create is.
            return TestClient(app).post("/demo/tickets", json=body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [f.result() for f in [pool.submit(post), pool.submit(post)]]

        assert [r.status_code for r in responses] == [201, 201]
        created = sorted(r.json()["id"] for r in responses)
        assert created == ["TASK-002", "TASK-003"]
        assert len(store.list_tickets(include_archive=False)) == 7


class TestPatchEndpoint:
    """TASK-018: `PATCH /{repo}/tickets/{id}` -- the body is the assignments."""

    def test_sets_a_field(self, client):
        ticket = client.patch("/demo/tickets/TASK-001", json={"priority": "high"}).json()
        assert ticket["priority"] == "high"
        assert client.get("/demo/tickets/TASK-001").json()["priority"] == "high"

    def test_answers_what_a_get_would_say(self, client):
        patched = client.patch("/demo/tickets/TASK-001", json={"priority": "high"}).json()
        assert patched == client.get("/demo/tickets/TASK-001").json()

    def test_several_fields_at_once(self, client):
        ticket = client.patch("/demo/tickets/TASK-001",
                              json={"priority": "low", "effort": "large"}).json()
        assert (ticket["priority"], ticket["effort"]) == ("low", "large")

    def test_json_types_are_taken_as_they_are(self, client):
        ticket = client.patch("/demo/tickets/TASK-001", json={
            "hours": 9, "tags": ["auth"], "requires_human": True, "effort": None,
        }).json()
        assert ticket["hours"] == 9
        assert ticket["tags"] == ["auth"]
        assert ticket["requires_human"] is True
        assert ticket["effort"] is None

    def test_a_field_with_its_own_endpoint_is_422(self, client):
        r = client.patch("/demo/tickets/TASK-001", json={"status": "complete"})
        assert r.status_code == 422
        assert r.json()["detail"] == "Cannot set 'status' via 'set'. Use 'llpm status'."
        assert client.get("/demo/tickets/TASK-001").json()["status"] == "open"

    def test_a_system_written_field_is_422(self, client):
        r = client.patch("/demo/tickets/TASK-001", json={"created_by": "someone else"})
        assert r.status_code == 422
        assert r.json()["detail"] == "Cannot set 'created_by' -- managed automatically."

    def test_a_bad_enum_is_422(self, client):
        r = client.patch("/demo/tickets/TASK-001", json={"priority": "urgent"})
        assert r.status_code == 422
        assert "Invalid priority 'urgent'" in r.json()["detail"]

    def test_an_empty_patch_is_422(self, client):
        assert client.patch("/demo/tickets/TASK-001", json={}).status_code == 422

    def test_unknown_id_is_404(self, client):
        r = client.patch("/demo/tickets/NOPE-999", json={"priority": "high"})
        assert r.status_code == 404
        assert r.json()["detail"] == "Ticket 'NOPE-999' not found."


class TestBlockerEndpoints:
    """TASK-019: one pair in full. The other three are the same handler shape
    over the same service pattern, smoke-tested below."""

    def test_add_answers_with_the_updated_ticket(self, client):
        r = client.post("/demo/tickets/FEAT-002/blockers", json={"id": "TASK-001"})
        assert r.status_code == 200
        ticket = r.json()
        assert ticket["blockers"] == [{"id": "TASK-001", "resolved": False}]

    def test_the_effect_is_visible_at_once(self, client):
        """A blocker changes a derived field, so answering with the ticket
        saves the caller a follow-up GET."""
        ticket = client.post("/demo/tickets/FEAT-002/blockers",
                             json={"id": "TASK-001"}).json()
        assert ticket["is_blocked"] is True
        assert ticket["effective_status"] == "blocked"
        assert ticket == client.get("/demo/tickets/FEAT-002").json()

    def test_add_is_idempotent(self, client):
        client.post("/demo/tickets/FEAT-002/blockers", json={"id": "TASK-001"})
        r = client.post("/demo/tickets/FEAT-002/blockers", json={"id": "TASK-001"})
        assert r.status_code == 200
        assert r.json()["blockers"] == [{"id": "TASK-001", "resolved": False}]

    def test_delete(self, client):
        ticket = client.request("DELETE", "/demo/tickets/TASK-001/blockers",
                                json={"id": "FEAT-002"}).json()
        assert ticket["blockers"] == [{"id": "FEAT-001", "resolved": True}]
        assert ticket["is_blocked"] is False

    def test_delete_of_a_missing_edge_is_404(self, client):
        r = client.request("DELETE", "/demo/tickets/TASK-001/blockers",
                           json={"id": "EPIC-001"})
        assert r.status_code == 404
        assert r.json()["detail"] == "'EPIC-001' is not a blocker on TASK-001."

    def test_unknown_blocker_is_422(self, client):
        r = client.post("/demo/tickets/FEAT-002/blockers", json={"id": "NOPE-999"})
        assert r.status_code == 422
        assert r.json()["detail"] == "Ticket 'NOPE-999' not found."

    def test_self_blocker_is_422(self, client):
        r = client.post("/demo/tickets/FEAT-002/blockers", json={"id": "FEAT-002"})
        assert r.status_code == 422
        assert "cannot block itself" in r.json()["detail"]

    def test_unknown_ticket_is_404(self, client):
        r = client.post("/demo/tickets/NOPE-999/blockers", json={"id": "FEAT-002"})
        assert r.status_code == 404

    def test_a_missing_target_is_422(self, client):
        assert client.post("/demo/tickets/FEAT-002/blockers", json={}).status_code == 422


class TestOtherEdgeEndpoints:
    """A smoke per remaining pair: add, see it on the ticket, remove it."""

    def test_after(self, client):
        ticket = client.post("/demo/tickets/FEAT-002/after", json={"id": "TASK-001"}).json()
        assert ticket["after"] == ["TASK-001"]
        assert ticket["is_blocked"] is False  # soft edge, never blocks
        ticket = client.request("DELETE", "/demo/tickets/FEAT-002/after",
                                json={"id": "TASK-001"}).json()
        assert ticket["after"] == []

    def test_waits(self, client):
        stem = "repos.marginalia.llpm.features.FEAT-010"
        ticket = client.post("/demo/tickets/FEAT-002/waits", json={"stem": stem}).json()
        assert [w["stem"] for w in ticket["waits_on"]] == [stem]
        ticket = client.request("DELETE", "/demo/tickets/FEAT-002/waits",
                                json={"stem": stem}).json()
        assert ticket["waits_on"] == []

    def test_waits_refuses_a_ticket_id(self, client):
        r = client.post("/demo/tickets/FEAT-002/waits", json={"stem": "FEAT-001"})
        assert r.status_code == 422
        assert "holds full vault stems" in r.json()["detail"]

    def test_serves(self, client):
        goal = "goals.unified-agent-platform"
        ticket = client.post("/demo/tickets/FEAT-002/serves", json={"stem": goal}).json()
        assert ticket["serves"] == [goal]
        ticket = client.request("DELETE", "/demo/tickets/FEAT-002/serves",
                                json={"stem": goal}).json()
        assert ticket["serves"] == []

    def test_serves_on_a_task_is_422(self, client):
        r = client.post("/demo/tickets/TASK-001/serves", json={"stem": "goals.x"})
        assert r.status_code == 422
        assert "only valid on epics/features" in r.json()["detail"]

    def test_delete_of_a_missing_edge_is_404(self, client):
        for edge, body in (("after", {"id": "TASK-001"}),
                           ("waits", {"stem": "repos.x.llpm.features.FEAT-001"}),
                           ("serves", {"stem": "goals.nope"})):
            r = client.request("DELETE", f"/demo/tickets/FEAT-002/{edge}", json=body)
            assert r.status_code == 404, edge


# ---------------------------------------------------------------------------
# MCP over the streamable-HTTP transport (FEAT-016)
#
# The protocol itself is pinned in test_mcp.py; what matters here is that the
# mount speaks it -- board from the path, JSON in and out, and a session that
# deliberately does not outlive the request.
# ---------------------------------------------------------------------------

def rpc(client, method, params=None, message_id=1):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return client.post("/demo/mcp", json=message)


class TestMcpEndpoint:
    def test_initialize(self, client):
        result = rpc(client, "initialize", {"protocolVersion": mcp.PROTOCOL_VERSION}).json()
        assert result["result"]["serverInfo"]["name"] == "llpm"

    def test_tools_are_listed(self, client):
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert {t["name"] for t in tools} == {t.name for t in mcp.TOOLS}

    def test_a_tool_call_reads_the_board_in_the_path(self, client):
        result = rpc(client, "tools/call",
                     {"name": "get_ticket", "arguments": {"id": "FEAT-001"}}).json()
        assert json.loads(result["result"]["content"][0]["text"])["id"] == "FEAT-001"

    def test_an_unknown_board_is_404_not_a_tool_error(self, client):
        """The board is addressed by the transport, so a bad one fails there."""
        r = client.post("/nosuch/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 404

    def test_a_notification_is_accepted_with_no_body(self, client):
        r = client.post("/demo/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202
        assert r.content == b""

    def test_a_stateless_session_makes_the_caller_name_itself(self, client):
        """No session survives the request, so `initialize` can't supply
        provenance -- the same ruling POST /tickets makes."""
        result = rpc(client, "tools/call", {
            "name": "create_ticket",
            "arguments": {"type": "task", "title": "Anonymous"},
        }).json()["result"]
        assert result["isError"] is True
        assert "created_by" in result["content"][0]["text"]

    def test_a_named_caller_can_file_one(self, client):
        result = rpc(client, "tools/call", {
            "name": "create_ticket",
            "arguments": {"type": "task", "title": "Filed over MCP",
                          "created_by": "agent-1"},
        }).json()["result"]
        created = json.loads(result["content"][0]["text"])
        assert created["status"] == "draft"
        assert client.get(f"/demo/tickets/{created['id']}").json()["created_by"] == "agent-1"

    def test_no_sse_stream_says_so(self, client):
        assert client.get("/demo/mcp").status_code == 405
