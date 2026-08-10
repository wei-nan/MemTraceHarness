# MemTraceHarness agent entry point

Read `docs/operating-contract.md` and the relevant section of `docs/architecture.md` before
non-trivial changes. The contract distinguishes enforced behavior from accepted policy and planned
work; do not report policy-only behavior as implemented.

Hard project boundaries:

- Claude, Codex, and Antigravity model execution is CLI-only. Do not add provider HTTP clients,
  SDKs, endpoints, or provider API-key configuration.
- `MemTraceClient` is only the MemTrace MCP control-plane client; it is not a model transport.
- Preserve raw provider output in the local trace store and write only summaries/references to
  MemTrace.
- Keep one editing provider per run until isolated-worktree fan-out is implemented.
- Never claim missing token usage is zero. Preserve `complete`, `partial`, or `unavailable`.
- Unit tests must inject subprocess output and must not consume model quota.
- Harness output remains draft evidence; it cannot approve its own Agent Loop gate or Improvement
  Loop proposal.
- Role identity is the job in the pipeline (route, plan, escalate, verify, implement), not a vendor
  pin: every role's provider/model — Controller, Planner, Planner-escalation, Red Team, and
  Developer alike — is project-configurable via a custom role-profiles file
  (`HARNESS_ROLE_PROFILES_FILE_<PROJECT>`). What must stay enforced regardless of vendor choice:
  only the `developer` profile may hold `workspace-write` (every other role stays `read-only`);
  each role's `context_policy` shape (Controller only gets a loop snapshot, Red Team only
  gate-scoped evidence, etc.); and Red Team / Planner-escalation both fail closed with no fallback
  chain. The "independent reviewer" and "consistent behavior" guarantees come from every role stage
  already being a fresh, separately-invoked CLI call — never a continued conversation — and from
  those permission/context boundaries, not from forcing any role onto a specific vendor.
- A local loop status never proves that MemTrace changed gate, blocked, rejected, reject-count, or
  completed state.
