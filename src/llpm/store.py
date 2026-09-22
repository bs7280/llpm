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

import http.client
import io
import json
import os
import re
import selectors
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from . import parser as _parser


class MdTreeStoreError(Exception):
    """A vault-store operation failed in a way worth surfacing to the user as a
    clean, actionable message rather than a raw traceback.

    Raised for TLS trust problems (Python not trusting the homelab mkcert root
    CA), a misconfigured ``[store] ca`` path, and an unreachable vault.
    """


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
        """Move the ticket at ref -- and every note below it -- into the
        archive. Returns the new ref."""
        ...

    def delete(self, ref: Path) -> None:
        """Remove the ticket at ref and every note below it."""
        ...

    def subnotes(self, ref: Path) -> list[str]:
        """Names of the notes hanging *below* the ticket at ref -- the natural
        home for whatever accumulates around a ticket, one child segment per
        kind (``<ID>.agent-workers.<worker>…``, ``<ID>.human-review…``). These
        are never tickets, whatever their frontmatter says: ``list_tickets``
        skips them, ``archive`` carries them along, ``delete`` removes them.
        Returns ``[]`` when there are none."""
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

    def read_foreign(self, stem: str) -> tuple[str, dict | None]:
        """Read the frontmatter of an arbitrary vault note by full stem
        (cross-board ``waits_on`` targets). Returns ``(state, frontmatter)``:

        - ``("ok", fm)``          -- note found and parsed
        - ``("missing", None)``   -- definitive miss (note does not exist)
        - ``("error", None)``     -- note exists but failed to parse
        - ``("unavailable", None)`` -- this store cannot resolve foreign
          stems, or the vault is unreachable; callers must degrade
          gracefully (never raise, never spuriously block)
        """
        ...

    def scan_by_type(self, type_value: str) -> list[tuple[str, dict]]:
        """Find notes anywhere in scope whose frontmatter ``type:`` equals
        ``type_value`` (e.g. ``"goal"`` -- goal is a type, not a place).
        Returns ``(stem, frontmatter)`` pairs. Notes that fail to parse are
        skipped, not fatal. A store with nothing to scan (or that predates
        this protocol method) returns ``[]``."""
        ...

    def begin_read_scope(self) -> None:
        """Start a fresh read scope: drop anything this store cached from an
        earlier one.

        A store may cache reads *within* one logical operation -- ``MdTreeStore``
        keeps resolved foreign stems, so a board listing resolves each distinct
        ``waits_on`` target once instead of once per ticket. That cache is
        correct for the length of a call and wrong past it: ``llpm serve`` and
        marginalia's ``/api/llpm`` mount hold one store per board for the life
        of the process, where a per-run cache freezes a cross-board target's
        status at whatever it was when first read (TASK-021).

        The scope is one service call, and ``service`` opens it -- callers reach
        this through ``service._begin_read_scope``, which no-ops for a store
        that doesn't implement it (a local directory caches nothing), the same
        degrade rule ``read_foreign`` and ``list_boards`` follow.
        """
        ...

    def load_frontmatter(self, include_archive: bool = True) -> list[tuple[Path, dict]]:
        """Every ticket's ``(ref, frontmatter)``, however this store gets it
        cheapest -- the read behind every listing, board view and rollup.

        Separate from ``list_tickets`` + ``read_ref`` because NOTHING that
        walks a whole board wants the bodies, and on a remote store fetching
        them is the entire cost: the vault answers one
        ``?include=frontmatter`` request with the whole board, where per-ref
        reads are one HTTP round trip per ticket. Optional -- callers reach it
        through ``parser.load_all_tickets``, which falls back to the per-ref
        walk for a store that doesn't implement it (a local directory has
        nothing to gain, since it parses the file either way).

        Tickets whose frontmatter won't parse are skipped, not fatal, matching
        the per-ref walk.
        """
        ...


