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

Cursor (`cursor-agent`, provider `cursor`, command override `HARNESS_CURSOR_COMMAND`) runs
`--print --output-format stream-json`; usage comes from the final `result` event and is `partial`
because the stream does not say whether `inputTokens` already contains `cacheReadTokens`. Probed
against 2026.10.14 (2026-10-14): a `read-only` profile maps to `--mode ask` (reads, refuses to write or
run commands; `--mode plan` is not used because it answers with a plan instead of doing the stage), and
`workspace-write` maps to `--force` (without it `-p` can edit files but every shell command is denied).
The CLI has no schema flag, so a profile's output schema is appended to the prompt. The tier is part of
the model name (`gpt-5.6-sol-high`), so `reasoning_effort` is recorded but not passed. Hard failures (bad
model, not logged in) print plain text on stderr with exit 1; model-side failures are a `result` event
with `is_error`. Read-only calls still get the git-status safety net. **Not wired for Cursor:** the
MemTrace MCP read tools (Cursor reads its own `~/.cursor/mcp.json`; the Harness does not inject or
check it), so a Cursor role has no MemTrace lookups unless the operator registers them there.

## Failure and safety semantics

- The single-run command requires exactly one provider. The bounded loop runs multiple read-only
  roles serially but grants write access only to the Gemini developer. Cross-provider replay or
  parallel editing inside one run still requires isolated worktrees; concurrent *tasks* of one project
  already run in separate per-task git worktrees (see the operating contract).
