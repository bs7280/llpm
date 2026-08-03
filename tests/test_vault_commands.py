"""Vault-mode command tests (TASK-004).

All commands that previously used the legacy
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)
    store = _make_store(docs_root)
pattern are tested here against FakeStore (in-memory seam) wired in via
monkeypatch, mirroring the TestFakeStoreSeam pattern in test_store.py.

Each test proves:
1. The command no longer hits the filesystem for ticket I/O.
2. The command round-trips correctly through the store seam.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

from llpm import commands, parser
from llpm.__main__ import main


# ---------------------------------------------------------------------------
# FakeStore — identical to the one in test_store.py; duplicated here so this
# test file stays self-contained (avoids importing from test_store which isn't
# a proper module).
# ---------------------------------------------------------------------------

class FakeStore:
    """In-memory TicketStore. No filesystem access for ticket data."""

    def __init__(self):
        self.active = {}    # filename -> (frontmatter, body)
        self.archived = {}  # filename -> (frontmatter, body)
        self.blobs = {}     # name -> text
        self.foreign = {}   # vault stem -> frontmatter (cross-board notes)
        self.foreign_reachable = True
        self.goal_notes = {}  # vault stem -> frontmatter (type: goal notes)

    def list_tickets(self, include_archive=True):
        refs = [PurePosixPath(name) for name in self.active]
        if include_archive:
            refs.extend(PurePosixPath("archive") / name for name in self.archived)
        return sorted(refs)

    def read(self, ticket_id):
        upper_id = ticket_id.upper()
        for ref in self.list_tickets(include_archive=True):
            if ref.name.upper().startswith(upper_id):
                fm, body = self.read_ref(ref)
                return ref, fm, body
        return None

    def read_ref(self, ref):
        fm, body = self._bucket(ref)[ref.name]
        return dict(fm), body

    def write(self, ref, frontmatter, body):
        self._bucket(ref)[ref.name] = (dict(frontmatter), body)

    def create_exclusive(self, filename, content):
        if filename in self.active or filename in self.archived:
            raise FileExistsError(filename)
        fm, body = parser.parse_text(content, source=filename)
        self.active[filename] = (fm, body)
        return PurePosixPath(filename)

    def archive(self, ref):
        self.archived[ref.name] = self.active.pop(ref.name)
        return PurePosixPath("archive") / ref.name

    def delete(self, ref):
        del self._bucket(ref)[ref.name]

    def read_blob(self, name):
        return self.blobs.get(name)

    def write_blob(self, name, text):
        self.blobs[name] = text

    def exists(self, ticket_id):
        return self.read(ticket_id) is not None

    def read_foreign(self, stem):
        if not self.foreign_reachable:
            return ("unavailable", None)
        if stem in self.foreign:
            return ("ok", dict(self.foreign[stem]))
        return ("missing", None)

    def scan_by_type(self, type_value):
        return [
            (stem, dict(fm))
            for stem, fm in self.goal_notes.items()
            if fm.get("type") == type_value
        ]

    def _bucket(self, ref):
        return self.archived if ref.parent.name == "archive" else self.active


def _seed(store, ticket_id, title, *, ticket_type="task", status="open",
          parent=None, blockers=None, serves=None, tags=None, origin=None, created_by=None):
    fm = {
        "id": ticket_id,
        "type": ticket_type,
        "title": title,
        "status": status,
        "priority": "medium",
        "parent": parent,
        "blockers": blockers or [],
        "created": "2026-01-01",
        "updated": "2026-01-01",
        "completed": None,
        "tags": tags or [],
    }
    if serves is not None:
        fm["serves"] = serves
    if origin is not None:
        fm["origin"] = origin
    if created_by is not None:
        fm["created_by"] = created_by
    slug = title.upper().replace(" ", "_")
    store.active[f"{ticket_id}_{slug}.md"] = (fm, f"# {title}\n")


@pytest.fixture
def vault_project(tmp_path, monkeypatch):
    """A FakeStore wired into command dispatch via _resolve_store_and_root.

    The docs sentinel dir does NOT have a tickets/ subdir — proving that
    vault commands bypass the local-init check.
    """
    docs = tmp_path / "vault-sentinel"
    # Do NOT create docs/tickets — vault commands must not need it.
    fake = FakeStore()
    # Patch _resolve_store_and_root to return (fake, docs) for any args
    monkeypatch.setattr(
        commands,
        "_resolve_store_and_root",
        lambda args: (fake, docs),
    )
    return docs, fake


def _run(*args, docs=None):
    cmd = []
    if docs:
        cmd.extend(["--docs-root", str(docs)])
    cmd.extend(args)
    main(cmd)


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestVaultStatus:
    def test_status_flip(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")

        _run("status", "TASK-101", "in-progress", docs=docs)

        out = capsys.readouterr().out
        assert "TASK-101: open -> in-progress" in out
        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["status"] == "in-progress"
        # No real filesystem ticket I/O
        assert not (docs / "tickets").exists()

    def test_status_complete_sets_completed_date(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")

        with patch.object(commands, "_today", return_value="2026-07-05"):
            _run("status", "TASK-101", "complete", docs=docs)

        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["completed"] == "2026-07-05"

    def test_status_not_found(self, vault_project):
        docs, fake = vault_project
        with pytest.raises(SystemExit):
            _run("status", "NOPE-999", "open", docs=docs)

    def test_awaiting_round_trips_through_store_seam(self, vault_project, capsys):
        """FEAT-012: --awaiting must work through MdTreeStore (and any store),
        not just LocalDirStore -- it's written via the same _write_ticket
        seam as every other frontmatter mutation."""
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task", status="in-progress")

        _run("status", "TASK-101", "review", "--awaiting", "deploy", docs=docs)
        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["awaiting"] == "deploy"

        # Self-clear on the next transition, same as the local-dir store.
        _run("status", "TASK-101", "complete", docs=docs)
        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert "awaiting" not in fm


# ---------------------------------------------------------------------------
# cmd_set
# ---------------------------------------------------------------------------

class TestVaultSet:
    def test_set_field(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")

        _run("set", "TASK-101", "priority=high", docs=docs)

        out = capsys.readouterr().out
        assert "priority = high" in out
        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["priority"] == "high"

    def test_set_multiple_fields(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")

        _run("set", "TASK-101", "priority=high", "effort=large", docs=docs)

        out = capsys.readouterr().out
        assert "priority = high" in out
        assert "effort = large" in out

    def test_set_tags(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")

        _run("set", "TASK-101", "tags=infra,vault", docs=docs)

        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["tags"] == ["infra", "vault"]

    def test_set_parent_validates_existence(self, vault_project):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        # EPIC-001 doesn't exist in the store
        with pytest.raises(SystemExit):
            _run("set", "TASK-101", "parent=EPIC-001", docs=docs)

    def test_set_not_found(self, vault_project):
        docs, fake = vault_project
        with pytest.raises(SystemExit):
            _run("set", "NOPE-999", "priority=high", docs=docs)


# ---------------------------------------------------------------------------
# cmd_blocker_add / rm / list
# ---------------------------------------------------------------------------

class TestVaultBlocker:
    def test_blocker_add(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Blocker ticket", status="open")
        _seed(fake, "TASK-102", "Blocked ticket")

        _run("blocker", "add", "TASK-102", "--blocked-by", "TASK-101", docs=docs)

        out = capsys.readouterr().out
        assert "now blocked by 'TASK-101'" in out
        fm, _ = fake.active["TASK-102_BLOCKED_TICKET.md"]
        assert "TASK-101" in fm["blockers"]

    def test_blocker_add_duplicate(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Blocker", status="open")
        _seed(fake, "TASK-102", "Blocked", blockers=["TASK-101"])

        _run("blocker", "add", "TASK-102", "--blocked-by", "TASK-101", docs=docs)

        out = capsys.readouterr().out
        assert "already blocked by" in out

    def test_blocker_add_nonexistent_blocker(self, vault_project):
        docs, fake = vault_project
        _seed(fake, "TASK-102", "Blocked ticket")
        with pytest.raises(SystemExit):
            _run("blocker", "add", "TASK-102", "--blocked-by", "FAKE-999", docs=docs)

    def test_blocker_rm(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Blocker", status="complete")
        _seed(fake, "TASK-102", "Blocked", blockers=["TASK-101"])

        _run("blocker", "rm", "TASK-102", "--blocked-by", "TASK-101", docs=docs)

        out = capsys.readouterr().out
        assert "removed blocker 'TASK-101'" in out
        fm, _ = fake.active["TASK-102_BLOCKED.md"]
        assert "TASK-101" not in fm["blockers"]

    def test_blocker_rm_not_found(self, vault_project):
        docs, fake = vault_project
        _seed(fake, "TASK-102", "Blocked ticket")
        with pytest.raises(SystemExit):
            _run("blocker", "rm", "TASK-102", "--blocked-by", "NOPE-999", docs=docs)

    def test_blocker_list(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Blocker", status="open")
        _seed(fake, "TASK-102", "Blocked", blockers=["TASK-101"])

        _run("blocker", "list", "TASK-102", docs=docs)

        out = capsys.readouterr().out
        assert "[BLOCKING]" in out
        assert "BLOCKED" in out

    def test_blocker_list_resolved(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Blocker", status="complete")
        _seed(fake, "TASK-102", "Blocked", blockers=["TASK-101"])

        _run("blocker", "list", "TASK-102", docs=docs)

        out = capsys.readouterr().out
        assert "[RESOLVED]" in out
        assert "all blockers resolved" in out

    def test_blocker_list_no_blockers(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "No blockers")

        _run("blocker", "list", "TASK-101", docs=docs)

        out = capsys.readouterr().out
        assert "No blockers" in out


# ---------------------------------------------------------------------------
# cmd_archive
# ---------------------------------------------------------------------------

class TestVaultArchive:
    def test_archive_single(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Done thing", status="complete")

        _run("archive", "TASK-101", docs=docs)

        out = capsys.readouterr().out
        assert "Archived TASK-101" in out
        assert "TASK-101_DONE_THING.md" in fake.archived
        assert fake.active == {}

    def test_archive_non_closed_fails(self, vault_project):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Open thing", status="open")
        with pytest.raises(SystemExit):
            _run("archive", "TASK-101", docs=docs)

    def test_archive_all(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Done one", status="complete")
        _seed(fake, "TASK-102", "Done two", status="closed")
        _seed(fake, "TASK-103", "Still open", status="open")

        _run("archive", "--all", "--yes", docs=docs)

        out = capsys.readouterr().out
        assert "Archived 2 ticket(s)" in out
        assert "TASK-101_DONE_ONE.md" in fake.archived
        assert "TASK-102_DONE_TWO.md" in fake.archived
        # open ticket stays active
        assert "TASK-103_STILL_OPEN.md" in fake.active

    def test_archive_all_none(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Open thing", status="open")

        _run("archive", "--all", "--yes", docs=docs)

        out = capsys.readouterr().out
        assert "No closed tickets" in out


# ---------------------------------------------------------------------------
# cmd_delete
# ---------------------------------------------------------------------------

class TestVaultDelete:
    def test_delete(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Delete me")

        _run("delete", "TASK-101", "--yes", docs=docs)

        out = capsys.readouterr().out
        assert "Deleted TASK-101" in out
        assert fake.read("TASK-101") is None

    def test_delete_cleans_up_blockers(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Blocker to delete")
        _seed(fake, "TASK-102", "References blocker", blockers=["TASK-101"])

        _run("delete", "TASK-101", "--yes", docs=docs)

        fm, _ = fake.active["TASK-102_REFERENCES_BLOCKER.md"]
        assert "TASK-101" not in fm["blockers"]

    def test_delete_clears_parent(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "EPIC-001", "Parent epic", ticket_type="epic")
        _seed(fake, "TASK-101", "Child task", parent="EPIC-001")

        _run("delete", "EPIC-001", "--yes", docs=docs)

        fm, _ = fake.active["TASK-101_CHILD_TASK.md"]
        assert fm["parent"] is None

    def test_delete_not_found(self, vault_project):
        docs, fake = vault_project
        with pytest.raises(SystemExit):
            _run("delete", "NOPE-999", "--yes", docs=docs)


# ---------------------------------------------------------------------------
# cmd_todo
# ---------------------------------------------------------------------------

class TestVaultTodo:
    def test_todo_add(self, vault_project, capsys):
        docs, fake = vault_project

        _run("todo", "--add", "vault task here", docs=docs)

        out = capsys.readouterr().out
        assert "(1) vault task here" in out
        assert fake.blobs["TODO.md"] == "- (1) vault task here\n"

    def test_todo_list(self, vault_project, capsys):
        docs, fake = vault_project
        fake.blobs["TODO.md"] = "- (1) item one\n- (2) item two\n"

        _run("todo", "--list", docs=docs)

        out = capsys.readouterr().out
        assert "TODO (2 items)" in out
        assert "(1) item one" in out

    def test_todo_rm(self, vault_project, capsys):
        docs, fake = vault_project
        fake.blobs["TODO.md"] = "- (1) remove me\n"

        _run("todo", "--rm", "1", docs=docs)

        out = capsys.readouterr().out
        assert "Removed (1): remove me" in out
        assert fake.blobs["TODO.md"].strip() == ""

    def test_todo_ids_never_reuse(self, vault_project, capsys):
        docs, fake = vault_project

        _run("todo", "--add", "A", docs=docs)
        _run("todo", "--add", "B", docs=docs)
        _run("todo", "--rm", "1", docs=docs)
        _run("todo", "--add", "C", docs=docs)

        out = capsys.readouterr().out
        assert "(3) C" in out

    def test_todo_empty_list(self, vault_project, capsys):
        docs, fake = vault_project

        _run("todo", "--list", docs=docs)

        out = capsys.readouterr().out
        assert "empty" in out

    def test_todo_rm_not_found(self, vault_project):
        docs, fake = vault_project
        with pytest.raises(SystemExit):
            _run("todo", "--rm", "99", docs=docs)

    def test_todo_no_filesystem_writes(self, vault_project, capsys):
        docs, fake = vault_project

        _run("todo", "--add", "check isolation", docs=docs)

        # Nothing written to the sentinel docs dir
        assert not (docs / "TODO.md").exists()


# ---------------------------------------------------------------------------
# cmd_serves (FEAT-004)
# ---------------------------------------------------------------------------

class TestVaultServes:
    def test_serves_add_round_trips(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "FEAT-101", "A feature", ticket_type="feature")

        _run("serves", "add", "FEAT-101", "goals.unified-agent-platform", docs=docs)

        out = capsys.readouterr().out
        assert "now serves" in out
        fm, _ = fake.active["FEAT-101_A_FEATURE.md"]
        assert fm["serves"] == ["goals.unified-agent-platform"]

    def test_serves_task_rejected(self, vault_project):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "A task")
        with pytest.raises(SystemExit):
            _run("serves", "add", "TASK-101", "goals.a", docs=docs)


# ---------------------------------------------------------------------------
# cmd_goals (FEAT-008)
# ---------------------------------------------------------------------------

def _seed_goal(fake, stem, title, *, status="stamped"):
    fake.goal_notes[stem] = {"type": "goal", "title": title, "status": status}


class TestVaultGoals:
    def test_no_goal_notes(self, vault_project, capsys):
        docs, fake = vault_project
        _run("goals", docs=docs)
        out = capsys.readouterr().out
        assert "No 'type: goal' notes found." in out

    def test_rollup_direct_and_inherited_serving(self, vault_project, capsys):
        docs, fake = vault_project
        _seed_goal(fake, "goals.my-goal", "My Goal")
        _seed(fake, "EPIC-101", "An epic", ticket_type="epic", status="in-progress",
              serves=["goals.my-goal"])
        _seed(fake, "TASK-101", "Inherits via parent", parent="EPIC-101", status="open")
        _seed(fake, "TASK-102", "Unrelated", status="open")

        _run("goals", docs=docs)

        out = capsys.readouterr().out
        assert "goals.my-goal" in out
        assert "[stamped]" in out
        assert "EPIC-101 (in-progress)" in out
        assert "TASK-101 (open)" in out
        assert "TASK-102" not in out

    def test_draft_goal_not_flagged_as_gap(self, vault_project, capsys):
        docs, fake = vault_project
        _seed_goal(fake, "goals.proposed", "Proposed Goal", status="draft")

        _run("goals", docs=docs)

        out = capsys.readouterr().out
        assert "draft -- proposal, not binding" in out
        assert "-- 0 unplanned gap(s) --" in out

    def test_stamped_goal_with_no_serving_tickets_is_a_gap(self, vault_project, capsys):
        docs, fake = vault_project
        _seed_goal(fake, "goals.orphaned", "Orphaned Goal")

        _run("goals", docs=docs)

        out = capsys.readouterr().out
        assert "UNPLANNED GAP" in out
        assert "-- 1 unplanned gap(s) --" in out
        assert "goals.orphaned" in out

    def test_json_output(self, vault_project, capsys):
        docs, fake = vault_project
        _seed_goal(fake, "goals.my-goal", "My Goal")
        _seed(fake, "FEAT-101", "A feature", ticket_type="feature", status="complete",
              serves=["goals.my-goal"])

        _run("goals", "--json", docs=docs)

        import json
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data) == 1
        goal = data[0]
        assert goal["stem"] == "goals.my-goal"
        assert goal["stamped"] is True
        assert goal["total"] == 1
        assert goal["done"] == 1
        assert goal["pct_done"] == 100
        assert goal["unplanned_gap"] is False


# ---------------------------------------------------------------------------
# cmd_orphans (FEAT-011)
# ---------------------------------------------------------------------------

class TestVaultOrphans:
    def test_no_orphans(self, vault_project, capsys):
        docs, fake = vault_project
        _run("orphans", docs=docs)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out

    def test_unattached_agent_ticket_reported(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Drifted idea", origin="agent", created_by="s-1")
        _run("orphans", docs=docs)
        out = capsys.readouterr().out
        assert "1 orphaned agent-created ticket(s)" in out
        assert "TASK-101" in out

    def test_triaged_ticket_not_reported(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Needs triage", origin="agent", created_by="s-1", tags=["triage"])
        _run("orphans", docs=docs)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out

    def test_goal_attached_ticket_not_reported(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "FEAT-101", "New capability", ticket_type="feature",
              origin="agent", created_by="s-1", serves=["goals.my-goal"])
        _run("orphans", docs=docs)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out

    def test_human_origin_never_reported(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Human idea")  # no origin set
        _run("orphans", docs=docs)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out


# ---------------------------------------------------------------------------
# cmd_waits + cross-board effective status (FEAT-005)
# ---------------------------------------------------------------------------

FOREIGN_STEM = "repos.marginalia.llpm.features.FEAT-010"


def _foreign_fm(status="open"):
    return {"id": "FEAT-010", "type": "feature", "title": "Foreign", "status": status}


class TestVaultWaits:
    def test_waits_add_reports_target_status(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        fake.foreign[FOREIGN_STEM] = _foreign_fm("in-progress")

        _run("waits", "add", "TASK-101", "--on", FOREIGN_STEM, docs=docs)

        out = capsys.readouterr().out
        assert f"now waits on '{FOREIGN_STEM}'" in out
        assert "currently: in-progress" in out
        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["waits_on"] == [FOREIGN_STEM]

    def test_waits_add_missing_target_warns(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")

        _run("waits", "add", "TASK-101", "--on", "repos.gone.llpm.tasks.TASK-001", docs=docs)

        out = capsys.readouterr().out
        assert "not found in the vault" in out
        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["waits_on"] == ["repos.gone.llpm.tasks.TASK-001"]

    def test_waits_add_ticket_id_rejected(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        with pytest.raises(SystemExit):
            _run("waits", "add", "TASK-101", "--on", "FEAT-010", docs=docs)
        err = capsys.readouterr().err
        assert "llpm blocker" in err

    def test_waits_add_duplicate(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        fake.foreign[FOREIGN_STEM] = _foreign_fm()
        _run("waits", "add", "TASK-101", "--on", FOREIGN_STEM, docs=docs)
        _run("waits", "add", "TASK-101", "--on", FOREIGN_STEM, docs=docs)
        out = capsys.readouterr().out
        assert "already waits on" in out

    def test_waits_rm(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        fake.foreign[FOREIGN_STEM] = _foreign_fm()
        _run("waits", "add", "TASK-101", "--on", FOREIGN_STEM, docs=docs)

        _run("waits", "rm", "TASK-101", "--on", FOREIGN_STEM, docs=docs)

        out = capsys.readouterr().out
        assert "no longer waits on" in out
        fm, _ = fake.active["TASK-101_MY_TASK.md"]
        assert fm["waits_on"] == []

    def test_waits_rm_not_present(self, vault_project):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        with pytest.raises(SystemExit):
            _run("waits", "rm", "TASK-101", "--on", FOREIGN_STEM, docs=docs)

    def test_waits_list_states(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        fake.foreign[FOREIGN_STEM] = _foreign_fm("complete")
        _run("waits", "add", "TASK-101", "--on", FOREIGN_STEM, docs=docs)
        capsys.readouterr()

        _run("waits", "list", "TASK-101", docs=docs)

        out = capsys.readouterr().out
        assert "[RESOLVED]" in out
        assert "all waits resolved" in out

    def test_cannot_set_waits_on_via_set(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "My task")
        with pytest.raises(SystemExit):
            _run("set", "TASK-101", f"waits_on={FOREIGN_STEM}", docs=docs)
        err = capsys.readouterr().err
        assert "llpm waits" in err


class TestVaultWaitsEffectiveStatus:
    """waits_on contributes to derived 'blocked' -- and degrades gracefully."""

    def _seed_waiting(self, fake, status="open"):
        _seed(fake, "TASK-101", "Waiting task")
        fm, body = fake.active["TASK-101_WAITING_TASK.md"]
        fm["waits_on"] = [FOREIGN_STEM]
        fake.active["TASK-101_WAITING_TASK.md"] = (fm, body)

    def test_unresolved_target_blocks(self, vault_project, capsys):
        docs, fake = vault_project
        self._seed_waiting(fake)
        fake.foreign[FOREIGN_STEM] = _foreign_fm("in-progress")

        _run("board", "--json", docs=docs)

        import json
        data = json.loads(capsys.readouterr().out)
        t = next(t for t in data if t["id"] == "TASK-101")
        assert t["effective_status"] == "blocked"
        assert t["waits_on"][0]["blocking"] is True
        assert t["waits_on"][0]["status"] == "in-progress"

    def test_resolved_target_does_not_block(self, vault_project, capsys):
        docs, fake = vault_project
        self._seed_waiting(fake)
        fake.foreign[FOREIGN_STEM] = _foreign_fm("complete")

        _run("board", "--json", docs=docs)

        import json
        data = json.loads(capsys.readouterr().out)
        t = next(t for t in data if t["id"] == "TASK-101")
        assert t["effective_status"] == "open"
        assert t["waits_on"][0]["resolved"] is True

    def test_missing_target_blocks(self, vault_project, capsys):
        docs, fake = vault_project
        self._seed_waiting(fake)
        # FOREIGN_STEM not in fake.foreign -> missing

        _run("board", "--json", docs=docs)

        import json
        data = json.loads(capsys.readouterr().out)
        t = next(t for t in data if t["id"] == "TASK-101")
        assert t["effective_status"] == "blocked"
        assert t["waits_on"][0]["state"] == "missing"

    def test_unreachable_vault_does_not_block(self, vault_project, capsys):
        docs, fake = vault_project
        self._seed_waiting(fake)
        fake.foreign_reachable = False

        _run("board", "--json", docs=docs)

        import json
        data = json.loads(capsys.readouterr().out)
        t = next(t for t in data if t["id"] == "TASK-101")
        assert t["effective_status"] == "open"
        assert t["waits_on"][0]["state"] == "unavailable"
        assert t["waits_on"][0]["blocking"] is False


# ---------------------------------------------------------------------------
# cmd_after (FEAT-006)
# ---------------------------------------------------------------------------

class TestVaultAfter:
    def test_after_add_round_trips(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "First task")
        _seed(fake, "TASK-102", "Second task")

        _run("after", "add", "TASK-102", "--after", "TASK-101", docs=docs)

        out = capsys.readouterr().out
        assert "now ordered after 'TASK-101'" in out
        fm, _ = fake.active["TASK-102_SECOND_TASK.md"]
        assert fm["after"] == ["TASK-101"]

    def test_after_never_blocks_json(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "First task")
        _seed(fake, "TASK-102", "Second task")
        _run("after", "add", "TASK-102", "--after", "TASK-101", docs=docs)
        capsys.readouterr()

        _run("board", "--json", docs=docs)

        import json
        data = json.loads(capsys.readouterr().out)
        t = next(t for t in data if t["id"] == "TASK-102")
        assert t["effective_status"] == "open"
        assert t["after"] == ["TASK-101"]


# ---------------------------------------------------------------------------
# --json derived fields flow through the store seam, not the sentinel path
# ---------------------------------------------------------------------------

class TestVaultJsonDerivedFields:
    def test_board_json_resolved_blocker_not_blocked(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Done blocker", status="complete")
        _seed(fake, "TASK-102", "Ready task", blockers=["TASK-101"])

        _run("board", "--json", docs=docs)

        import json
        data = json.loads(capsys.readouterr().out)
        t = next(t for t in data if t["id"] == "TASK-102")
        assert t["effective_status"] == "open"
        assert t["is_blocked"] is False
        assert t["blockers"] == [{"id": "TASK-101", "resolved": True}]

    def test_list_json_children_derived(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "EPIC-001", "Parent epic", ticket_type="epic")
        _seed(fake, "TASK-101", "Child task", parent="EPIC-001")

        _run("list", "--json", docs=docs)

        import json
        data = json.loads(capsys.readouterr().out)
        epic = next(t for t in data if t["id"] == "EPIC-001")
        assert epic["children"] == ["TASK-101"]


# ---------------------------------------------------------------------------
# Provenance (FEAT-007) — vault mode
# ---------------------------------------------------------------------------

class TestVaultProvenance:
    def test_create_injects_provenance(self, vault_project, capsys, monkeypatch):
        docs, fake = vault_project
        monkeypatch.setenv("LLPM_CREATED_BY", "dispatch-run-42")

        with patch.object(commands, "_today", return_value="2026-08-01"):
            # --triage: FEAT-011 requires agent-origin tickets to attach to a
            # goal or explicitly triage; unrelated to what this test checks.
            _run("create", "task", "Agent-made vault task", "--triage", docs=docs)

        _, fm, _ = fake.read("TASK-001")
        assert fm["managed_by"] == "llpm"
        assert fm["origin"] == "agent"
        assert fm["created_by"] == "dispatch-run-42"
        assert fm["commits"] == []

    def test_mutation_stamps_managed_by(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Legacy ticket")  # no managed_by in seed

        _run("set", "TASK-101", "priority=high", docs=docs)

        fm, _ = fake.active["TASK-101_LEGACY_TICKET.md"]
        assert fm["managed_by"] == "llpm"

    def test_status_review_records_explicit_commit(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Done work")

        _run("status", "TASK-101", "review", "--commit", "deadbeef", docs=docs)

        out = capsys.readouterr().out
        assert "Captured 1 commit(s)" in out
        fm, _ = fake.active["TASK-101_DONE_WORK.md"]
        assert fm["commits"] == ["deadbeef"]


class TestVaultCreateIntakePolicy:
    """FEAT-011: creation always succeeds regardless of goal attachment --
    exercised against FakeStore to prove that's store-agnostic, not just a
    LocalDirStore quirk. --serves/--triage remain optional conveniences."""

    def test_unattached_agent_ticket_still_succeeds(self, vault_project, capsys):
        docs, fake = vault_project
        _run("create", "task", "Orphan idea", "--origin", "agent",
             "--created-by", "s-1", docs=docs)
        out, err = capsys.readouterr()
        assert err == ""
        assert fake.read("TASK-001") is not None

    def test_triage_tags(self, vault_project, capsys):
        docs, fake = vault_project
        _run("create", "task", "Needs triage", "--origin", "agent",
             "--created-by", "s-1", "--triage", docs=docs)
        _, fm, _ = fake.read("TASK-001")
        assert fm["tags"] == ["triage"]

    def test_serves_optional_attachment(self, vault_project, capsys):
        docs, fake = vault_project
        _run("create", "feature", "New capability", "--origin", "agent",
             "--created-by", "s-1", "--serves", "goals.my-goal", docs=docs)
        _, fm, _ = fake.read("FEAT-001")
        assert fm["serves"] == ["goals.my-goal"]


# ---------------------------------------------------------------------------
# cmd_project
# ---------------------------------------------------------------------------

class TestVaultProject:
    def test_project_counts(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "Open task")
        _seed(fake, "TASK-102", "Done task", status="complete")

        _run("project", docs=docs)

        out = capsys.readouterr().out
        assert "Total:        2" in out
        assert "open" in out
        assert "complete" in out

    def test_project_json(self, vault_project, capsys):
        docs, fake = vault_project
        _seed(fake, "TASK-101", "A task")

        _run("project", "--json", docs=docs)

        import json
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["counts"]["total"] == 1
        assert "valid_statuses" in data
        assert "valid_types" in data


# ---------------------------------------------------------------------------
# cmd_create — bundled template fallback (scope 2)
# ---------------------------------------------------------------------------

class TestVaultCreateBundledFallback:
    """cmd_create falls back to bundled templates when store has no template."""

    def test_create_uses_bundled_when_store_has_no_template(self, vault_project, capsys):
        docs, fake = vault_project
        # No templates seeded in fake.blobs — store.read_blob returns None

        with patch.object(commands, "_today", return_value="2026-07-05"):
            _run("create", "task", "My vault task", docs=docs)

        out = capsys.readouterr().out
        assert "TASK-001" in out
        # Ticket was created in fake store
        assert fake.read("TASK-001") is not None
        _, fm, _ = fake.read("TASK-001")
        assert fm["type"] == "task"
        assert fm["title"] == "My vault task"

    def test_create_store_template_overrides_bundled(self, vault_project, capsys):
        docs, fake = vault_project
        # Seed a custom template in the store (override layer)
        custom_tmpl = (
            "---\n"
            'id: "__ID__"\n'
            "type: task\n"
            'title: "__TITLE__"\n'
            "status: draft  # draft | planned | open | in-progress | review | complete | closed | deferred\n"
            "priority: medium  # low | medium | high\n"
            "effort: null\n"
            "requires_human: false\n"
            "parent: null\n"
            "blockers: []\n"
            'created: "__DATE__"\n'
            'updated: "__DATE__"\n'
            "completed: null\n"
            "tags: []\n"
            "model_tier: heavy\n"
            "---\n\n## Vault Custom\n"
        )
        fake.blobs["templates/task.md"] = custom_tmpl

        with patch.object(commands, "_today", return_value="2026-07-05"):
            _run("create", "task", "Custom template task", docs=docs)

        _, fm, body = fake.read("TASK-001")
        # The store-side template sets model_tier: heavy
        assert fm.get("model_tier") == "heavy"
        assert "Vault Custom" in body

    def test_create_unknown_type_fails(self, vault_project):
        docs, fake = vault_project
        # Type "nonexistent" has no bundled template
        with pytest.raises(SystemExit):
            _run("create", "nonexistent", "Will fail", docs=docs)

    def test_create_all_bundled_types(self, vault_project, capsys):
        docs, fake = vault_project
        # All four bundled types should work without any store templates
        with patch.object(commands, "_today", return_value="2026-07-05"):
            for t in ("task", "feature", "epic", "research"):
                _run("create", t, f"A {t}", docs=docs)

        capsys.readouterr()  # drain
        assert len(fake.active) == 4


# ---------------------------------------------------------------------------
# cmd_init — vault-aware (scope 2)
# ---------------------------------------------------------------------------

class TestVaultInit:
    def test_init_vault_mode_prints_no_local_init_needed(self, tmp_path, monkeypatch, capsys):
        """llpm init in a vault-configured repo exits 0 with a helpful message."""
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[store]\nkind = "mdtree"\nurl = "https://agent-memory.home.lab"\nstem = "myrepo"\n'
        )

        # init succeeds (exit 0) — no SystemExit
        main(["init"])

        out = capsys.readouterr().out
        assert "no local init needed" in out.lower() or "vault store" in out.lower()
        # No local directories created
        assert not (tmp_path / "llpm").exists()

    def test_init_local_mode_still_works(self, tmp_path, capsys):
        """llpm init with local dir store continues to work as before."""
        docs = tmp_path / "docs"
        main(["--docs-root", str(docs), "init"])
        out = capsys.readouterr().out
        assert "Initialized" in out
        assert (docs / "tickets").exists()
        assert (docs / "templates" / "task.md").exists()
