"""Tests for VaultRef + MdTreeStore (HTTP-backed TicketStore).

HTTP calls are mocked with unittest.mock (at ``MdTreeStore._send``, the one
exchange every request goes through) so no network is required in CI. The
keep-alive tests are the exception: they run a real HTTP/1.1 server on
localhost, because connection reuse is socket behaviour a mock can't show.
"""

from __future__ import annotations

import fnmatch
import http.server
import json
import threading
import time
import urllib.error
import urllib.parse
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llpm.parser import parse_text
from llpm.store import MdTreeStore, MdTreeStoreError, TicketStore, VaultRef


# ---------------------------------------------------------------------------
# VaultRef
# ---------------------------------------------------------------------------

class TestVaultRef:
    def test_name_is_last_segment(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        assert ref.name == "TASK-001"
        assert ref.stem == "TASK-001"

    def test_parts_active(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001", is_archived=False)
        assert "archive" not in ref.parts
        assert ref.name in ref.parts

    def test_parts_archived(self):
        ref = VaultRef("repos.foo.llpm.archive.TASK-001", is_archived=True)
        assert "archive" in ref.parts

    def test_str(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        assert str(ref) == "repos.foo.llpm.tasks.TASK-001"

    def test_frozen(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        with pytest.raises(Exception):
            ref.vault_stem = "other"

    def test_is_ticketstore_ref(self):
        # VaultRef satisfies the .name / .stem / .parts contract
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        assert hasattr(ref, "name")
        assert hasattr(ref, "stem")
        assert hasattr(ref, "parts")


# ---------------------------------------------------------------------------
# Helpers for mocking _send
# ---------------------------------------------------------------------------

def _response(body: str | bytes | dict, status: int = 200):
    """Build a mock response: what `_send` returns (a context manager with read())."""
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=body)
    mock.status = status
    return mock


def _http_error(code: int):
    import urllib.error
    return urllib.error.HTTPError(url="", code=code, msg="", hdrs=None, fp=None)


def _ssl_error():
    """What the TLS handshake raises when the server cert isn't trusted."""
    import ssl
    return ssl.SSLCertVerificationError("unable to get local issuer certificate")


TICKET_CONTENT = """\
---
id: TASK-001
type: task
title: Test ticket
status: open
priority: medium
effort: small
parent: null
blockers: []
created: '2026-07-04'
updated: '2026-07-04'
completed: null
tags: []
---

## Description

A test ticket body.
"""

# ---------------------------------------------------------------------------
# MdTreeStore
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    return MdTreeStore("https://agent-memory.home.lab", "myrepo")


class TestMdTreeStoreProtocol:
    def test_is_ticketstore(self, store):
        assert isinstance(store, TicketStore)


class TestMdTreeStoreRead:
    def test_read_found(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            result = store.read("TASK-001")

        assert result is not None
        ref, fm, body = result
        assert isinstance(ref, VaultRef)
        assert fm["id"] == "TASK-001"
        assert "A test ticket body" in body
        assert not ref.is_archived

    def test_read_case_insensitive(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            result = store.read("task-001")

        assert result is not None

    def test_read_not_found_returns_none(self, store):
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)):
            result = store.read("NOPE-999")

        assert result is None

    def test_read_archived(self, store):
        archived_content = TICKET_CONTENT.replace("status: open", "status: closed")

        def side_effect(req_or_url, *args, **kwargs):
            # All active type stems return 404; archive returns the note
            url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
            if "archive" in url:
                return _response(archived_content)
            raise _http_error(404)

        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            result = store.read("TASK-001")

        assert result is not None
        ref, fm, _ = result
        assert ref.is_archived
        assert "archive" in ref.parts

    def test_read_ref(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            fm, body = store.read_ref(ref)

        assert fm["id"] == "TASK-001"

    def test_read_ref_not_found_raises(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.NOPE-999")
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)):
            with pytest.raises(FileNotFoundError):
                store.read_ref(ref)

    def test_exists_true(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            assert store.exists("TASK-001") is True

    def test_exists_false(self, store):
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)):
            assert store.exists("NOPE-999") is False


class TestMdTreeStoreWrite:
    def test_write(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"stem": ref.vault_stem, "created": False, "etag": "abc"})
            store.write(ref, {"id": "TASK-001", "type": "task", "title": "t"}, "body\n")
        mock_open.assert_called_once()

    def test_create_exclusive_success(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"stem": "repos.myrepo.llpm.tasks.TASK-099", "created": True, "etag": "abc"})
            ref = store.create_exclusive("TASK-099_MY_TASK.md", TICKET_CONTENT.replace("TASK-001", "TASK-099"))

        assert isinstance(ref, VaultRef)
        assert ref.name == "TASK-099"
        assert not ref.is_archived

    def test_create_exclusive_conflict_raises(self, store):
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(409)):
            with pytest.raises(FileExistsError):
                store.create_exclusive("TASK-099_MY_TASK.md", TICKET_CONTENT.replace("TASK-001", "TASK-099"))


