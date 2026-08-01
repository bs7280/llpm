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

    def test_invalid_model_tier(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["model_tier"] = "turbo"
        errors = parser.validate_frontmatter(fm)
        assert any("Invalid model_tier" in e for e in errors)

    def test_valid_model_tier(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["model_tier"] = "light"
        assert parser.validate_frontmatter(fm) == []

    def test_null_model_tier_ok(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["model_tier"] = None
        assert parser.validate_frontmatter(fm) == []

    def test_invalid_origin(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["origin"] = "robot"
        errors = parser.validate_frontmatter(fm)
        assert any("Invalid origin" in e for e in errors)

    def test_valid_origins(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        for origin in ("human", "agent", None):
            fm["origin"] = origin
            assert parser.validate_frontmatter(fm) == []

    def test_commits_must_be_list(self, docs_root):
        path = docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md"
        fm, _ = parser.parse_document(path)
        fm["commits"] = "abc123"
        errors = parser.validate_frontmatter(fm)
        assert any("'commits' must be a list" in e for e in errors)

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


# -- Goals rollup (FEAT-008) --

def _set_fields(path: Path, **fields) -> None:
    fm, body = parser.parse_document(path)
    fm.update(fields)
    parser.write_document(path, fm, body)


def _write_goal(docs_root: Path, filename: str, *, title: str, status: str) -> str:
    """Write a `type: goal` note into tickets/. Returns the identifier
    LocalDirStore.scan_by_type keys it by (filename stem, same as tickets --
    a local dir has no real vault stems)."""
    path = docs_root / "tickets" / filename
    fm = {"id": path.stem, "type": "goal", "title": title, "status": status}
    parser.write_document(path, fm, f"# {title}\n")
    return path.stem


class TestGoalNotesScan:
    def test_finds_goal_notes(self, docs_root):
        _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        stems = {stem for stem, _ in parser.get_goal_notes(docs_root)}
        assert "GOAL-001_MY_GOAL" in stems

    def test_ignores_non_goal_tickets(self, docs_root):
        notes = parser.get_goal_notes(docs_root)
        assert notes == []  # fixture tree has no type: goal tickets


class TestGoalRollup:
    def test_inherits_serves_through_parent_chain(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        _set_fields(docs_root / "tickets" / "EPIC-001_CLI_TOOLING.md", serves=[goal_stem])

        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)

        # EPIC-001 declares serves directly; FEAT-001/FEAT-002 inherit via
        # parent; TASK-001/RESEARCH-001 inherit transitively through FEAT-002.
        serving_ids = {s["id"] for s in goal["serving"]}
        assert serving_ids == {"EPIC-001", "FEAT-001", "FEAT-002", "TASK-001", "RESEARCH-001"}
        assert goal["total"] == 5

    def test_unrelated_tickets_excluded(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        _set_fields(docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md", serves=[goal_stem])

        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)

        # FEAT-002 is a sibling, not a descendant of FEAT-001 -- excluded.
        serving_ids = {s["id"] for s in goal["serving"]}
        assert serving_ids == {"FEAT-001"}

    def test_counts_and_pct_done(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        _set_fields(docs_root / "tickets" / "EPIC-001_CLI_TOOLING.md", serves=[goal_stem])

        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)

        # complete: FEAT-001, RESEARCH-001. blocked: TASK-001 (unresolved
        # FEAT-002 blocker). in-progress: EPIC-001, FEAT-002.
        assert goal["counts"] == {"complete": 2, "blocked": 1, "in-progress": 2}
        assert goal["done"] == 2
        assert goal["pct_done"] == 40

    def test_stamped_true_for_status_stamped(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        rollup = parser.get_goal_rollup(docs_root)
        assert next(g for g in rollup if g["stem"] == goal_stem)["stamped"] is True

    def test_stamped_false_for_draft(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-002_DRAFT_GOAL.md", title="Draft Goal", status="draft")
        rollup = parser.get_goal_rollup(docs_root)
        assert next(g for g in rollup if g["stem"] == goal_stem)["stamped"] is False

    def test_stamped_goal_with_no_serving_tickets_is_a_gap(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)
        assert goal["total"] == 0
        assert goal["unplanned_gap"] is True

    def test_draft_goal_with_no_serving_tickets_is_not_a_gap(self, docs_root):
        # Drafts are proposals -- never flagged, per FEAT-008/goals convention.
        goal_stem = _write_goal(docs_root, "GOAL-002_DRAFT_GOAL.md", title="Draft Goal", status="draft")
        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)
        assert goal["unplanned_gap"] is False

    def test_stamped_goal_with_only_blocked_serving_tickets_is_a_gap(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        # A single ticket, blocked by a dangling (nonexistent) blocker id --
        # no other ticket needed to prove "nothing workable" here.
        path = docs_root / "tickets" / "TASK-002_LONE.md"
        fm = {
            "id": "TASK-002", "type": "task", "title": "Lone", "status": "open",
            "priority": "medium", "parent": None, "blockers": ["TASK-999"],
            "created": "2026-01-01", "updated": "2026-01-01", "completed": None,
            "tags": [], "serves": [goal_stem],
        }
        parser.write_document(path, fm, "# Lone\n")

        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)
        assert goal["counts"] == {"blocked": 1}
        assert goal["unplanned_gap"] is True

    def test_stamped_goal_fully_done_is_not_a_gap(self, docs_root):
        # All serving tickets complete/closed -- the goal is achieved, not
        # stuck; must not be flagged even though nothing is "workable".
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        _set_fields(docs_root / "tickets" / "FEAT-001_EXPANDED_FRONTMATTER.md", serves=[goal_stem])

        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)
        assert goal["counts"] == {"complete": 1}
        assert goal["pct_done"] == 100
        assert goal["unplanned_gap"] is False

    def test_stamped_goal_with_in_progress_serving_ticket_is_not_a_gap(self, docs_root):
        goal_stem = _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        _set_fields(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md", serves=[goal_stem])
        # FEAT-002 is in-progress -- active work exists, so this isn't a gap
        # even though nothing is literally in "open".
        rollup = parser.get_goal_rollup(docs_root)
        goal = next(g for g in rollup if g["stem"] == goal_stem)
        assert goal["unplanned_gap"] is False

    def test_stamped_sorts_before_draft(self, docs_root):
        _write_goal(docs_root, "GOAL-002_DRAFT_GOAL.md", title="Draft Goal", status="draft")
        _write_goal(docs_root, "GOAL-001_MY_GOAL.md", title="My Goal", status="stamped")
        rollup = parser.get_goal_rollup(docs_root)
        assert [g["stamped"] for g in rollup] == [True, False]
