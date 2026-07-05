# LLPM — LLM Project Manager

A CLI tool for stateless, markdown-based project management designed for LLM
multi-agent workflows. All state lives in markdown files with YAML frontmatter;
the CLI handles ID generation, status validation, blocker resolution, and date
tracking.

## Quick start

```bash
uv sync                    # install deps
uv run llpm --help         # see all commands
uv run llpm init           # set up llpm/tickets/ and llpm/templates/
uv run pytest -x -v        # run the test suite
```

See `llpm help` (or `CLAUDE.md`) for the full command reference.

## Store configuration

By default LLPM stores tickets on the local filesystem under `./llpm/`. The
store is resolved in this order:

1. `--docs-root` flag (forces a local dir store)
2. `LLPM_DOCS_ROOT` env var (forces a local dir store)
3. `.llpm/config.toml`, discovered by walking up from the current directory
4. Default: `./llpm/`

### `.llpm/config.toml`

Local directory store (the default):

```toml
[store]
kind = "dir"
root = "./llpm"      # resolved relative to the config file
```

Vault store (tickets kept in the agent-memory / markdown-tree-service vault over
its HTTP API):

```toml
[store]
kind = "mdtree"
url  = "https://agent-memory.home.lab"
stem = "myrepo"                        # repos.<stem>.llpm.* namespace
ca   = "/path/to/rootCA.pem"           # optional — see TLS trust below
```

## TLS trust for the vault store (`kind = "mdtree"`)

The homelab serves `*.home.lab` with a certificate signed by a **mkcert** root
CA. Python's stdlib `urllib` (which LLPM uses — no extra deps) does **not** read
the macOS system trust store, so that CA is not trusted out of the box even
though your browser and `curl` accept it. `NODE_EXTRA_CA_CERTS` (exported for
node MCP clients) does nothing for Python.

Without trust configured you'll get a clear, actionable error rather than a
urllib traceback. Fix it one of two ways:

**Option A — environment variable (per shell, or make it permanent).** Python's
`ssl` honors `SSL_CERT_FILE` / `SSL_CERT_DIR`:

```bash
export SSL_CERT_FILE="$(mkcert -CAROOT)/rootCA.pem"
```

Add that line to your `~/.zshrc`, right next to `NODE_EXTRA_CA_CERTS`, so every
shell trusts the homelab CA.

**Option B — `ca` key in `.llpm/config.toml` (per repo, no env needed).** Point
the store at the mkcert root CA so the repo works without any per-shell setup:

```toml
[store]
kind = "mdtree"
url  = "https://agent-memory.home.lab"
stem = "myrepo"
ca   = "/Users/you/Library/Application Support/mkcert/rootCA.pem"
```

Get the exact path with `mkcert -CAROOT` (the file is `rootCA.pem` inside that
directory). Relative `ca` paths resolve against the config file's directory and
`~` is expanded. The root CA is also downloadable from https://certs.home.lab.
