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
- a governed run's record stays in the local trace store and is **not** also written to the project's
  MemTrace workspace as a "Harness loop draft" node (explicit operator decision, 2026-10-07: those drafts had
  become 95% of one specification workspace); what a run teaches reaches the knowledge base through the
  Controller's own `kb_updates`. The `--writeback` CLI flag still writes one on request;
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
proposals, which the Harness still cannot approve for itself. **PLANNED, not implemented:** a
"key-point" layer above the digests. Conversation archives, nightly digests and loop drafts are still
written draft-labelled; what the Controller writes is not (next paragraph).

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
edits its checkout in place. A project that cannot use worktrees still runs `max_workers` tasks side by
side (checking, querying, running scripts), but only one at a time may *develop*: the Developer stage
takes a per-project edit lock (`edit_locks`) and waits for it, a lock whose owner lost its slot is taken
over, and operational runs never take it. With `max_workers: 1` there is nothing to share, so no lock.
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

**Diagnosing and re-running without the Agent Loop (operator decision, 2026-10-08).** The chat model's sandbox
has no network, so a connection or DNS failure seen there says nothing about an outside service (a Telegram answer
once blamed "TWSE unreachable from here" when the project's own script was merely timing out on a slow legacy
endpoint). Diagnosis and re-running a job are the harness's own job; only changing code goes through the Agent Loop.
`ops_mcp.py` is an MCP server (spawned outside the CLI sandbox, like the TaiwanTrade proxy) given to the chat reply
for a project whose scope file declares something: `probe_hosts` (hostnames `http_probe` may GET; redirects are
reported, never followed), `log_files` (what `read_log` may tail) and `job: name = command` lines. **Enforced:**
those allowlists; `run_job` never runs anything — it records a `job_requests` row, the gateway shows a Telegram
confirm button, and the command (re-read from the scope file at tap time) runs in a daemon thread only after the
tap, once per request, with a 15-minute limit; its result is sent and written to the chat log. Unanswered
requests expire after 10 minutes. **Prompt policy, not enforced:** that the model probes before answering and only
starts a development task when the cause is in the project's code. Wired for Codex and Claude chat models only
(Antigravity chat gets the notice but no tools). **Unverified against live models:** that a chat model calls these
tools sensibly. A project that declares nothing is told to say it cannot verify from the chat sandbox.

**Orders proposed by the chat model (explicit operator decision, 2026-10-05).** For a project
listed in `HARNESS_TAIWANTRADE_ORDER_PROJECTS` the chat model may *propose* a limit order
(`create_order_intent`); a human must confirm it. ENFORCED in code: the tool only creates a
TaiwanTrade order intent, which never reaches the broker; the one-time confirmation token goes to the
harness database and never to the model; the model has no tool to place, confirm, cancel or amend;
only a button tap from an allowlisted Telegram chat sends the order (`POST /trade/orders`), at most
once per intent; an unconfirmed proposal expires and is reported as not placed; the result of every
confirmed, cancelled, failed or expired order is sent to the human and written to the chat log;
Agent Loop roles never get the tool; a per-order value limit (default 100000 TWD) applies on top of
TaiwanTrade's own risk limits. A cancellation is proposed the same way (`request_order_cancel`) and sent
only on the human's tap (the gateway makes the `DELETE`). **Not implemented:** amending orders, market orders.

**Replies to a paused task (2026-10-05).** A schedule run that stops `needs_human` is only a
notification: no approval is opened for it, so a later human reply cannot resume the monitoring run with
a different goal (an operational run is told to use `needs_human` only when it could not do the work).
A swipe-reply to a paused task's question goes to the chat model, which resumes the task only by emitting
`HARNESS_APPROVAL_ANSWER::`; a question, a remark, a failed model call or any doubt leaves the task paused
(the buttons, `/approve` and `/clarify` always work). The marker is honored only for the approval that
message replied to, and only while it is still pending.

