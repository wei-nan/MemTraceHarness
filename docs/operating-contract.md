# Harness operating contract

This document is the repository-local operating contract for humans and agents. MemTrace stores
the decision history and project specification; this file states what the checked-out runtime must
do when MemTrace is unavailable or before targeted KB retrieval completes.

Status labels:

- **ENFORCED**: runtime validation and tests currently enforce the rule.
- **POLICY_ONLY**: accepted policy, but not all runtime paths enforce it yet.
- **PLANNED**: direction only; callers must not rely on it.

If this file, the current code, and a resolved MemTrace decision disagree, stop and record the
conflict. Do not silently select the most convenient source.

## Authority and memory routing

The Harness uses three authoritative memory planes:

| Plane | Question | Authoritative content |
| --- | --- | --- |
| Harness runtime | What happened? | local turns, CLI sessions, attempts, usage, traces, checkpoints |
| Project specification | What should be built and was it accepted? | requirements, decisions, plans, development and acceptance evidence |
| Agent Loop | How must work proceed? | task skeleton, stage/gate procedure, handoff and human checkpoints |

Improvement Loop is a proposal channel, not a fourth context bundle loaded on every run. It receives
stable cross-run aggregates and may create proposals; it cannot adopt its own proposal.

**ENFORCED**:

- raw provider output remains in the local trace directory;
- local SQLite stores run, execution, turn, provider-session and checkpoint references;
- MemTrace writeback is explicit and draft-only;
- missing usage remains `unavailable`, never zero.

**POLICY_ONLY**:

- project-spec and Agent Loop references should be written once at their authoritative location;
- stage checkpoints with durable value should be promoted to MemTrace as summaries/references;
- observations become candidate learning only after recurrence or evidence, and become adopted
  learning only after replay and/or human approval.

The Harness project specification is in `ws_ef463f70`. The shared Agent Loop is `ws_6aa957c3` and
the Improvement Loop is `ws_8c553f98`. Target projects must configure their own specification
workspace; `ws_spec_plan` is not a universal default for every project.

## Role contract

**ENFORCED** packaged primary roles:

```text
Controller          Codex gpt-5.6-luna        read-only
Planner             Claude sonnet             read-only
Planning escalation Claude opus               read-only
Red Team             Codex gpt-5.6-sol         read-only
Developer            Antigravity Gemini        workspace-write
```

Only the Developer profile may write to the workspace. A fallback inherits the Role permission,
structured-output schema, context policy and stage boundary; selecting another model does not select
that model's usual role.

Opus remains a G1 `reasoning_gap` escalation. Sol remains a fail-closed gate verifier. Neither has a
packaged fallback.

## Conversation and checkpoint contract

**ENFORCED**:

- a loop belongs to a Harness `conversation_id`;
- each execution attempt is appended to SQLite before another provider can be invoked;
- every attempt produces a versioned, bounded checkpoint and `ResumeEnvelope`;
- provider session IDs are recorded when the CLI exposes them;
- `--conversation-id` loads the latest envelope into a new run;
- original trace files are not deleted or replaced by compaction.

The envelope is split into runtime, project-spec, Agent Loop and optional Improvement contexts. It
contains bounded artifacts and references, not hidden provider reasoning.

**Current limitation**: continuing an existing conversation starts a new Harness run at Controller
entry with the latest envelope. It does not yet jump directly to an arbitrary saved Python call or
skip every previously completed logical stage.

**POLICY_ONLY**:

- provider-native resume is a same-provider optimization only;
- context soft/hard thresholds, turn count, artifact size, inactivity and stage boundaries select
  when semantic compaction is needed;
- accepted decisions cannot be rewritten by the compactor.

**PLANNED**: semantic compaction, periodic rebuild from raw artifacts and direct stage-machine resume.

## Explicit fallback contract

Fallback is selected and recorded by the Harness. Provider-native hidden fallback is not the
default.

**ENFORCED** Controller order:

```text
Codex gpt-5.6-luna
  -> Claude sonnet
  -> Antigravity gemini-3.6-flash-high
```

Only these failure categories may cross provider boundaries:

- `quota_exhausted`
- `rate_limit`
- `provider_overloaded`

`context_limit`, timeout, network, authentication, permission, configuration, schema, safety and
unknown failures do not trigger a cross-provider fallback. Error classification is conservative;
unknown text fails closed.

The availability ledger records quota bucket, provider/model, cooldown, consecutive failures and an
error signature. Capacity-failure cooldown grows exponentially up to one week, and a known shared
quota bucket is not probed repeatedly during cooldown. This means a
Luna quota failure may also make Sol unavailable when both use the same configured Codex account;
the Red Team gate then stops closed even if Sonnet successfully continued the Controller role.

**Current limitation**: the packaged cooldown is a conservative configurable interval. The schema
reserves `retry_after` and `reset_at`, but provider-specific five-hour/weekly reset timestamps are not
yet parsed into the availability ledger.

## Gate and adoption boundary

Harness results are draft evidence. They do not prove that MemTrace changed `gate_state`, `blocked`,
`gate-rejected`, `reject_count` or `completed` state. An unavailable Sol run cannot produce a gate
PASS. A second non-PASS gate result stops for human review.

Improvement automation may collect usage, build a retrospective bundle and create a proposal. It
must not approve or adopt the proposal, change Agent Loop policy, or lower the quality floor.

## Remaining delivery order

1. semantic compaction with source lineage and drift tests;
2. exact stage-machine resume from a checkpoint;
3. provider-specific reset/retry-after parsing;
4. backlog polling and paused-run wakeup (POLICY_ONLY via Telegram Gateway & UnattendedScanner);
5. scheduled/wave Improvement aggregation with deduplication;
6. isolated-worktree multi-writer execution.
