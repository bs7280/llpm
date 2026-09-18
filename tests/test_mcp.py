"""MCP tools over the service layer (FEAT-016).

Three things are being protected:

1. The protocol an MCP client actually speaks -- handshake, tools/list,
   tools/call -- answered over a real stdio round trip, not just in-process.
2. That the tools are *faces for the service* and nothing more: what a tool
   returns is what the service returns, and a rule llpm refuses comes back as
   llpm's own sentence for the model to read.
3. Provenance: a ticket filed over MCP lands the way an agent-filed ticket
   lands, stamped with a caller the server did not invent.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from conftest import load_fake_store
from llpm import mcp, service
from llpm.store import LocalDirStore, MdTreeStoreError


@pytest.fixture(params=["local", "vault"])
def store(request, docs_root):
    """The fixture board, once as a LocalDirStore and once as a fake vault."""
    if request.param == "local":
        return LocalDirStore(docs_root)
    return load_fake_store(docs_root)


@pytest.fixture
def session(store):
    """A session that has been through the handshake, as a client's would be."""
    s = mcp.Session(store)
    s.handle(_request(0, "initialize", {
        "protocolVersion": mcp.PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "claude-code", "version": "2.1.4"},
    }))
    return s


def _request(message_id, method, params=None) -> dict:
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(session, tool, **arguments) -> dict:
    """One tools/call, answered. Returns the tool result (content + friends)."""
    reply = session.handle(_request(1, "tools/call",
                                    {"name": tool, "arguments": arguments}))
    assert "error" not in reply, reply
    return reply["result"]


def payload(result):
    """What the tool said, decoded from its text content."""
    return json.loads(result["content"][0]["text"])


def error_text(result) -> str:
    assert result["isError"] is True, result
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Handshake and tool listing
# ---------------------------------------------------------------------------

