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
- MemTrace writeback is explicit; conversation archives and nightly digests are written as
  draft-labelled notes (tags `harness`, `draft`);
- operator preferences are adopted and retired by the Harness itself, with the safeguards below;
- missing usage remains `unavailable`, never zero.

**Autonomous operator preferences (explicit operator decision, 2026-10-02).** The Harness derives
the operator's standing preferences from the operator's own conversations (nightly digest) and
adopts, replaces and retires them without waiting for approval; the operator corrects one in chat
(`HARNESS_PREFERENCE_CORRECT::`) or on the status page, which is an after-the-fact view and
override, not a gate. ENFORCED in code: every change cites the human's own turns and is checked
against the log; at most 5 rules are adopted per project-day (the rest wait for the operator);
only the newest 40 are put in front of the chat model; nothing is deleted, so a retired rule stays
on record and can be restored. This does not extend to Agent Loop gates or Improvement Loop
proposals, which the Harness still cannot approve for itself. **PLANNED, not implemented:** the same
autonomy for durable knowledge nodes (a "key-point" layer above the digests); until it ships,
knowledge the Harness writes to MemTrace stays draft-labelled.

**Topic recall (explicit operator decision, 2026-10-03).** Behind chat, a slower background
model (`HARNESS_RECALL_*`, default: the digest chain) judges each human message's topic
(continue an active topic / new topic / small talk), explores the cold-memory and spec
workspaces for earlier discussion, and writes a short-lived *topic brief* to local SQLite
(`topic_briefs`); the chat model reads the briefs on its next turn, marked 整理中 while a run is
pending, and is told to use one only if the current message is about that topic. ENFORCED in
code: every related node a brief cites must be a search hit the harness supplied or be confirmed
to exist in MemTrace (anything else is dropped, and no push is sent for dropped nodes); briefs
expire after `HARNESS_RECALL_TTL_HOURS` (default 72, sliding — a topic that comes up again is
extended); at most one recall run per project at a time; a topic with no history is stored as
fresh and never pushed. Briefs are local drafts and never written to MemTrace by this path.
The model may push one Telegram message when it found a connection the human likely no longer
has in mind. **PLANNED, not implemented:** a reverse index so the nightly digest can mark which
older items a day's topics continued.

**Concurrent tasks per project (explicit operator decision, 2026-10-05).** A project runs up to
`max_workers` governed tasks at once (`harness-scope.md`, default 3; 1 restores one-at-a-time).
ENFORCED in code: slots are claimed atomically in SQLite (`workspace_locks`, one row per running
task), so the limit holds across threads; a task that finds every slot busy waits in a persistent
FIFO queue (`task_queue`) and is started when a slot frees, including after a gateway restart; a
schedule whose previous run is still running *or still queued* is skipped for that cycle rather than
queued behind itself (other schedules are unaffected); an approved conversation waits for a slot
the same way. Each task in a git project runs in its own `git worktree` on branch
`harness/<conversation_id>` (`worktrees.py`), so concurrent Developers never edit the same files;
a project that is not a git repository, has no commit yet to branch from, or has `max_workers: 1`
edits its checkout in place and is limited to one task at a time (they would share one folder).
Worktrees branch from the checkout's HEAD, so uncommitted or git-ignored files are not in them —
`worktree_setup_command` (run in the fresh worktree with `HARNESS_REPO_ROOT` set) is the hook for
that. The converge-stage Controller is told what a finished task would bring back (commits, files,
whether it merges cleanly, whether the base moved) and may choose `merge`: the harness then merges
the branch into the checkout's base branch itself, but only when that checkout is clean and still on
that branch, and aborts on conflict (leaving the work on its branch and stopping for a human). Work
that is not merged, or whose run stopped for a human, keeps its worktree. One editing provider per
run is unchanged: this isolates *tasks* from each other, it is not provider fan-out inside a run.
**Not implemented:** automatically resolving merge conflicts, pruning worktrees of abandoned or
failed-with-changes runs, and the unattended scanner still wants the whole project idle.

**POLICY_ONLY**:

- project-spec and Agent Loop references should be written once at their authoritative location;
- stage checkpoints with durable value should be promoted to MemTrace as summaries/references;
- observations become candidate learning only after recurrence or evidence, and become adopted
  learning only after replay and/or human approval.

The Harness project specification is in `ws_ef463f70`. The shared Agent Loop is `ws_6aa957c3` and
the Improvement Loop is `ws_8c553f98`. Target projects must configure their own specification
workspace; `ws_spec_plan` is not a universal default for every project.

## Role contract

**ENFORCED** packaged primary roles (packaged defaults — see below for what's actually validated):

```text
Controller          Codex gpt-5.6-luna        read-only
Planner             Claude sonnet             read-only
Planning escalation Claude opus               read-only
Red Team             Codex gpt-5.6-sol         read-only
Developer            Antigravity Gemini        workspace-write
```

A role's identity is its job in the pipeline, not a vendor pin. Every role's provider/model —
Controller, Planner, Planning escalation, Red Team, and Developer alike — is project-configurable
via `HARNESS_ROLE_PROFILES_FILE_<PROJECT>`; the table above is only the packaged default a project
gets if it doesn't override anything. What **is** hard-validated regardless of vendor choice:

- only the `developer` profile may hold `workspace-write` — every other role stays `read-only`;
- each role's `context_policy` shape (Controller only ever gets a compact loop snapshot, Red Team
  only gate-scoped evidence, Developer the accepted plan plus repo, etc.) — this is an information-
  flow boundary, independent of which model is behind the role;
- Red Team and Planning escalation both fail closed with no packaged fallback chain, so a gate
  verdict or an escalated plan never silently degrades to a different/weaker model mid-stage.

A fallback inherits the Role permission, structured-output schema, context policy and stage
boundary; selecting another model does not select that model's usual role. The "independent
reviewer" and "consistent behavior" properties people actually rely on come from every role stage
already being a fresh, separately-invoked CLI call — never a continued conversation — and from the
permission/context boundaries above, not from forcing any role onto a specific vendor.

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
- `cli_unavailable` — the provider CLI could not be launched at all (executable missing, wrapper
  script pointing at a stale path, `cannot execute`). The request was never evaluated, so the next
  candidate is safe. This category ignores a profile's `fallback_on` list, sets no cooldown (the
  primary is retried on the next stage so a fixed install recovers immediately), and always sends
  the operator a Telegram alert via `AgentLoopRunner.alert_callback` — even when no fallback
  exists — naming the failed and (if any) substitute provider/model.

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
6. isolated-worktree multi-provider fan-out inside one run (per-task worktrees already isolate
   concurrent tasks; see "Concurrent tasks per project").
