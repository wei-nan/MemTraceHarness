# MemTraceHarness agent entry point

Read `docs/operating-contract.md` and the relevant section of `docs/architecture.md` before
non-trivial changes. The contract distinguishes enforced behavior from accepted policy and planned
work; do not report policy-only behavior as implemented.

Hard project boundaries:

- Claude, Codex, Antigravity, and Cursor model execution is CLI-only. Do not add provider HTTP clients,
  SDKs, endpoints, or provider API-key configuration. The one exception (operator decision,
  2026-10-10): a read-only `GET /v1/models` for the status page's Claude model menu, with a key
  (`HARNESS_MODEL_LIST_ANTHROPIC_KEY`) that is used for nothing else — see "Model list key" in
  the operating contract.
- `MemTraceClient` is only the MemTrace MCP control-plane client; it is not a model transport.
- Preserve raw provider output in the local trace store and write only summaries/references to
  MemTrace.
- Keep one editing provider per run until isolated-worktree fan-out is implemented. (Concurrent
  *tasks* each get their own worktree — see "Concurrent tasks per project" in the operating
  contract — but a single run still has one editing provider.)
- Execution sources select operator-managed CLI wrappers, separately from provider and model.
  Preserve canonical providers and `execution_source_id` in traces; route every named-source call
  through the shared lease/cooldown boundary. A registered source requires a separate per-Harness
  grant and never adds workspace permissions. Do not read/copy account credentials or turn wrappers
  into shell command strings. See `docs/execution-sources.md`.
- Never claim missing token usage is zero. Preserve `complete`, `partial`, or `unavailable`.
- Unit tests must inject subprocess output and must not consume model quota.
- The Harness and its services are separate: `memtrace_harness` never imports `memtrace_services` and
  vice versa; a service reaches a Harness only through its local HTTP API; the Harness holds no chat
  transport, bot token or chat id (see "Harness and services" in the operating contract). Each Harness
  runs against one workspace folder and keeps its environment in that folder's `.harness.env` and its model
  preferences in `.harness/models.toml` — never read or print the former, and never put a secret in a command line or a log.
- Harness output remains draft evidence; it cannot approve its own Agent Loop gate or Improvement
  Loop proposal.
- Operator-enabled proactive observation (`improvement.py`) may queue platform fixes through the
  normal task/worktree/test/delivery path; it never approves an Improvement proposal. Observation
  source exports require opt-in and coordinator grants. Business schedules/preferences in other
  Harnesses remain suggestions. See `docs/proactive-improvement.md`.
- The primary agent owns dialogue, planning, task decisions and goal checks. Its single model chain
  is the historical `HARNESS_CHAT_*` configuration; the persisted `controller` execution slot is a
  compatibility name, not a second agent. Plan, review and develop are skills it runs (`stage_skills.py`,
  `skills/<name>/SKILL.md`). A skill's identity is its job, not a vendor pin: every profile's
  provider/model is project-configurable: the primary agent through its shared model settings,
  and the `plan`, `plan-escalation`, `review` and `develop` skills via a custom role-profiles file (`HARNESS_ROLE_PROFILES_FILE` in the
  workspace's `.harness.env`; the pre-skill names `planner`/`red-team`/`developer` still load). What
  must stay enforced regardless of vendor choice: only the `develop` profile may hold
  `workspace-write` (every other profile stays `read-only`); each profile's `context_policy` shape
  (primary agent gets the project conversation and snapshot, `review` only gate-scoped evidence, etc.). The "independent
  reviewer" guarantee comes from every skill being a fresh, separately-invoked CLI call that is
  handed artifacts, never another skill's conversation, and from those permission/context
  boundaries, not from forcing any skill onto a specific vendor. The primary agent uses
  `primary-session` context (current project conversation plus execution snapshot); legacy low-level
  controller replays may use `loop-snapshot`. Review remains gate-scoped and independently invoked.
- Operator-authorized shared delivery (2026-10-16) is a bounded exception: when the shared
  `worktree-pr-ci-main` policy is enabled, the runtime may push task branches, open PRs, wait/check CI,
  repair in the same worktrees, merge into remote main, close explicit task issues and clean worktrees.
  `delivery.py` owns these operations and verifies actual remote evidence; models cannot self-approve.
  Generic shared notes grant no additional capabilities. All other confirmations remain single-use.
- Every other operation requiring confirmation needs its own tap. Never auto-approve from a stored
  allowlist or a plan-wide grant; old callbacks granting permanent or plan-wide approval are refused.
- Decentralized autonomous authorization (operator decision, 2026-10-16): each Harness encounters
  capability, tool, or scope gaps directly in its own workspace and must ask its own operator via
  single-use confirmation cards (`propose_scope_change`, `request_install`), never asking another
  Harness over A2A or through a coordinator to proxy or grant tool permissions.
- A task is reported done only when the Controller's goal check answers every success criterion it
  wrote for the user's whole request (`goal_plan.py`); never mark it `succeeded` on the strength of
  one phase.
- A local loop status never proves that MemTrace changed gate, blocked, rejected, reject-count, or
  completed state.