class TestHandshake:
    def test_initialize_answers_a_tools_server(self, store):
        reply = mcp.Session(store).handle(_request(0, "initialize", {
            "protocolVersion": mcp.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "2.1.4"},
        }))
        result = reply["result"]
        assert result["protocolVersion"] == mcp.PROTOCOL_VERSION
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "llpm"
        assert "blocked" in result["instructions"]

    def test_an_older_protocol_the_server_supports_is_kept(self, store):
        reply = mcp.Session(store).handle(
            _request(0, "initialize", {"protocolVersion": "2024-11-05"})
        )
        assert reply["result"]["protocolVersion"] == "2024-11-05"

    def test_an_unknown_protocol_gets_this_server_s_own(self, store):
        reply = mcp.Session(store).handle(
            _request(0, "initialize", {"protocolVersion": "1999-01-01"})
        )
        assert reply["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION

    def test_a_notification_is_never_answered(self, session):
        assert session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_ping(self, session):
        assert session.handle(_request(7, "ping"))["result"] == {}

    def test_an_unknown_method_is_a_protocol_error(self, session):
        reply = session.handle(_request(8, "resources/list"))
        assert reply["error"]["code"] == -32601
        assert "resources/list" in reply["error"]["message"]

    def test_a_non_jsonrpc_message_is_refused(self, session):
        assert session.handle({"method": "ping", "id": 1})["error"]["code"] == -32600


class TestToolListing:
    """What an MCP client sees -- and that it stays a face for the service."""

    EXPECTED = {
        "list_tickets", "next_tickets", "get_ticket", "create_ticket",
        "set_status", "set_fields",
        "blocker_add", "blocker_rm", "after_add", "after_rm",
        "waits_add", "waits_rm", "serves_add", "serves_rm",
    }

    def test_every_tool_is_listed(self, session):
        listed = session.handle(_request(2, "tools/list"))["result"]["tools"]
        assert {t["name"] for t in listed} == self.EXPECTED

    def test_each_entry_is_usable_by_a_client(self, session):
        for tool in session.handle(_request(2, "tools/list"))["result"]["tools"]:
            assert tool["description"].strip()
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            # Everything required is described, or a client cannot fill it in.
            assert set(schema["required"]) <= set(schema["properties"])
            assert all(p.get("type") for p in schema["properties"].values())

    def test_the_registry_has_no_duplicates(self):
        assert len(mcp.TOOLS) == len(mcp.TOOLS_BY_NAME) == len(self.EXPECTED)

    def test_an_unknown_tool_is_a_protocol_error(self, session):
        reply = session.handle(_request(3, "tools/call", {"name": "drop_board"}))
        assert reply["error"]["code"] == -32602
        assert "drop_board" in reply["error"]["message"]


# ---------------------------------------------------------------------------
# Reads -- the service's answer, verbatim
# ---------------------------------------------------------------------------

class TestReadTools:
    def test_list_tickets_is_the_service_call(self, session, store):
        assert payload(call(session, "list_tickets")) == \
            json.loads(json.dumps(service.list_tickets(store), default=str))

    def test_filters_travel(self, session):
        listed = payload(call(session, "list_tickets", status="blocked"))
        assert [t["id"] for t in listed] == ["TASK-001"]

    def test_fields_trims(self, session):
        listed = payload(call(session, "list_tickets", fields=["id", "title"]))
        assert all(set(t) == {"id", "title"} for t in listed)

    def test_get_ticket_carries_the_body(self, session):
        ticket = payload(call(session, "get_ticket", id="FEAT-001"))
        assert ticket["id"] == "FEAT-001"
        assert "## Problem" in ticket["body"]

    def test_body_can_be_switched_off(self, session):
        assert "body" not in payload(call(session, "get_ticket", id="FEAT-001", body=False))

    def test_a_dict_result_is_also_structured(self, session):
        result = call(session, "get_ticket", id="FEAT-001")
        assert result["structuredContent"] == payload(result)

    def test_a_missing_ticket_is_a_tool_error(self, session):
        assert "NOPE-999" in error_text(call(session, "get_ticket", id="NOPE-999"))

    def test_a_missing_required_argument_says_so(self, session):
        assert error_text(call(session, "get_ticket")) == "Error: 'id' is required."


class TestNextTicketsTool:
    """FEAT-009's face. Which tickets are ready and in what order is pinned in
    test_service.py; what matters here is that the tool is that call and adds
    nothing of its own."""

    @pytest.fixture
    def ready(self, store):
        """The fixture board plus one ticket nothing stops a worker taking --
        the fixture board alone has nothing ready (TASK-001 is blocked)."""
        result = service.create_ticket(
            store, "task", "Fresh work",
            body="## Acceptance Criteria\n\n- [ ] Works\n",
            origin="human", created_by="test", today="2026-03-20",
        )
        service.set_fields(store, result["id"], {"effort": "small"}, today="2026-03-20")
        service.set_status(store, result["id"], "open", today="2026-03-20")
        return result["id"]

    def test_is_the_service_call(self, session, store, ready):
        assert payload(call(session, "next_tickets")) == \
            json.loads(json.dumps(service.next_tickets(store), default=str))

    def test_a_dry_queue_is_an_empty_list(self, session):
        assert payload(call(session, "next_tickets")) == []

    def test_one_by_default_and_limit_for_more(self, session, store, ready):
        service.set_status(store, "TASK-001", "planned", today="2026-03-20")
        second = service.create_ticket(
            store, "task", "More fresh work",
            body="## Acceptance Criteria\n\n- [ ] Works\n",
            origin="human", created_by="test", today="2026-03-20",
        )["id"]
        service.set_fields(store, second, {"effort": "small"}, today="2026-03-20")
        service.set_status(store, second, "open", today="2026-03-20")

        assert len(payload(call(session, "next_tickets"))) == 1
        assert [t["id"] for t in payload(call(session, "next_tickets", limit=5))] == \
            sorted([ready, second])

    def test_tier_travels(self, session, ready):
        assert [t["id"] for t in payload(call(session, "next_tickets", tier="standard"))] \
            == [ready]
        assert payload(call(session, "next_tickets", tier="heavy")) == []

    def test_an_unknown_tier_is_llpm_s_own_sentence(self, session):
        assert "Invalid model_tier: 'gigantic'" in \
            error_text(call(session, "next_tickets", tier="gigantic"))

    def test_a_non_integer_limit_is_refused_by_the_schema(self, session):
        assert "'limit' must be an integer" in \
            error_text(call(session, "next_tickets", limit="2"))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

class TestCreateTicket:
    def test_lands_draft_with_provenance_from_the_handshake(self, session, store):
        created = payload(call(session, "create_ticket", type="task",
                               title="Wire the reconciler"))
        assert created["status"] == "draft"

        _, fm, _ = service.read_ticket(store, created["id"])
        assert (fm["origin"], fm["created_by"]) == ("agent", "claude-code/2.1.4")
        assert fm["managed_by"] == "llpm"

    def test_a_body_does_not_beat_the_intake_policy(self, session, store):
        """An agent-filed ticket lands draft even fully specified -- a planner
        promotes it. The board opts a type in, not the caller."""
        created = payload(call(session, "create_ticket", type="task",
                               title="Spec'd already", body="## Goal\nDo it.\n"))
        assert created["status"] == "draft"

    def test_an_auto_approved_type_lands_open(self, store):
        s = mcp.Session(store, auto_approve=("task",), created_by="agent-1")
        created = payload(call(s, "create_ticket", type="task", title="Approved",
                               body="## Goal\nDo it.\n"))
        assert created["status"] == "open"

    def test_the_call_can_name_its_own_author(self, session, store):
        created = payload(call(session, "create_ticket", type="task", title="Relayed",
                               origin="human", created_by="ben"))
        _, fm, _ = service.read_ticket(store, created["id"])
        assert (fm["origin"], fm["created_by"]) == ("human", "ben")

    def test_a_server_with_no_identity_makes_the_caller_say(self, store):
        """No handshake, no --created-by, no env: the server must not invent
        provenance -- the same ruling the REST surface makes."""
        assert "created_by" in error_text(
            call(mcp.Session(store), "create_ticket", type="task", title="Anonymous")
        )

    def test_llpm_s_own_rules_still_apply(self, session):
        assert "Invalid priority" in error_text(
            call(session, "create_ticket", type="task", title="x", priority="urgent")
        )
        assert "serves" in error_text(
            call(session, "create_ticket", type="task", title="x", serves=["goals.x"])
        )


class TestSetStatus:
    def test_changes_the_status(self, session, store):
        result = payload(call(session, "set_status", id="TASK-001", status="review"))
        assert (result["previous_status"], result["status"]) == ("open", "review")
        assert service.read_ticket(store, "TASK-001")[1]["status"] == "review"

    def test_an_invalid_transition_comes_back_in_llpm_s_words(self, session, store):
        message = error_text(call(session, "set_status", id="TASK-001", status="shipped"))
        assert message.startswith("Error: Invalid status: 'shipped'. Must be one of: ")
        assert service.read_ticket(store, "TASK-001")[1]["status"] == "open"

    def test_awaiting_outside_review_is_refused(self, session):
        assert "--awaiting" in error_text(
            call(session, "set_status", id="TASK-001", status="complete",
                 awaiting="reviewer")
        )

    def test_commits_are_recorded_as_named(self, session, store):
        """This server never runs git: SHAs come from the caller."""
        result = payload(call(session, "set_status", id="TASK-001", status="review",
                              commits=["abc123"]))
        assert result["commits_captured"] == 1
        assert service.read_ticket(store, "TASK-001")[1]["commits"] == ["abc123"]


class TestSetFields:
    def test_sets(self, session, store):
        result = payload(call(session, "set_fields", id="FEAT-001",
                              fields={"priority": "low", "effort": "small"}))
        assert [c["field"] for c in result["changes"]] == ["priority", "effort"]
        fm = service.read_ticket(store, "FEAT-001")[1]
        assert (fm["priority"], fm["effort"]) == ("low", "small")

    def test_a_field_with_its_own_tool_is_refused(self, session):
        assert "llpm status" in error_text(
            call(session, "set_fields", id="FEAT-001", fields={"status": "review"})
        )

    def test_provenance_is_not_settable(self, session):
        assert "managed automatically" in error_text(
            call(session, "set_fields", id="FEAT-001", fields={"created_by": "someone"})
        )

    def test_fields_must_be_an_object(self, session):
        assert "must be an object" in error_text(
            call(session, "set_fields", id="FEAT-001", fields=["priority=low"])
        )


class TestEdgeTools:
    def test_blocker_add_and_rm(self, session, store):
        added = payload(call(session, "blocker_add", id="FEAT-001", blocker="FEAT-002"))
        assert (added["target"], added["changed"]) == ("FEAT-002", True)
        assert service.read_ticket(store, "FEAT-001")[1]["blockers"] == ["FEAT-002"]

        payload(call(session, "blocker_rm", id="FEAT-001", blocker="FEAT-002"))
        assert service.read_ticket(store, "FEAT-001")[1]["blockers"] == []

    def test_a_blocker_must_be_a_real_ticket(self, session):
        assert "not found" in error_text(
            call(session, "blocker_add", id="FEAT-001", blocker="TASK-404")
        )

    def test_after_reports_a_cycle_without_refusing_it(self, session):
        payload(call(session, "after_add", id="FEAT-001", after="FEAT-002"))
        result = payload(call(session, "after_add", id="FEAT-002", after="FEAT-001"))
        assert result["cycle_warning"] is True

    def test_waits_takes_stems_not_ids(self, session, store):
        stem = "repos.marginalia.llpm.features.FEAT-010"
        result = payload(call(session, "waits_add", id="FEAT-001", stem=stem))
        assert result["changed"] is True
        assert service.read_ticket(store, "FEAT-001")[1]["waits_on"] == [stem]

        assert "full vault stems" in error_text(
            call(session, "waits_add", id="FEAT-001", stem="FEAT-002")
        )

    def test_waits_rm(self, session, store):
        stem = "repos.marginalia.llpm.features.FEAT-010"
        call(session, "waits_add", id="FEAT-001", stem=stem)
        payload(call(session, "waits_rm", id="FEAT-001", stem=stem))
        assert service.read_ticket(store, "FEAT-001")[1]["waits_on"] == []

    def test_serves_is_for_epics_and_features(self, session, store):
        payload(call(session, "serves_add", id="FEAT-001", stem="goals.autonomous-task-loop"))
        assert service.read_ticket(store, "FEAT-001")[1]["serves"] == \
            ["goals.autonomous-task-loop"]

        assert "epics/features" in error_text(
            call(session, "serves_add", id="TASK-001", stem="goals.x")
        )

    def test_serves_rm(self, session, store):
        call(session, "serves_add", id="FEAT-001", stem="goals.x")
        payload(call(session, "serves_rm", id="FEAT-001", stem="goals.x"))
        assert service.read_ticket(store, "FEAT-001")[1]["serves"] == []

    def test_every_edge_tool_pairs_add_with_rm(self):
        edges = {n.rsplit("_", 1)[0] for n in mcp.TOOLS_BY_NAME if n.endswith(("_add", "_rm"))}
        for edge in edges:
            assert {f"{edge}_add", f"{edge}_rm"} <= set(mcp.TOOLS_BY_NAME)


# ---------------------------------------------------------------------------
# Wrong-typed arguments and malformed envelopes
# ---------------------------------------------------------------------------

class TestArgumentTypeValidation:
    """A tool call straight into ``service.py`` trusts its caller's types --
    these are the three repro cases from mission-control's review (session
    e84197f0): each silently corrupted data instead of refusing the call.
    ``list("abc123")`` makes six one-character SHAs, a ``fields`` string is
    iterated as characters, ``_as_list(5)`` makes a tag out of the int."""

    def test_set_status_commits_must_be_an_array(self, session, store):
        before = service.read_ticket(store, "TASK-001")[1].get("commits") or []
        message = error_text(call(session, "set_status", id="TASK-001",
                                  status="review", commits="abc123"))
        assert "'commits'" in message
        assert "array" in message
        assert service.read_ticket(store, "TASK-001")[1].get("commits") in (None, before)

    def test_list_tickets_fields_must_be_an_array(self, session):
        message = error_text(call(session, "list_tickets", fields="id,title"))
        assert "'fields'" in message
        assert "array" in message

    def test_create_ticket_tags_must_be_an_array(self, session, store):
        before = {t["id"] for t in service.list_tickets(store)}
        message = error_text(call(session, "create_ticket", type="task",
                                  title="Should not be filed", tags=5))
        assert "'tags'" in message
        assert "array" in message
        assert {t["id"] for t in service.list_tickets(store)} == before

    def test_array_items_are_type_checked_too(self, session):
        message = error_text(call(session, "set_status", id="TASK-001",
                                  status="review", commits=[123]))
        assert "'commits' items" in message
        assert "string" in message

    def test_a_boolean_argument_rejects_a_string(self, session):
        message = error_text(call(session, "get_ticket", id="TASK-001", body="yes"))
        assert "'body'" in message
        assert "boolean" in message


class TestMalformedEnvelope:
    """Bad JSON-RPC structure -- not a bad tool call -- so it never reaches
    llpm's own rules. Answered as -32602 with a sentence, never -32603 with
    raw Python exception text (the AttributeError/TypeError that surfaced
    before this fix)."""

    def test_non_object_params_is_invalid_params_not_a_crash(self, session):
        reply = session.handle(_request(1, "tools/call", ["not", "an", "object"]))
        assert reply["error"]["code"] == -32602
        assert "params" in reply["error"]["message"]

    def test_non_string_tool_name_is_invalid_params_not_a_crash(self, session):
        reply = session.handle(_request(
            2, "tools/call", {"name": ["get_ticket"], "arguments": {}}
        ))
        assert reply["error"]["code"] == -32602
        assert "name" in reply["error"]["message"]

    def test_non_scalar_id_is_invalid_params_not_a_crash(self, session):
        reply = session.handle({"jsonrpc": "2.0", "id": {"not": "scalar"}, "method": "ping"})
        assert reply["error"]["code"] == -32602
        assert "id" in reply["error"]["message"]
        assert reply["id"] is None


# ---------------------------------------------------------------------------
# Degrading
# ---------------------------------------------------------------------------

class TestFailureModes:
    def test_an_unreachable_vault_is_a_readable_tool_error(self, session, monkeypatch):
        """The store's message is written to be acted on, so it has to reach
        the model as tool output rather than a JSON-RPC code."""
        def boom(*a, **kw):
            raise MdTreeStoreError("Could not reach the vault at https://vault.test")

        monkeypatch.setattr(session.store, "read", boom)
        assert "Could not reach the vault" in error_text(
            call(session, "get_ticket", id="FEAT-001")
        )

    def test_an_unexpected_failure_does_not_end_the_session(self, session, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(session.store, "read", boom)
        reply = session.handle(_request(9, "tools/call",
                                        {"name": "get_ticket", "arguments": {"id": "FEAT-001"}}))
        assert reply["error"]["code"] == -32603
        assert "disk on fire" in reply["error"]["message"]
        # ...and the next call still works.
        assert session.handle(_request(10, "ping"))["result"] == {}


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------

class TestStdioTransport:
    def _run(self, store, *messages):
        stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        stdout = io.StringIO()
        mcp.serve_stdio(mcp.Session(store, created_by="agent-1"), stdin, stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_a_whole_client_conversation(self, store):
        replies = self._run(
            store,
            _request(1, "initialize", {"protocolVersion": mcp.PROTOCOL_VERSION,
                                       "clientInfo": {"name": "claude-code"}}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _request(2, "tools/list"),
            _request(3, "tools/call", {"name": "get_ticket",
                                       "arguments": {"id": "FEAT-001", "body": False}}),
        )
        # The notification is not answered, so three requests -> three replies.
        assert [r["id"] for r in replies] == [1, 2, 3]
        assert replies[0]["result"]["serverInfo"]["name"] == "llpm"
        assert len(replies[1]["result"]["tools"]) == len(mcp.TOOLS)
        assert payload(replies[2]["result"])["id"] == "FEAT-001"

    def test_blank_lines_are_skipped(self, store):
        stdin = io.StringIO("\n\n" + json.dumps(_request(1, "ping")) + "\n")
        stdout = io.StringIO()
        mcp.serve_stdio(mcp.Session(store), stdin, stdout)
        assert len(stdout.getvalue().splitlines()) == 1

    def test_unparseable_input_is_answered_not_fatal(self, store):
        stdin = io.StringIO("{not json\n" + json.dumps(_request(1, "ping")) + "\n")
        stdout = io.StringIO()
        mcp.serve_stdio(mcp.Session(store), stdin, stdout)
        first, second = [json.loads(line) for line in stdout.getvalue().splitlines()]
        assert (first["id"], first["error"]["code"]) == (None, -32700)
        assert second["id"] == 1

    def test_every_line_is_one_json_object(self, store):
        """The framing the transport depends on: no pretty-printing on the wire,
        whatever the tool result looks like inside."""
        replies = self._run(store, _request(1, "tools/call",
                                            {"name": "list_tickets", "arguments": {}}))
        assert len(replies) == 1


# ---------------------------------------------------------------------------
# `llpm mcp` -- the command a client is registered against
# ---------------------------------------------------------------------------

class TestTheCommand:
    """A real process, real stdio framing, and no optional extra: an MCP client
    has to work on the pyyaml-only install, so fastapi/uvicorn are blocked here
    the way test_serve.py blocks them."""

    BLOCK_EXTRA = (
        "import sys\n"
        "sys.modules['fastapi'] = None\n"
        "sys.modules['uvicorn'] = None\n"
    )

    def _run(self, cwd, *messages):
        code = self.BLOCK_EXTRA + "from llpm.__main__ import main\nmain(['mcp'])\n"
        return subprocess.run(
            [sys.executable, "-c", code],
            input="".join(json.dumps(m) + "\n" for m in messages),
            capture_output=True, text=True, cwd=str(cwd),
        )

    @pytest.fixture
    def board(self, tmp_path):
        (tmp_path / "myrepo" / "llpm" / "tickets").mkdir(parents=True)
        return tmp_path / "myrepo"

    def test_a_client_conversation_without_the_api_extra(self, board):
        result = self._run(
            board,
            _request(1, "initialize", {"protocolVersion": mcp.PROTOCOL_VERSION,
                                       "clientInfo": {"name": "claude-code"}}),
            _request(2, "tools/list"),
            _request(3, "tools/call", {"name": "list_tickets", "arguments": {}}),
        )
        assert result.returncode == 0, result.stderr
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        assert [r["id"] for r in replies] == [1, 2, 3]
        assert len(replies[1]["result"]["tools"]) == len(mcp.TOOLS)
        assert payload(replies[2]["result"]) == []  # an empty board, not an error

    def test_the_banner_stays_off_the_protocol_stream(self, board):
        result = self._run(board, _request(1, "ping"))
        assert result.stdout.strip() == json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        assert "llpm mcp:" in result.stderr
        assert "myrepo" in result.stderr

    def test_it_files_tickets_as_an_agent(self, board):
        """No LLPM_ORIGIN in the environment: an MCP caller is an agent anyway,
        and the client's handshake name is who filed it."""
        result = self._run(
            board,
            _request(1, "initialize", {"clientInfo": {"name": "claude-code",
                                                      "version": "2.1.4"}}),
            _request(2, "tools/call", {"name": "create_ticket",
                                       "arguments": {"type": "task", "title": "Filed"}}),
            _request(3, "tools/call", {"name": "get_ticket",
                                       "arguments": {"id": "TASK-001", "body": False}}),
        )
        assert result.returncode == 0, result.stderr
        ticket = payload(json.loads(result.stdout.splitlines()[2])["result"])
        assert (ticket["status"], ticket["origin"]) == ("draft", "agent")
        assert ticket["created_by"] == "claude-code/2.1.4"
