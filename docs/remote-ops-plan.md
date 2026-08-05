# Remote operations plan (Telegram channel + scheduled backlog loop)

Status: **IMPLEMENTED** — Telegram gateway, primary session hot-context logging & consolidation, out-of-band approval requests, and unattended backlog scanner are fully implemented.

## 1. Goal

Give the Harness two new entry points beyond the existing synchronous `run` / `loop` CLI commands:

1. an inbound **remote channel** (Telegram first) so a human can trigger/steer work without being at
   the keyboard;
2. an **unattended scheduler** that scans for backlog work every hour, drives it through the existing
   Bounded Agent Loop, and uses the same remote channel to ask for authorization or clarification when
   the loop cannot proceed on its own.

Boundary: local git (`add`/`commit`/`branch`) inside the `--working-directory` working tree is in
scope — the Developer role already has `workspace-write`, so committing its own work is a natural
extension, not a new permission class. **`git push` to a remote is a separate, gated action**: it must
go through the same Approval Request mechanism as any other human checkpoint (§6) before it executes —
never automatic, even after the Agent Loop passes both gates. GitHub-specific actions beyond `push`
(opening a PR, commenting on an issue/PR, using the GitHub API) remain out of scope for this proposal;
if wanted later, that is a separate, explicitly scoped addition — do not fold it into this one.

## 2. New component: Telegram gateway (long-running process)

Everything the Harness has today is a one-shot CLI invocation. This is the first requirement for a
**persistent background process** ("gateway"), which is new territory:

- Use long polling (`getUpdates`), not a webhook. A webhook requires a public HTTPS endpoint on the
  dev machine; long polling needs nothing inbound and keeps the whole surface area outbound-only.
- Config (new `.env` keys, same pattern as existing `HARNESS_*` vars):
  - `HARNESS_TELEGRAM_BOT_TOKEN` — required to enable the gateway at all; absent = gateway does not
    start, exactly like a missing provider CLI today.
  - `HARNESS_TELEGRAM_ALLOWED_CHAT_IDS` — comma-separated allowlist. **Hard requirement**: any chat ID
    not on this list is ignored (not even acknowledged) at the transport layer, before any model or
    project logic sees the message. A bot token alone must never be sufficient to command the
    Harness — anyone who finds the token but isn't allowlisted gets nothing.
- Gateway responsibilities: receive inbound messages, send outbound notifications/approval prompts,
  and nothing else. It must not itself hold write permission to any workspace — it only dispatches
  into the existing `run`/`loop` machinery, which keeps today's permission model (only the Developer
  role profile is `workspace-write`) intact.
- Crash/restart: gateway runs under a supervisor (Windows Task Scheduler "on failure restart" or
  equivalent) since there's no existing service-management story in this repo.

## 3. Capability A — inbound message handling

A chat message needs to resolve to a concrete `(workspace, working-directory)` pair before anything
can run, but Telegram messages don't carry those. Per user decision, this is defined **up front, per
project, as a markdown file** — not a config format the user has to learn — following the same
convention the repo already uses for `AGENTS.md`/`CLAUDE.md`/`GEMINI.md`. Working name:
`harness-scope.md`, one per project, checked into (or placed alongside) that project's own working
directory. It states, in plain language:

- which MemTrace workspace is authoritative for this project's specification/decisions;
- the working directory boundary (the gateway must never operate outside it);
- anything project-specific the chat/scan flow should know (e.g. which risk level to default to,
  any areas that are explicitly off-limits even inside the working tree).

The gateway still needs one small top-level index telling it *which* projects exist and where their
`harness-scope.md` lives — a short list of paths is enough; the actual scope content stays in each
project's own markdown file rather than duplicated into a central config. Example index shape (to be
finalized by whoever implements it):

```text
D:\Workspace\MemTraceHarness\harness-scope.md
D:\Workspace\MemTrace\harness-scope.md
```

Flow: inbound message → **chat model** (see §5) classifies intent (which project, what's being asked,
is this a new task vs. a reply to a pending approval) → reads that project's `harness-scope.md` →
dispatches to the existing `run` or `loop` entry point with the resolved project's flags. The chat
model does triage/routing only; it must never be the model that plans or writes code — that stays the
job of the existing role-profile pipeline.

