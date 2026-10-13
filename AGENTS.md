# MemTraceHarness agent entry point

Read `docs/operating-contract.md` and the relevant section of `docs/architecture.md` before
non-trivial changes. The contract distinguishes enforced behavior from accepted policy and planned
work; do not report policy-only behavior as implemented.

Hard project boundaries:

- Claude, Codex, and Antigravity model execution is CLI-only. Do not add provider HTTP clients,
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
- Never claim missing token usage is zero. Preserve `complete`, `partial`, or `unavailable`.
- Unit tests must inject subprocess output and must not consume model quota.
- The Harness and its services are separate: `memtrace_harness` never imports `memtrace_services` and
  vice versa; a service reaches a Harness only through its local HTTP API; the Harness holds no chat
  transport, bot token or chat id (see "Harness and services" in the operating contract). Each Harness
  runs against one workspace folder and keeps its environment in that folder's `.harness.env` and its model
  preferences in `.harness/models.toml` — never read or print the former, and never put a secret in a command line or a log.
- Harness output remains draft evidence; it cannot approve its own Agent Loop gate or Improvement
  Loop proposal.
- The Controller is the only role; plan, review and develop are skills it runs (`stage_skills.py`,
  `skills/<name>/SKILL.md`). A skill's identity is its job, not a vendor pin: every profile's
  provider/model — the Controller and the `plan`, `plan-escalation`, `review` and `develop` skills —
  is project-configurable via a custom role-profiles file (`HARNESS_ROLE_PROFILES_FILE` in the
  workspace's `.harness.env`; the pre-skill names `planner`/`red-team`/`developer` still load). What
  must stay enforced regardless of vendor choice: only the `develop` profile may hold
  `workspace-write` (every other profile stays `read-only`); each profile's `context_policy` shape
  (Controller only gets a loop snapshot, `review` only gate-scoped evidence, etc.). The "independent
  reviewer" guarantee comes from every skill being a fresh, separately-invoked CLI call that is
  handed artifacts, never another skill's conversation, and from those permission/context
  boundaries, not from forcing any skill onto a specific vendor.
- A task is reported done only when the Controller's goal check answers every success criterion it
  wrote for the user's whole request (`goal_plan.py`); never mark it `succeeded` on the strength of
  one phase.
- A local loop status never proves that MemTrace changed gate, blocked, rejected, reject-count, or
  completed state.
