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

## MCP server

`llpm mcp` serves the board to an MCP client over stdio, so an agent session
changes tickets through llpm's rules — intake policy, provenance, the edge
vocabulary, status transitions — instead of writing notes behind its back. Every
tool is one service-layer function, the same ones the CLI and the HTTP API call.

Register it with Claude Code:

```bash
claude mcp add llpm -- llpm mcp        # run from the repo whose board you want
```

or, checked into the repo as `.mcp.json`:

```json
{
  "mcpServers": {
    "llpm": { "command": "llpm", "args": ["mcp"] }
  }
}
```

It needs no optional extra (stdlib + pyyaml), speaks JSON-RPC on stdin/stdout,
and logs to stderr. The board is whichever one the store resolution above finds,
so the server is started in the repo it belongs to.

Thirteen tools: `list_tickets`, `get_ticket`, `create_ticket`, `set_status`,
`set_fields`, and the four edge pairs `blocker_add`/`blocker_rm`,
`after_add`/`after_rm`, `waits_add`/`waits_rm`, `serves_add`/`serves_rm`. Each
answers with what its service function returns, and a rule llpm refuses comes
back as a tool error carrying llpm's own message rather than a protocol failure.

**Provenance.** Tickets filed through MCP are `origin: agent` (pass
`origin: "human"` when relaying a request a person made) and therefore land
`draft` unless the board's `[intake] auto_approve` list covers their type.
`created_by` is the caller, resolved in this order: the argument on the call,
then `--created-by` / `LLPM_CREATED_BY`, then the name the client gave in the
MCP handshake. This server never runs git, so `set_status` records only the
commit SHAs you name — the CLI is still what harvests them from a checkout.

**Remotely.** `llpm serve` (the `llpm[api]` extra) mounts the same tools at
`POST /<board>/mcp` over the streamable-HTTP transport — JSON in, JSON out, no
SSE and no server-side session. Because nothing survives the request, an HTTP
caller names `created_by` in the tool arguments, exactly as it does for
`POST /<board>/tickets`.

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
