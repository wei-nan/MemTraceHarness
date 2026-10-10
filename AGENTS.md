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
- Role identity is the job in the pipeline (route, plan, escalate, verify, implement), not a vendor
  pin: every role's provider/model — Controller, Planner, Planner-escalation, Red Team, and
  Developer alike — is project-configurable via a custom role-profiles file
  (`HARNESS_ROLE_PROFILES_FILE` in the workspace's `.harness.env`). What must stay enforced regardless of vendor choice:
  only the `developer` profile may hold `workspace-write` (every other role stays `read-only`);
  each role's `context_policy` shape (Controller only gets a loop snapshot, Red Team only
  gate-scoped evidence, etc.); and Red Team / Planner-escalation both fail closed with no fallback
  chain. The "independent reviewer" and "consistent behavior" guarantees come from every role stage
  already being a fresh, separately-invoked CLI call — never a continued conversation — and from
  those permission/context boundaries, not from forcing any role onto a specific vendor.
- A local loop status never proves that MemTrace changed gate, blocked, rejected, reject-count, or
  completed state.
