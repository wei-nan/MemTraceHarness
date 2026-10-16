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

The project has one primary agent for conversation, planning, task decisions and goal checks.
Its model is configured once under the historical `HARNESS_CHAT_*` settings (shown as 主代理 on
the status page). Development, review and other skills are execution capabilities it invokes.
Each task decision sees the current project conversation and execution results.

Multi-step plans run tools, development, installations and A2A requests in order and report
progress. Each action requiring approval asks separately; standing and plan-wide grants no
longer bypass confirmation. An asynchronous request waits for its actual result. General
automatic replanning of failed operational steps remains planned.

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
[`src/memtrace_harness/default-role-profiles.toml`](src/memtrace_harness/default-role-profiles.toml)
— this is only the default a project gets if it doesn't override anything:

| Profile | Provider/model (packaged default) | Permission | Purpose |
| --- | --- | --- | --- |
| `controller` | Codex `gpt-5.6-luna` | read-only | The only role: plans the goal in phases, picks the skills, checks the goal is met; quota fallback to Sonnet then Gemini |
| `plan` | Claude `sonnet` | read-only | Skill: the plan for one phase |
| `plan-escalation` | Claude `opus` | read-only | The plan skill's stronger-model mode (G1 reasoning-gap only) |
| `review` | Codex `gpt-5.6-sol` | read-only | Skill: independent G1/G2 review in a fresh context |
| `develop` | Antigravity `gemini-3.8-flash-high` | workspace-write | Skill: accepted-plan implementation and one bounded correction |

The Controller is the only role; `plan`, `review` and `develop` are skills it runs (`skills/<name>/SKILL.md`).
For the user's whole request it writes a goal plan (success criteria and phases), and a task is reported
done only when its final goal check answers every criterion — see "One Controller, stage skills" in
[`docs/architecture.md`](docs/architecture.md). The pre-skill profile names (`planner`, `red-team`,
`developer`, `planner-escalation`) still load from an older roles file.

Use `--profiles-file <path.toml>` to load another file, or `HARNESS_ROLE_PROFILES_FILE_<PROJECT>`
for a per-project override — every profile's provider/model is project-configurable, including
Controller and develop, not just the packaged defaults above. What validation actually enforces,
independent of vendor choice: only `develop` may hold `workspace-write` (every other profile stays
`read-only`); each profile's `context_policy` shape (Controller only a compact loop snapshot, `review`
only gate-scoped evidence, `develop` the accepted plan plus repo). Each skill is already a fresh,
independently-invoked CLI call that is handed artifacts, never another skill's conversation — that's what
makes a reviewer distinct from the work it's reviewing, not a forced vendor split. A task can name models
for its own skills with `model=`/`develop=`, `plan=` and `review=` prefixes on its goal.

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

## Workspaces, the Harness and its services

A **Harness** is the model-based work of one project: its roles and models, its Agent Loop, its
memory. It runs against exactly one **workspace** — a folder that may be empty and need not be a git
repository — and everything it needs is in that folder:

```text
my-project/
  .harness.env        its environment: MemTrace URL/token, its API token, CLI paths — mode 0600
  .harness/           its state: harness.sqlite3, raw provider traces, git worktrees, and the
                      preference files models.toml (chat/digest/recall models, memory, behaviour)
                      and roles.toml (the Agent Loop's role profiles)
  harness-scope.md    what the project is: MemTrace workspace id, off-limits, jobs, worker slots
```

A Harness has no chat transport and no web page of its own. Those are **services**, separate
processes with their own env files that only ever reach a Harness through its local HTTP API
(`HARNESS_API_PORT`, bearer token `HARNESS_API_TOKEN`):

| process | command | holds | talks to |
| --- | --- | --- | --- |
| Harness | `memtrace-harness -C <workspace> serve` | `.harness.env` | model CLIs, MemTrace |
| Telegram service (one bot, one Harness) | `python -m memtrace_services.telegram.main --env-file telegram.env` | bot token, chat allowlist | Telegram, its Harness's API |
| status page (any number of Harnesses) | `python -m memtrace_services.web.main --config web.toml` | each Harness's API token | the Harnesses' APIs |

See [`docs/services.md`](docs/services.md) for the design (the outbox, addresses, what a service may
and may not do) and the migration runbook. `memtrace-harness migrate` turns an old shared `.env` +
`projects.index.txt` setup into workspaces; `examples/` has a commented file for each of the three.

```powershell
# a new project, from an empty folder
memtrace-harness init-workspace ~/Workspace/my-project
memtrace-harness -C ~/Workspace/my-project doctor      # what would keep it from working
memtrace-harness -C ~/Workspace/my-project serve       # (or let launchd do it)
```