class TestMdTreeStoreList:
    def test_list_tickets_active(self, store):
        items_by_type = {
            "tasks": [{"stem": "repos.myrepo.llpm.tasks.TASK-001", "title": "t"}],
            "features": [{"stem": "repos.myrepo.llpm.features.FEAT-001", "title": "f"}],
            "epics": [],
            "research": [],
        }

        def side_effect(url, *args, **kwargs):
            url_str = url if isinstance(url, str) else url.full_url
            for sub, items in items_by_type.items():
                if sub in url_str:
                    return _response({"items": items, "total": len(items)})
            return _response({"items": [], "total": 0})

        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            refs = store.list_tickets(include_archive=False)

        assert len(refs) == 2
        names = {r.name for r in refs}
        assert "TASK-001" in names
        assert "FEAT-001" in names
        assert all(not r.is_archived for r in refs)

    def test_list_tickets_includes_archive(self, store):
        def side_effect(url, *args, **kwargs):
            url_str = url if isinstance(url, str) else url.full_url
            if "archive" in url_str:
                return _response({"items": [{"stem": "repos.myrepo.llpm.archive.TASK-000", "title": "old"}], "total": 1})
            return _response({"items": [], "total": 0})

        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            refs = store.list_tickets(include_archive=True)

        archived = [r for r in refs if r.is_archived]
        assert len(archived) == 1
        assert archived[0].name == "TASK-000"

    def test_list_tickets_exclude_archive(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"items": [], "total": 0})
            refs = store.list_tickets(include_archive=False)

        # Should not have called archive endpoint
        calls = [str(c) for c in mock_open.call_args_list]
        assert not any("archive" in c for c in calls)


# ---------------------------------------------------------------------------
# Notes below a ticket (FEAT-014) -- agent run notes are never tickets
# ---------------------------------------------------------------------------

def _url_of(req_or_url) -> str:
    return req_or_url if isinstance(req_or_url, str) else req_or_url.full_url


def _query(req_or_url) -> dict:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(_url_of(req_or_url)).query)
    return {k: v[0] for k, v in qs.items()}


def _serve_listing(stems_by_pattern: dict[str, list]):
    """urlopen side_effect: a paginating /notes endpoint over fixed stems,
    honoring limit/offset the way the real service does. An entry may be a
    bare stem, or ``(stem, frontmatter)`` -- the frontmatter is returned only
    when the caller asked for ``include=frontmatter``, as the vault does."""
    def side_effect(req_or_url, *a, **kw):
        q = _query(req_or_url)
        entries = stems_by_pattern.get(q["pattern"], [])
        limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
        page = []
        for entry in entries[offset:offset + limit]:
            stem, fm = entry if isinstance(entry, tuple) else (entry, None)
            item = {"stem": stem, "title": None}
            if fm is not None and q.get("include") == "frontmatter":
                item["frontmatter"] = fm
            page.append(item)
        return _response({"items": page, "total": len(entries), "limit": limit, "offset": offset})
    return side_effect


