"""Command implementations for LLPM CLI.

Commands are printers: they resolve a store from the environment, call
``service.py`` for anything that touches ticket data, and render the result in
llpm's terminal idiom. Rules, validation and the JSON shape live in the service
so the CLI and the HTTP API (``api.py``) can't drift apart.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from datetime import date
from importlib import resources as importlib_resources
from pathlib import Path

import yaml

from . import parser, service
from .service import (
    _as_hours,
    _coerce_number,
    _merge_commits,
    _priority_key,
    _slugify,
    _split_keys,
    _TEXT_FIELDS,
)
from .service import children_index as _children_index
from .service import inject_provenance as _inject_provenance
from .service import ticket_dict as _ticket_to_dict
from .service import write_ticket as _write_ticket
from .store import LocalDirStore, MdTreeStore, TicketStore


# -- Helpers --

def _today() -> str:
    """Return today's date as YYYY-MM-DD. Mockable in tests."""
    return date.today().isoformat()


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

    Returns a config dict on success (keys: ``kind``, plus kind-specific keys,
    plus ``"intake"``), or None if no config file was found.

    Supported kinds:
    - ``"dir"``: local filesystem store; includes ``"docs_root"`` (Path).
    - ``"mdtree"``: vault HTTP store; includes ``"base_url"`` (str),
      ``"repo_stem"`` (str), and ``"ca"`` (str path to a CA bundle, or None).

    ``"intake"`` is the raw ``[intake]`` table (e.g. ``{"auto_approve": [...]}``,
    FEAT-011's policy-as-data) -- ``{}`` when the section is absent.
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

            intake = data.get("intake", {})
            store_section = data.get("store", {})
            kind = store_section.get("kind", "dir")

            if kind == "dir":
                root_str = store_section.get("root", "./llpm")
                docs_root = (current / root_str).resolve()
                return {"kind": "dir", "docs_root": docs_root, "intake": intake}

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
                    "intake": intake,
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

    Resolution order for the *store location* (kind + docs_root/etc.):
    1. --docs-root flag  (forces kind=dir)
    2. LLPM_DOCS_ROOT env var  (forces kind=dir)
    3. In-repo .llpm/config.toml (walk upward from CWD)
    4. Default: ./llpm dir, kind=dir (no error — _require_initialized handles it)

    ``[intake]`` policy is resolved separately from store location: branches 1
    and 2 override *where the store lives*, not which board's policy applies,
    so they still consult the discovered .llpm/config.toml (if any) for
    ``"intake"``. This matters for the task fabric, where LLPM_DOCS_ROOT is
    the box-spawn env contract — board policy (`require_goal`, `auto_approve`)
    must not silently vanish just because the store location came from the
    env var instead of config discovery.

    A malformed/unknown-kind config found during that lookup still raises
    SystemExit (via `_find_repo_config`), even though its `[store]` table is
    about to be ignored in favor of the override — a broken config.toml
    should fail loudly, not be silently ignored just because this particular
    command happened to bypass its store section.
    """
    override_root: Path | None = None
    if hasattr(args, "docs_root") and args.docs_root:
        override_root = Path(args.docs_root).resolve()
    else:
        env = os.environ.get("LLPM_DOCS_ROOT")
        if env:
            override_root = Path(env).resolve()

    if override_root is not None:
        repo_config = _find_repo_config()
        intake = repo_config["intake"] if repo_config is not None else {}
        return {"kind": "dir", "docs_root": override_root, "intake": intake}

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


def _resolve_intake_config(args) -> dict:
    """Resolve the ``[intake]`` policy table from ``.llpm/config.toml``
    (FEAT-011: policy-as-data, not hardcoded). ``{}`` -- so an empty
    auto-approve list -- when no config file/section is found: agent
    tickets land draft by default until a project explicitly opts a kind in.
    """
    return _resolve_store_config(args).get("intake") or {}


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


_REQUIRE_GOAL_MODES = {"off", "warn", "enforce"}


def _resolve_require_goal(args) -> str:
    """Resolve `[intake] require_goal` (off|warn|enforce, default "warn").

    Goal attachment is never enforced at *creation* -- ruling from Ben+fable
    (2026-08-01) overriding the original FEAT-011 spec: creation always
    succeeds for agent-origin tickets regardless of attachment. This value
    only governs the pull-based `llpm orphans`/`llpm goals` report:
    "off" mutes it for boards that don't track goals at all; "warn"
    (default) surfaces unattached agent tickets there, informational only;
    "enforce" is the same report today, but is the per-board opt-in for a
    *future* dispatcher (`llpm next`, FEAT-009) to refuse to select orphaned
    agent tickets as ready work -- "reconciler refuses to dispatch orphans,"
    not "create fails." llpm has no dispatcher yet, so "enforce" has no
    behavioral teeth in this codebase today beyond labeling the report.
    """
    mode = _resolve_intake_config(args).get("require_goal", "warn")
    if mode not in _REQUIRE_GOAL_MODES:
        print(
            f"Error: Invalid [intake] require_goal '{mode}'. Must be one of: "
            f"{', '.join(sorted(_REQUIRE_GOAL_MODES))}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return mode


def _harvest_commits(ticket_id: str) -> list[str]:
    """Full SHAs of commits in the CWD repo whose subject line mentions the
    ticket ID as a whole token, oldest first. Best-effort: no git, no repo,
    or a timeout -> [].

    This formalizes the ticket-IDs-in-commit-subjects convention: the soft
    links become durable ``commits:`` entries at review/complete time. Body
    mentions don't count -- a commit that mentions this ID only in its body
    (e.g. because it references a related ticket) is not captured; that's
    what explicit --commit is for.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H%x1f%s", f"--grep={ticket_id}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    pattern = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(ticket_id)}(?![0-9])")
    shas = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        if pattern.search(subject):
            shas.append(sha)
    return shas[::-1]


