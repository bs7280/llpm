"""Tests for llpm.store: LocalDirStore unit tests + FakeStore seam proof."""

from pathlib import Path, PurePosixPath

import pytest

from llpm import commands, parser
from llpm.__main__ import main
from llpm.store import LocalDirStore, TicketStore


# -- LocalDirStore --

class TestLocalDirStoreList:
    def test_list_all(self, docs_root):
        store = LocalDirStore(docs_root)
        # 5 active + 1 archived
        assert len(store.list_tickets(include_archive=True)) == 6

    def test_list_active_only(self, docs_root):
        store = LocalDirStore(docs_root)
        refs = store.list_tickets(include_archive=False)
        assert len(refs) == 5
        assert all("archive" not in str(r) for r in refs)

    def test_list_sorted(self, docs_root):
        store = LocalDirStore(docs_root)
        refs = store.list_tickets()
        assert refs == sorted(refs)

    def test_list_uninitialized(self, tmp_path):
        store = LocalDirStore(tmp_path / "nope")
        assert store.list_tickets() == []

    def test_matches_parser_find_tickets(self, docs_root):
        # parser.find_tickets must return exactly what the store lists
        store = LocalDirStore(docs_root)
        assert parser.find_tickets(docs_root) == store.list_tickets()


class TestLocalDirStoreRead:
    def test_read_found(self, docs_root):
        store = LocalDirStore(docs_root)
        result = store.read("FEAT-001")
        assert result is not None
        ref, fm, body = result
        assert fm["id"] == "FEAT-001"
        assert "## Problem" in body
        assert ref.name.startswith("FEAT-001")

    def test_read_case_insensitive(self, docs_root):
        store = LocalDirStore(docs_root)
        assert store.read("feat-001") is not None

    def test_read_archived(self, docs_root):
        store = LocalDirStore(docs_root)
        result = store.read("FEAT-000")
        assert result is not None
        assert "archive" in str(result[0])

    def test_read_not_found(self, docs_root):
        store = LocalDirStore(docs_root)
        assert store.read("NOPE-999") is None

    def test_read_ref(self, docs_root):
        store = LocalDirStore(docs_root)
        ref = store.list_tickets(include_archive=False)[0]
        fm, body = store.read_ref(ref)
        assert "id" in fm

    def test_exists(self, docs_root):
        store = LocalDirStore(docs_root)
        assert store.exists("TASK-001") is True
        assert store.exists("task-001") is True
        assert store.exists("NOPE-999") is False


class TestLocalDirStoreWrite:
    def test_write_roundtrip(self, docs_root):
        store = LocalDirStore(docs_root)
        ref, fm, body = store.read("FEAT-002")
        fm["status"] = "review"
        store.write(ref, fm, body)
        _, fm2, body2 = store.read("FEAT-002")
        assert fm2["status"] == "review"
        assert body2 == body

    def test_write_matches_parser_write_document(self, docs_root, tmp_path):
        # Byte-identical output to the original write path
        store = LocalDirStore(docs_root)
        ref, fm, body = store.read("FEAT-001")
        via_parser = tmp_path / "via_parser.md"
        parser.write_document(via_parser, fm, body)
        store.write(ref, fm, body)
        assert ref.read_text() == via_parser.read_text()


class TestLocalDirStoreCreateExclusive:
    def test_create(self, docs_root):
        store = LocalDirStore(docs_root)
        content = "---\nid: TASK-099\n---\nbody\n"
        ref = store.create_exclusive("TASK-099_NEW.md", content)
        assert ref == docs_root / "tickets" / "TASK-099_NEW.md"
        assert ref.read_text() == content

    def test_create_collision_raises(self, docs_root):
        store = LocalDirStore(docs_root)
        store.create_exclusive("TASK-099_NEW.md", "---\nid: x\n---\n")
        with pytest.raises(FileExistsError):
            store.create_exclusive("TASK-099_NEW.md", "---\nid: y\n---\n")


class TestLocalDirStoreArchiveDelete:
    def test_archive(self, docs_root):
        store = LocalDirStore(docs_root)
        ref, fm, _ = store.read("FEAT-001")
        dst = store.archive(ref)
        assert not ref.exists()
        assert dst == docs_root / "tickets" / "archive" / ref.name
        assert dst.exists()
        # Still findable (archive scanned)
        assert store.exists("FEAT-001")

    def test_delete(self, docs_root):
        store = LocalDirStore(docs_root)
        ref, _, _ = store.read("TASK-001")
        store.delete(ref)
        assert not ref.exists()
        assert store.read("TASK-001") is None


class TestLocalDirStoreBlobs:
    def test_read_blob_missing(self, docs_root):
        store = LocalDirStore(docs_root)
        assert store.read_blob("TODO.md") is None

    def test_blob_roundtrip(self, docs_root):
        store = LocalDirStore(docs_root)
        store.write_blob("TODO.md", "- (1) hello\n")
        assert store.read_blob("TODO.md") == "- (1) hello\n"
        assert (docs_root / "TODO.md").read_text() == "- (1) hello\n"

    def test_read_blob_template(self, docs_root):
        store = LocalDirStore(docs_root)
        text = store.read_blob("templates/task.md")
        assert text is not None
        assert "__ID__" in text


