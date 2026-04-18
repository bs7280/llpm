"""Command implementations for LLPM CLI."""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import date
from importlib import resources as importlib_resources
from pathlib import Path

from . import parser


# -- Helpers --

def _today() -> str:
    """Return today's date as YYYY-MM-DD. Mockable in tests."""
    return date.today().isoformat()


def _slugify(title: str) -> str:
    """Convert title to UPPER_SNAKE_CASE for filenames."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", title)
    return "_".join(cleaned.upper().split())


def _read_body(args) -> str | None:
    """Read body content from --body, --body-file, or stdin pipe. Returns None if no body."""
    if hasattr(args, "body") and args.body:
        return args.body

    if hasattr(args, "body_file") and args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")

    # Check for piped stdin (only if stdin is not a tty and has data)
    try:
        import select as _select
        if not sys.stdin.isatty() and _select.select([sys.stdin], [], [], 0.0)[0]:
            content = sys.stdin.read()
            if content.strip():
                return content
    except Exception:
        pass

    return None


def _resolve_docs_root(args) -> Path:
    """Resolve docs root from args, env var, or default."""
    if hasattr(args, "docs_root") and args.docs_root:
        return Path(args.docs_root).resolve()
    env = os.environ.get("LLPM_DOCS_ROOT")
    if env:
        return Path(env).resolve()
    return Path("llpm").resolve()


def _templates_source() -> Path:
    """Get path to bundled templates directory."""
    return importlib_resources.files("llpm") / "templates"


def _skills_source() -> Path:
    """Get path to bundled skills directory."""
    return importlib_resources.files("llpm") / "skills"


def _project_templates(docs_root: Path) -> Path:
    """Get path to project-local templates directory."""
    return docs_root / "templates"


def _require_initialized(docs_root: Path) -> None:
    """Error and exit if the project isn't initialized."""
    if not (docs_root / "tickets").exists():
        print(f"Error: Not initialized. Run 'llpm init' first.", file=sys.stderr)
        raise SystemExit(1)


def _require_ticket(docs_root: Path, ticket_id: str) -> tuple[Path, dict, str]:
    """Find and parse a ticket, or exit with error."""
    path = parser.find_ticket_by_id(docs_root, ticket_id)
    if path is None:
        print(f"Error: Ticket '{ticket_id}' not found.", file=sys.stderr)
        raise SystemExit(1)
    fm, body = parser.parse_document(path)
    return path, fm, body


# -- Commands --

def cmd_init(args) -> None:
    docs_root = _resolve_docs_root(args)
    tickets_dir = docs_root / "tickets"

    if tickets_dir.exists():
        print(f"Already initialized: {docs_root}. Run 'llpm list' to see tickets.")
        return

    # Create directories
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "archive").mkdir()
    print(f"Initialized llpm in {docs_root}")
    print(f"  {tickets_dir}")
    print(f"  {tickets_dir / 'archive'}")

    # Copy bundled templates to project
    templates_dst = _project_templates(docs_root)
    templates_dst.mkdir(parents=True, exist_ok=True)

    src_templates = _templates_source()
    for template_file in sorted(Path(str(src_templates)).glob("*.md")):
        dst = templates_dst / template_file.name
        if not dst.exists():
            shutil.copy2(template_file, dst)
            print(f"  {dst}")


