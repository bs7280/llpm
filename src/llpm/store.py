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

import os
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