@contextmanager
def _cli_errors():
    """Render any service error the CLI way: one ``Error: …`` line, exit 1.

    The service raises typed errors carrying llpm's existing message text, so a
    command that becomes a service caller keeps printing exactly what it printed
    before -- and the API renders the same errors as status codes.
    """
    try:
        yield
    except service.ServiceError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from e


def _require_ticket(store: TicketStore, ticket_id: str) -> tuple[Path, dict, str]:
    """Find and parse a ticket, or exit with error."""
    with _cli_errors():
        return service.read_ticket(store, ticket_id)


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

    use_json = getattr(args, "json", False)

    # The board is loaded whole (filtering is pure, and children resolve against
    # every ticket), so an empty board is still distinguishable from filters
    # that matched nothing -- two different messages in the table view.
    board = service.load_board(
        store, include_archive=getattr(args, "include_archived", False)
    )
    with _cli_errors():
        filtered = service.filter_tickets(
            board,
            status=getattr(args, "status", None),
            type=getattr(args, "type", None),
            parent=getattr(args, "parent", None),
        )

    if use_json:
        _json_out(filtered)
        return

    if not board:
        print("No tickets found.")
        return

    if not filtered:
        print("No tickets match the filters.")
        return

    # Print table
    print(f"{'ID':<16} {'Type':<11} {'Status':<15} {'Priority':<11} Title")
    print("-" * 75)
    for t in filtered:
        tier = t["model_tier"]
        tier_chip = f" [{tier}]" if tier else ""
        print(f"{t['id']:<16} {t['type']:<11} {t['effective_status']:<15} "
              f"{t['priority']:<11} {t['title']}{tier_chip}")


