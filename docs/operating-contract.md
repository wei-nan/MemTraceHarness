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

## One primary agent and single-use confirmation (operator decision, 2026-10-15)

This section supersedes the older split between a chat model and a snapshot-only Controller,
and every older standing-permission or plan-wide-approval rule below.

**ENFORCED** (`primary_agent.py`, service/CLI wiring, `tests/test_primary_agent.py`):

- One primary agent owns dialogue, planning, progress decisions and goal checks. All use the
  historical `HARNESS_CHAT_PROVIDER`, `HARNESS_CHAT_MODEL` and `HARNESS_CHAT_FALLBACKS` settings.
  The status page shows one primary-agent model editor. A legacy controller-model edit targets
  those same settings. Existing controller entries in roles files remain readable for low-level
  replay, but service-owned runs and the loop CLI derive their effective primary profile from
  the shared settings. An already-started task keeps the chain captured at its start.
- The persisted profile id `controller` and stage ids remain compatible with checkpoints. They
  identify structured decision turns of the primary agent, not another decision maker. Before
  every such call the runtime rehydrates the current project conversation alongside the loop
  snapshot. Explicit decisions and progress are recorded in that conversation. The continuation
  is local durable context, not a shared provider-native session or private reasoning transfer.
- Only develop holds workspace-write. Review receives its own gate evidence and never receives
  the primary agent's conversation. Each skill remains a fresh CLI invocation.
- A mixed step plan can invoke registered tools, declared jobs, develop, request_install and
  propose_scope_change. Development waits for its durable task result; installs and scope changes
  wait for their own confirmation/result; A2A questions and issue reports wait for the actual
  remote result. Enqueuing a request is never success for that step. Job creation and execution
  are separate steps in a plan. The next step runs only after the previous result is checked.
- A development interrupted without a durable result is held for a human, never blindly replayed.
  A stopped development can await its ordinary task clarification and consume the resumed result.
  Cancelling a plan stops subsequent development calls and rejects its pending clarification;
  an already-cancelled plan cannot be revived by a queued resume. In-flight A2A requests are
  not remotely revoked by cancelling the local plan. Ordinary tool/operation plan failures
  retain the existing retry/skip/cancel behavior.
- Read-only exploration needs no tap. Every action that requires confirmation uses a fresh
  request and tap. Neither an old allowed.json entry nor a persisted approve_all field grants
  permission. Legacy permanent-approval and plan-wide-approval callbacks are refused at runtime.
  A declared publish_job also queues a single-use confirmation rather than publishing automatically.
  Double taps cannot execute twice; an expired request or a job whose command changed cannot run.
- Goal checks must name every recorded criterion exactly once. Duplicates, substitutions, unmet
  and unverified criteria cannot close a task as succeeded. This validates coverage, not the truth
  of a model's evidence. A primary-agent development run must supply a usable goal plan.
- Raw dialogue attempts, including failures and superseded turns, are kept in the local trace
  store. Plain-text dialogue usage remains unavailable. No raw transcript is sent to MemTrace.
- A dialogue candidate waits at most 180 seconds before the declared fallback is tried. After
  30 seconds a still-pending attempt emits one wait notice; a fallback emits a transition notice.
  These notices do not claim completed work and require no extra model call. Exact short progress
  queries (for example `有在處理嗎？`) from the same audience while a dialogue call is pending are
  answered locally and recorded, without cancelling that call. New instructions, quoted replies,
  attachments and other audiences retain their normal handling. Development stage limits are separate.
- Chat progress explains the intended work at each stage's start and its result at the end;
  automatic checks and integration emit both boundaries too. Final development messages are short,
  while the complete goal check and technical evidence stay in the local run record. Reports preserve
  unrun checks, remaining gaps and partial integration, and use runtime delivery results rather than
  a model's promise to merge. Scheduled notification policies remain unchanged.
- Fresh structured CLI outputs include a nullable `user_summary` alongside their normal evidence.
  The prompt requests one or two plain-language sentences; the renderer bounds the displayed length.
  Older artifacts without the field still load and use bounded existing summaries. Generating the
  short summary uses the existing call, with no additional model invocation.

**POLICY_ONLY:** that the agent chooses sensible steps, explores alternatives before asking,
revises development phases appropriately, and interprets new user steering correctly. A new
message is available at the next decision boundary; it does not mutate or interrupt an in-flight
skill. The model does not gain write access by being the primary agent.

**PLANNED:** general automatic repair/replanning of operational plans, first-class remote
capability inventory/job delegation, and content-hash approval of generated tools. The existing
final gate and tool-install boundaries remain as detailed below. A job confirmation currently
binds its command text; transitive script/dependency content binding is not implemented.

## Proactive cross-Harness platform maintenance (operator decision, 2026-10-16)

**ENFORCED** (`improvement.py`, `memtrace_services/observation.py`, API/runtime/web wiring):
operator-enabled source Harnesses share bounded conversation, task-outcome and schedule evidence
through their local main-token APIs. The status service relays only configured targets; a coordinator
has a separate source allowlist and enabled/auto-development switches. Sources not sharing or not
reachable are reported as such. No new evidence causes no model call; no new findings causes no
notification. Findings require checked source IDs and verbatim quotes, remain draft local evidence
and never approve/adopt an Improvement Loop proposal or change a MemTrace gate.

This is a bounded exception to the older work-review proposal-only policy: the explicitly enabled
coordinator may queue platform development through the existing governed worktree/test/delivery
path, one automatic improvement at a time and at most three per rolling day. Other projects' business
schedule/preference suggestions never dispatch automatically. Queued work rechecks enablement and
source grants. The shared delivery policy, GitHub repo and verification command are prerequisites;
the observer cannot approve a confirmation, lower a gate, or directly execute a generated operation.
An in-flight task finishes under its existing boundaries; interrupted development is not replayed.

**POLICY_ONLY:** diagnosis quality, whether the quoted evidence supports the suggested mechanism
change, and preserving business intent in the generated fix. A verified quote proves provenance,
not the interpretation. Existing nightly audit and `/review` keep their own prior scope. This does
not add arbitrary access to remote chats or complete historical logs. See
[proactive-improvement.md](proactive-improvement.md) for configuration, retention and limits.

## Multiple CLI execution sources (operator decision, 2026-10-16)

**ENFORCED** (`execution_sources.py`, config/factory/process/loop/admin/API/web wiring,
`tests/test_execution_sources.py` and API/web tests):

- Provider, execution source and model are separate identities. A source is an operator-managed local
  executable/wrapper, never a provider HTTP client or credential. Multiple sources may use one adapter.
  Sources and quota/capacity state are host-wide; per-Harness grants and source choices remain in its
  `models.toml`/`roles.toml`. A global registration grants no workspace permission.
- All model chains, primary-agent decisions and dialogue, background duties, utility calls, and skill
  fallbacks can select named sources. Current provider-only settings retain their original commands.
  Named sources require provider matching and per-Harness authorization, rechecked at launch. They
  never change sandbox flags, repo roots or permission/context boundaries.
- SQLite leases enforce cross-process concurrency per declared quota pool. Capacity failures cool that
  pool; login/CLI failures cool only that source. Busy/disabled sources can advance only through the
  configured fallback chain. Test failures and generic editing timeouts do not authorize account replay.
  Named-source cooldown is managed globally, separately from legacy per-Harness availability records.
- Traces preserve canonical provider, actual source ID, model and quota pool. Raw provider output remains
  local, and usage completeness is preserved. Source configuration changes are revisioned; changing a
  captured executable/binding cannot silently redirect an invocation. Revocation stops future calls,
  and an in-flight call retains its original binding.

**POLICY_ONLY:** that registered wrappers isolate actual accounts and faithfully `exec` the provider
CLI; a different command name is not proof of a different login. **PLANNED:** automatic account/model
inventory, authenticated quota discovery and distributed multi-host scheduling. Existing source-less
commands are intentionally outside named-source concurrency management. Configuration, limits and
examples are in [execution-sources.md](execution-sources.md).

## Shared memory and automatic delivery (operator decision, 2026-10-16)

This section supersedes older per-action confirmation and local-merge rules **only for this fixed,
operator-enabled delivery workflow**. Unrelated tools, installations, scope changes and publish jobs
still require their own confirmation.

**ENFORCED** (`shared_memory.py`, `delivery.py`, service/loop/runtime and web wiring):

- Private conversation, preferences and memory remain per Harness. Operator-authored shared notes live
  in `~/.memtrace-harness/shared-memory.sqlite3` (`HARNESS_SHARED_MEMORY_DB` relocates it), outside the
  Git repositories. Harnesses using that file read it at each new chat/task. No private memories are
  automatically promoted. The global web section creates/edits/disables/removes notes through an
  authenticated Harness API; services never open its database. Changes retain revisions and reject
  stale edits. Review receives shared operator instructions, never the private chat transcript.
- The first note carries the explicit `worktree-pr-ci-main` policy. Its switch also controls automatic
  delivery; editing its prose does not change the runtime's fixed allowed operations. Ordinary notes
  provide context only. An unreadable policy store never authorizes a fallback merge.
- New development tasks use one isolated worktree per selected Git repo, even with one worker. Configured
  GitHub repos start from fetched `origin/main`, preserving the operator's checkout and local changes.
  An empty/non-Git workspace can still chat, but cannot develop in place under this policy. Each repo
  needs `github_repo`, a matching GitHub origin, and a configured `verify_command` that actually passes.
- Model goal planning/checks cover the complete local implementation request. Runtime-owned remote
  delivery criteria are checked separately by code, never claimed by a model before execution. The
  shared context tells every skill that the runtime owns remote writes and this bounded workflow
  already has operator authorization. After the complete local goal check and final gate, the loop
  returns `delivering`, never local success or
  a local merge for changed code. All selected repos' final verification must report `passed`;
  baseline failures, missing commands and unrun checks cannot authorize delivery. The runtime pushes
  only task branches without force, reuses or creates a PR to `main`, checks its exact head commit,
  CI and conflict state, then invokes `gh pr merge --merge --match-head-commit`. No admin bypass.