The `-C <folder>` option may be left out when you are inside the workspace. `run`, `loop` and `probe`
work with or without one.

## Configuration

Two files, one purpose each: `.harness.env` is the *environment* (`examples/harness.env`: secrets,
endpoints, CLI paths) and `.harness/models.toml` the *preferences* (`examples/models.toml`: which model
chats, digests and recalls, what falls back to what; the status page edits it and a change takes
effect at once). Command variables accept an executable name or explicit path, which is
useful when WindowsApps or PATH resolution prevents a CLI from starting.

```text
HARNESS_CLAUDE_COMMAND=claude
HARNESS_CODEX_COMMAND=codex
HARNESS_ANTIGRAVITY_COMMAND=agy
HARNESS_ANTIGRAVITY_OUTPUT_MODE=auto
HARNESS_CLI_TIMEOUT_SECONDS=900
```

`HARNESS_ANTIGRAVITY_OUTPUT_MODE=auto` inspects `agy --help` without a model call. The locally
verified Antigravity 1.1.10 advertises stream JSON and model selection. Its usage schema remains
version-sensitive and is normalized as partial until pinned by real fixtures.

Settings have no per-project suffixes: a workspace is one project, so `[chat] model` is that
project's chat model (the same setting is the environment variable `HARNESS_CHAT_MODEL`, which still wins if set). The models a child process runs under never inherit this file's secrets — a
provider CLI, a verify command or a job gets only `PATH`, `HOME`, the locale, proxy and CLI-login
variables (`HARNESS_PASSTHROUGH_ENV` adds names), and only a Codex call wired to the MemTrace MCP
server gets `MEMTRACE_API_TOKEN`.

## Remote operations (chat service + unattended scanner)

You clone `MemTraceHarness` once; every project you operate gets its own workspace and its own
`serve` process (they share the code and `.venv`, nothing else).

### 1. The project: `harness-scope.md`

Each workspace holds one `harness-scope.md`. It's plain markdown; the Harness reads a handful of
`key: value` lines out of it:

```markdown
# Harness scope — MemTrace

- workspace_id: ws_spec_plan_memtrace
- default_risk_level: medium
- off_limits: production database, deploy to prod

This project is the MemTrace core service. Backlog items live in the workspace above.
```

Required: `workspace_id` (the MemTrace workspace this project's specs/decisions live in). Optional:
`working_directory` (defaults to the folder the file is in), `name` (defaults to the folder name),
`default_risk_level` (`low`/`medium`/`high`, defaults to `medium`), `off_limits` (comma-separated
phrases the Harness refuses to act on even inside the working tree; `.harness.env` and `.harness` are
always added), `max_workers` (how many tasks of this project may run at once, default `3`; extra
tasks queue, and `1` keeps one-at-a-time editing in place), `worktree_setup_command` (shell command
run inside a task's fresh git worktree, with `HARNESS_REPO_ROOT` set to the main checkout, to make it
usable — e.g. link `node_modules` or copy an `.env`).

`init-workspace` writes this file for a new workspace. When the workspace is a git checkout it also
adds `.harness.env` and `.harness/` to `.git/info/exclude`, so secrets and state never show in
`git status` and are never committed, without touching the project's own `.gitignore`.

### 2. Run it

```powershell
# the Harness: its housekeeping plus the local API the services connect to
memtrace-harness -C <workspace> serve

# the bot in front of it (its own env file, outside the workspace)
python -m memtrace_services.telegram.main --env-file ~/.config/memtraceharness/<project>/telegram.env

# the status page over every Harness you list in web.toml
python -m memtrace_services.web.main --config ~/.config/memtraceharness/web.toml

# Backlog scan only, one pass (no API) — useful for a scheduled task
memtrace-harness -C <workspace> scan
```

`serve` runs until stopped (SIGTERM drains in-flight runs within `HARNESS_SHUTDOWN_GRACE_SECONDS`).
The Telegram service's `TELEGRAM_ALLOWED_CHAT_IDS` is a hard allowlist enforced before anything is
relayed — a chat that isn't listed gets silently dropped, token or no token. A Harness started
without `HARNESS_API_TOKEN` refuses to serve its API.

### What happens on each

- **Telegram service**: long-polls the bot; relays each allowed message (and the message it replies to)
  to its Harness, and each button tap; delivers what the Harness wants said — replies, approval
  requests, schedule results, notices — as Telegram messages and tells the Harness which message
  carries what, so a reply to an approval still resolves to that approval. If the Harness is down,
  nothing is lost: updates wait in Telegram, and the Harness's outbox waits for the service.
- **Harness**: a message goes to the chat model (or is a command: `/approve <id>`, `/reject <id>
  <reason>`, `/clarify <id> <answer>`, `/schedules`, `/status`, ...); approvals resume that
  conversation's Agent Loop automatically. Anything asking for something out of scope (GitHub PRs,
  email, an off-limits phrase from `harness-scope.md`) is rejected with an explanation.
