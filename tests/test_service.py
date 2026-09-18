"""Tests for the service layer (TASK-016 reads, TASK-017 ``set_status``,
TASK-018 ``create_ticket``/``set_fields``, TASK-019 the edge pairs).

Every service function runs against BOTH stores -- the local directory and the
fake vault -- holding the same board, because the whole point of the seam is
that a board answers the same way whichever store it is read through.

The CLI regression net lives in test_commands.py / test_json.py: those tests are
unchanged, and every `cmd_*` that touches ticket data now reaches it through
here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import load_fake_store
from llpm import parser, service
from llpm.store import LocalDirStore


@pytest.fixture(params=["local", "vault"])
def store(request, docs_root):
    """The fixture board, once as a LocalDirStore and once as a fake vault."""
    if request.param == "local":
        return LocalDirStore(docs_root)
    return load_fake_store(docs_root)


def ids(tickets) -> list[str]:
    return [t["id"] for t in tickets]


# ---------------------------------------------------------------------------
# list_tickets
# ---------------------------------------------------------------------------

class TestListTickets:
    def test_active_board_sorted_by_priority_then_id(self, store):
        assert ids(service.list_tickets(store)) == [
            "EPIC-001", "FEAT-001", "FEAT-002",  # high
            "RESEARCH-001", "TASK-001",          # medium
        ]

    def test_archive_excluded_by_default(self, store):
        assert "FEAT-000" not in ids(service.list_tickets(store))
        assert "FEAT-000" in ids(service.list_tickets(store, include_archive=True))

    def test_status_filter_matches_effective_status(self, store):
        # TASK-001's stored status is 'open'; FEAT-002 blocks it.
        assert ids(service.list_tickets(store, status="blocked")) == ["TASK-001"]
        assert ids(service.list_tickets(store, status="open")) == []

    def test_type_filter(self, store):
        assert ids(service.list_tickets(store, type="feature")) == ["FEAT-001", "FEAT-002"]

    def test_parent_filter_is_case_insensitive(self, store):
        assert ids(service.list_tickets(store, parent="feat-002")) == ["RESEARCH-001", "TASK-001"]

    def test_filters_combine(self, store):
        assert ids(service.list_tickets(store, parent="EPIC-001", type="feature")) == [
            "FEAT-001", "FEAT-002",
        ]

    def test_children_resolve_against_the_whole_board(self, store):
        """A filtered listing still reports every child, not just matching ones."""
        epic = service.list_tickets(store, type="epic")[0]
        assert sorted(epic["children"]) == ["FEAT-001", "FEAT-002"]

    def test_listing_carries_no_body(self, store):
        assert all("body" not in t for t in service.list_tickets(store))

    def test_fields_trims_entries(self, store):
        listed = service.list_tickets(store, fields=["id", "title"])
        assert all(set(t) == {"id", "title"} for t in listed)
        assert listed[0]["id"] == "EPIC-001"

    def test_unknown_field_is_invalid(self, store):
        with pytest.raises(service.Invalid) as e:
            service.list_tickets(store, fields=["id", "nope"])
        assert "nope" in str(e.value)

    def test_unknown_field_is_rejected_before_the_board_is_loaded(self, store):
        """On the vault a board load is hundreds of requests -- a typo in
        `fields=` must not pay for one."""
        with patch.object(service, "load_board", side_effect=AssertionError("board loaded")):
            with pytest.raises(service.Invalid):
                service.list_tickets(store, fields=["nope"])

    def test_body_is_not_a_listing_field(self, store):
        """Listings never carry bodies, so asking for one is a mistake worth
        naming rather than silently dropping."""
        with pytest.raises(service.Invalid):
            service.list_tickets(store, fields=["body"])

    def test_empty_board(self, tmp_path):
        (tmp_path / "tickets").mkdir()
        assert service.list_tickets(LocalDirStore(tmp_path)) == []


class TestBothStoresAgree:
    def test_same_board_same_listing(self, docs_root):
        """The only field allowed to differ between stores is `path` -- one is a
        filesystem location, the other a vault stem."""
        local = service.list_tickets(LocalDirStore(docs_root), include_archive=True)
        vault = service.list_tickets(load_fake_store(docs_root), include_archive=True)

        for a, b in zip(local, vault, strict=True):
            assert {k: v for k, v in a.items() if k != "path"} == \
                   {k: v for k, v in b.items() if k != "path"}

    def test_same_board_same_ticket(self, docs_root):
        local = service.get_ticket(LocalDirStore(docs_root), "TASK-001")
        vault = service.get_ticket(load_fake_store(docs_root), "TASK-001")
        assert local.pop("path") != vault.pop("path")
        assert local == vault


# ---------------------------------------------------------------------------
# get_ticket / read_ticket
# ---------------------------------------------------------------------------

class TestGetTicket:
    def test_includes_body_by_default(self, store):
        ticket = service.get_ticket(store, "FEAT-001")
        assert ticket["id"] == "FEAT-001"
        assert "## Problem" in ticket["body"]
        assert ticket["body_html"] is None

    def test_body_false_omits_it(self, store):
        ticket = service.get_ticket(store, "FEAT-001", body=False)
        assert "body" not in ticket
        assert "body_html" not in ticket

    def test_id_is_case_insensitive(self, store):
        assert service.get_ticket(store, "feat-001")["id"] == "FEAT-001"

    def test_derived_fields(self, store):
        ticket = service.get_ticket(store, "TASK-001")
        assert ticket["effective_status"] == "blocked"
        assert ticket["is_blocked"] is True
        assert ticket["blockers"] == [
            {"id": "FEAT-001", "resolved": True},
            {"id": "FEAT-002", "resolved": False},
        ]

    def test_children_without_an_index(self, store):
        """`show` passes no children index and pays one board load."""
        assert sorted(service.get_ticket(store, "EPIC-001")["children"]) == [
            "FEAT-001", "FEAT-002",
        ]

    def test_archived_ticket_is_marked(self, store):
        assert service.get_ticket(store, "FEAT-000")["archived"] is True

    def test_unknown_id_raises_not_found(self, store):
        with pytest.raises(service.NotFound) as e:
            service.get_ticket(store, "NOPE-999")
        # The CLI prints this message verbatim after `Error: `.
        assert str(e.value) == "Ticket 'NOPE-999' not found."

    def test_read_ticket_returns_the_raw_triple(self, store):
        ref, fm, body = service.read_ticket(store, "TASK-001")
        assert fm["id"] == "TASK-001"
        assert fm["status"] == "open"          # stored, not derived
        assert body.lstrip().startswith("## Description")
        assert ref.name.startswith("TASK-001")


# ---------------------------------------------------------------------------
# Derivation -- the ticket dict must keep agreeing with the parser's own answers
# ---------------------------------------------------------------------------

class TestDerivation:
    def test_effective_status_matches_the_parser(self, store):
        """`_derive` resolves blockers once and derives the status from the
        details, instead of asking `effective_status` to resolve them again.
        The answers must stay identical."""
        for ref, fm in parser.load_all_tickets(store, include_archive=True):
            derived, _, _ = service._derive(store, fm)
            assert derived == parser.effective_status(store, fm), fm["id"]

    def test_blocker_resolution_is_read_once_per_blocker(self, docs_root):
        """TASK-001 has two blockers. Building its dict must resolve each once
        -- on the vault store every extra read is an HTTP round trip, and the
        old shape resolved them twice (once for `blocked`, once for details)."""
        store = load_fake_store(docs_root)
        ref, fm, _ = service.read_ticket(store, "TASK-001")

        reads: list[str] = []
        real_read = store.read
        store.read = lambda tid: (reads.append(tid), real_read(tid))[1]
        service.ticket_dict(store, ref, fm, children_by_parent={})

        assert sorted(reads) == ["FEAT-001", "FEAT-002"]

    def test_a_board_load_does_not_read_ticket_by_ticket(self, docs_root):
        """The board is loaded ONCE and everything else answers from it.

        A listing used to cost a round trip per ticket (the body, which no
        caller wanted) plus one per blocker. Against the vault that was the
        whole latency of the endpoint: ~63 requests for a 40-ticket board,
        ~25 s from inside a container where each one pays a fresh handshake.
        `load_frontmatter` answers the board in one call and `board_index`
        resolves blockers out of it, so neither count may grow with the board
        again without this test saying so.
        """
        store = load_fake_store(docs_root)
        loads, blocker_reads = [], []
        real_load, real_read = store.load_frontmatter, store.read
        store.load_frontmatter = lambda **kw: (loads.append(kw), real_load(**kw))[1]
        store.read = lambda tid: (blocker_reads.append(tid), real_read(tid))[1]

        tickets = service.list_tickets(store, include_archive=True)

        assert len(tickets) > 1  # the board is not trivially empty
        assert len(loads) == 1  # ONE board read, however many tickets there are
        assert blocker_reads == []  # blockers came from the map, not the store

    def test_blockers_resolve_from_the_board_map(self, store):
        """A hit answers from the map; a MISS still falls through to the store
        -- an archived blocker isn't in an active-only board, and must not
        read as 'not found' (which would wrongly block the ticket)."""
        ref, fm, _ = service.read_ticket(store, "TASK-001")
        fm = dict(fm, blockers=["FEAT-000"])  # FEAT-000 is archived + complete
        by_id = service.board_index(parser.load_all_tickets(store, include_archive=False))

        assert "FEAT-000" not in by_id
        detail = parser.get_blocker_details(store, fm, by_id)[0]
        assert (detail["status"], detail["resolved"]) == ("complete", True)

    def test_board_index_answers_without_the_store(self, store):
        """What the map is for: resolving a blocker with the store unplugged."""
        by_id = service.board_index(parser.load_all_tickets(store, include_archive=True))
        fm = {"blockers": ["FEAT-001"]}
        detail = parser.get_blocker_details(None, fm, by_id)[0]
        assert (detail["id"], detail["resolved"]) == ("FEAT-001", True)

    def test_terminal_status_survives_unresolved_blockers(self, store):
        """A complete ticket stays complete however its blockers look."""
        ref, fm, body = service.read_ticket(store, "FEAT-001")
        fm["blockers"] = ["FEAT-002"]  # in-progress, so unresolved
        assert service.ticket_dict(store, ref, fm)["effective_status"] == "complete"


# ---------------------------------------------------------------------------
# Field contract
# ---------------------------------------------------------------------------

class TestTicketFields:
    def test_ticket_fields_matches_ticket_dict(self, store):
        """TICKET_FIELDS is what `fields=` validates against -- it must stay
        exactly the keys a listing entry has."""
        listed = service.list_tickets(store)[0]
        assert set(listed) == set(service.TICKET_FIELDS)

    def test_body_keys_are_the_only_extras_on_a_full_ticket(self, store):
        full = service.get_ticket(store, "FEAT-001")
        assert set(full) - set(service.TICKET_FIELDS) == {"body", "body_html"}


# ---------------------------------------------------------------------------
# list_boards
# ---------------------------------------------------------------------------

class TestListBoards:
    def test_store_that_cannot_see_the_vault_answers_empty(self, docs_root):
        assert service.list_boards(LocalDirStore(docs_root)) == []

    def test_delegates_to_the_store(self, docs_root):
        store = load_fake_store(docs_root)
        store.list_boards = lambda: ["llpm", "marginalia"]
        assert service.list_boards(store) == ["llpm", "marginalia"]


# ---------------------------------------------------------------------------
# set_status (TASK-017)
#
# These mirror the cmd_status tests in test_commands.py / test_vault_commands.py
# one level down: the rules now live here, and those CLI tests are the
# unchanged regression net proving the printer still says the same things.
# ---------------------------------------------------------------------------

def status_of(store, ticket_id: str) -> str:
    """The *stored* status, read back through the store."""
    _, fm, _ = service.read_ticket(store, ticket_id)
    return fm["status"]


def frontmatter(store, ticket_id: str) -> dict:
    return service.read_ticket(store, ticket_id)[1]


class TestSetStatus:
    def test_reports_the_transition(self, store):
        assert service.set_status(store, "FEAT-002", "review", today="2026-03-20") == {
            "id": "FEAT-002",
            "previous_status": "in-progress",
            "status": "review",
            "commits_captured": 0,
        }

    def test_writes_through_the_store(self, store):
        service.set_status(store, "FEAT-002", "review", today="2026-03-20")
        assert status_of(store, "FEAT-002") == "review"

    def test_id_is_case_insensitive(self, store):
        service.set_status(store, "feat-002", "review", today="2026-03-20")
        assert status_of(store, "FEAT-002") == "review"

    def test_stamps_updated(self, store):
        service.set_status(store, "FEAT-002", "review", today="2026-03-20")
        assert frontmatter(store, "FEAT-002")["updated"] == "2026-03-20"

    def test_without_a_date_it_stamps_the_servers_day(self, store):
        """The CLI passes its own mockable clock; an HTTP write has none."""
        with patch.object(service, "_today", return_value="2026-05-05"):
            service.set_status(store, "FEAT-002", "review")
        assert frontmatter(store, "FEAT-002")["updated"] == "2026-05-05"

    def test_complete_stamps_completed(self, store):
        service.set_status(store, "FEAT-002", "complete", today="2026-03-20")
        assert frontmatter(store, "FEAT-002")["completed"] == "2026-03-20"

    def test_complete_keeps_an_existing_completed_date(self, store):
        service.set_status(store, "FEAT-002", "complete", today="2026-03-20")
        service.set_status(store, "FEAT-002", "open", today="2026-03-21")
        service.set_status(store, "FEAT-002", "complete", today="2026-03-22")
        assert frontmatter(store, "FEAT-002")["completed"] == "2026-03-20"

    def test_stamps_managed_by(self, store):
        """Fixture tickets predate managed_by; every mutation adds it."""
        assert "managed_by" not in frontmatter(store, "FEAT-002")
        service.set_status(store, "FEAT-002", "review", today="2026-03-20")
        assert frontmatter(store, "FEAT-002")["managed_by"] == "llpm"

    def test_body_is_left_alone(self, store):
        before = service.read_ticket(store, "FEAT-002")[2]
        service.set_status(store, "FEAT-002", "review", today="2026-03-20")
        assert service.read_ticket(store, "FEAT-002")[2] == before

    def test_unknown_id_raises_not_found(self, store):
        with pytest.raises(service.NotFound) as e:
            service.set_status(store, "NOPE-999", "open")
        assert str(e.value) == "Ticket 'NOPE-999' not found."

    def test_invalid_status_is_invalid(self, store):
        with pytest.raises(service.Invalid) as e:
            service.set_status(store, "FEAT-002", "shipped")
        assert "Invalid status: 'shipped'" in str(e.value)
        for value in sorted(parser.VALID_STATUSES):
            assert value in str(e.value)
        assert status_of(store, "FEAT-002") == "in-progress"

    def test_blocked_is_not_settable(self, store):
        """`blocked` is derived from unresolved blockers, never stored."""
        with pytest.raises(service.Invalid):
            service.set_status(store, "FEAT-002", "blocked")

    def test_a_rejected_call_never_touches_the_store(self, store):
        """Validation runs before the read, so a bad request costs no I/O."""
        store.read = lambda tid: pytest.fail(f"read {tid}")
        with pytest.raises(service.Invalid):
            service.set_status(store, "FEAT-002", "shipped")
        with pytest.raises(service.Invalid):
            service.set_status(store, "FEAT-002", "open", awaiting="deploy")


class TestSetStatusAwaiting:
    """FEAT-012: awaiting -- the review-queue discriminator."""

    def test_set_on_review(self, store):
        service.set_status(store, "FEAT-002", "review", awaiting="deploy", today="2026-03-20")
        assert frontmatter(store, "FEAT-002")["awaiting"] == "deploy"

    def test_absent_when_not_passed(self, store):
        service.set_status(store, "FEAT-002", "review", today="2026-03-20")
        assert "awaiting" not in frontmatter(store, "FEAT-002")

    def test_explicit_reviewer_allowed(self, store):
        service.set_status(store, "FEAT-002", "review", awaiting="reviewer", today="2026-03-20")
        assert frontmatter(store, "FEAT-002")["awaiting"] == "reviewer"

    def test_rejected_on_a_non_review_target(self, store):
        with pytest.raises(service.Invalid) as e:
            service.set_status(store, "FEAT-002", "open", awaiting="deploy")
        # Verbatim what the CLI has always printed after `Error: `.
        assert str(e.value) == (
            "--awaiting is only valid when the target status is 'review' (got 'open')."
        )

    def test_invalid_enum_rejected(self, store):
        with pytest.raises(service.Invalid) as e:
            service.set_status(store, "FEAT-002", "review", awaiting="bogus")
        assert "Invalid awaiting 'bogus'" in str(e.value)
        for value in ("reviewer", "push", "deploy", "human-verify", "human-answer"):
            assert value in str(e.value)

    def test_self_clears_on_the_next_transition(self, store):
        service.set_status(store, "FEAT-002", "review", awaiting="deploy", today="2026-03-20")
        service.set_status(store, "FEAT-002", "complete", today="2026-03-21")
        assert "awaiting" not in frontmatter(store, "FEAT-002")

    def test_reentering_review_without_awaiting_clears_it(self, store):
        service.set_status(store, "FEAT-002", "review", awaiting="deploy", today="2026-03-20")
        service.set_status(store, "FEAT-002", "in-progress", today="2026-03-21")
        service.set_status(store, "FEAT-002", "review", today="2026-03-22")
        assert "awaiting" not in frontmatter(store, "FEAT-002")


class TestSetStatusCommits:
    """FEAT-007: commits[] merge. Which SHAs arrive is the caller's business --
    the CLI harvests them from its CWD git repo, the API is handed them."""

    def test_records_the_given_shas(self, store):
        sha = "a" * 40
        result = service.set_status(
            store, "FEAT-002", "review", commits=[sha], today="2026-03-20"
        )
        assert result["commits_captured"] == 1
        assert frontmatter(store, "FEAT-002")["commits"] == [sha]

    def test_records_on_any_status_not_just_review(self, store):
        service.set_status(store, "TASK-001", "in-progress", commits=["abc1234"],
                           today="2026-03-20")
        assert frontmatter(store, "TASK-001")["commits"] == ["abc1234"]

    def test_prefix_dedup_across_calls(self, store):
        full = "b" * 40
        service.set_status(store, "FEAT-002", "review", commits=[full], today="2026-03-20")
        result = service.set_status(
            store, "FEAT-002", "complete", commits=[full[:8]], today="2026-03-21"
        )
        assert result["commits_captured"] == 0
        assert frontmatter(store, "FEAT-002")["commits"] == [full]

    def test_existing_entries_are_never_removed(self, store):
        service.set_status(store, "FEAT-002", "review", commits=["c" * 40], today="2026-03-20")
        service.set_status(store, "FEAT-002", "complete", commits=["d" * 40], today="2026-03-21")
        assert frontmatter(store, "FEAT-002")["commits"] == ["c" * 40, "d" * 40]

    def test_no_commits_no_key(self, store):
        """A ticket that never had commits: doesn't grow an empty one."""
        service.set_status(store, "FEAT-002", "review", commits=[], today="2026-03-20")
        assert "commits" not in frontmatter(store, "FEAT-002")


