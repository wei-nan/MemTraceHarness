# Harness workspaces and services

Status: the shape described here is **ENFORCED** by the code and tests named in each section (the
rules themselves are summarized in the operating contract, "Harness and services").

## The idea

The old Harness was one process serving every project from one `.env`, one SQLite file and one
Telegram long-poll loop, with a status page that read the same database. Settings were
`HARNESS_*_<PROJECT>` suffixes in a shared file, the bot token was readable by every model CLI the
process started, and "what the Harness does" and "how it reaches a person" were one 2800-line class.

Now:

- **A Harness is the model-based work of one project**: roles and models, the Agent Loop, the
  scanner, schedules, memory, the nightly digest. It runs against one **workspace**.
- **A service is a transport or a view**: the Telegram bot, the status page. It is a different
  process with a different env file and it only talks to a Harness over that Harness's local API.

```
 Telegram ⇄ memtrace-telegram ⇄ ┐                     ┌─ model CLIs (claude / codex / agy)
                                 ├─ HTTP (loopback) ─► memtrace-harness serve ─┤
 browser  ⇄ memtrace-web      ⇄ ┘      bearer token     │                     └─ MemTrace (MCP)
                                                         └─ <workspace>/.harness.env, .harness/
```

## The workspace

A folder, empty or not, git or not (`memtrace-harness init-workspace <folder>`):

| path | what | mode |
| --- | --- | --- |
| `.harness.env` | its environment: secrets, endpoints, CLI paths (`MEMTRACE_*`, `HARNESS_API_*`, `HARNESS_*_COMMAND`) | 0600 |
| `.harness/models.toml` | its preferences: chat / digest / recall models and fallbacks, memory workspace, behaviour | |
| `.harness/harness.sqlite3` | its store (turns, approvals, schedules, outbox, ...) | |
| `.harness/traces/` | raw provider output, git worktrees | |
| `.harness/roles.toml` | its own role profiles (optional; else the packaged default) | |
| `harness-scope.md` | the project: MemTrace workspace id, off-limits, jobs, worker slots | |