**Decision card (2026-10-07).** A stage that stops for a human states its own question as a
`decision_card` (`decision_card.py`): the blocker in a sentence, 2–3 concrete options each with its
action and tradeoff, a recommended option with a reason, and what happens if nobody answers. Every
role's output schema carries it (required, nullable), and each stage prompt appends the shared
`CARD_INSTRUCTION`. When a loop stops `needs_human` for an info-needed reason, the card of the *last
executed stage* is validated (`card_from_artifact`: it must be well-formed, and the stage's own
status/verdict/action must itself hand over to a human) and stored on the approval request; the
Telegram message then shows the card instead of the raw artifact dump, with one button per option
(`pick:<id>:<n>`) that resumes the task exactly as `/clarify` with that option's text would. A missing
or malformed card falls back to the previous unstructured message, so a model that fails to write one
never blocks a stop. Cards are never used for `model_output_invalid`, `config_change_required`,
`budget_exhausted` or approve/reject-style requests, and schedule-run notifications do not carry one yet.
**Not implemented:** a single editor (Controller) that merges or suppresses cards across stages;
recording which option was chosen versus recommended; citing past decisions/preferences in `basis`.
**Unverified against live models:** that Codex/Claude/Antigravity accept the nullable-object schema and
actually write useful cards — covered by unit tests with injected output only.

**Decision records and precedent (2026-10-07).** Each time the operator resolves a request the harness
put to them, one `decision_records` row is written (`decision_records.py`): what was asked, what they
chose, and whether that followed the recommendation (picked the recommended option, picked another,
answered in free text, approved/rejected a cardless proposal). Technical stops (`model_output_invalid`,
`config_change_required`, `budget_exhausted`) are not recorded. A button tap also becomes a `decision`
turn from the operator in the conversation log, so the nightly digest sees it and can derive a preference
from it through its existing cite-the-turn rule. The Controller — and only the Controller — is given an
`operator_precedent` context item: up to 8 recent decisions (disagreements with a recommendation listed
first) plus the operator's adopted preferences, framed as evidence, never an instruction; Planner, Red
Team and Developer never receive it, which keeps the earlier rule that preferences describe how Harness
talks to the human, not what the working roles build. A card's `basis` may cite only the `D<n>`/`P<n>`
ids that item actually contained; others are dropped. **Not implemented:** retrieving precedent by
decision kind, asking the operator why after a button tap that went against a recommendation (a reason is
only captured from free-text answers), and promoting repeated disagreements to a preference outside the
nightly digest.

**Trigger review (2026-10-07).** What an unattended trigger produces is read by the Controller before it
reaches the operator (`trigger_review.py`). It is one tool-less call through the project's Controller role
profile (its provider/model, then its own fallbacks, in the Controller's neutral workspace), and it can
only *reduce* what the operator is shown, on grounds the harness checks; a missing, failed or
inadmissible verdict always degrades to the previous behaviour.

- *Schedule results* (`TelegramGateway._schedule_delivery`): `push`, `digest_only` or `pause_and_notify`.
  Holding a result back must cite which of the five earlier reports it repeats (and, for a `needs_human`
  result, that report must have been pushed); `failed`/`budget_exhausted` runs are never reviewed or held
  back; twelve held-back results in a row force a push; pausing needs at least three earlier reports and is
  never applied to an alert. A held-back result is still written to the chat log, tagged `【未推送：…】`, so
  the nightly digest sees it. A pause keeps the schedule (indefinitely) and tells the operator with
  "繼續排程"/"取消排程" buttons; tapping one is recorded as a decision (resume = disagreement).
- *Scanner candidates* (`UnattendedScanner._review_candidates`): per candidate `propose`, `defer` or
  `drop`. The first proposed candidate goes to the operator with the Controller's reason appended; a
  candidate can be deferred at most three times; a `drop` must cite `D<n>` operator decisions that were in
  the precedent it was shown, is told to the operator, and keeps the item out of proposals for seven days.

The review is only wired in by the serve command (`gateway`); without it, behaviour is unchanged.
**Not implemented:** reviewing *before* a run to skip it (only the result is reviewed), rewriting the pushed
text (it is pushed or held verbatim), and a review through the full Controller adapter — it uses the plain
chat-command path like the digest and recall, so its raw output is not kept in the trace store and it has no
quota-bucket/fallback bookkeeping. **Unverified against live models:** that the prompts produce admissible
verdicts and that the thresholds (3 reports to pause, 12 silent results, 3 deferrals, 7 days) suit real
schedules; covered by unit tests with injected output only.

**Controller maintains the knowledge base; the operator accepts completions (explicit operator decision,
2026-10-07).** The converge-stage Controller may propose `kb_updates` (`completion_claims.py`); no role calls
a MemTrace write tool itself. The harness validates and applies them: a `claim_done` for a node id taken
from the run's context, and `note` nodes with links, written *without* the draft/human-gate labels (tags
`harness`, `controller`) — whether a workspace accepts the write is that workspace's own setting, and a
refusal is reported, not worked around. A claim never edits the original node's body (task nodes carry
machine-read JSON): it creates a separate completion-claim node linked `extends` to the original and moves
the original from `status:open` to `status:claimed-done`, rewriting tags only when the node's current tags
could be read. Every governed run that reaches converge with `finish`/`merge` files a `completion_claims`
row and is reported with 驗收通過/還沒完成 buttons; nothing is accepted by silence and a claim has no
deadline. Accepting sets the node to `status:done` and `resolved`; sending it back reopens it, and the
rejection is kept as a `task_claim` decision which the Controller's precedent lists with a warning ("宣告
完成，被你退回") so it can calibrate what it declares finished. Operational and schedule runs never reach
converge and carry no claim. **Not implemented:** promoting repeated rejections into a standing lesson,
usage statistics for which precedents the Controller relied on (card `basis` is recorded only on the card),
and Controller-maintained knowledge for tasks that have no node. **Unverified against live models:** that
a Controller fills `kb_updates` sensibly, and that MemTrace accepts the `tags`/`resolution_status` update
the harness sends (the read side — `get_node` returning `tags` — was confirmed on a live node).