class TestSetStatusReturnedTicket:
    def test_include_ticket_matches_a_plain_read(self, store):
        """The POST response has to be exactly what a GET would say."""
        result = service.set_status(
            store, "FEAT-002", "review", awaiting="deploy",
            today="2026-03-20", include_ticket=True,
        )
        assert result["ticket"] == service.get_ticket(store, "FEAT-002")
        assert result["ticket"]["status"] == "review"
        assert result["ticket"]["awaiting"] == "deploy"

    def test_omitted_by_default(self, store):
        result = service.set_status(store, "FEAT-002", "review", today="2026-03-20")
        assert "ticket" not in result

    def test_serialization_is_opt_in_because_it_costs_reads(self, store):
        """Serializing re-derives blockers (a store read each) and loads the
        board for `children`. The CLI prints `ID: old -> new` and must not pay
        for that -- on the vault those are HTTP round trips."""
        reads: list[str] = []
        real_read = store.read
        store.read = lambda tid: (reads.append(tid), real_read(tid))[1]

        service.set_status(store, "TASK-001", "in-progress", today="2026-03-20")
        assert reads == ["TASK-001"]  # TASK-001 has two blockers; neither is read

        reads.clear()
        service.set_status(store, "TASK-001", "review", today="2026-03-21",
                           include_ticket=True)
        assert sorted(reads) == ["FEAT-001", "FEAT-002", "TASK-001"]


