"""Tests for VaultRef + MdTreeStore (HTTP-backed TicketStore).

HTTP calls are mocked with unittest.mock so no network is required in CI.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llpm.store import MdTreeStore, TicketStore, VaultRef


# ---------------------------------------------------------------------------
# VaultRef
# ---------------------------------------------------------------------------

class TestVaultRef:
    def test_name_is_last_segment(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        assert ref.name == "TASK-001"
        assert ref.stem == "TASK-001"

    def test_parts_active(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001", is_archived=False)
        assert "archive" not in ref.parts
        assert ref.name in ref.parts

    def test_parts_archived(self):
        ref = VaultRef("repos.foo.llpm.archive.TASK-001", is_archived=True)
        assert "archive" in ref.parts

    def test_str(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        assert str(ref) == "repos.foo.llpm.tasks.TASK-001"

    def test_frozen(self):
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        with pytest.raises(Exception):
            ref.vault_stem = "other"

    def test_is_ticketstore_ref(self):
        # VaultRef satisfies the .name / .stem / .parts contract
        ref = VaultRef("repos.foo.llpm.tasks.TASK-001")
        assert hasattr(ref, "name")
        assert hasattr(ref, "stem")
        assert hasattr(ref, "parts")


# ---------------------------------------------------------------------------
# Helpers for mocking urllib
# ---------------------------------------------------------------------------

def _response(body: str | bytes | dict, status: int = 200):
    """Build a mock urllib response context manager."""
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=body)
    mock.status = status
    return mock


def _http_error(code: int):
    import urllib.error
    return urllib.error.HTTPError(url="", code=code, msg="", hdrs=None, fp=None)


TICKET_CONTENT = """\
---
id: TASK-001
type: task
title: Test ticket
status: open
priority: medium
effort: small
parent: null
blockers: []
created: '2026-07-04'
updated: '2026-07-04'
completed: null
tags: []
---

## Description