class TestMdTreeStoreSubnotes:
    def test_list_ignores_notes_below_a_ticket(self, store):
        # The service's fnmatch `*` crosses dots, so the bucket glob returns
        # the ticket's whole subtree -- whatever the segment is called.
        listing = _serve_listing({
            "repos.myrepo.llpm.tasks.*": [
                "repos.myrepo.llpm.tasks.TASK-001",
                "repos.myrepo.llpm.tasks.TASK-001.agent-workers.sonnet-a3f9",
                "repos.myrepo.llpm.tasks.TASK-001.agent-workers.sonnet-a3f9.test-report",
                "repos.myrepo.llpm.tasks.TASK-001.runs.task-001-3fa4b2c1",
                "repos.myrepo.llpm.tasks.TASK-002",
            ],
            "repos.myrepo.llpm.archive.*": [
                "repos.myrepo.llpm.archive.TASK-000",
                "repos.myrepo.llpm.archive.TASK-000.agent-workers.w1",
            ],
        })
        with patch.object(MdTreeStore, "_send", side_effect=listing):
            refs = store.list_tickets(include_archive=True)

        assert [r.vault_stem for r in refs] == [
            "repos.myrepo.llpm.archive.TASK-000",
            "repos.myrepo.llpm.tasks.TASK-001",
            "repos.myrepo.llpm.tasks.TASK-002",
        ]

    def test_dotted_repo_stem(self):
        # The one-segment rule is relative to the namespace, not an absolute
        # segment count.
        store = MdTreeStore("https://agent-memory.home.lab", "org.repo")
        assert store._is_ticket_stem("repos.org.repo.llpm.tasks.TASK-001")
        assert not store._is_ticket_stem("repos.org.repo.llpm.tasks.TASK-001.agent-workers.w1")
        assert not store._is_ticket_stem("repos.other.llpm.tasks.TASK-001")

    def test_sibling_bucket_is_not_a_ticket(self, store):
        # Depth alone doesn't decide: a non-ticket bucket beside the ticket
        # buckets sits at exactly a ticket's depth. `list_tickets` never saw
        # these (it globs each bucket by name); `load_frontmatter` globs the
        # whole namespace, so it does.
        assert not store._is_ticket_stem("repos.myrepo.llpm.milestones.M0")
        assert not store._is_ticket_stem("repos.myrepo.llpm.templates.task")
        assert store._is_ticket_stem("repos.myrepo.llpm.tasks.TASK-001")
        assert store._is_ticket_stem("repos.myrepo.llpm.archive.TASK-000")

    def test_load_frontmatter_skips_a_sibling_bucket(self, store):
        # The board load crashed with KeyError: 'id' -- a milestone note
        # carries `key:`, not `id:`, and was parsed as a ticket anyway.
        listing = _serve_listing({
            "repos.myrepo.llpm.*": [
                ("repos.myrepo.llpm.tasks.TASK-001", {"id": "TASK-001", "type": "task"}),
                ("repos.myrepo.llpm.milestones.M0", {"key": "M0", "title": "Week 1"}),
                ("repos.myrepo.llpm.templates.task", {"type": "task"}),
                ("repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1", {"worker": "w1"}),
            ],
        })
        with patch.object(MdTreeStore, "_send", side_effect=listing):
            pairs = store.load_frontmatter(include_archive=True)

        assert [ref.vault_stem for ref, _ in pairs] == ["repos.myrepo.llpm.tasks.TASK-001"]

    def test_listing_asks_for_the_max_page(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"items": [], "total": 0})
            store.list_tickets(include_archive=False)
        assert all(_query(c.args[0])["limit"] == "1000" for c in mock_open.call_args_list)

    def test_listing_follows_pagination(self, store):
        store._LIST_PAGE_SIZE = 2
        stems = [f"repos.myrepo.llpm.features.FEAT-{n:03d}" for n in range(1, 6)]
        listing = _serve_listing({"repos.myrepo.llpm.features.*": stems})
        with patch.object(MdTreeStore, "_send", side_effect=listing) as mock_open:
            refs = store.list_tickets(include_archive=False)

        assert [r.name for r in refs] == [f"FEAT-{n:03d}" for n in range(1, 6)]
        feature_calls = [c for c in mock_open.call_args_list if "features" in _url_of(c.args[0])]
        assert [_query(c.args[0])["offset"] for c in feature_calls] == ["0", "2", "4"]

    def test_next_id_sees_tickets_past_the_first_page(self, store):
        # The failure this guards: worker notes fill the page, the highest
        # real ticket falls off the end, and next_id mints an ID that exists.
        from llpm import parser

        store._LIST_PAGE_SIZE = 3
        stems = [f"repos.myrepo.llpm.features.FEAT-001.agent-workers.w{i}" for i in range(5)]
        stems += ["repos.myrepo.llpm.features.FEAT-001", "repos.myrepo.llpm.features.FEAT-076"]
        listing = _serve_listing({"repos.myrepo.llpm.features.*": sorted(stems)})
        with patch.object(MdTreeStore, "_send", side_effect=listing):
            assert parser.next_id(store, "feature") == "FEAT-077"

    def test_listing_dedupes_across_a_shifting_page_boundary(self, store):
        # A note created between two fetches repeats the boundary item.
        store._LIST_PAGE_SIZE = 2
        pages = {
            0: ["repos.myrepo.llpm.tasks.TASK-001", "repos.myrepo.llpm.tasks.TASK-002"],
            2: ["repos.myrepo.llpm.tasks.TASK-002", "repos.myrepo.llpm.tasks.TASK-003"],
        }

        def side_effect(req_or_url, *a, **kw):
            q = _query(req_or_url)
            if "tasks" not in q["pattern"]:
                return _response({"items": [], "total": 0})
            items = [{"stem": s} for s in pages[int(q["offset"])]]
            return _response({"items": items, "total": 4})

        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            refs = store.list_tickets(include_archive=False)
        assert [r.name for r in refs] == ["TASK-001", "TASK-002", "TASK-003"]

    def test_subnotes(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        listing = _serve_listing({
            "repos.myrepo.llpm.tasks.TASK-001.*": [
                "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1.screenshots",
                "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1",
            ],
        })
        with patch.object(MdTreeStore, "_send", side_effect=listing):
            assert store.subnotes(ref) == [
                "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1",
                "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1.screenshots",
            ]

    def _delete_recorder(self, subnotes, fail=None):
        """side_effect serving the subnote listing and recording DELETEs."""
        deleted = []
        listing = _serve_listing({"repos.myrepo.llpm.tasks.TASK-001.*": subnotes})

        def side_effect(req_or_url, *a, **kw):
            if isinstance(req_or_url, str) or req_or_url.get_method() != "DELETE":
                return listing(req_or_url)
            stem = urllib.parse.unquote(req_or_url.full_url.rsplit("/", 1)[-1])
            if fail and stem in fail:
                raise _http_error(fail[stem])
            deleted.append(stem)
            return _response({"stem": stem, "dangling": []})

        return side_effect, deleted

    def test_delete_removes_subtree_deepest_first_ticket_last(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        side_effect, deleted = self._delete_recorder([
            "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1",
            "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1.screenshots",
            "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1.screenshots.login",
        ])
        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            store.delete(ref)

        assert deleted == [
            "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1.screenshots.login",
            "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1.screenshots",
            "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1",
            "repos.myrepo.llpm.tasks.TASK-001",
        ]

    def test_delete_tolerates_subnote_already_gone(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        gone = "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1"
        side_effect, deleted = self._delete_recorder([gone], fail={gone: 404})
        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            store.delete(ref)
        assert deleted == ["repos.myrepo.llpm.tasks.TASK-001"]

    def test_delete_keeps_ticket_when_a_subnote_delete_fails(self, store):
        # An interrupted delete must never leave subnotes under a missing ticket.
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        stuck = "repos.myrepo.llpm.tasks.TASK-001.agent-workers.w1"
        side_effect, deleted = self._delete_recorder([stuck], fail={stuck: 500})
        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            with pytest.raises(urllib.error.HTTPError):
                store.delete(ref)
        assert deleted == []


class TestMdTreeStoreArchiveDelete:
    def test_archive(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"old_stem": ref.vault_stem, "new_stem": "repos.myrepo.llpm.archive.TASK-001", "moves": [], "relinked_files": 0})
            new_ref = store.archive(ref)

        assert isinstance(new_ref, VaultRef)
        assert new_ref.is_archived
        assert "archive" in new_ref.parts

    def test_delete(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"stem": ref.vault_stem, "dangling": []})
            store.delete(ref)
        # One listing for notes below the ticket (none here), then the DELETE.
        assert mock_open.call_count == 2
        last = mock_open.call_args_list[-1].args[0]
        assert last.get_method() == "DELETE"
        assert last.full_url.endswith("repos.myrepo.llpm.tasks.TASK-001")


class TestMdTreeStoreBlobs:
    def test_read_blob_todo(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response("- (1) do this\n")
            result = store.read_blob("TODO.md")
        assert result == "- (1) do this\n"

    def test_read_blob_template(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response("---\ntype: task\n---\n")
            result = store.read_blob("templates/task.md")
        assert result is not None

    def test_read_blob_not_found(self, store):
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)):
            result = store.read_blob("TODO.md")
        assert result is None

    def test_read_blob_unmappable_returns_none(self, store):
        assert store.read_blob("unknown/path.txt") is None

    def test_write_blob(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"stem": "repos.myrepo.llpm.todo", "created": True, "etag": "x"})
            store.write_blob("TODO.md", "- (1) thing\n")
        mock_open.assert_called_once()


