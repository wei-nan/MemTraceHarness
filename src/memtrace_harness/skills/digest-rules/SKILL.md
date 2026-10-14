---
name: digest-rules
description: The rules the nightly memory digest follows when it turns one day of a project's conversation into long-term memory. Rule text only; the harness still builds the data, validates every citation and writes the result.
kind: domain
---

A domain skill: it holds no profile or permission and is not a stage. Edit the wording here; the JSON shape, the citation check and the
writes stay in memory_digest.py.

## rules

- A user turn may end with （回覆的訊息：「…」）. That quoted text is a message the human swiped to reply to (often a
push from the harness or a script), not the human's own words: use it to understand what the reply is about,
never as a preference and never as evidence of what the human said.

- Every item must cite the turn numbers from THIS day's log that support it. Items without valid citations
are discarded automatically.

- decisions: things agreed or settled. facts: durable project knowledge worth remembering (data, results,
configuration). new_open_items: questions or tasks raised and not finished this day. resolved_open_items:
carried items this day's log shows were resolved, cited by their index and the resolving turns.

- open_item_review: go through EVERY carried open item and say which of these it is. status `decided`: the
log shows the human or the harness actually settled it (reason = what was decided). status `stale`: its
premise no longer holds (for example the position it concerns was sold, the schedule it concerns was
stopped, the failure it concerns was fixed); reason = why. status `needs_operator`: it is still real and
only the human can decide or supply something; reason = the one decision or input needed. `decided` and
`stale` must cite turns from THIS day's log that show it (a `stale` item may instead cite `state_refs` ids
from the schedule state above), otherwise use `needs_operator` or leave the item out. Items you leave out
stay open unchanged. Do not use resolved_open_items for these; it is only for items completed in this day's
log.

- process_lessons: how the harness, models, or tools behaved (for example a model timing out or a step
failing), not project content.

- preference_candidates: ONLY standing preferences the human (speaker user) stated about how they want to be
worked with in future conversations. Cite only user turns. One-off requests, questions about the current
task, and project content are NOT preferences; when unsure, leave it out. scope is "global" when it applies
to every project (e.g. reply language) or "project" when only to this one. category is one of:
language_format, communication, approval, tools_permissions, workflow, other. explicit is true only when the
human stated it as a rule (e.g. 以後, 一律, 不要再, always). These are adopted automatically, so be conservative.
Do not repeat an adopted preference unchanged; when a candidate refines or contradicts an adopted one, list
that one's id in `replaces`.

- preference_retirements: adopted preferences (by id) that the human, in THIS day's log, explicitly withdrew
or contradicted (e.g. 不用了, 那個不要了) without giving a replacement. Cite only user turns. When unsure, leave it
out.

- Write every text field and the summary in Traditional Chinese (繁體中文，台灣用語). One sentence per item. Never
invent anything that is not in the log.

- Use [] for an empty list.

## live_state

Live state queried from this project's own tools when this digest ran (as of that moment, not as of the day). Each block is headed by
its id in square brackets and holds the raw reply of one read-only tool.

## live_state_rule

- A status that the live state above already answers (for example whether an order was sent or filled, or what is held) is a
fact, not an open question: never put it in new_open_items or mark it needs_operator. A carried open item about it may be reviewed
as `stale` citing the block's id (for example `live:list_orders`) in `state_refs`. A block whose query failed carries no id: treat
that status as unknown and do not guess.
