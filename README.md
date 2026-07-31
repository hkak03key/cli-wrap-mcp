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
- **Bounded output.** Inline tool output is truncated at `inline_max_output_bytes`
  by default; `inline_on_large_output: file` diverts oversized output to a file
  (path plus head/tail excerpts), and `output_mode: file` always writes the full
  output to a file — success or failure — for audit-trail use. Callers can also
  pass the auto-injected `file_output_dir` parameter to force the full output into
  a directory of their choosing.
- **Job isolation.** Background job IDs are strictly format-checked, blocking path
  traversal through `job_id`.
- **Guardrails, not a sandbox.** For "arbitrary subcommand" tools (an `array` param
  with `allow_dash_prefix: true`), `deny_pattern`, forced trailing flags, and `env`
  forcing constrain what the model can do — but a determined CLI often has more than
  one spelling for the same effect. Treat them as accident prevention and put the
  real security boundary in the credentials the wrapped CLI runs with (see
  [`examples/gcloud.yml`](examples/gcloud.yml)).

Trust model: the YAML config is a trusted local file (it decides *which* binaries
can run); the tool *arguments* coming from the model are untrusted and constrained
as above. The wrapped CLI runs with your local privileges.

## Install / run

Requires Python >= 3.11. With [uv](https://docs.astral.sh/uv/):

```sh
uvx cli-wrap-mcp@0.2.1 --config /path/to/config.yml
```

Or straight from git (pin a tag, or a commit SHA for full immutability):

```sh
uvx --from git+https://github.com/hkak03key/cli-wrap-mcp@v0.2.1 cli-wrap-mcp --config /path/to/config.yml
uvx --from git+https://github.com/hkak03key/cli-wrap-mcp@<commit-sha> cli-wrap-mcp --config /path/to/config.yml
```

Try the bundled examples:

```sh
uvx cli-wrap-mcp@0.2.1 --config examples/echo.yml
```

The server speaks MCP on stdio, so drive it from a client rather than a pipe or a
file redirect (see [Known limitations](#known-limitations)).

[`examples/gcloud.yml`](examples/gcloud.yml) shows the "arbitrary subcommand with
forced env vars and options" pattern (variadic `array` param + `deny_pattern` +
`env` forcing).

### Claude Code

Project `.mcp.json`:

```json
{
  "mcpServers": {
    "echo-demo": {
      "command": "uvx",
      "args": ["cli-wrap-mcp@0.2.1", "--config", "./configs/echo.yml"]
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
      "args": ["cli-wrap-mcp@0.2.1", "--config", "${CLAUDE_PLUGIN_ROOT}/configs/gh-explorer.yml"]
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
| `defaults.output_mode` | no | Default output mode for all tools: `inline` (default) or `file` (see per-tool `output_mode`). |
| `defaults.inline_max_output_bytes` | no | Default inline size limit for all tools. |
| `defaults.inline_on_large_output` | no | Default overflow behavior for all tools: `truncate` (default) or `file`. |
| `defaults.file_output_dir` | no | Default output root for all tools (absolute path; see per-tool `file_output_dir`). |
| `defaults.env` | no | Environment variables forced for every tool (mapping of `VAR_NAME` → string; quote numbers). Merged over the inherited environment at execution time, so config values always win. |
| `tools` | yes | List of tool definitions (at least one). |

Per tool:

| Key | Required | Default | Description |
|:----|:---------|:--------|:------------|
| `name` | yes | — | Tool name, `[A-Za-z0-9_-]+`. Must be unique (including job-generated `_start`/`_status`/`_result`/`_cancel` names). |
| `description` | no | `name` | Tool description shown to the model. |
| `argv` | yes | — | Non-empty list of strings. `{param}` placeholders are substituted after validation; each element stays a single argv entry. |
| `mode` | no | `sync` | `sync` (run and return) or `job` (background, see below). |
| `timeout_sec` | no | `60` | Sync-mode timeout. |
| `output_mode` | no | inherits `defaults` (`inline`) | `inline`: output is returned in the reply, subject to `inline_max_output_bytes`. `file`: output is **always** written to a file in full — success or failure, any size — and the reply carries the path plus head/tail excerpts (audit trail). |
| `inline_max_output_bytes` | no | inherits `defaults` (`50000`) | Inline size limit (`output_mode: inline` only; also caps job `_result` tails). |
| `inline_on_large_output` | no | inherits `defaults` (`truncate`) | What happens when inline output exceeds the limit: `truncate` (excess is lost) or `file` (full output goes to a file, reply carries path plus excerpts). |
| `file_output_dir` | no | inherits `defaults` (cache dir) | Output root for this tool (absolute path). File outputs go to `<root>/outputs/`, job state to `<root>/jobs/`, so all traces of a tool accumulate under one configured location. |
| `params` | no | `{}` | Mapping of parameter name → spec. |
| `env` | no | `{}` | Environment variables forced for this tool. Merged over `defaults.env` (tool wins), then over the inherited environment at execution time. |

Per parameter (`params.<name>`):

| Key | Required | Default | Description |
|:----|:---------|:--------|:------------|
| `type` | no | `string` | `string`, `integer`, `boolean` (booleans render as `true`/`false`), or `array` (list of strings, see below). |
| `description` | no | `""` | Shown in the tool schema. |
| `required` | no | `true` | Optional parameters must have a `default` if referenced in argv (optional arrays implicitly default to `[]`). |
| `pattern` | no | — | Regex allowlist, string/array params, matched with `fullmatch` (per item for arrays). |
| `deny_pattern` | no | — | Regex blocklist, string/array params: a value that `fullmatch`es is rejected (per item for arrays). Combine with `allow_dash_prefix: true` to allow flags in general while blocking specific ones. |
| `enum` | no | — | Allowed values (type-checked at load time; string items for arrays). |
| `default` | no | — | Used when the argument is omitted (type-checked at load time). |
| `allow_dash_prefix` | no | `false` | Permit values starting with `-` (off by default; injection guard). Applies per item for arrays. |

### Array (variadic) parameters

`type: array` accepts a list of strings and expands into that many argv elements —
use it to pass a variable-length subcommand tail (`gcloud {args}`). Rules:

- The placeholder must be an **entire argv element** (`"{args}"`); embedding it in a
  larger element (`"--x={args}"`) is a load-time error, because the expansion would
  collapse into one element and change meaning.
- `pattern`, `deny_pattern`, `enum`, and the dash-prefix guard are applied to
  **each item** individually; every item stays exactly one argv element (no shell,
  no word splitting).
- An empty list expands to zero elements. Optional arrays default to `[]` unless an
  explicit `default` is given.
- Fixed argv elements placed *after* the placeholder still apply, which lets a config
  force trailing flags that override anything the model passed earlier (for
  argparse-style CLIs the last occurrence of a flag wins).

Parameter names must match `[a-z_][a-z0-9_]*` and must not be Python keywords.
`file_output_dir` is **reserved**: the engine injects it into every sync tool as an
optional absolute-path parameter; when set, the full output is always written under
that directory — regardless of size, exit code, or the tool's `output_mode` — and
only the file path plus excerpts are returned. It overrides the config-level
`file_output_dir` for that call.

### File output layout

Every file output is a per-invocation directory (same layout as job dirs):

```
<root>/outputs/<tool>-<timestamp>-<id>/
  stdout.log   # full stdout
  stderr.log   # full stderr
  meta.json    # tool, argv, started_at, exit_code (timed_out on timeout)
<root>/jobs/<job_id>/
  stdout.log  stderr.log  meta.json  pid  exit_code
```

`meta.json` records what was executed, so a `file`-mode tool leaves a self-contained
audit trail: what ran, when, what it printed (including failures and timeouts,
best-effort). `<root>` resolution: per-call `file_output_dir` param > tool
`file_output_dir` > `defaults.file_output_dir` > `~/.cache/cli-mcp/<server>/`
(override the cache location with `CLI_MCP_CACHE_DIR`).

## Job mode

`mode: job` wraps long-running commands. Instead of one tool, four are exposed:

- `<name>_start` — starts the command detached (own process group), returns a `job_id`
- `<name>_status` — running/exited state plus stdout/stderr tails
- `<name>_result` — final output (tail-limited by `inline_max_output_bytes`)
- `<name>_cancel` — SIGTERM to the whole process group

Job logs and metadata persist under `<root>/jobs/` (the tool's `file_output_dir`,
or the cache dir), so finished jobs remain inspectable (best-effort) even across
server restarts. See [`examples/sleep-job.yml`](examples/sleep-job.yml).

## Known limitations

**Feeding requests in from a pipe or a file loses the tail of the replies.** When
stdin reaches EOF, the MCP Python SDK tears the session down without waiting for the
requests it is still handling, so the last ones come back unanswered — the commands
themselves already ran, to completion or until `timeout_sec` killed them; only the
responses are never written. A missing reply therefore never means the command was
skipped, and re-running it repeats its side effects. A `mode: job` command is worse
off: it keeps running in the background and the lost reply takes its `job_id` with
it, leaving it findable only under `<root>/jobs/`. How much of the tail goes missing
depends on timing, so a batch that came back whole once is no guarantee for the next.
Tracked upstream as
[python-sdk#2678](https://github.com/modelcontextprotocol/python-sdk/issues/2678).

Closing the server's stdout early is a separate hazard, not covered by that upstream
issue: the SDK dies on a `BrokenPipeError`. Cut it as early as `… | head -1` and it
dies before most of the commands run, taking the replies of the ones that did run
with it.

What keeps a caller clear of both is reading each reply before stdin closes,
whatever the medium — pasting a batch into a terminal and pressing Ctrl-D drops
replies just like a pipe does. To try a config out by hand, drive the server from a
client that waits. [Issue #7](https://github.com/hkak03key/cli-wrap-mcp/issues/7)
records how both show up here.

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
