"""Tests for llpm.parser module."""

import datetime
from pathlib import Path

import pytest
import yaml

from llpm import parser


class TestParseDocument:
    def test_parse_feature(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, body = parser.parse_document(path)
        assert fm["id"] == "FEAT-001"
        assert fm["type"] == "feature"
        assert fm["status"] == "complete"
        assert "## Problem" in body

    def test_parse_epic(self, docs_root):
        path = docs_root / "tickets" / "EPIC-001_CLI_TOOLING.md"
        fm, body = parser.parse_document(path)
        assert fm["id"] == "EPIC-001"
        assert fm["type"] == "epic"

    def test_parse_task(self, docs_root):
        path = docs_root / "tickets" / "TASK-001_ADD_PYYAML.md"
        fm, body = parser.parse_document(path)
        assert fm["id"] == "TASK-001"
        assert fm["blockers"] == ["FEAT-001", "FEAT-002"]

    def test_parse_research(self, docs_root):
        path = docs_root / "tickets" / "RESEARCH-001_YAML_LIBRARIES.md"
        fm, body = parser.parse_document(path)
        assert fm["id"] == "RESEARCH-001"
        assert fm["type"] == "research"

    def test_date_normalization(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        # Dates should be strings, not datetime.date
        assert isinstance(fm["created"], str)
        assert fm["created"] == "2026-03-15"

    def test_null_dates(self, docs_root):
        path = docs_root / "tickets" / "FEAT-002_DOC_PARSING.md"
        fm, _ = parser.parse_document(path)
        assert fm["completed"] is None

    def test_body_preserved(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, body = parser.parse_document(path)
        assert "## Problem" in body
        assert "## Solution" in body

    def test_no_frontmatter(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_text("No frontmatter here")
        with pytest.raises(ValueError, match="No frontmatter"):
            parser.parse_document(p)

    def test_unterminated_frontmatter(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_text("---\nid: test\n")
        with pytest.raises(ValueError, match="Unterminated"):
            parser.parse_document(p)


class TestWriteDocument:
    def test_roundtrip(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, body = parser.parse_document(path)
        parser.write_document(path, fm, body)
        fm2, body2 = parser.parse_document(path)
        assert fm2["id"] == fm["id"]
        assert fm2["status"] == fm["status"]
        assert "## Problem" in body2


class TestValidation:
    def test_valid(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        errors = parser.validate_frontmatter(fm)
        assert errors == []

    def test_missing_field(self):
        fm = {"id": "FEAT-001", "type": "feature"}
        errors = parser.validate_frontmatter(fm)
        assert any("Missing required field" in e for e in errors)

    def test_invalid_status(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["status"] = "bogus"
        errors = parser.validate_frontmatter(fm)
        assert any("Invalid status" in e for e in errors)

    def test_invalid_priority(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["priority"] = "ultra"
        errors = parser.validate_frontmatter(fm)
        assert any("Invalid priority" in e for e in errors)

    def test_invalid_effort(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["effort"] = "enormous"
        errors = parser.validate_frontmatter(fm)
        assert any("Invalid effort" in e for e in errors)

    def test_id_prefix_mismatch(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["id"] = "TASK-001"  # mismatch with type=feature
        errors = parser.validate_frontmatter(fm)
        assert any("does not match type" in e for e in errors)


class TestTicketDiscovery:
    def test_find_all(self, docs_root):
        tickets = parser.find_tickets(docs_root, include_archive=True)
        # 5 active + 1 archived
        assert len(tickets) == 6

    def test_exclude_archive(self, docs_root):
        tickets = parser.find_tickets(docs_root, include_archive=False)
        assert len(tickets) == 5
        # Ensure none are from the archive subdirectory
        archive_dir = docs_root / "tickets" / "archive"
        assert all(not str(t).startswith(str(archive_dir)) for t in tickets)

    def test_empty_dir(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        assert parser.find_tickets(docs) == []

    def test_find_by_id(self, docs_root):
        path = parser.find_ticket_by_id(docs_root, "FEAT-001")
        assert path is not None
        assert "FEAT-001" in path.name

    def test_find_by_id_case_insensitive(self, docs_root):
        path = parser.find_ticket_by_id(docs_root, "feat-001")
        assert path is not None

    def test_find_by_id_archived(self, docs_root):
        path = parser.find_ticket_by_id(docs_root, "FEAT-000")
        assert path is not None
        assert "archive" in str(path)

    def test_find_by_id_not_found(self, docs_root):
        assert parser.find_ticket_by_id(docs_root, "NOPE-999") is None


class TestNextId:
    def test_next_feature(self, docs_root):
        # FEAT-000 (archive), FEAT-001, FEAT-002 exist -> next is FEAT-003
        assert parser.next_id(docs_root, "feature") == "FEAT-003"

    def test_next_task(self, docs_root):
        # TASK-001 exists -> next is TASK-002
        assert parser.next_id(docs_root, "task") == "TASK-002"

    def test_next_epic(self, docs_root):
        assert parser.next_id(docs_root, "epic") == "EPIC-002"

    def test_first_of_type(self, docs_root):
        # No BUG tickets exist
        assert parser.next_id(docs_root, "bug") == "BUG-001"


class TestLoadAllTickets:
    def test_load_all(self, docs_root):
        tickets = parser.load_all_tickets(docs_root, include_archive=True)
        assert len(tickets) == 6

    def test_load_active_only(self, docs_root):
        tickets = parser.load_all_tickets(docs_root, include_archive=False)
        assert len(tickets) == 5


class TestBlockerResolution:
    def test_is_blocked(self, docs_root):
        path = docs_root / "tickets" / "TASK-001_ADD_PYYAML.md"
        fm, _ = parser.parse_document(path)
        # FEAT-001 is complete (resolved), FEAT-002 is in-progress (blocking)
        assert parser.is_blocked(docs_root, fm) is True

    def test_not_blocked(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        assert parser.is_blocked(docs_root, fm) is False

    def test_effective_status_blocked(self, docs_root):
        path = docs_root / "tickets" / "TASK-001_ADD_PYYAML.md"
        fm, _ = parser.parse_document(path)
        assert parser.effective_status(docs_root, fm) == "blocked"

    def test_effective_status_not_overridden_when_complete(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        assert parser.effective_status(docs_root, fm) == "complete"

    def test_blocker_details(self, docs_root):
        path = docs_root / "tickets" / "TASK-001_ADD_PYYAML.md"
        fm, _ = parser.parse_document(path)
        details = parser.get_blocker_details(docs_root, fm)
        assert len(details) == 2
        resolved = [d for d in details if d["resolved"]]
        blocking = [d for d in details if not d["resolved"]]
        assert len(resolved) == 1  # FEAT-001
        assert len(blocking) == 1  # FEAT-002


class TestDerivedChildren:
    def test_children_of_epic(self, docs_root):
        children = parser.get_children(docs_root, "EPIC-001")
        child_ids = {c["id"] for c in children}
        assert "FEAT-001" in child_ids
        assert "FEAT-002" in child_ids

    def test_children_of_feature(self, docs_root):
        children = parser.get_children(docs_root, "FEAT-002")
        child_ids = {c["id"] for c in children}
        assert "TASK-001" in child_ids
        assert "RESEARCH-001" in child_ids

    def test_no_children(self, docs_root):
        children = parser.get_children(docs_root, "TASK-001")
        assert children == []