# ---------------------------------------------------------------------------
# create_ticket (TASK-018)
#
# The CLI regression net (TestCreate, TestProvenanceCreate, TestIntakePolicy in
# test_commands.py) is unchanged and still proves the printer; these pin the
# rules one level down, on both stores.
# ---------------------------------------------------------------------------

def created(store, result) -> dict:
    """Frontmatter of the ticket a create returned."""
    return frontmatter(store, result["id"])


class TestCreateTicket:
    def test_reports_what_it_filed(self, store):
        result = service.create_ticket(store, "task", "Wire the thing",
                                       origin="human", today="2026-03-20")
        assert result["id"] == "TASK-002"
        assert result["type"] == "task"
        assert result["title"] == "Wire the thing"
        assert result["status"] == "draft"
        assert "TASK-002" in result["path"]

    def test_the_ticket_is_readable_afterwards(self, store):
        service.create_ticket(store, "task", "Wire the thing", origin="human",
                              today="2026-03-20")
        fm = frontmatter(store, "TASK-002")
        assert fm["title"] == "Wire the thing"
        assert fm["created"] == fm["updated"] == "2026-03-20"

    def test_first_of_a_type_starts_at_001(self, store):
        assert service.create_ticket(store, "epic", "Second epic",
                                     origin="human")["id"] == "EPIC-002"

    def test_body_means_open_for_a_human(self, store):
        """A body is a spec someone wrote; a bare template body is a placeholder."""
        result = service.create_ticket(store, "task", "Specced", body="## Description\n\nGo.\n",
                                       origin="human")
        assert result["status"] == "open"
        assert created(store, result)["status"] == "open"

    def test_body_replaces_the_template_body(self, store):
        service.create_ticket(store, "task", "Specced", body="## Description\n\nGo.\n",
                              origin="human")
        _, _, body = service.read_ticket(store, "TASK-002")
        assert body.strip() == "## Description\n\nGo."

    def test_no_body_means_draft(self, store):
        assert service.create_ticket(store, "task", "Bare", origin="human")["status"] == "draft"

    def test_optional_fields_land_in_frontmatter(self, store):
        result = service.create_ticket(
            store, "task", "Loaded", origin="human", priority="high", effort="small",
            parent="FEAT-001", tags="auth,security", requires_human=True,
        )
        fm = created(store, result)
        assert fm["priority"] == "high"
        assert fm["effort"] == "small"
        assert fm["parent"] == "FEAT-001"
        assert fm["tags"] == ["auth", "security"]
        assert fm["requires_human"] is True

    def test_tags_take_a_list_as_well_as_a_string(self, store):
        """argparse hands a comma-joined string, JSON hands an array."""
        result = service.create_ticket(store, "task", "Listed", origin="human",
                                       tags=["auth", "security"])
        assert created(store, result)["tags"] == ["auth", "security"]

    def test_triage_adds_the_tag(self, store):
        result = service.create_ticket(store, "task", "Triaged", origin="human", triage=True)
        assert created(store, result)["tags"] == ["triage"]

    def test_triage_is_not_duplicated(self, store):
        result = service.create_ticket(store, "task", "Triaged", origin="human",
                                       tags="triage,auth", triage=True)
        assert created(store, result)["tags"] == ["triage", "auth"]

    def test_serves_on_a_feature(self, store):
        result = service.create_ticket(store, "feature", "Goal-serving", origin="human",
                                       serves="goals.unified-agent-platform")
        assert created(store, result)["serves"] == ["goals.unified-agent-platform"]

    def test_serves_on_a_task_is_invalid(self, store):
        with pytest.raises(service.Invalid) as e:
            service.create_ticket(store, "task", "Nope", origin="human", serves="goals.x")
        assert "only valid on epics/features" in str(e.value)

    def test_parent_must_exist(self, store):
        with pytest.raises(service.Invalid) as e:
            service.create_ticket(store, "task", "Orphan", origin="human", parent="NOPE-999")
        assert str(e.value) == "Parent ticket 'NOPE-999' not found."

    def test_a_rejected_create_writes_nothing(self, store):
        with pytest.raises(service.Invalid):
            service.create_ticket(store, "task", "Orphan", origin="human", parent="NOPE-999")
        assert not store.exists("TASK-002")

    def test_unknown_type_has_no_template(self, store):
        with pytest.raises(service.Invalid) as e:
            service.create_ticket(store, "banana", "Nope", origin="human")
        assert str(e.value) == "No template found for type 'banana'."

    def test_empty_title_is_invalid(self, store):
        with pytest.raises(service.Invalid):
            service.create_ticket(store, "task", "   ", origin="human")

    def test_invalid_enums_are_invalid(self, store):
        for kwargs in ({"origin": "robot"}, {"priority": "urgent"}, {"effort": "huge"}):
            with pytest.raises(service.Invalid):
                service.create_ticket(store, "task", "Bad", **{"origin": "human", **kwargs})

    def test_a_store_template_overrides_the_bundled_one(self, store):
        store.write_blob("templates/task.md",
                         "---\nid: \"__ID__\"\ntype: task\ntitle: \"__TITLE__\"\n"
                         "status: draft  # enum\npriority: medium  # enum\n"
                         "created: \"__DATE__\"\nupdated: \"__DATE__\"\n"
                         "completed: null\nblockers: []\ntags: []\n---\n\n## Local\n")
        service.create_ticket(store, "task", "Overridden", origin="human")
        assert "## Local" in service.read_ticket(store, "TASK-002")[2]

    def test_template_enum_comments_survive(self, docs_root):
        """Rendering is string substitution, not a YAML round trip, so the
        hints the templates carry reach the created ticket. Asserted on the
        local store, where the written bytes are readable."""
        store = LocalDirStore(docs_root)
        result = service.create_ticket(store, "task", "Commented", origin="human")
        text = Path(result["path"]).read_text(encoding="utf-8")
        assert "# draft | planned | open" in text
        assert "# low | medium | high" in text