- **`scan`** (also run every `--scan-interval-seconds` by `serve`): checks MemTrace for
  planned-but-not-implemented backlog items. If `HARNESS_UNATTENDED_WRITE_REQUIRES_APPROVAL=true` (the
  default), it asks for approval before doing any work; otherwise it runs the Agent Loop immediately.
  A project already mid-loop is skipped for that pass (workspace lock), so the same backlog won't be
  triggered twice while it's still running.
- Every chat message and every Agent Loop outcome is appended to the project's own running
  transcript (its "primary session"), so a later question like "what happened with that last task"
  is answerable from the same conversation. That transcript stays local, verbatim: nothing is
  written to MemTrace by day, and a transcript never goes into MemTrace at all — the night pass
  (below) writes organised notes, one per matter, as draft evidence, never as canonical knowledge.
  `git push` always requires a separate approval, even after a full Agent Loop pass.
- A scheduled run is logged as a `schedule_trigger` / `schedule_report` pair carrying the schedule's
  id, kept out of the conversation window and the nightly matter notes; chat and the Agent Loop
  instead get one line per schedule (latest result, firings in the last 24h). Replying to any
  message — including pushes from this bot's schedules or from external scripts using the same bot
  through the service's relay, which the Harness never otherwise sees — puts that message's text in
  front of the model.
- **Nightly memory digest** (`serve`, 02:00–02:59 in `HARNESS_SCHEDULE_TIMEZONE`, Asia/Taipei by default): each
  finished day of a project's transcript is replayed through one tool-less chat-model call into a
  digest (decisions, facts, open items carried forward until resolved, process lessons). Every item
  must cite turns from that day or it is discarded; the last few digests are injected into chat and
  Agent Loop context and mirrored to the project's memory workspace in MemTrace. Preferences
  found the same way may only cite the human's own turns, and the Harness adopts, replaces and
  retires them itself (at most 5 new per project-day; the rest wait on the status page). The
  operator corrects one by saying so in chat, or retires/restores it on the status page; the
  adopted rules reach the chat model numbered, newest 40 first. `memory-digest [--date YYYY-MM-DD] [--backfill] [--dry-run]` runs it
  by hand; the automatic pass only catches up the last 2 days.
- **Nightly matter notes** (same window, right after the digest): the whole day is split by matter
  *before* anything enters MemTrace — one memory node per matter (a rule, a bug, a decision, an open
  question), however the day's messages interleaved. The node is titled by the matter itself and
  holds the matter's current state, what is still open, and the decisions and facts with the dates
  and chat-turn numbers they came from (items without a real turn are dropped; the words themselves
  stay in the local log). A matter that already has a node is updated, not duplicated, so a
  strategy discussed over ten days is one note with a history. The daily digest links to the
  matters of its day. The Harness works off any backlog of unarchived days itself, oldest first, at most 7 days
  per project per night; `memory-topics [--date YYYY-MM-DD] [--days N] [--dry-run]` runs the same step by hand; contract: "Memory archive" in `docs/operating-contract.md`.

Full design rationale and the open decisions behind this feature are in
[`docs/remote-ops-plan.md`](docs/remote-ops-plan.md).

### Several repos, one project

A product often lives in more than one repo (a backend and an Android app) with one knowledge base. One
MemTrace workspace is one Harness, so those repos are one Harness too: its primary repo is the workspace
folder, the others are declared in `.harness/repos.toml` (`memtrace-harness repo add <name> <path> [--verify ...]
[--github owner/repo] [--setup ...] [--off-limits ...]`, see `examples/repos.toml`). There is one bot, one
conversation, one scanner and one nightly tidy for all of them.

