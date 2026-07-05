---
id: TASK-003
type: task
title: 'MdTreeStore TLS trust: mkcert CA not trusted by Python on dev machines'
status: review
priority: medium
effort: small
requires_human: false
parent: FEAT-002
blockers: []
created: '2026-07-05'
updated: '2026-07-05'
completed: null
tags:
- store
- infra
model_tier: light
---
## Problem
First consumer-repo e2e (2026-07-05): `llpm list` from claude-tools (`.llpm/config.toml` →
kind=mdtree, url=https://agent-memory.home.lab, stem=claude-tools) fails out of the box:

    ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

`MdTreeStore` uses stdlib urllib with the default SSL context (src/llpm/store.py `_get_json` /
`_get_raw` / `_put` / `_delete` / `_move` / `_list_pattern`). Python doesn't read the macOS
system trust store, so the homelab's mkcert root CA (signs `*.home.lab`) isn't trusted.
`NODE_EXTRA_CA_CERTS` (already exported for node MCP clients) does nothing for Python.

## Verified workaround (2026-07-05)

    SSL_CERT_FILE="$(mkcert -CAROOT)/rootCA.pem" llpm list

works — stdlib `ssl.create_default_context()` honors `SSL_CERT_FILE`/`SSL_CERT_DIR`. That run
was also the first successful llpm-over-vault read from a consumer repo (listed the empty
`repos.claude-tools.llpm.*` board).

## Scope
1. Catch `URLError`-wrapped `SSLCertVerificationError` in MdTreeStore and re-raise with an
   actionable message (point at `SSL_CERT_FILE`, `mkcert -CAROOT`, certs.home.lab) instead of a
   40-line urllib traceback.
2. Optional `ca = "/path/to/rootCA.pem"` key under `[store]` in `.llpm/config.toml` →
   `ssl.create_default_context(cafile=...)` threaded through the urllib calls, so a repo works
   without per-shell env setup.
3. Docs: client-trust requirement in README/store docs; recommend exporting `SSL_CERT_FILE`
   in `~/.zshrc` next to `NODE_EXTRA_CA_CERTS`.

## Acceptance
- Dev machine without env setup: clear, actionable error (or working `ca` config key).
- With `SSL_CERT_FILE` set: everything works (already true today).
- Coordinates with markdown-tree-service TASK-002 (llpm-over-vault e2e) — that e2e should
  exercise a TLS-verified client path, not localhost/http.

Context: vault `area.homelab.agent-platform.task-fabric.rollout` § Status snapshot (2026-07-05).

## Resolution (2026-07-05)
All three scope items done in `src/llpm/store.py`, `src/llpm/commands.py`,
`src/llpm/__main__.py`, `README.md`; tests in `tests/test_mdtreestore.py`.

- **Item 1 — actionable errors.** Centralized every `urllib.request.urlopen`
  call in `MdTreeStore` behind `_open()`, which threads the SSL context and
  translates transport failures into `MdTreeStoreError` (new). A
  `URLError`-wrapped `SSLCertVerificationError` → the TLS hint (`_tls_hint()`:
  points at `SSL_CERT_FILE`, `mkcert -CAROOT`, `~/.zshrc`, certs.home.lab).
  `HTTPError` (404/409) still propagates unchanged so `read`/`create_exclusive`
  keep working. Also translated generic transport failures (DNS/refused/reset —
  down vault / wrong URL) into a concise "Could not reach the vault at …"
  message, since those hit the same 40-line-traceback problem. `__main__.py`
  catches `MdTreeStoreError` and prints `Error: <msg>` with exit 1.
- **Item 2 — `ca` config key.** `[store] ca = "…"` parsed in
  `_find_repo_config` (relative → resolved against the config dir; `~`
  expanded), threaded through `_make_store*` into `MdTreeStore(ca=…)`. Built
  lazily via `ssl.create_default_context(cafile=ca)` in `_context()`; a bad `ca`
  path → actionable `MdTreeStoreError`. `ca=None` → context `None` → stdlib
  default (still honors `SSL_CERT_FILE`).
- **Item 3 — docs.** README now has "Store configuration" + "TLS trust for the
  vault store" sections (Option A `SSL_CERT_FILE` in `~/.zshrc`, Option B `ca`
  key).

Verified end-to-end (real urllib, not mocks): untrusted-cert host → clean TLS
hint, no traceback; unresolvable host → concise "Could not reach the vault";
valid `ca` against a trusted host → past TLS with no cert error. 222 tests pass
(6 new: TLS/unreachable/ca-threading/bad-ca + 2 config-parse).
