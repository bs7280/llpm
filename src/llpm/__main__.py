"""LLPM CLI entry point -- argparse setup and dispatch."""

from __future__ import annotations

import sys

from . import commands
from .parser import VALID_EFFORTS, VALID_PRIORITIES


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
            "Path to the docs directory (default: ./docs/ or LLPM_DOCS_ROOT env var). "
            "Must come BEFORE the subcommand."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # -- init --
    p_init = subparsers.add_parser(
        "init",
        description=(
            "Initialize LLPM in a project. Creates docs/tickets/, docs/tickets/archive/, "
            "and copies built-in templates to docs/templates/. Safe to re-run -- won't "
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

    # -- board --
    subparsers.add_parser(
        "board",
        description=(
            "Kanban board view showing active work. Displays 4 columns: BLOCKED, OPEN, "
            "IN-PROGRESS, REVIEW. Draft, planned, complete, closed, and deferred tickets "
            "are excluded -- use 'llpm backlog' for pre-work pipeline. Priority is shown "
            "with indicators: !!! = high, ' ! ' = medium."
        ),
        help="Kanban board of active work (blocked/open/in-progress/review)",
    )

    # -- backlog --
    subparsers.add_parser(
        "backlog",
        description=(
            "Show the pre-work pipeline: PLANNED tickets (spec'd, awaiting approval) "
            "and DRAFT tickets (stubs needing specification). Planning agents should "
            "look at DRAFT tickets to flesh out specs."
        ),
        help="Show planned and draft tickets",
    )

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

    # -- create --
    p_create = subparsers.add_parser(
        "create",
        description=(
            "Create a new ticket from a template. Templates are read from docs/templates/. "
            "Without a body, ticket is created as 'draft' with the template body as placeholder. "
            "With a body (--body, --body-file, or piped stdin), ticket is created as 'open' "
            "and the body replaces the template content. File creation is atomic (O_EXCL) "
            "to prevent ID collisions across parallel agents."
        ),
        help="Create a new ticket from a template",
    )
    p_create.add_argument(
        "ticket_type",
        help="Ticket type (epic, feature, task, research, or custom). Must have a matching template in docs/templates/.",
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

    # -- status --
    p_status = subparsers.add_parser(
        "status",
        description=(
            "Change a ticket's status. Always updates the 'updated' date. Setting status "
            "to 'complete' also sets the 'completed' date. 'blocked' is not a valid choice "
            "because it is derived from unresolved blockers."
        ),
        help="Change ticket status",
    )
    p_status.add_argument("ticket_id", help="Ticket ID")
    p_status.add_argument(
        "new_status",
        choices=VALID_STATUSES_FOR_SET,
        help="New status value",
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
            for action in subparsers.choices:
                if action == "blocker":
                    subparsers.choices[action].print_help()
                    break
            raise SystemExit(1)
        blocker_dispatch[args.blocker_action](args)
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