- Every selected repo is preflighted before the first merge and checked again immediately before its
  own merge. GitHub must actually report MERGED. Multi-repo remote merges are separate transactions:
  a later failure preserves and reports earlier merges. No-CI, pending/unknown checks, skipped/neutral
  checks without successful CI, pending review and unknown mergeability are not evidence of passing.
- Failed CI or conflicts trigger a normal fetch/merge and a fresh governed repair run in the **same**
  task worktrees, then local tests/gates and another non-force push. At most three repair attempts;
  an interrupted repair is held for inspection rather than blindly replayed. Remote diagnostics are
  untrusted data. Missing tools/auth, failed repairs or a one-hour CI/merge wait retain PRs/worktrees
  and report the gap rather than declaring success.
- After all selected repositories are merged, only explicitly associated task issues are closed and
  confirmed CLOSED. Prose references to historical issues are not swept up. Worktrees are removed
  without force only if clean and still at their verified commit. A clean local main is fast-forwarded
  when possible; a dirty/diverged checkout is preserved. New work uses fetched remote main regardless.
- Delivery intent and step results are durable in the private trace store. A restart resumes pending
  remote delivery by rereading GitHub state, without replaying settled development. Disabling the
  shared policy pauses subsequent operations and retains progress. Cancelling an owning plan also
  pauses delivery. Only remote merge, issue closure and cleanup confirmation produce final success.
- This implementation supports GitHub through the installed `git`/`gh` CLIs. GitLab MR delivery and
  non-Git targets are not implemented. The low-level `loop` replay CLI refuses to bypass an enabled
  shared delivery policy; submit such development through the running Harness instead.

**POLICY_ONLY:** that a model correctly identifies every task issue when the user does not supply an
explicit reference, and that follow-up problems are filed as new issues. The shared note instructs
this behavior; the runtime never reopens an old issue as part of delivery. Multi-host distribution
of the shared database and distributed transactions across repositories are not implemented.

## Harness and services (operator decision, 2026-10-09)

The Harness is the model-based work of one project; transports and presentation are separate
services. **ENFORCED** (`tests/test_service_boundary.py` and the API/service tests):

- One Harness serves one workspace (a folder; empty and non-git are fine) and keeps everything in it:
  `.harness.env` (environment: secrets, endpoints, CLI paths; mode 0600), `.harness/` (store, raw traces,
  worktrees, and the preference files `models.toml` and `roles.toml`), `harness-scope.md`. Preferences are
  not in the secrets file: the status page edits `models.toml`, never `.harness.env`. Settings have no
  per-project suffixes. When the workspace is a git
  checkout, `.harness.env` and `.harness/` are excluded locally (`.git/info/exclude`), and the project's
  off-limits list always includes them.
- The Harness has no chat transport: no Bot API call, no bot token, no chat id. What it wants said goes
  into an **outbox** (neutral events: text, optional `{label, id}` button rows, a `bind` saying what to
  remember the delivered message for); what people say reaches it as `handle_message` / `handle_action`.
  An audience or message reference is an opaque string a service hands in (`tg:<chat>`,
  `tg:<chat>:<message>`); the Harness only passes it back. `None` means "the operator, wherever they are".
- A service is its own process with its own env file, outside the workspace. It never imports the Harness
  (and the Harness never imports a service), never reads its database or env file, and reaches it only
  through the Harness's local HTTP API (loopback, bearer `HARNESS_API_TOKEN`; the API refuses to start
  without one). The Telegram service holds the only copy of the bot token and enforces the chat
  allowlist before anything is relayed; one bot serves one Harness.
- Delivery is at-least-once: an event is leased to a fetch, redelivered if not acknowledged, retried up
  to five times, and only then given up on (an order or job proposal that never reached the operator goes
  back to `pending`). The acknowledgement carries the message references that let a reply to an approval
  resolve to that approval.
- A child process (provider CLI, verify command, job, worktree setup) gets an allowlisted environment, never the
  Harness's secrets; only a Codex call wired to the MemTrace MCP server also gets `MEMTRACE_API_TOKEN`.

**POLICY_ONLY**: `off_limits` keeps agents away from `.harness.env` and `.harness/` by prompt; a Developer
that can read the workspace can read those files. Keeping service secrets (bot token) outside the workspace
is what protects them; the Harness's own secrets (MemTrace token, API token) rely on the allowlisted child
environment and file mode.

**Repos (operator decision, 2026-10-09; multi-repo editing, 2026-10-16).** One MemTrace workspace is one Harness;
the other repos of the same product are `.harness/repos.toml` entries (`ProjectScope.repos`, `for_repo()`).
**ENFORCED**: `repo=<name>::` selects one repo; `repo=<name1>,<name2>::` selects several; no prefix selects the
primary repo, and unknown names are refused before a slot is taken. Every selected repo gets its own task worktree and
branch, configured verify command, setup command, GitHub repo and off-limits rules. Only `develop` receives those
selected worktrees as writable roots; unselected repos are made available only to read-only profiles, and every CLI
adapter maps the granted roots into its provider workspace flags. The read-only safety check snapshots all granted
repos. Multi-repo tasks require every selected repo to be a Git checkout with a commit, so the Harness never falls
back to editing multiple original checkouts in place. Each configured verify command and the final gate runs against
its own repo. After the primary agent confirms all goal criteria, the Harness preflights every changed repo and merges
the branches separately; if a later Git merge fails, it reports earlier merges and retains the remaining worktrees.
Per-repo edit locks still apply when a task has to edit in place; worker slots, workspace lock, approvals, schedules
and conversation remain Harness-wide. The selected repo set is recorded so approval resume returns to the same
worktrees. Scanner candidates from GitHub issues continue to be proposed and worked in their owning repo. Jobs and ops
tools still use the primary repo only.

## Model list key (operator decision, 2026-10-10)

Claude Code cannot list its models, so the status page's Claude menu may ask Anthropic's Models endpoint.
**ENFORCED** (`tests/test_model_catalog.py`):

- The only provider HTTP call in the Harness is `GET https://api.anthropic.com/v1/models`, made from
  `model_catalog.py`. Model execution stays CLI-only; the key is never used for inference.
- The key is `HARNESS_MODEL_LIST_ANTHROPIC_KEY`, looked up in the real environment, then the workspace's
  `.harness.env`, then the Harness-wide `~/.memtrace-harness/secrets.env` (mode 0600; `HARNESS_GLOBAL_SECRETS_FILE`
  relocates it). That file takes this one variable only, and it is read on demand, never copied into the
  process environment; `child_env` drops it, so no model CLI ever sees it. It is not logged or shown on the
  status page, and `doctor` checks the file's mode and stray keys.
- No key, or a failed call, falls back to the built-in list plus models added by hand
  (`.harness/extra_models.json`); nothing else depends on it.

## Questions from another Harness (operator decision, 2026-10-14)

Another Harness may ask this one a read-only question over A2A (`memtrace_services/a2a`, `peer.py`; design in
[`a2a-peers.md`](a2a-peers.md)). **ENFORCED** for the receiving side (`tests/test_peer_api.py`,
`tests/test_a2a_service.py`), and for the calling side (`tests/test_peer_calling.py`):

- A peer is an outsider, not the operator. It reaches the Harness only through `/v1/peer/*` with
  `HARNESS_PEER_API_TOKEN`, which opens nothing else; the main token does not open `/v1/peer/*`, and the two
  tokens must differ. The a2a service authenticates peers by SHA-256 of a per-peer bearer token.
- Read-only: only the `ask` skill exists. The model gets MemTrace's read tools in an empty scratch folder;
  no ops tools, trading proxy, attachments, operator identity or preferences. The peer's words are quoted to it
  as data, and every `HARNESS_*` directive line in its reply is dropped, so nothing a peer says can start a
  task, schedule, note, install or approval. Delegation is not offered; when it is, it will stop at the
  receiving operator's tap.
- Limits: a request that has passed through 2 Harnesses is rejected (`hops`, cooperative); a global cap of
  `HARNESS_PEER_MAX_CONCURRENT` (default 2) unfinished requests, then `rejected: busy`; per-peer rate and
  concurrency limits in `a2a.toml`.
- A request is visible only to the peer that made it; another peer's id and a missing id are indistinguishable.
  A peer sees only the answer and fixed reason phrases. Requests are kept in the trace store (`peer_requests`).
- Asking a peer (`ask_peer`) is a `change`-tier tool: it needs the operator's tap, or a standing "always allow"
  bound to the peer name (revocable). The model never opens a connection; only peers listed in the a2a service's
  `[outbound.*]` can be asked. A peer's answer reaches the operator and the chat transcript only as quoted,
  unverified data with every `HARNESS_*` line removed; it is never an instruction.
- **Collaboration (`trust = "collaborate"`, ENFORCED, `tests/test_peer_collaborate.py`):** a peer the operator marks in the
  a2a service's `[peers.*]` may also report a problem. The Harness diagnoses it with the read-only ops tools in the
  project; if it proposes a fix, that is a confirm card (`peer_fix`, one tap or a standing per-peer "always allow"),
  and the tap starts an ordinary governed task. Code changes still go only through the Agent Loop, the peer's Task ends
  with that task, a declined or lapsed card ends it `REJECTED`, and the peer sees only fixed phrases until it
  completes.