def cmd_board(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    use_json = getattr(args, "json", False)
    tickets = parser.load_all_tickets(store, include_archive=False)
    columns = {"blocked": [], "open": [], "in-progress": [], "review": []}

    for path, fm in tickets:
        eff_status = parser.effective_status(store, fm)
        if eff_status in columns:
            columns[eff_status].append((path, fm))

    for items in columns.values():
        items.sort(key=lambda item: _priority_key(item[1]))

    if use_json:
        idx = _children_index(tickets)
        result = []
        for col_name in ("blocked", "open", "in-progress", "review"):
            for path, fm in columns[col_name]:
                result.append(_ticket_to_dict(store, path, fm, children_by_parent=idx))
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
                # FEAT-012: awaiting chip, review column only -- makes review
                # a filterable set of queues (reviewer/Ben/CI-actionable)
                # without splitting it into separate board columns.
                awaiting_chip = (
                    f"  [awaiting: {fm['awaiting']}]"
                    if col_name == "review" and fm.get("awaiting") else ""
                )
                print(f"  {indicator} {fm['id']:<16} {fm['title']}{tier_chip}{serves_chip}{awaiting_chip}")
        print()


def cmd_backlog(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    use_json = getattr(args, "json", False)
    tickets = parser.load_all_tickets(store, include_archive=False)
    sections = {"planned": [], "draft": []}

    for path, fm in tickets:
        status = fm.get("status")
        if status in sections:
            sections[status].append((path, fm))

    if use_json:
        idx = _children_index(tickets)
        result = []
        for section in ("planned", "draft"):
            for path, fm in sections[section]:
                result.append(_ticket_to_dict(store, path, fm, children_by_parent=idx))
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

    if getattr(args, "json", False):
        with _cli_errors():
            _json_out(service.get_ticket(store, args.ticket_id))
        return

    # The text view prints more than the ticket dict carries (blocker titles and
    # statuses, whether `effort:` is present at all), so it renders from the
    # frontmatter the service hands back.
    path, fm, body = _require_ticket(store, args.ticket_id)
    eff_status = parser.effective_status(store, fm)

    print(f"ID:        {fm['id']}")
    print(f"Type:      {fm['type']}")
    print(f"Title:     {fm['title']}")
    print(f"Status:    {eff_status}")
    if fm.get("awaiting"):
        print(f"Awaiting:  {fm['awaiting']}")
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
    """Resolve the environment, call, print. The rules live in
    ``service.create_ticket``.

    Provenance inference stays here: ``LLPM_ORIGIN`` / ``LLPM_CREATED_BY`` and
    the human-by-default rule are about a *shell*, and so is the ``[intake]``
    policy, which lives in the repo's ``.llpm/config.toml``. The service is
    handed both answers rather than reaching for an environment a server
    doesn't have.
    """
    store, docs_root = _resolve_store_and_root(args)
    origin, created_by = _resolve_provenance(args)

    with _cli_errors():
        result = service.create_ticket(
            store,
            args.ticket_type,
            args.title,
            body=_read_body(args),
            parent=getattr(args, "parent", None),
            priority=getattr(args, "priority", None),
            effort=getattr(args, "effort", None),
            tags=getattr(args, "tags", None),
            requires_human=getattr(args, "requires_human", False),
            origin=origin,
            created_by=created_by,
            serves=getattr(args, "serves", None),
            triage=getattr(args, "triage", False),
            auto_approve=_resolve_intake_config(args).get("auto_approve") or [],
            today=_today(),
        )

    print(f"Created {result['id']}: {result['title']}")
    print(f"File: {result['path']}")


def cmd_status(args) -> None:
    """Harvest, call, print. The rules live in ``service.set_status``.

    Harvesting stays here because it needs a CWD git repo: explicit --commit
    SHAs record on any status change, and auto-harvest runs at review AND
    complete, so the worker's git context is used even when someone else later
    flips complete elsewhere. The server never runs git -- it passes commits in.
    """
    store, docs_root = _resolve_store_and_root(args)

    new_status = args.new_status
    explicit = list(getattr(args, "commit", None) or [])
    auto_harvest = new_status in ("review", "complete")
    harvested = _harvest_commits(args.ticket_id.upper()) if auto_harvest else []

    with _cli_errors():
        result = service.set_status(
            store,
            args.ticket_id,
            new_status,
            awaiting=getattr(args, "awaiting", None),
            commits=harvested + explicit,
            today=_today(),
        )

    print(f"{result['id']}: {result['previous_status']} -> {result['status']}")
    if result["commits_captured"]:
        print(f"Captured {result['commits_captured']} commit(s) -> commits[]")


def cmd_set(args) -> None:
    """Parse ``field=value`` argv, call, print one line per change.

    Splitting the assignments is genuinely argv's business; every rule about
    what a field may hold -- the enums, the null spellings, numeric coercion,
    the refusals -- lives in ``service.set_fields``, which an HTTP PATCH hits
    with the same values already typed.
    """
    store, docs_root = _resolve_store_and_root(args)

    fields = {}
    for assignment in args.assignments:
        if "=" not in assignment:
            # Legacy single-field syntax: llpm set ID field value
            print(f"Error: Use field=value syntax (e.g., 'priority=high').", file=sys.stderr)
            raise SystemExit(1)
        field, value = assignment.split("=", 1)
        fields[field.strip()] = value.strip()

    with _cli_errors():
        result = service.set_fields(store, args.ticket_id, fields, today=_today())

    for change in result["changes"]:
        print(f"{result['id']}: {change['field']} = {change['value']} "
              f"(was {change['previous']})")


def cmd_serves_add(args) -> None:
    store, docs_root = _resolve_store_and_root(args)
    goal_stem = args.goal_stem.strip()

    with _cli_errors():
        result = service.serves_add(store, args.ticket_id, goal_stem, today=_today())

    if not result["changed"]:
        print(f"{result['id']}: already serves '{goal_stem}'.")
        return
    print(f"{result['id']}: now serves '{goal_stem}'")


def cmd_serves_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)
    goal_stem = args.goal_stem.strip()

    with _cli_errors():
        result = service.serves_rm(store, args.ticket_id, goal_stem, today=_today())

    print(f"{result['id']}: no longer serves '{goal_stem}'")


def cmd_after_add(args) -> None:
    store, docs_root = _resolve_store_and_root(args)
    other_id = args.after

    with _cli_errors():
        result = service.after_add(store, args.ticket_id, other_id, today=_today())

    if not result["changed"]:
        print(f"{result['id']}: already ordered after '{other_id}'.")
        return
    # Soft cycles warn, never error: the edge is advice, not a constraint.
    if result["cycle_warning"]:
        print(f"Warning: soft ordering cycle -- '{other_id}' already comes after "
              f"{result['id']}. Edge added anyway.")
    print(f"{result['id']}: now ordered after '{other_id}' (soft -- never blocks)")


def cmd_after_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)
    other_id = args.after

    with _cli_errors():
        result = service.after_rm(store, args.ticket_id, other_id, today=_today())

    print(f"{result['id']}: no longer ordered after '{other_id}'")


