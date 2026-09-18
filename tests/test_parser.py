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


# -- Orphan report (FEAT-011) --

def _write_agent_ticket(docs_root: Path, filename: str, *, ticket_type="task",
                         parent=None, tags=None, serves=None) -> None:
    path = docs_root / "tickets" / filename
    fm = {
        "id": path.stem.split("_")[0], "type": ticket_type, "title": "An agent idea",
        "status": "draft", "priority": "medium", "parent": parent, "blockers": [],
        "created": "2026-01-01", "updated": "2026-01-01", "completed": None,
        "tags": tags or [], "origin": "agent", "created_by": "session-1",
    }
    if serves is not None:
        fm["serves"] = serves
    parser.write_document(path, fm, "# An agent idea\n")


class TestOrphanReport:
    def test_unattached_agent_ticket_is_an_orphan(self, docs_root):
        _write_agent_ticket(docs_root, "TASK-002_ORPHAN.md")
        orphans = parser.get_orphans(docs_root)
        assert [o["id"] for o in orphans] == ["TASK-002"]

    def test_triaged_ticket_excluded(self, docs_root):
        _write_agent_ticket(docs_root, "TASK-002_TRIAGED.md", tags=["triage"])
        assert parser.get_orphans(docs_root) == []

    def test_own_serves_excludes(self, docs_root):
        _write_agent_ticket(docs_root, "FEAT-003_ATTACHED.md", ticket_type="feature",
                             serves=["goals.a"])
        assert parser.get_orphans(docs_root) == []

    def test_inherited_serves_via_parent_excludes(self, docs_root):
        # FEAT-002 (fixture) has no serves -- give it one, then a fresh agent
        # task parented under it should inherit the attachment.
        _set_fields(docs_root / "tickets" / "FEAT-002_DOC_PARSING.md", serves=["goals.a"])
        _write_agent_ticket(docs_root, "TASK-002_CHILD.md", parent="FEAT-002")
        assert parser.get_orphans(docs_root) == []

    def test_human_origin_never_flagged(self, docs_root):
        # The fixture tree is entirely human-origin (no `origin` field) and
        # unattached -- the policy never gated it, so it must never appear.
        assert parser.get_orphans(docs_root) == []

    def test_archived_ticket_excluded(self, docs_root):
        _write_agent_ticket(docs_root, "archive/TASK-999_OLD.md")
        assert parser.get_orphans(docs_root) == []


# -- Dispatch readiness (TASK-014) --

def _fm(**overrides) -> dict:
    """A dispatchable task's frontmatter -- override one field per test."""
    base = {
        "id": "TASK-900", "type": "task", "title": "A task", "status": "open",
        "priority": "medium", "effort": "small", "model_tier": "standard",
        "requires_human": False,
    }
    base.update(overrides)
    return base


REAL_AC = "## Acceptance Criteria\n\n- [ ] The thing works\n"


class TestSectionBody:
    def test_missing_section_is_none(self):
        assert parser.section_body("## Description\n\ntext\n", "Acceptance Criteria") is None

    def test_reads_to_the_next_same_level_heading(self):
        body = "## Acceptance Criteria\n\n- [ ] one\n\n## Notes\n\nnot criteria\n"
        section = parser.section_body(body, "Acceptance Criteria")
        assert "- [ ] one" in section
        assert "not criteria" not in section

    def test_subsections_stay_inside(self):
        body = "## Verification\n\n### Manually\n\n- click it\n\n## Related\n\nelsewhere\n"
        section = parser.section_body(body, "Verification")
        assert "click it" in section
        assert "elsewhere" not in section

    def test_heading_match_is_case_insensitive(self):
        assert parser.section_body("## acceptance criteria\n\n- [ ] one\n",
                                   "Acceptance Criteria") is not None

    def test_a_comment_inside_a_fence_is_not_a_heading(self):
        # Without fence tracking the `# run it` line would end the section and
        # the criteria below it would vanish -- a fake `no-ac`.
        body = "## Acceptance Criteria\n\n```bash\n# run it\nmake test\n```\n\n- [ ] it passes\n"
        section = parser.section_body(body, "Acceptance Criteria")
        assert "- [ ] it passes" in section

    def test_last_section_runs_to_the_end(self):
        section = parser.section_body("## Notes\n\nstuff\n\n## Verification\n\n- done\n",
                                      "Verification")
        assert section.strip() == "- done"