- **A Harness has its own name on the peer network** (`HARNESS_PEER_NAME`, default the project's name lowercased; ENFORCED, `tests/test_peer_pairing.py`): links, the Agent Card and audit labels use it, never the folder's or project's name; a rename changes only the label (access rests on the token), and two Harnesses with one name cannot be paired.
- **Links are kept by the Harness and changed on the status page** (`peer_links.py`, `web/pairing.py`;
  ENFORCED, `tests/test_peer_pairing.py`): `.harness/peer_links.json`, mode 0600, holds only the SHA-256 of an inbound token
  (shown once, when the link is made) and the token of an outbound link; the main-token view and the page's data carry no
  secret. A revocation takes effect at the A2A service's next read (≤ 10 s) without a restart. A pairing is both halves
  or neither; the token goes between Harnesses through the page's process and never to the browser (an invite string is the one
  deliberate exception, shown once). The generic admin routes cannot make a link. The status page has no login.
- **POLICY_ONLY:** under Codex the CLI's read-only sandbox, not this code, decides what else the model could read;
  see "Honest limits" in `a2a-peers.md`.

## Safety envelope and project-owned loops (operator decision, 2026-10-14)

The Harness guarantees a safety envelope around every project's work; the shape of the work itself (which
stages run, how many reviews, which providers) belongs to the project. **PLANNED** as a whole: nothing in
this section is enforced yet, and the current single-Controller loop with its plan/review/develop skills,
G1/G2 and goal check (see "Role contract") stays as it is until each item below ships and is marked
**ENFORCED** here. Callers must not rely on it.

**The envelope is the Harness's and a project cannot weaken it.** Its purpose is three things, and each is
checked by code the Harness runs, not by a model's judgement:

- **Stay in scope.** Writes are confined to the task's worktree and the paths the task declared. At
  completion the Harness compares the actual diff with the declared scope; anything outside it blocks the task.
- **Do not harm the system.** Execution stays in the sandbox; sudo and deletion are never offered; installing
  a capability needs the operator's tap. (Largely ENFORCED today through the ops allowlist, capability
  consent and the git-status safety net on read-only calls.)
- **Do not leak data.** The diff and the output are scanned for secrets; `.harness.env` is never read or
  printed; what is written to the trace store or MemTrace is redacted first; outbound network use by a
  produced tool is declared, not assumed. (Only partly enforced today.)

**A tool the Controller produces needs a person's confirmation before it can run.** A tool is anything with a
side effect or that will run again: a script, a scheduled job, a skill, an MCP server, an outbound call, and
the project's own loop definition. Ordinary code changes to the project are not tools; they go through the
project's loop and its verification as usual.

**Which path a piece of work takes (the interface; PLANNED).** The split is whether the product runs again by
itself or has a side effect, not who wrote it. A new scheduled script is a tool even if it is one line; a change
to a View or an API is ordinary code even if it is large.

| Work | Who guides it | Harness check at the end |
|---|---|---|
| Ordinary project code (feature, fix, refactor) | the project's own loop; Harness adds no stage | none beyond the always-on envelope (worktree-only writes, no sudo/delete, no `.harness.env`); merge stays the Controller's/operator's decision |
| A tool (script, job, skill, MCP, outbound call, loop definition) | the Controller/Developer | the one final gate (scope, safety, sensitivity), then the operator's content-hash confirmation |

The Harness never inserts a review, plan or test-gating stage into the first row. Test results are information
to the project's loop and the operator; whether a failing test blocks progress is the project's choice, not a
Harness gate.

**ENFORCED (2026-10-15): verification is information, and `loop_mode: lean`.** Two parts of the above are in code
now; the tool gate, secret scan and declared-scope diff are still PLANNED.

- `verify_command` is run by the Harness and its result is classified (`verification.py`): `passed`,
  `could_not_run` (a `MISSING_TOOL:` line, exit 127, "SDK location not found", a timeout), `baseline_failing`
  (in a task worktree the same command is run once on the main checkout; failing only tests that already fail
  there) or `regression` (a failure the base does not have, or one that cannot be told apart from it). Only a
  `regression` sends work back. The others are recorded and shown to the Developer, G2 and the operator, and are
  never charged to the task.
- `loop_mode: lean` in a project's `harness-scope.md` (default since 2026-10-15; `full` is the older plan/G1/develop/G2 loop, opt-in): the Harness adds no plan,
  G1 or G2. A phase is develop, then the verification; a regression goes back to the Developer once, a second
  one stops the task as `needs_human`; anything else proceeds, and the Controller's converge step sees the
  recorded `verify` stage. The Controller is told to assign `develop` only.
- **Final gate, first step (ENFORCED 2026-10-15, `final_gate.py`).** After the last phase and before the
  Controller's converge step, the Harness checks what changed, by code, in every `loop_mode`:
  - *Boundary* (stops the task): a file outside the working folder, the Harness's own files (`.harness`,
    `.harness.env`), `.env*` (not `.env.example`), key files, or anything in the project's `off_limits`.
  - *Scope* (stops the task): the Controller gives each `develop` phase `paths` (files or folders, globs allowed).
    When every develop phase declared some, a change outside all of them stops the task; when any phase declared
    none, only the boundary applies.
  - *Secrets* (stop): private keys and common token formats in added lines. *Personal-data formats* (Taiwan ID and
    mobile numbers) only warn. Excerpts are masked in every message and trace.
  - *Source of the change:* with a task worktree, git (the diff against the starting commit plus new files, with
    line numbers). Without git, or when a project is edited in place (a diff there would include the operator's own
    uncommitted work), only the files the Developer reported are checked; a file changed but not reported is not
    seen.
  - A stop is `needs_human` with the exact findings and a fingerprint of those findings and contents. The operator
    waives it by answering `放行 gate <fingerprint>`; any change to the content needs a new waiver. A model can add a
    finding and cannot clear one. Still PLANNED: the tool check (a script, job, skill, MCP or loop definition needs
    a content-hash confirmation), redaction before trace/MemTrace writes, entropy-based secret detection.
- A fresh task worktree can be made usable by the project's `worktree_setup_command` (in `harness-scope.md`, or per
  repo in `.harness/repos.toml`), e.g. copying a gitignored `local.properties`; `HARNESS_REPO_ROOT` is the main
  checkout.

- The tool declares what it reads, writes and connects to. The operator is shown the diff and that
  declaration, never only the model's summary.
- Approval is bound to a hash of the content. Any change voids it and asks again, as the per-job command
  hash allowlist already does.
- An unconfirmed tool stays a draft: it cannot be scheduled or called by another task.
- Loosening a limit asks for a tap; tightening one does not.

**The loop belongs to the project.** A project defines its own loop in its own repository, in the form its
CLIs already load natively (`AGENTS.md`, `CLAUDE.md`, skills). The Harness dispatches that loop inside the
envelope and sees what comes out (diff, trace, declared permissions), not how it got there.

- The envelope lives in the Harness, outside the worktree the project's agent can write, so a loop
  definition cannot reduce it however it is edited. The loop definition itself is a tool and is confirmed
  like one; otherwise an agent could rewrite its own loop to hollow out its quality checks.
- A project with no loop of its own gets a minimal default: one develop run, the project's `verify_command`,
  then the envelope checks.
- **The Harness's own loop shrinks to one gate, at the end.** The several mid-loop gates (G1 on a plan, G2
  on a development, the Controller's goal check as a stop condition) are retired as Harness mechanism. After
  the work, the gate runs once and answers three questions, all by code the Harness runs: is the diff inside
  the declared scope, is anything unsafe (a tool or loop definition not yet confirmed, a forbidden command,
  a write outside the worktree), and does the diff or output hold anything sensitive. A failed check blocks
  the task as `needs_human` with the exact finding; there is no model-revision round behind it. A model may
  add a finding but can never clear one. "Gate" in this section means this final gate; in older text and in
  `AGENTS.md` it also meant G1/G2, which keep running until this ships.
- The plan, review and develop skills, and a model-based review or goal check, become a reference loop a
  project may copy and own. They are quality layers a project chooses, not the safety guarantee, because a
  model's verdict cannot be.
- Stage-aware features (resuming past settled stages, the stage detail in approval messages, progress on the
  status page) depend on the Harness knowing the stages. A project loop may emit checkpoints and
  `needs_human`; if it emits neither, the Harness treats it as a black box and reports only the diff and
  the trace.
- Migration order: new projects first; projects with uncommitted work in their folder (Beri,
  TWTradingStrategy) last, and never on a dirty checkout.

**A project that finds a tool missing asks the Harness that has the machine (operator decision, 2026-10-14;
PLANNED).** `kubectl` is the running example. A project's Harness (Beri's, say) reaches a step it cannot do
because no allowed tool covers it, for instance restarting a service, and the following happens. Nothing
below is built; the pieces it reuses (A2A `ask`/`collaborate`, `request_install`, declared jobs with
command-hash allowlisting) are ENFORCED as described elsewhere in this contract.

- **The signal is structured.** A verification or job that cannot run because something is absent reports a
  `MISSING_TOOL: <name>` line (Beri's `.harness/verify.sh` already does), so "the tests failed" and "the tests
  could not run here" are different outcomes.
- **First tap, to ask.** The project's Harness asks the operator whether it may ask the machine's Harness
  (`ask_peer`, which already needs a tap). The card says what is being asked: whether `kubectl` exists and
  where, for what purpose. This consent is to ask, not to use anything.
- **The inventory is read-only and narrow.** The machine's Harness checks only `PATH` and a short fixed list of
  locations (`command -v`, `--version`) and reports path, version and whether a credentials file (for
  `kubectl`, a kubeconfig) exists: present or absent, never its content. It does not search the disk. What it
  says back is data to the receiver, never an instruction.
- **Tool absent.** The answer is the install command, shown to the operator, who decides and runs it. For a
  tool that reaches a cluster, the system or credentials the Harness gives the command and does not install;
  an ordinary package may still go through `request_install`'s own tap. Installing a binary is not
  configuring it: credentials are set up by the operator and the Harness never touches them. After installing,
  the project asks again, and this becomes the next case.
- **Tool present.** The machine's Harness does not hand the project's agent the binary's path. It proposes a
  named, bounded **job**, for example `kubectl rollout restart deployment/l8s -n <namespace>`, and the second
  tap shows the exact command and the context it will run against (for `kubectl`, the kube context). The
  Harness runs the job itself, so credentials never pass through any agent; the project's agent receives a
  job name and its result.
- **Scope of the grant.** Default is this one run. The operator may choose "always allow this exact command",
  bound to the command's hash and revocable. Verbs that destroy or expose (`delete`, `exec`, `drain`, reading
  secrets) can never be made standing, whatever is tapped. `sudo` and deletion stay refused as everywhere
  else in the envelope.