def cmd_list(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    tickets = parser.load_all_tickets(docs_root, include_archive=False)
    if not tickets:
        print("No tickets found.")
        return

    # Apply filters
    status_filter = getattr(args, "status", None)
    type_filter = getattr(args, "type", None)
    parent_filter = getattr(args, "parent", None)

    filtered = []
    for path, fm, body in tickets:
        eff_status = parser.effective_status(docs_root, fm)

        if status_filter and eff_status != status_filter:
            continue
        if type_filter and fm.get("type") != type_filter:
            continue
        if parent_filter:
            p = fm.get("parent") or ""
            if p.upper() != parent_filter.upper():
                continue

        filtered.append((fm, eff_status))

    if not filtered:
        print("No tickets match the filters.")
        return

    # Print table
    print(f"{'ID':<16} {'Type':<11} {'Status':<15} {'Priority':<11} Title")
    print("-" * 75)
    for fm, eff_status in filtered:
        print(f"{fm['id']:<16} {fm['type']:<11} {eff_status:<15} {fm['priority']:<11} {fm['title']}")


def cmd_board(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    tickets = parser.load_all_tickets(docs_root, include_archive=False)
    columns = {"blocked": [], "open": [], "in-progress": [], "review": []}

    for path, fm, body in tickets:
        eff_status = parser.effective_status(docs_root, fm)
        if eff_status in columns:
            columns[eff_status].append(fm)

    for col_name in ("blocked", "open", "in-progress", "review"):
        items = columns[col_name]
        print(f"-- {col_name.upper()} ({len(items)}) --")
        if not items:
            print("  (empty)")
        else:
            for fm in items:
                pri = fm.get("priority", "medium")
                indicator = "!!!" if pri == "high" else " ! " if pri == "medium" else "   "
                print(f"  {indicator} {fm['id']:<16} {fm['title']}")
        print()


def cmd_backlog(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    tickets = parser.load_all_tickets(docs_root, include_archive=False)
    sections = {"planned": [], "draft": []}

    for path, fm, body in tickets:
        status = fm.get("status")
        if status in sections:
            sections[status].append(fm)

    for section in ("planned", "draft"):
        items = sections[section]
        print(f"-- {section.upper()} ({len(items)}) --")
        if not items:
            print("  (empty)")
        else:
            print(f"  {'ID':<16} {'Type':<11} {'Priority':<11} Title")
            print(f"  {'-' * 60}")
            for fm in items:
                print(f"  {fm['id']:<16} {fm['type']:<11} {fm['priority']:<11} {fm['title']}")
        print()


def cmd_show(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    path, fm, body = _require_ticket(docs_root, args.ticket_id)
    eff_status = parser.effective_status(docs_root, fm)

    print(f"ID:        {fm['id']}")
    print(f"Type:      {fm['type']}")
    print(f"Title:     {fm['title']}")
    print(f"Status:    {eff_status}")
    print(f"Priority:  {fm['priority']}")

    if "effort" in fm:
        print(f"Effort:    {fm['effort'] or '-'}")
    if fm.get("requires_human"):
        print(f"Requires:  HUMAN ACTION")

    print(f"Parent:    {fm.get('parent') or '-'}")

    # Derived children
    children = parser.get_children(docs_root, fm["id"])
    if children:
        child_ids = ", ".join(c["id"] for c in children)
        print(f"Children:  {child_ids} (derived)")
    else:
        print(f"Children:  -")

    # Blockers with details
    blockers = fm.get("blockers") or []
    if blockers:
        details = parser.get_blocker_details(docs_root, fm)
        parts = []
        for d in details:
            tag = "[RESOLVED]" if d["resolved"] else "[BLOCKING]"
            parts.append(f"{d['id']} ({d['status']}) {tag}")
        print(f"Blockers:  {', '.join(parts)}")
    else:
        print(f"Blockers:  -")

    print(f"Created:   {fm.get('created') or '-'}")
    print(f"Updated:   {fm.get('updated') or '-'}")
    print(f"Completed: {fm.get('completed') or '-'}")

    tags = fm.get("tags") or []
    print(f"Tags:      {', '.join(tags) if tags else '-'}")
    print(f"File:      {path.resolve()}")
    print()
    print(body)


def cmd_create(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    ticket_type = args.ticket_type
    title = args.title

    # Read template from project templates dir
    template_path = _project_templates(docs_root) / f"{ticket_type}.md"
    if not template_path.exists():
        print(f"Error: No template found for type '{ticket_type}' at {template_path}", file=sys.stderr)
        raise SystemExit(1)

    template_text = template_path.read_text(encoding="utf-8")

    # Validate parent if specified
    parent_id = getattr(args, "parent", None)
    if parent_id:
        if parser.find_ticket_by_id(docs_root, parent_id) is None:
            print(f"Error: Parent ticket '{parent_id}' not found.", file=sys.stderr)
            raise SystemExit(1)

    # Read body
    body = _read_body(args)
    today = _today()

    # Determine status
    status = "open" if body else "draft"

    # Atomic create with O_EXCL retry
    max_retries = 3
    for attempt in range(max_retries):
        ticket_id = parser.next_id(docs_root, ticket_type)
        slug = _slugify(title)
        filename = f"{ticket_id}_{slug}.md"
        filepath = docs_root / "tickets" / filename

        # String substitution on template
        content = template_text
        content = content.replace("__ID__", ticket_id)
        content = content.replace("__TITLE__", title)
        content = content.replace("__DATE__", today)

        # Replace status in the template line (preserve comment)
        content = re.sub(
            r"^(status:\s*)draft(\s*#.*)$",
            rf"\g<1>{status}\2",
            content,
            count=1,
            flags=re.MULTILINE,
        )

        # Handle optional fields via string substitution
        priority = getattr(args, "priority", None) or "medium"
        content = re.sub(
            r"^(priority:\s*)medium(\s*#.*)$",
            rf"\g<1>{priority}\2",
            content,
            count=1,
            flags=re.MULTILINE,
        )

        effort = getattr(args, "effort", None)
        if effort:
            content = re.sub(
                r"^(effort:\s*)null(\s*#.*)$",
                rf"\g<1>{effort}\2",
                content,
                count=1,
                flags=re.MULTILINE,
            )

        if parent_id:
            content = content.replace("parent: null", f"parent: {parent_id}", 1)

        tags = getattr(args, "tags", None)
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
            tag_yaml = "[" + ", ".join(tag_list) + "]"
            content = content.replace("tags: []", f"tags: {tag_yaml}", 1)

        requires_human = getattr(args, "requires_human", False)
        if requires_human:
            content = content.replace("requires_human: false", "requires_human: true", 1)

        # Replace body if provided
        if body:
            # Split on second --- to get frontmatter vs body
            parts = content.split("---", 2)
            if len(parts) >= 3:
                content = parts[0] + "---" + parts[1] + "---\n" + body

        # Atomic file creation
        try:
            fd = os.open(str(filepath), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Created {ticket_id}: {title}")
            print(f"File: {filepath}")
            return
        except FileExistsError:
            continue

    print(f"Error: Could not create ticket after {max_retries} retries (ID collision).", file=sys.stderr)
    raise SystemExit(1)


def cmd_status(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    path, fm, body = _require_ticket(docs_root, args.ticket_id)
    old_status = fm["status"]
    new_status = args.new_status

    fm["status"] = new_status
    fm["updated"] = _today()

    if new_status == "complete" and not fm.get("completed"):
        fm["completed"] = _today()

    parser.write_document(path, fm, body)
    print(f"{fm['id']}: {old_status} -> {new_status}")


def cmd_set(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    path, fm, body = _require_ticket(docs_root, args.ticket_id)

    # Restricted fields
    FORBIDDEN = {"id", "type", "created", "updated", "completed"}
    REDIRECT = {
        "status": "Use 'llpm status'.",
        "blockers": "Use 'llpm blocker'.",
    }

    # Parse field=value pairs
    assignments = args.assignments
    changes = []

    for assignment in assignments:
        if "=" in assignment:
            field, value = assignment.split("=", 1)
        else:
            # Legacy single-field syntax: llpm set ID field value
            # Only works if exactly 2 args remain
            print(f"Error: Use field=value syntax (e.g., 'priority=high').", file=sys.stderr)
            raise SystemExit(1)

        field = field.strip()
        value = value.strip()

        if field in FORBIDDEN:
            print(f"Error: Cannot set '{field}' -- managed automatically.", file=sys.stderr)
            raise SystemExit(1)

        if field in REDIRECT:
            print(f"Error: Cannot set '{field}' via 'set'. {REDIRECT[field]}", file=sys.stderr)
            raise SystemExit(1)

        # Validate enums
        if field == "priority" and value not in parser.VALID_PRIORITIES:
            print(f"Error: Invalid priority '{value}'. Must be one of: {', '.join(sorted(parser.VALID_PRIORITIES))}", file=sys.stderr)
            raise SystemExit(1)

        if field == "effort":
            if value.lower() in ("null", "none"):
                value = None
            elif value not in parser.VALID_EFFORTS:
                print(f"Error: Invalid effort '{value}'. Must be one of: {', '.join(sorted(parser.VALID_EFFORTS))}", file=sys.stderr)
                raise SystemExit(1)

        # Handle null/none
        if isinstance(value, str) and value.lower() in ("null", "none"):
            value = None

        # Handle list fields
        if field == "tags":
            value = [t.strip() for t in value.split(",")] if value else []

        # Handle requires_human
        if field == "requires_human":
            value = value.lower() in ("true", "yes", "1")

        # Validate parent exists
        if field == "parent" and value is not None:
            if parser.find_ticket_by_id(docs_root, value) is None:
                print(f"Error: Parent ticket '{value}' not found.", file=sys.stderr)
                raise SystemExit(1)

        changes.append((field, value))

    # All validations passed -- apply changes
    for field, value in changes:
        old = fm.get(field)
        fm[field] = value
        print(f"{fm['id']}: {field} = {value} (was {old})")

    fm["updated"] = _today()
    parser.write_document(path, fm, body)


def cmd_blocker_add(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    path, fm, body = _require_ticket(docs_root, args.ticket_id)
    blocker_id = args.blocked_by

    # Validate blocker exists
    if parser.find_ticket_by_id(docs_root, blocker_id) is None:
        print(f"Error: Ticket '{blocker_id}' not found.", file=sys.stderr)
        raise SystemExit(1)

    blockers = fm.get("blockers") or []

    # Check for duplicate (case-insensitive)
    if any(b.upper() == blocker_id.upper() for b in blockers):
        print(f"{fm['id']}: already blocked by '{blocker_id}'.")
        return

    blockers.append(blocker_id.upper())
    fm["blockers"] = blockers
    fm["updated"] = _today()
    parser.write_document(path, fm, body)
    print(f"{fm['id']}: now blocked by '{blocker_id}'")


def cmd_blocker_rm(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    path, fm, body = _require_ticket(docs_root, args.ticket_id)
    blocker_id = args.blocked_by

    blockers = fm.get("blockers") or []
    upper_id = blocker_id.upper()

    matching = [b for b in blockers if b.upper() == upper_id]
    if not matching:
        print(f"Error: '{blocker_id}' is not a blocker on {fm['id']}.", file=sys.stderr)
        raise SystemExit(1)

    fm["blockers"] = [b for b in blockers if b.upper() != upper_id]
    fm["updated"] = _today()
    parser.write_document(path, fm, body)
    print(f"{fm['id']}: removed blocker '{blocker_id}'")


def cmd_blocker_list(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    path, fm, body = _require_ticket(docs_root, args.ticket_id)
    print(f"{fm['id']}: {fm['title']}")
    print()

    blockers = fm.get("blockers") or []
    if not blockers:
        print("No blockers.")
        return

    details = parser.get_blocker_details(docs_root, fm)
    print("Blockers:")
    unresolved = 0
    for d in details:
        tag = "[RESOLVED]" if d["resolved"] else "[BLOCKING]"
        if not d["resolved"]:
            unresolved += 1
        print(f"  {d['id']:<16} {d['status']:<15} {d['title']:<30} {tag}")

    print()
    if unresolved > 0:
        print(f"Status: BLOCKED ({unresolved} unresolved)")
    else:
        print("Status: all blockers resolved")


def cmd_archive(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    archive_dir = docs_root / "tickets" / "archive"
    archive_dir.mkdir(exist_ok=True)

    archive_all = getattr(args, "all", False)
    auto_yes = getattr(args, "yes", False)

    if archive_all:
        # Find all closed/complete non-archived tickets
        tickets = parser.load_all_tickets(docs_root, include_archive=False)
        to_archive = []
        for path, fm, body in tickets:
            if fm.get("status") in parser.RESOLVED_STATUSES:
                to_archive.append((path, fm))

        if not to_archive:
            print("No closed tickets to archive.")
            return

        print(f"Found {len(to_archive)} closed ticket(s) to archive:")
        for path, fm in to_archive:
            print(f"  {fm['id']}  {fm['title']}")

        if not auto_yes:
            response = input("Archive all? [y/N]: ").strip().lower()
            if response != "y":
                print("Cancelled.")
                return

        for path, fm in to_archive:
            dst = archive_dir / path.name
            path.rename(dst)
        print(f"Archived {len(to_archive)} ticket(s).")
    else:
        ticket_id = args.ticket_id
        path, fm, body = _require_ticket(docs_root, ticket_id)

        if fm.get("status") not in parser.RESOLVED_STATUSES:
            print(f"Error: {fm['id']} is '{fm['status']}'. Only complete or closed tickets can be archived.", file=sys.stderr)
            raise SystemExit(1)

        dst = archive_dir / path.name
        path.rename(dst)
        print(f"Archived {fm['id']} -> tickets/archive/{path.name}")


def cmd_delete(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    path, fm, body = _require_ticket(docs_root, args.ticket_id)
    ticket_id = fm["id"]
    auto_yes = getattr(args, "yes", False)

    # Find relationships
    all_tickets = parser.load_all_tickets(docs_root, include_archive=False)
    references = []
    children_of = []

    for t_path, t_fm, t_body in all_tickets:
        if t_fm["id"] == ticket_id:
            continue
        # Check if this ticket is in someone's blockers
        t_blockers = t_fm.get("blockers") or []
        if any(b.upper() == ticket_id.upper() for b in t_blockers):
            references.append((t_fm["id"], "blockers"))
        # Check if this ticket is someone's parent
        if (t_fm.get("parent") or "").upper() == ticket_id.upper():
            children_of.append(t_fm["id"])

    print(f"{ticket_id}: {fm['title']}")
    print()

    if references or children_of:
        print("WARNING: This ticket is referenced by:")
        for ref_id, ref_field in references:
            print(f"  - {ref_id} {ref_field} list")
        for child_id in children_of:
            print(f"  - {child_id} has this as parent")
        print()
        print("Deleting will:")
        for ref_id, ref_field in references:
            print(f"  - Remove {ticket_id} from {ref_id}'s {ref_field}")
        for child_id in children_of:
            print(f"  - Orphan {child_id} (parent will become null)")
        print()

    if not auto_yes:
        response = input("Delete? [y/N]: ").strip().lower()
        if response != "y":
            print("Cancelled.")
            return

    # Clean up references
    for t_path, t_fm, t_body in all_tickets:
        modified = False
        if t_fm["id"] == ticket_id:
            continue

        # Remove from blockers
        t_blockers = t_fm.get("blockers") or []
        new_blockers = [b for b in t_blockers if b.upper() != ticket_id.upper()]
        if len(new_blockers) != len(t_blockers):
            t_fm["blockers"] = new_blockers
            modified = True

        # Clear parent
        if (t_fm.get("parent") or "").upper() == ticket_id.upper():
            t_fm["parent"] = None
            modified = True

        if modified:
            t_fm["updated"] = _today()
            parser.write_document(t_path, t_fm, t_body)

    path.unlink()
    print(f"Deleted {ticket_id}.")


def cmd_todo(args) -> None:
    docs_root = _resolve_docs_root(args)
    _require_initialized(docs_root)

    todo_path = docs_root / "TODO.md"

    # Determine action
    add_text = getattr(args, "add", None)
    rm_id = getattr(args, "rm", None)
    list_todos = getattr(args, "list", False)
    interactive = getattr(args, "interactive", False)

    if add_text:
        _todo_add(todo_path, add_text)
    elif rm_id is not None:
        _todo_rm(todo_path, rm_id)
    elif list_todos:
        _todo_list(todo_path)
    elif interactive:
        _todo_interactive(todo_path)
    else:
        # No flags -- show help hint
        print("Usage: llpm todo --add \"text\" | --rm <id> | --list | --interactive")
        print("Run 'llpm todo --help' for details.")
        raise SystemExit(1)


def _todo_parse(todo_path: Path) -> list[tuple[int, str]]:
    """Parse TODO.md, returning list of (id, text) tuples."""
    if not todo_path.exists():
        return []
    items = []
    for line in todo_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^- \((\d+)\) (.+)$", line)
        if m:
            items.append((int(m.group(1)), m.group(2)))
    return items


def _todo_write(todo_path: Path, items: list[tuple[int, str]]) -> None:
    """Write TODO.md from list of (id, text) tuples."""
    lines = [f"- ({item_id}) {text}" for item_id, text in items]
    todo_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _todo_next_id(items: list[tuple[int, str]]) -> int:
    """Get next stable ID (never reuses)."""
    if not items:
        return 1
    return max(item_id for item_id, _ in items) + 1


def _todo_add(todo_path: Path, text: str) -> None:
    items = _todo_parse(todo_path)
    new_id = _todo_next_id(items)
    items.append((new_id, text))
    _todo_write(todo_path, items)
    print(f"({new_id}) {text}")


def _todo_rm(todo_path: Path, item_id: int) -> None:
    items = _todo_parse(todo_path)
    matching = [(i, t) for i, t in items if i == item_id]
    if not matching:
        print(f"Error: TODO ({item_id}) not found.", file=sys.stderr)
        raise SystemExit(1)
    removed_text = matching[0][1]
    items = [(i, t) for i, t in items if i != item_id]
    _todo_write(todo_path, items)
    print(f"Removed ({item_id}): {removed_text}")


def _todo_list(todo_path: Path) -> None:
    items = _todo_parse(todo_path)
    if not items:
        print("TODO: empty")
        return
    print(f"TODO ({len(items)} items):")
    for item_id, text in items:
        print(f"  ({item_id}) {text}")


def _todo_interactive(todo_path: Path) -> None:
    print("TODO REPL (empty line or ctrl-d to exit):")
    count = 0
    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            if not line.strip():
                break
            items = _todo_parse(todo_path)
            new_id = _todo_next_id(items)
            items.append((new_id, line.strip()))
            _todo_write(todo_path, items)
            print(f"  ({new_id}) {line.strip()}")
            count += 1
    except KeyboardInterrupt:
        print()
    if count:
        print(f"Added {count} item(s).")


def cmd_skills(args) -> None:
    """List, show, or install bundled Claude skills."""
    skills_dir = _skills_source()
    skills_path = Path(str(skills_dir))

    available = sorted(p.stem for p in skills_path.glob("*.md"))

    show_name = getattr(args, "show", None)
    install_name = getattr(args, "install", None)

    if show_name:
        if show_name not in available:
            print(f"Error: Skill '{show_name}' not found. Available: {', '.join(available)}", file=sys.stderr)
            raise SystemExit(1)
        content = (skills_path / f"{show_name}.md").read_text(encoding="utf-8")
        print(content)

    elif install_name:
        if install_name not in available:
            print(f"Error: Skill '{install_name}' not found. Available: {', '.join(available)}", file=sys.stderr)
            raise SystemExit(1)

        # Install to .claude/commands/ relative to cwd (or docs_root parent)
        install_dir = Path(".claude/commands").resolve()
        install_dir.mkdir(parents=True, exist_ok=True)
        dst = install_dir / f"{install_name}.md"

        src = skills_path / f"{install_name}.md"
        shutil.copy2(src, dst)
        print(f"Installed skill '{install_name}' to {dst}")

    else:
        # List available skills
        if not available:
            print("No skills available.")
            return
        print("Available skills:")
        for name in available:
            # Read first non-empty, non-heading line as description
            content = (skills_path / f"{name}.md").read_text(encoding="utf-8")
            first_heading = ""
            for line in content.splitlines():
                if line.startswith("# "):
                    first_heading = line[2:].strip()
                    break
            print(f"  {name:<25} {first_heading}")
        print()
        print("Usage:")
        print("  llpm skills --show <name>      Print skill content to stdout")
        print("  llpm skills --install <name>    Install to .claude/commands/")