def cmd_waits_add(args) -> None:
    store, docs_root = _resolve_store_and_root(args)
    target = args.on.strip()

    with _cli_errors():
        result = service.waits_add(store, args.ticket_id, target, today=_today())

    if not result["changed"]:
        print(f"{result['id']}: already waits on '{target}'.")
        return

    # Best-effort feedback on the target's current state; never fails the add.
    if result["state"] == "ok":
        print(f"{result['id']}: now waits on '{target}' "
              f"(currently: {result['target_status']})")
    elif result["state"] == "missing":
        print(f"{result['id']}: now waits on '{target}'")
        print(f"Warning: '{target}' not found in the vault -- blocking until it exists.")
    else:
        print(f"{result['id']}: now waits on '{target}' "
              f"(target status unknown from this store)")


def cmd_waits_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)
    target = args.on.strip()

    with _cli_errors():
        result = service.waits_rm(store, args.ticket_id, target, today=_today())

    print(f"{result['id']}: no longer waits on '{target}'")


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
    blocker_id = args.blocked_by

    with _cli_errors():
        result = service.blocker_add(store, args.ticket_id, blocker_id, today=_today())

    if not result["changed"]:
        print(f"{result['id']}: already blocked by '{blocker_id}'.")
        return
    print(f"{result['id']}: now blocked by '{blocker_id}'")


def cmd_blocker_rm(args) -> None:
    store, docs_root = _resolve_store_and_root(args)
    blocker_id = args.blocked_by

    with _cli_errors():
        result = service.blocker_rm(store, args.ticket_id, blocker_id, today=_today())

    print(f"{result['id']}: removed blocker '{blocker_id}'")


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
        for path, fm in tickets:
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


# Notes below a ticket are meant to be spammed (dozens per worker is normal);
# the delete prompt names a few and counts the rest.
_SUBNOTE_PREVIEW = 5


