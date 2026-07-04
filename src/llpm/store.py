"""TicketStore protocol and LocalDirStore implementation for LLPM.

This module defines the storage seam that all ticket I/O flows through.
Parsing/serialization of frontmatter stays in parser.py (pure text logic);
derived-at-read invariants (blocked, children, effective status) stay above
this seam -- they compose store reads, they are not store methods.

Refs returned by ``list_tickets`` are Path-like objects (``.name``/``.stem``
must work). For LocalDirStore they are real ``pathlib.Path`` objects, which
is what the CLI prints today -- zero behavior change.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import parser as _parser


@runtime_checkable
class TicketStore(Protocol):
    """Minimal storage abstraction for ticket data.

    Filename conventions are part of the contract: tickets are named
    ``<TYPE>-NNN_SLUG.md``; archived tickets live under an ``archive``
    sub-location but keep their filename.
    """

    def list_tickets(self, include_archive: bool = True) -> list[Path]:
        """Return a sorted list of Path-like ticket refs."""
        ...

    def read(self, ticket_id: str) -> tuple[Path, dict, str] | None:
        """Find a ticket by ID (case-insensitive filename prefix match) and
        parse it. Returns (ref, frontmatter, body), or None if not found.
        Parse errors propagate (ValueError / yaml.YAMLError)."""
        ...

    def read_ref(self, ref: Path) -> tuple[dict, str]:
        """Parse the ticket at a ref previously returned by list_tickets."""
        ...

    def write(self, ref: Path, frontmatter: dict, body: str) -> None:
        """Overwrite the ticket at ref with new frontmatter + body."""
        ...

    def create_exclusive(self, filename: str, content: str) -> Path:
        """Create a new ticket atomically (O_EXCL semantics). Raises
        FileExistsError if the name is taken. Returns the new ref."""
        ...

    def archive(self, ref: Path) -> Path:
        """Move the ticket at ref into the archive. Returns the new ref."""
        ...

    def delete(self, ref: Path) -> None:
        """Remove the ticket at ref."""
        ...

    def read_blob(self, name: str) -> str | None:
        """Read a named blob relative to the docs root (e.g. 'TODO.md',
        'templates/task.md'). Returns None if it does not exist."""
        ...

    def write_blob(self, name: str, text: str) -> None:
        """Write a named blob relative to the docs root."""
        ...

    def exists(self, ticket_id: str) -> bool:
        """True if a ticket with this ID exists (active or archived)."""
        ...


class LocalDirStore:
    """Behavior-preserving filesystem implementation of TicketStore.

    Wraps the exact filesystem operations previously inlined in parser.py
    and commands.py -- including the O_CREAT|O_EXCL atomic create.
    """

    def __init__(self, docs_root: Path) -> None:
        self.docs_root = docs_root
        self.tickets_dir = docs_root / "tickets"
        self.archive_dir = self.tickets_dir / "archive"

    def list_tickets(self, include_archive: bool = True) -> list[Path]:
        if not self.tickets_dir.exists():
            return []

        results = list(self.tickets_dir.glob("*.md"))
        if include_archive and self.archive_dir.exists():
            results.extend(self.archive_dir.glob("*.md"))

        return sorted(results)

    def read(self, ticket_id: str) -> tuple[Path, dict, str] | None:
        ref = self._find(ticket_id)
        if ref is None:
            return None
        fm, body = self.read_ref(ref)
        return ref, fm, body

    def read_ref(self, ref: Path) -> tuple[dict, str]:
        return _parser.parse_document(ref)

    def write(self, ref: Path, frontmatter: dict, body: str) -> None:
        _parser.write_document(ref, frontmatter, body)

    def create_exclusive(self, filename: str, content: str) -> Path:
        filepath = self.tickets_dir / filename
        fd = os.open(str(filepath), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    def archive(self, ref: Path) -> Path:
        self.archive_dir.mkdir(exist_ok=True)
        dst = self.archive_dir / ref.name
        ref.rename(dst)
        return dst

    def delete(self, ref: Path) -> None:
        ref.unlink()

    def read_blob(self, name: str) -> str | None:
        path = self.docs_root / name
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def write_blob(self, name: str, text: str) -> None:
        (self.docs_root / name).write_text(text, encoding="utf-8")

    def exists(self, ticket_id: str) -> bool:
        return self._find(ticket_id) is not None

    def _find(self, ticket_id: str) -> Path | None:
        """Case-insensitive ID prefix match on filename, active + archive."""
        upper_id = ticket_id.upper()
        for ref in self.list_tickets(include_archive=True):
            if ref.name.upper().startswith(upper_id):
                return ref
        return None


# ---------------------------------------------------------------------------
# VaultRef — path-like ref for vault-stored tickets
# ---------------------------------------------------------------------------

# Maps ticket type key → plural sub-stem used in vault paths
_TYPE_STEMS: dict[str, str] = {
    "task": "tasks",
    "feature": "features",
    "epic": "epics",
    "research": "research",
}

# Reverse map: plural sub-stem → type key (used when parsing listed stems)
_STEM_TO_TYPE: dict[str, str] = {v: k for k, v in _TYPE_STEMS.items()}


@dataclass(frozen=True)
class VaultRef:
    """Path-like ref for a vault-stored ticket.

    Satisfies the ``.name``, ``.stem``, and ``.parts`` contract expected by
    llpm commands.  ``"archive" in ref.parts`` is the canonical archived-test.
    """

    vault_stem: str   # e.g. "repos.foo.llpm.tasks.TASK-001"
    is_archived: bool = False

    @property
    def name(self) -> str:
        """The ticket ID segment (last dot-segment of vault_stem)."""
        return self.vault_stem.split(".")[-1]

    @property
    def stem(self) -> str:
        """Same as name — no .md extension in vault refs."""
        return self.name

    @property
    def parts(self) -> tuple[str, ...]:
        """``"archive" in ref.parts`` detects archived tickets."""
        if self.is_archived:
            return ("archive", self.name)
        return (self.name,)

    def __str__(self) -> str:
        return self.vault_stem


# ---------------------------------------------------------------------------
# MdTreeStore — vault-backed TicketStore over markdown-tree-service HTTP API
# ---------------------------------------------------------------------------

class MdTreeStore:
    """TicketStore implementation that stores tickets in the agent-memory vault.

    Talks to the markdown-tree-service REST API using stdlib ``urllib`` only —
    no extra dependencies.

    Stem layout::

        repos.<repo_stem>.llpm.tasks.TASK-001
        repos.<repo_stem>.llpm.features.FEAT-001
        repos.<repo_stem>.llpm.epics.EPIC-001
        repos.<repo_stem>.llpm.research.RES-001
        repos.<repo_stem>.llpm.archive.TASK-001   (archived)
        repos.<repo_stem>.llpm.todo                (TODO blob)
        repos.<repo_stem>.llpm.templates.<type>    (template blobs)

    Errors are loud: connection failures propagate; 404 on a stem returns None
    from ``read()``; 409 on ``create_exclusive`` raises ``FileExistsError``.
    """

    def __init__(self, base_url: str, repo_stem: str) -> None:
        """
        Args:
            base_url:  e.g. ``"https://agent-memory.home.lab"``
            repo_stem: the repo namespace, e.g. ``"llpm"`` giving
                       ``repos.llpm.llpm.*`` stems.
        """
        self._base = base_url.rstrip("/")
        self._ns = f"repos.{repo_stem}.llpm"

    # -- Internal HTTP helpers ------------------------------------------------

    def _url(self, stem: str) -> str:
        return f"{self._base}/api/v1/notes/{urllib.parse.quote(stem, safe='')}"

    def _get_json(self, url: str) -> dict:
        with urllib.request.urlopen(url) as r:
            return json.loads(r.read())

    def _get_raw(self, stem: str) -> str | None:
        """Fetch raw markdown content for a stem. Returns None on 404."""
        url = self._url(stem) + "/raw"
        try:
            with urllib.request.urlopen(url) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _put(self, stem: str, content: str) -> None:
        """Upsert a note (overwrite if exists)."""
        data = json.dumps({"content": content}).encode()
        req = urllib.request.Request(
            self._url(stem),
            data=data,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req):
            pass

    def _put_exclusive(self, stem: str, content: str) -> None:
        """Create a note only if it doesn't exist. Raises FileExistsError on 409."""
        data = json.dumps({"content": content}).encode()
        url = self._url(stem) + "?if_absent=true"
        req = urllib.request.Request(
            url,
            data=data,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req):
                pass
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise FileExistsError(f"vault note already exists: {stem}") from e
            raise

    def _delete(self, stem: str) -> None:
        req = urllib.request.Request(self._url(stem), method="DELETE")
        with urllib.request.urlopen(req):
            pass

    def _move(self, stem: str, new_stem: str) -> None:
        url = (
            self._url(stem)
            + "/move?new_stem="
            + urllib.parse.quote(new_stem, safe="")
        )
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req):
            pass

    def _list_pattern(self, pattern: str) -> list[dict]:
        """List notes matching a glob pattern. Returns list of {stem, title} dicts."""
        url = (
            self._base
            + "/api/v1/notes?pattern="
            + urllib.parse.quote(pattern, safe="")
        )
        data = self._get_json(url)
        return data.get("items", [])

    # -- Parsing helpers ------------------------------------------------------

    @staticmethod
    def _parse(content: str, source: str = "<vault>") -> tuple[dict, str]:
        """Parse raw markdown text into (frontmatter, body)."""
        return _parser.parse_text(content, source=source)

    @staticmethod
    def _serialize(frontmatter: dict, body: str) -> str:
        return _parser.serialize_document(frontmatter, body)

    # -- Stem helpers ---------------------------------------------------------

    def _active_stem(self, type_key: str, ticket_id: str) -> str:
        type_sub = _TYPE_STEMS.get(type_key.lower(), type_key.lower() + "s")
        return f"{self._ns}.{type_sub}.{ticket_id.upper()}"

    def _archive_stem(self, ticket_id: str) -> str:
        return f"{self._ns}.archive.{ticket_id.upper()}"

    def _ref_for_stem(self, stem: str) -> VaultRef:
        """Build a VaultRef, detecting archive from stem structure."""
        parts = stem.split(".")
        # stem looks like: repos.<repo>.llpm.<sub>.<ID>
        # sub is index 3; ID is index 4
        sub = parts[3] if len(parts) > 3 else ""
        is_archived = sub == "archive"
        return VaultRef(vault_stem=stem, is_archived=is_archived)

    # -- TicketStore protocol -------------------------------------------------

    def list_tickets(self, include_archive: bool = True) -> list[VaultRef]:
        refs: list[VaultRef] = []

        for sub_stem in _TYPE_STEMS.values():
            pattern = f"{self._ns}.{sub_stem}.*"
            for item in self._list_pattern(pattern):
                refs.append(VaultRef(vault_stem=item["stem"], is_archived=False))

        if include_archive:
            pattern = f"{self._ns}.archive.*"
            for item in self._list_pattern(pattern):
                refs.append(VaultRef(vault_stem=item["stem"], is_archived=True))

        return sorted(refs, key=lambda r: r.vault_stem)

    def read(self, ticket_id: str) -> tuple[VaultRef, dict, str] | None:
        upper_id = ticket_id.upper()

        # Try each active type stem
        for sub_stem in _TYPE_STEMS.values():
            stem = f"{self._ns}.{sub_stem}.{upper_id}"
            content = self._get_raw(stem)
            if content is not None:
                fm, body = self._parse(content, source=stem)
                return VaultRef(vault_stem=stem, is_archived=False), fm, body

        # Try archive
        stem = self._archive_stem(upper_id)
        content = self._get_raw(stem)
        if content is not None:
            fm, body = self._parse(content, source=stem)
            return VaultRef(vault_stem=stem, is_archived=True), fm, body

        return None

    def read_ref(self, ref: VaultRef) -> tuple[dict, str]:
        content = self._get_raw(ref.vault_stem)
        if content is None:
            raise FileNotFoundError(f"vault note not found: {ref.vault_stem}")
        return self._parse(content, source=ref.vault_stem)

    def write(self, ref: VaultRef, frontmatter: dict, body: str) -> None:
        content = self._serialize(frontmatter, body)
        self._put(ref.vault_stem, content)

    def create_exclusive(self, filename: str, content: str) -> VaultRef:
        """Create a new ticket note exclusively.

        ``filename`` follows the LocalDirStore convention (e.g.
        ``TASK-001_MY_TASK.md``).  The ID and type are parsed from the content
        to build the vault stem.
        """
        fm, _ = self._parse(content, source=filename)
        ticket_id: str = fm["id"].upper()
        ticket_type: str = fm.get("type", "task").lower()
        sub_stem = _TYPE_STEMS.get(ticket_type, ticket_type + "s")
        stem = f"{self._ns}.{sub_stem}.{ticket_id}"
        self._put_exclusive(stem, content)
        return VaultRef(vault_stem=stem, is_archived=False)

    def archive(self, ref: VaultRef) -> VaultRef:
        ticket_id = ref.name
        new_stem = self._archive_stem(ticket_id)
        self._move(ref.vault_stem, new_stem)
        return VaultRef(vault_stem=new_stem, is_archived=True)

    def delete(self, ref: VaultRef) -> None:
        self._delete(ref.vault_stem)

    def read_blob(self, name: str) -> str | None:
        """Read a named blob from the vault.

        Blob name mapping:
        - ``"TODO.md"`` → ``<ns>.todo``
        - ``"templates/<type>.md"`` → ``<ns>.templates.<type>``
        """
        stem = self._blob_stem(name)
        if stem is None:
            return None
        return self._get_raw(stem)

    def write_blob(self, name: str, text: str) -> None:
        stem = self._blob_stem(name)
        if stem is None:
            raise ValueError(f"Cannot map blob name to vault stem: {name!r}")
        self._put(stem, text)

    def exists(self, ticket_id: str) -> bool:
        return self.read(ticket_id) is not None

    def _blob_stem(self, name: str) -> str | None:
        """Map a blob name to a vault stem, or None if not mappable."""
        if name == "TODO.md":
            return f"{self._ns}.todo"
        if name.startswith("templates/"):
            # e.g. "templates/task.md" -> "<ns>.templates.task"
            rest = name[len("templates/"):]
            template_name = rest.removesuffix(".md")
            return f"{self._ns}.templates.{template_name}"
        return None