- **Two taps, two meanings.** The first lets the project ask; the second grants one concrete command. Neither
  stands in for the other, and a tap given before it is known whether the tool exists never carries over to a
  grant.

Unchanged: the Harness output stays draft evidence and cannot approve its own gate; CLI-only model
execution; one editing provider per run; the permission rules in "Role contract".

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
- MemTrace writeback is explicit; the nightly topic notes and daily digests are written as
  draft-labelled notes (tags `harness`, `draft`), and **no conversation transcript is ever written to
  MemTrace** — see "Memory archive" below;
- a governed run's record stays in the local trace store and is **not** also written to the project's
  MemTrace workspace as a "Harness loop draft" node (explicit operator decision, 2026-10-07: those drafts had
  become 95% of one specification workspace); what a run teaches reaches the knowledge base through the
  Controller's own `kb_updates`. The `--writeback` CLI flag still writes one on request;
- the chat model holds no MemTrace write tool, on any vendor; it reads (Codex and Claude alike) and asks the
  Harness to write with `HARNESS_KB_NOTE::<type>::<title>::<body>` lines, which the Harness validates (max 3 per
  reply), writes to the project's memory workspace tagged `harness`, `chat`, `note`, and reports back to the
  operator, failures included (2026-10-09);
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
"key-point" layer above the digests. Topic notes, nightly digests and loop drafts are still
written draft-labelled; what the Controller writes is not (next paragraph).

**Memory archive: hot by day, topics by night (explicit operator decision, 2026-10-10).** Applies to
every Harness, because it is how the shared code works, not a per-project setting.

- *By day* the conversation exists only in the local hot log (SQLite): verbatim, complete, and what
  chat and the Agent Loop read as their recent context (`Prior discussion`, the recent daily digests).
  Nothing is written to MemTrace on a timer, and **a transcript — raw or lightly trimmed — never
  goes into MemTrace at all**. What cannot wait for the night is written at once as an already
  organised note by the chat model (`HARNESS_KB_NOTE::`).
- *By night* (the digest window) the Harness reads the whole day and splits it **by matter before
  anything enters MemTrace**: one memory node is one matter (a strategy rule, a bug, a decision, an
  open question), however the day's messages interleaved. A day with many subjects produces many
  nodes, never one node holding the day.
- **Automatic memory archive switch** (`[archive].enabled` in `.harness/models.toml`, default `true`):
  when off, the nightly pass skips topic filing and its scheduled weekly matter review/draft retirement.
  Explicit `memory-topics`, `memory-rebuild` and `memory-reorg` commands remain available.
- A node's **title is the matter itself**, in the words someone would search for — no date, turn
  number, project name or `Draft` prefix (the tags and the memory workspace say what and whose it is).
  Its **body is organised, not quoted**: a one-line current state first, then decisions, facts and
  open items, each with the turns it came from (`#687–#688`; the words are in the local log when the
  source is needed).
- A matter that already has a node is **updated, not duplicated**: the Harness keeps a local
  `memory_topics` table (matter → node), shows the model the existing matters, and a later day's
  discussion of the same matter revises that node (its current state replaced, older entries kept
  dated below). The nightly digest is the day's index: it links to the matters it touched.
- The Harness does this itself: every night it archives each finished day that has no matters yet —
  yesterday, a day it was down for, history from before this existed — oldest first, at most 7 days per
  project per night (a longer backlog is worked off over several nights). No operator action is needed;
  `memory-topics` only runs the same step by hand.
- *The model* for all of this (the nightly split, the weekly review, a rebuild) is the project's own
  **memory-archive chain** (`[archive]` in `.harness/models.toml`, chosen on the status page like the chat,
  digest and recall chains); with none configured it follows the digest chain. A configured chain is used as
  given and never degrades into a weaker one. The call has no tools: it is shown the day's log and the matters
  already known, and the Harness does every write.
- ENFORCED in code: every item cites turns that exist in that day's log, or it is dropped; a matter
  the model says continues an existing one must name an existing one; a failed model call or
  MemTrace write marks nothing archived and the next pass retries (days are caught up in order, so a
  matter's history stays chronological); only after a day's matters are written are that day's turns
  marked archived. Schedule firings are not matters; only each schedule's last result reaches the
  digest.

**Reorganising the knowledge base (explicit operator decision, 2026-10-10).** The Harness can go back and
reorganise what it wrote, by three means. All three touch only the Harness's own notes (matters and old
drafts, tagged `harness`) — never a node a person or another role created — and every removal is
MemTrace's soft delete (restorable for 30 days) with the local row kept.

- *Weekly matter review* (after the nightly archive, per project, at most weekly, only with at least 6
  matters): a model reads the matter list and may propose `merge` (two or more matters that are one
  subject), `retitle` (the title no longer names what the matter is about) and `resolve` (nothing more to
  do on it). The Harness validates every op against the matters it showed: ids must exist and be live, a
  matter appears in one op only, a `resolve` needs a reason and a matter untouched for 7 days, and a pass
  does at most 10 ops. A merge combines the matters' dated entries into the kept matter, updates its node
  and soft-deletes the others' nodes; a resolved matter stays searchable, marked resolved, and is reopened
  by the nightly archive if it is discussed again.
- *Draft retirement* (nightly, after the archive): an hourly `Draft: …` node written before 2026-10-10 is
  soft-deleted once every day its turns span has been archived into matters. Nothing is lost: the words are
  in the local log and the substance is in the matters.
- *Rebuild* (`/rebuild`, the chat model's `HARNESS_MEMORY_REBUILD::`, or `memory-topics --rebuild`; never
  scheduled): asking for it only shows a confirmation card, and it starts only after a tap on "開始重建".
  `/reorganize` (or `HARNESS_MEMORY_REORG::`) runs the matter review and the draft retirement at once, without
  a card, since every change in them can be undone. The local log is complete, so all matters can be
  derived again from the first day, with the current prompts and models. The old matters are superseded
  first and their nodes are soft-deleted only after the new set is fully written; an interrupted rebuild
  resumes where it stopped.
- Every pass is recorded as a run with its ops (`memory_reorg_runs`, `memory_reorg_ops`), reported to the
  operator with a one-tap undo (`memory-reorg --undo <run>` does the same by hand), and the matter notes are
  protected from the weekly knowledge-base tidying (`kb_gardening`).

**Nightly audit and self-calibration (explicit operator decision, 2026-10-11).** The Harness starts it
itself, in the digest window after the digest and the archive have filed the day (`memory_audit.py`,
`duties.audit_night`; by hand: `memory-audit`). It works toward one **north star: the operator reaches
what they want with the least repetition and round-tripping** — the Harness understands once and does
the right thing.

- **Automatic audit switch** (`[audit].enabled` in `.harness/models.toml`, default `true`): when off,
  the scheduled nightly audit is skipped. An explicit `memory-audit` command still runs on request.

- *Metrics are mechanical, not the model's opinion* (`compute_metrics`, recomputable for any past day, per
  ten operator messages): corrections ("我說的是…", "你誤會了"), restated messages, matters that came back on
  3+ of the last 14 days, matters that took 12+ turns in one day and are still open, and flaws in the
  Harness's own filing (digest items dropped as ungrounded, questions the digest could not settle,
  undone reorganisations). Their sum is the *friction* index shown on the status page.
- *The audit is an independent model call* (`[audit]` chain in `.harness/models.toml`, chosen on the status
  page; with none configured it follows the digest chain). It has no tools and no chat persona; it is
  shown the metrics, the operator's usual level, the day's log, the notes in force and what became of
  earlier findings, and it may find nothing. ENFORCED in code: a finding must be one of four kinds, must cite
  turns that exist in that day's log (except a `memory_quality` finding, which may rest on the numbers) or
  it is dropped; at most 6 findings and 3 new notes a night.
- *What it may change by itself*: a **calibration note** — one short rule (8–200 characters) added to the
  prompt of the chat, digest or archive stage, labelled as learned from audits and stated never to override
  that task's rules, evidence requirement or output format. ENFORCED: a note that tries to loosen those
  rules (ignore/skip citing/approve/tool or write permission), duplicates an active note, or would be the 9th
  active note for its stage is refused and becomes a proposal instead. The audit never edits code, models,
  gates, role profiles or Improvement Loop proposals; those it can only *propose* to the operator (it still
  cannot approve its own proposal).
- *Every correction is checked afterwards.* Seven days after a note was adopted the Harness recomputes the
  metric the note was meant to move (misunderstanding → corrections, repeat work → restatements and
  recurring matters, round trips → long open matters, memory quality → filing flaws) against the seven-day
  baseline before it. At least 20% lower keeps the note (`effective`); unchanged or worse retires it
  (`ineffective`), and the operator is told; no baseline signal or too few active days is `inconclusive` and
  keeps the note. Retired notes stay on record, and an earlier failed fix is shown to later audits so it is
  not proposed again.