**Knowledge-base maps and tidying (explicit operator decision, 2026-10-07).** Each workspace a project
uses (its specification workspace and its cold-memory workspace) gets a *map* the Controller writes and
keeps current as one pinned node (`知識庫地圖：…`, tags `harness`, `controller`, `charter`), and is tidied by
the Controller (`kb_gardening.py`). A pass runs per workspace at most weekly in the nightly window, or on
`/garden`; a failed pass is not retried for six hours. The model is shown a harness-computed survey and a
compact node listing (identical titles collapsed into one `G<n>` line) and answers with the map text and
ops: `dedupe`, `delete`, `retag`, `set_resolution`, `supersede`, `link`, `retitle`, `pin`. **ENFORCED:** the
harness applies the ops, never the model; an op may only name nodes the model was shown; at most 100 nodes
removed and 40 other ops per pass; pinned nodes, nodes a person confirmed, nodes ever explicitly asked for,
nodes tagged `daily-digest`/`primary-session`/`task`, and the map itself are never removed or rewritten.
Wrong nodes may be deleted (operator decision): a delete is MemTrace's soft delete (30-day trash), the
node's content is also kept in the local trace store, and the operator is told after every pass that
removed anything, with a button that restores the whole pass (from the trash, else recreated from the local
copy). The map's text is kept locally and given as a context item to the Controller and the working
roles (it is navigation, not operator preference). Only the workspaces of the harness's own projects are
touched. **Not implemented:** gardening driven by retrieval statistics (`ask_count`/`traversal_count` are
shown to the model but not acted on by the harness), undoing non-delete ops (retag, retitle, resolution,
links, pin), and moving content between workspaces. **Unverified against live models:** that the Controller
model produces a useful map and sound ops from this prompt; nothing has been run against a live workspace.

**Research conclusions promoted into the specification workspace (explicit operator decision, 2026-10-07).**
Chats and operational runs never reach a stage where the Controller could record what they found, so
results lived only in the cold-memory workspace's nightly digests (drafts). `/promote [project]` runs
`kb_promotion.py`: the Controller reads the digests and the specification workspace's real nodes and proposes
*conclusion notes* (what was tried, the result with its numbers, how far to trust it) and a *directions
overview* (one row per strategy or approach with its status — 採用/研究中/淘汰/擱置/未定 — and the node holding its
detail). **ENFORCED:** every note and every overview row needs verbatim quotes, and the harness checks each
quote occurs in the digest it names (ignoring whitespace and markdown) — an item with none that checks out is
dropped with its claim; every number of two digits or more in a note or row must occur in the digests it cites
(so a recomputed or misremembered figure cannot become knowledge); the provenance section of each note and of
the overview is written by the harness from the verified quotes; links go only to existing specification
nodes; at most 8 notes and 15 directions per pass. Notes are written without draft labels (tags `harness`,
`controller`, `conclusion`), keyed by title so a rerun updates them instead of duplicating; the overview is
pinned, protected from tidying and given to the Controller and the working roles as context. The model may
also name existing specification nodes it thinks are out of date; these are only reported to the operator,
never changed. A pass reports what it wrote with a button that soft-deletes everything it created. The pass also runs by itself: in the
nightly window, after the digest pass has finished, per project at most weekly and only when a digest changed
since the last successful pass (no model call otherwise); every real pass leaves a record, so a failure or a
skip is not retried for six hours, and the operator is told only if something was written, flagged or
dropped (`/promote` always runs and always reports). Only the specification workspace is written; the
cold-memory workspace is read. **Not implemented:** running it on
reverting an edit to a note a pass wrote earlier, and checking that a note's
*claims* follow from its quotes beyond the numbers. **Verified on live data (dry run, nothing written):** on
TWTradingStrategy the Controller model produced 7 notes and 6 directions with every quote verified and all 62
numbers present in the cited digests.

