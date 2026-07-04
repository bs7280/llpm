"""Tests for --json output on all read commands."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from llpm.__main__ import main
from llpm import commands, parser


def run_cli(*args, docs_root=None):
    """Helper to run CLI commands with a docs_root."""
    cmd = []
    if docs_root:
        cmd.extend(["--docs-root", str(docs_root)])
    cmd.extend(args)
    main(cmd)


def run_json(*args, docs_root=None, capsys=None):
    """Run a CLI command with --json and return parsed output."""
    run_cli(*args, docs_root=docs_root)
    return json.loads(capsys.readouterr().out)


# -- list --json --


class TestListJson:
    def test_returns_array(self, docs_root, capsys):
        data = run_json("list", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, list)
        assert len(data) == 5  # 5 active tickets

    def test_ticket_shape(self, docs_root, capsys):
        data = run_json("list", "--json", docs_root=docs_root, capsys=capsys)
        ticket = next(t for t in data if t["id"] == "FEAT-001")

        # All required fields present
        for key in (
            "id", "type", "title", "status", "effective_status", "is_blocked",
            "priority", "effort", "parent", "children", "blockers", "tags",
            "requires_human", "created", "updated", "completed", "archived", "path",
        ):
            assert key in ticket, f"Missing key: {key}"

        # No body in list mode
        assert "body" not in ticket

        # Spot-check values
        assert ticket["type"] == "feature"
        assert ticket["status"] == "complete"
        assert ticket["effective_status"] == "complete"
        assert ticket["is_blocked"] is False
        assert ticket["priority"] == "high"
        assert ticket["effort"] == "medium"
        assert ticket["parent"] == "EPIC-001"
        assert ticket["tags"] == ["templates", "core"]
        assert ticket["created"] == "2026-03-15"
        assert ticket["completed"] == "2026-03-18"
        assert ticket["archived"] is False
        assert "FEAT-001" in ticket["path"]

    def test_effective_status_blocked(self, docs_root, capsys):
        data = run_json("list", "--json", docs_root=docs_root, capsys=capsys)
        task = next(t for t in data if t["id"] == "TASK-001")
        assert task["status"] == "open"  # stored
        assert task["effective_status"] == "blocked"  # derived
        assert task["is_blocked"] is True

    def test_children_derived(self, docs_root, capsys):
        data = run_json("list", "--json", docs_root=docs_root, capsys=capsys)
        epic = next(t for t in data if t["id"] == "EPIC-001")
        assert "FEAT-001" in epic["children"]
        assert "FEAT-002" in epic["children"]

    def test_blockers_with_resolved(self, docs_root, capsys):
        data = run_json("list", "--json", docs_root=docs_root, capsys=capsys)
        task = next(t for t in data if t["id"] == "TASK-001")
        assert len(task["blockers"]) == 2
        # FEAT-001 is complete -> resolved, FEAT-002 is in-progress -> not resolved
        resolved = {b["id"]: b["resolved"] for b in task["blockers"]}
        assert resolved["FEAT-001"] is True
        assert resolved["FEAT-002"] is False

    def test_filter_status(self, docs_root, capsys):
        data = run_json("list", "--json", "--status", "blocked", docs_root=docs_root, capsys=capsys)
        assert all(t["effective_status"] == "blocked" for t in data)
        assert any(t["id"] == "TASK-001" for t in data)

    def test_filter_type(self, docs_root, capsys):
        data = run_json("list", "--json", "--type", "epic", docs_root=docs_root, capsys=capsys)
        assert all(t["type"] == "epic" for t in data)
        assert len(data) == 1

    def test_filter_parent(self, docs_root, capsys):
        data = run_json("list", "--json", "--parent", "EPIC-001", docs_root=docs_root, capsys=capsys)
        assert all(t["parent"] == "EPIC-001" for t in data)
        assert len(data) == 2

    def test_empty_returns_empty_array(self, tmp_path, capsys):
        docs = tmp_path / "docs"
        (docs / "tickets").mkdir(parents=True)
        data = run_json("list", "--json", docs_root=docs, capsys=capsys)
        assert data == []

    def test_no_matches_returns_empty_array(self, docs_root, capsys):
        data = run_json("list", "--json", "--status", "deferred", docs_root=docs_root, capsys=capsys)
        assert data == []

    def test_include_archived(self, docs_root, capsys):
        data = run_json("list", "--json", "--include-archived", docs_root=docs_root, capsys=capsys)
        ids = [t["id"] for t in data]
        assert "FEAT-000" in ids  # archived ticket included
        archived_ticket = next(t for t in data if t["id"] == "FEAT-000")
        assert archived_ticket["archived"] is True

    def test_excludes_archived_by_default(self, docs_root, capsys):
        data = run_json("list", "--json", docs_root=docs_root, capsys=capsys)
        ids = [t["id"] for t in data]
        assert "FEAT-000" not in ids

    def test_parseable_by_jq(self, docs_root, capsys):
        """Acceptance: output is valid JSON."""
        run_cli("list", "--json", docs_root=docs_root)
        raw = capsys.readouterr().out
        parsed = json.loads(raw)
        assert isinstance(parsed, list)


# -- show --json --


class TestShowJson:
    def test_returns_object_with_body(self, docs_root, capsys):
        data = run_json("show", "FEAT-001", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, dict)
        assert data["id"] == "FEAT-001"
        assert "body" in data
        assert "## Problem" in data["body"]
        assert data["body_html"] is None

    def test_body_roundtrip(self, docs_root, capsys):
        """Body matches the source file content after frontmatter."""
        path = parser.find_ticket_by_id(docs_root, "FEAT-001")
        _, original_body = parser.parse_document(path)
        data = run_json("show", "FEAT-001", "--json", docs_root=docs_root, capsys=capsys)
        assert data["body"] == original_body

    def test_all_frontmatter_fields(self, docs_root, capsys):
        data = run_json("show", "TASK-001", "--json", docs_root=docs_root, capsys=capsys)
        assert data["effort"] == "small"
        assert data["requires_human"] is False
        assert data["parent"] == "FEAT-002"
        assert data["tags"] == ["deps"]

    def test_not_found(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("show", "NOPE-999", "--json", docs_root=docs_root)


# -- board --json --


class TestBoardJson:
    def test_returns_array(self, docs_root, capsys):
        data = run_json("board", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, list)

    def test_only_board_statuses(self, docs_root, capsys):
        data = run_json("board", "--json", docs_root=docs_root, capsys=capsys)
        statuses = {t["effective_status"] for t in data}
        assert statuses <= {"blocked", "open", "in-progress", "review"}

    def test_contains_blocked_ticket(self, docs_root, capsys):
        data = run_json("board", "--json", docs_root=docs_root, capsys=capsys)
        assert any(t["id"] == "TASK-001" and t["effective_status"] == "blocked" for t in data)

    def test_excludes_complete(self, docs_root, capsys):
        data = run_json("board", "--json", docs_root=docs_root, capsys=capsys)
        assert not any(t["effective_status"] in ("complete", "closed", "draft") for t in data)


# -- backlog --json --


class TestBacklogJson:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_returns_planned_and_draft(self, mock_today, docs_root, capsys):
        # Create a draft ticket
        run_cli("create", "feature", "Draft thing", docs_root=docs_root)
        capsys.readouterr()  # clear create output
        data = run_json("backlog", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, list)
        statuses = {t["status"] for t in data}
        assert statuses <= {"planned", "draft"}
        assert any(t["title"] == "Draft thing" for t in data)

    def test_empty_backlog(self, docs_root, capsys):
        # No planned/draft tickets in fixtures by default
        data = run_json("backlog", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, list)


# -- blocker list --json --


class TestBlockerListJson:
    def test_returns_object(self, docs_root, capsys):
        data = run_json("blocker", "list", "TASK-001", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, dict)
        assert data["id"] == "TASK-001"

    def test_blocker_details(self, docs_root, capsys):
        data = run_json("blocker", "list", "TASK-001", "--json", docs_root=docs_root, capsys=capsys)
        assert len(data["blockers"]) == 2
        for b in data["blockers"]:
            assert "id" in b
            assert "status" in b
            assert "title" in b
            assert "resolved" in b

    def test_resolved_flags(self, docs_root, capsys):
        data = run_json("blocker", "list", "TASK-001", "--json", docs_root=docs_root, capsys=capsys)
        resolved = {b["id"]: b["resolved"] for b in data["blockers"]}
        assert resolved["FEAT-001"] is True  # complete
        assert resolved["FEAT-002"] is False  # in-progress

    def test_no_blockers(self, docs_root, capsys):
        data = run_json("blocker", "list", "FEAT-001", "--json", docs_root=docs_root, capsys=capsys)
        assert data["blockers"] == []


# -- project --json --


class TestProjectJson:
    def test_returns_object(self, docs_root, capsys):
        data = run_json("project", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, dict)

    def test_paths(self, docs_root, capsys):
        data = run_json("project", "--json", docs_root=docs_root, capsys=capsys)
        assert data["llpm_root"] == str(docs_root)
        assert data["tickets_dir"] == str(docs_root / "tickets")
        assert data["archive_dir"] == str(docs_root / "tickets" / "archive")

    def test_enums(self, docs_root, capsys):
        data = run_json("project", "--json", docs_root=docs_root, capsys=capsys)
        assert "open" in data["valid_statuses"]
        assert "complete" in data["resolved_statuses"]
        assert "feature" in data["valid_types"]
        assert "high" in data["valid_priorities"]
        assert "large" in data["valid_efforts"]

    def test_counts(self, docs_root, capsys):
        data = run_json("project", "--json", docs_root=docs_root, capsys=capsys)
        counts = data["counts"]
        assert counts["total"] == 6  # 5 active + 1 archived
        assert isinstance(counts["by_status"], dict)
        assert isinstance(counts["by_type"], dict)

    def test_text_output(self, docs_root, capsys):
        run_cli("project", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "LLPM Root:" in out
        assert "By status:" in out
        assert "By type:" in out


# -- todo --json --


class TestTodoJson:
    def test_add_json(self, docs_root, capsys):
        data = run_json("todo", "--add", "Fix bug", "--json", docs_root=docs_root, capsys=capsys)
        assert data["id"] == 1
        assert data["text"] == "Fix bug"

    def test_list_json(self, docs_root, capsys):
        run_cli("todo", "--add", "First", docs_root=docs_root)
        run_cli("todo", "--add", "Second", docs_root=docs_root)
        capsys.readouterr()
        data = run_json("todo", "--list", "--json", docs_root=docs_root, capsys=capsys)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0] == {"id": 1, "text": "First"}
        assert data[1] == {"id": 2, "text": "Second"}

    def test_list_empty_json(self, docs_root, capsys):
        data = run_json("todo", "--list", "--json", docs_root=docs_root, capsys=capsys)
        assert data == []

    def test_rm_json(self, docs_root, capsys):
        run_cli("todo", "--add", "Remove me", docs_root=docs_root)
        capsys.readouterr()
        data = run_json("todo", "--rm", "1", "--json", docs_root=docs_root, capsys=capsys)
        assert data["id"] == 1
        assert data["text"] == "Remove me"
        assert data["removed"] is True

    def test_text_unchanged(self, docs_root, capsys):
        run_cli("todo", "--add", "Hello", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "(1) Hello" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# -- text output unchanged --


class TestTextUnchanged:
    """Verify --json doesn't affect default text output."""

    def test_list_text_unchanged(self, docs_root, capsys):
        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "ID" in out
        assert "EPIC-001" in out
        # Not JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_show_text_unchanged(self, docs_root, capsys):
        run_cli("show", "FEAT-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "ID:" in out
        assert "## Problem" in out

    def test_board_text_unchanged(self, docs_root, capsys):
        run_cli("board", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert "OPEN" in out
