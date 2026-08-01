"""Command implementations for LLPM CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import date
from importlib import resources as importlib_resources
from pathlib import Path

import yaml

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


def _write_ticket(store: TicketStore, ref: Path, fm: dict, body: str) -> None:
    """Chokepoint for every ticket mutation: stamps the ownership key
    (``managed_by: llpm``, decision 6 -- the k8s kind/managed-by split) so
    tickets self-describe their write path, then writes through the store."""
    fm.setdefault("managed_by", "llpm")
    store.write(ref, fm, body)


def _resolve_provenance(args) -> tuple[str, str | None]:
    """Resolve (origin, created_by) for a new ticket.

    Precedence: CLI flag > env var. When origin is unsignaled it is inferred:
    'agent' if a created_by id is present (harnesses set LLPM_CREATED_BY),
    else 'human' -- a bare human shell needs no configuration.
    """
    created_by = getattr(args, "created_by", None) or os.environ.get("LLPM_CREATED_BY") or None
    origin = getattr(args, "origin", None) or os.environ.get("LLPM_ORIGIN") or None
    if origin is None:
        origin = "agent" if created_by else "human"
    if origin not in parser.VALID_ORIGINS:
        print(
            f"Error: Invalid origin '{origin}'. Must be one of: "
            f"{', '.join(sorted(parser.VALID_ORIGINS))} (check LLPM_ORIGIN).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return origin, created_by


def _inject_provenance(content: str, origin: str, created_by: str | None) -> str:
    """Append provenance + ownership keys to rendered template frontmatter.

    Textual insertion (not re-serialization) so template enum-hint comments
    survive into the created ticket. Keys the template already carries are
    left alone.
    """
    try:
        fm, _ = parser.parse_text(content, source="<template>")
    except (ValueError, yaml.YAMLError):
        return content  # malformed template: let create fail/succeed as before

    lines = []
    if "managed_by" not in fm:
        lines.append("managed_by: llpm")
    if "origin" not in fm:
        lines.append(f"origin: {origin}")
    if created_by and "created_by" not in fm:
        # created_by is caller input -- serialize the one line properly
        lines.append(yaml.safe_dump({"created_by": created_by}, default_flow_style=False).strip())
    if "commits" not in fm:
        lines.append("commits: []")
    if not lines:
        return content

    parts = content.split("---", 2)
    fm_block = parts[1].rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    return "---" + fm_block + "---" + parts[2]


def _harvest_commits(ticket_id: str) -> list[str]:
    """Full SHAs of commits in the CWD repo that mention the ticket ID,
    oldest first. Best-effort: no git, no repo, or a timeout -> [].

    This formalizes the ticket-IDs-in-commit-messages convention: the soft
    links become durable ``commits:`` entries at review/complete time.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H", f"--grep={ticket_id}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    shas = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return shas[::-1]


def _merge_commits(existing: list[str], new_shas: list[str]) -> list[str]:
    """Append new SHAs, prefix-aware so short and full forms of the same
    commit don't both land in the list. Never removes entries."""
    merged = list(existing)
    for sha in new_shas:
        sha = sha.strip()
        if not sha:
            continue
        if not any(m.startswith(sha) or sha.startswith(m) for m in merged):
            merged.append(sha)
    return merged


def _require_ticket(store: TicketStore, ticket_id: str) -> tuple[Path, dict, str]:
    """Find and parse a ticket, or exit with error."""
    result = store.read(ticket_id)
    if result is None:
        print(f"Error: Ticket '{ticket_id}' not found.", file=sys.stderr)
        raise SystemExit(1)
    return result


def _ticket_to_dict(store: TicketStore, path: Path, fm: dict, body: str | None = None) -> dict:
    """Serialize a ticket to the JSON output schema.

    If body is None, it is omitted (list mode). If provided, it is included (show mode).
    """
    eff_status = parser.effective_status(store, fm)
    is_blocked = eff_status == "blocked"

    children = parser.get_children(store, fm["id"])
    child_ids = [c["id"] for c in children]

    blocker_details = parser.get_blocker_details(store, fm) if fm.get("blockers") else []

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
        "model_tier": fm.get("model_tier"),
        "parent": fm.get("parent"),
        "children": child_ids,
        "blockers": [
            {"id": d["id"], "resolved": d["resolved"]}
            for d in blocker_details
        ],
        "serves": fm.get("serves") or [],
        "waits_on": parser.get_waits_on_details(store, fm),
        "after": fm.get("after") or [],
        "tags": fm.get("tags") or [],
        "requires_human": fm.get("requires_human", False),
        "origin": fm.get("origin"),
        "created_by": fm.get("created_by"),
        "commits": fm.get("commits") or [],
        "managed_by": fm.get("managed_by"),
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


