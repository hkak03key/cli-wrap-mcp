# cli-wrap-mcp

Turn any CLI into an [MCP](https://modelcontextprotocol.io/) server with a declarative YAML config.

`cli-wrap-mcp` is a small engine that reads a YAML file describing a set of tools
(each tool = one argv template + typed, validated parameters) and serves them as an
MCP stdio server. No code generation, no per-tool server projects — one config file
per server.

```yaml
server:
  name: gh-explorer
  description: Read-only GitHub exploration tools.

tools:
  - name: pr_view
    description: Show a pull request.
    argv: ["gh", "pr", "view", "{number}", "--repo", "{repo}", "--json", "title,body,state"]
    params:
      number:
        type: integer
        description: PR number.
      repo:
        type: string
        description: Repository in owner/name form.
        pattern: "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
```

## Why

Giving an agent raw shell access means permissioning at the granularity of "can run
commands". Wrapping a CLI as an MCP server flips that: the agent sees a small set of
**narrow, typed, validated tools**, and you manage permissions per MCP server / per
tool. A config file is all it takes to mint a new server, so each domain (GitHub
exploration, build tooling, ...) can ship its own MCP definition without owning any
engine code.

## Safety design

Safety is the core of this engine, enforced at execution and load time:

- **No shell, ever.** Commands run as `argv` arrays with `shell=False`. There is no
  code path that concatenates a shell string, so `; rm -rf /`, `$(...)`, pipes etc.
  stay inert single arguments.
- **Validation before interpolation.** Every parameter value must pass its `type`
  check, `pattern` (regex **fullmatch**), and `enum` before it is rendered into argv.
- **Argument-injection guard.** Rendered values starting with `-` are rejected by
  default, so a model cannot smuggle `--force`-style flags into a positional slot.
  Opt out per parameter with `allow_dash_prefix: true`.
- **Strict placeholders.** `{param}` only — format specs, conversions, attribute or
  index access (`{p.__class__}`) are load-time errors, as are placeholders that
  reference undefined parameters.
- **stdout is protocol-only.** The MCP stdio channel is never polluted; all engine
  logging goes to stderr.
- **Bounded output.** Tool output is truncated at `max_output_bytes` by default;
  `on_large_output: spill` writes the full output to a cache file and returns only
  the path plus head/tail excerpts. Callers can also pass the auto-injected
  `output_dir` parameter to force the full stdout into a file of their choosing.
- **Job isolation.** Background job IDs are strictly format-checked, blocking path
  traversal through `job_id`.

Trust model: the YAML config is a trusted local file (it decides *which* binaries
can run); the tool *arguments* coming from the model are untrusted and constrained
as above. The wrapped CLI runs with your local privileges.

## Install / run

Requires Python >= 3.11. With [uv](https://docs.astral.sh/uv/):

```sh
uvx cli-wrap-mcp@0.1.0 --config /path/to/config.yml
```

Or straight from git (pin a tag, or a commit SHA for full immutability):

```sh
uvx --from git+https://github.com/hkak03key/cli-wrap-mcp@v0.1.0 cli-wrap-mcp --config /path/to/config.yml
uvx --from git+https://github.com/hkak03key/cli-wrap-mcp@<commit-sha> cli-wrap-mcp --config /path/to/config.yml
```

Try the bundled examples:

```sh
uvx cli-wrap-mcp@0.1.0 --config examples/echo.yml
```

### Claude Code

Project `.mcp.json`:

```json
{
  "mcpServers": {
    "echo-demo": {
      "command": "uvx",
      "args": ["cli-wrap-mcp@0.1.0", "--config", "./configs/echo.yml"]
    }
  }
}
```

From a Claude Code plugin, ship only your configs and reference them via
`${CLAUDE_PLUGIN_ROOT}`:

```json
{
  "mcpServers": {
    "gh-explorer": {
      "command": "uvx",
      "args": ["cli-wrap-mcp@0.1.0", "--config", "${CLAUDE_PLUGIN_ROOT}/configs/gh-explorer.yml"]
    }
  }
}
```

## Config reference

Top level:

| Key | Required | Description |
|:----|:---------|:------------|
| `server.name` | yes | MCP server name. |
| `server.description` | no | Served as the MCP `instructions`. |
| `defaults.on_large_output` | no | Default large-output mode for all tools: `truncate` (default) or `spill`. |
| `tools` | yes | List of tool definitions (at least one). |

Per tool:

| Key | Required | Default | Description |
|:----|:---------|:--------|:------------|
| `name` | yes | — | Tool name, `[A-Za-z0-9_-]+`. Must be unique (including job-generated `_start`/`_status`/`_result`/`_cancel` names). |
| `description` | no | `name` | Tool description shown to the model. |
| `argv` | yes | — | Non-empty list of strings. `{param}` placeholders are substituted after validation; each element stays a single argv entry. |
| `mode` | no | `sync` | `sync` (run and return) or `job` (background, see below). |
| `timeout_sec` | no | `60` | Sync-mode timeout. |
| `max_output_bytes` | no | `50000` | Output size limit returned inline. |
| `on_large_output` | no | inherits `defaults` | `truncate` or `spill`. |
| `params` | no | `{}` | Mapping of parameter name → spec. |

Per parameter (`params.<name>`):

| Key | Required | Default | Description |
|:----|:---------|:--------|:------------|
| `type` | no | `string` | `string`, `integer`, or `boolean` (booleans render as `true`/`false`). |
| `description` | no | `""` | Shown in the tool schema. |
| `required` | no | `true` | Optional parameters must have a `default` if referenced in argv. |
| `pattern` | no | — | Regex, string params only, matched with `fullmatch`. |
| `enum` | no | — | Allowed values (type-checked at load time). |
| `default` | no | — | Used when the argument is omitted (type-checked at load time). |
| `allow_dash_prefix` | no | `false` | Permit values starting with `-` (off by default; injection guard). |

Parameter names must match `[a-z_][a-z0-9_]*` and must not be Python keywords.
`output_dir` is **reserved**: the engine injects it into every sync tool as an
optional absolute-path parameter; when set, the full stdout is always written to a
file there and only the file path plus excerpts are returned.

Spill files and job state live under `~/.cache/cli-mcp/<server>/` (override with
`CLI_MCP_CACHE_DIR`).

## Job mode

`mode: job` wraps long-running commands. Instead of one tool, four are exposed:

- `<name>_start` — starts the command detached (own process group), returns a `job_id`
- `<name>_status` — running/exited state plus stdout/stderr tails
- `<name>_result` — final output (tail-limited by `max_output_bytes`)
- `<name>_cancel` — SIGTERM to the whole process group

Job logs and metadata persist under the cache dir, so finished jobs remain
inspectable (best-effort) even across server restarts. See
[`examples/sleep-job.yml`](examples/sleep-job.yml).

## Development

```sh
uv sync
uv run pytest
```

Releases are published to PyPI via GitHub Releases using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no API tokens) —
see [`.github/workflows/publish.yml`](.github/workflows/publish.yml).

## License

[MIT](LICENSE)
