# CLI execution sources

A provider selects an adapter and protocol. A model selects what that CLI should run. An execution
source selects one operator-managed executable/wrapper and its login environment. Multiple sources
can use the same provider and model; they do not create additional provider types or grant repo access.
All execution remains CLI-only.

## Configure on the status page

1. Open **全域 → 執行來源**. Register a stable source ID, display name, provider, executable path,
   quota-pool ID and maximum concurrency. A source ID's provider is immutable; disable an obsolete
   source rather than changing its identity. Changes retain an audit revision and reject stale edits.
2. Open each Harness's **設定 → 允許使用的執行來源** and grant the sources it may use. Registering
   a global source alone does not grant any Harness permission to call it.
3. In a primary-agent, background-duty or skill model editor, select a provider, execution source and
   model for each row. For example, work Codex/model A → personal Codex/model A → work Claude/model B.

The registry and capacity state live in `~/.memtrace-harness/execution-sources.sqlite3` by default.
`HARNESS_EXECUTION_SOURCES_DB` can relocate the file. Harness processes sharing this file share capacity
and cooldowns. The web service only proxies authenticated Harness API calls; it never opens the registry.
These are host-local facilities, not a distributed scheduler across machines.

## Wrappers and credentials

The command is one executable path or binary name, never a shell command with arguments, an alias,
an endpoint, or a credential. Absolute paths are preferable for launch agents. A wrapper must select
its own login/configuration environment and `exec` the actual provider CLI with `"$@"`, preserving
stdin, stdout, stderr, cwd, arguments and exit status. It must not launch a background model process
and exit before that process finishes. Account login and credential storage remain the CLI/operator's
responsibility. The Harness never reads or copies account credential files.

Different command names do not prove that different accounts are logged in. The operator must bind
sources to the appropriate accounts and group sources sharing actual limits into the same quota pool.
The registry's enabled/idle status is not evidence of valid login, available model access, remaining
quota or a provider reset timestamp. Model menus remain provider-level menus; actual calls establish
whether a source can use a particular model.

Existing provider-only settings use their original `HARNESS_*_COMMAND` executable and environment.
They remain compatible and are **not** subject to the named-source scheduler. Select named sources
throughout a chain when shared account capacity management is required.

## Workspace settings

Per-Harness grants and model choices stay in `.harness/models.toml`:

```toml
[execution_sources]
allowed = ["codex-work", "codex-personal", "claude-work"]

[chat]
provider = "codex"
source = "codex-work"
model = "your-model-id"
fallbacks = ["codex@codex-personal/your-model-id", "claude@claude-work/sonnet"]
```

`digest`, `archive`, `audit` and `recall` use the same optional `source` setting. When one duty inherits
another duty's model chain, it inherits the chain's sources as well. Service-owned Controller calls use
the primary agent's complete chain, including sources. Utility/JSON-repair calls use its selected source.
A source-less row explicitly means the original provider command; it does not inherit another row's account.

Skill profiles in `.harness/roles.toml` accept `source` on both the primary profile and fallback entries:

```toml
# Add to an existing profile; retain its permission and context policy.
[profiles.develop]
# ...existing required fields...
provider = "codex"
source = "codex-work"
model = "your-model-id"

[[profiles.develop.fallbacks]]
provider = "codex"
source = "codex-personal"
model = "your-model-id"
reasoning_effort = "high"
quota_bucket = "legacy-compatible-label"
```

For a named source, its registered quota pool overrides the legacy profile's `quota_bucket` label.
A one-task model override can explicitly name `model=codex@codex-personal/your-model-id::…`; an override
that keeps the provider retains the selected source unless another source is explicitly named. A provider
change clears the old provider's source. Permanent source changes can be proposed by the existing model
change tool but still require their own confirmation; they cannot register or grant new sources.

## Runtime boundaries

- The source must match the provider and be allowed by the invoking Harness. Workspace grants and
  source enablement are checked again before each CLI launch. Revoking permission or disabling a source
  stops subsequent calls, while an in-flight call finishes normally. Changing a captured command/provider/
  quota binding rejects the old invocation instead of silently running a different executable.
- SQLite leases serialize admission across processes. Sources in one quota pool share the minimum
  concurrency limit of that pool's enabled sources. A busy source returns `source_busy` without starting
  another CLI or marking its quota exhausted. The declared fallback chain may try its next candidate;
  without a usable candidate, the stage stops. There is no hidden account rotation or background retry.
- Capacity errors cool the shared pool for 30 minutes. Authentication and missing-executable errors cool
  only the affected source for 5 minutes. The operator can clear cooldown for a later attempt; that does
  not terminate a live lease or prove login has been repaired. Named-source cooldowns are global, while
  source-less legacy calls retain the previous per-Harness availability behavior.
- Leases cover the subprocess's whole lifetime and release on completion/timeout. An expired lease for
  a still-live child retains capacity. Interrupted task cleanup recognizes a custom wrapper only when
  its process-group leader PID has a recorded lease, its command matches the registered wrapper, and
  it is working inside that task's directory. Normal provider-process recognition remains supported.
- Source changes do not alter sandbox flags, writable roots, stage permissions, review isolation or
  context policy. Development fallback remains sequential and preserves the same worktree; a generic
  write-capable timeout or test failure does not authorize switching accounts and replaying edits.
- Local traces keep canonical provider, actual `execution_source_id`, model, quota pool and original
  provider output. Pipeline/detail views show the source. Plain dialogue usage remains unavailable;
  choosing a different account never turns missing usage into zero.

Validation uses injected subprocess output and disposable databases, with no model calls required.