class TestCreateProvenance:
    """FEAT-007: who filed this, resolved by the caller and stamped here."""

    def test_stamps_origin_created_by_commits_and_managed_by(self, store):
        result = service.create_ticket(store, "task", "Filed", origin="agent",
                                       created_by="claude/session-1")
        fm = created(store, result)
        assert fm["origin"] == "agent"
        assert fm["created_by"] == "claude/session-1"
        assert fm["commits"] == []
        assert fm["managed_by"] == "llpm"

    def test_human_origin_needs_no_created_by(self, store):
        fm = created(store, service.create_ticket(store, "task", "Filed", origin="human"))
        assert fm["origin"] == "human"
        assert "created_by" not in fm

    def test_the_default_origin_is_agent(self, store):
        """A caller that doesn't say is treated as an agent -- the conservative
        answer, since that is the one the intake policy drafts."""
        result = service.create_ticket(store, "task", "Unsigned", created_by="bot")
        assert created(store, result)["origin"] == "agent"
        assert result["status"] == "draft"

    def test_created_ticket_validates(self, store):
        result = service.create_ticket(store, "task", "Filed", origin="agent",
                                       created_by="claude/session-1")
        assert parser.validate_frontmatter(created(store, result)) == []


class TestCreateIntakePolicy:
    """FEAT-011: agent-proposed tickets land draft unless their kind is
    explicitly auto-approved by the board."""

    def test_agent_origin_lands_draft_even_with_a_body(self, store):
        result = service.create_ticket(store, "task", "Agent work", body="## Description\n\nGo.\n",
                                       origin="agent", created_by="claude/1")
        assert result["status"] == "draft"
        assert created(store, result)["status"] == "draft"

    def test_auto_approved_kind_keeps_open(self, store):
        result = service.create_ticket(store, "task", "Agent work", body="## Description\n\nGo.\n",
                                       origin="agent", created_by="claude/1",
                                       auto_approve=["task"])
        assert result["status"] == "open"
        assert created(store, result)["status"] == "open"

    def test_auto_approve_is_per_kind(self, store):
        result = service.create_ticket(store, "task", "Agent work", body="## Description\n\nGo.\n",
                                       origin="agent", created_by="claude/1",
                                       auto_approve=["research"])
        assert result["status"] == "draft"

    def test_human_origin_is_never_gated(self, store):
        result = service.create_ticket(store, "task", "Human work", body="## Description\n\nGo.\n",
                                       origin="human")
        assert result["status"] == "open"

    def test_goal_attachment_is_never_enforced(self, store):
        """Creation always succeeds unattached -- `llpm orphans` is the report."""
        result = service.create_ticket(store, "task", "Unattached", origin="agent",
                                       created_by="claude/1")
        assert created(store, result)["parent"] is None


