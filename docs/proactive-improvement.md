# Proactive platform improvement

The operator authorized MemTraceHarness on 2026-10-16 to observe other Harness conversations,
task outcomes and schedules, proactively fix platform mechanisms, and retain business-schedule
and preference changes for other projects as suggestions.

## Configuration

Sharing is off by default. In each source Harness's `.harness/models.toml`:

```toml
[observation]
share_enabled = true
```

In the platform coordinator's model settings:

```toml
[improvement]
enabled = true
auto_develop = true
interval_seconds = 1800
sources = ["MemTraceHarness", "Beri"] # status-web target names, not guessed peer names
```

The status page's project settings have separate switches for sharing, coordinating and automatic
development. It also shows source coverage, pending evidence, findings and task states. The source
list and interval can be edited in the file or through the main-token admin API. All read dynamically.
Turning coordination off stops collection/analysis and future dispatch; turning automatic development
off allows observation to continue. Queued tasks recheck these settings and source authorization.
An already-running development is not killed; ordinary confirmation and cancellation still apply.

The platform scope must configure its real `github_repo` and `verify_command`, and the shared
`worktree-pr-ci-main` delivery policy must be enabled before automatic development starts. Missing
requirements appear on the status page. No in-place or unverified delivery fallback is authorized.

## Data flow

`memtrace-web` owns `ObservationRelay`, a background service using only its already-configured
authenticated Harness targets. Every minute it checks for enabled coordinators. Each source is
collected at the coordinator's interval. `GET /v1/observations?after=<cursor>` is main-token-only and
honors the source's sharing switch; `POST /v1/observations` accepts only the coordinator's allowed
source names. Peer tokens cannot use either endpoint. This is a separate operator-authorized
observation channel, not an automatic approval of ordinary A2A questions.

The export includes bounded primary-conversation text, selected task status/recommendation fields
and schedule state. It excludes raw CLI traces, stderr, argv, model-private output, credential files
and observer notification turns. Common pasted token/password patterns are masked on export and
ingest; this is best-effort filtering, not detection of every possible secret. The first collection
contains only the latest 80 dialogue turns, 20 task outcomes and 50 schedules. Later collections
walk at most 80 new turns per interval. A cursor advances only after the coordinator acknowledges
the whole batch; duplicate IDs are ignored. It is not a complete historical transcript export.
Dialogue and task evidence must be dated within the last 48 hours; undated, old and implausibly
future-dated records cannot start fixes. Schedule evidence describes its current snapshot, including
the actual last-run time in its text. Old findings remain historical and cannot dispatch work.

Evidence and per-source cursors/status live in the coordinator's own trace database. No Harness
reads another Harness's database, and services never import Harness code. Unreachable and non-sharing
sources remain explicitly distinguishable. The evidence cache is retained for 30 days after receipt;
local original traces remain in their owning Harness. Removing a source prevents future analysis and
queued dispatch from that source; it does not erase already-received records or findings.

## Decisions and execution

The coordinator checks once a minute, with at most one observation call-chain active. It uses its
configured primary-agent models in a fresh CLI call in a temporary directory outside the repo.
No Harness ops/peer tools are attached. Claude disables tools and external MCP configs, Codex uses
read-only with user config/rules ignored, Antigravity uses read-only plan/sandbox, and Cursor uses
ask/sandbox. An Antigravity plan handoff that cannot finish headlessly times out and follows the
declared fallback; it never switches to accept-edits for observation.
There is no provider HTTP transport, model override or new account rotation. Every attempt preserves
raw output locally, canonical provider/source attribution and unavailable usage as appropriate.
No new evidence means no model call. Successful empty findings mean no notification.

A review considers at most 60 unprocessed events, interleaved across sources, and four findings. Every finding must cite supplied
IDs and 12–300-character verbatim quotes, checked by code. Invalid output consumes no evidence and
retries after the interval. Quotes establish provenance, not the truth of a model's interpretation.
Stable finding keys suppress repeated notifications and work. A later recurrence, supported by new
dated evidence more than a day after a settled case, gets a new finding/task; the old case stays settled.
Suggestions are not silently converted into code or settings changes.

Automatic work is limited to one platform improvement at a time and at most three dispatched cases
in a rolling day. User work and existing queues take priority. A runtime transaction creates the
finding-to-task link and queue entry together. Paused queued entries can resume after re-enablement;
interrupted development is retained for inspection, never blindly replayed. A fresh task's primary
agent must check the current source and evidence, plan, develop and verify through the normal loop.
Its goal is fixed to the current platform repo; it cannot select additional repos via an evidence
prefix. Findings cannot execute tools, commands, alter model preferences, approve confirmations,
or write business schedules/preferences in another Harness.

The normal worktree, permission, independent skill contexts, final gate, tests and shared remote
delivery checks remain authoritative. Task states follow actual runtime results (`delivering` is
pending, not success), including resumed tasks. Observation findings are draft local evidence; they
never approve/adopt MemTrace Improvement Loop proposals or change MemTrace gate state.

Interpretation quality, sensible prioritization and the semantic correctness of a suggested fix are
model judgments, not proven by citation checks. The task prompt instructs preserving security,
confirmation boundaries, current model choices and other projects' business intent. The observer
does not directly adjust live service settings or reload running Harnesses after a merge.
