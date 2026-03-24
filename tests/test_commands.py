"""Tests for llpm.commands module (via CLI dispatch)."""

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


class TestInit:
    def test_fresh_init(self, tmp_path, capsys):
        docs = tmp_path / "docs"
        run_cli("init", docs_root=docs)
        out = capsys.readouterr().out
        assert "Initialized" in out
        assert (docs / "tickets").exists()
        assert (docs / "tickets" / "archive").exists()
        assert (docs / "templates" / "feature.md").exists()
        assert (docs / "templates" / "task.md").exists()
        assert (docs / "templates" / "epic.md").exists()
        assert (docs / "templates" / "research.md").exists()

    def test_already_initialized(self, docs_root, capsys):
        run_cli("init", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Already initialized" in out


class TestList:
    def test_list_all(self, docs_root, capsys):
        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "EPIC-001" in out
        assert "FEAT-001" in out
        assert "TASK-001" in out

    def test_list_filter_status(self, docs_root, capsys):
        run_cli("list", "--status", "blocked", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "TASK-001" in out
        assert "FEAT-001" not in out

    def test_list_filter_type(self, docs_root, capsys):
        run_cli("list", "--type", "epic", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "EPIC-001" in out
        assert "FEAT-001" not in out

    def test_list_filter_parent(self, docs_root, capsys):
        run_cli("list", "--parent", "EPIC-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "FEAT-001" in out
        assert "FEAT-002" in out
        assert "EPIC-001" not in out

    def test_list_derived_blocked(self, docs_root, capsys):
        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        # TASK-001 should show as blocked (has unresolved blocker FEAT-002)
        lines = [l for l in out.splitlines() if "TASK-001" in l]
        assert len(lines) == 1
        assert "blocked" in lines[0]

    def test_list_empty(self, tmp_path, capsys):
        docs = tmp_path / "docs"
        (docs / "tickets").mkdir(parents=True)
        run_cli("list", docs_root=docs)
        out = capsys.readouterr().out
        assert "No tickets found" in out


class TestShow:
    def test_show_normal(self, docs_root, capsys):
        run_cli("show", "FEAT-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "FEAT-001" in out
        assert "complete" in out
        assert "## Problem" in out

    def test_show_blocked(self, docs_root, capsys):
        run_cli("show", "TASK-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "blocked" in out
        assert "[BLOCKING]" in out
        assert "[RESOLVED]" in out

    def test_show_children(self, docs_root, capsys):
        run_cli("show", "EPIC-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "derived" in out
        assert "FEAT-001" in out

    def test_show_not_found(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("show", "NOPE-999", docs_root=docs_root)


class TestCreate:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_draft(self, mock_today, docs_root, capsys):
        run_cli("create", "feature", "New Feature", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "FEAT-003" in out
        # Verify file
        path = parser.find_ticket_by_id(docs_root, "FEAT-003")
        assert path is not None
        fm, body = parser.parse_document(path)
        assert fm["status"] == "draft"
        assert fm["created"] == "2026-03-20"
        assert "## Problem" in body  # template body preserved

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_with_body(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Do thing", "--body", "Custom body here", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "TASK-002" in out
        path = parser.find_ticket_by_id(docs_root, "TASK-002")
        fm, body = parser.parse_document(path)
        assert fm["status"] == "open"
        assert "Custom body here" in body

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_with_body_file(self, mock_today, docs_root, tmp_path, capsys):
        body_file = tmp_path / "spec.md"
        body_file.write_text("## Spec\n\nDetailed spec here.")
        run_cli("create", "feature", "From file", "--body-file", str(body_file), docs_root=docs_root)
        path = parser.find_ticket_by_id(docs_root, "FEAT-003")
        fm, body = parser.parse_document(path)
        assert fm["status"] == "open"
        assert "Detailed spec here" in body

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_with_parent(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Sub task", "--parent", "EPIC-001", docs_root=docs_root)
        path = parser.find_ticket_by_id(docs_root, "TASK-002")
        fm, _ = parser.parse_document(path)
        assert fm["parent"] == "EPIC-001"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_with_priority_and_tags(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Tagged task", "--priority", "high", "--tags", "auth,security", docs_root=docs_root)
        path = parser.find_ticket_by_id(docs_root, "TASK-002")
        fm, _ = parser.parse_document(path)
        assert fm["priority"] == "high"
        assert fm["tags"] == ["auth", "security"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_with_effort(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Effortful", "--effort", "xlarge", docs_root=docs_root)
        path = parser.find_ticket_by_id(docs_root, "TASK-002")
        fm, _ = parser.parse_document(path)
        assert fm["effort"] == "xlarge"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_requires_human(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Get API key", "--requires-human", docs_root=docs_root)
        path = parser.find_ticket_by_id(docs_root, "TASK-002")
        fm, _ = parser.parse_document(path)
        assert fm["requires_human"] is True

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_create_first_of_type(self, mock_today, docs_root, capsys):
        # Create a custom template
        tmpl = docs_root / "templates" / "bug.md"
        feat_tmpl = (docs_root / "templates" / "feature.md").read_text()
        tmpl.write_text(feat_tmpl.replace("type: feature", "type: bug"))
        run_cli("create", "bug", "First Bug", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "BUG-001" in out

    def test_create_invalid_parent(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("create", "task", "Orphan", "--parent", "FAKE-999", docs_root=docs_root)

    def test_create_missing_template(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("create", "nonexistent", "Won't work", docs_root=docs_root)


class TestStatus:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_update_status(self, mock_today, docs_root, capsys):
        run_cli("status", "FEAT-002", "review", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "in-progress -> review" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["status"] == "review"
        assert fm["updated"] == "2026-03-20"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_complete_sets_date(self, mock_today, docs_root, capsys):
        run_cli("status", "FEAT-002", "complete", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["completed"] == "2026-03-20"

    def test_not_found(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("status", "NOPE-999", "open", docs_root=docs_root)

    def test_cannot_set_blocked(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("status", "FEAT-001", "blocked", docs_root=docs_root)


class TestBlocker:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_add(self, mock_today, docs_root, capsys):
        run_cli("blocker", "add", "FEAT-002", "--blocked-by", "RESEARCH-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "now blocked by" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert "RESEARCH-001" in fm["blockers"]

    def test_add_duplicate(self, docs_root, capsys):
        run_cli("blocker", "add", "TASK-001", "--blocked-by", "FEAT-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "already blocked by" in out

    def test_add_nonexistent_blocker(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("blocker", "add", "FEAT-002", "--blocked-by", "FAKE-999", docs_root=docs_root)

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_rm(self, mock_today, docs_root, capsys):
        run_cli("blocker", "rm", "TASK-001", "--blocked-by", "FEAT-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "removed blocker" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "TASK-001_ADD_PYYAML.md")
        assert "FEAT-001" not in fm["blockers"]

    def test_rm_not_found(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("blocker", "rm", "TASK-001", "--blocked-by", "NOPE-999", docs_root=docs_root)

    def test_list_blockers(self, docs_root, capsys):
        run_cli("blocker", "list", "TASK-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "[RESOLVED]" in out
        assert "[BLOCKING]" in out
        assert "BLOCKED" in out

    def test_list_no_blockers(self, docs_root, capsys):
        run_cli("blocker", "list", "FEAT-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "No blockers" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_resolved_when_complete(self, mock_today, docs_root, capsys):
        # Complete FEAT-002 which blocks TASK-001
        run_cli("status", "FEAT-002", "complete", docs_root=docs_root)
        # Now check TASK-001 blockers
        run_cli("blocker", "list", "TASK-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "all blockers resolved" in out


class TestSet:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_equals_syntax(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-002", "priority=low", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "priority = low" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["priority"] == "low"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_multiple_fields(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-002", "priority=low", "effort=xlarge", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "priority = low" in out
        assert "effort = xlarge" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_tags(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-002", "tags=a,b,c", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["tags"] == ["a", "b", "c"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_set_parent(self, mock_today, docs_root, capsys):
        run_cli("set", "RESEARCH-001", "parent=EPIC-001", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "RESEARCH-001_YAML_LIBRARIES.md")
        assert fm["parent"] == "EPIC-001"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_null(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-001", "parent=null", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md")
        assert fm["parent"] is None

    def test_cannot_set_status(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-001", "status=open", docs_root=docs_root)

    def test_cannot_set_blockers(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-001", "blockers=TASK-001", docs_root=docs_root)

    def test_cannot_set_id(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-001", "id=FEAT-999", docs_root=docs_root)

    def test_invalid_priority(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-001", "priority=ultra", docs_root=docs_root)

    def test_invalid_effort(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-001", "effort=enormous", docs_root=docs_root)

    def test_not_found(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("set", "NOPE-999", "priority=high", docs_root=docs_root)

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_title(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-002", "title=New Title", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["title"] == "New Title"


class TestArchive:
    def test_archive_single(self, docs_root, capsys):
        run_cli("archive", "FEAT-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Archived" in out
        assert not (docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md").exists()
        assert (docs_root / "tickets" / "archive" / "FEAT-001_EXPANDED_FRONTMATTER.md").exists()

    def test_archive_non_closed(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("archive", "FEAT-002", docs_root=docs_root)

    def test_archive_all(self, docs_root, capsys):
        run_cli("archive", "--all", "--yes", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Archived" in out
        # FEAT-001 and RESEARCH-001 are complete
        assert not (docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md").exists()
        assert not (docs_root / "tickets" / "RESEARCH-001_YAML_LIBRARIES.md").exists()


class TestDelete:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_delete_with_cleanup(self, mock_today, docs_root, capsys):
        # FEAT-001 is in TASK-001's blockers and is parent of nothing (but EPIC-001 has it as child by derivation)
        run_cli("delete", "FEAT-001", "--yes", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Deleted" in out
        assert not (docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md").exists()
        # TASK-001 should have FEAT-001 removed from blockers
        fm, _ = parser.parse_document(docs_root / "tickets" / "TASK-001_ADD_PYYAML.md")
        assert "FEAT-001" not in fm["blockers"]

    def test_delete_not_found(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("delete", "NOPE-999", "--yes", docs_root=docs_root)


class TestBoard:
    def test_board_output(self, docs_root, capsys):
        run_cli("board", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert "OPEN" in out
        assert "IN-PROGRESS" in out
        assert "REVIEW" in out
        assert "TASK-001" in out  # should be in BLOCKED


class TestBacklog:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_backlog(self, mock_today, docs_root, capsys):
        # Create a draft ticket first
        run_cli("create", "feature", "Draft thing", docs_root=docs_root)
        run_cli("backlog", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "DRAFT" in out
        assert "Draft thing" in out


class TestTodo:
    def test_add(self, docs_root, capsys):
        run_cli("todo", "--add", "Fix the bug", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "(1) Fix the bug" in out

    def test_ids_increment(self, docs_root, capsys):
        run_cli("todo", "--add", "First", docs_root=docs_root)
        run_cli("todo", "--add", "Second", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "(2) Second" in out

    def test_ids_never_reuse(self, docs_root, capsys):
        run_cli("todo", "--add", "A", docs_root=docs_root)
        run_cli("todo", "--add", "B", docs_root=docs_root)
        run_cli("todo", "--rm", "1", docs_root=docs_root)
        run_cli("todo", "--add", "C", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "(3) C" in out  # not (1)

    def test_list(self, docs_root, capsys):
        run_cli("todo", "--add", "Item one", docs_root=docs_root)
        run_cli("todo", "--add", "Item two", docs_root=docs_root)
        run_cli("todo", "--list", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "TODO (2 items)" in out

    def test_list_short(self, docs_root, capsys):
        run_cli("todo", "--add", "Hello", docs_root=docs_root)
        run_cli("todo", "-l", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "(1) Hello" in out

    def test_empty(self, docs_root, capsys):
        run_cli("todo", "--list", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "empty" in out

    def test_rm(self, docs_root, capsys):
        run_cli("todo", "--add", "Remove me", docs_root=docs_root)
        run_cli("todo", "--rm", "1", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Removed (1)" in out

    def test_rm_not_found(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("todo", "--rm", "99", docs_root=docs_root)

    def test_bare_shows_help(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("todo", docs_root=docs_root)


class TestHelp:
    def test_help(self, capsys):
        run_cli("help")
        out = capsys.readouterr().out
        assert "llpm init" in out
        assert "llpm create" in out

    def test_help_verbose(self, capsys):
        run_cli("help", "--verbose")
        out = capsys.readouterr().out
        assert "llpm init" in out
        # Verbose should have more detailed output
        assert "Initialize LLPM" in out