A task can select one repo with `repo=<name>::` or several with `repo=<name1>,<name2>::` (the same prefix works
in a schedule's task text); no prefix means the primary repo. Each selected repo gets its own task worktree,
verify command, setup command and off-limits rules. Only those selected worktrees are writable; read-only roles
can inspect all configured repos regardless of provider. Multi-repo tasks require committed Git checkouts. The
Harness verifies and runs the final gate in every selected repo, preflights all changed branches, then merges
them separately after the goal check passes. If a later merge fails, earlier merges are reported and remaining
worktrees are kept for recovery. Scanner proposals from a repo's GitHub issues are worked in that repo, and a
resumed conversation returns to the same selected repo set. Concurrent tasks still use separate worktrees.

### Several projects

One workspace, one `serve`, one bot per project (a project being one knowledge base); the status page lists them all. A project runs up to
`max_workers` tasks at once (default 3), each in its own `git worktree` on branch
`harness/<conversation_id>` so Developers never edit the same files; further tasks wait in a queue
and start as slots free up. A schedule never overlaps itself: if its previous run is still running (or
queued) the next occurrence is skipped. When a task finishes, the converge-stage Controller sees what
it would bring back and may choose `merge`; the harness merges the branch into the checkout's base
branch only if that checkout is clean, and stops for a human on conflict. Projects that are not git
repositories or have no commit yet still run `max_workers` tasks side by side, in place; only their
development (the Developer stage) takes turns, via a per-project edit lock, so checks and queries
never wait behind a code change. `max_workers: 1` runs one task at a time.

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

See [`docs/execution-sources.md`](docs/execution-sources.md) for multiple CLI accounts, per-Harness source grants, shared capacity and account-aware fallback.

See [`docs/proactive-improvement.md`](docs/proactive-improvement.md) for opt-in observation of
other Harness conversations, task outcomes and schedules, and governed automatic platform fixes.

See [`docs/operating-contract.md`](docs/operating-contract.md) for behavioral status and
[`docs/architecture.md`](docs/architecture.md) for adapter and usage contracts.

## Verify without model calls

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python -m compileall -q src tests
.\.venv\Scripts\python -m unittest discover -s tests -p "test_*.py" -v
# or run with pytest:
.\.venv\Scripts\pytest -v
```

Tests inject deterministic subprocess results and mock external HTTP/API connections; they do not launch Claude, Codex, Antigravity, or Cursor, nor do they hit Telegram servers.

## TaiwanTrade proxy (read-only, plus confirm-first orders)

Agent CLIs run sandboxed with no network, so they cannot reach `127.0.0.1:8000`. Harness ships a
stdio MCP server (`python -m memtrace_harness.taiwantrade_mcp`) that the CLI spawns *outside* its
sandbox. It exposes only a GET allowlist (quotes, daily/intraday data, screens, positions,
balance, order *listing*); order placement/cancel/amend, auth/key management and watchlist writes
are not reachable. Opt in by writing the API key to a file (`chmod 600`) and setting:

```bash
export HARNESS_TAIWANTRADE_API_KEY_FILE=~/.config/memtrace-harness/taiwantrade.key
export HARNESS_TAIWANTRADE_API_URL=http://127.0.0.1:8000/api/v1   # optional, this is the default
```

Only the key *path* is passed to the CLI. All roles, Controller included, receive the server; the Controller prompt is amended to allow it.

### Orders: the model proposes, you confirm in the chat

Placing an order is off by default. List a project in `HARNESS_TAIWANTRADE_ORDER_PROJECTS`
(comma-separated, e.g. `TWTradingStrategy`) and only that project's **chat** gets one extra tool,
`create_order_intent` (limit orders; odd-lot quantity is in shares). Agent Loop roles never get it.

```text
model --create_order_intent--> TaiwanTrade /trade/order-intents   (nothing reaches the broker)
        one-time confirmation token -> harness SQLite, NOT to the model
Harness --> chat: "買進 2327 | 1 股 | 限價 628" [✅ 確認下單] [❌ 取消]
you tap confirm --> the Harness calls /trade/orders with the token --> result sent to you and logged
```

The confirmation is enforced in code, not by the prompt: the model has no tool that places, confirms,
cancels or amends an order; the token never enters the model's context; only a tap that an allowlisted chat made (relayed by its service)
button tap can send the order, at most once per intent (compare-and-set on the intent's status); an
unconfirmed proposal expires with TaiwanTrade's own TTL (5 min) and is reported as not placed. A
harness-side per-order value limit applies on top of TaiwanTrade's own risk limits
(`HARNESS_TAIWANTRADE_MAX_ORDER_VALUE`, default 100000 TWD). The API key needs TaiwanTrade's `trade`
scope. The model can also propose **cancelling** an order (`request_order_cancel`), confirmed the same
way (the Harness, not the model, sends the `DELETE`). Amending an order is not available.

- **Reorganising memory** (same night pass, by the Harness itself): old hourly `Draft: …` nodes are moved to the
  MemTrace trash once every day they span is archived into matters; once a week a model reviews the matters and may
  merge two that are one subject, retitle one, or mark a finished one resolved (the Harness validates every change,
  and the report carries a one-tap undo). You can also just say so in chat ("整理一下記憶", "重建記憶") or use
  `/reorganize` and `/rebuild`; a rebuild only starts after you tap a confirmation card. `memory-reorg --review
  [--dry-run] | --retire-drafts | --undo RUN` runs these by hand; `memory-topics --rebuild` derives all matters again from the local log (old notes are trashed only
  after the new set is written; an interrupted rebuild resumes). Only the Harness's own notes are touched, every
  removal is a soft delete.