class LocalDirStore:
    """Behavior-preserving filesystem implementation of TicketStore.

    Wraps the exact filesystem operations previously inlined in parser.py
    and commands.py -- including the O_CREAT|O_EXCL atomic create.
    """

    # ``<ID>.<anything>.md`` is a note *below* ticket ``<ID>`` -- the dotted-
    # filename spelling of a vault child stem (``TASK-001.agent-workers.w1.md``).
    # Ticket files are ``<ID>_SLUG.md`` and slugs never contain dots, so an ID
    # followed by a dot is unambiguous.
    _SUBNOTE_RE = re.compile(r"^[A-Za-z]+-\d+\.(?!md$)")
    _TICKET_ID_RE = re.compile(r"^[A-Za-z]+-\d+")

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

        return sorted(p for p in results if not self._SUBNOTE_RE.match(p.name))

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
        for sub in self._subnote_paths(ref):
            sub.rename(self.archive_dir / sub.name)
        dst = self.archive_dir / ref.name
        ref.rename(dst)
        return dst

    def delete(self, ref: Path) -> None:
        for sub in self._subnote_paths(ref):
            sub.unlink()
        ref.unlink()

    def subnotes(self, ref: Path) -> list[str]:
        return [p.name for p in self._subnote_paths(ref)]

    def _subnote_paths(self, ref: Path) -> list[Path]:
        """Sibling files ``<ID>.<…>.md`` of the ticket at ref."""
        m = self._TICKET_ID_RE.match(ref.name)
        if m is None:
            return []
        # The trailing dot ends the ID, so TASK-001 never claims TASK-0010's.
        return sorted(ref.parent.glob(f"{m.group(0)}.*.md"))

    def read_blob(self, name: str) -> str | None:
        path = self.docs_root / name
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def write_blob(self, name: str, text: str) -> None:
        (self.docs_root / name).write_text(text, encoding="utf-8")

    def exists(self, ticket_id: str) -> bool:
        return self._find(ticket_id) is not None

    def read_foreign(self, stem: str) -> tuple[str, dict | None]:
        # A local directory has no vault to resolve foreign stems against.
        return ("unavailable", None)

    def begin_read_scope(self) -> None:
        # Nothing is cached: every read goes to the filesystem, which is the
        # source of truth. Implemented anyway so the protocol has no optional
        # holes for callers to probe around.
        pass

    def load_frontmatter(self, include_archive: bool = True) -> list[tuple[Path, dict]]:
        """The per-ref walk, minus the bodies. A local directory reads the
        whole file either way, so there is nothing to batch here -- this
        exists so every store answers the same question, and the remote
        store's one-request version isn't a special case callers must know
        about."""
        pairs: list[tuple[Path, dict]] = []
        for ref in self.list_tickets(include_archive=include_archive):
            try:
                fm, _body = self.read_ref(ref)
            except (ValueError, yaml.YAMLError):
                continue  # unparseable ticket: skipped, never fatal
            pairs.append((ref, fm))
        return pairs

    def scan_by_type(self, type_value: str) -> list[tuple[str, dict]]:
        # A local dir has no vault -- this only sees its own tickets dir.
        results = []
        for ref in self.list_tickets(include_archive=True):
            try:
                fm, _ = self.read_ref(ref)
            except (ValueError, yaml.YAMLError):
                continue
            if fm.get("type") == type_value:
                results.append((ref.stem, fm))
        return results

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