class TestIsPlaceholder:
    @pytest.mark.parametrize("text", [
        "",
        "\n\n",
        "_What needs to be done?_",
        "- [ ] _Criterion 1_\n- [ ] _Criterion 2_",
        "1. _How to verify this works_",
        "*fill me in*",
        "<!-- a template comment -->",
    ])
    def test_placeholders(self, text):
        assert parser.is_placeholder(text) is True

    @pytest.mark.parametrize("text", [
        "- [ ] PyYAML in pyproject.toml",
        "1. Run the suite and watch it pass",
        "Prose stating the condition.",
        "- [ ] _Criterion 1_\n- [ ] A real one",
        "**bold is real prose**",
    ])
    def test_real_content(self, text):
        assert parser.is_placeholder(text) is False


class TestDispatchProblems:
    def test_a_clean_ticket_has_no_problems(self):
        assert parser.dispatch_problems(_fm(), REAL_AC) == []

    def test_no_ac_when_the_section_is_missing(self):
        assert parser.dispatch_problems(_fm(), "## Description\n\ndo it\n") == ["no-ac"]

    def test_no_ac_when_the_section_is_still_the_placeholder(self):
        body = "## Acceptance Criteria\n\n- [ ] _Criterion 1_\n- [ ] _Criterion 2_\n"
        assert parser.dispatch_problems(_fm(), body) == ["no-ac"]

    def test_one_real_bullet_clears_no_ac(self):
        body = "## Acceptance Criteria\n\n- [ ] _Criterion 1_\n- [ ] A real one\n"
        assert parser.dispatch_problems(_fm(), body) == []

    def test_a_feature_is_judged_on_verification(self):
        assert parser.dispatch_problems(_fm(type="feature"), REAL_AC) == ["no-ac"]
        assert parser.dispatch_problems(
            _fm(type="feature"), "## Verification\n\n1. Run the suite\n") == []

    def test_a_type_with_no_criteria_heading_is_not_judged_on_one(self):
        # Epics and research notes state no done-condition a worker implements
        # against, so they never report no-ac.
        assert parser.dispatch_problems(_fm(type="epic"), "## Objective\n\nship it\n") == []
        assert parser.dispatch_problems(_fm(type="research"), "") == []

    def test_no_effort(self):
        assert parser.dispatch_problems(_fm(effort=None), REAL_AC) == ["no-effort"]

    def test_requires_human(self):
        assert parser.dispatch_problems(_fm(requires_human=True), REAL_AC) == ["requires-human"]

    def test_no_tier(self):
        assert parser.dispatch_problems(_fm(model_tier=None), REAL_AC) == ["no-tier"]
        # An absent key reads the same as an explicit null.
        fm = _fm()
        del fm["model_tier"]
        assert parser.dispatch_problems(fm, REAL_AC) == ["no-tier"]

    def test_codes_come_out_in_vocabulary_order(self):
        problems = parser.dispatch_problems(
            _fm(effort=None, model_tier=None, requires_human=True), ""
        )
        assert problems == ["no-ac", "no-effort", "requires-human", "no-tier"]
        assert problems == [c for c in parser.DISPATCH_PROBLEMS if c in problems]

    def test_every_code_it_can_report_has_a_legend_line(self):
        assert set(parser.DISPATCH_PROBLEMS) == {
            "no-ac", "no-effort", "requires-human", "no-tier"}
        assert all(parser.DISPATCH_PROBLEMS.values())

    def test_a_ticket_straight_from_the_template_is_not_dispatchable(self, docs_root):
        # The case that produced the ticket: TASK-052 reached the dispatcher
        # with the template's own placeholder criteria.
        fm, body = parser.parse_document(docs_root / "templates" / "task.md")
        assert parser.dispatch_problems(fm, body) == ["no-ac", "no-effort"]

    def test_a_feature_straight_from_the_template_is_not_dispatchable(self, docs_root):
        fm, body = parser.parse_document(docs_root / "templates" / "feature.md")
        assert "no-ac" in parser.dispatch_problems(fm, body)