class TestCreateCollisions:
    """Atomic create is what makes parallel agents safe on one board."""

    def test_retries_past_a_taken_id(self, store):
        real = store.create_exclusive
        calls = []

        def once(filename, content):
            calls.append(filename)
            if len(calls) == 1:
                raise FileExistsError(filename)
            return real(filename, content)

        store.create_exclusive = once
        result = service.create_ticket(store, "task", "Raced", origin="human")
        assert len(calls) == 2
        assert store.exists(result["id"])

    def test_exhausted_retries_are_a_conflict(self, store):
        store.create_exclusive = lambda filename, content: (_ for _ in ()).throw(
            FileExistsError(filename)
        )
        with pytest.raises(service.Conflict) as e:
            service.create_ticket(store, "task", "Doomed", origin="human")
        assert "ID collision" in str(e.value)


class TestCreateReturnedTicket:
    def test_include_ticket_matches_a_plain_read(self, store):
        result = service.create_ticket(store, "task", "Filed", origin="human",
                                       today="2026-03-20", include_ticket=True)
        assert result["ticket"] == service.get_ticket(store, result["id"])

    def test_omitted_by_default(self, store):
        assert "ticket" not in service.create_ticket(store, "task", "Filed", origin="human")

    def test_serialization_costs_no_extra_board_load(self, store):
        """A brand-new ticket has no children, so serializing it must not pay
        for the whole-board load `ticket_dict` otherwise does to find them --
        on the vault that load is one request per ticket. `next_id` lists the
        board either way, so the test counts listings rather than forbidding
        them: asking for the ticket must cost no more of them."""
        listings: list[bool] = []
        real = store.list_tickets
        store.list_tickets = lambda include_archive=True: (
            listings.append(include_archive), real(include_archive=include_archive))[1]

        service.create_ticket(store, "task", "Filed", origin="human")
        plain = len(listings)

        listings.clear()
        result = service.create_ticket(store, "task", "Filed again", origin="human",
                                       include_ticket=True)
        assert len(listings) == plain
        assert result["ticket"]["children"] == []


# ---------------------------------------------------------------------------
# set_fields (TASK-018)
# ---------------------------------------------------------------------------