# ---------------------------------------------------------------------------
# read_foreign — cross-board stem reads (FEAT-005)
# ---------------------------------------------------------------------------

FOREIGN_STEM = "repos.other.llpm.features.FEAT-010"


class TestMdTreeStoreReadForeign:
    def test_found(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            state, fm = store.read_foreign(FOREIGN_STEM)
        assert state == "ok"
        assert fm["id"] == "TASK-001"

    def test_archived_target_followed(self, store):
        def side_effect(req_or_url, *args, **kwargs):
            url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
            if "archive" in url:
                return _response(TICKET_CONTENT.replace("status: open", "status: complete"))
            raise _http_error(404)

        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            state, fm = store.read_foreign(FOREIGN_STEM)

        assert state == "ok"
        assert fm["status"] == "complete"

    def test_missing(self, store):
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)):
            state, fm = store.read_foreign(FOREIGN_STEM)
        assert state == "missing"
        assert fm is None

    def test_non_board_stem_no_archive_probe(self, store):
        # goals.* stems have no archive variant; a 404 is a definitive miss
        # after a single request.
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)) as mock_open:
            state, _ = store.read_foreign("goals.agent-memory-scoped-auth")
        assert state == "missing"
        assert mock_open.call_count == 1

    def test_unreachable_degrades_not_raises(self, store):
        err = ConnectionRefusedError("Connection refused")
        with patch.object(MdTreeStore, "_send", side_effect=err):
            state, fm = store.read_foreign(FOREIGN_STEM)
        assert state == "unavailable"
        assert fm is None

    def test_tls_failure_degrades_not_raises(self, store):
        with patch.object(MdTreeStore, "_send", side_effect=_ssl_error()):
            state, _ = store.read_foreign(FOREIGN_STEM)
        assert state == "unavailable"

    def test_unparseable_target_is_error(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response("no frontmatter here\n")
            state, fm = store.read_foreign(FOREIGN_STEM)
        assert state == "error"
        assert fm is None

    def test_result_cached(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            store.read_foreign(FOREIGN_STEM)
            store.read_foreign(FOREIGN_STEM)
        assert mock_open.call_count == 1

    def test_the_cache_does_not_outlive_the_read_scope(self, store):
        """TASK-021: the cache is per read scope, not per process. `llpm serve`
        keeps one store per board alive, so without this a waits_on target's
        status was frozen at whatever it was when first read."""
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            assert store.read_foreign(FOREIGN_STEM)[1]["status"] == "open"

            store.begin_read_scope()
            mock_open.return_value = _response(
                TICKET_CONTENT.replace("status: open", "status: complete")
            )
            assert store.read_foreign(FOREIGN_STEM)[1]["status"] == "complete"
        assert mock_open.call_count == 2

    def test_a_write_to_a_cached_stem_invalidates_it(self, store):
        """The same staleness *within* a scope: a store that just wrote the
        note it cached must not go on answering with the pre-write copy."""
        ref = VaultRef(FOREIGN_STEM)
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            store.read_foreign(FOREIGN_STEM)

            fm, body = store.read_ref(ref)
            fm["status"] = "complete"
            store.write(ref, fm, body)

            mock_open.return_value = _response(
                TICKET_CONTENT.replace("status: open", "status: complete")
            )
            assert store.read_foreign(FOREIGN_STEM)[1]["status"] == "complete"

    def test_archiving_a_cached_ticket_invalidates_it(self, store):
        """read_foreign follows a ticket to its archive stem, so a cached entry
        is dropped when either spelling is written."""
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            store.read_foreign(FOREIGN_STEM)
            store.archive(VaultRef(FOREIGN_STEM))
        assert store._foreign_cache == {}

    def test_an_unrelated_write_keeps_the_cache(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            store.read_foreign(FOREIGN_STEM)
            store.write(VaultRef("repos.myrepo.llpm.tasks.TASK-001"), {"id": "TASK-001"}, "")
            store.read_foreign(FOREIGN_STEM)
        assert list(store._foreign_cache) == [FOREIGN_STEM]

    def test_archive_variant(self):
        assert (
            MdTreeStore._archive_variant("repos.x.llpm.features.FEAT-010")
            == "repos.x.llpm.archive.FEAT-010"
        )
        assert MdTreeStore._archive_variant("repos.x.llpm.archive.FEAT-010") is None
        assert MdTreeStore._archive_variant("goals.some-goal") is None


# ---------------------------------------------------------------------------
# scan_by_type -- vault-wide type scan (FEAT-008)
# ---------------------------------------------------------------------------

class TestMdTreeStoreScanByType:
    def test_single_page_filters_by_type(self, store):
        def side_effect(url, *a, **kw):
            url_str = url if isinstance(url, str) else url.full_url
            assert "include=frontmatter" in url_str
            return _response({
                "items": [
                    {"stem": "goals.a", "title": None, "frontmatter": {"type": "goal", "status": "stamped"}},
                    {"stem": "scratch.x", "title": None, "frontmatter": {"type": "note"}},
                ],
                "total": 2,
            })

        with patch.object(MdTreeStore, "_send", side_effect=side_effect) as mock_open:
            results = store.scan_by_type("goal")

        assert results == [("goals.a", {"type": "goal", "status": "stamped"})]
        mock_open.assert_called_once()

    def test_paginates_across_pages(self, store):
        store._SCAN_PAGE_SIZE = 2
        pages = {
            0: {
                "items": [
                    {"stem": "goals.a", "frontmatter": {"type": "goal"}},
                    {"stem": "scratch.x", "frontmatter": {"type": "note"}},
                ],
                "total": 3,
            },
            2: {
                "items": [{"stem": "goals.b", "frontmatter": {"type": "goal"}}],
                "total": 3,
            },
        }

        def side_effect(url, *a, **kw):
            url_str = url if isinstance(url, str) else url.full_url
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url_str).query)
            return _response(pages[int(qs["offset"][0])])

        with patch.object(MdTreeStore, "_send", side_effect=side_effect) as mock_open:
            results = store.scan_by_type("goal")

        assert {stem for stem, _ in results} == {"goals.a", "goals.b"}
        assert mock_open.call_count == 2

    def test_page_500_falls_back_to_per_stem_fetches(self, store):
        # A bad note's frontmatter 500s the bulk include=frontmatter call for
        # its whole page; the scan must degrade to per-stem fetches for that
        # page rather than losing every note on it.
        def side_effect(req_or_url, *a, **kw):
            url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
            if "include=frontmatter" in url:
                raise _http_error(500)
            if url.rstrip("/").endswith("/frontmatter"):
                if "goals.a" in url:
                    return _response({"type": "goal", "status": "stamped"})
                raise _http_error(500)  # this note is unreadable -- skipped
            return _response({
                "items": [{"stem": "goals.a", "title": None}, {"stem": "scratch.bad", "title": None}],
                "total": 2,
            })

        with patch.object(MdTreeStore, "_send", side_effect=side_effect):
            results = store.scan_by_type("goal")

        assert results == [("goals.a", {"type": "goal", "status": "stamped"})]

    def test_non_500_http_error_propagates(self, store):
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)):
            with pytest.raises(urllib.error.HTTPError):
                store.scan_by_type("goal")

    def test_no_matches_returns_empty(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response({"items": [], "total": 0})
            assert store.scan_by_type("goal") == []


# ---------------------------------------------------------------------------
# TLS trust (TASK-003)
# ---------------------------------------------------------------------------

def _https_connection(body: str = TICKET_CONTENT):
    """patch() for ``http.client.HTTPSConnection`` whose every exchange answers
    200 with ``body`` -- for asserting how the connection itself is built."""
    conn_cls = MagicMock(name="HTTPSConnection")
    conn = conn_cls.return_value
    conn.sock = None
    conn.getresponse.return_value.status = 200
    conn.getresponse.return_value.read.return_value = body.encode()
    return patch("http.client.HTTPSConnection", conn_cls)


class TestMdTreeStoreTLS:
    """Through the real connection code: only the socket's connect is faked."""

    def test_ssl_verify_failure_raises_actionable_error(self, store):
        # A cert-verify failure surfaces as MdTreeStoreError, not a raw traceback.
        with patch("http.client.HTTPSConnection.connect", side_effect=_ssl_error()):
            with pytest.raises(MdTreeStoreError) as exc:
                store.read("TASK-001")
        msg = str(exc.value)
        assert "SSL_CERT_FILE" in msg
        assert "mkcert" in msg
        assert "certs.home.lab" in msg

    def test_no_ca_uses_default_context(self, store):
        # Without a configured CA the connection gets context=None, so
        # http.client builds the stdlib default (SSL_CERT_FILE-aware) one.
        with _https_connection() as conn_cls:
            store.read("TASK-001")
        conn_cls.assert_called_once_with("agent-memory.home.lab", context=None)

    def test_ca_config_builds_and_threads_context(self):
        # A configured CA path builds ONE SSL context, threaded into the one
        # connection every request reuses.
        sentinel = MagicMock(name="ssl_ctx")
        store = MdTreeStore("https://agent-memory.home.lab", "myrepo", ca="/tmp/rootCA.pem")
        with patch("ssl.create_default_context", return_value=sentinel) as mk_ctx, \
             _https_connection() as conn_cls:
            store.read("TASK-001")
            store.read("TASK-001")
        mk_ctx.assert_called_once_with(cafile="/tmp/rootCA.pem")
        conn_cls.assert_called_once_with("agent-memory.home.lab", context=sentinel)

    def test_bad_ca_path_raises_store_error(self):
        store = MdTreeStore(
            "https://agent-memory.home.lab", "myrepo", ca="/no/such/rootCA.pem"
        )
        with _https_connection() as conn_cls:
            with pytest.raises(MdTreeStoreError) as exc:
                store.read("TASK-001")
        assert "ca" in str(exc.value).lower()
        conn_cls.assert_not_called()  # refused before any connection exists

    def test_http_error_still_propagates_as_404(self, store):
        # The SSL handling must not swallow ordinary HTTP errors: 404 -> None.
        with patch.object(MdTreeStore, "_send", side_effect=_http_error(404)):
            assert store.read("NOPE-999") is None

    def test_unreachable_vault_raises_concise_error(self, store):
        # A non-cert transport failure (down vault / wrong URL) becomes a
        # concise MdTreeStoreError, not a raw traceback — and not the TLS hint.
        err = ConnectionRefusedError("Connection refused")
        with patch("http.client.HTTPSConnection.connect", side_effect=err):
            with pytest.raises(MdTreeStoreError) as exc:
                store.read("TASK-001")
        msg = str(exc.value)
        assert "Could not reach the vault" in msg
        assert "Connection refused" in msg
        assert "mkcert" not in msg  # not the TLS hint

    def test_a_url_that_is_not_http_is_a_store_error(self):
        store = MdTreeStore("agent-memory.home.lab", "myrepo")
        with pytest.raises(MdTreeStoreError, match="http"):
            store.read("TASK-001")


# ---------------------------------------------------------------------------
# Config.toml discovery (TASK-001)
# ---------------------------------------------------------------------------

class TestConfigTomlDiscovery:
    def test_no_config_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "dir"
        assert cfg["docs_root"] == (tmp_path / "llpm").resolve()

    def test_finds_config_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "dir"\nroot = "./mytickets"\n')

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "dir"
        assert cfg["docs_root"] == (tmp_path / "mytickets").resolve()

    def test_finds_config_in_parent(self, tmp_path, monkeypatch):
        subdir = tmp_path / "src" / "module"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "dir"\nroot = "./llpm"\n')

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "dir"
        assert cfg["docs_root"] == (tmp_path / "llpm").resolve()

    def test_mdtree_config_parsed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[store]\nkind = "mdtree"\nurl = "https://agent-memory.home.lab"\nstem = "myrepo"\n'
        )

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "mdtree"
        assert cfg["base_url"] == "https://agent-memory.home.lab"
        assert cfg["repo_stem"] == "myrepo"
        assert cfg["ca"] is None

    def test_mdtree_config_parses_ca(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[store]\nkind = "mdtree"\nurl = "https://agent-memory.home.lab"\n'
            'stem = "myrepo"\nca = "/opt/pki/rootCA.pem"\n'
        )

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["ca"] == "/opt/pki/rootCA.pem"

    def test_mdtree_config_ca_relative_resolves_to_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[store]\nkind = "mdtree"\nurl = "https://agent-memory.home.lab"\n'
            'stem = "myrepo"\nca = "certs/rootCA.pem"\n'
        )

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["ca"] == str((tmp_path / "certs" / "rootCA.pem").resolve())

    def test_unknown_kind_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "unknown_kind"\n')

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        with pytest.raises(SystemExit):
            _resolve_store_config(Args())

    def test_env_var_takes_priority_over_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "dir"\nroot = "./from_config"\n')
        monkeypatch.setenv("LLPM_DOCS_ROOT", str(tmp_path / "from_env"))

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert "from_env" in str(cfg["docs_root"])


