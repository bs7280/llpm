"""LLPM CLI entry point -- argparse setup and dispatch."""

from __future__ import annotations

import sys

from . import commands
from .parser import VALID_EFFORTS, VALID_PRIORITIES
from .store import MdTreeStoreError


VALID_STATUSES_FOR_SET = ["draft", "planned", "open", "in-progress", "review", "complete", "closed", "deferred"]


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="llpm",
        description=(
            "LLPM -- LLM Project Manager. A CLI tool for stateless, markdown-based "
            "project management designed for LLM multi-agent workflows. All state "
            "lives in markdown files with YAML frontmatter. The CLI handles ID "
            "generation, status validation, blocker resolution, and date tracking."
        ),
    )
    parser.add_argument(
        "--docs-root",
        default=None,
        help=(
            "Path to the docs directory (default: ./llpm/ or LLPM_DOCS_ROOT env var). "
            "Must come BEFORE the subcommand."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # -- init --
    p_init = subparsers.add_parser(
        "init",
        description=(
            "Initialize LLPM in a project. Creates llpm/tickets/, llpm/tickets/archive/, "
            "and copies built-in templates to llpm/templates/. Safe to re-run -- won't "
            "overwrite existing templates."
        ),
        help="Initialize LLPM project structure and templates",
    )

    # -- list --
    p_list = subparsers.add_parser(
        "list",
        description=(
            "List all active (non-archived) tickets in a table. Shows effective status, "
            "which means tickets with unresolved blockers show as 'blocked' regardless "
            "of their stored status. Supports filtering by status, type, and parent."
        ),
        help="List tickets with optional filters",
    )
    p_list.add_argument("--status", help="Filter by effective status (e.g., open, blocked, in-progress)")
    p_list.add_argument("--type", help="Filter by ticket type (e.g., task, feature, epic)")
    p_list.add_argument("--parent", help="Filter by parent ticket ID (case-insensitive)")
    p_list.add_argument("--json", action="store_true", help="Output as JSON array (sorted by priority high->low, then ID)")
    p_list.add_argument("--include-archived", action="store_true", help="Include archived tickets (only with --json)")

    # -- board --
    p_board = subparsers.add_parser(
        "board",
        description=(
            "Kanban board view showing active work. Displays 4 columns: BLOCKED, OPEN, "
            "IN-PROGRESS, REVIEW. Draft, planned, complete, closed, and deferred tickets "
            "are excluded -- use 'llpm backlog' for pre-work pipeline. Priority is shown "
            "with indicators: !!! = high, ' ! ' = medium."
        ),
        help="Kanban board of active work (blocked/open/in-progress/review)",
    )
    p_board.add_argument("--json", action="store_true", help="Output as JSON array (column order, then priority high->low, then ID)")

    # -- backlog --
    p_backlog = subparsers.add_parser(
        "backlog",
        description=(
            "Show the pre-work pipeline: PLANNED tickets (spec'd, awaiting approval) "
            "and DRAFT tickets (stubs needing specification). Planning agents should "
            "look at DRAFT tickets to flesh out specs."
        ),
        help="Show planned and draft tickets",
    )
    p_backlog.add_argument("--json", action="store_true", help="Output as JSON array")

    # -- show --
    p_show = subparsers.add_parser(
        "show",
        description=(
            "Display full details of a single ticket including all frontmatter fields, "
            "derived children, blocker resolution status, and the complete markdown body. "
            "Status shown is the effective (derived) status."
        ),
        help="Show full ticket details",
    )
    p_show.add_argument("ticket_id", help="Ticket ID (e.g., FEAT-001). Case-insensitive.")
    p_show.add_argument("--json", action="store_true", help="Output as JSON object")

    # -- create --
    p_create = subparsers.add_parser(
        "create",
        description=(
            "Create a new ticket from a template. Templates are read from llpm/templates/. "
            "Without a body, ticket is created as 'draft' with the template body as placeholder. "
            "With a body (--body, --body-file, or piped stdin), ticket is created as 'open' "
            "and the body replaces the template content. File creation is atomic (O_EXCL) "
            "to prevent ID collisions across parallel agents. Agent-origin tickets (FEAT-011 "
            "intake policy) always land 'draft' unless their type is on the project's "
            "[intake] auto_approve list, and must attach to a goal via --serves/--parent or "
            "pass --triage -- 'llpm orphans' reports ones that later drift unattached."
        ),
        help="Create a new ticket from a template",
    )
    p_create.add_argument(
        "ticket_type",
        help="Ticket type (epic, feature, task, research, or custom). Must have a matching template in llpm/templates/.",
    )
    p_create.add_argument("title", help="Human-readable title for the ticket")
    p_create.add_argument("--body", help="Inline body text (replaces template body, sets status to 'open')")
    p_create.add_argument("--body-file", help="Path to a file containing the body text")
    p_create.add_argument("--parent", help="Parent ticket ID (validated to exist)")
    p_create.add_argument(
        "--priority", choices=sorted(VALID_PRIORITIES), default="medium",
        help="Priority level (default: medium)",
    )
    p_create.add_argument(
        "--effort", choices=sorted(VALID_EFFORTS),
        help="Effort/complexity estimate (optional)",
    )
    p_create.add_argument("--tags", help="Comma-separated tags (e.g., 'auth,security')")
    p_create.add_argument(
        "--requires-human", action="store_true",
        help="Mark as requiring human action (agents should surface this to the user)",
    )
    p_create.add_argument(
        "--origin", choices=["human", "agent"],
        help=(
            "Provenance: who created this ticket. Default: LLPM_ORIGIN env var; "
            "else 'agent' when a created-by id is present, else 'human'."
        ),
    )
    p_create.add_argument(
        "--created-by", dest="created_by", metavar="ID",
        help="Provenance: agent/session id. Default: LLPM_CREATED_BY env var.",
    )
    p_create.add_argument(
        "--serves", metavar="GOAL_STEMS",
        help=(
            "Comma-separated full stems of goal notes this ticket serves "
            "(epics/features only). For agent-origin tickets this (or "
            "--parent chained to a goal-serving ancestor) satisfies the "
            "FEAT-011 intake policy's goal-attachment requirement."
        ),
    )
    p_create.add_argument(
        "--triage", action="store_true",
        help=(
            "Explicitly land in the triage pool instead of attaching to a "
            "goal (tags the ticket 'triage'). Agent-origin tickets must "
            "pass this, --serves, or a goal-attached --parent (FEAT-011)."
        ),
    )

    # -- status --
    p_status = subparsers.add_parser(
        "status",
        description=(
            "Change a ticket's status. Always updates the 'updated' date. Setting status "
            "to 'complete' also sets the 'completed' date. 'blocked' is not a valid choice "
            "because it is derived from unresolved blockers. Flipping to 'review' or "
            "'complete' captures provenance: commits mentioning the ticket ID in the "
            "CWD git repo (plus any --commit SHAs) are recorded in commits[]."
        ),
        help="Change ticket status",
    )
    p_status.add_argument("ticket_id", help="Ticket ID")
    p_status.add_argument(
        "new_status",
        choices=VALID_STATUSES_FOR_SET,
        help="New status value",
    )
    p_status.add_argument(
        "--commit", action="append", metavar="SHA", dest="commit",
        help=(
            "Record a commit SHA on the ticket (repeatable). On 'review' and "
            "'complete', commits in the CWD repo mentioning the ticket ID are "
            "also captured automatically into commits[]."
        ),
    )

    # -- set --
    p_set = subparsers.add_parser(
        "set",
        description=(
            "Set frontmatter fields on a ticket. Use field=value syntax. Multiple fields "
            "can be set in one call. Cannot set 'status' (use 'llpm status') or 'blockers' "
            "(use 'llpm blocker'). Cannot set 'id', 'type', 'created', 'updated', "
            "'completed' (managed automatically). List fields like 'tags' accept "
            "comma-separated values. Use 'null' to clear a field."
        ),
        help="Set frontmatter fields (field=value syntax)",
    )
    p_set.add_argument("ticket_id", help="Ticket ID")
    p_set.add_argument("assignments", nargs="+", help="Field assignments (e.g., priority=high effort=large)")

    # -- blocker --
    p_blocker = subparsers.add_parser(
        "blocker",
        description=(
            "Manage ticket blockers (dependency relationships). Blockers must be valid "
            "ticket IDs -- no free-text. When a blocking ticket reaches 'complete' or "
            "'closed' status, it auto-resolves. The --blocked-by flag makes direction "
            "explicit: 'blocker add TASK-001 --blocked-by FEAT-002' means TASK-001 "
            "cannot proceed until FEAT-002 is resolved."
        ),
        help="Manage ticket blockers (add/rm/list)",
    )
    blocker_sub = p_blocker.add_subparsers(dest="blocker_action")

    p_ba = blocker_sub.add_parser("add", help="Add a blocker to a ticket")
    p_ba.add_argument("ticket_id", help="The ticket that is blocked")
    p_ba.add_argument("--blocked-by", required=True, dest="blocked_by", help="The ticket ID that is blocking")

    p_br = blocker_sub.add_parser("rm", help="Remove a blocker (for correcting mistakes)")
    p_br.add_argument("ticket_id", help="The ticket to remove the blocker from")
    p_br.add_argument("--blocked-by", required=True, dest="blocked_by", help="The blocker ticket ID to remove")

    p_bl = blocker_sub.add_parser("list", help="List blockers with resolution status")
    p_bl.add_argument("ticket_id", help="Ticket ID to list blockers for")
    p_bl.add_argument("--json", action="store_true", help="Output as JSON object")

    # -- after --
    p_after = subparsers.add_parser(
        "after",
        description=(
            "Manage soft-precedence edges. 'after' records ordering advice "
            "(\"do this one after that one\") between tickets on this board. It "
            "NEVER blocks -- it is a scheduler tie-break within the ready set and "
            "a dashed edge in diagrams. Violating the order is allowed; cycles "
            "warn but are not errors. For hard dependencies use 'llpm blocker'."
        ),
        help="Manage soft-precedence edges (add/rm) -- ordering advice, never blocks",
    )
    after_sub = p_after.add_subparsers(dest="after_action")

    p_aa = after_sub.add_parser("add", help="Order a ticket after another (soft)")
    p_aa.add_argument("ticket_id", help="The ticket that should come later")
    p_aa.add_argument("--after", required=True, dest="after", help="The ticket ID that should come first")

    p_ar = after_sub.add_parser("rm", help="Remove a soft-precedence edge")
    p_ar.add_argument("ticket_id", help="The ticket to remove the edge from")
    p_ar.add_argument("--after", required=True, dest="after", help="The ticket ID to remove from 'after'")

    # -- waits --
    p_waits = subparsers.add_parser(
        "waits",
        description=(
            "Manage cross-board dependencies. 'waits_on' holds full vault stems of "
            "tickets on other boards (e.g. repos.marginalia.llpm.features.FEAT-010) "
            "and contributes to the derived 'blocked' status while any target is "
            "unresolved. Bare ticket IDs are rejected -- same-board dependencies "
            "belong in 'llpm blocker'. With a local-dir store (or the vault "
            "unreachable) target status is unknown and does NOT block; archived "
            "targets are followed to their archive stem automatically."
        ),
        help="Manage cross-board dependencies (add/rm/list)",
    )
    waits_sub = p_waits.add_subparsers(dest="waits_action")

    p_wa = waits_sub.add_parser("add", help="Add a cross-board dependency")
    p_wa.add_argument("ticket_id", help="The ticket that waits")
    p_wa.add_argument("--on", required=True, dest="on", help="Full vault stem of the target ticket")

    p_wr = waits_sub.add_parser("rm", help="Remove a cross-board dependency")
    p_wr.add_argument("ticket_id", help="The ticket to remove the dependency from")
    p_wr.add_argument("--on", required=True, dest="on", help="The target stem to remove")

    p_wl = waits_sub.add_parser("list", help="List cross-board waits with resolution state")
    p_wl.add_argument("ticket_id", help="Ticket ID to list waits for")
    p_wl.add_argument("--json", action="store_true", help="Output as JSON object")

    # -- serves --
    p_serves = subparsers.add_parser(
        "serves",
        description=(
            "Manage structured goal references on epics/features. 'serves' links a "
            "ticket to any 'type: goal' note in the vault by its full stem (e.g. "
            "goals.unified-agent-platform) -- cross-repo by design. Existence is "
            "not validated (goals may live anywhere); ticket IDs are rejected "
            "(use 'llpm blocker' for dependencies). Only epics and features can "
            "serve goals -- tasks serve via their parent."
        ),
        help="Manage goal references on epics/features (add/rm)",
    )
    serves_sub = p_serves.add_subparsers(dest="serves_action")

    p_sa = serves_sub.add_parser("add", help="Add a goal reference to an epic/feature")
    p_sa.add_argument("ticket_id", help="The epic/feature that serves the goal")
    p_sa.add_argument("goal_stem", help="Full vault stem of the goal note (e.g. goals.my-goal)")

    p_sr = serves_sub.add_parser("rm", help="Remove a goal reference")
    p_sr.add_argument("ticket_id", help="The ticket to remove the goal reference from")
    p_sr.add_argument("goal_stem", help="The goal stem to remove")

    # -- archive --
    p_archive = subparsers.add_parser(
        "archive",
        description=(
            "Move completed/closed tickets to the archive directory. Archived tickets "
            "remain on disk and are scanned for ID generation (IDs never reuse). "
            "Use --all to archive all closed tickets at once."
        ),
        help="Archive completed/closed tickets",
    )
    p_archive.add_argument("ticket_id", nargs="?", help="Ticket ID to archive (or use --all)")
    p_archive.add_argument("--all", action="store_true", help="Archive all closed/complete tickets")
    p_archive.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # -- delete --
    p_delete = subparsers.add_parser(
        "delete",
        description=(
            "Delete a ticket file. Warns about and cleans up relationships: removes the "
            "ticket from other tickets' blocker lists and orphans children (sets their "
            "parent to null). Primarily for correcting mistakes."
        ),
        help="Delete a ticket (with relationship cleanup)",
    )
    p_delete.add_argument("ticket_id", help="Ticket ID to delete")
    p_delete.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # -- todo --
    p_todo = subparsers.add_parser(
        "todo",
        description=(
            "Quick TODO inbox for capturing ideas. Uses stable IDs that never reuse. "
            "Agents should read TODOs, triage into tickets, then remove. Use "
            "--interactive for rapid-fire entry during testing sessions."
        ),
        help="TODO inbox (--add, --rm, --list, --interactive)",
    )
    p_todo.add_argument("--add", "-a", metavar="TEXT", help="Add a new TODO item")
    p_todo.add_argument("--rm", type=int, metavar="ID", help="Remove a TODO item by its stable ID")
    p_todo.add_argument("--list", "-l", action="store_true", help="List all TODO items")
    p_todo.add_argument("--interactive", "-i", action="store_true", help="REPL mode for rapid entry")
    p_todo.add_argument("--json", action="store_true", help="Output as JSON (works with --add, --rm, --list)")

    # -- skills --
    p_skills = subparsers.add_parser(
        "skills",
        description=(
            "List, show, or install bundled Claude skills. Skills are markdown files "
            "that guide Claude through common LLPM workflows (e.g., project init, "
            "migration from old FD system). Use --show to print to stdout (pipeable), "
            "or --install to write directly to .claude/commands/."
        ),
        help="Manage bundled Claude skills (list/show/install)",
    )
    p_skills.add_argument("--show", "-s", metavar="NAME", help="Print a skill's content to stdout")
    p_skills.add_argument("--install", metavar="NAME", help="Install a skill to .claude/commands/")

    # -- goals --
    p_goals = subparsers.add_parser(
        "goals",
        description=(
            "Roll up per-goal progress from 'serves:' chains. Scans the vault for "
            "'type: goal' notes (a type scan, never a location assumption -- goal is "
            "a type, not a place) and, for each, aggregates status counts from "
            "tickets on this board that serve it -- directly via 'serves:', or "
            "inherited through their parent chain. Only 'status: stamped' goals bind "
            "planning and can show an unplanned-gap flag (no workable serving "
            "tickets); draft goals render as proposals."
        ),
        help="Roll up goal progress from serves: chains; flag unplanned gaps",
    )
    p_goals.add_argument("--json", action="store_true", help="Output as JSON array")

    # -- orphans --
    p_orphans = subparsers.add_parser(
        "orphans",
        description=(
            "Report agent-created tickets (origin: agent) that attach to no goal -- "
            "neither their own 'serves' nor any ancestor's -- and aren't tagged "
            "'triage'. 'llpm create' gates this at creation time (FEAT-011), but the "
            "graph can drift afterward (e.g. an ancestor's 'serves' edge removed via "
            "'llpm serves rm'); this is the standing report for that drift. "
            "Human-authored tickets are never flagged -- they were never gated."
        ),
        help="Report agent-created tickets with no goal attachment (FEAT-011)",
    )
    p_orphans.add_argument("--json", action="store_true", help="Output as JSON array")

    # -- project --
    p_project = subparsers.add_parser(
        "project",
        description=(
            "Show project-level metadata: paths, valid enums, and ticket counts. "
            "With --json, returns a single object suitable for bootstrapping a UI."
        ),
        help="Show project metadata and ticket counts",
    )
    p_project.add_argument("--json", action="store_true", help="Output as JSON object")

    # -- help --
    p_help = subparsers.add_parser(
        "help",
        description="Show help for all commands.",
        help="Show detailed help for all commands",
    )
    p_help.add_argument("--verbose", "-v", action="store_true", help="Show full help for every command")

    return parser, subparsers


def main(argv: list[str] | None = None) -> None:
    # UTF-8 safety
    if hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser, subparsers = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        raise SystemExit(1)

    try:
        _run(args, parser, subparsers)
    except MdTreeStoreError as e:
        # Vault store trust/config problem — show the actionable message, not a
        # urllib traceback.
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


def _run(args, parser, subparsers) -> None:
    """Dispatch a parsed command to its handler."""
    dispatch = {
        "init": commands.cmd_init,
        "list": commands.cmd_list,
        "board": commands.cmd_board,
        "backlog": commands.cmd_backlog,
        "show": commands.cmd_show,
        "create": commands.cmd_create,
        "status": commands.cmd_status,
        "set": commands.cmd_set,
        "archive": commands.cmd_archive,
        "delete": commands.cmd_delete,
        "goals": commands.cmd_goals,
        "orphans": commands.cmd_orphans,
        "project": commands.cmd_project,
        "skills": commands.cmd_skills,
        "todo": commands.cmd_todo,
    }

    if args.command == "help":
        _cmd_help(parser, subparsers, verbose=getattr(args, "verbose", False))
        return

    if args.command == "blocker":
        blocker_dispatch = {
            "add": commands.cmd_blocker_add,
            "rm": commands.cmd_blocker_rm,
            "list": commands.cmd_blocker_list,
        }
        if not args.blocker_action:
            # Print blocker subcommand help
            subparsers.choices["blocker"].print_help()
            raise SystemExit(1)
        blocker_dispatch[args.blocker_action](args)
        return

    if args.command == "serves":
        serves_dispatch = {
            "add": commands.cmd_serves_add,
            "rm": commands.cmd_serves_rm,
        }
        if not args.serves_action:
            subparsers.choices["serves"].print_help()
            raise SystemExit(1)
        serves_dispatch[args.serves_action](args)
        return

    if args.command == "after":
        after_dispatch = {
            "add": commands.cmd_after_add,
            "rm": commands.cmd_after_rm,
        }
        if not args.after_action:
            subparsers.choices["after"].print_help()
            raise SystemExit(1)
        after_dispatch[args.after_action](args)
        return

    if args.command == "waits":
        waits_dispatch = {
            "add": commands.cmd_waits_add,
            "rm": commands.cmd_waits_rm,
            "list": commands.cmd_waits_list,
        }
        if not args.waits_action:
            subparsers.choices["waits"].print_help()
            raise SystemExit(1)
        waits_dispatch[args.waits_action](args)
        return

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        raise SystemExit(1)


def _cmd_help(parser, subparsers, verbose: bool = False) -> None:
    """Print help for all commands."""
    parser.print_help()
    print()

    for name, sub in subparsers.choices.items():
        print("=" * 70)
        print(f"llpm {name}")
        print("=" * 70)
        if verbose:
            sub.print_help()
        else:
            desc = sub.description or sub.format_usage()
            print(f"  {desc.strip()}")
        print()


if __name__ == "__main__":
    main()