_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _priority_key(fm: dict) -> tuple[int, str]:
    """Sort key for list/board output: priority high->low, then ID."""
    return (_PRIORITY_RANK.get(fm.get("priority"), 1), fm.get("id") or "")


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

    filtered.sort(key=lambda item: _priority_key(item[1]))

    if use_json:
        _json_out([_ticket_to_dict(store, path, fm) for path, fm, _ in filtered])
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

    for items in columns.values():
        items.sort(key=lambda item: _priority_key(item[1]))

    if use_json:
        result = []
        for col_name in ("blocked", "open", "in-progress", "review"):
            for path, fm in columns[col_name]:
                result.append(_ticket_to_dict(store, path, fm))
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
                tier = fm.get("model_tier")
                tier_chip = f" [{tier}]" if tier else ""
                serves = fm.get("serves") or []
                serves_chip = f"  (serves: {', '.join(serves)})" if serves else ""
                print(f"  {indicator} {fm['id']:<16} {fm['title']}{tier_chip}{serves_chip}")
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
                result.append(_ticket_to_dict(store, path, fm))
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
        _json_out(_ticket_to_dict(store, path, fm, body=body))
        return

    eff_status = parser.effective_status(store, fm)

    print(f"ID:        {fm['id']}")
    print(f"Type:      {fm['type']}")
    print(f"Title:     {fm['title']}")
    print(f"Status:    {eff_status}")
    print(f"Priority:  {fm['priority']}")

    if "effort" in fm:
        print(f"Effort:    {fm['effort'] or '-'}")
    if fm.get("model_tier"):
        print(f"Tier:      {fm['model_tier']}")
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

    # Cross-board waits with resolution state
    waits_details = parser.get_waits_on_details(store, fm)
    if waits_details:
        parts = []
        for d in waits_details:
            label = d["status"] or d["state"]
            parts.append(f"{d['stem']} ({label}) {_waits_tag(d)}")
        print(f"Waits on:  {', '.join(parts)}")

    # Soft precedence -- ordering advice, never blocks
    after = fm.get("after") or []
    if after:
        print(f"After:     {', '.join(after)} (soft)")

    # Goal references (epics/features)
    serves = fm.get("serves") or []
    if serves or fm.get("type") in parser.SERVES_TYPES:
        print(f"Serves:    {', '.join(serves) if serves else '-'}")

    print(f"Created:   {fm.get('created') or '-'}")
    print(f"Updated:   {fm.get('updated') or '-'}")
    print(f"Completed: {fm.get('completed') or '-'}")

    # Provenance (FEAT-007)
    if fm.get("origin") or fm.get("created_by"):
        by = f" (by {fm['created_by']})" if fm.get("created_by") else ""
        print(f"Origin:    {fm.get('origin') or '-'}{by}")
    commits = fm.get("commits") or []
    if commits:
        print(f"Commits:   {', '.join(c[:10] for c in commits)}")

    tags = fm.get("tags") or []
    print(f"Tags:      {', '.join(tags) if tags else '-'}")
    location = path.resolve() if isinstance(path, Path) else path
    print(f"File:      {location}")
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

        # Provenance + ownership (FEAT-007)
        origin, created_by = _resolve_provenance(args)
        content = _inject_provenance(content, origin, created_by)

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

    # Commit capture (FEAT-007). Explicit --commit SHAs record on any status
    # change; auto-harvest runs at review AND complete, so the worker's git
    # context is used even when someone else later flips complete elsewhere.
    explicit = list(getattr(args, "commit", None) or [])
    harvested = _harvest_commits(fm["id"]) if new_status in ("review", "complete") else []
    captured = 0
    if explicit or harvested:
        existing = list(fm.get("commits") or [])
        merged = _merge_commits(existing, harvested + explicit)
        captured = len(merged) - len(existing)
        if merged:
            fm["commits"] = merged

    _write_ticket(store, path, fm, body)
    print(f"{fm['id']}: {old_status} -> {new_status}")
    if captured:
        print(f"Captured {captured} commit(s) -> commits[]")


