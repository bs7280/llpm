"""`llpm serve` wiring and the optional-extra boundary (TASK-016).

Two things are being protected here:

1. The core install stays pyyaml-only. FastAPI/uvicorn are the ``llpm[api]``
   extra, so every other command must work without them -- proved by running
   the CLI in a subprocess with those modules blocked.
2. `serve` points the router at the right board(s): one board from the usual
   config discovery, or every board in a vault, with one store cached per repo.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from llpm import commands, service
from llpm.store import MdTreeStore


# ---------------------------------------------------------------------------
# The core install does not need the extra
# ---------------------------------------------------------------------------

# Setting a module to None in sys.modules is what CPython treats as "this import
# is blocked": the import raises ModuleNotFoundError, exactly as it would on a
# machine that never installed the extra.
_BLOCK_EXTRA = (
    "import sys\n"
    "sys.modules['fastapi'] = None\n"
    "sys.modules['uvicorn'] = None\n"
)


def _cli_without_extra(argv: list[str], cwd) -> subprocess.CompletedProcess:
    code = _BLOCK_EXTRA + "from llpm.__main__ import main\n" + f"main({argv!r})\n"
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(cwd)
    )


class TestWithoutTheApiExtra:
    def test_help_works(self, tmp_path):
        r = _cli_without_extra(["--help"], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "usage: llpm" in r.stdout
        assert "serve" in r.stdout  # the command is still advertised

    def test_a_real_command_works(self, tmp_path):
        (tmp_path / "llpm" / "tickets").mkdir(parents=True)
        r = _cli_without_extra(["list"], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "No tickets found" in r.stdout

    def test_serve_explains_itself(self, tmp_path):
        r = _cli_without_extra(["serve"], cwd=tmp_path)
        assert r.returncode == 1
        assert "llpm[api]" in r.stderr
        assert "install" in r.stderr.lower()

    def test_nothing_on_the_cli_path_imports_fastapi(self, tmp_path):
        """Not just importable-without -- not imported at all, so the extra
        stays genuinely optional rather than accidentally required."""
        r = subprocess.run(
            [sys.executable, "-c",
             "import llpm.__main__, sys;"
             " print(any(m in sys.modules for m in ('fastapi', 'uvicorn')))"],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert r.stdout.strip() == "False", r.stderr


# ---------------------------------------------------------------------------
# Board wiring
# ---------------------------------------------------------------------------

def _args(**kw):
    return SimpleNamespace(**{"docs_root": None, "vault": None, **kw})


class TestSingleBoard:
    @pytest.fixture
    def board(self, tmp_path):
        """A local board at <tmp>/myrepo/llpm, so its name is 'myrepo'."""
        docs = tmp_path / "myrepo" / "llpm"
        (docs / "tickets").mkdir(parents=True)
        return docs

    def test_named_after_the_directory_holding_the_docs_root(self, board):
        store_for, boards, label = commands._serve_stores(_args(docs_root=str(board)))
        assert boards() == ["myrepo"]
        assert "myrepo" in label

    def test_serves_that_board(self, board):
        store_for, _, _ = commands._serve_stores(_args(docs_root=str(board)))
        assert store_for("myrepo").docs_root == board

    def test_any_other_repo_is_not_found(self, board):
        store_for, _, _ = commands._serve_stores(_args(docs_root=str(board)))
        with pytest.raises(service.NotFound) as e:
            store_for("marginalia")
        assert "marginalia" in str(e.value)

    def test_vault_board_is_named_by_its_repo_stem(self):
        cfg = {"kind": "mdtree", "base_url": "https://vault.test", "repo_stem": "llpm"}
        assert commands._board_name(cfg) == "llpm"


class TestVaultMode:
    ARGS = dict(vault="https://vault.test")

    def test_one_store_per_repo_cached(self):
        store_for, _, _ = commands._serve_stores(_args(**self.ARGS))
        assert store_for("llpm") is store_for("llpm")
        assert store_for("llpm") is not store_for("marginalia")

    def test_stores_are_pointed_at_the_vault(self):
        store_for, _, label = commands._serve_stores(_args(**self.ARGS))
        store = store_for("marginalia")
        assert isinstance(store, MdTreeStore)
        assert store._ns == "repos.marginalia.llpm"
        assert "https://vault.test" in label

    def test_a_board_name_cannot_address_the_rest_of_the_vault(self):
        store_for, _, _ = commands._serve_stores(_args(**self.ARGS))
        for bad in ("area.homelab", "../secrets", "a b"):
            with pytest.raises(service.Invalid):
                store_for(bad)


class TestBoardDiscovery:
    """`serve --vault` enumerates boards from the vault's own stems."""

    LISTING = [
        {"stem": "repos.llpm.llpm.tasks.TASK-001"},
        {"stem": "repos.llpm.llpm.tasks.TASK-001.agent-workers.w1"},  # not a board
        {"stem": "repos.llpm.llpm.archive.TASK-000"},
        {"stem": "repos.marginalia.llpm.features.FEAT-026"},
        {"stem": "repos.coaching_platfrom_saas.llpm.epics.EPIC-001"},
        {"stem": "repos.marginalia.docs.readme"},  # a repo, but no llpm board
        {"stem": "area.homelab.networking"},       # not a repo at all
    ]

    def test_repo_names_come_from_the_stems(self):
        store = MdTreeStore("https://vault.test", repo_stem="llpm")
        with patch.object(MdTreeStore, "_list_pattern", return_value=self.LISTING) as p:
            assert store.list_boards() == ["coaching_platfrom_saas", "llpm", "marginalia"]
        p.assert_called_once_with("repos.*.llpm.*")

    def test_the_answer_does_not_depend_on_the_store_s_own_repo(self):
        with patch.object(MdTreeStore, "_list_pattern", return_value=self.LISTING):
            assert MdTreeStore("https://vault.test", repo_stem="llpm").list_boards() == \
                   MdTreeStore("https://vault.test", repo_stem="").list_boards()

    def test_service_reaches_it_through_the_store(self):
        store = MdTreeStore("https://vault.test", repo_stem="llpm")
        with patch.object(MdTreeStore, "_list_pattern", return_value=self.LISTING):
            assert service.list_boards(store) == store.list_boards()
