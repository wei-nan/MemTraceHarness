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
| `.harness.env` | this Harness's settings and secrets | 0600 |
| `.harness/harness.sqlite3` | its store (turns, approvals, schedules, outbox, ...) | |
| `.harness/traces/` | raw provider output, git worktrees | |
| `.harness/roles.toml` | its own role profiles (optional; else the packaged default) | |
| `harness-scope.md` | the project: MemTrace workspace id, off-limits, jobs, worker slots | |

When the folder is a git checkout, `.harness.env` and `.harness/` go into `.git/info/exclude` (the
project's own `.gitignore` is not touched), and `.harness.env` / `.harness` are always in the
project's off-limits list.

`.harness.env` holds only what the Harness needs: `MEMTRACE_MCP_URL`/`MEMTRACE_API_TOKEN`, the CLI
commands, the chat / digest / recall models and fallbacks, `HARNESS_MEMORY_WORKSPACE_ID`, approval and
schedule settings, and the API's `HARNESS_API_HOST` / `HARNESS_API_PORT` / `HARNESS_API_TOKEN`. There are no
per-project suffixes. A service's settings (bot token, chat ids) are never in it — `doctor` flags them if they are.
Changing a model from the status page writes the file and takes effect in the running process at
once.

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

```bash
memtrace-harness migrate --env .env                 # dry run: what would be written where
memtrace-harness migrate --env .env --apply         # write it
memtrace-harness migrate --env instances/project-c/.env --apply --services-dir ~/.config/memtraceharness   # another legacy instance
```

For each project in the index it writes `<project>/.harness.env` (suffixed settings made plain,
a fresh API token and the next free port), copies that project's slice of the shared store and its
raw traces into `<project>/.harness/` (rows of other projects are dropped; the operator's global
preferences stay; old Telegram `(chat, message)` ids become `tg:<chat>:<message>` references),
copies its roles file, writes `<services>/<project>/telegram.env` when the project had a bot of its
own, `<services>/web.toml`, and launchd plists under `<services>/launchd/`. A project that shared the
global bot is reported: a bot now serves exactly one Harness. The legacy files are never changed or
deleted, and an existing `.harness.env` is never overwritten.

Switching over: stop the old gateway, copy the plists into `~/Library/LaunchAgents` and
`launchctl bootstrap gui/$(id -u) <plist>`; `memtrace-harness -C <workspace> doctor` first.
Scripts that read the old `.env` for a bot token (e.g. a daily-report script) should post to the
Telegram service's relay instead.
