"""Command implementations for LLPM CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tomllib
from datetime import date
from importlib import resources as importlib_resources
from pathlib import Path

from . import parser
from .store import LocalDirStore, MdTreeStore, TicketStore


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


def _find_repo_config() -> dict | None:
    """Walk upward from CWD to find .llpm/config.toml.

    Returns a config dict on success (keys: ``kind``, plus kind-specific keys),
    or None if no config file was found.

    Supported kinds:
    - ``"dir"``: local filesystem store; includes ``"docs_root"`` (Path).
    - ``"mdtree"``: vault HTTP store; includes ``"base_url"`` (str),
      ``"repo_stem"`` (str), and ``"ca"`` (str path to a CA bundle, or None).
    """
    current = Path.cwd()
    while True:
        config_path = current / ".llpm" / "config.toml"
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            except Exception as e:
                print(f"Error: Failed to parse {config_path}: {e}", file=sys.stderr)
                raise SystemExit(1)

            store_section = data.get("store", {})
            kind = store_section.get("kind", "dir")

            if kind == "dir":
                root_str = store_section.get("root", "./llpm")
                docs_root = (current / root_str).resolve()
                return {"kind": "dir", "docs_root": docs_root}

            if kind == "mdtree":
                base_url = store_section.get("url")
                repo_stem = store_section.get("stem")
                if not base_url or not repo_stem:
                    print(
                        f"Error: store.kind=mdtree in {config_path} requires "
                        f"'url' and 'stem' fields.",
                        file=sys.stderr,
                    )
                    raise SystemExit(1)
                ca_str = store_section.get("ca")
                ca = None
                if ca_str:
                    # Resolve relative to the config dir; expand ~ and normalize.
                    ca = str((current / os.path.expanduser(ca_str)).resolve())
                return {
                    "kind": "mdtree",
                    "base_url": base_url,
                    "repo_stem": repo_stem,
                    "ca": ca,
                }

            print(
                f"Error: Unknown store kind {kind!r} in {config_path} "
                f"— supported: dir, mdtree",
                file=sys.stderr,
            )
            raise SystemExit(1)

        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
        current = parent


def _resolve_store_config(args) -> dict:
    """Resolve the full store configuration.

    Resolution order:
    1. --docs-root flag  (forces kind=dir)
    2. LLPM_DOCS_ROOT env var  (forces kind=dir)
    3. In-repo .llpm/config.toml (walk upward from CWD)
    4. Default: ./llpm dir, kind=dir (no error — _require_initialized handles it)
    """
    if hasattr(args, "docs_root") and args.docs_root:
        return {"kind": "dir", "docs_root": Path(args.docs_root).resolve()}

    env = os.environ.get("LLPM_DOCS_ROOT")
    if env:
        return {"kind": "dir", "docs_root": Path(env).resolve()}

    repo_config = _find_repo_config()
    if repo_config is not None:
        return repo_config

    # Default: ./llpm dir
    return {"kind": "dir", "docs_root": Path("llpm").resolve()}


def _resolve_docs_root(args) -> Path:
    """Resolve docs root from args, env var, in-repo config, or default.

    For mdtree stores returns a sentinel path (not used for file I/O).
    """
    cfg = _resolve_store_config(args)
    if cfg["kind"] == "dir":
        return cfg["docs_root"]
    # mdtree: return a non-existent sentinel path so callers don't crash
    return Path("/dev/null/mdtree-sentinel")


def _templates_source() -> Path:
    """Get path to bundled templates directory."""
    return importlib_resources.files("llpm") / "templates"


def _skills_source() -> Path:
    """Get path to bundled skills directory."""
    return importlib_resources.files("llpm") / "skills"


def _project_templates(docs_root: Path) -> Path:
    """Get path to project-local templates directory."""
    return docs_root / "templates"


def _make_store(docs_root: Path, kind: str = "dir", **kwargs) -> TicketStore:
    """Construct the ticket store for a given docs root.

    Built once per command dispatch; all ticket I/O flows through it.
    For MdTreeStore pass ``kind="mdtree"`` with ``base_url`` and ``repo_stem``
    kwargs (or use ``_make_store_from_config``).
    """
    if kind == "dir":
        return LocalDirStore(docs_root)
    if kind == "mdtree":
        return MdTreeStore(
            base_url=kwargs["base_url"],
            repo_stem=kwargs["repo_stem"],
            ca=kwargs.get("ca"),
        )
    raise SystemExit(f"Unknown store kind: {kind!r}")


def _make_store_from_config(cfg: dict) -> TicketStore:
    """Construct a TicketStore from a resolved store-config dict."""
    if cfg["kind"] == "dir":
        return LocalDirStore(cfg["docs_root"])
    if cfg["kind"] == "mdtree":
        return MdTreeStore(
            base_url=cfg["base_url"],
            repo_stem=cfg["repo_stem"],
            ca=cfg.get("ca"),
        )
    raise SystemExit(f"Unknown store kind: {cfg['kind']!r}")


def _resolve_store_and_root(args) -> tuple[TicketStore, Path]:
    """Resolve the store and docs_root from args in one call.

    Returns ``(store, docs_root)`` where ``docs_root`` is a real path for
    ``dir`` stores and a sentinel path for ``mdtree`` stores.  Also validates
    that the project is initialized (or skips the check for mdtree).
    """
    cfg = _resolve_store_config(args)
    docs_root: Path = cfg.get("docs_root", Path("/dev/null/mdtree-sentinel"))
    store = _make_store_from_config(cfg)
    _require_initialized(docs_root, store=store)
    return store, docs_root


def _require_initialized(docs_root: Path, store: TicketStore | None = None) -> None:
    """Error and exit if the project isn't initialized.

    For MdTreeStore pass the store as well — vault stores skip the local
    filesystem check (the vault is always assumed to exist).
    """
    if isinstance(store, MdTreeStore):
        return  # vault stores need no local init check
    if not (docs_root / "tickets").exists():
        print(f"Error: Not initialized. Run 'llpm init' first.", file=sys.stderr)
        raise SystemExit(1)


def _require_ticket(store: TicketStore, ticket_id: str) -> tuple[Path, dict, str]:
    """Find and parse a ticket, or exit with error."""
    result = store.read(ticket_id)
    if result is None:
        print(f"Error: Ticket '{ticket_id}' not found.", file=sys.stderr)
        raise SystemExit(1)
    return result


def _ticket_to_dict(docs_root: Path, path: Path, fm: dict, body: str | None = None) -> dict:
    """Serialize a ticket to the JSON output schema.

    If body is None, it is omitted (list mode). If provided, it is included (show mode).
    """
    eff_status = parser.effective_status(docs_root, fm)
    is_blocked = eff_status == "blocked"

    children = parser.get_children(docs_root, fm["id"])
    child_ids = [c["id"] for c in children]

    blocker_details = parser.get_blocker_details(docs_root, fm) if fm.get("blockers") else []

    archived = "archive" in path.parts

    result = {
        "id": fm["id"],
        "type": fm["type"],
        "title": fm["title"],
        "status": fm["status"],
        "effective_status": eff_status,
        "is_blocked": is_blocked,
        "priority": fm["priority"],
        "effort": fm.get("effort"),
        "parent": fm.get("parent"),
        "children": child_ids,
        "blockers": [
            {"id": d["id"], "resolved": d["resolved"]}
            for d in blocker_details
        ],
        "tags": fm.get("tags") or [],
        "requires_human": fm.get("requires_human", False),
        "created": fm.get("created"),
        "updated": fm.get("updated"),
        "completed": fm.get("completed"),
        "archived": archived,
        "path": str(path),
    }

    if body is not None:
        result["body"] = body
        result["body_html"] = None

    return result


def _json_out(data) -> None:
    """Print data as JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