def cmd_delete(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    path, fm, body = _require_ticket(store, args.ticket_id)
    ticket_id = fm["id"]
    auto_yes = getattr(args, "yes", False)

    # Find relationships
    all_tickets = parser.load_all_tickets(store, include_archive=False)
    references = []
    children_of = []

    for t_path, t_fm in all_tickets:
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

    subnotes = parser.get_subnotes(store, path)
    if subnotes:
        print(f"Deleting will also remove {len(subnotes)} note(s) below this ticket:")
        for name in subnotes[:_SUBNOTE_PREVIEW]:
            print(f"  - {name}")
        if len(subnotes) > _SUBNOTE_PREVIEW:
            print(f"  ... and {len(subnotes) - _SUBNOTE_PREVIEW} more")
        print()

    if not auto_yes:
        response = input("Delete? [y/N]: ").strip().lower()
        if response != "y":
            print("Cancelled.")
            return

    # Clean up references
    for t_path, t_fm in all_tickets:
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
            # The board load carries no bodies; read the one ticket being
            # rewritten so its body goes back untouched. A delete touches a
            # handful of referencing tickets, so this is a handful of reads --
            # not one per ticket on the board, which is what it replaced.
            _, t_body = store.read_ref(t_path)
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
    for path, fm in tickets:
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


def cmd_orphans(args) -> None:
    store, docs_root = _resolve_store_and_root(args)

    use_json = getattr(args, "json", False)
    mode = _resolve_require_goal(args)

    if mode == "off":
        if use_json:
            _json_out({"require_goal": mode, "orphans": []})
        else:
            print('Goal-attachment tracking is off for this board ([intake] require_goal = "off").')
        return

    orphans = parser.get_orphans(store)

    if use_json:
        _json_out({"require_goal": mode, "orphans": orphans})
        return

    if not orphans:
        print("No orphaned agent-created tickets.")
        return

    consequence = (
        "NOT dispatch-eligible once llpm next enforces this (FEAT-009)" if mode == "enforce"
        else "informational only, does not block anything yet"
    )
    print(f"-- {len(orphans)} orphaned agent-created ticket(s) (require_goal={mode}: {consequence}) --")
    for o in orphans:
        by = f"  (created_by: {o['created_by']})" if o.get("created_by") else ""
        print(f"  {o['id']:<12} {o['title']}  [{o['status']}]{by}")


# -- serve (FEAT-015) --

# FastAPI and uvicorn are an optional extra: the core install stays pyyaml-only,
# so `llpm --help` (and every other command) works without them. Nothing outside
# cmd_serve imports llpm.api.
_SERVE_EXTRA_HINT = (
    "Error: 'llpm serve' needs the optional API extra -- install it with "
    "`uv tool install --editable '.[api]'`, `uv sync --extra api` in a checkout, "
    "or `pip install 'llpm[api]'`."
)

# A board name becomes a vault stem segment, so keep it to segment-safe
# characters rather than letting a request address arbitrary parts of the vault.
_BOARD_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

SERVE_DEFAULT_HOST = "127.0.0.1"
SERVE_DEFAULT_PORT = 8787


def _board_name(cfg: dict) -> str:
    """The name a board answers to over HTTP.

    For a vault store that is its repo stem, so this repo's board is served at
    ``/llpm/tickets`` -- the same name its stems already use. For a local dir it
    is the directory holding the docs root (``…/<repo>/llpm`` -> ``<repo>``).
    """
    if cfg["kind"] == "mdtree":
        return cfg["repo_stem"]
    return cfg["docs_root"].parent.name or "board"


def _serve_stores(args) -> tuple[callable, callable, str]:
    """Wire ``llpm serve`` to its board(s): ``(store_for, boards, label)``.

    Without ``--vault``: the single board from the usual config discovery,
    served under its own name; any other repo in the path is a 404. With
    ``--vault``: every board in that vault, one store cached per repo -- the
    cache is the point, since a store rebuilt per request throws away its
    connection to the vault.
    """
    cfg = _resolve_store_config(args)
    vault = getattr(args, "vault", None)

    if vault:
        ca = cfg.get("ca")
        cache: dict[str, TicketStore] = {}

        def store_for(repo: str) -> TicketStore:
            if not _BOARD_NAME_RE.match(repo):
                raise service.Invalid(f"Invalid board name: {repo!r}")
            if repo not in cache:
                cache[repo] = MdTreeStore(base_url=vault, repo_stem=repo, ca=ca)
            return cache[repo]

        # list_boards scans the whole vault, so the repo stem it was built with
        # is irrelevant -- this store exists only to ask that one question.
        discovery = MdTreeStore(base_url=vault, repo_stem="", ca=ca)
        return store_for, discovery.list_boards, f"every board in {vault}"

    docs_root: Path = cfg.get("docs_root", Path("/dev/null/mdtree-sentinel"))
    store = _make_store_from_config(cfg)
    _require_initialized(docs_root, store=store)
    board = _board_name(cfg)

    def store_for(repo: str) -> TicketStore:
        if repo != board:
            raise service.NotFound(
                f"No board {repo!r} on this server -- it serves {board!r}."
            )
        return store

    where = cfg["base_url"] if cfg["kind"] == "mdtree" else docs_root
    return store_for, (lambda: [board]), f"board {board!r} ({where})"


def cmd_serve(args) -> None:
    try:
        import uvicorn

        from . import api
    except ModuleNotFoundError as e:
        if e.name not in ("fastapi", "uvicorn"):
            raise
        print(_SERVE_EXTRA_HINT, file=sys.stderr)
        raise SystemExit(1)

    store_for, boards, label = _serve_stores(args)
    host = getattr(args, "host", None) or SERVE_DEFAULT_HOST
    port = getattr(args, "port", None) or SERVE_DEFAULT_PORT

    print(f"llpm serve: {label}")
    print(f"  http://{host}:{port}/boards")
    uvicorn.run(api.make_app(store_for, boards=boards), host=host, port=port)


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