- *Reversible and reported*: each night's findings, metrics, notes and verdicts are sent to the operator
  with a one-tap "撤銷這次校正" (`memory-audit --undo RUN`); nothing is deleted. A failed model call records
  nothing, so the day is audited on the next pass; only finished days that are already digested and archived
  are audited, the latest 3 at most, 2 per night.
- **Known limits.** The correction and restatement detectors are phrase and similarity heuristics, so they
  miss a quiet misunderstanding and can flag a harmless repetition; the model audit reads the same log and
  can be wrong in the same way as the stages it audits (hence the separate chain and the mechanical check
  after a week). The verdict needs a week of data, so a note on a quiet project may stay `inconclusive`.

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
The model may push one chat message when it found a connection the human likely no longer
has in mind. **PLANNED, not implemented:** a reverse index so the nightly digest can mark which
older items a day's topics continued.

- **Background recall switch** (`[recall].enabled` in `.harness/models.toml`, default `true`): when
  off, incoming chat messages do not start topic-recall runs. Existing briefs remain available to
  the chat model until they expire; explicit memory searches are unaffected.

**Concurrent tasks per project (explicit operator decision, 2026-10-05).** A project runs up to
`max_workers` governed tasks at once (`harness-scope.md`, default 3; 1 restores one-at-a-time).
ENFORCED in code: slots are claimed atomically in SQLite (`workspace_locks`, one row per running
task), so the limit holds across threads; a task that finds every slot busy waits in a persistent
FIFO queue (`task_queue`) and is started when a slot frees, including after a Harness restart; a
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
has no network, so a connection or DNS failure seen there says nothing about an outside service (a chat answer
once blamed "TWSE unreachable from here" when the project's own script was merely timing out on a slow legacy
endpoint). Diagnosis and re-running a job are the harness's own job; only changing code goes through the Agent Loop.
`ops_mcp.py` is an MCP server (spawned outside the CLI sandbox, like the TaiwanTrade proxy) given to the chat reply
for a project whose scope file declares something: `probe_hosts` (hostnames `http_probe` may GET; redirects are
reported, never followed), `log_files` (what `read_log` may tail) and `job: name = command` lines. **Enforced:**
those allowlists; `run_job` never runs anything — it records a `job_requests` row, the Harness shows a
confirm button in the chat, and the command (re-read from the scope file at tap time) runs in a daemon thread only after the
tap, once per request, with a 15-minute limit; its result is sent and written to the chat log. Unanswered
requests expire after 10 minutes (reminded once before that, see below). **Prompt policy, not enforced:** that the model probes before answering and only
starts a development task when the cause is in the project's code. Wired for Codex and Claude chat models only
(Antigravity chat gets the notice but no tools). **Unverified against live models:** that a chat model calls these
tools sensibly. A project that declares nothing is told to say it cannot verify from the chat sandbox.

**Giving a project more ops tools without a code change (operator decision, 2026-10-10).** Which ops tools a
chat reply has is whatever the scope file declares, and the chat model has no way to write that file. When it
needs a capability that is not declared it calls `propose_scope_change` (kind `probe_host`, `log_file` or `job`),
which only records a `job_requests` row of kind `scope_change`; the Harness shows the exact proposal in the chat
with a "確認新增" button. **Enforced:** the proposal is validated when it is made and again at the tap (bare
hostname; log path relative and inside the project; job name charset and a one-line command of at most 1000
characters); nothing is written before the tap; a double tap applies it once; an unanswered request expires
after 10 minutes; the edit keeps the rest of the scope file and is an atomic replace; the in-memory scope is
reloaded so the next chat reply has the tool, with no restart. A newly added `job` still needs its own
confirm tap before it runs. A project that declares nothing still gets `propose_scope_change`. **Prompt policy,
not enforced:** that the model checks `list_jobs` first and only proposes what the user's request needs.
**Not implemented:** removing a declaration through the chat (edit the scope file), and any other scope field.

*Job shape, add-and-run, reminders (operator decision, 2026-10-13; ENFORCED, `tests/test_scope.py`,
`tests/test_ops_mcp.py`, `tests/test_service.py`; the same for every Harness, nothing is Beri-specific).* A newly
proposed job must be a short call, not a program: at most 400 characters, no code written inline for an
interpreter (`python -c`, `node -e`, `perl -e`, ...), balanced quotes, and valid shell (`bash -n`, nothing is run).
Logic goes in a script file inside the project that the job only runs (`bash scripts/x.sh`): the script must
already exist, parse (`bash -n` / Python `compile`) and contain no line the command policy refuses
(`command_policy.py`). The chat sandbox is read-only, so a missing script is written through the development flow
first. A proposed name that is a prefix of an existing job's name (or the reverse) is refused with a pointer to
re-propose under the existing name, which replaces it. **Add-and-run:** `propose_scope_change` takes
`run_after=true` (jobs only) when the request is "add it and run it"; the card then says so and reads
"確認新增並執行", and that one tap adds the job and runs it once. It is never offered "always allow" and is never
auto-approved by the allow list; without `run_after` a new job still needs its own tap to run. **Reminders:** a
request that is still waiting 3 minutes before its 10-minute limit gets one reminder, which replaces the original
card's buttons; an expired request is reported as "not done" with a "重新提出" button that queues the same
request again with a fresh limit (once; the expired one becomes `retried`). **Known limit:** the allow list binds
to the command text, so editing a script a job runs does not make an "always allowed" job ask again; the script
is checked when the job is proposed or added, not on each run. **Not implemented:** a configurable limit
(`JOB_REQUEST_TTL_SECONDS` and `JOB_REMINDER_LEAD_SECONDS` are constants in `ops_mcp.py`).
The Developer can still edit the scope file as project code unless `off_limits` says otherwise.

**Read freely, ask before changing, never for some things (operator decision, 2026-10-12; ENFORCED, `tests/test_ops_read.py`).**
The chat model's ops server always has four read-only tools that need no confirm tap: `read_file`, `list_dir`,
`query_db` and `system_status`. **Enforced:** a path must resolve (symlinks followed) inside the project's own folder or a
root the operator declared in the scope file (`- read_roots: ...`) — reading another Harness's workspace, its `.harness`
database or `~/.config/memtraceharness` is therefore something the reading Harness must declare, never implied; files that
hold credentials (`.harness.env`, `*.env`, `web.toml`, keys, `.ssh`...) are refused by name even inside a declared root, and
every returned text has tokens masked; `query_db` opens the file `mode=ro` with `query_only` and a SQLite authorizer that
allows only SELECT (a write, `ATTACH`, `PRAGMA` or a second statement fails), at most 200 rows, 5 seconds;
`system_status` is a fixed set of argv lists with no shell (`launchctl list/print`, `ps`, `git status/log/diff --stat/branch`), plus device errors: `simulator_log` / `mac_log` (`log show`, last hour, error+fault, validated process name) and `crash_reports` (DiagnosticReports and synced-iPhone MobileDevice crash files, fixed directories) and `device_crash_reports` (`xcrun devicectl` file copy of `systemCrashLogs` from a paired iPhone, including Xcode wireless; read-only; the phone must be unlocked).
Changing anything still goes through a request and a confirm tap. **Never run for a model, whatever the allowlist says**
(`command_policy.py`): `sudo`, deleting files or data (`rm`, `find -delete`, `git clean`, `git reset --hard`, forced push...),
and changing system or security settings (`defaults write`, `launchctl bootout/load...`, `networksetup`, `pmset`...). Such a
command cannot be declared as a job, a declared one is refused at run time, and the chat model is told to hand the user the
command to run in their own terminal; the check is fail-closed text matching, not a sandbox, so it can also refuse a harmless
look-alike. **Allowed list:** the confirm card of a job has a second button, "確認並之後都允許"; the Harness then records the
job name plus the SHA-256 of its exact command in `.harness/allowed.json` (written only by the Harness, mode 0600; the chat
model has no tool for it and `.harness` is off-limits to the Developer). A request for that same job with that same command
runs without a tap and says so in the chat; one changed character means it asks again; a forbidden command never matches.
`memtrace-harness allowed list|revoke <job>` shows and removes entries. **Prompt policy, not enforced:** that the model reads
and judges the result before answering instead of handing raw output over. **Not implemented:** a job's result is still sent to the chat as-is rather than being assessed by a model.
**The allowed list also holds the other requests the chat model makes** (operator decision, 2026-10-12; ENFORCED): a GitHub
action, a scope change and a model change each carry the same second button. What an entry is bound to: a GitHub action → its
*type* in one repo (`issue_comment` on `owner/repo`; the text and number differ each time, so an exact match would never
repeat, and `issue_close` is a separate entry); a scope change → the exact change, never a job command the command policy
forbids; a model change → the exact target, provider and model. Each auto-run says so in the chat; `memtrace-harness allowed
revoke <job or label>` removes one.

**Tools declare a tier; new tools follow the same flow (operator decision, 2026-10-12; ENFORCED, `tests/test_tool_registry.py`).**
Every tool the chat model can call is a `ToolSpec` in `tool_registry.py` with a tier: `read` (runs at once), `propose` (only
records a request that has its own confirm card) or `change` (the call becomes a `tool_call` request; the handler runs only
after a tap or an allowed-list match). **`change` is the default tier**, an unknown tier is refused at registration, and a tool
that is not registered cannot be called. A `change` tool gets the confirm card, expiry, double-tap guard, "確認並之後都允許"
(bound to the exact tool and arguments unless the tool declares a narrower `rule`) and the result message without any new
code in `service.py`; a request kind the Harness has no handler for is refused rather than run. The command policy (sudo,
deletion, system or security settings) sits outside the tiers and applies to every tool that runs a command. **`browse`**
(`browser.py`, tier `read`): a fresh headless Chrome with a throw-away profile (no logins), JavaScript run, text and links
returned; http/https only; loopback, private and link-local hosts refused unless listed in `probe_hosts`; the result is
framed as untrusted data and tokens are masked. **Known gap:** a public page redirecting to an internal address is not caught.
**Not implemented:** browser clicking, typing and submitting (they would be `change` tools); entering passwords, payment details
or CAPTCHAs stays the user's own. **Prompt policy, not enforced:** that the model ignores instructions found inside a page —
the enforced part is that nothing a page says can start a `change` action without a tap or an allowed rule.