A message that doesn't resolve to a project with a `harness-scope.md`, or that requests something
outside that project's stated boundary (e.g. "check my email", "open a PR" — see §1 for the current
git/GitHub boundary), must be rejected with a message explaining why, not silently reinterpreted.

### 3.1 Two kinds of "session" — do not conflate them

Chat and actual work are **deliberately two different session concepts**, run by two different
models:

- **Primary session** = one long-lived thread per project (see §3.2) that covers casual chat,
  substantive discussion/planning, and dev-result reporting. The chat model handles it. Unlike a
  stateless chatbot, this session is backed by durable "hot" context (§3.2) precisely so it does *not*
  reset every time you close Telegram or the underlying model has to be swapped.
- **Work session** = a Harness `conversation_id`, exactly the same object `loop` already creates and
  resumes today via `--conversation-id`. It's created the moment the primary session dispatches a task,
  and it's what actually carries plan/gate/development state through Controller → Planner → Red Team →
  Developer → convergence.

One primary session can create many work sessions over time (one per task you ask for), and a work
session can pause and later be resumed by a reply in the same primary session. A work session's
outcome (result, or a paused/needs-human state) is reported back into the primary session as a turn,
so "what did the dev work conclude" is answerable from the same thread you were discussing it in. But
primary and work sessions are still not the same lifetime or the same model: the chat model never
plans or writes code, and the work-session role profiles never see raw Telegram traffic — they only
see what the chat model extracted into a task envelope. This mirrors §5's model split and is worth
stating explicitly so an implementer doesn't accidentally merge them into one object.

**Confirmed**: one primary session per project, one-to-one with that project's `harness-scope.md`
(§3) — not one global session across every project, and not one per Telegram message thread. The
hot-context and consolidation design in §3.2 is scoped accordingly: every hot-context row and every
consolidation pass belongs to exactly one `(project, primary_session_id)` pair.

### 3.2 Primary session: hot context, model-swap continuity, consolidation to MemTrace

This is the part that needs to be more complete than "MemTrace has cold memory." The primary session
needs its own **local, durable, hot context** — separate from MemTrace and separate from the existing
per-Loop `ResumeEnvelope` — so that (a) a conversation survives the acting chat model being swapped
mid-discussion for quota reasons, and (b) only what's actually worth keeping gets folded back into
MemTrace, not every "ok thanks" message.

**1. Hot context log (new, in the existing SQLite ledger, next to the conversation/checkpoint tables)**

One append-only table, one row per turn:

```text
primary_session_id, project, turn_seq, created_at,
speaker (user | chat_model | work_session_report),
provider, model,
turn_type (chat | discussion | decision | dev_report),
content,
source_work_conversation_id (nullable — set when this turn reports a Loop outcome)
```

This is intentionally a different object from the Loop's `ResumeEnvelope`: the envelope is a bounded,
schema-validated checkpoint for the *stage machine* (Controller/Planner/Gate/Developer); the hot
context log is a plain running transcript for the *conversation*. A work session's terminal result
(succeeded / needs_human / failed) gets appended here as a `dev_report` turn when it happens, which is
also the mechanism by which "tell me the dev result" becomes answerable in plain chat rather than
requiring you to go query SQLite directly.

**2. Model-swap continuity, reusing the existing fallback contract**

The chat model gets its own ordered fallback list, same shape as Controller's fallback chain in
`default-role-profiles.toml` (§5's `HARNESS_CHAT_PROVIDER`/`HARNESS_CHAT_MODEL` becomes a primary +
ordered fallback list, not a single pair). On a classified `quota_exhausted` / `rate_limit` /
`provider_overloaded` failure — reusing the existing conservative classifier from
`docs/operating-contract.md` (`context_limit`, timeout, auth, permission, safety, and unknown failures
do **not** trigger a swap) — the gateway starts model B and rehydrates it with a bounded slice of the
hot context log: the most recent N turns verbatim, plus a short rolling summary of anything older than
that window. Model B can then say "picking up from where we left off" instead of the user having to
re-explain the discussion. This is the same philosophy as the existing Controller fallback +
`ResumeEnvelope` continuation, just applied one layer up, at the conversation level instead of the
stage-machine level.

**3. Consolidation back to MemTrace (cold), with a chat-vs-substance filter**

Trigger on whichever comes first:

- a configured time interval (e.g. every N hours), or
- the hot context log for a primary session crossing a size threshold (turn count or token count).

When triggered:

1. **Classify.** Run the unconsolidated turns through the chat model (no need for a heavier model —
   this is a labeling task) and tag each as `chat` (small talk, status checks, no lasting value) or
   `substantive` (a decision was made, a plan was agreed, a requirement got clarified, a dev result is
   worth remembering). Only `substantive` turns are candidates for MemTrace.
2. **Write as draft.** Substantive turns get written to MemTrace with provenance (which primary
   session, which turns, which model produced the summary) as **draft** content — this must follow the
   exact same non-authority boundary already stated in `docs/operating-contract.md`: MemTrace
   writeback is explicit and draft-only, and observations become adopted knowledge only after human
   approval, never automatically because a consolidation job ran. Chat-derived writeback does not get
   a looser bar than model-derived writeback already has.
3. **Mark consolidated, don't delete.** Once written, mark those hot-context rows as consolidated so
   they're excluded from future classify passes and from the "rehydrate model B" slice in step 2 above
   (no need to keep re-feeding old, already-summarized turns into every model swap). The raw rows stay
   in SQLite — this matches the existing "original trace files are not deleted or replaced by
   compaction" rule; only what's *actively re-fed* into a fresh model context shrinks over time, not
   the historical record itself.

Net effect: the primary session behaves like one continuous conversation from the user's side —
survives model swaps, remembers what was actually decided, and periodically hands the substantive
parts to MemTrace as durable, reviewable draft knowledge — without either flooding MemTrace with idle
chat or losing context every time quota forces a provider switch.

## 4. Capability B — hourly backlog scan

- A scheduled trigger (hourly) queries MemTrace per registered project for items that are
  "planned but not implemented" — i.e. an accepted plan/decision node without a corresponding
  development/acceptance evidence node. This query needs a concrete definition in MemTrace terms
  (which node types / edges count as "planned" vs "implemented"); that mapping needs to be pinned down
  with whoever owns the MemTrace schema before this is built, since `search_nodes`/`traverse` alone
  don't define "backlog" — that's a project-specific convention today.
- For each match, start (or resume) a `loop` run using the existing role-profile pipeline —
  no new model logic here, this reuses `default-role-profiles.toml` as-is.
- **Concurrency guard**: before starting a new loop for a project, check there isn't already an
  active `conversation_id` for that workspace. The scan must not double-trigger a project that's
  mid-loop; it should skip and try again next hour.
- When a loop stage stops for human review — the existing `needs_human` outcome, a second non-PASS
  gate verdict, a hard token-budget stop, or an ambiguous/ out-of-scope requirement the Planner
  flags — the scan driver sends a Telegram notification (see §6) instead of just leaving it in
  SQLite for someone to notice later. This is the main behavior change: today `needs_human` is a
  local, silent stop; going forward it must also produce an outbound notification when it originates
  from the unattended scanner.

## 5. Capability C — explicit chat-model vs. work-model separation

Two different cost/latency profiles, two different config surfaces:

| Use | Where configured | Model class | Notes |
| --- | --- | --- | --- |
| Telegram message triage/routing, formatting outbound notifications | new `HARNESS_CHAT_PROVIDER` / `HARNESS_CHAT_MODEL` (or a `[chat]` table in the project registry file) | small/fast/cheap (e.g. a flash/haiku-class model) | Never writes code, never plans; only classifies intent and drafts short messages |
| Actual planning/gating/implementation once a task is dispatched | existing `default-role-profiles.toml` (`controller`/`planner`/`red-team`/`developer`) | unchanged | The hourly scan and the Telegram channel both funnel into the same existing loop — they don't get their own model policy |

The hourly scan's *trigger* logic (querying MemTrace for backlog, checking for an in-flight
`conversation_id`) is deterministic bookkeeping and should not need a model call at all. Keep it that
way — don't spend chat-model or work-model tokens just to decide "is there backlog." Only the chat
model touches natural-language Telegram traffic, and only the existing loop profiles touch project
code.

## 6. Authorization when nobody is at the keyboard

This is the key open question and the answer should be one mechanism reused everywhere (Telegram
inbound task, hourly-scan escalation, and any future channel):

**New durable object: Approval Request**, persisted in SQLite next to the existing conversation/run
tables:

```text
id, conversation_id, workspace, working_directory, stage_ref,
reason (gate_reject_twice | reasoning_gap | budget_exhausted | out_of_scope | ambiguous_requirement | git_push),
proposed_action / summary, status (pending | approved | rejected | expired),
created_at, responded_at, responded_by_chat_id
```

Rules, matching the "stop closed" philosophy already stated in `docs/operating-contract.md`:

- Creating an Approval Request **pauses** that conversation's loop exactly where the existing
  checkpoint/`ResumeEnvelope` mechanism already pauses it today — no new pause semantics needed, just
  a notification hook on the existing `needs_human` path.
- The gateway sends the summary to every allowlisted chat ID with a short reference (e.g.
  `/approve appr_1a2b3c`, `/reject appr_1a2b3c <reason>`, `/clarify appr_1a2b3c <answer>`).
- **No timeout auto-approval, ever.** If nobody responds, the request simply stays `pending` and the
  loop stays paused — same as today when a human forgets to check SQLite. Silence must never be
  interpreted as consent, especially for the Developer (write-capable) role.
- Only an allowlisted chat ID may respond, and each Approval Request accepts exactly one terminal
  response (approve/reject) — no replay, no double-spend of a single approval across runs.
- An approval resumes the loop the same way `--conversation-id` continuation already works today,
  with the human's decision written into the envelope as context, not silently merged into the
  Planner's output.
- Recommend one more guard beyond "resume on approval": for any loop instance that the *scanner*
  started (not a human-initiated Telegram request), require approval before the **first** Developer
  (write) stage runs at all — not only on rejection/ambiguity. Nobody is watching an unattended run in
  real time, so the first filesystem-write action in a fully autonomous run is a reasonable place to
  keep a mandatory human checkpoint, at least until this pipeline has a track record. This can be a
  config flag (`unattended_write_requires_approval = true` default) so it can be relaxed later
  deliberately rather than by omission.
- **`git push` is always an Approval Request**, regardless of whether the run was human- or
  scanner-initiated, and regardless of gate outcome. Local commits happen automatically as part of the
  Developer stage's normal work; the push itself is the irreversible, shared-state action and gets its
  own `reason = git_push` request with the commit(s)/branch summarized so the human can review before
  it leaves the machine. This matches how a push would be confirmed in an interactive session — it's
  just routed to Telegram instead of a chat prompt when nobody's at the keyboard.

This directly answers the two things you asked to discuss:

- **How to handle authorization**: the Approval Request record + Telegram round-trip above, reusing
  the loop's existing pause/resume/checkpoint mechanism rather than inventing a new one.
- **Human not at the PC**: that's exactly what the Telegram channel is for — the loop already
  persists everything to SQLite before pausing (per the existing conversation/checkpoint contract), so
  "wait indefinitely for an out-of-band reply" is a small addition, not a redesign.

## 7. Other things worth deciding before implementation starts

- **Blast radius of the token**: a leaked `HARNESS_TELEGRAM_BOT_TOKEN` plus a leaked/guessed allowlist
  chat ID is remote code execution on the dev box (Developer role has `workspace-write`). Store the
  token like any other secret (`.env`, already gitignored) and treat the allowlist as a security
  boundary, not a UX nicety.
- **Idempotency / locking** for the hourly scan (mentioned in §4) — needs a concrete lock, e.g. a
  `scan_lock` row per workspace, since two overlapping scan ticks must not both decide to start a loop.
- **Per-project scope enforcement**: the project registry (§3) should be the *only* source of valid
  `working_directory` values the gateway will ever pass to `run`/`loop`. A chat message must never be
  allowed to supply an arbitrary path — that would let a compromised/allowlisted chat point the
  Developer role at a directory outside the intended project.
- **Rate limiting**: cap chat-model calls per chat ID per hour so a chatty allowlisted user (or a
  misbehaving script) can't burn budget on triage alone.
- **Observability**: log inbound/outbound Telegram traffic into the same trace store discipline the
  rest of the Harness uses (`data/traces/` + SQLite), so an approval decision is auditable the same
  way a model turn is.
- **Testing discipline**: match the existing pattern (`tests/` inject deterministic fixtures instead of
  hitting real providers) — the gateway's tests should inject fake Telegram API responses, not call the
  real Bot API.
