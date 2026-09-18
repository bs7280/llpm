"""Tests for the service layer (TASK-016 reads, TASK-017 ``set_status``).

Every service function runs against BOTH stores -- the local directory and the
fake vault -- holding the same board, because the whole point of the seam is
that a board answers the same way whichever store it is read through.

The CLI regression net lives in test_commands.py / test_json.py: those tests are
unchanged, and `cmd_list` / `cmd_show` / `cmd_status` now reach ticket data
through here.
"""

from __future__ import annotations

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
        for ref, fm, _ in parser.load_all_tickets(store, include_archive=True):
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