# -- Commands --

def cmd_init(args) -> None:
    cfg = _resolve_store_config(args)

    # Vault stores need no local init — templates fall back to bundled.
    if cfg["kind"] == "mdtree":
        print("Vault store configured: no local init needed.")
        print("Templates fall back to bundled; run 'llpm create' directly.")
        return

    docs_root: Path = cfg["docs_root"]
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
    store, docs_root = _resolve_store_and_root(args)

    include_archived = getattr(args, "include_archived", False)
    use_json = getattr(args, "json", False)

    tickets = parser.load_all_tickets(store, include_archive=include_archived)
    if not tickets:
        if use_json:
            _json_out([])
        else:
            print("No tickets found.")
        return

    # Apply filters
    status_filter = getattr(args, "status", None)
    type_filter = getattr(args, "type", None)
    parent_filter = getattr(args, "parent", None)

    filtered = []
    for path, fm, body in tickets:
        eff_status = parser.effective_status(store, fm)

        if status_filter and eff_status != status_filter:
            continue
        if type_filter and fm.get("type") != type_filter:
            continue
        if parent_filter:
            p = fm.get("parent") or ""
            if p.upper() != parent_filter.upper():
                continue

        filtered.append((path, fm, eff_status))

    if use_json:
        _json_out([_ticket_to_dict(docs_root, path, fm) for path, fm, _ in filtered])
        return

    if not filtered:
        print("No tickets match the filters.")
        return

    # Print table
    print(f"{'ID':<16} {'Type':<11} {'Status':<15} {'Priority':<11} Title")
    print("-" * 75)
    for path, fm, eff_status in filtered:
        tier = fm.get("model_tier")
        tier_chip = f" [{tier}]" if tier else ""
        print(f"{fm['id']:<16} {fm['type']:<11} {eff_status:<15} {fm['priority']:<11} {fm['title']}{tier_chip}")


