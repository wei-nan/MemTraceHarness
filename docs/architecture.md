# CLI-first architecture

## Boundary

MemTrace is the knowledge, evidence, and minimal coordination plane. MemTraceHarness is the runtime
and measurement plane. The three model providers are reachable only through their local CLIs.

```text
MemTrace MCP                         local authenticated CLIs
     |                                          |
     v                                          v
context refs -> TaskEnvelope -> CliModelAdapter -> CliProcessRunner
                                      |                 |
                                      |                 +-> stdout JSONL
                                      |                 +-> stderr / exit / timeout
                                      v
                              normalized ModelResponse
                                      |
                          +-----------+-----------+
                          |                       |
                          v                       v
SQLite ledger          raw trace files
                          |
                          v (explicit only)
                  MemTrace draft summary
```

The SQLite ledger is also the durable conversation plane. It stores conversations, turns, provider
sessions, checkpoints, memory references and quota-bucket availability. Raw events remain the
authoritative replay source.

Provider API clients, API endpoints, and provider API-key configuration are outside the design.
`MemTraceClient` speaks MCP JSON-RPC to MemTrace only and cannot execute a model.

## Adapter contract

Every provider adapter must:

1. build an argv list for one non-interactive turn;
2. execute through `CliProcessRunner` with `shell=False`;
3. preserve the raw stdout/stderr before parsing;
4. identify the provider run/session/thread when exposed;
5. normalize usage without inventing unavailable fields;
6. return `failed`, `timed_out`, or `unavailable` instead of fabricating a response.

Provider-specific command construction and parsing live under `adapters/`. Process lifecycle,
timeout, executable resolution, and OS errors live in `cli_process.py`.

## Usage contract

```json
{
  "input_tokens": 0,
  "cached_input_tokens": 0,
  "cache_creation_input_tokens": 0,
  "output_tokens": 0,
  "reasoning_output_tokens": 0,
  "total_tokens": null,
  "cost_usd": null,
  "completeness": "complete | partial | unavailable"
}
```

- `complete` means the adapter recognized the provider's documented terminal usage event for the
  fields that provider exposes. It does not mean every provider exposes the same accounting fields.
- `partial` means useful usage was found but the event schema or field coverage is not stable enough
  to call complete.
- `unavailable` means no recognized usage was exposed. Zero values must not be interpreted as zero
  cost.

Claude's final result usage and Codex's `turn.completed.usage` are normalized directly. Codex usage
is summed when a process emits multiple completed turns. Antigravity output mode is capability
detected from `agy --help`: older versions use text and report usage as unavailable; versions that
advertise stream JSON are parsed conservatively as partial until their schema is pinned by fixture.

## Failure and safety semantics

- The single-run command requires exactly one provider. The bounded loop runs multiple read-only
  roles serially but grants write access only to the Gemini developer. Cross-provider replay or
  parallel editing still requires isolated worktrees.
- `probe` calls only `--version` and does not send a model prompt.
- The harness never uses `shell=True` or injects credentials into argv.
- Claude and Codex receive the task through stdin, avoiding OS command-line disclosure and Windows
  argv length limits. Antigravity 1.0.13 still requires a positional print prompt; it is redacted
  from SQLite command metadata.
- A timeout or unavailable executable still produces an auditable local trace record.
- The harness does not mark Agent Loop gate state, approve a proposal, or call `submit_outcome`.
- MemTrace writeback is optional and draft-only; raw provider transcripts remain local.

## Durable continuation

Each loop run creates or joins a `conversation_id`. Every execution attempt is committed to SQLite
before another provider is invoked, then a versioned, bounded `ResumeEnvelope` checkpoint is written.
The envelope separates runtime, project-spec, Agent Loop and optional Improvement references.

Passing `--conversation-id` to a later run injects the latest envelope into Controller context. This
is cold-start continuation, not transfer of Claude thinking blocks, Codex reasoning traces or
provider-specific tool-call state. The current implementation re-enters at Controller; exact
logical-stage resume and semantic compaction remain planned.

## Role-profile loop

The packaged v0.4 policy is:

```text
controller          Codex gpt-5.6-luna        read-only
planner             Claude sonnet             read-only
planner-escalation  Claude opus               read-only
red-team            Codex gpt-5.6-sol         read-only
developer           Antigravity Gemini        workspace-write
```

The normal path is Luna control, Sonnet planning, Sol G1, Gemini development, Sol G2, and Luna
convergence. High risk, cross-module scope, architecture change, and open questions do not select
Opus by themselves. Opus is allowed only after a first G1 `reasoning_gap`, an explicit custom
policy/human decision, or a separate Improvement Loop replay; the built-in live loop implements
only the first case.

One evidence-backed repair is allowed before human escalation:

- non-`missing_input` G1 rejection: Sonnet revision, except `reasoning_gap` uses Opus;
- non-`missing_input` G2 rejection: Gemini revision;
- second non-PASS verdict: stop with local `needs_human`.

These are new stage invocations with the rejection artifact, not blind process retries. Provider
fallbacks are never implicit. The packaged Controller candidate pool is Luna, then Sonnet, then
Gemini 3.6; it is used only for classified quota, rate-limit or overloaded failures. The Role
permission, output schema and context policy remain those of Controller. Planner and Developer have
no packaged fallback yet. Opus escalation and Sol Red Team fail closed; an unavailable Sol run
cannot produce a gate PASS.

Quota buckets are explicit because multiple models can share one account limit. A failed bucket is
placed in an exponentially increasing cooldown (capped at one week) and is not repeatedly probed.
Attempts, fallback index, failure category and
usage completeness are persisted. Provider-specific reset timestamp parsing is not complete, so a
configured cooldown must not be described as the provider's known five-hour or weekly reset time.

The controller receives only a compact `LoopSnapshot`: task/risk identifiers, context references,
stage state, selected artifact fields/counts, and observed token total. It runs in a neutral Harness
directory with Codex minimal-context flags. Planner and developer roles receive targeted hydrated
evidence; gates receive normalized plan/development artifacts, not raw provider streams.

Every role profile also supplies a packaged JSON Schema to its CLI. Codex uses `--output-schema`,
Claude uses `--json-schema`, and Antigravity uses `--json-schema`. The Harness still parses and
validates the final object fail-closed because CLI versions and provider behavior can drift.

`LoopSummary.status` is a local Harness result. Neither `succeeded` nor `needs_human` means that a
MemTrace task row changed state. Optional writeback creates an inquiry draft containing stage/model/
usage/trace references and explicitly disclaims unsupported DB transitions.

Persistence is local-first. The Harness saves every stage attempt and checkpoint before the next
provider call, and saves the terminal summary before optional MemTrace writeback. A process or
writeback failure therefore leaves an auditable partial run instead of discarding already-spent
model usage.

## Token budget

A configured hard budget is evaluated after every terminal stage. Budget units prefer a provider's
`total_tokens`; otherwise they use `input_tokens + output_tokens`. Cached input and reasoning fields
remain separately observable and are not added again. If usage becomes unavailable while a hard
budget is active, the loop stops closed because it cannot prove that the budget is respected.

This is a stage-boundary circuit breaker, not a provider-side token limit: the current stage may
cross the threshold before its usage event is available. Improvement evaluation must include
controller, planner, gate, development, correction, and convergence cost.

## Version drift

CLI output is a versioned external contract. Raw events are therefore authoritative. When an
adapter parser changes, add fixtures for the observed CLI version and reparse historical traces;
do not rewrite historical provider output.
