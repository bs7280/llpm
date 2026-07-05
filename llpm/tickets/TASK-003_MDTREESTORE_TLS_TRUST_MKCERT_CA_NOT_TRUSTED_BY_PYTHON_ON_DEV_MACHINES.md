---
id: "TASK-003"
type: task
title: "MdTreeStore TLS trust: mkcert CA not trusted by Python on dev machines"
status: open  # draft | planned | open | in-progress | review | complete | closed | deferred (blocked is derived)
priority: medium  # low | medium | high
effort: small  # trivial | small | medium | large | xlarge
requires_human: false
parent: FEAT-002
blockers: []
created: "2026-07-05"
updated: "2026-07-05"
completed: null
tags: [store, infra]
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