# ---------------------------------------------------------------------------
# load_frontmatter: a whole board in ONE request (the board-load fast path)
# ---------------------------------------------------------------------------

def _listing(items: list[dict]) -> dict:
    return {"items": items, "total": len(items), "limit": 1000, "offset": 0}


def _fm(ticket_id: str, **over) -> dict:
    fm = {"id": ticket_id, "type": "task", "title": f"T {ticket_id}",
          "status": "open", "priority": "medium"}
    fm.update(over)
    return fm


class TestLoadFrontmatter:
    NS = "repos.myrepo.llpm"

    def _page(self):
        return _listing([
            {"stem": f"{self.NS}.tasks.TASK-001", "frontmatter": _fm("TASK-001")},
            {"stem": f"{self.NS}.features.FEAT-001", "frontmatter": _fm("FEAT-001", type="feature")},
            {"stem": f"{self.NS}.archive.TASK-000", "frontmatter": _fm("TASK-000", status="complete")},
            # never tickets: a note BELOW a ticket, and the board's own blobs
            {"stem": f"{self.NS}.tasks.TASK-001.agent-workers.w1", "frontmatter": _fm("TASK-001")},
            {"stem": f"{self.NS}.todo", "frontmatter": {}},
        ])

    def test_one_request_for_the_whole_board(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(self._page())
            pairs = store.load_frontmatter()

        # THE point of this method: one round trip, not one per ticket
        assert mock_open.call_count == 1
        url = mock_open.call_args[0][0]
        assert "include=frontmatter" in url
        assert [ref.name for ref, _ in pairs] == ["TASK-000", "FEAT-001", "TASK-001"]
        assert [fm["id"] for _, fm in pairs] == ["TASK-000", "FEAT-001", "TASK-001"]

    def test_subnotes_and_blobs_are_not_tickets(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(self._page())
            stems = [ref.vault_stem for ref, _ in store.load_frontmatter()]
        assert f"{self.NS}.tasks.TASK-001.agent-workers.w1" not in stems
        assert f"{self.NS}.todo" not in stems

    def test_include_archive_false_drops_the_archive(self, store):
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.return_value = _response(self._page())
            pairs = store.load_frontmatter(include_archive=False)
        assert [ref.name for ref, _ in pairs] == ["FEAT-001", "TASK-001"]
        assert all(not ref.is_archived for ref, _ in pairs)

    def test_falls_back_per_stem_when_the_vault_cannot_include_frontmatter(self, store):
        """An older vault answers the listing without a `frontmatter` key."""
        listing = _listing([{"stem": f"{self.NS}.tasks.TASK-001"}])
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.side_effect = [_response(listing), _response(_fm("TASK-001"))]
            pairs = store.load_frontmatter()

        assert [fm["id"] for _, fm in pairs] == ["TASK-001"]
        assert mock_open.call_count == 2
        assert mock_open.call_args_list[1][0][0].endswith("/frontmatter")

    def test_an_unreadable_note_is_skipped_not_fatal(self, store):
        listing = _listing([
            {"stem": f"{self.NS}.tasks.TASK-001"},          # frontmatter fetch 404s
            {"stem": f"{self.NS}.tasks.TASK-002", "frontmatter": _fm("TASK-002")},
        ])
        with patch.object(MdTreeStore, "_send") as mock_open:
            mock_open.side_effect = [_response(listing), _http_error(404)]
            pairs = store.load_frontmatter()
        assert [fm["id"] for _, fm in pairs] == ["TASK-002"]


# ---------------------------------------------------------------------------
# Keep-alive (TASK-015) -- a real HTTP/1.1 server on localhost, so reuse, a
# dropped connection and the retry are real socket behaviour, not a mock's
# ---------------------------------------------------------------------------

def _id(i: int) -> str:
    return f"TASK-{i:03d}"


def _stem(i: int) -> str:
    return f"repos.myrepo.llpm.tasks.{_id(i)}"


def _note(i: int) -> str:
    return TICKET_CONTENT.replace("TASK-001", _id(i))


class _FakeVault(http.server.ThreadingHTTPServer):
    """The vault calls these tests make -- the paginated listing, ``/raw``
    reads and ``PUT`` -- over in-memory notes, counting connections and
    requests. ``hang_up(n)`` decides the fate of request ``n``: None answers
    and keeps the connection, ``"after"`` answers then closes without saying
    so (an idle timeout, a proxy recycling), ``"before"`` closes unanswered."""

    daemon_threads = True

    def __init__(self, notes: dict[str, str]):
        self.notes = dict(notes)
        self.connections = 0
        self.requests = 0
        self.hang_up = lambda n: None
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _VaultHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _VaultHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive unless someone closes

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._answer(self._get)

    def do_PUT(self):
        content = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["content"]
        self.server.notes[self._stem()] = content
        self._answer(lambda: (200, {}))

    def _stem(self) -> str:
        path = urllib.parse.urlsplit(self.path).path
        return urllib.parse.unquote(path.removeprefix("/api/v1/notes/").removesuffix("/raw"))

    def _get(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/api/v1/notes":
            q = dict(urllib.parse.parse_qsl(url.query))
            stems = sorted(s for s in self.server.notes if fnmatch.fnmatchcase(s, q["pattern"]))
            offset, limit = int(q.get("offset", 0)), int(q.get("limit", 100))
            items = []
            for stem in stems[offset:offset + limit]:
                item = {"stem": stem, "title": None}
                if q.get("include") == "frontmatter":
                    item["frontmatter"] = parse_text(self.server.notes[stem])[0]
                items.append(item)
            return 200, {"items": items, "total": len(stems)}
        content = self.server.notes.get(self._stem())
        return (200, content.encode()) if content is not None else (404, {"detail": "no note"})

    def _answer(self, respond):
        with self.server.lock:
            self.server.requests += 1
            n = self.server.requests
        fate = self.server.hang_up(n)
        if fate == "before":
            self.close_connection = True
            return
        status, body = respond()
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        if fate == "after":
            self.close_connection = True  # no `Connection: close` -- the client isn't told


@pytest.fixture
def vault():
    server = _FakeVault({_stem(i): _note(i) for i in range(1, 6)})
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def live_store(vault):
    store = MdTreeStore(vault.url, "myrepo")
    yield store
    store.close()


class TestKeepAlive:
    N = 5  # tickets on the fake vault

    def test_a_listing_and_n_reads_share_one_connection(self, vault, live_store):
        pairs = live_store.load_frontmatter()
        bodies = [live_store.read_ref(ref)[1] for ref, _ in pairs]

        assert len(bodies) == self.N
        assert vault.requests == self.N + 1
        assert vault.connections == 1

    def test_error_responses_leave_the_connection_usable(self, vault, live_store):
        # read() probes tasks, features, epics, research, archive in turn:
        # four 404s, then the hit -- all five on one connection.
        vault.notes["repos.myrepo.llpm.archive.TASK-009"] = _note(9)
        ref, fm, _ = live_store.read("TASK-009")

        assert fm["id"] == "TASK-009" and ref.is_archived
        assert vault.requests == 5
        assert vault.connections == 1

    def test_writes_ride_the_same_connection(self, vault, live_store):
        ref, fm, body = live_store.read("TASK-001")
        fm["status"] = "complete"
        live_store.write(ref, fm, body)

        assert live_store.read("TASK-001")[1]["status"] == "complete"
        assert vault.connections == 1

    def test_a_server_that_hangs_up_after_every_answer(self, vault, live_store):
        vault.hang_up = lambda n: "after"
        ids = [fm["id"] for _, fm in live_store.load_frontmatter()]

        assert [live_store.read(i)[1]["id"] for i in ids] == ids
        assert vault.connections == vault.requests == self.N + 1

    def test_a_hang_up_while_idle_is_seen_before_sending(self, vault, live_store):
        # The common drop: the server closed an idle connection. The request
        # must go out on a new connection, never into the dead one -- which
        # matters for a create or a move, not just a read.
        vault.hang_up = lambda n: "after" if n == 1 else None
        live_store.read("TASK-001")
        sock = live_store._local.conn.sock
        deadline = time.monotonic() + 5
        while not MdTreeStore._peer_closed(sock):  # until the FIN lands
            assert time.monotonic() < deadline
            time.sleep(0.01)

        with patch.object(MdTreeStore, "_exchange", wraps=MdTreeStore._exchange) as exchange:
            assert live_store.read("TASK-002")[1]["id"] == "TASK-002"
        assert exchange.call_count == 1
        assert vault.connections == 2

    def test_a_request_dropped_on_a_reused_connection_is_retried_once(self, vault, live_store):
        # The race the idle check can't see: the server hangs up as the
        # request arrives. Resent once, on a new connection, transparently.
        vault.hang_up = lambda n: "before" if n == 2 else None
        live_store.read("TASK-001")

        assert live_store.read("TASK-002")[1]["id"] == "TASK-002"
        assert vault.requests == 3  # the dropped one + its retry
        assert vault.connections == 2

    def test_retried_once_not_in_a_loop(self, vault, live_store):
        vault.hang_up = lambda n: "before" if n >= 2 else None
        live_store.read("TASK-001")

        with pytest.raises(MdTreeStoreError, match="Could not reach the vault"):
            live_store.read_ref(VaultRef(_stem(2)))
        assert vault.requests == 3

    def test_a_fresh_connection_that_is_dropped_is_not_retried(self, vault, live_store):
        # Not staleness: the server saw this request. Resending a create or a
        # move on a guess could apply it twice.
        vault.hang_up = lambda n: "before"

        with pytest.raises(MdTreeStoreError, match="Could not reach the vault"):
            live_store.read_ref(VaultRef(_stem(1)))
        assert vault.requests == 1

    def test_each_thread_gets_its_own_connection(self, vault, live_store):
        # `llpm serve` shares one store across a threadpool; one shared
        # connection would interleave their exchanges.
        errors = []

        def worker(i):
            try:
                for _ in range(10):
                    assert live_store.read_ref(VaultRef(_stem(i)))[0]["id"] == _id(i)
            except BaseException as e:
                errors.append(e)
            finally:
                live_store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert vault.requests == 40
        assert vault.connections == 4
