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
- Keep the default role boundary intact: Luna controls, Sonnet plans every risk level, Opus is a
  reasoning-gap escalation, Sol red-teams read-only, and only Gemini development may write.
- A local loop status never proves that MemTrace changed gate, blocked, rejected, reject-count, or
  completed state.