def cmd_set(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)

    # Restricted fields (provenance is written by the system, never by set)
    FORBIDDEN = {"id", "type", "created", "updated", "completed",
                 "origin", "created_by", "commits", "managed_by"}
    REDIRECT = {
        "status": "Use 'llpm status'.",
        "blockers": "Use 'llpm blocker'.",
        "serves": "Use 'llpm serves'.",
        "waits_on": "Use 'llpm waits'.",
        "after": "Use 'llpm after'.",
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

        if field == "model_tier":
            if value.lower() in ("null", "none"):
                value = None
            elif value not in parser.VALID_MODEL_TIERS:
                print(f"Error: Invalid model_tier '{value}'. Must be one of: {', '.join(sorted(parser.VALID_MODEL_TIERS))}", file=sys.stderr)
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
    _write_ticket(store, path, fm, body)


def _require_serves_capable(fm: dict) -> None:
    """Exit with an error unless the ticket type can carry `serves:`."""
    if fm.get("type") not in parser.SERVES_TYPES:
        print(
            f"Error: 'serves' is only valid on epics/features "
            f"({fm['id']} is a {fm.get('type')}). Tasks serve goals via their parent.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def cmd_serves_add(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    _require_serves_capable(fm)

    goal_stem = args.goal_stem.strip()
    # Catch the common mistake of passing a ticket ID instead of a vault stem.
    # Existence validation stays soft until type-keyed schema matching lands.
    if parser.TICKET_ID_RE.match(goal_stem.upper()):
        print(
            f"Error: 'serves' holds full vault stems to goal notes "
            f"(e.g. goals.unified-agent-platform), not ticket IDs. "
            f"For ticket dependencies use 'llpm blocker'.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    serves = fm.get("serves") or []
    if goal_stem in serves:
        print(f"{fm['id']}: already serves '{goal_stem}'.")
        return

    serves.append(goal_stem)
    fm["serves"] = serves
    fm["updated"] = _today()
    _write_ticket(store, path, fm, body)
    print(f"{fm['id']}: now serves '{goal_stem}'")


def cmd_serves_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)

    goal_stem = args.goal_stem.strip()
    serves = fm.get("serves") or []
    if goal_stem not in serves:
        print(f"Error: {fm['id']} does not serve '{goal_stem}'.", file=sys.stderr)
        raise SystemExit(1)

    fm["serves"] = [s for s in serves if s != goal_stem]
    fm["updated"] = _today()
    _write_ticket(store, path, fm, body)
    print(f"{fm['id']}: no longer serves '{goal_stem}'")


def _after_reaches(store: TicketStore, start_id: str, target_id: str) -> bool:
    """True if following `after` edges from start reaches target -- the
    soft-cycle probe for 'llpm after add'."""
    target = target_id.upper()
    seen: set[str] = set()
    frontier = [start_id.upper()]
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        found = store.read(current)
        if found is None:
            continue
        _, fm, _ = found
        frontier.extend(a.upper() for a in fm.get("after") or [])
    return False


def cmd_after_add(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    other_id = args.after

    # Soft edge, but still a real ticket reference -- same rule as blockers.
    if not store.exists(other_id):
        print(f"Error: Ticket '{other_id}' not found.", file=sys.stderr)
        raise SystemExit(1)

    after = fm.get("after") or []
    if any(a.upper() == other_id.upper() for a in after):
        print(f"{fm['id']}: already ordered after '{other_id}'.")
        return

    # Soft cycles warn, never error: the edge is advice, not a constraint.
    if _after_reaches(store, other_id, fm["id"]):
        print(f"Warning: soft ordering cycle -- '{other_id}' already comes after {fm['id']}. Edge added anyway.")

    after.append(other_id.upper())
    fm["after"] = after
    fm["updated"] = _today()
    _write_ticket(store, path, fm, body)
    print(f"{fm['id']}: now ordered after '{other_id}' (soft -- never blocks)")


def cmd_after_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    other_id = args.after

    after = fm.get("after") or []
    upper_id = other_id.upper()
    if not any(a.upper() == upper_id for a in after):
        print(f"Error: {fm['id']} is not ordered after '{other_id}'.", file=sys.stderr)
        raise SystemExit(1)

    fm["after"] = [a for a in after if a.upper() != upper_id]
    fm["updated"] = _today()
    _write_ticket(store, path, fm, body)
    print(f"{fm['id']}: no longer ordered after '{other_id}'")


# Display tag for each waits_on resolution state ('ok' depends on resolved)
_WAITS_STATE_TAGS = {
    "missing": "[MISSING]",
    "error": "[ERROR]",
    "unavailable": "[UNKNOWN]",
}


def _waits_tag(detail: dict) -> str:
    if detail["state"] == "ok":
        return "[RESOLVED]" if detail["resolved"] else "[BLOCKING]"
    return _WAITS_STATE_TAGS[detail["state"]]


def cmd_waits_add(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    target = args.on.strip()

    # Bare ticket IDs are intra-board dependencies -- that's what blockers are.
    if parser.TICKET_ID_RE.match(target.upper()):
        print(
            f"Error: 'waits_on' holds full vault stems "
            f"(e.g. repos.marginalia.llpm.features.FEAT-010). "
            f"For same-board dependencies use 'llpm blocker'.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    waits = fm.get("waits_on") or []
    if target in waits:
        print(f"{fm['id']}: already waits on '{target}'.")
        return

    waits.append(target)
    fm["waits_on"] = waits
    fm["updated"] = _today()
    _write_ticket(store, path, fm, body)

    # Best-effort feedback on the target's current state; never fails the add.
    state, target_fm = parser._read_foreign(store, target)
    if state == "ok":
        print(f"{fm['id']}: now waits on '{target}' (currently: {target_fm.get('status')})")
    elif state == "missing":
        print(f"{fm['id']}: now waits on '{target}'")
        print(f"Warning: '{target}' not found in the vault -- blocking until it exists.")
    else:
        print(f"{fm['id']}: now waits on '{target}' (target status unknown from this store)")


def cmd_waits_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    target = args.on.strip()

    waits = fm.get("waits_on") or []
    if target not in waits:
        print(f"Error: {fm['id']} does not wait on '{target}'.", file=sys.stderr)
        raise SystemExit(1)

    fm["waits_on"] = [w for w in waits if w != target]
    fm["updated"] = _today()
    _write_ticket(store, path, fm, body)
    print(f"{fm['id']}: no longer waits on '{target}'")


def cmd_waits_list(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    details = parser.get_waits_on_details(store, fm)

    if getattr(args, "json", False):
        _json_out({
            "id": fm["id"],
            "title": fm["title"],
            "waits_on": details,
        })
        return

    print(f"{fm['id']}: {fm['title']}")
    print()

    if not details:
        print("No cross-board waits.")
        return

    print("Waits on:")
    blocking = 0
    for d in details:
        tag = _waits_tag(d)
        if d["blocking"]:
            blocking += 1
        print(f"  {d['stem']:<48} {d['status'] or d['state']:<15} {tag}")

    print()
    if blocking > 0:
        print(f"Status: BLOCKED ({blocking} unresolved)")
    elif any(d["state"] == "unavailable" for d in details):
        print("Status: unknown (cross-board reads unavailable; not blocking)")
    else:
        print("Status: all waits resolved")


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
    _write_ticket(store, path, fm, body)
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
    _write_ticket(store, path, fm, body)
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
            _write_ticket(store, t_path, t_fm, t_body)

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
        "valid_model_tiers": sorted(parser.VALID_MODEL_TIERS),
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


def cmd_goals(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    use_json = getattr(args, "json", False)
    rollup = parser.get_goal_rollup(store)

    if use_json:
        _json_out(rollup)
        return

    if not rollup:
        print("No 'type: goal' notes found.")
        return

    for g in rollup:
        tag = "[stamped]" if g["stamped"] else "[draft -- proposal, not binding]"
        print(f"== {g['stem']}  {tag} ==")
        print(f"{g['title']}")
        if g["serving"]:
            parts = [f"{s['id']} ({s['status']})" for s in g["serving"]]
            print(f"  Serving (this board): {', '.join(parts)}")
        else:
            print(f"  Serving (this board): (none)")
        if g["total"]:
            counts_str = " ".join(f"{k}={v}" for k, v in sorted(g["counts"].items()))
            print(f"  {counts_str}  done={g['done']}/{g['total']} ({g['pct_done']}%)")
        if g["unplanned_gap"]:
            print(f"  [UNPLANNED GAP] no workable serving tickets -- plan with Ben, don't freelance")
        print()

    gaps = [g for g in rollup if g["unplanned_gap"]]
    print(f"-- {len(gaps)} unplanned gap(s) --")
    for g in gaps:
        print(f"  {g['stem']}  {g['title']}")


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