A test ticket body.
"""

# ---------------------------------------------------------------------------
# MdTreeStore
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    return MdTreeStore("https://agent-memory.home.lab", "myrepo")


class TestMdTreeStoreProtocol:
    def test_is_ticketstore(self, store):
        assert isinstance(store, TicketStore)


class TestMdTreeStoreRead:
    def test_read_found(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            result = store.read("TASK-001")

        assert result is not None
        ref, fm, body = result
        assert isinstance(ref, VaultRef)
        assert fm["id"] == "TASK-001"
        assert "A test ticket body" in body
        assert not ref.is_archived

    def test_read_case_insensitive(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            result = store.read("task-001")

        assert result is not None

    def test_read_not_found_returns_none(self, store):
        with patch("urllib.request.urlopen", side_effect=_http_error(404)):
            result = store.read("NOPE-999")

        assert result is None

    def test_read_archived(self, store):
        archived_content = TICKET_CONTENT.replace("status: open", "status: closed")

        def side_effect(req_or_url, *args, **kwargs):
            # All active type stems return 404; archive returns the note
            url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
            if "archive" in url:
                return _response(archived_content)
            raise _http_error(404)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = store.read("TASK-001")

        assert result is not None
        ref, fm, _ = result
        assert ref.is_archived
        assert "archive" in ref.parts

    def test_read_ref(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            fm, body = store.read_ref(ref)

        assert fm["id"] == "TASK-001"

    def test_read_ref_not_found_raises(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.NOPE-999")
        with patch("urllib.request.urlopen", side_effect=_http_error(404)):
            with pytest.raises(FileNotFoundError):
                store.read_ref(ref)

    def test_exists_true(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response(TICKET_CONTENT)
            assert store.exists("TASK-001") is True

    def test_exists_false(self, store):
        with patch("urllib.request.urlopen", side_effect=_http_error(404)):
            assert store.exists("NOPE-999") is False


class TestMdTreeStoreWrite:
    def test_write(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response({"stem": ref.vault_stem, "created": False, "etag": "abc"})
            store.write(ref, {"id": "TASK-001", "type": "task", "title": "t"}, "body\n")
        mock_open.assert_called_once()

    def test_create_exclusive_success(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response({"stem": "repos.myrepo.llpm.tasks.TASK-099", "created": True, "etag": "abc"})
            ref = store.create_exclusive("TASK-099_MY_TASK.md", TICKET_CONTENT.replace("TASK-001", "TASK-099"))

        assert isinstance(ref, VaultRef)
        assert ref.name == "TASK-099"
        assert not ref.is_archived

    def test_create_exclusive_conflict_raises(self, store):
        with patch("urllib.request.urlopen", side_effect=_http_error(409)):
            with pytest.raises(FileExistsError):
                store.create_exclusive("TASK-099_MY_TASK.md", TICKET_CONTENT.replace("TASK-001", "TASK-099"))


class TestMdTreeStoreList:
    def test_list_tickets_active(self, store):
        items_by_type = {
            "tasks": [{"stem": "repos.myrepo.llpm.tasks.TASK-001", "title": "t"}],
            "features": [{"stem": "repos.myrepo.llpm.features.FEAT-001", "title": "f"}],
            "epics": [],
            "research": [],
        }

        def side_effect(url, *args, **kwargs):
            url_str = url if isinstance(url, str) else url.full_url
            for sub, items in items_by_type.items():
                if sub in url_str:
                    return _response({"items": items, "total": len(items)})
            return _response({"items": [], "total": 0})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            refs = store.list_tickets(include_archive=False)

        assert len(refs) == 2
        names = {r.name for r in refs}
        assert "TASK-001" in names
        assert "FEAT-001" in names
        assert all(not r.is_archived for r in refs)

    def test_list_tickets_includes_archive(self, store):
        def side_effect(url, *args, **kwargs):
            url_str = url if isinstance(url, str) else url.full_url
            if "archive" in url_str:
                return _response({"items": [{"stem": "repos.myrepo.llpm.archive.TASK-000", "title": "old"}], "total": 1})
            return _response({"items": [], "total": 0})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            refs = store.list_tickets(include_archive=True)

        archived = [r for r in refs if r.is_archived]
        assert len(archived) == 1
        assert archived[0].name == "TASK-000"

    def test_list_tickets_exclude_archive(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response({"items": [], "total": 0})
            refs = store.list_tickets(include_archive=False)

        # Should not have called archive endpoint
        calls = [str(c) for c in mock_open.call_args_list]
        assert not any("archive" in c for c in calls)


class TestMdTreeStoreArchiveDelete:
    def test_archive(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response({"old_stem": ref.vault_stem, "new_stem": "repos.myrepo.llpm.archive.TASK-001", "moves": [], "relinked_files": 0})
            new_ref = store.archive(ref)

        assert isinstance(new_ref, VaultRef)
        assert new_ref.is_archived
        assert "archive" in new_ref.parts

    def test_delete(self, store):
        ref = VaultRef("repos.myrepo.llpm.tasks.TASK-001")
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response({"stem": ref.vault_stem, "dangling": []})
            store.delete(ref)
        mock_open.assert_called_once()


class TestMdTreeStoreBlobs:
    def test_read_blob_todo(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response("- (1) do this\n")
            result = store.read_blob("TODO.md")
        assert result == "- (1) do this\n"

    def test_read_blob_template(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response("---\ntype: task\n---\n")
            result = store.read_blob("templates/task.md")
        assert result is not None

    def test_read_blob_not_found(self, store):
        with patch("urllib.request.urlopen", side_effect=_http_error(404)):
            result = store.read_blob("TODO.md")
        assert result is None

    def test_read_blob_unmappable_returns_none(self, store):
        assert store.read_blob("unknown/path.txt") is None

    def test_write_blob(self, store):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _response({"stem": "repos.myrepo.llpm.todo", "created": True, "etag": "x"})
            store.write_blob("TODO.md", "- (1) thing\n")
        mock_open.assert_called_once()


# ---------------------------------------------------------------------------
# Config.toml discovery (TASK-001)
# ---------------------------------------------------------------------------

class TestConfigTomlDiscovery:
    def test_no_config_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "dir"
        assert cfg["docs_root"] == (tmp_path / "llpm").resolve()

    def test_finds_config_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "dir"\nroot = "./mytickets"\n')

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "dir"
        assert cfg["docs_root"] == (tmp_path / "mytickets").resolve()

    def test_finds_config_in_parent(self, tmp_path, monkeypatch):
        subdir = tmp_path / "src" / "module"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "dir"\nroot = "./llpm"\n')

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "dir"
        assert cfg["docs_root"] == (tmp_path / "llpm").resolve()

    def test_mdtree_config_parsed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[store]\nkind = "mdtree"\nurl = "https://agent-memory.home.lab"\nstem = "myrepo"\n'
        )

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert cfg["kind"] == "mdtree"
        assert cfg["base_url"] == "https://agent-memory.home.lab"
        assert cfg["repo_stem"] == "myrepo"

    def test_unknown_kind_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "unknown_kind"\n')

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        with pytest.raises(SystemExit):
            _resolve_store_config(Args())

    def test_env_var_takes_priority_over_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / ".llpm"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[store]\nkind = "dir"\nroot = "./from_config"\n')
        monkeypatch.setenv("LLPM_DOCS_ROOT", str(tmp_path / "from_env"))

        from llpm.commands import _resolve_store_config
        class Args: docs_root = None
        cfg = _resolve_store_config(Args())
        assert "from_env" in str(cfg["docs_root"])
