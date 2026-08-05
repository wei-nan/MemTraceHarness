# MemTraceHarness

CLI-only execution harness between MemTrace and local coding agents.

```text
MemTrace <-> MemTraceHarness -> claude CLI
                              -> codex CLI
                              -> agy CLI (Antigravity)
```

Model execution never uses Anthropic, OpenAI, or Google provider HTTP APIs or SDKs. Each adapter
starts the provider's installed, authenticated CLI as a subprocess and captures its structured
output. MemTrace MCP remains an optional control-plane connection for context hydration and
draft-only writeback; it is not a model transport.

## What the harness owns

- normalized task envelopes and explicit provider selection
- validated role profiles and a bounded multi-stage Agent Loop
- non-interactive CLI execution with `shell=False`, cwd, timeout, and exit status
- provider JSONL capture and normalized token usage
- raw stdout/stderr traces outside MemTrace
- SQLite conversation/run/turn/checkpoint summaries, quota state, and evidence references
- draft-only MemTrace writeback when explicitly requested

The harness does not approve its own result, resolve an inquiry, adopt an Improvement Loop
proposal, turn model consensus into canonical knowledge, or claim that MemTrace changed a gate,
blocked, rejected, reject-count, or completed state.

## Prerequisites

Install and authenticate the CLIs you intend to use:

| Agent | Default executable | Non-interactive invocation | Usage handling |
| --- | --- | --- | --- |
| Claude | `claude` | `--print --output-format stream-json --verbose` | Provider usage and cost from the result event |
| Codex | `codex` | `exec --json --sandbox workspace-write` | Per-turn input, cached input, output, and reasoning tokens |
| Antigravity | `agy` | `--print --sandbox`; adds stream JSON when supported | Version-sensitive; normalized as partial and raw output remains authoritative |

Run a capability check without spending model quota:

```powershell
cd D:\Workspace\MemTraceHarness
.\.venv\Scripts\python -m memtrace_harness probe --json
```

Missing commands, Windows execution permission errors, and non-zero version checks are reported as
unavailable. `probe` never submits a model prompt.

## Install

```powershell
cd D:\Workspace\MemTraceHarness
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

No provider API key is accepted by MemTraceHarness. Authentication belongs to each provider CLI.

## Run

Agent selection is required so an old dry-run command cannot unexpectedly consume provider quota.
One run executes one provider; this prevents multiple editing agents from contaminating the same
working tree and makes token attribution reproducible:

```powershell
.\.venv\Scripts\python -m memtrace_harness run `
  --workspace ws_spec_plan `
  --working-directory D:\Workspace\MemTrace `
  --goal "Review the current Agent Loop token attribution" `
  --context-ref mem_f2e46f6a `
  --agent codex `
  --json
```

The process exits `0` only when the selected CLI succeeds. A missing CLI, timeout, permission
failure, or non-zero exit remains a persisted failed execution and returns exit code `1`.

To compare Claude, Codex, and Antigravity, create an isolated worktree for each provider and start
one run per worktree with the same task envelope. Shared-worktree fan-out is intentionally not
implemented in v0.4.

## Bounded Agent Loop

The `loop` command executes the accepted role policy serially:

```text
Luna control -> Sonnet plan -> Sol G1 -> Gemini develop -> Sol G2 -> Luna converge
```

All low-, medium-, and high-risk tasks start with Sonnet. Risk changes the evidence and gate
scrutiny, not the planner model. Opus is absent from the normal path and is invoked only when the
first Sol G1 verdict is `REJECT` with `reason_code=reasoning_gap`. Other repairable G1 findings get
one evidence-backed Sonnet revision. A repairable G2 rejection gets one Gemini revision. A second
rejection stops for human review.

```powershell
$env:HARNESS_CODEX_COMMAND = Join-Path `
  $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe"

.\.venv\Scripts\python -m memtrace_harness loop `
  --workspace ws_spec_plan `
  --working-directory D:\Workspace\MemTrace `
  --goal "Implement the accepted Agent Loop change" `
  --risk-level high `
  --max-total-tokens 120000 `
  --json
```

The JSON result includes a `conversation_id`. Pass it to a later invocation to cold-start from the
latest durable `ResumeEnvelope`:

```powershell
.\.venv\Scripts\python -m memtrace_harness loop `
  --workspace ws_spec_plan `
  --working-directory D:\Workspace\MemTrace `
  --goal "Continue the accepted Agent Loop change" `
  --conversation-id conv_example123 `
  --json