class TestProtocolConformance:
    def test_localdirstore_is_ticketstore(self, docs_root):
        assert isinstance(LocalDirStore(docs_root), TicketStore)

    def test_fakestore_is_ticketstore(self):
        assert isinstance(FakeStore(), TicketStore)


# -- FakeStore: in-memory TicketStore proving the seam --

class FakeStore:
    """In-memory TicketStore. No filesystem access for ticket data."""

    def __init__(self):
        self.active = {}    # filename -> (frontmatter, body)
        self.archived = {}  # filename -> (frontmatter, body)
        self.blobs = {}     # name -> text

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

    def read_blob(self, name):
        return self.blobs.get(name)

    def write_blob(self, name, text):
        self.blobs[name] = text

    def exists(self, ticket_id):
        return self.read(ticket_id) is not None

    def _bucket(self, ref):
        return self.archived if ref.parent.name == "archive" else self.active


def _seed(store, ticket_id, title, *, ticket_type="task", status="open",
          parent=None, blockers=None):
    fm = {
        "id": ticket_id,
        "type": ticket_type,
        "title": title,
        "status": status,
        "priority": "medium",
        "parent": parent,
        "blockers": blockers or [],
        "created": "2026-01-01",
        "updated": "2026-01-01",
        "completed": None,
        "tags": [],
    }
    slug = title.upper().replace(" ", "_")
    store.active[f"{ticket_id}_{slug}.md"] = (fm, f"# {title}\n")


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    """A FakeStore wired into command dispatch, plus a docs_root whose
    tickets/ dir exists (for the init check) but stays EMPTY -- proving
    all ticket I/O goes through the fake."""
    docs = tmp_path / "docs"
    (docs / "tickets").mkdir(parents=True)
    fake = FakeStore()
    monkeypatch.setattr(commands, "_make_store", lambda docs_root, **kw: fake)
    monkeypatch.setattr(commands, "_make_store_from_config", lambda cfg: fake)
    return docs, fake


class TestFakeStoreSeam:
    def _run(self, docs, *args):
        main(["--docs-root", str(docs), *args])

    def test_status_end_to_end(self, fake_project, capsys):
        docs, fake = fake_project
        _seed(fake, "TASK-101", "Fake thing")

        self._run(docs, "status", "TASK-101", "in-progress")

        out = capsys.readouterr().out
        assert "TASK-101: open -> in-progress" in out
        fm, _ = fake.active["TASK-101_FAKE_THING.md"]
        assert fm["status"] == "in-progress"
        # No ticket files ever touched the filesystem
        assert list((docs / "tickets").rglob("*.md")) == []

    def test_board_derives_blocked_through_store(self, fake_project, capsys):
        docs, fake = fake_project
        _seed(fake, "TASK-101", "Blocker ticket", status="open")
        _seed(fake, "TASK-102", "Blocked ticket", status="open",
              blockers=["TASK-101"])

        self._run(docs, "board")

        out = capsys.readouterr().out
        blocked_col = out.split("-- OPEN")[0]
        assert "TASK-102" in blocked_col
        assert list((docs / "tickets").rglob("*.md")) == []

    def test_blocker_add_and_list(self, fake_project, capsys):
        docs, fake = fake_project
        _seed(fake, "TASK-101", "Blocker ticket", status="complete")
        _seed(fake, "TASK-102", "Blocked ticket")

        self._run(docs, "blocker", "add", "TASK-102", "--blocked-by", "TASK-101")
        self._run(docs, "blocker", "list", "TASK-102")

        out = capsys.readouterr().out
        assert "TASK-102: now blocked by 'TASK-101'" in out
        assert "all blockers resolved" in out

    def test_archive_end_to_end(self, fake_project, capsys):
        docs, fake = fake_project
        _seed(fake, "TASK-101", "Done thing", status="complete")

        self._run(docs, "archive", "TASK-101")

        out = capsys.readouterr().out
        assert "Archived TASK-101 -> tickets/archive/TASK-101_DONE_THING.md" in out
        assert "TASK-101_DONE_THING.md" in fake.archived
        assert fake.active == {}

    def test_todo_through_blobs(self, fake_project, capsys):
        docs, fake = fake_project

        self._run(docs, "todo", "--add", "remember this")
        self._run(docs, "todo", "--list")

        out = capsys.readouterr().out
        assert "(1) remember this" in out
        assert fake.blobs["TODO.md"] == "- (1) remember this\n"
        assert not (docs / "TODO.md").exists()

    def test_next_id_against_fake(self, fake_project):
        docs, fake = fake_project
        _seed(fake, "TASK-101", "A thing")
        fake.archived["TASK-205_OLD.md"] = (
            {"id": "TASK-205", "type": "task", "title": "Old", "status": "closed",
             "priority": "low", "parent": None, "blockers": [],
             "created": "2025-01-01", "updated": "2025-01-01",
             "completed": "2025-01-02", "tags": []},
            "old\n",
        )
        # Scan-for-max includes archive; works identically against any store
        assert parser.next_id(fake, "task") == "TASK-206"
        assert parser.next_id(fake, "feature") == "FEAT-001"