- `probe` calls only `--version` and does not send a model prompt.
- The harness never uses `shell=True` or injects credentials into argv.
- Cursor takes the prompt as its last argument (redacted from SQLite command metadata like Antigravity's).
- Claude and Codex receive the task through stdin, avoiding OS command-line disclosure and Windows
  argv length limits. Antigravity 1.0.13 still requires a positional print prompt; it is redacted
  from SQLite command metadata.
- A timeout or unavailable executable still produces an auditable local trace record.
- The harness does not mark Agent Loop gate state, approve a proposal, or call `submit_outcome`.
- MemTrace writeback is optional and draft-labelled; raw provider transcripts and conversation
  transcripts remain local — the night pass writes organised, one-matter-per-node notes. The
  one exception is the operator's standing preferences, which the Harness adopts and retires
  itself under the safeguards in `docs/operating-contract.md`.

## Durable continuation

Each loop run creates or joins a `conversation_id`. Every execution attempt is committed to SQLite
before another provider is invoked, then a versioned, bounded `ResumeEnvelope` checkpoint is written.
The envelope separates runtime, project-spec, Agent Loop and optional Improvement references.

Passing `--conversation-id` to a later run injects the latest envelope into Controller context. This
is cold-start continuation, not transfer of Claude thinking blocks, Codex reasoning traces or
provider-specific tool-call state.

Every resumed run starts again at Controller, which always re-routes. Settled work is then reused
rather than re-executed: the latest valid plan, a G1 or G2 that already PASSed, and a development
artifact that is not `INVALID` are rebuilt from the trace store (`_reconstruct_progress` in
`loop.py`) and skipped. A REJECT or NEEDS_HUMAN gate is not reused, so a resume re-runs it with any
new human clarification in context. Semantic compaction and provider-native thread continuation
remain planned.

## One Controller, stage skills

The Controller is the only role. Plan, review and develop are *skills* it runs: packaged
`skills/<name>/SKILL.md` files (`stage_skills.py`) whose front matter names the profile (model,
permission, fallback chain) and output schema, and whose sections hold the stable prompt policy. The
Harness runs each skill as a fresh CLI call and hands back its artifact; the Controller decides which
skill runs next, the Harness decides what is allowed.

```text
controller          Codex gpt-5.6-luna        read-only        (the only role)
plan                Claude sonnet             read-only        skill
plan-escalation     Claude opus               read-only        the plan skill's stronger-model mode
review              Codex gpt-5.6-sol         read-only        skill (G1 on a plan, G2 on a development)
develop             Antigravity Gemini        workspace-write  skill
```

A second kind, the *domain skill* (`kind: domain` in the front matter), is rule text only: a background duty reads
it when it builds its prompt (`stage_skills.domain_section`). It names no profile, schema or permission, so it
cannot hold write access or become a stage; it is not in the Controller's catalog or `PRECONDITIONS`. The first one
is `skills/digest-rules/` (the nightly digest's rules). The Harness still builds the data, validates every
citation and does every write; only the wording of the rules moved out of Python. Lookups a duty must always make
before asking the model are code, not skill text (`live_state.py`).

Profile ids are the skill names. The pre-skill names (`planner`, `planner-escalation`, `red-team`,
`developer`) still load from an older roles file, and a `[profiles.plan.escalation]` sub-table is read as
the `plan-escalation` profile.

**Goal plan.** At the start the Controller writes a goal plan for the user's *whole* request: the
restated goal, observable `success_criteria`, and ordered `phases`. Each phase names the skills it needs
(`["plan"]`, `["develop"]` or `["plan","develop"]`) and its own acceptance conditions; `review` is never
chosen, it follows every plan (G1) and every development (build/test then G2) automatically. The
Controller is given the user's own words (`user_request`), not only the chat model's one-line goal.

**Goal check.** After each phase the Controller looks at what was produced against the success criteria
(`progress`: next_phase, revise_phases, finish, ask_human) and, when the last phase is done, `converge`
reports a per-criterion `goal_check`. The Harness refuses to finish while the check is missing, skips a
criterion, or lists anything unmet: the task stops as `needs_human` and says it is only partly done. The
Controller can instead choose `revise_phases` to add more work (at most six phases in all). The final
message carries the criterion-by-criterion report. Stages of phase N>1 are recorded as `pN-…`; a resumed
run reuses a settled phase only while it and every phase before it keep the same title and skills.

## Role-profile loop

The packaged v0.4 policy is the table above. The default one-phase plan runs the original pipeline:
Luna control, Sonnet planning, Sol G1, Gemini development, Sol G2, and Luna
convergence. Each stage has a distinct job:

- **Controller (start)** routes the request: plan it, run it as an operational action, ask a human,
  stop, or report that no action is needed. It sees only the compact snapshot.
- **Plan** produces the plan: affected files, acceptance criteria, scope exclusions, open
  questions, and rollback. **G1** owns scope: it checks that the plan is concrete and bounded, and
  rejects it when it is not. It is a model verdict.
- **Develop** is the only skill with write access. It implements the accepted plan and runs
  tests. Its own "completed" status is not treated as final.
- **Review (G2)** owns conformance: it checks that the development matches what the accepted plan
  expected. If the project configures a `verify_command`, the Harness runs it first, and a failure
  becomes a synthetic REJECT that skips the model and drives the same revision path.
- **Converge (Controller)** runs after development and gates have passed, because a finished
  development is not the same as finished work. It decides `finish`, `merge` (integrate the
  task's isolated worktree back into the main line, reporting conflicts as `needs_human`),
  `revise_phases` (the goal is not reached yet) or `ask_human`, and reports the goal check.
- **Operate** handles requests that need no code change. Controller routes them to a direct action
  and the loop ends without planning or development.

Models remain in every gate: G1 and G2 judge scope and conformance, which a deterministic check
cannot fully cover, while `verify_command` covers what it can.

High risk, cross-module scope, architecture change, and open questions do not select
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
permission, output schema and context policy remain those of Controller. The packaged default
profiles give Planner and Developer no fallback chain. A workspace's own `roles.toml` may declare
one: Beri's Developer, for example, falls back to Antigravity Claude Sonnet and then Claude Sonnet,
both on quota, rate-limit or overload failures. Opus escalation and Sol Red Team fail closed; an unavailable Sol run
cannot produce a gate PASS.

A timed-out call of a read-only profile (the Controller, plan, review) also falls back to the next
candidate, whatever its `fallback_on` says: it cannot have changed anything, and a call silent for the
whole timeout says nothing about the request (**ENFORCED**, `tests/test_loop.py`). `develop` never does, since
a timed-out call may already have edited files. The packaged Controller timeout is 180 seconds (its calls
take seconds), so a hung call is given up early; `develop` keeps 3600 and the rest the global 900.

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

## Planned: safety envelope, one final gate, project-owned loops

Direction decided 2026-10-14; **PLANNED**, nothing here is built. The role-profile loop above (G1, G2, goal check)
is what runs today and stays so until each piece ships. The contract section is "Safety envelope and
project-owned loops" in `operating-contract.md`.

**Built 2026-10-15:** verification classified as passed / could_not_run / baseline_failing / regression (only a
regression sends work back), and `loop_mode: lean`, now the default for every project (develop + verification, no plan/G1/G2). The rest
below is still planned. Details: "ENFORCED (2026-10-15)" in `operating-contract.md`.

- **Split.** The Harness owns a safety envelope and dispatch; a project owns the shape of its loop, in its own
  repository, in the form its CLIs already load (`AGENTS.md`, `CLAUDE.md`, skills). The Harness sees diff, trace
  and declared permissions, not the stages in between.
- **One final gate.** The mid-loop gates go away as Harness mechanism. After the work, a single gate, run as
  code, checks (1) scope: the diff against the paths the task declared; (2) safety: no unconfirmed tool or loop
  definition, no forbidden command, no write outside the worktree; (3) sensitivity: no secret or private data in
  the diff or output, and redaction before anything reaches the trace store or MemTrace. A failure is
  `needs_human` with the finding. A model may add a finding but never clear one.
- **Tool confirmation.** A script, scheduled job, skill, MCP server, outbound call or the project's loop
  definition is a draft until a person confirms the diff plus its declared reads, writes and connections. Approval
  binds to a content hash and lapses on any change.
- **Where it lives.** The gate and its limits sit in the Harness, outside the worktree the project's agent can
  write, so a project's loop cannot lower them. A project with no loop of its own gets one develop run, its
  `verify_command`, then the gate.
- **Cost.** Stage-aware features (resume past settled stages, stage detail in approvals, status-page progress)
  need the Harness to know the stages; a project loop may emit checkpoints and `needs_human`, otherwise it is a
  black box.
- **Missing tools.** A project that cannot do a step reports `MISSING_TOOL: <name>`, asks the operator for a tap,
  then asks the Harness that has the machine over A2A. That Harness inventories read-only (PATH and fixed
  places; a credentials file is reported present or absent, never read). Absent: it gives the operator the
  install command and does not install anything that reaches a cluster, the system or credentials. Present: it
  proposes a named bounded job (a second tap, exact command and context shown) that the Harness runs itself, so
  no agent receives the binary's path or credentials. Destructive or exposing verbs are never made standing.
  Contract text: "A project that finds a tool missing asks the Harness that has the machine".
- **Today's code that maps onto it:** the git-status safety net on read-only calls, the per-job command-hash
  allowlist, capability install consent, per-task worktrees, `verify_command`. Missing: declared scope and its diff
  check, the secret scan, redaction before writes, the tool declaration and confirmation card.

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

## Harness and services

The Harness is the model-based work of one project, run against one workspace folder; the Telegram
bot and the status page are separate services that reach it only through its local HTTP API and its
outbox. See [`services.md`](services.md) for the design and the migration runbook, and "Harness and
services" in the operating contract for the enforced rules.