**`github_ci`** (`github_ci.py`, tier `read`; operator request 2026-10-12, after a chat could not read a private repo's failing Actions
run through `browse`): `list` recent runs, `view` one run's jobs and steps, `failed_log` the failing steps' log (the end is kept), through
the host's own logged-in `gh`. **Enforced** (`tests/test_github_ci.py`): offered only when the scope declares a `github_repo`; the repo must
be one of the project's own; fixed sub-commands in an argv list, numeric run id only; output token-masked and clipped; nothing is written to
GitHub. **Not implemented:** re-running a workflow or any other write (they would be `change` tools).

**Multi-step plans (operator decision, 2026-10-12; ENFORCED, `tests/test_plans.py` and the plan tests in `tests/test_service.py`).**
When a request needs more than one action, the chat model ends its reply with `HARNESS_PLAN::<one-line JSON>` (goal, up to 10
steps, each a title, a tool, its arguments and an expectation). The Harness runs the steps in order from its tick
(`plan_runner.py`), keeps one progress message and edits it in place (new `edit` outbox kind; the Telegram service
implements it), checks every step's result against its expectation, and ends with a verdict (達成 / 部分達成 / 未達成).
**Enforced:** one unfinished plan per project; a step is a registered `read` or `change` tool or a declared job (`run_job`),
never a `propose_*` tool and never a code change — **changing code always goes through the Agent Loop**
(`HARNESS_TASK_START::`), never through a plan; a step keeps its tier's rules, so a job or `change` step waits for the user's tap
or an allowed-list match, and a command the command policy forbids is refused at creation and at run time; the plan-wide
"核准這個計畫的所有步驟" button only answers the confirm cards of steps created from the plan itself (the step list cannot change
after creation); expectations are checked by rule first (`ok`, `contains`, `not_contains`, `regex`) and only `model`
expectations use one fresh tool-less model call, and a result that call cannot judge clearly counts as not met; a failed or
unmet step stops the plan and asks (重試 / 略過 / 取消 — at most 3 attempts per step), it is never retried or worked around on
its own; a plan older than 24 hours is cancelled; state is persisted, so after a restart a read step that was running is run
again, a step waiting on a confirm card keeps waiting and a result waiting to be judged is judged. **Prompt policy, not
enforced:** when the model chooses a plan over a single action, and that it adds a status-check step after each action.
**Not implemented:** the model revising the remaining steps mid-plan (the user decides after a failure instead), code-change
steps, several plans at once, and plans for chats other than the operator's.

**Cancelling what the Harness is doing (operator decision, 2026-10-12; ENFORCED, `tests/test_service.py`, `tests/test_message_worker.py`).**
`/cancel` (or `/stop`) is deterministic, never goes through a model, and works while a chat reply is being made. It cancels:
the chat reply being made (killed and not redone — unlike a follow-up message, which merges), the project's unfinished plan, every
request still waiting for a tap (their buttons are removed) and the tasks waiting in the queue. **It does not stop what is already
running:** a task, job or sub-agent that has started finishes by itself and is reported as usual; the reply lists which are still
running. A job or step that finishes after its plan was cancelled does not revive the plan. The plan's own "🛑 取消計畫" button
cancels only that plan and its waiting requests. **Not implemented:** killing a running task or job.