def cmd_board(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    use_json = getattr(args, "json", False)
    tickets = parser.load_all_tickets(store, include_archive=False)
    columns = {"blocked": [], "open": [], "in-progress": [], "review": []}

    for path, fm, body in tickets:
        eff_status = parser.effective_status(store, fm)
        if eff_status in columns:
            columns[eff_status].append((path, fm))

    if use_json:
        result = []
        for col_name in ("blocked", "open", "in-progress", "review"):
            for path, fm in columns[col_name]:
                result.append(_ticket_to_dict(docs_root, path, fm))
        _json_out(result)
        return

    for col_name in ("blocked", "open", "in-progress", "review"):
        items = columns[col_name]
        print(f"-- {col_name.upper()} ({len(items)}) --")
        if not items:
            print("  (empty)")
        else:
            for path, fm in items:
                pri = fm.get("priority", "medium")
                indicator = "!!!" if pri == "high" else " ! " if pri == "medium" else "   "
                print(f"  {indicator} {fm['id']:<16} {fm['title']}")
        print()


def cmd_backlog(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    use_json = getattr(args, "json", False)
    tickets = parser.load_all_tickets(store, include_archive=False)
    sections = {"planned": [], "draft": []}

    for path, fm, body in tickets:
        status = fm.get("status")
        if status in sections:
            sections[status].append((path, fm))

    if use_json:
        result = []
        for section in ("planned", "draft"):
            for path, fm in sections[section]:
                result.append(_ticket_to_dict(docs_root, path, fm))
        _json_out(result)
        return

    for section in ("planned", "draft"):
        items = sections[section]
        print(f"-- {section.upper()} ({len(items)}) --")
        if not items:
            print("  (empty)")
        else:
            print(f"  {'ID':<16} {'Type':<11} {'Priority':<11} Title")
            print(f"  {'-' * 60}")
            for path, fm in items:
                tier = fm.get("model_tier")
                tier_chip = f" [{tier}]" if tier else ""
                print(f"  {fm['id']:<16} {fm['type']:<11} {fm['priority']:<11} {fm['title']}{tier_chip}")
        print()


def cmd_show(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)

    if getattr(args, "json", False):
        _json_out(_ticket_to_dict(docs_root, path, fm, body=body))
        return

    eff_status = parser.effective_status(store, fm)

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
    children = parser.get_children(store, fm["id"])
    if children:
        child_ids = ", ".join(c["id"] for c in children)
        print(f"Children:  {child_ids} (derived)")
    else:
        print(f"Children:  -")

    # Blockers with details
    blockers = fm.get("blockers") or []
    if blockers:
        details = parser.get_blocker_details(store, fm)
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
    store, docs_root = _resolve_store_and_root(args)

    ticket_type = args.ticket_type
    title = args.title

    # Read template: store-side first (vault override or local project copy),
    # then fall back to bundled templates.
    template_text = store.read_blob(f"templates/{ticket_type}.md")
    if template_text is None:
        # Bundled fallback — avoids requiring per-board template seeding for vault stores.
        bundled_path = _templates_source() / f"{ticket_type}.md"
        bundled_file = Path(str(bundled_path))
        if bundled_file.exists():
            template_text = bundled_file.read_text(encoding="utf-8")
        else:
            print(f"Error: No template found for type '{ticket_type}'.", file=sys.stderr)
            raise SystemExit(1)

    # Validate parent if specified
    parent_id = getattr(args, "parent", None)
    if parent_id:
        if not store.exists(parent_id):
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
        ticket_id = parser.next_id(store, ticket_type)
        slug = _slugify(title)
        filename = f"{ticket_id}_{slug}.md"

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
            filepath = store.create_exclusive(filename, content)
            print(f"Created {ticket_id}: {title}")
            print(f"File: {filepath}")
            return
        except FileExistsError:
            continue

    print(f"Error: Could not create ticket after {max_retries} retries (ID collision).", file=sys.stderr)
    raise SystemExit(1)


def cmd_status(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    old_status = fm["status"]
    new_status = args.new_status

    fm["status"] = new_status
    fm["updated"] = _today()

    if new_status == "complete" and not fm.get("completed"):
        fm["completed"] = _today()

    store.write(path, fm, body)
    print(f"{fm['id']}: {old_status} -> {new_status}")


def cmd_set(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)

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
            if not store.exists(value):
                print(f"Error: Parent ticket '{value}' not found.", file=sys.stderr)
                raise SystemExit(1)

        changes.append((field, value))

    # All validations passed -- apply changes
    for field, value in changes:
        old = fm.get(field)
        fm[field] = value
        print(f"{fm['id']}: {field} = {value} (was {old})")

    fm["updated"] = _today()
    store.write(path, fm, body)


def cmd_blocker_add(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    blocker_id = args.blocked_by

    # Validate blocker exists
    if not store.exists(blocker_id):
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
    store.write(path, fm, body)
    print(f"{fm['id']}: now blocked by '{blocker_id}'")


def cmd_blocker_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    blocker_id = args.blocked_by

    blockers = fm.get("blockers") or []
    upper_id = blocker_id.upper()

    matching = [b for b in blockers if b.upper() == upper_id]
    if not matching:
        print(f"Error: '{blocker_id}' is not a blocker on {fm['id']}.", file=sys.stderr)
        raise SystemExit(1)

    fm["blockers"] = [b for b in blockers if b.upper() != upper_id]
    fm["updated"] = _today()
    store.write(path, fm, body)
    print(f"{fm['id']}: removed blocker '{blocker_id}'")


def cmd_blocker_list(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)

    if getattr(args, "json", False):
        details = parser.get_blocker_details(store, fm)
        _json_out({
            "id": fm["id"],
            "title": fm["title"],
            "blockers": [
                {"id": d["id"], "status": d["status"], "title": d["title"], "resolved": d["resolved"]}
                for d in details
            ],
        })
        return

    print(f"{fm['id']}: {fm['title']}")
    print()

    blockers = fm.get("blockers") or []
    if not blockers:
        print("No blockers.")
        return

    details = parser.get_blocker_details(store, fm)
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
    store, docs_root = _resolve_store_and_root(args)

    archive_all = getattr(args, "all", False)
    auto_yes = getattr(args, "yes", False)

    if archive_all:
        # Find all closed/complete non-archived tickets
        tickets = parser.load_all_tickets(store, include_archive=False)
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
            store.archive(path)
        print(f"Archived {len(to_archive)} ticket(s).")
    else:
        ticket_id = args.ticket_id
        path, fm, body = _require_ticket(store, ticket_id)

        if fm.get("status") not in parser.RESOLVED_STATUSES:
            print(f"Error: {fm['id']} is '{fm['status']}'. Only complete or closed tickets can be archived.", file=sys.stderr)
            raise SystemExit(1)

        store.archive(path)
        print(f"Archived {fm['id']} -> tickets/archive/{path.name}")


def cmd_delete(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    ticket_id = fm["id"]
    auto_yes = getattr(args, "yes", False)

    # Find relationships
    all_tickets = parser.load_all_tickets(store, include_archive=False)
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
            store.write(t_path, t_fm, t_body)

    store.delete(path)
    print(f"Deleted {ticket_id}.")


TODO_BLOB = "TODO.md"


def cmd_todo(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    # Determine action
    add_text = getattr(args, "add", None)
    rm_id = getattr(args, "rm", None)
    list_todos = getattr(args, "list", False)
    interactive = getattr(args, "interactive", False)

    use_json = getattr(args, "json", False)

    if add_text:
        _todo_add(store, add_text, use_json=use_json)
    elif rm_id is not None:
        _todo_rm(store, rm_id, use_json=use_json)
    elif list_todos:
        _todo_list(store, use_json=use_json)
    elif interactive:
        _todo_interactive(store)
    else:
        # No flags -- show help hint
        print("Usage: llpm todo --add \"text\" | --rm <id> | --list | --interactive")
        print("Run 'llpm todo --help' for details.")
        raise SystemExit(1)


def _todo_parse(store: TicketStore) -> list[tuple[int, str]]:
    """Parse TODO.md, returning list of (id, text) tuples."""
    text = store.read_blob(TODO_BLOB)
    if text is None:
        return []
    items = []
    for line in text.splitlines():
        m = re.match(r"^- \((\d+)\) (.+)$", line)
        if m:
            items.append((int(m.group(1)), m.group(2)))
    return items


def _todo_write(store: TicketStore, items: list[tuple[int, str]]) -> None:
    """Write TODO.md from list of (id, text) tuples."""
    lines = [f"- ({item_id}) {text}" for item_id, text in items]
    store.write_blob(TODO_BLOB, "\n".join(lines) + "\n" if lines else "")


def _todo_next_id(items: list[tuple[int, str]]) -> int:
    """Get next stable ID (never reuses)."""
    if not items:
        return 1
    return max(item_id for item_id, _ in items) + 1


def _todo_add(store: TicketStore, text: str, *, use_json: bool = False) -> None:
    items = _todo_parse(store)
    new_id = _todo_next_id(items)
    items.append((new_id, text))
    _todo_write(store, items)
    if use_json:
        _json_out({"id": new_id, "text": text})
    else:
        print(f"({new_id}) {text}")


def _todo_rm(store: TicketStore, item_id: int, *, use_json: bool = False) -> None:
    items = _todo_parse(store)
    matching = [(i, t) for i, t in items if i == item_id]
    if not matching:
        print(f"Error: TODO ({item_id}) not found.", file=sys.stderr)
        raise SystemExit(1)
    removed_text = matching[0][1]
    items = [(i, t) for i, t in items if i != item_id]
    _todo_write(store, items)
    if use_json:
        _json_out({"id": item_id, "text": removed_text, "removed": True})
    else:
        print(f"Removed ({item_id}): {removed_text}")


def _todo_list(store: TicketStore, *, use_json: bool = False) -> None:
    items = _todo_parse(store)
    if use_json:
        _json_out([{"id": item_id, "text": text} for item_id, text in items])
        return
    if not items:
        print("TODO: empty")
        return
    print(f"TODO ({len(items)} items):")
    for item_id, text in items:
        print(f"  ({item_id}) {text}")


def _todo_interactive(store: TicketStore) -> None:
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
            items = _todo_parse(store)
            new_id = _todo_next_id(items)
            items.append((new_id, line.strip()))
            _todo_write(store, items)
            print(f"  ({new_id}) {line.strip()}")
            count += 1
    except KeyboardInterrupt:
        print()
    if count:
        print(f"Added {count} item(s).")


def cmd_project(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    use_json = getattr(args, "json", False)

    tickets = parser.load_all_tickets(store, include_archive=True)

    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for path, fm, body in tickets:
        eff = parser.effective_status(store, fm)
        by_status[eff] = by_status.get(eff, 0) + 1
        t = fm.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    # Discover custom types from templates (local-dir stores only; vault uses bundled)
    valid_types = sorted(parser.TYPE_PREFIXES.keys())
    templates_dir = _project_templates(docs_root)
    if templates_dir.exists():
        for tmpl in templates_dir.glob("*.md"):
            t = tmpl.stem
            if t not in valid_types:
                valid_types.append(t)
        valid_types = sorted(valid_types)

    is_vault = isinstance(store, MdTreeStore)
    root_label = store._ns if is_vault else str(docs_root)
    tickets_label = f"{root_label}/tickets" if not is_vault else f"{root_label}.tasks|features|epics|research"
    archive_label = f"{root_label}/tickets/archive" if not is_vault else f"{root_label}.archive"

    data = {
        "llpm_root": root_label,
        "tickets_dir": tickets_label,
        "archive_dir": archive_label,
        "valid_statuses": sorted(parser.VALID_STATUSES),
        "resolved_statuses": sorted(parser.RESOLVED_STATUSES),
        "valid_types": valid_types,
        "valid_priorities": sorted(parser.VALID_PRIORITIES),
        "valid_efforts": sorted(parser.VALID_EFFORTS),
        "counts": {
            "total": len(tickets),
            "by_status": by_status,
            "by_type": by_type,
        },
    }

    if use_json:
        _json_out(data)
        return

    # Text output
    print(f"LLPM Root:    {root_label}")
    print(f"Tickets:      {tickets_label}")
    print(f"Archive:      {archive_label}")
    print(f"Total:        {len(tickets)}")
    print()
    print("By status:")
    for s, count in sorted(by_status.items()):
        print(f"  {s:<15} {count}")
    print()
    print("By type:")
    for t, count in sorted(by_type.items()):
        print(f"  {t:<15} {count}")


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
