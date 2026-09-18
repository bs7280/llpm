"""Shared fixtures for LLPM tests."""

import shutil
from pathlib import Path, PurePosixPath

import pytest

from llpm import commands, parser

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "docs"


@pytest.fixture
def docs_root(tmp_path):
    """Copy fixture data to a temp directory for isolated testing."""
    dst = tmp_path / "docs"
    shutil.copytree(FIXTURES_DIR, dst)
    return dst


class FakeStore:
    """In-memory TicketStore -- the vault seam without a vault.

    Same shape as the copies inside test_store.py and test_vault_commands.py;
    new tests take this one so there isn't a fourth.
    """

    def __init__(self):
        self.active = {}    # filename -> (frontmatter, body)
        self.archived = {}  # filename -> (frontmatter, body)
        self.blobs = {}     # name -> text
        self.foreign = {}   # vault stem -> frontmatter (cross-board notes)
        self.foreign_reachable = True
        self.goal_notes = {}  # vault stem -> frontmatter (type: goal notes)
        self.subnote_names = {}  # ticket filename -> names of notes below it

    def list_tickets(self, include_archive=True):
        refs = [PurePosixPath(name) for name in self.active]
        if include_archive:
            refs.extend(PurePosixPath("archive") / name for name in self.archived)
        return sorted(refs)

    def read(self, ticket_id):
        upper_id = ticket_id.upper()
        for ref in self.list_tickets(include_archive=True):
            if ref.name.upper().startswith(upper_id):
                fm, body = self.read_ref(ref)
                return ref, fm, body
        return None

    def read_ref(self, ref):
        fm, body = self._bucket(ref)[ref.name]
        return dict(fm), body

    def write(self, ref, frontmatter, body):
        self._bucket(ref)[ref.name] = (dict(frontmatter), body)

    def create_exclusive(self, filename, content):
        if filename in self.active or filename in self.archived:
            raise FileExistsError(filename)
        fm, body = parser.parse_text(content, source=filename)
        self.active[filename] = (fm, body)
        return PurePosixPath(filename)

    def archive(self, ref):
        self.archived[ref.name] = self.active.pop(ref.name)
        return PurePosixPath("archive") / ref.name

    def delete(self, ref):
        del self._bucket(ref)[ref.name]

    def subnotes(self, ref):
        return list(self.subnote_names.get(ref.name, []))

    def read_blob(self, name):
        return self.blobs.get(name)

    def write_blob(self, name, text):
        self.blobs[name] = text

    def exists(self, ticket_id):
        return self.read(ticket_id) is not None

    def read_foreign(self, stem):
        if not self.foreign_reachable:
            return ("unavailable", None)
        if stem in self.foreign:
            return ("ok", dict(self.foreign[stem]))
        return ("missing", None)

    def scan_by_type(self, type_value):
        return [
            (stem, dict(fm))
            for stem, fm in self.goal_notes.items()
            if fm.get("type") == type_value
        ]

    def _bucket(self, ref):
        return self.archived if ref.parent.name == "archive" else self.active


def load_fake_store(docs_root: Path) -> FakeStore:
    """A FakeStore holding exactly the board at ``docs_root``.

    Lets a test assert that the same board answers the same way whichever store
    it is read through -- the local dir and the vault are supposed to be two
    spellings of one thing.
    """
    store = FakeStore()
    for path in sorted((docs_root / "tickets").glob("*.md")):
        store.active[path.name] = parser.parse_document(path)
    archive = docs_root / "tickets" / "archive"
    if archive.exists():
        for path in sorted(archive.glob("*.md")):
            store.archived[path.name] = parser.parse_document(path)
    return store


@pytest.fixture(autouse=True)
def _hermetic_provenance(monkeypatch):
    """Keep the suite hermetic: pytest runs inside the llpm repo, so real
    commit harvesting would leak actual repo SHAs into fixture tickets, and
    ambient LLPM_* env would flip provenance defaults. Provenance tests
    re-patch what they need."""
    monkeypatch.delenv("LLPM_ORIGIN", raising=False)
    monkeypatch.delenv("LLPM_CREATED_BY", raising=False)
    monkeypatch.setattr(commands, "_harvest_commits", lambda ticket_id: [])