When the folder is a git checkout, `.harness.env` and `.harness/` go into `.git/info/exclude` (the
project's own `.gitignore` is not touched), and `.harness.env` / `.harness` are always in the
project's off-limits list.

Environment and preferences are separate files. `.harness.env` holds what the Harness needs to run and
reach things — `MEMTRACE_MCP_URL`/`MEMTRACE_API_TOKEN`, the CLI paths and timeout, the API's
`HARNESS_API_HOST` / `HARNESS_API_PORT` / `HARNESS_API_TOKEN`; `models.toml` holds what the operator
chooses (`[chat]`, `[digest]`, `[recall]` provider/model/fallbacks, `[memory]`, `[behavior]`) and is what the
status page edits — by a comment-preserving text patch, so what you write there survives — taking effect in
the running process at once and never rewriting the file that holds the secrets. The loader reads the real
environment first, then `models.toml`, then `.harness.env`. There are no per-project suffixes. A service's
settings (bot token, chat ids) are in neither — `doctor` flags them, and a preference left in
`.harness.env`, if they are.

## The Harness API

`memtrace_harness/api.py`. Loopback HTTP, `Authorization: Bearer <HARNESS_API_TOKEN>` on everything but
`/v1/health`; the Harness will not serve without a token.

| route | |
| --- | --- |
| `POST /v1/messages` `{text, audience, reply_to_ref?, reply_to_text?}` | what a person said; answered through the outbox (202) |
| `POST /v1/actions` `{id, audience, message_ref?}` | a button tap; returns `{toast}` |
| `GET /v1/events?limit=&wait=` | undelivered outbox events (long-poll) |
| `POST /v1/events/<id>/ack` `{ok, message_refs?}` | the delivery receipt |
| `GET /v1/commands` | the slash commands, for a service's own menu |
| `GET /v1/status`, `GET /v1/changes?since=&wait=`, `GET /v1/turns/<id>` | what the status page shows |
| `POST /v1/admin/<setting>` | model settings, role profiles, approve/reject, preferences |

**Messages are answered once per burst (operator decision, 2026-10-11; enforced).** A plain chat message is handled at once. If the same person sends another plain chat message before the reply has gone out, the reply being made is cancelled — a chat model call in flight is killed, a finished-but-unsent reply is dropped, and anything the model asked to have confirmed meanwhile (jobs, settings, orders still `pending`) is cancelled — and the model thinks again with everything said so far, so there is one answer. Nothing a dropped reply said (task, schedule, note, preference markers) has been acted on, because markers are only read from a reply that goes out. Only the new words are added to the transcript again. A command, a reply to a specific message, or another audience's message is never merged and waits its turn. After `MAX_RESTARTS` (3) restarts for one burst the worker stops interrupting. **Not implemented:** a message that arrives while a *non-chat* step (a command, a started task) is running; it simply waits its turn.

Messages are handled one after another in arrival order by a worker thread, so a slow model reply
never blocks the API.

## The outbox

Everything the Harness wants said is a row in its `outbox` table:

```json
{"id": 17, "kind": "message", "audience": null, "text": "…",
 "actions": [[{"label": "✅ 核准", "id": "approve:appr_1"}, {"label": "❌ 拒絕", "id": "reject:appr_1"}]],
 "bind": {"type": "approval", "id": "appr_1"}}
```

`kind` is `message`, `clear` (remove the buttons of the message `ref`) or `typing`. `audience` is an
address a service handed in earlier (`tg:<chat>`), or `null` for "the operator, wherever they are".

- **At-least-once.** A fetch leases an event for 60 s; an unacknowledged lease lapses and the event is
  offered again; a negative acknowledgement retries up to 5 times, then gives up.
- **`bind`** says what to remember the delivered message for. The receipt's `message_refs`
  (`tg:<chat>:<message>`) are stored on the approval / order / job, so a reply to that message, or a
  later `clear`, finds it. A proposal that never reached the operator goes back to `pending` and is shown again.
- Delivered events are pruned after 48 hours.

## The Telegram service

`memtrace_services/telegram/` — one process per bot, one bot per Harness. Its own env file
(`examples/telegram.env`), kept outside the workspace.

- Long-polls `getUpdates`; **drops every update from a chat outside `TELEGRAM_ALLOWED_CHAT_IDS`** before
  anything is relayed; relays text (with the replied-to message) and button taps; keeps the update
  offset in its own state file. If the Harness is down it does not advance the offset, so nothing is lost.
- Long-polls the Harness outbox and delivers it: `audience: null` goes to every allowed chat, `tg:<chat>`
  only there, any other address is acknowledged and dropped. Long texts are split; buttons ride on the last chunk.
- Optional **relay** (`TELEGRAM_RELAY_PORT`): scripts that used to read the Harness's `.env` for the bot
  token POST `/v1/send {"text"}` or `/v1/send_photo {"path"}` with the relay's own token instead. Photos are read only from
  `TELEGRAM_RELAY_PHOTO_ROOTS`.

## The status page

`memtrace_services/web/` — one process over any number of Harnesses (`examples/web.toml`). It
aggregates their `/v1/status`, pushes the result to browsers over SSE (redrawn when any Harness
reports a change), and forwards each write to the Harness that owns the project. Preference ids are
only unique per Harness, so the page addresses them as `<harness>:<id>`. Loopback by default and no login of its own.

## Running it

Three kinds of process, each its own launchd job: `memtrace-harness -C <workspace> serve`,
`python -m memtrace_services.telegram.main --env-file <telegram.env>`, `python -m memtrace_services.web.main --config <web.toml>`
(`scripts/run-*.sh` wrap them for the foreground). Restarting one never restarts another: bouncing the bot
or the page does not touch a running task, and a Harness restart only pauses delivery until it is back.

## Migrating from the shared `.env`

Order matters; the legacy files are never changed or deleted, so every step before the last can be undone.

1. **Stop the old gateway(s)** (`launchctl bootout gui/$(id -u)/com.memtraceharness.gateway`, and `.project-c`
   if you run it). The copy of the store is an online backup and safe, but a running gateway leaves a
   workspace lock and an `active` conversation in it, and those would be carried over and block the new
   Harness. A bot also cannot be polled by two processes at once.
2. **Dry run** — what would be written where: `memtrace-harness migrate --env .env`.
3. **Apply**: `memtrace-harness migrate --env .env --apply`. A separate legacy instance (project-c) is a second
   run with its own env and a higher first port, into the same services dir:
   `migrate --env instances/project-c/.env --apply` (the status page's `web.toml` is merged, not replaced, and the
   ports the first run handed out are skipped).
4. **Check** each workspace: `memtrace-harness -C <workspace> doctor`.
5. **Install** the plists from `~/.config/memtraceharness/launchd/`: Harness first, then its Telegram service,
   then the page; send a message to the bot and confirm the reply.
6. Keep the old `.env` until nothing else reads it (a script that posts to Telegram with the old bot token should
   move to the Telegram service's relay).

What is carried: each project's environment (MemTrace URL/token, CLI paths, TaiwanTrade settings → `.harness.env`),
its preferences (suffixed chat/digest/recall/memory settings made plain → `models.toml`), its roles file, its slice of the
shared store and raw traces, its bot token and chat allowlist (→ the Telegram service's env), and the bot's update
offset (so nothing is replayed). Every legacy key is accounted for; the store paths and the project index are
replaced by the workspace layout, the status page's port by `web.toml`.

The store: a copy of the shared SQLite file per project (online backup), then everything that belongs to another project
is deleted and the file vacuumed. Global things stay (provider cooldowns, the operator's global preferences). Old
Telegram `(chat, message)` ids become `tg:<chat>:<message>` references and old schedule `chat_id`s become `tg:<chat>`
addresses the first time the new store opens; the old gateway's offset table is dropped. Nothing else needs doing by hand.

**Several repos on one MemTrace workspace** (Beri and beri-android). They are one Harness with several repos
(`.harness/repos.toml`, `memtrace-harness repo ...`): the knowledge base is the Harness's identity, so what belongs
to it — one scanner, one nightly tidy, one workspace lock, one conversation and bot — exists once. `migrate` does
this by itself for scope files that name the same `workspace_id` (the first in the index is the primary repo;
`--primary NAME` picks another): the others become repos with their verify command, GitHub repo and off-limits, their
approvals (resuming in their own repo), decisions, preferences and schedules (gaining a `repo=<name>::` prefix) join
the primary's store, their bots are retired, and their chat transcript and digests stay in the legacy store (the digests
are already in MemTrace). A task works in one repo (`repo=<name>::<goal>`; none = primary); that repo's worktree and
edit lock, verify command and off-limits apply, a read-only Claude role can read the other repos, and no role ever
writes outside the task's repo.

**Not supported:** two Harnesses on one MemTrace workspace — they would each scan the same backlog, tidy the same knowledge base and run tasks on it at once, with nothing to coordinate them. Declare the second repo instead.
