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


# The real harvester, captured before the conftest autouse fixture stubs it.
_REAL_HARVEST = commands._harvest_commits


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

    @patch.object(commands, "_today", return_value="2026-08-02")
    def test_create_all_types_include_worklog(self, mock_today, docs_root, capsys):
        """Every bundled ticket type gets a ## Worklog section in the body (TASK-010)."""
        for ttype in ("task", "feature", "epic", "research"):
            run_cli("create", ttype, f"Worklog check {ttype}", docs_root=docs_root)
            out = capsys.readouterr().out
            ticket_id = out.split("Created ", 1)[1].split(":", 1)[0].strip()
            path = parser.find_ticket_by_id(docs_root, ticket_id)
            assert path is not None, f"could not find newly created {ttype} ticket {ticket_id}"
            _, body = parser.parse_document(path)
            assert "## Worklog" in body, f"{ttype} body missing ## Worklog"


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


class TestAwaiting:
    """FEAT-012: awaiting -- review-queue discriminator on 'llpm status'."""

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_set_on_review(self, mock_today, docs_root, capsys):
        run_cli("status", "FEAT-002", "review", "--awaiting", "deploy", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["awaiting"] == "deploy"

    def test_absent_when_not_passed(self, docs_root):
        run_cli("status", "FEAT-002", "review", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert "awaiting" not in fm

    def test_explicit_reviewer_allowed(self, docs_root):
        # Equivalent to absent in effect, but still an allowed explicit value.
        run_cli("status", "FEAT-002", "review", "--awaiting", "reviewer", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["awaiting"] == "reviewer"

    def test_rejected_on_non_review_target(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("status", "FEAT-002", "open", "--awaiting", "deploy", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "only valid when the target status is 'review'" in err

    def test_invalid_enum_rejected(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("status", "FEAT-002", "review", "--awaiting", "bogus", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "Invalid awaiting 'bogus'" in err
        for value in ("reviewer", "push", "deploy", "human-verify", "human-answer"):
            assert value in err

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_self_clears_on_next_transition(self, mock_today, docs_root, capsys):
        run_cli("status", "FEAT-002", "review", "--awaiting", "deploy", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["awaiting"] == "deploy"

        run_cli("status", "FEAT-002", "complete", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert "awaiting" not in fm

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_reenters_review_without_awaiting_clears_it(self, mock_today, docs_root, capsys):
        # Re-flipping into review WITHOUT --awaiting must not carry the old value forward.
        run_cli("status", "FEAT-002", "review", "--awaiting", "deploy", docs_root=docs_root)
        run_cli("status", "FEAT-002", "in-progress", docs_root=docs_root)
        run_cli("status", "FEAT-002", "review", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert "awaiting" not in fm

    def test_set_command_redirects(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-002", "awaiting=deploy", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "llpm status" in err

    def test_show_prints_awaiting_line(self, docs_root, capsys):
        run_cli("status", "FEAT-002", "review", "--awaiting", "human-verify", docs_root=docs_root)
        capsys.readouterr()
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Awaiting:  human-verify" in out

    def test_show_omits_awaiting_line_when_absent(self, docs_root, capsys):
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Awaiting:" not in out

    def test_board_chip_on_review_entry(self, docs_root, capsys):
        run_cli("status", "FEAT-002", "review", "--awaiting", "deploy", docs_root=docs_root)
        capsys.readouterr()
        run_cli("board", docs_root=docs_root)
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if "FEAT-002" in l]
        assert len(lines) == 1
        assert "[awaiting: deploy]" in lines[0]

    def test_board_no_chip_without_awaiting(self, docs_root, capsys):
        run_cli("status", "FEAT-002", "review", docs_root=docs_root)
        capsys.readouterr()
        run_cli("board", docs_root=docs_root)
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if "FEAT-002" in l]
        assert len(lines) == 1
        assert "[awaiting:" not in lines[0]


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


class TestProvenanceCreate:
    """FEAT-007: origin/created_by/managed_by/commits injected at create."""

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_defaults_human(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Plain human task", docs_root=docs_root)
        path = parser.find_ticket_by_id(docs_root, "TASK-002")
        fm, _ = parser.parse_document(path)
        assert fm["origin"] == "human"
        assert fm["managed_by"] == "llpm"
        assert fm["commits"] == []
        assert "created_by" not in fm

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_agent_flags(self, mock_today, docs_root, capsys):
        # --triage: FEAT-011 requires agent-origin tickets to attach to a
        # goal or explicitly triage; unrelated to what this test checks.
        run_cli("create", "task", "Agent task", "--origin", "agent",
                "--created-by", "session-abc123", "--triage", docs_root=docs_root)
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "TASK-002"))
        assert fm["origin"] == "agent"
        assert fm["created_by"] == "session-abc123"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_env_created_by_infers_agent(self, mock_today, docs_root, capsys, monkeypatch):
        monkeypatch.setenv("LLPM_CREATED_BY", "session-env-9")
        run_cli("create", "task", "Env agent task", "--triage", docs_root=docs_root)
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "TASK-002"))
        assert fm["origin"] == "agent"
        assert fm["created_by"] == "session-env-9"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_explicit_origin_beats_inference(self, mock_today, docs_root, capsys, monkeypatch):
        monkeypatch.setenv("LLPM_ORIGIN", "human")
        monkeypatch.setenv("LLPM_CREATED_BY", "bens-shell")
        run_cli("create", "task", "Attributed human task", docs_root=docs_root)
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "TASK-002"))
        assert fm["origin"] == "human"
        assert fm["created_by"] == "bens-shell"

    def test_invalid_env_origin_errors(self, docs_root, capsys, monkeypatch):
        monkeypatch.setenv("LLPM_ORIGIN", "robot")
        with pytest.raises(SystemExit):
            run_cli("create", "task", "Bad origin", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "Invalid origin 'robot'" in err

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_template_comments_survive_injection(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Commented task", docs_root=docs_root)
        text = parser.find_ticket_by_id(docs_root, "TASK-002").read_text(encoding="utf-8")
        assert "# draft | planned" in text  # enum-hint comment intact
        assert "managed_by: llpm" in text

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_created_ticket_validates(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Valid task", "--created-by", "s-1", "--triage", docs_root=docs_root)
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "TASK-002"))
        assert parser.validate_frontmatter(fm) == []


class TestProvenanceMutation:
    """FEAT-007: managed_by stamped on mutate; provenance locked from set."""

    @pytest.mark.parametrize("field", ["origin", "created_by", "commits", "managed_by"])
    def test_set_forbids_provenance_fields(self, docs_root, field):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-002", f"{field}=x", docs_root=docs_root)

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_mutation_stamps_managed_by(self, mock_today, docs_root, capsys):
        # Fixture tickets predate managed_by; any mutation adds it.
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert "managed_by" not in fm
        run_cli("set", "FEAT-002", "priority=low", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["managed_by"] == "llpm"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_status_flip_stamps_managed_by(self, mock_today, docs_root, capsys):
        run_cli("status", "TASK-001", "in-progress", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "TASK-001_ADD_PYYAML.md")
        assert fm["managed_by"] == "llpm"


class TestCommitCapture:
    """FEAT-007: commits[] captured at review/complete + explicit --commit."""

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_review_harvests(self, mock_today, docs_root, capsys, monkeypatch):
        sha = "a" * 40
        monkeypatch.setattr(commands, "_harvest_commits", lambda tid: [sha])
        run_cli("status", "FEAT-002", "review", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Captured 1 commit(s)" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["commits"] == [sha]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_explicit_commit_any_status(self, mock_today, docs_root, capsys):
        run_cli("status", "TASK-001", "in-progress", "--commit", "abc1234", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "TASK-001_ADD_PYYAML.md")
        assert fm["commits"] == ["abc1234"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_no_harvest_outside_review_complete(self, mock_today, docs_root, capsys, monkeypatch):
        calls = []
        monkeypatch.setattr(commands, "_harvest_commits", lambda tid: (calls.append(tid), [])[1])
        run_cli("status", "TASK-001", "in-progress", docs_root=docs_root)
        assert calls == []
        run_cli("status", "TASK-001", "review", docs_root=docs_root)
        assert calls == ["TASK-001"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_prefix_dedup(self, mock_today, docs_root, capsys, monkeypatch):
        full = "b" * 40
        monkeypatch.setattr(commands, "_harvest_commits", lambda tid: [full])
        run_cli("status", "FEAT-002", "review", docs_root=docs_root)
        # Re-flip with a short prefix of the same sha: no duplicate
        run_cli("status", "FEAT-002", "complete", "--commit", full[:8], docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["commits"] == [full]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_harvest_from_real_git_repo(self, mock_today, docs_root, tmp_path, capsys, monkeypatch):
        import subprocess
        repo = tmp_path / "workrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "--allow-empty", "-q", "-m", "feat: TASK-001 add pyyaml dep"],
            cwd=repo, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.strip()

        monkeypatch.chdir(repo)
        monkeypatch.setattr(commands, "_harvest_commits", _REAL_HARVEST)
        run_cli("status", "TASK-001", "complete", docs_root=docs_root)

        fm, _ = parser.parse_document(docs_root / "tickets" / "TASK-001_ADD_PYYAML.md")
        assert fm["commits"] == [sha]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_show_displays_commits(self, mock_today, docs_root, capsys):
        run_cli("status", "FEAT-002", "review", "--commit", "c" * 40, docs_root=docs_root)
        capsys.readouterr()
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Commits:   cccccccccc" in out


class TestPrioritySort:
    """list/board order by priority (high->low), then ID (TASK-006)."""

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_list_sorted_by_priority(self, mock_today, docs_root, capsys):
        run_cli("set", "TASK-001", "priority=low", docs_root=docs_root)
        capsys.readouterr()
        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        ids = [l.split()[0] for l in out.splitlines()[2:] if l.strip()]
        # high: EPIC-001, FEAT-001, FEAT-002 | medium: RESEARCH-001 | low: TASK-001
        assert ids == ["EPIC-001", "FEAT-001", "FEAT-002", "RESEARCH-001", "TASK-001"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_board_column_sorted_by_priority(self, mock_today, docs_root, capsys):
        # EPIC-001 and FEAT-002 are both in-progress; by ID alone EPIC-001
        # sorts first, so dropping its priority must flip the order.
        run_cli("set", "EPIC-001", "priority=low", docs_root=docs_root)
        capsys.readouterr()
        run_cli("board", docs_root=docs_root)
        out = capsys.readouterr().out
        lines = out.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("-- IN-PROGRESS"))
        section = []
        for l in lines[start + 1:]:
            if l.startswith("--"):
                break
            if l.strip():
                section.append(l)
        assert "FEAT-002" in section[0]
        assert "EPIC-001" in section[1]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_list_json_sorted(self, mock_today, docs_root, capsys):
        import json as _json
        run_cli("set", "TASK-001", "priority=low", docs_root=docs_root)
        capsys.readouterr()
        run_cli("list", "--json", docs_root=docs_root)
        data = _json.loads(capsys.readouterr().out)
        ids = [t["id"] for t in data]
        assert ids == ["EPIC-001", "FEAT-001", "FEAT-002", "RESEARCH-001", "TASK-001"]


class TestServes:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_serves_add_feature(self, mock_today, docs_root, capsys):
        run_cli("serves", "add", "FEAT-002", "goals.unified-agent-platform", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "now serves 'goals.unified-agent-platform'" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["serves"] == ["goals.unified-agent-platform"]
        assert fm["updated"] == "2026-03-20"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_serves_add_epic(self, mock_today, docs_root, capsys):
        run_cli("serves", "add", "EPIC-001", "prj.marginalia.versions.v1", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "EPIC-001_CLI_TOOLING.md")
        assert fm["serves"] == ["prj.marginalia.versions.v1"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_serves_add_duplicate(self, mock_today, docs_root, capsys):
        run_cli("serves", "add", "FEAT-002", "goals.a", docs_root=docs_root)
        run_cli("serves", "add", "FEAT-002", "goals.a", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "already serves" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["serves"] == ["goals.a"]

    def test_serves_add_task_rejected(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("serves", "add", "TASK-001", "goals.a", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "only valid on epics/features" in err

    def test_serves_add_ticket_id_rejected(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("serves", "add", "FEAT-002", "FEAT-001", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "full vault stems" in err

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_serves_rm(self, mock_today, docs_root, capsys):
        run_cli("serves", "add", "FEAT-002", "goals.a", docs_root=docs_root)
        run_cli("serves", "add", "FEAT-002", "goals.b", docs_root=docs_root)
        run_cli("serves", "rm", "FEAT-002", "goals.a", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "no longer serves 'goals.a'" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["serves"] == ["goals.b"]

    def test_serves_rm_not_present(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("serves", "rm", "FEAT-002", "goals.nope", docs_root=docs_root)

    def test_cannot_set_serves_via_set(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-002", "serves=goals.a", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "llpm serves" in err

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_show_displays_serves(self, mock_today, docs_root, capsys):
        run_cli("serves", "add", "FEAT-002", "goals.a", docs_root=docs_root)
        capsys.readouterr()
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Serves:    goals.a" in out

    def test_show_feature_without_serves_shows_dash(self, docs_root, capsys):
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Serves:    -" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_board_shows_serves(self, mock_today, docs_root, capsys):
        # FEAT-002 is in-progress, so it appears on the board
        run_cli("serves", "add", "FEAT-002", "goals.a", docs_root=docs_root)
        capsys.readouterr()
        run_cli("board", docs_root=docs_root)
        out = capsys.readouterr().out
        line = next(l for l in out.splitlines() if "FEAT-002" in l)
        assert "(serves: goals.a)" in line


class TestWaitsLocalStore:
    """waits_on on a local-dir store: mutations work, cross-board status is
    unknown, and unknown NEVER blocks (graceful offline degradation)."""

    STEM = "repos.marginalia.llpm.features.FEAT-010"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_waits_add_unknown_status(self, mock_today, docs_root, capsys):
        run_cli("waits", "add", "FEAT-002", "--on", self.STEM, docs_root=docs_root)
        out = capsys.readouterr().out
        assert f"now waits on '{self.STEM}'" in out
        assert "unknown from this store" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["waits_on"] == [self.STEM]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_unavailable_does_not_block(self, mock_today, docs_root, capsys):
        run_cli("waits", "add", "FEAT-002", "--on", self.STEM, docs_root=docs_root)
        capsys.readouterr()
        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        line = next(l for l in out.splitlines() if "FEAT-002" in l)
        assert "in-progress" in line  # stored status, not 'blocked'

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_waits_list_unknown(self, mock_today, docs_root, capsys):
        run_cli("waits", "add", "FEAT-002", "--on", self.STEM, docs_root=docs_root)
        capsys.readouterr()
        run_cli("waits", "list", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "[UNKNOWN]" in out
        assert "not blocking" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_show_surfaces_waits(self, mock_today, docs_root, capsys):
        run_cli("waits", "add", "FEAT-002", "--on", self.STEM, docs_root=docs_root)
        capsys.readouterr()
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert f"Waits on:  {self.STEM} (unavailable) [UNKNOWN]" in out

    def test_waits_list_none(self, docs_root, capsys):
        run_cli("waits", "list", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "No cross-board waits" in out


class TestAfter:
    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_after_add(self, mock_today, docs_root, capsys):
        run_cli("after", "add", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "now ordered after 'RESEARCH-001'" in out
        assert "never blocks" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["after"] == ["RESEARCH-001"]
        assert fm["updated"] == "2026-03-20"

    def test_after_add_nonexistent(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("after", "add", "FEAT-002", "--after", "NOPE-999", docs_root=docs_root)

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_after_add_duplicate(self, mock_today, docs_root, capsys):
        run_cli("after", "add", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)
        run_cli("after", "add", "FEAT-002", "--after", "research-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "already ordered after" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["after"] == ["RESEARCH-001"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_after_never_blocks(self, mock_today, docs_root, capsys):
        # RESEARCH-001 is open (unresolved); a hard blocker would flip FEAT-002
        # to blocked -- 'after' must not.
        run_cli("after", "add", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)
        capsys.readouterr()
        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        line = next(l for l in out.splitlines() if "FEAT-002" in l)
        assert "in-progress" in line
        assert "blocked" not in line

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_after_cycle_warns_but_adds(self, mock_today, docs_root, capsys):
        run_cli("after", "add", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)
        capsys.readouterr()
        run_cli("after", "add", "RESEARCH-001", "--after", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Warning: soft ordering cycle" in out
        # Edge still added -- soft cycles are allowed
        fm, _ = parser.parse_document(docs_root / "tickets" / "RESEARCH-001_YAML_LIBRARIES.md")
        assert fm["after"] == ["FEAT-002"]

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_after_self_cycle_warns(self, mock_today, docs_root, capsys):
        run_cli("after", "add", "FEAT-002", "--after", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Warning: soft ordering cycle" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_after_rm(self, mock_today, docs_root, capsys):
        run_cli("after", "add", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)
        run_cli("after", "rm", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "no longer ordered after" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["after"] == []

    def test_after_rm_not_present(self, docs_root):
        with pytest.raises(SystemExit):
            run_cli("after", "rm", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)

    def test_cannot_set_after_via_set(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-002", "after=RESEARCH-001", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "llpm after" in err

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_show_displays_after(self, mock_today, docs_root, capsys):
        run_cli("after", "add", "FEAT-002", "--after", "RESEARCH-001", docs_root=docs_root)
        capsys.readouterr()
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "After:     RESEARCH-001 (soft)" in out


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


class TestStoreDiscovery:
    """Tests for TASK-001: in-repo .llpm/config.toml discovery."""

    def test_config_toml_dir_kind(self, tmp_path):
        """_find_repo_config finds .llpm/config.toml and returns a dict with kind='dir'."""
        # Set up a repo-like dir with .llpm/config.toml
        repo = tmp_path / "myrepo"
        repo.mkdir()
        llpm_dir = repo / ".llpm"
        llpm_dir.mkdir()
        (llpm_dir / "config.toml").write_text(
            '[store]\nkind = "dir"\nroot = "./llpm"\n', encoding="utf-8"
        )

        import os
        original_cwd = os.getcwd()
        try:
            os.chdir(repo)
            result = commands._find_repo_config()
        finally:
            os.chdir(original_cwd)

        assert result is not None
        assert result["kind"] == "dir"
        assert result["docs_root"] == (repo / "llpm").resolve()

    def test_config_toml_custom_root(self, tmp_path):
        """_find_repo_config respects a custom root path."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        llpm_dir = repo / ".llpm"
        llpm_dir.mkdir()
        (llpm_dir / "config.toml").write_text(
            '[store]\nkind = "dir"\nroot = "./custom/tickets-root"\n', encoding="utf-8"
        )

        import os
        original_cwd = os.getcwd()
        try:
            os.chdir(repo)
            result = commands._find_repo_config()
        finally:
            os.chdir(original_cwd)

        assert result is not None
        assert result["docs_root"] == (repo / "custom" / "tickets-root").resolve()

    def test_config_toml_unknown_kind_errors(self, tmp_path):
        """_find_repo_config raises SystemExit for truly unknown store kinds."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        llpm_dir = repo / ".llpm"
        llpm_dir.mkdir()
        (llpm_dir / "config.toml").write_text(
            '[store]\nkind = "s3"\n',
            encoding="utf-8",
        )

        import os
        original_cwd = os.getcwd()
        try:
            os.chdir(repo)
            with pytest.raises(SystemExit):
                commands._find_repo_config()
        finally:
            os.chdir(original_cwd)

    def test_config_toml_walk_upward(self, tmp_path):
        """_find_repo_config walks upward from a nested CWD to find config."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        llpm_dir = repo / ".llpm"
        llpm_dir.mkdir()
        (llpm_dir / "config.toml").write_text(
            '[store]\nkind = "dir"\nroot = "./llpm"\n', encoding="utf-8"
        )
        nested = repo / "src" / "sub"
        nested.mkdir(parents=True)

        import os
        original_cwd = os.getcwd()
        try:
            os.chdir(nested)
            result = commands._find_repo_config()
        finally:
            os.chdir(original_cwd)

        assert result is not None
        # Should find the repo root's config and resolve root relative to it
        assert result["kind"] == "dir"
        assert result["docs_root"] == (repo / "llpm").resolve()

    def test_no_config_toml_returns_none(self, tmp_path):
        """_find_repo_config returns None when no .llpm/config.toml is found."""
        # Use tmp_path with no .llpm dir
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        import os
        original_cwd = os.getcwd()
        try:
            os.chdir(empty_dir)
            result = commands._find_repo_config()
        finally:
            os.chdir(original_cwd)

        assert result is None

    def test_resolve_store_config_flag_overrides_toml(self, tmp_path):
        """--docs-root flag takes priority over .llpm/config.toml."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        llpm_dir = repo / ".llpm"
        llpm_dir.mkdir()
        (llpm_dir / "config.toml").write_text(
            '[store]\nkind = "dir"\nroot = "./from_toml"\n', encoding="utf-8"
        )
        explicit_root = tmp_path / "explicit"

        import os
        import types
        original_cwd = os.getcwd()
        try:
            os.chdir(repo)
            args = types.SimpleNamespace(docs_root=str(explicit_root))
            cfg = commands._resolve_store_config(args)
        finally:
            os.chdir(original_cwd)

        assert cfg["docs_root"] == explicit_root.resolve()
        assert cfg["kind"] == "dir"

    def test_resolve_store_config_env_overrides_toml(self, tmp_path, monkeypatch):
        """LLPM_DOCS_ROOT env var takes priority over .llpm/config.toml."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        llpm_dir = repo / ".llpm"
        llpm_dir.mkdir()
        (llpm_dir / "config.toml").write_text(
            '[store]\nkind = "dir"\nroot = "./from_toml"\n', encoding="utf-8"
        )
        env_root = tmp_path / "from_env"
        monkeypatch.setenv("LLPM_DOCS_ROOT", str(env_root))

        import os
        import types
        original_cwd = os.getcwd()
        try:
            os.chdir(repo)
            args = types.SimpleNamespace(docs_root=None)
            cfg = commands._resolve_store_config(args)
        finally:
            os.chdir(original_cwd)

        assert cfg["docs_root"] == env_root.resolve()
        assert cfg["kind"] == "dir"


class TestModelTierValidation:
    """Tests for TASK-007: model_tier enum validation + board surfacing."""

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_set_valid_tier(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-002", "model_tier=light", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "model_tier = light" in out
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["model_tier"] == "light"

    def test_set_invalid_tier_errors(self, docs_root, capsys):
        with pytest.raises(SystemExit):
            run_cli("set", "FEAT-002", "model_tier=turbo", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "Invalid model_tier 'turbo'" in err
        assert "heavy, light, standard" in err

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_set_null_clears_tier(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-002", "model_tier=standard", docs_root=docs_root)
        run_cli("set", "FEAT-002", "model_tier=null", docs_root=docs_root)
        fm, _ = parser.parse_document(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md")
        assert fm["model_tier"] is None

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_board_shows_tier_chip(self, mock_today, docs_root, capsys):
        # FEAT-002 is in-progress, so it appears on the board
        run_cli("set", "FEAT-002", "model_tier=heavy", docs_root=docs_root)
        capsys.readouterr()
        run_cli("board", docs_root=docs_root)
        out = capsys.readouterr().out
        line = next(l for l in out.splitlines() if "FEAT-002" in l)
        assert "[heavy]" in line

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_show_displays_tier(self, mock_today, docs_root, capsys):
        run_cli("set", "FEAT-002", "model_tier=standard", docs_root=docs_root)
        capsys.readouterr()
        run_cli("show", "FEAT-002", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "Tier:      standard" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_model_tier_in_json(self, mock_today, docs_root, capsys):
        import json as _json
        run_cli("set", "FEAT-002", "model_tier=light", docs_root=docs_root)
        capsys.readouterr()
        run_cli("list", "--json", docs_root=docs_root)
        data = _json.loads(capsys.readouterr().out)
        feat = next(t for t in data if t["id"] == "FEAT-002")
        assert feat["model_tier"] == "light"
        # Absent tier serializes as null, key always present
        epic = next(t for t in data if t["id"] == "EPIC-001")
        assert epic["model_tier"] is None


class TestModelTierDisplay:
    """Tests for TASK-002: model_tier chip in ls/backlog output."""

    def test_list_shows_model_tier_chip(self, docs_root, capsys):
        """ls output includes [tier] chip when model_tier is set."""
        # Add model_tier to TASK-001 fixture (has status=open so shows in list)
        ticket_path = docs_root / "tickets" / "TASK-001_ADD_PYYAML.md"
        content = ticket_path.read_text(encoding="utf-8")
        # Insert model_tier after the tags line
        content = content.replace("tags: [deps]", "tags: [deps]\nmodel_tier: heavy", 1)
        ticket_path.write_text(content, encoding="utf-8")

        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "[heavy]" in out

    def test_list_no_model_tier_no_chip(self, docs_root, capsys):
        """ls output has no tier chip for tickets without model_tier."""
        run_cli("list", docs_root=docs_root)
        out = capsys.readouterr().out
        # None of the fixture tickets have model_tier
        assert "[heavy]" not in out
        assert "[standard]" not in out
        assert "[light]" not in out

    def test_backlog_shows_model_tier_chip(self, docs_root, capsys):
        """backlog output includes [tier] chip when model_tier is set on a planned ticket."""
        # Create a planned ticket with model_tier
        ticket_path = docs_root / "tickets" / "TASK-PLANNED_WITH_TIER.md"
        ticket_path.write_text(
            "---\n"
            'id: "TASK-999"\n'
            "type: task\n"
            'title: "Tier test"\n'
            "status: planned\n"
            "priority: medium\n"
            "effort: null\n"
            "requires_human: false\n"
            "parent: null\n"
            "blockers: []\n"
            'created: "2026-07-04"\n'
            'updated: "2026-07-04"\n'
            "completed: null\n"
            "tags: []\n"
            "model_tier: light\n"
            "---\n\n## Description\n\nTest ticket.\n",
            encoding="utf-8",
        )

        run_cli("backlog", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "[light]" in out

    def test_templates_include_model_tier(self):
        """All four bundled templates include model_tier field."""
        from llpm.commands import _templates_source
        from pathlib import Path

        templates_dir = Path(str(_templates_source()))
        for tmpl_name in ("task.md", "feature.md", "epic.md", "research.md"):
            content = (templates_dir / tmpl_name).read_text(encoding="utf-8")
            assert "model_tier:" in content, f"{tmpl_name} missing model_tier field"

    def test_templates_include_worklog(self):
        """All four bundled templates carry a ## Worklog section with a format hint (TASK-010)."""
        from llpm.commands import _templates_source
        from pathlib import Path

        templates_dir = Path(str(_templates_source()))
        for tmpl_name in ("task.md", "feature.md", "epic.md", "research.md"):
            content = (templates_dir / tmpl_name).read_text(encoding="utf-8")
            assert "## Worklog" in content, f"{tmpl_name} missing ## Worklog section"
            assert content.rstrip().endswith("-->"), f"{tmpl_name} Worklog section should be last"
            # Format-hint comment: entry format + append-only rule
            assert "<agent/session>" in content, f"{tmpl_name} Worklog missing entry-format hint"
            assert "Append-only" in content, f"{tmpl_name} Worklog missing append-only hint"


# ---------------------------------------------------------------------------
# Ticket-intake policy (FEAT-011)
# ---------------------------------------------------------------------------

class TestCreateIntakePolicy:
    """Agent-origin tickets land draft unless auto-approved. Goal attachment
    is NEVER enforced at creation (ruling from Ben+fable, 2026-08-01,
    overriding the original FEAT-011 spec) -- creation always succeeds;
    unattached agent tickets are a pull-based `llpm orphans`/`llpm goals`
    concern instead. Human origin is unaffected either way."""

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_human_origin_bypasses_policy(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Human idea", "--origin", "human", docs_root=docs_root)
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "TASK-002"))
        assert fm["status"] == "draft"

    def test_agent_origin_unattached_still_succeeds(self, docs_root, capsys):
        # No --serves/--parent/--triage -- creation must not fail or warn.
        run_cli("create", "task", "Orphan idea", "--origin", "agent",
                "--created-by", "s-1", docs_root=docs_root)
        out, err = capsys.readouterr()
        assert err == ""
        assert "Created TASK-002" in out
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "TASK-002"))
        assert fm["status"] == "draft"  # draft-by-default still applies

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_agent_origin_triage_tags(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Needs triage", "--origin", "agent",
                "--created-by", "s-1", "--triage", docs_root=docs_root)
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "TASK-002"))
        assert fm["tags"] == ["triage"]
        assert fm["status"] == "draft"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_agent_origin_serves_on_feature_optional(self, mock_today, docs_root, capsys):
        run_cli("create", "feature", "New capability", "--origin", "agent",
                "--created-by", "s-1", "--serves", "goals.my-goal", docs_root=docs_root)
        fm, _ = parser.parse_document(parser.find_ticket_by_id(docs_root, "FEAT-003"))
        assert fm["serves"] == ["goals.my-goal"]

    def test_serves_on_task_rejected(self, docs_root, capsys):
        # Structural rule (serves is epic/feature-only), independent of the
        # (removed) attachment mandate -- still enforced, same as `llpm
        # serves add`.
        with pytest.raises(SystemExit):
            run_cli("create", "task", "Bad", "--serves", "goals.x", docs_root=docs_root)
        err = capsys.readouterr().err
        assert "only valid on epics/features" in err


class TestIntakeAutoApproveConfig:
    """[intake] auto_approve in .llpm/config.toml (FEAT-011: policy-as-data).

    Needs config-file discovery, so these bypass the --docs-root flag (which
    short-circuits _resolve_store_config before it ever reads config.toml)
    and chdir into a fresh project instead, mirroring TestConfigTomlDiscovery
    in test_mdtreestore.py.
    """

    def _init_project(self, tmp_path, monkeypatch, auto_approve):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        approve_toml = ", ".join(f'"{k}"' for k in auto_approve)
        (config_dir / "config.toml").write_text(
            f'[store]\nkind = "dir"\nroot = "./llpm"\n\n'
            f"[intake]\nauto_approve = [{approve_toml}]\n"
        )
        main(["init"])

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_auto_approved_kind_bypasses_draft(self, mock_today, tmp_path, monkeypatch, capsys):
        self._init_project(tmp_path, monkeypatch, ["task"])
        main(["create", "task", "Pre-vetted", "--origin", "agent", "--created-by", "s-1",
              "--triage", "--body", "some body text"])
        fm, _ = parser.parse_document(parser.find_ticket_by_id(tmp_path / "llpm", "TASK-001"))
        assert fm["status"] == "open"  # body given + auto-approved kind

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_non_approved_kind_still_drafts_despite_body(self, mock_today, tmp_path, monkeypatch, capsys):
        self._init_project(tmp_path, monkeypatch, ["research"])
        main(["create", "task", "Not vetted", "--origin", "agent", "--created-by", "s-1",
              "--triage", "--body", "some body text"])
        fm, _ = parser.parse_document(parser.find_ticket_by_id(tmp_path / "llpm", "TASK-001"))
        assert fm["status"] == "draft"  # forced despite body: "task" isn't auto-approved


# ---------------------------------------------------------------------------
# cmd_orphans (FEAT-011)
# ---------------------------------------------------------------------------

class TestOrphans:
    """Default require_goal=warn (no .llpm/config.toml [intake] section)."""

    def test_no_orphans(self, docs_root, capsys):
        run_cli("orphans", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_unattached_agent_ticket_reported(self, mock_today, docs_root, capsys):
        # Creation succeeds unattached (no gate) -- it shows up in the report.
        run_cli("create", "task", "Drifted idea", "--origin", "agent",
                "--created-by", "s-1", docs_root=docs_root)
        capsys.readouterr()  # discard the "Created TASK-002..." output

        run_cli("orphans", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "1 orphaned agent-created ticket(s)" in out
        assert "warn: informational only" in out
        assert "TASK-002" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_triaged_ticket_not_reported(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Needs triage", "--origin", "agent",
                "--created-by", "s-1", "--triage", docs_root=docs_root)
        run_cli("orphans", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_goal_attached_ticket_not_reported(self, mock_today, docs_root, capsys):
        run_cli("create", "feature", "New capability", "--origin", "agent",
                "--created-by", "s-1", "--serves", "goals.my-goal", docs_root=docs_root)
        run_cli("orphans", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out

    def test_human_origin_never_reported(self, docs_root, capsys):
        # FEAT-001/EPIC-001/etc in the fixture are unattached and human-origin
        # (no `origin` field at all) -- never flagged, the policy never gated them.
        run_cli("orphans", docs_root=docs_root)
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_json_output(self, mock_today, docs_root, capsys):
        run_cli("create", "task", "Drifted idea", "--origin", "agent",
                "--created-by", "s-1", docs_root=docs_root)
        capsys.readouterr()  # discard the "Created TASK-002..." output above

        run_cli("orphans", "--json", docs_root=docs_root)

        import json
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["require_goal"] == "warn"
        assert len(data["orphans"]) == 1
        assert data["orphans"][0]["id"] == "TASK-002"
        assert data["orphans"][0]["created_by"] == "s-1"


class TestRequireGoalConfig:
    """[intake] require_goal in .llpm/config.toml (off|warn|enforce).

    Needs config-file discovery (see TestIntakeAutoApproveConfig's note on
    why --docs-root can't be used here)."""

    def _init_project(self, tmp_path, monkeypatch, require_goal=None):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        intake = f'require_goal = "{require_goal}"\n' if require_goal else ""
        (config_dir / "config.toml").write_text(
            f'[store]\nkind = "dir"\nroot = "./llpm"\n\n[intake]\n{intake}'
        )
        main(["init"])

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_default_is_warn(self, mock_today, tmp_path, monkeypatch, capsys):
        self._init_project(tmp_path, monkeypatch)
        main(["create", "task", "Orphan", "--origin", "agent", "--created-by", "s-1"])
        capsys.readouterr()
        main(["orphans"])
        out = capsys.readouterr().out
        assert "warn: informational only" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_off_mutes_report(self, mock_today, tmp_path, monkeypatch, capsys):
        self._init_project(tmp_path, monkeypatch, require_goal="off")
        main(["create", "task", "Orphan", "--origin", "agent", "--created-by", "s-1"])
        capsys.readouterr()
        main(["orphans"])
        out = capsys.readouterr().out
        assert "off for this board" in out
        assert "Orphan" not in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_off_json_shape(self, mock_today, tmp_path, monkeypatch, capsys):
        self._init_project(tmp_path, monkeypatch, require_goal="off")
        main(["create", "task", "Orphan", "--origin", "agent", "--created-by", "s-1"])
        capsys.readouterr()
        main(["orphans", "--json"])

        import json
        data = json.loads(capsys.readouterr().out)
        assert data == {"require_goal": "off", "orphans": []}

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_enforce_labels_report(self, mock_today, tmp_path, monkeypatch, capsys):
        self._init_project(tmp_path, monkeypatch, require_goal="enforce")
        main(["create", "task", "Orphan", "--origin", "agent", "--created-by", "s-1"])
        capsys.readouterr()
        main(["orphans"])
        out = capsys.readouterr().out
        assert "NOT dispatch-eligible" in out

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_enforce_does_not_block_creation(self, mock_today, tmp_path, monkeypatch, capsys):
        # "enforce" gates a future dispatcher, never `llpm create` itself.
        self._init_project(tmp_path, monkeypatch, require_goal="enforce")
        main(["create", "task", "Orphan", "--origin", "agent", "--created-by", "s-1"])
        out, err = capsys.readouterr()
        assert err == ""
        assert "Created" in out

    def test_invalid_value_errors(self, tmp_path, monkeypatch, capsys):
        self._init_project(tmp_path, monkeypatch, require_goal="block")
        with pytest.raises(SystemExit):
            main(["orphans"])
        err = capsys.readouterr().err
        assert "Invalid [intake] require_goal 'block'" in err


class TestIntakeConfigWithStoreOverride:
    """Regression for the FEAT-011 review defect: --docs-root / LLPM_DOCS_ROOT
    override *store location* only. [intake] policy must still come from the
    discovered .llpm/config.toml even when an override wins branch 1/2 of
    _resolve_store_config -- before the fix, those branches returned early
    without ever calling _find_repo_config(), so require_goal/auto_approve
    silently reset to defaults whenever the store came from the flag or env
    var. LLPM_DOCS_ROOT is the box-spawn env contract in the task fabric, so
    this is exactly the path board policy must survive.

    Each project's own [store] root ("./configured_store") deliberately
    differs from the override target ("./override_store") -- a plain dir
    store has no config.toml of its own, so a pass here can only mean intake
    came from the discovered config, not from something at the override path.
    """

    def _write_config(self, tmp_path, monkeypatch, intake_toml):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            f'[store]\nkind = "dir"\nroot = "./configured_store"\n\n'
            f"[intake]\n{intake_toml}"
        )
        return tmp_path / "override_store"

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_docs_root_flag_still_honors_require_goal(self, mock_today, tmp_path, monkeypatch, capsys):
        override_root = self._write_config(tmp_path, monkeypatch, 'require_goal = "enforce"\n')
        main(["--docs-root", str(override_root), "init"])
        main(["--docs-root", str(override_root), "create", "task", "Orphan",
              "--origin", "agent", "--created-by", "s-1"])
        capsys.readouterr()

        main(["--docs-root", str(override_root), "orphans"])
        out = capsys.readouterr().out
        assert "NOT dispatch-eligible" in out  # enforce label, not the default "warn"

        # The override really won for store location (ticket lives under
        # override_store, config's own configured_store was never created).
        assert (override_root / "tickets").exists()
        assert not (tmp_path / "configured_store").exists()

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_env_var_still_honors_require_goal(self, mock_today, tmp_path, monkeypatch, capsys):
        override_root = self._write_config(tmp_path, monkeypatch, 'require_goal = "enforce"\n')
        monkeypatch.setenv("LLPM_DOCS_ROOT", str(override_root))
        main(["init"])
        main(["create", "task", "Orphan", "--origin", "agent", "--created-by", "s-1"])
        capsys.readouterr()

        main(["orphans"])
        out = capsys.readouterr().out
        assert "NOT dispatch-eligible" in out

        assert (override_root / "tickets").exists()
        assert not (tmp_path / "configured_store").exists()

    @patch.object(commands, "_today", return_value="2026-03-20")
    def test_docs_root_flag_still_honors_auto_approve(self, mock_today, tmp_path, monkeypatch, capsys):
        override_root = self._write_config(tmp_path, monkeypatch, 'auto_approve = ["task"]\n')
        main(["--docs-root", str(override_root), "init"])
        main(["--docs-root", str(override_root), "create", "task", "Pre-vetted",
              "--origin", "agent", "--created-by", "s-1", "--body", "some body text"])
        fm, _ = parser.parse_document(parser.find_ticket_by_id(override_root, "TASK-001"))
        assert fm["status"] == "open"  # auto_approve honored despite store override

    def test_override_without_any_config_file_is_unchanged(self, tmp_path, monkeypatch, capsys):
        """No .llpm/config.toml anywhere above CWD -- override branches must
        keep behaving exactly as before the fix: intake resolves to {}."""
        import types

        empty_cwd = tmp_path / "no_config_here"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        override_root = tmp_path / "override_store"

        args = types.SimpleNamespace(docs_root=str(override_root))
        cfg = commands._resolve_store_config(args)
        assert cfg["kind"] == "dir"
        assert cfg["docs_root"] == override_root.resolve()
        assert cfg.get("intake") == {}

        main(["--docs-root", str(override_root), "init"])
        main(["--docs-root", str(override_root), "orphans"])
        out = capsys.readouterr().out
        assert "No orphaned agent-created tickets." in out  # default warn mode, nothing to report yet