```

Every attempt is stored before another provider is invoked. Continuation currently re-enters at the
Controller with the latest bounded envelope; exact stage-machine resume and semantic compaction are
not yet implemented.

The packaged profile policy lives in
[`src/memtrace_harness/default-role-profiles.toml`](src/memtrace_harness/default-role-profiles.toml):

| Profile | Provider/model | Permission | Purpose |
| --- | --- | --- | --- |
| `controller` | Codex `gpt-5.6-luna` | read-only | Start routing and convergence; quota fallback to Sonnet then Gemini 3.6 |
| `planner` | Claude `sonnet` | read-only | Default planning for every risk level |
| `planner-escalation` | Claude `opus` | read-only | G1 reasoning-gap escalation only |
| `red-team` | Codex `gpt-5.6-sol` | read-only | G1/G2 adversarial review |
| `developer` | Antigravity `gemini-3.1-pro-high` | workspace-write | Accepted-plan implementation and one bounded correction |

Use `--profiles-file <path.toml>` to load another file. Validation keeps Luna, Sonnet, Opus, and
Sol in their accepted roles, requires the developer to use a `gemini-*` model through Antigravity,
and rejects any profile set that grants write access to a role other than `developer`.

Each role also receives a packaged JSON Schema through the provider's structured-output flag. An
invalid or missing final JSON object stops the loop; the Harness never infers a PASS from prose.

Only classified `quota_exhausted`, `rate_limit`, or `provider_overloaded` failures may cross a
provider boundary. Context, timeout, network, authentication, permission, configuration, schema,
safety, and unknown failures stop closed. Quota buckets are explicit: if Luna and Sol share the
same Codex account bucket, a Luna quota failure also prevents the Harness from probing Sol during
cooldown, so the Red Team gate cannot pass.

`--max-total-tokens` is enforced at stage boundaries. It uses provider `total_tokens` when present,
otherwise input plus output tokens; cached input is not added a second time. If any provider reports
usage as unavailable under a hard budget, the loop stops closed with `budget_exhausted`. A stage can
cross the limit before the Harness observes its terminal usage; the Harness does not pretend to
provide a pre-token hard cutoff.

The controller runs from a neutral Harness directory and asks Codex to ignore user config and
exec-policy rules. Its prompt receives a compact snapshot rather than repository bodies or raw
transcripts. This is a best-effort context reduction: measure actual CLI input usage because host
or provider instructions may still be injected.

### Optional MemTrace context and writeback

```powershell
$env:MEMTRACE_MCP_URL = "http://localhost:8000/api/v1/mcp/mcp"
$env:MEMTRACE_API_TOKEN = "<MemTrace token>"

.\.venv\Scripts\python -m memtrace_harness run `
  --workspace ws_spec_plan `
  --working-directory D:\Workspace\MemTrace `
  --goal "Review the harness boundary" `
  --context-ref mem_f2e46f6a `
  --hydrate-context `
  --writeback `
  --agent claude
```

Writeback contains status, normalized usage, claims, and local trace references. Provider raw
stdout/stderr is not copied into MemTrace. Local summary and execution rows are committed before the
optional MCP writeback, so an external write failure does not erase already-spent model evidence.

## Configuration

See `.env.example`. Command variables accept an executable name or explicit path, which is useful
when WindowsApps or PATH resolution prevents a CLI from starting.

```text
HARNESS_CLAUDE_COMMAND=claude
HARNESS_CODEX_COMMAND=codex
HARNESS_ANTIGRAVITY_COMMAND=agy
HARNESS_ANTIGRAVITY_OUTPUT_MODE=auto
HARNESS_CLI_TIMEOUT_SECONDS=900
HARNESS_TRACE_DB=data/harness.sqlite3
HARNESS_TRACE_ROOT=data/traces
```

`HARNESS_ANTIGRAVITY_OUTPUT_MODE=auto` inspects `agy --help` without a model call. The locally
verified Antigravity 1.1.10 advertises stream JSON and model selection. Its usage schema remains
version-sensitive and is normalized as partial until pinned by real fixtures.

## Traces

```text
data/
  harness.sqlite3
  traces/
    run_<id>/
      01_control-start/
        attempt_0/
          controller.stdout.log
      02_plan/
        attempt_0/
          planner.stdout.log
      ...
```

SQLite stores task/run metadata, conversations, turns, provider sessions, stage/profile/model
attribution, checkpoints, quota availability, normalized usage, local outcome status, and trace
references. The raw provider stream stays in the trace directory so parser changes can be replayed
without expanding the MemTrace knowledge graph.

See [`docs/operating-contract.md`](docs/operating-contract.md) for behavioral status and
[`docs/architecture.md`](docs/architecture.md) for adapter and usage contracts.

## Verify without model calls

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python -m compileall -q src tests
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Tests inject deterministic subprocess results; they do not launch Claude, Codex, or Antigravity.