**Review of the harness's own work (explicit operator decision, 2026-10-08).** `/review [project]`, a request
in chat (the chat model emits `HARNESS_REVIEW_START::`), or — once a first review exists — the nightly window
once a week (when the period holds at least 5 runs) reviews how the harness's work went (`work_review.py`), per
workspace, over the period since the last successful review (the first covers all history; a failed attempt
leaves its period for the next one). **ENFORCED:** the *report* is computed by the harness from its own records
with no model involved (outcomes, stage states, model calls and quota exhaustion, fallbacks, gate verdicts,
repeated questions with the date of the last one, operator decisions and claims, held-back schedule results,
the latest stopped or failed cases); the Controller's *interpretation* is checked against it — every finding,
proposal and lesson needs verbatim quotes that occur in the report, and every number of two digits or more in
it must occur in the report. The prompt carries a short hand-written list of facts about the harness the
report cannot show (`HARNESS_FACTS`: what `quota_cooldown` means, that held-back schedule results are
deliberate, that decision records began on 2026-10-07 …); **it must be updated whenever the behaviour it
describes changes.** Proposed changes to how the harness works reach the operator as decision cards
(reason `review_proposal`); their answer is kept as precedent, resumes no task, and the harness changes nothing
by itself — a proposal that should be done is a separate, explicit request. Lessons about how work goes are
written to the project's memory workspace without draft labels (tags `harness`, `controller`, `work-lesson`),
keyed by title and with their quotes, undoable with one button. Each workspace also gets one pinned
*record* node (`工作復盤紀錄：<project>`, tag `work-review-index`, protected from tidying) in the project's memory
workspace, rendered by the harness from the reviews that passed the checks: the latest findings, the lessons
kept so far (up to 8, newer replacing older of the same title) and a line per earlier review, linked
`related_to` to the lesson nodes; each review updates it in place. The record, and the research-directions
overview, reach the next conversations: they are given to the chat model (as "overviews the Controller
keeps", with an instruction to check that a problem still exists before telling the human it does) and to
the Controller and the working roles as context items, cut at 4500 characters each in a prompt. They
describe the past and say so; the next review replaces the record. **Not implemented:** judging whether an
individual held-back schedule result was right, comparing more than one previous period, and acting on a
chosen proposal. **Verified on live data (dry run, nothing written):** on TWTradingStrategy the Controller
produced findings that correctly separated recent from old problems and flagged repeated TaiwanTrade 401
errors as the current blocker.

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

**MemTrace lookups for Codex roles (2026-10-07).** Every role may query MemTrace read-only
(`search_nodes`, `get_node`, `list_nodes`, `traverse`). Claude picks the tools up from the user's own CLI
configuration; Codex cannot: the harness runs it under its own `CODEX_HOME` (`scripts/codex-will`, whose
config lists no MCP server), and the Controller and Red Team calls add `--ignore-user-config`, which would
drop one anyway. So `CodexCliAdapter` passes the server as `--config mcp_servers.memtrace.*` overrides
(URL from `MEMTRACE_MCP_URL`, lookups only via `enabled_tools`, the token by `bearer_token_env_var` so it is
never on the command line). Before this, 227 recorded Controller runs on Codex contained no tool event of any
kind and Codex Red Team runs never touched MemTrace, while the Controller's prompt told it to look things up.
Verified live: a Codex Controller call with these flags completed a `search_nodes` call. **Not verified:**
whether Antigravity, the Controller's other fallback, has MemTrace tools at all.

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