**Abandoning a task stops its CLI, and a long run is announced (operator request 2026-10-12, after chat_7efeada2's Developer CLI
went on to commit, push and open PR #197 nine minutes after the task had been marked failed, and chat_7fe66edf's 49-minute Developer
stage made the operator send the same instruction again). ENFORCED, `tests/test_task_abandon_and_heartbeat.py`:**
`HarnessService.abandon_task` (the status page's 放棄 button and the dead-edit-lock sweep both use it) first terminates provider CLI
process groups working inside the task's own worktree, then marks it failed. Only a group whose *leader* is a provider CLI
(codex / claude / agy / antigravity / gemini) with its working directory inside the worktree is signalled; this process, its group
and a person's own shell in that folder are not. A task without a worktree (in-place project) is never swept by directory. The
provider CLIs run in their own session, so they can outlive a Harness that was killed rather than shut down; the sweep finds them by
directory, not by a pid the Harness no longer has. `report_long_running` (a serve-loop duty) sends one chat note when a live
non-schedule task has had no stage finish for 10 minutes, then at most one more every 20; it says the task is still running and not to
re-send the instruction. **Not implemented:** stopping a *running* task that still has a live Harness thread (the button refuses it,
and `/cancel` does not touch it), sweeping orphan CLIs automatically at startup, and the note's state surviving a restart.

**Other unfinished work is shown to a new task, and a chat task on a covered issue waits for a tap (operator request 2026-10-12, after
three tasks on Beri#194 each started blind to the others and produced two pull requests for one issue). `other_work.py`; ENFORCED in
`tests/test_other_work.py`.** Every task the Harness starts or resumes gets an `other-work` context item (content type `context`, so the
Controller reads it too) listing the project's other unfinished work from the last 7 days: tasks running now (schedule runs excluded),
tasks waiting in the queue, and tasks whose own worktree has commits not yet in its base branch or uncommitted edits (stopped, failed,
waiting for a human, or finished but unmerged). It reads the worktree's real `HEAD` and `git status`, not the branch name the Harness
recorded, because a CLI can switch branches (chat_7efeada2 did). **Prompt policy, not enforced:** that a model that sees the list
inspects the other branch and continues or avoids it instead of redoing it. **Enforced:** when the chat model asks to start a task whose
goal names an issue number (`#194`, `issue-194`, `gh:owner/repo#194`, `/issues/194`) that another listed task also names, the task is
not started; a card offers 照常開始 / 取消. A goal without an issue number is never held (it only gets the list). The hold lasts 30
minutes and lives in memory, so a restart drops it and the request has to be made again. Schedules, the unattended scanner and approval
resumes are never held. **Known limits:** "merged" means merged into the local base branch, so work whose pull request is still open on
the remote but was merged locally is not listed, and a squash- or rebase-merged branch still looks unmerged; remote pull requests are not
queried. **Not implemented:** continuing from the other task's branch (a new task still starts from the base branch), and a
force-refusal rather than a tap.

**Interrupted tasks are recovered at startup, and a restart script refuses to kill a running task (operator request 2026-10-12, after a
force-restart left chat_be448f5b's Developer CLI running with no thread, a held slot and no word to the operator). `recover_interrupted_tasks`,
run once by `serve` right after the "已啟動" message; ENFORCED, `tests/test_startup_recovery.py`.** A conversation still marked
active/running (last 14 days) whose last activity predates this process and which no thread here runs was interrupted: the Harness stops the
provider CLI left in its worktree (same narrowing as abandoning), marks it failed, frees its slot, and sends one message per project with each
task's goal, last finished stage and what its worktree holds (commits not yet in the base branch, uncommitted edits, or nothing). It is written
to the chat log too. A schedule's interrupted run is released quietly, because a stale schedule lock would skip that schedule for good. Slot
locks taken before this process started with no thread behind them are released; locks taken after it started are never touched. **The task is
not resumed** — what happens next is the operator's call. `scripts/restart-harness.sh <Label|all> [--force]` checks `workspace_locks` and skips
a Harness with a task running unless forced. **Not implemented:** a drain mode that stops taking tasks and waits for the running ones to
finish before exiting (a normal stop still waits only the 119-second grace period, then kills the CLI), automatic resume of an interrupted task,
and recovery for a Harness that is never started again.

**Files sent in chat (operator decision, 2026-10-12; ENFORCED, `tests/test_attachments.py`, the media tests in
`tests/test_telegram_service.py`, `tests/test_api.py`, `tests/test_chat_command_media.py`).** The Telegram service relays photos,
documents, voice, audio, video, video notes and animations (up to Telegram's 20 MB bot limit; a file it cannot fetch is
reported to the person, never silently dropped) as base64 in `POST /v1/messages`; a message with files and no words is still
a message. The Harness (never a path named by a service) saves each file under `<workspace>/.harness/inbox/` — mode 0600, name
sanitised so it cannot leave the folder, deleted after 7 days — at most 10 files and 40 MB per message. Files sent in one burst
are answered together. For the chat model: images are looked at (Claude reads the path with a Read permission limited to the inbox; Antigravity reads
the path from its working folder — verified 2026-10-12 for a PNG and a PDF; Codex gets `-i`); a video gives up to 4 frames plus its transcript; audio and voice are transcribed (`HARNESS_TRANSCRIBE_COMMAND`,
default the `whisper` CLI **only when a model is already on disk — the Harness never starts a model download itself**; without a
transcriber the model is told it could not hear it); text-like files and docx/rtf/doc (macOS `textutil`) are put in the prompt,
clipped; a PDF is readable by Claude and Antigravity (they read the path themselves) but not by Codex; anything else is saved and acknowledged as unreadable. The
prompt says file contents are untrusted data. **Not enforced:** that the model reports only what it actually saw or heard.
**Not implemented:** PDF text extraction for Codex, xlsx/pptx, files in the status page.

**Installing what is missing, only with the user's consent (operator decision, 2026-10-12; ENFORCED, `tests/test_capabilities.py`
and the install test in `tests/test_service.py`).** The chat model's `request_install` (tier `propose`) only records a request; the
user sees a confirm card with the source, the exact procedure, the reason and an honest warning, and nothing is installed until they
tap "確認安裝". **Every install needs its own tap — no "always allow", and a forged always-allow tap is just a confirm.** Kinds, each a
fixed procedure and never a shell string the model wrote: `pip` / `npm` into `.harness/capabilities/{python,node}` (isolated; names
that are paths, URLs or options are refused), `brew` (global — the card says so), `model_file` (https URL plus a required SHA-256; a
mismatch is deleted), `script` (an https script, i.e. "curl | sh", downloaded, hashed and run with `sh`), `git` (a clone into
`.harness/capabilities/src/<name>` plus an optional one-line install command) and `skill` (a SKILL.md saved to `.harness/skills/`
and offered to the chat model only, never the Developer; the card shows its content). Arbitrary https URLs and git sources are
allowed **because the user confirms each one**. **Never, whatever the consent:** `sudo` / `doas` — a script that mentions them is
not run, a git install command is checked with the command policy (no sudo, deletion or system settings), and a shim named `sudo`
that fails is first on the PATH of everything an install runs; uninstalling is deletion and stays the user's own. **Honest limit:**
a downloaded script or a package's own install step can still do whatever the user's account can; the card says so, and the SHA-256
of what ran, its source and the reason are recorded in `.harness/capabilities.json`. Installed things are put on the PATH of jobs
and of the transcriber, which is not permission to run them: use still goes through the usual tiers. The Harness never starts a
model download by itself (the whisper model for voice and video is the first thing it will ask for). **Prompt policy, not
enforced:** that the model asks instead of telling the user to install things. **Not implemented:** updating or removing installs,
and installs in a plan step.

**One-off model for a task (operator decision, 2026-10-11).** The operator can name a model for a single task in
chat ("請用 opus 5.5 設計這個頁面"); the chat model writes `model=<model>::` (or `model=<provider>/<model>::`, alongside
`repo=`) in front of the `HARNESS_TASK_START::` goal. **ENFORCED** (`tests/test_model_override.py`): the prefix is routing,
not part of the goal; it applies to the develop skill, as its first candidate, with the project's own chain for that
skill behind it, so any failed call of the chosen model (not only capacity errors) continues on the defaults; nothing is
written to `models.toml` or the role-profiles file, so the next task is back to the defaults; the permission and context
shape of the Developer are unchanged; an unresolvable provider refuses the task before a slot is taken. It is
carried by the goal text, so queued tasks, schedules and approval resumes keep it. **Prompt policy, not enforced:** that the chat
model adds the prefix only when the operator names a model. **Not covered:** the chat reply itself and the
other skills (the Controller, plan, review) use their configured models unless the goal also names them with
`plan=<model>::` or `review=<model>::` (same rules; `develop=<model>::` is `model=`); a chosen model that succeeds with
unparseable output does not fall back (the stage stops as for any invalid output).

**Controller can end a task with nothing to do (2026-10-11).** At the start stage the Controller may choose
`no_action_needed` (goal already met, or the operator said the current state is acceptable): the run ends `succeeded`
and the reason is reported, with no new approval. `stop` and `ask_human` still end as `needs_human`; before this action
existed, an operator answer of "already fine" came back as a new question each time (beri-android#140, four rounds).
**Prompt policy, not enforced:** that the Controller picks it instead of `stop` in that situation.

**Skills for the chat and the Developer (operator decision, 2026-10-11).** Claude CLI loads installed skills
(`~/.claude/skills`, the project's `.claude/skills`) but, headless, nobody can approve the `Skill` tool, so it was
refused. **ENFORCED** (`tests/test_cli.py`): every Claude chat call and the Claude Developer (`acceptEdits`) pass
`Skill` in `--allowedTools`. The read-only roles (Controller, Planner, Planner-escalation, Red Team) do not get it.
**Unverified against a live model:** that the model then calls a skill; Codex needed no change (its skills come from
`CODEX_HOME`) and whether its Controller/Red Team `--ignore-user-config` calls skip skills is not checked.

**Chat reply limits and failure log (2026-10-11).** One chat reply may run up to 600 seconds per candidate model
(`CHAT_REPLY_TIMEOUT_SECONDS`; it was 60, which cut off a reply that was still looking things up — an Antigravity
MemTrace lookup measured 111 seconds). A failed chat call (timeout, non-zero exit, or exit 0 with no text — reported as
"結束碼 0 但沒有任何文字輸出") appends one JSON line to the workspace's `.harness/chat-failures.log` with the provider,
model, return code, and the tails of stdout (2000 characters) and stderr (1000). **Enforced:** neither the prompt nor the
command is ever written there, the file is trimmed to its last 100 lines once past 1 MB, and a logging failure never
affects the reply. **Not done:** the Claude chat call still has the whole Claude Code toolset and the Skill tool, so a
small model may try to write files or use built-in scheduling instead of answering.

**Permanent model changes from the chat (operator decision, 2026-10-11).** The chat model can propose a lasting model
change with the ops tool `propose_model_change` (target: `chat`, `digest`, `archive`, `recall`, or a role:
`controller`, `plan`, `plan-escalation`, `review`, `develop`; the older role names are read as these). **ENFORCED** (`tests/test_model_override.py`,
`tests/test_service.py`): the tool only records a request (valid 10 minutes); nothing is written until the operator taps
「確認變更」; only the target's *primary* provider/model changes (fallbacks, reasoning effort, permission and context
policy stay), written to `models.toml` or the workspace's own roles file exactly as the status page does; a role change
whose current settings the new provider does not accept is rolled back; a double tap applies once. A Harness without its
own roles file cannot change a role (the packaged default is shared). **Prompt policy, not enforced:** that the chat model
uses this tool only for a lasting change and `model=` for a single task. **Not allowed (operator decision, 2026-10-11):** changing fallbacks or effort
from chat; those stay on the status page.

**Choices from the chat (operator decision, 2026-10-11).** When the chat model needs the operator to decide, it ends its reply
with one `HARNESS_CHOICES::{"question":…,"options":[{"label":…,"text":…}]}` line (one line of JSON, 2–6 options, label up to
30 characters, `text` the sentence the pick stands for). The Harness strips the line, sends the reply with the options as
buttons plus a "略過" button, and **keeps the question in `chat_questions` until it is answered, dismissed or 7 days old**, so
a chat that scrolled past it loses nothing; it is also listed on the status page ("待你選擇", with its option buttons and a
dismiss button). **Enforced:** an unusable line (bad JSON, fewer than 2 options, no question) leaves the reply as plain
text; a pick or dismissal applies once (a second tap says it was already answered); picking an option is processed exactly as
if the operator had typed its `text` to the chat (so any confirm button that follows still needs its own tap), in the
background. **Prompt policy, not enforced:** when the model chooses to offer choices. **Not implemented:** reminding the
operator in the chat about a question still open.

**"Waiting on you" on the status page (operator decision, 2026-10-10).** Each project's status carries a `waiting`
block, shown as one banner above the tabs with a total ("有 N 項在等你處理"): pending approvals (a question with a
decision card shows its options and an answer box; a bare approve of a question stays refused, see
`INFO_NEEDED_REASONS`), unexpired confirm-button requests (jobs, scope changes, GitHub actions, orders — to be tapped
in the chat), and **interrupted tasks** (a conversation still marked active/running with no live thread in the
in-flight tracker for 10 minutes, shown with whether it holds the workspace lock). The page can answer a question
(`clarify` with text, or `choose` an option) and abandon an interrupted task (marks it failed and frees its slot;
refused for a task the tracker says is running). The overview tab shows the **Issue list**: open / queued / held
counts from the last scan pass's snapshot (`scan_snapshots`; the page itself never calls GitHub), each held issue
with why. Whether the workspace is held is read live from the lock table (never stored): the page names the holder and, when
the in-flight tracker shows no running thread for it, says scans are being skipped because of it. **Not enforced:** the
10-minute and 7-day thresholds are display heuristics; a stale "interrupted" mark can still appear for work that
ended without updating its conversation.

**GitHub writes from the chat (operator decision, 2026-10-10).** A chat model's own GitHub connector cannot write:
`codex exec` runs with approval `never`, so every write tool call is refused (an issue could be neither opened nor
closed that way). The chat instead calls `propose_github_action`, which only records a `job_requests` row of kind
`external_action`; the Harness shows the exact action (every free-text field in full, and for an existing issue/PR
its current title) with a confirm button and runs the `gh` command itself after the tap, reporting the result.
`github_actions.py` holds one table entry per action — `issue_create`, `issue_close`, `issue_reopen`,
`issue_comment`, `issue_label`, `pr_comment` — so adding one is a table entry, not new plumbing. **Enforced:** the
repo must be the project's `github_repo` or one of its repos' (nothing to configure; checked at the proposal and
again at the tap); every field is bounded (title 200 characters, body 8000, comment 4000, at most 10 labels);
`gh` is run with an argument list, never a shell; nothing happens before the tap; a double tap runs once; an
unanswered request expires after 10 minutes; a project with no declared repo does not get the tool. **Not
offered, deliberately:** merging a PR, deleting or editing an issue, repo settings. **Prompt policy, not
enforced:** that the model uses this tool instead of its own connector. Actions run under the `gh` login of the
account running the Harness; an unassigned open issue is then a scanner candidate like any other and still needs
the operator's approval to be worked on.

**Orders proposed by the chat model (explicit operator decision, 2026-10-05).** For a project
listed in `HARNESS_TAIWANTRADE_ORDER_PROJECTS` the chat model may *propose* a limit order
(`create_order_intent`); a human must confirm it. ENFORCED in code: the tool only creates a
TaiwanTrade order intent, which never reaches the broker; the one-time confirmation token goes to the
harness database and never to the model; the model has no tool to place, confirm, cancel or amend;
only a button tap relayed by a service's allowlisted chat sends the order (`POST /trade/orders`), at most
once per intent; an unconfirmed proposal expires and is reported as not placed; the result of every
confirmed, cancelled, failed or expired order is sent to the human and written to the chat log;
Agent Loop roles never get the tool; a per-order value limit (default 100000 TWD) applies on top of
TaiwanTrade's own risk limits. A cancellation is proposed the same way (`request_order_cancel`) and sent
only on the human's tap (the Harness makes the `DELETE`). **Not implemented:** amending orders, market orders.

**Tool-first rule for working roles (2026-10-09, operator decision).** Planner (and its revision/escalation),
Developer (and its revision) and operational runs are shown a tool catalog built from the TaiwanTrade tool
definitions (`tool_catalog()`), including what Agent Loop roles do NOT have (the order tools; endpoints not
listed, e.g. chart images). They are told to use a listed tool instead of calling the API or reading a key
file, and, when no listed tool covers the need, to stop with `needs_human` and a decision card rather than
build their own client. **Enforced:** the catalog content always matches the served tools. **Prompt policy,
not enforced:** that a role checks it and stops; nothing scans code for direct API calls or key-file reads,
and the key file stays readable. Antigravity's MCP registration (`agy mcp add`) is still manual, but the Harness now checks it at startup (`agy_mcp_check.py`): a missing, disabled or mis-pointed `taiwantrade`/`memtrace` server is printed, logged and sent to the operator (through the outbox) with the `agy mcp add` command to fix it. It only reports; it never registers anything and never blocks startup.

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
chat message then shows the card instead of the raw artifact dump, with one button per option
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

- *Schedule results* (`HarnessService._schedule_delivery`): `push`, `digest_only` or `pause_and_notify`.
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

The review is only wired in by the serve command (`serve`); without it, behaviour is unchanged.
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
row and is announced to the operator — **without** an acceptance prompt (explicit operator decision,
2026-10-11, superseding the earlier 驗收通過/還沒完成 buttons): the Harness does not wait for anyone to look at the
result, and the node stays `status:claimed-done` (a claim, never an acceptance by the operator). If the
operator is unhappy with a result they say so in chat and it is handled as a new task. The buttons on
messages sent before this change still work, and past rejections are still kept as `task_claim` decisions
in the Controller's precedent ("宣告完成，被你退回"). The converge prompt tells the Controller that a human
looking at the result is not a condition for finishing; `ask_human` is for real decisions only.
**Auto-publish (enforced):** a scope file may name one of its own declared jobs as `- publish_job: <name>`;
after a task finishes through converge the Harness runs that job itself — no confirm tap, since the operator
chose the command by declaring it — and appends its output (e.g. the preview address) to the announcement.
A name that is not a declared job is ignored; a failing job is reported and never blocks the report.
**Dead edit lock (enforced):** the edit lock is only held while a task's thread runs, so a holder that no
thread in this process is running for (and that has held it over `DEAD_LOCK_GRACE_SECONDS`) is abandoned and
its lock released when the next task asks, and the operator is told. The chat model is given the running
tasks and the last auto-publish output as context. Operational and schedule runs never reach
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

> **LEGACY, being retired.** This section and "Gate and adoption boundary" describe the loop the Harness runs
> today. The target is in "Safety envelope and project-owned loops": no Harness-owned stages for ordinary
> code, one final gate for tools. Until that ships, this is what runs; do not extend it.

The Controller is the only role; plan, review and develop are skills it runs (see "One Controller, stage
skills" in `docs/architecture.md`). **ENFORCED** packaged primary profiles (packaged defaults — see below for
what's actually validated):

```text
controller          Codex gpt-5.6-luna        read-only        the only role
plan                Claude sonnet             read-only        skill
plan-escalation     Claude opus               read-only        the plan skill's stronger-model mode
review              Codex gpt-5.6-sol         read-only        skill (G1 and G2)
develop             Antigravity Gemini        workspace-write  skill
```

A skill's identity is its job, not a vendor pin. Every profile's provider/model is
project-configurable via `HARNESS_ROLE_PROFILES_FILE_<PROJECT>`; the table above is only the packaged
default a project gets if it doesn't override anything. What **is** hard-validated regardless of vendor
choice (`tests/test_role_profiles.py`, `tests/test_stage_skills.py`):

- only the `develop` profile may hold `workspace-write` — the Controller and every other skill stay `read-only`;
- each profile's `context_policy` shape (Controller only ever gets a compact loop snapshot, `review`
  only gate-scoped evidence, `develop` the accepted plan plus repo, etc.) — this is an information-
  flow boundary, independent of which model is behind the skill;
- `review` and `plan-escalation` may carry a fallback chain; every fallback is recorded on the stage result.
  Pointing `review` at the same provider/model as `develop` is allowed but logged, because it weakens the check
  in practice (the context is still separate).

**Domain skills and the digest's live state (operator decision, 2026-10-14).** **ENFORCED**
(`tests/test_stage_skills.py`, `tests/test_live_state.py`): a skill with `kind: domain` is rule text for a
background duty, read through `domain_section`. It must not declare a profile, schema, permission or modes
(refused at load), so only the `develop` stage skill can ever hold `workspace-write`, and it is never in the
Controller's catalog. The nightly digest's rules are the first (`skills/digest-rules/SKILL.md`); the prompt is
byte-identical to the former inline text when no live state is given. **ENFORCED**: a project may declare
`- digest_state_queries: list_orders, get_positions` in its scope file; before each digest the Harness calls those
read-only TaiwanTrade tools (argument-free ones only, key from `HARNESS_TAIWANTRADE_API_KEY_FILE`) and gives the
digest the replies as `[live:<tool>]` blocks, which it may cite as `state_refs` to review a carried item as `stale`.
Default is none, so projects without the line or the key are unchanged; a failed query reads as unknown and cannot be
cited. **Not done:** other duties (archive, gardening, promotion, work review, trigger review) still carry their
rules in Python; moving them is to be decided after a few nights of digests show the pilot helps.

**TaiwanTrade gateway for scheduled scripts (operator decision, 2026-10-14).** **ENFORCED**
(`tests/test_taiwantrade_gateway.py`): a scheduled job is a script run by a model CLI whose environment is an
allowlist with no secrets, so it could not send `X-API-KEY` (the TAIEX chart job got 401). With
`HARNESS_TAIWANTRADE_GATEWAY_PORT` set (and `HARNESS_TAIWANTRADE_API_KEY_FILE`), the Harness serves a loopback,
GET-only forwarder that adds the key from the key file on each call; the script points its base URL at it
(non-secret `TAIWANTRADE_BASE_URL`, passed via `HARNESS_PASSTHROUGH_ENV`). Only the read-only tool paths of
`taiwantrade_mcp.TOOLS` plus `/api/v1/charts/taiex` are forwarded; orders, other methods and other paths get 403/405.
The key never enters a model-visible environment. Any local process can use the gateway, which is the same read-only
reach the MCP tools already give. **Not done:** a per-caller token for the gateway.

**No repeated question after a resume (operator request, 2026-10-14).** **ENFORCED as instruction only**
(`tests/test_loop.py`): when the goal carries a `Human clarification:` (what a resumed task gets), every Controller
stage is told to treat that answer as settled, never ask the same or an equivalent question again, and, if the answer
gives the operator a step, finish with that step named as pending on them. **Not enforced in code:** a model that
ignores the instruction can still re-ask; a deterministic duplicate-question check is not built.

**Goal plan and completion check (operator request 2026-10-13).** **ENFORCED**
(`tests/test_goal_plan.py`, `tests/test_loop_goal_plan.py`): the Controller plans phases for the user's whole
request (skills per phase: plan, develop, or both; review always follows); a task is reported `succeeded` only
when the Controller's final `goal_check` answers every success criterion and lists none unmet; a missing or
partial check stops the task as `needs_human` ("only partly done") and the unmet items are named; at most six
phases per run; the user's own message reaches the Controller verbatim (`user_request`) and follows the
conversation across resumes. **Prompt policy, not enforced:** that the Controller's criteria really cover
everything asked and that its evidence for "met" is honest; the Harness checks the shape and coverage, not the
truth. A resumed run's revised phases (added mid-run) are re-executed rather than reused.

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
  the operator an alert (through the outbox) via `AgentLoopRunner.alert_callback` — even when no fallback
  exists — naming the failed and (if any) substitute provider/model.
- `authentication` — the provider's login has expired (e.g. Claude's `Failed to authenticate: OAuth
  session expired`). Like `cli_unavailable`, the request was never evaluated: it ignores a profile's
  `fallback_on`, sets no cooldown (the primary is retried on the next stage, so a re-login recovers
  at once) and always alerts the operator to log in again. The Claude adapter surfaces the
  `is_error` result text as the execution error so this can be recognised.

`context_limit`, timeout, network, permission, configuration, schema, safety and
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

> **LEGACY, being retired** (see the note under "Role contract").

**POLICY_ONLY** (operator decision 2026-10-09): G1 and G2 are told to PASS unless the plan or change violates safety/security (secret or identifier exposure, unauthorized external side effect, destructive action, off-limits access, bypassed control); everything else is a low/medium finding on a PASS. They are told not to require a git repository or diff. Only the prompt carries this; the verdict stays the model's and the Harness does not override it. **Tiers** (same date): the Planner marks each acceptance criterion `[必要]` (essential) or `[建議]` (hardening) up front; G1 gates only `[必要]` and safety, and after the plan was revised once it rejects only for `security`. What G1 notices beyond that is returned as findings with severity `defer…` (or low/medium on a PASS); the Harness collects them (`deferred_findings`, max 12) and hands them to the Developer and G2 as a `deferred-notes` context item, which G2 must not reject over. Plan revisions fix only blocking findings and may not add scope. All prompt/convention based; no schema change.

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