# The only buckets under `<ns>` that hold tickets. `<ns>.templates.<type>` is
# the other bucket llpm itself writes; a board is free to keep more.
_TICKET_BUCKETS: frozenset[str] = frozenset(_TYPE_STEMS.values()) | {"archive"}


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

    Talks to the markdown-tree-service REST API using stdlib ``http.client``
    only — no extra dependencies — over one keep-alive connection per thread
    (see ``_send``).

    Stem layout::

        repos.<repo_stem>.llpm.tasks.TASK-001
        repos.<repo_stem>.llpm.features.FEAT-001
        repos.<repo_stem>.llpm.epics.EPIC-001
        repos.<repo_stem>.llpm.research.RES-001
        repos.<repo_stem>.llpm.archive.TASK-001   (archived)
        repos.<repo_stem>.llpm.todo                (TODO blob)
        repos.<repo_stem>.llpm.templates.<type>    (template blobs)

    A ticket is exactly one segment below its bucket. Anything deeper --
    ``…tasks.TASK-001.agent-workers.<worker>[.<child>…]`` (the notes a
    dispatched worker spams), ``…TASK-001.human-review…``, whatever kind comes
    next -- belongs to that ticket and is never a ticket itself (see
    ``subnotes``). The rule is structural on purpose: ``agent-workers`` is one
    child kind among several, and llpm special-cases none of them.

    Errors are loud: connection failures propagate; 404 on a stem returns None
    from ``read()``; 409 on ``create_exclusive`` raises ``FileExistsError``.
    """

    def __init__(self, base_url: str, repo_stem: str, ca: str | None = None) -> None:
        """
        Args:
            base_url:  e.g. ``"https://agent-memory.home.lab"``
            repo_stem: the repo namespace, e.g. ``"llpm"`` giving
                       ``repos.llpm.llpm.*`` stems.
            ca:        optional path to a CA bundle (PEM) used to verify the
                       server certificate.  When set, it is threaded into every
                       request via ``ssl.create_default_context(cafile=ca)`` so
                       the repo works without per-shell env setup.  When None,
                       the stdlib default context is used, which honors the
                       ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` env vars.
        """
        self._base = base_url.rstrip("/")
        self._ns = f"repos.{repo_stem}.llpm"
        self._ca = ca
        self._ssl_ctx: ssl.SSLContext | None = None  # built lazily from _ca
        # One keep-alive connection per THREAD, never per store: `llpm serve`
        # keeps one store per board and FastAPI runs its endpoints on a
        # threadpool, and an HTTPConnection can't carry two exchanges at once.
        self._local = threading.local()
        # Foreign-stem read cache, scoped to ONE read scope (see
        # begin_read_scope): several tickets often wait on the same target, and
        # board rendering resolves each ticket independently. It is emptied at
        # every service call and whenever this store writes a stem it holds, so
        # a long-lived process never serves a frozen answer (TASK-021).
        self._foreign_cache: dict[str, tuple[str, dict | None]] = {}

    # -- Internal HTTP helpers ------------------------------------------------

    def _url(self, stem: str) -> str:
        return f"{self._base}/api/v1/notes/{urllib.parse.quote(stem, safe='')}"

    def _context(self) -> ssl.SSLContext | None:
        """SSL context for requests, or None to use the stdlib default (which
        honors SSL_CERT_FILE/SSL_CERT_DIR).  Built once from ``self._ca``."""
        if self._ca is None:
            return None
        if self._ssl_ctx is None:
            try:
                self._ssl_ctx = ssl.create_default_context(cafile=self._ca)
            except OSError as e:
                raise MdTreeStoreError(
                    f"store.ca points at {self._ca!r}, which could not be loaded "
                    f"({e}). Expected the homelab mkcert root CA — find its path "
                    f"with `mkcert -CAROOT` (the file is rootCA.pem inside)."
                ) from e
        return self._ssl_ctx

    def _open(self, req_or_url) -> io.BytesIO:
        """Send one request (a URL for a GET, a ``urllib.request.Request`` for
        anything else) and turn transport failures into an actionable
        ``MdTreeStoreError`` instead of a 40-line traceback.  ``HTTPError`` (any
        non-2xx) propagates unchanged so callers keep handling 404/409
        themselves."""
        try:
            return self._send(req_or_url)
        except urllib.error.HTTPError:
            raise  # an OSError too -- keep it out of the branch below
        except ssl.SSLCertVerificationError as e:
            raise MdTreeStoreError(self._tls_hint()) from e
        except (OSError, http.client.HTTPException) as e:
            # DNS failure, connection refused/reset, etc. — the vault is
            # unreachable, not a cert-trust problem.
            raise MdTreeStoreError(
                f"Could not reach the vault at {self._base}: {e}. "
                f"Check that the service is up and that store.url in "
                f".llpm/config.toml is correct."
            ) from e

    # How a reused connection the server has already dropped fails
    # (``RemoteDisconnected`` is both of the first two).
    _STALE = (http.client.BadStatusLine, ConnectionResetError, BrokenPipeError)

    def _send(self, req_or_url) -> io.BytesIO:
        """One exchange over this thread's keep-alive connection -- the
        urlopen-shaped seam ``_open`` wraps (and the tests stub): a URL or a
        ``Request`` in, the whole body out, ``HTTPError`` for any non-2xx, raw
        transport exceptions otherwise.

        A fresh connection per request cost a TCP + TLS handshake each time --
        34 ms against 14 ms reused on the LAN, and seconds from a container
        that re-resolves DNS per connection (TASK-015).

        The server may close a connection whenever it is idle. When it hung up
        *between* our requests that is seen before sending, so nothing goes
        into a dead socket. The race left over -- it hung up as the request
        arrived -- raises one of ``_STALE`` and is retried once on a fresh
        connection. Only a *reused* connection is retried: a fresh one failing
        isn't staleness, and resending a create or a move on a guess could
        apply it twice.
        """
        conn = self._connection()
        req = (req_or_url if isinstance(req_or_url, urllib.request.Request)
               else urllib.request.Request(req_or_url))
        if conn.sock is not None and self._peer_closed(conn.sock):
            conn.close()
        reused = conn.sock is not None
        try:
            resp, body = self._exchange(conn, req)
        except self._STALE:
            if not reused:
                raise
            resp, body = self._exchange(conn, req)
        if not 200 <= resp.status < 300:
            raise urllib.error.HTTPError(
                req.full_url, resp.status, resp.reason, resp.headers, io.BytesIO(body)
            )
        return io.BytesIO(body)

    @staticmethod
    def _exchange(conn: http.client.HTTPConnection, req: urllib.request.Request):
        """Send ``req`` and read the whole response: ``(response, body)``.
        Reading all of it is what frees the connection for the next request."""
        try:
            conn.request(
                req.get_method(), req.selector, body=req.data,
                headers={"User-Agent": "llpm", **dict(req.header_items())},
            )
            resp = conn.getresponse()
            return resp, resp.read()
        except BaseException:
            conn.close()  # a half-finished exchange must never carry the next one
            raise

    @staticmethod
    def _peer_closed(sock) -> bool:
        """True when an idle keep-alive socket is readable. Between requests
        the server has nothing to say, so readable means it hung up (EOF or a
        TLS close_notify) -- the check urllib3 makes before reusing one."""
        with selectors.DefaultSelector() as sel:
            sel.register(sock, selectors.EVENT_READ)
            return bool(sel.select(timeout=0))

    def _connection(self) -> http.client.HTTPConnection:
        """This thread's connection, made on first use. It connects lazily and
        reconnects by itself after a close."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            url = urllib.parse.urlsplit(self._base)
            if url.scheme == "https":
                conn = http.client.HTTPSConnection(url.netloc, context=self._context())
            elif url.scheme == "http":
                conn = http.client.HTTPConnection(url.netloc)
            else:
                raise MdTreeStoreError(
                    f"store.url must be an http(s) URL, got {self._base!r}."
                )
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's connection; the next request opens a new one.
        Optional -- a process exiting closes it just the same."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()

    def _tls_hint(self) -> str:
        """Actionable message for a server-certificate verification failure."""
        lines = [
            f"TLS certificate verification failed talking to {self._base}.",
            "",
            "Python doesn't use the macOS system trust store, so the homelab's",
            "mkcert root CA (which signs *.home.lab) isn't trusted by default.",
            "Fix it with either:",
            "",
            "  * Point Python at the mkcert root CA for this shell (add to",
            "    ~/.zshrc next to NODE_EXTRA_CA_CERTS to make it permanent):",
            '      export SSL_CERT_FILE="$(mkcert -CAROOT)/rootCA.pem"',
            "",
            "  * Or set a CA path in .llpm/config.toml so no per-shell env is",
            "    needed:",
            "      [store]",
            '      ca = "/path/to/rootCA.pem"   # `mkcert -CAROOT`/rootCA.pem',
            "",
            "The root CA is also downloadable from https://certs.home.lab",
        ]
        if self._ca:
            lines += [
                "",
                f"(store.ca = {self._ca!r} is configured but verification still "
                "failed — confirm it's the correct mkcert rootCA.pem.)",
            ]
        return "\n".join(lines)

    def _get_json(self, url: str) -> dict:
        with self._open(url) as r:
            return json.loads(r.read())

    def _get_raw(self, stem: str) -> str | None:
        """Fetch raw markdown content for a stem. Returns None on 404."""
        url = self._url(stem) + "/raw"
        try:
            with self._open(url) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _put(self, stem: str, content: str) -> None:
        """Upsert a note (overwrite if exists)."""
        self._invalidate(stem)
        data = json.dumps({"content": content}).encode()
        req = urllib.request.Request(
            self._url(stem),
            data=data,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with self._open(req):
            pass

    def _put_exclusive(self, stem: str, content: str) -> None:
        """Create a note only if it doesn't exist. Raises FileExistsError on 409."""
        self._invalidate(stem)
        data = json.dumps({"content": content}).encode()
        url = self._url(stem) + "?if_absent=true"
        req = urllib.request.Request(
            url,
            data=data,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._open(req):
                pass
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise FileExistsError(f"vault note already exists: {stem}") from e
            raise

    def _delete(self, stem: str) -> None:
        self._invalidate(stem)
        req = urllib.request.Request(self._url(stem), method="DELETE")
        with self._open(req):
            pass

    def _move(self, stem: str, new_stem: str) -> None:
        self._invalidate(stem, new_stem)
        url = (
            self._url(stem)
            + "/move?new_stem="
            + urllib.parse.quote(new_stem, safe="")
        )
        req = urllib.request.Request(url, method="POST", data=b"")
        with self._open(req):
            pass

    # The service's page-size ceiling (its default is 100). A bucket listing
    # also returns every note *below* each ticket, so one default-sized page
    # silently drops real tickets -- and next_id then mints an ID that exists.
    _LIST_PAGE_SIZE = 1000

    def _list_pattern(self, pattern: str, include_frontmatter: bool = False) -> list[dict]:
        """List every note matching a glob pattern, following ``total`` /
        ``offset`` until exhausted. Returns list of {stem, title} dicts --
        plus ``frontmatter`` per item when ``include_frontmatter`` is set,
        which is what makes a whole-board read one request."""
        items: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while True:
            url = (
                self._base
                + "/api/v1/notes?pattern="
                + urllib.parse.quote(pattern, safe="")
                + ("&include=frontmatter" if include_frontmatter else "")
                + f"&limit={self._LIST_PAGE_SIZE}&offset={offset}"
            )
            data = self._get_json(url)
            page = data.get("items", [])
            for item in page:
                # A note created between two page fetches shifts the listing
                # and can repeat an item across the page boundary.
                if item["stem"] not in seen:
                    seen.add(item["stem"])
                    items.append(item)
            offset += len(page)
            if not page or offset >= data.get("total", 0):
                return items

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

    def _is_ticket_stem(self, stem: str) -> bool:
        """True for ``<ns>.<ticket bucket>.<ID>`` exactly.

        Two different things get excluded here, and both callers need both.
        The service's fnmatch ``*`` crosses dots, so ``<ns>.tasks.*`` also
        returns everything below each ticket -- those notes belong to the
        ticket, they are not tickets. And ``load_frontmatter`` globs the whole
        namespace, so it also sees buckets that aren't ticket buckets at all:
        ``<ns>.templates.<type>``, or whatever else a board keeps beside its
        tickets (one board has ``<ns>.milestones.M0``). Those sit at the same
        depth as a ticket, so depth alone can't tell them apart -- and one
        parsed as a ticket is a ``KeyError: 'id'`` on the whole board.
        """
        prefix = self._ns + "."
        if not stem.startswith(prefix):
            return False
        bucket, sep, tail = stem[len(prefix):].partition(".")
        return bool(sep) and "." not in tail and bucket in _TICKET_BUCKETS

    def list_tickets(self, include_archive: bool = True) -> list[VaultRef]:
        refs: list[VaultRef] = []

        for sub_stem in _TYPE_STEMS.values():
            pattern = f"{self._ns}.{sub_stem}.*"
            for item in self._list_pattern(pattern):
                if self._is_ticket_stem(item["stem"]):
                    refs.append(VaultRef(vault_stem=item["stem"], is_archived=False))

        if include_archive:
            pattern = f"{self._ns}.archive.*"
            for item in self._list_pattern(pattern):
                if self._is_ticket_stem(item["stem"]):
                    refs.append(VaultRef(vault_stem=item["stem"], is_archived=True))

        return sorted(refs, key=lambda r: r.vault_stem)

    def load_frontmatter(self, include_archive: bool = True) -> list[tuple[VaultRef, dict]]:
        """The whole board's frontmatter in ONE request (see the protocol).

        ``?include=frontmatter`` over the board's own family returns every
        ticket's parsed frontmatter alongside its stem, so a board load costs
        one round trip instead of a listing per bucket plus a ``/raw`` fetch
        per ticket. On this repo's board that is 1 request where the per-ref
        walk took 45, and from inside a container -- where every request pays
        a fresh DNS + TCP + TLS handshake -- it is the difference between
        ~25 s and well under one.

        Degrades the way the rest of this store does: a vault that doesn't
        support ``include=frontmatter`` (no ``frontmatter`` key in its items)
        falls back to per-stem frontmatter fetches, and a ticket whose
        frontmatter won't parse is skipped rather than failing the board.
        """
        pairs: list[tuple[VaultRef, dict]] = []
        for item in self._list_pattern(f"{self._ns}.*", include_frontmatter=True):
            stem = item["stem"]
            if not self._is_ticket_stem(stem):
                continue  # a note BELOW a ticket is never a ticket
            ref = self._ref_for_stem(stem)
            if ref.is_archived and not include_archive:
                continue
            fm = item.get("frontmatter")
            if fm is None:  # vault predating include=frontmatter on listings
                fm = self._get_frontmatter(stem)
            if fm:
                pairs.append((ref, fm))
        return sorted(pairs, key=lambda p: p[0].vault_stem)

    def _get_frontmatter(self, stem: str) -> dict | None:
        """One note's frontmatter, for the pre-``include=frontmatter``
        fallback. An unreadable note is skipped, never fatal."""
        try:
            return self._get_json(self._url(stem) + "/frontmatter") or {}
        except (urllib.error.HTTPError, MdTreeStoreError):
            return None

    def list_boards(self) -> list[str]:
        """Repo names that have an llpm board in this vault.

        Vault-wide, not board-scoped: the answer doesn't depend on which repo
        this store was built for, so any ``MdTreeStore`` pointed at a vault can
        answer it. Derived from the stems themselves (``repos.<repo>.llpm.…``)
        rather than from a ``repos.*.llpm`` pattern, because the service's
        fnmatch ``*`` crosses dots and intermediate stems aren't always notes.
        """
        boards: set[str] = set()
        for item in self._list_pattern("repos.*.llpm.*"):
            parts = item["stem"].split(".")
            if len(parts) > 3 and parts[0] == "repos" and parts[2] == "llpm":
                boards.add(parts[1])
        return sorted(boards)

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
        # The service's move is subtree-wide: subnotes ride along.
        self._move(ref.vault_stem, new_stem)
        return VaultRef(vault_stem=new_stem, is_archived=True)

    def delete(self, ref: VaultRef) -> None:
        # Unlike move, the service's DELETE removes one note. Clear what hangs
        # below the ticket first, deepest first and the ticket last, so an
        # interrupted delete leaves a ticket with fewer subnotes -- never
        # subnotes under a ticket that no longer exists.
        for stem in sorted(self.subnotes(ref), key=lambda s: s.count("."), reverse=True):
            try:
                self._delete(stem)
            except urllib.error.HTTPError as e:
                if e.code != 404:  # already gone (a concurrent cleanup) is fine
                    raise
        self._delete(ref.vault_stem)

    def subnotes(self, ref: VaultRef) -> list[str]:
        return sorted(i["stem"] for i in self._list_pattern(ref.vault_stem + ".*"))

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

    def begin_read_scope(self) -> None:
        """Empty the foreign-stem cache (see the protocol's docstring).

        Cheap and unconditional: a board load repopulates only the stems it
        actually touches, and every other caller reads one ticket."""
        self._foreign_cache.clear()

    def _invalidate(self, *stems: str) -> None:
        """Forget cached foreign reads of stems this store is about to write.

        A read scope that writes the very note it cached would otherwise go on
        answering with the pre-write frontmatter -- the same staleness across a
        call that ``begin_read_scope`` fixes across calls. Archiving moves a
        ticket to a sibling stem, and ``read_foreign`` follows that move, so a
        cached entry is dropped when *either* spelling is written.
        """
        if not self._foreign_cache:
            return
        targets = set(stems)
        for key in list(self._foreign_cache):
            if key in targets or self._archive_variant(key) in targets:
                del self._foreign_cache[key]

    def read_foreign(self, stem: str) -> tuple[str, dict | None]:
        if stem not in self._foreign_cache:
            self._foreign_cache[stem] = self._read_foreign_uncached(stem)
        return self._foreign_cache[stem]

    def _read_foreign_uncached(self, stem: str) -> tuple[str, dict | None]:
        try:
            content = self._get_raw(stem)
            if content is None:
                # llpm boards move archived tickets to a different stem;
                # follow a completed-and-archived target before declaring
                # it missing.
                archive_stem = self._archive_variant(stem)
                if archive_stem:
                    content = self._get_raw(archive_stem)
            if content is None:
                return ("missing", None)
        except (MdTreeStoreError, urllib.error.HTTPError):
            return ("unavailable", None)

        try:
            fm, _ = self._parse(content, source=stem)
        except (ValueError, yaml.YAMLError):
            return ("error", None)
        return ("ok", fm)

    # Page size for scan_by_type. Small enough that when the bulk
    # include=frontmatter call 500s on one bad note (see _scan_page), the
    # per-stem fallback it triggers only has to redo this many requests,
    # not the whole vault.
    _SCAN_PAGE_SIZE = 100

    def scan_by_type(self, type_value: str) -> list[tuple[str, dict]]:
        """Vault-wide scan for notes with a given frontmatter ``type:`` value.

        Naive full scan, paginated -- the vault has no type index yet
        (deferred; goals-layer design decision 7 builds one when scan cost
        actually bites). Individual notes whose frontmatter the vault can't
        parse are skipped, not fatal (mirrors read_foreign's degrade rule).
        """
        results: list[tuple[str, dict]] = []
        offset = 0
        total = None
        while total is None or offset < total:
            total, pairs = self._scan_page(offset)
            results.extend((stem, fm) for stem, fm in pairs if fm.get("type") == type_value)
            offset += self._SCAN_PAGE_SIZE
        return results

    def _scan_page(self, offset: int) -> tuple[int, list[tuple[str, dict]]]:
        """One page of ``(stem, frontmatter)`` pairs for scan_by_type.

        Falls back to per-stem frontmatter fetches, skipping ones that
        error, if the bulk ``include=frontmatter`` call 500s -- one note
        with frontmatter the vault can't parse server-side must not blind
        the scan to its whole page.
        """
        bulk_url = (
            self._base
            + f"/api/v1/notes?pattern=*&include=frontmatter&limit={self._SCAN_PAGE_SIZE}&offset={offset}"
        )
        try:
            data = self._get_json(bulk_url)
            pairs = [(i["stem"], i.get("frontmatter") or {}) for i in data["items"]]
            return data["total"], pairs
        except urllib.error.HTTPError as e:
            if e.code != 500:
                raise

        stems_url = (
            self._base + f"/api/v1/notes?pattern=*&limit={self._SCAN_PAGE_SIZE}&offset={offset}"
        )
        data = self._get_json(stems_url)
        pairs = []
        for entry in data["items"]:
            stem = entry["stem"]
            try:
                fm = self._get_json(self._url(stem) + "/frontmatter")
            except urllib.error.HTTPError:
                continue  # unreadable note -- skip, don't fail the whole scan
            pairs.append((stem, fm))
        return data["total"], pairs

    @staticmethod
    def _archive_variant(stem: str) -> str | None:
        """For an llpm-board ticket stem (``....llpm.<type>.<ID>``), the stem
        the ticket would have after archiving; None for non-board stems."""
        parts = stem.split(".")
        if len(parts) >= 3 and parts[-3] == "llpm" and parts[-2] != "archive":
            return ".".join(parts[:-2] + ["archive", parts[-1]])
        return None

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