class TestSetFields:
    def test_reports_each_change(self, store):
        result = service.set_fields(store, "TASK-001", {"priority": "high"},
                                    today="2026-03-20")
        assert result == {
            "id": "TASK-001",
            "changes": [{"field": "priority", "value": "high", "previous": "medium"}],
        }

    def test_writes_through_the_store(self, store):
        service.set_fields(store, "TASK-001", {"priority": "high"}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["priority"] == "high"

    def test_several_fields_at_once(self, store):
        service.set_fields(store, "TASK-001", {"priority": "low", "effort": "large"},
                           today="2026-03-20")
        fm = frontmatter(store, "TASK-001")
        assert (fm["priority"], fm["effort"]) == ("low", "large")

    def test_stamps_updated_and_managed_by(self, store):
        service.set_fields(store, "TASK-001", {"priority": "high"}, today="2026-03-20")
        fm = frontmatter(store, "TASK-001")
        assert fm["updated"] == "2026-03-20"
        assert fm["managed_by"] == "llpm"

    def test_body_is_left_alone(self, store):
        before = service.read_ticket(store, "TASK-001")[2]
        service.set_fields(store, "TASK-001", {"priority": "high"}, today="2026-03-20")
        assert service.read_ticket(store, "TASK-001")[2] == before

    def test_unknown_id_raises_not_found(self, store):
        with pytest.raises(service.NotFound):
            service.set_fields(store, "NOPE-999", {"priority": "high"})

    def test_nothing_to_set_is_invalid(self, store):
        with pytest.raises(service.Invalid):
            service.set_fields(store, "TASK-001", {})

    @pytest.mark.parametrize("field", ["status", "blockers", "serves", "waits_on",
                                       "after", "awaiting"])
    def test_fields_with_their_own_command_are_refused(self, store, field):
        with pytest.raises(service.Invalid) as e:
            service.set_fields(store, "TASK-001", {field: "whatever"})
        assert f"Cannot set '{field}' via 'set'." in str(e.value)

    @pytest.mark.parametrize("field", ["id", "type", "created", "updated", "completed",
                                       "origin", "created_by", "commits", "managed_by"])
    def test_system_written_fields_are_refused(self, store, field):
        with pytest.raises(service.Invalid) as e:
            service.set_fields(store, "TASK-001", {field: "whatever"})
        assert str(e.value) == f"Cannot set '{field}' -- managed automatically."

    def test_a_refusal_happens_before_the_read(self, store):
        store.read = lambda tid: pytest.fail(f"read {tid}")
        with pytest.raises(service.Invalid):
            service.set_fields(store, "TASK-001", {"status": "open"})

    def test_one_bad_value_changes_nothing(self, store):
        """Every field is validated before any is applied."""
        with pytest.raises(service.Invalid):
            service.set_fields(store, "TASK-001", {"priority": "high", "effort": "huge"})
        assert frontmatter(store, "TASK-001")["priority"] == "medium"

    def test_invalid_enums(self, store):
        for field, value in (("priority", "urgent"), ("effort", "huge"),
                             ("model_tier", "titanic")):
            with pytest.raises(service.Invalid) as e:
                service.set_fields(store, "TASK-001", {field: value})
            assert f"Invalid {field} '{value}'" in str(e.value)

    def test_parent_must_exist(self, store):
        with pytest.raises(service.Invalid) as e:
            service.set_fields(store, "TASK-001", {"parent": "NOPE-999"})
        assert str(e.value) == "Parent ticket 'NOPE-999' not found."

    def test_parent_that_exists(self, store):
        service.set_fields(store, "TASK-001", {"parent": "EPIC-001"}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["parent"] == "EPIC-001"


class TestSetFieldsCoercion:
    """The CLI hands strings, JSON hands typed values; both spellings mean the
    same thing, so neither caller pre-converts."""

    def test_numbers_are_coerced_from_strings(self, store):
        service.set_fields(store, "TASK-001", {"hours": "9"}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["hours"] == 9

    def test_floats_too(self, store):
        service.set_fields(store, "TASK-001", {"hours": "2.5"}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["hours"] == 2.5

    def test_json_numbers_pass_through(self, store):
        service.set_fields(store, "TASK-001", {"hours": 9}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["hours"] == 9

    def test_text_fields_keep_leading_zeros(self, store):
        service.set_fields(store, "TASK-001", {"milestone": "007"}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["milestone"] == "007"

    def test_tags_split_a_string(self, store):
        service.set_fields(store, "TASK-001", {"tags": "auth,security"}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["tags"] == ["auth", "security"]

    def test_tags_take_a_list(self, store):
        service.set_fields(store, "TASK-001", {"tags": ["auth"]}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["tags"] == ["auth"]

    def test_requires_human_from_a_string(self, store):
        service.set_fields(store, "TASK-001", {"requires_human": "true"}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["requires_human"] is True

    def test_requires_human_from_a_bool(self, store):
        service.set_fields(store, "TASK-001", {"requires_human": True}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["requires_human"] is True

    @pytest.mark.parametrize("null", ["null", "none", None])
    def test_null_spellings_clear_a_field(self, store, null):
        service.set_fields(store, "TASK-001", {"effort": null}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["effort"] is None

    def test_null_tags_become_an_empty_list(self, store):
        service.set_fields(store, "TASK-001", {"tags": None}, today="2026-03-20")
        assert frontmatter(store, "TASK-001")["tags"] == []


class TestSetFieldsReturnedTicket:
    def test_include_ticket_matches_a_plain_read(self, store):
        result = service.set_fields(store, "TASK-001", {"priority": "high"},
                                    today="2026-03-20", include_ticket=True)
        assert result["ticket"] == service.get_ticket(store, "TASK-001")
        assert result["ticket"]["priority"] == "high"

    def test_omitted_by_default(self, store):
        assert "ticket" not in service.set_fields(store, "TASK-001", {"priority": "high"},
                                                  today="2026-03-20")


# ---------------------------------------------------------------------------
# Edges (TASK-019)
#
# Four pairs over one shape. Each pair gets its own rules tested; the shared
# shape (idempotent add, NotFound on a missing rm, `updated`/`managed_by`
# stamped, the returned ticket) is asserted once per pair where it differs and
# once across all four in TestEveryEdgePair.
# ---------------------------------------------------------------------------

class TestBlockerEdges:
    def test_add(self, store):
        result = service.blocker_add(store, "RESEARCH-001", "FEAT-001", today="2026-03-20")
        assert result["id"] == "RESEARCH-001"
        assert result["changed"] is True
        assert frontmatter(store, "RESEARCH-001")["blockers"] == ["FEAT-001"]

    def test_add_upper_cases_the_id(self, store):
        service.blocker_add(store, "RESEARCH-001", "feat-001", today="2026-03-20")
        assert frontmatter(store, "RESEARCH-001")["blockers"] == ["FEAT-001"]

    def test_duplicate_add_is_a_no_op(self, store):
        service.blocker_add(store, "RESEARCH-001", "FEAT-001", today="2026-03-20")
        before = frontmatter(store, "RESEARCH-001")["updated"]
        result = service.blocker_add(store, "RESEARCH-001", "feat-001", today="2026-03-21")
        assert result["changed"] is False
        assert frontmatter(store, "RESEARCH-001")["blockers"] == ["FEAT-001"]
        assert frontmatter(store, "RESEARCH-001")["updated"] == before  # no write

    def test_unknown_blocker_is_refused(self, store):
        """`blockers` holds real ticket IDs only -- `blocked` is derived by
        resolving each one, so free text would block forever."""
        with pytest.raises(service.Invalid) as e:
            service.blocker_add(store, "RESEARCH-001", "NOPE-999")
        assert str(e.value) == "Ticket 'NOPE-999' not found."
        assert frontmatter(store, "RESEARCH-001")["blockers"] == []

    def test_self_blocker_is_refused(self, store):
        """A ticket blocking itself can never resolve."""
        with pytest.raises(service.Invalid) as e:
            service.blocker_add(store, "RESEARCH-001", "research-001")
        assert str(e.value) == "Ticket RESEARCH-001 cannot block itself."

    def test_unknown_ticket_is_not_found(self, store):
        with pytest.raises(service.NotFound):
            service.blocker_add(store, "NOPE-999", "FEAT-001")

    def test_rm(self, store):
        result = service.blocker_rm(store, "TASK-001", "feat-001", today="2026-03-20")
        assert result["changed"] is True
        assert frontmatter(store, "TASK-001")["blockers"] == ["FEAT-002"]

    def test_rm_of_a_missing_edge_is_not_found(self, store):
        with pytest.raises(service.NotFound) as e:
            service.blocker_rm(store, "TASK-001", "EPIC-001")
        assert str(e.value) == "'EPIC-001' is not a blocker on TASK-001."

    def test_the_added_blocker_shows_up_as_derived_blocked(self, store):
        """The point of answering with the ticket: the caller sees the effect
        without a second read. (FEAT-002 is in-progress and TASK-001 open, so
        the new edge really does flip it -- a terminal ticket never derives
        blocked, whatever its blockers say.)"""
        result = service.blocker_add(store, "FEAT-002", "TASK-001",
                                     today="2026-03-20", include_ticket=True)
        assert result["ticket"]["is_blocked"] is True
        assert result["ticket"]["effective_status"] == "blocked"
        assert result["ticket"]["blockers"] == [{"id": "TASK-001", "resolved": False}]

    def test_removing_the_last_unresolved_blocker_unblocks(self, store):
        result = service.blocker_rm(store, "TASK-001", "FEAT-002",
                                    today="2026-03-20", include_ticket=True)
        assert result["ticket"]["is_blocked"] is False
        assert result["ticket"]["effective_status"] == "open"


class TestAfterEdges:
    def test_add(self, store):
        result = service.after_add(store, "RESEARCH-001", "FEAT-001", today="2026-03-20")
        assert result["changed"] is True
        assert result["cycle_warning"] is False
        assert frontmatter(store, "RESEARCH-001")["after"] == ["FEAT-001"]

    def test_soft_edge_never_blocks(self, store):
        """`after` is advice: TASK-001 is open (unresolved as a blocker would
        be), and ordering behind it must not make FEAT-002 blocked."""
        result = service.after_add(store, "FEAT-002", "TASK-001",
                                   today="2026-03-20", include_ticket=True)
        assert result["ticket"]["is_blocked"] is False
        assert result["ticket"]["after"] == ["TASK-001"]

    def test_duplicate_add_is_a_no_op(self, store):
        service.after_add(store, "RESEARCH-001", "FEAT-001", today="2026-03-20")
        result = service.after_add(store, "RESEARCH-001", "feat-001", today="2026-03-21")
        assert result["changed"] is False
        assert frontmatter(store, "RESEARCH-001")["after"] == ["FEAT-001"]

    def test_unknown_target_is_refused(self, store):
        with pytest.raises(service.Invalid) as e:
            service.after_add(store, "RESEARCH-001", "NOPE-999")
        assert str(e.value) == "Ticket 'NOPE-999' not found."

    def test_a_cycle_warns_and_still_adds(self, store):
        """Cycles on a soft edge can't deadlock anything, so refusing one would
        just make ordering depend on which ticket you touched first."""
        service.after_add(store, "RESEARCH-001", "FEAT-001", today="2026-03-20")
        result = service.after_add(store, "FEAT-001", "RESEARCH-001", today="2026-03-20")
        assert result["cycle_warning"] is True
        assert result["changed"] is True
        assert frontmatter(store, "FEAT-001")["after"] == ["RESEARCH-001"]

    def test_rm(self, store):
        service.after_add(store, "RESEARCH-001", "FEAT-001", today="2026-03-20")
        service.after_rm(store, "RESEARCH-001", "feat-001", today="2026-03-21")
        assert frontmatter(store, "RESEARCH-001")["after"] == []

    def test_rm_of_a_missing_edge_is_not_found(self, store):
        with pytest.raises(service.NotFound) as e:
            service.after_rm(store, "RESEARCH-001", "FEAT-001")
        assert str(e.value) == "RESEARCH-001 is not ordered after 'FEAT-001'."


class TestWaitsEdges:
    STEM = "repos.marginalia.llpm.features.FEAT-010"

    def test_add(self, store):
        result = service.waits_add(store, "RESEARCH-001", self.STEM, today="2026-03-20")
        assert result["changed"] is True
        assert frontmatter(store, "RESEARCH-001")["waits_on"] == [self.STEM]

    def test_a_ticket_id_is_refused(self, store):
        """Bare IDs are intra-board dependencies -- that's what blockers are."""
        with pytest.raises(service.Invalid) as e:
            service.waits_add(store, "RESEARCH-001", "FEAT-001")
        assert "holds full vault stems" in str(e.value)
        assert frontmatter(store, "RESEARCH-001").get("waits_on", []) == []

    def test_an_unresolvable_stem_still_lands(self, store):
        """The edge exists precisely for work that isn't filed yet."""
        result = service.waits_add(store, "RESEARCH-001", self.STEM, today="2026-03-20")
        assert result["state"] in ("missing", "unavailable")
        assert frontmatter(store, "RESEARCH-001")["waits_on"] == [self.STEM]

    def test_a_resolvable_stem_reports_its_status(self, docs_root):
        store = load_fake_store(docs_root)
        store.foreign[self.STEM] = {"id": "FEAT-010", "status": "in-progress"}
        result = service.waits_add(store, "RESEARCH-001", self.STEM, today="2026-03-20")
        assert (result["state"], result["target_status"]) == ("ok", "in-progress")

    def test_an_unresolved_wait_blocks(self, docs_root):
        store = load_fake_store(docs_root)
        store.foreign[self.STEM] = {"id": "FEAT-010", "status": "open"}
        result = service.waits_add(store, "FEAT-002", self.STEM,
                                   today="2026-03-20", include_ticket=True)
        assert result["ticket"]["is_blocked"] is True

    def test_duplicate_add_is_a_no_op(self, store):
        service.waits_add(store, "RESEARCH-001", self.STEM, today="2026-03-20")
        result = service.waits_add(store, "RESEARCH-001", self.STEM, today="2026-03-21")
        assert result["changed"] is False
        assert frontmatter(store, "RESEARCH-001")["waits_on"] == [self.STEM]

    def test_rm(self, store):
        service.waits_add(store, "RESEARCH-001", self.STEM, today="2026-03-20")
        service.waits_rm(store, "RESEARCH-001", self.STEM, today="2026-03-21")
        assert frontmatter(store, "RESEARCH-001")["waits_on"] == []

    def test_rm_of_a_missing_edge_is_not_found(self, store):
        with pytest.raises(service.NotFound) as e:
            service.waits_rm(store, "RESEARCH-001", self.STEM)
        assert str(e.value) == f"RESEARCH-001 does not wait on '{self.STEM}'."


class TestServesEdges:
    GOAL = "goals.unified-agent-platform"

    def test_add_to_a_feature(self, store):
        result = service.serves_add(store, "FEAT-002", self.GOAL, today="2026-03-20")
        assert result["changed"] is True
        assert frontmatter(store, "FEAT-002")["serves"] == [self.GOAL]

    def test_add_to_an_epic(self, store):
        service.serves_add(store, "EPIC-001", self.GOAL, today="2026-03-20")
        assert frontmatter(store, "EPIC-001")["serves"] == [self.GOAL]

    def test_a_task_is_refused(self, store):
        """Tasks serve goals via their parent, not directly."""
        with pytest.raises(service.Invalid) as e:
            service.serves_add(store, "TASK-001", self.GOAL)
        assert str(e.value) == (
            "'serves' is only valid on epics/features (TASK-001 is a task). "
            "Tasks serve goals via their parent."
        )

    def test_a_ticket_id_is_refused(self, store):
        with pytest.raises(service.Invalid) as e:
            service.serves_add(store, "FEAT-002", "FEAT-001")
        assert "holds full vault stems to goal notes" in str(e.value)

    def test_the_stem_is_soft_validated(self, store):
        """A goal note can live in another repo's tree, so only the shape is
        checked -- llpm can't always resolve the address."""
        service.serves_add(store, "FEAT-002", "goals.nothing.here.yet", today="2026-03-20")
        assert frontmatter(store, "FEAT-002")["serves"] == ["goals.nothing.here.yet"]

    def test_duplicate_add_is_a_no_op(self, store):
        service.serves_add(store, "FEAT-002", self.GOAL, today="2026-03-20")
        result = service.serves_add(store, "FEAT-002", self.GOAL, today="2026-03-21")
        assert result["changed"] is False
        assert frontmatter(store, "FEAT-002")["serves"] == [self.GOAL]

    def test_rm(self, store):
        service.serves_add(store, "FEAT-002", self.GOAL, today="2026-03-20")
        service.serves_rm(store, "FEAT-002", self.GOAL, today="2026-03-21")
        assert frontmatter(store, "FEAT-002")["serves"] == []

    def test_rm_of_a_missing_edge_is_not_found(self, store):
        with pytest.raises(service.NotFound) as e:
            service.serves_rm(store, "FEAT-002", self.GOAL)
        assert str(e.value) == f"FEAT-002 does not serve '{self.GOAL}'."


# (ticket, add, rm, target) -- one row per edge pair, for the rules all four share.
EDGE_PAIRS = [
    ("RESEARCH-001", service.blocker_add, service.blocker_rm, "FEAT-001"),
    ("RESEARCH-001", service.after_add, service.after_rm, "FEAT-001"),
    ("RESEARCH-001", service.waits_add, service.waits_rm,
     "repos.marginalia.llpm.features.FEAT-010"),
    ("FEAT-002", service.serves_add, service.serves_rm, "goals.unified-agent-platform"),
]


@pytest.mark.parametrize("ticket_id,add,rm,target",
                         EDGE_PAIRS, ids=lambda v: getattr(v, "__name__", None))
class TestEveryEdgePair:
    def test_add_stamps_updated_and_managed_by(self, store, ticket_id, add, rm, target):
        add(store, ticket_id, target, today="2026-03-20")
        fm = frontmatter(store, ticket_id)
        assert fm["updated"] == "2026-03-20"
        assert fm["managed_by"] == "llpm"

    def test_rm_stamps_updated(self, store, ticket_id, add, rm, target):
        add(store, ticket_id, target, today="2026-03-20")
        rm(store, ticket_id, target, today="2026-03-21")
        assert frontmatter(store, ticket_id)["updated"] == "2026-03-21"

    def test_without_a_date_the_servers_day_is_stamped(self, store, ticket_id, add, rm, target):
        with patch.object(service, "_today", return_value="2026-05-05"):
            add(store, ticket_id, target)
        assert frontmatter(store, ticket_id)["updated"] == "2026-05-05"

    def test_the_body_is_left_alone(self, store, ticket_id, add, rm, target):
        before = service.read_ticket(store, ticket_id)[2]
        add(store, ticket_id, target, today="2026-03-20")
        assert service.read_ticket(store, ticket_id)[2] == before

    def test_unknown_ticket_is_not_found(self, store, ticket_id, add, rm, target):
        for fn in (add, rm):
            with pytest.raises(service.NotFound) as e:
                fn(store, "NOPE-999", target)
            assert str(e.value) == "Ticket 'NOPE-999' not found."

    def test_include_ticket_matches_a_plain_read(self, store, ticket_id, add, rm, target):
        result = add(store, ticket_id, target, today="2026-03-20", include_ticket=True)
        assert result["ticket"] == service.get_ticket(store, ticket_id)

    def test_ticket_is_omitted_by_default(self, store, ticket_id, add, rm, target):
        assert "ticket" not in add(store, ticket_id, target, today="2026-03-20")

    def test_add_then_rm_round_trips(self, store, ticket_id, add, rm, target):
        before = frontmatter(store, ticket_id)
        add(store, ticket_id, target, today="2026-03-20")
        rm(store, ticket_id, target, today="2026-03-21")
        after = frontmatter(store, ticket_id)
        for field in ("blockers", "after", "waits_on", "serves"):
            assert (after.get(field) or []) == (before.get(field) or [])
